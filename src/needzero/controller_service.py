"""Headless machine controller lifecycle. This module never imports UI code."""

from __future__ import annotations

import logging
import os
import secrets
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from . import BUILD_CHANNEL
from . import config as cfgmod
from .api import Api
from .config_store import ConfigStore
from .controller_logging import configure_controller_logging
from .controlloop import Controller
from .fans import create_backend as create_fans
from .ipc import Dispatcher, IpcServer, read_token
from .runtime_paths import ipc_token_path, machine_config_path, machine_dir
from .sensors import create_backend as create_sensors
from .single_instance import SingleInstance
from .windows_security import (
    assert_no_reparse_points,
    current_user_sid,
    ensure_ipc_token_access,
    ensure_machine_data_dir,
    is_elevated,
    protect_file_for_user,
    protect_machine_file,
    protected_file_reader_sid,
    require_protected_directory,
    require_protected_file,
)


_LOGGER = logging.getLogger(__name__)
_SHUTDOWN_ACK_GRACE_SECONDS = 0.1


def _secure_machine_config_save(config, path: Path) -> None:
    """Publish machine JSON only after its staged owner/DACL is exact."""
    assert_no_reparse_points(Path(path))
    cfgmod.save(config, path, prepare_temp=protect_machine_file)


def _create_manual_dev_token(path: Path, sid: str) -> None:
    """Atomically create a fresh token; never accept a planted existing file."""
    path = Path(path)
    assert_no_reparse_points(path)
    if path.exists():
        raise FileExistsError(path)
    temporary = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="ascii",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(secrets.token_hex(32))
            handle.flush()
            os.fsync(handle.fileno())
        protect_file_for_user(temporary, sid)
        os.replace(temporary, path)
        temporary = None
        published = True
        ensure_ipc_token_access(path, sid)
        read_token(path)
    except BaseException:
        if published:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _prepare_manual_dev_runtime() -> None:
    """Create protected runtime state only for an explicitly elevated DEV EXE."""
    if not getattr(sys, "frozen", False) or BUILD_CHANNEL != "dev":
        return
    if not is_elevated():
        raise PermissionError(
            "the unsigned DEV controller must be started from an elevated terminal"
        )

    sid = current_user_sid()
    data_dir = ensure_machine_data_dir()
    config_path = machine_config_path()
    token_path = ipc_token_path()
    if data_dir != machine_dir():
        raise RuntimeError("machine data directory resolver mismatch")

    assert_no_reparse_points(config_path)
    if config_path.exists():
        require_protected_file(config_path)
    else:
        _secure_machine_config_save(cfgmod.Config.defaults(), config_path)

    assert_no_reparse_points(token_path)
    if token_path.exists():
        reader = protected_file_reader_sid(token_path)
        if reader != sid:
            raise RuntimeError(
                "existing IPC token belongs to another desktop identity; refusing reuse"
            )
        require_protected_file(token_path, sid)
        ensure_ipc_token_access(token_path, sid)
        read_token(token_path)
    else:
        _create_manual_dev_token(token_path, sid)
    require_protected_directory(data_dir, sid)


def _publish_config(controller, sensors, candidate) -> None:
    """Publish both live consumers or restore both to their prior state."""
    previous = cfgmod.clone(controller.get_config())
    updater = getattr(sensors, "set_manual_ids", None)
    try:
        if callable(updater):
            updater(candidate.sensors)
        controller.set_config(candidate)
    except BaseException:
        rollback_errors = []
        try:
            controller.set_config(previous)
        except BaseException as exc:
            rollback_errors.append(exc)
        if callable(updater):
            try:
                updater(previous.sensors)
            except BaseException as exc:
                rollback_errors.append(exc)
        if rollback_errors:
            _LOGGER.critical("live configuration rollback failed: %s", rollback_errors)
        raise


class ControllerService:
    """Own sensor/fan backends, controller loop and authenticated IPC."""

    def __init__(
        self,
        *,
        config_path: Path,
        token_path: Path,
        sensor_factory: Callable = create_sensors,
        fan_factory: Callable = create_fans,
        controller_factory: Callable = Controller,
        server_factory: Callable = IpcServer,
        config_save_fn: Callable = cfgmod.save,
    ) -> None:
        self.config_path = Path(config_path)
        self.token_path = Path(token_path)
        self._sensor_factory = sensor_factory
        self._fan_factory = fan_factory
        self._controller_factory = controller_factory
        self._server_factory = server_factory
        self._config_save = config_save_fn

    def run(self, stop_event: Optional[threading.Event] = None) -> int:
        stop_event = stop_event or threading.Event()
        try:
            token = read_token(self.token_path)
            config, warning, _broken_path = cfgmod.load_with_warning(self.config_path)
        except BaseException:
            _LOGGER.exception("controller credentials/configuration could not be read")
            return 1

        sensors = None
        fans = None
        controller = None
        server = None
        server_thread = None
        server_errors = []
        shutdown_requested = threading.Event()
        exit_status = 0
        try:
            sensors = self._sensor_factory(config.sensor_backend, config.sensors)
            fans = self._fan_factory(
                config.fan_backend,
                preferred_controller_id=config.controller_id,
            )
            selected_id = getattr(fans, "controller_id", None)
            if selected_id and config.controller_id != selected_id:
                config.controller_id = selected_id
            self._config_save(config, self.config_path)

            controller = self._controller_factory(config, sensors, fans)

            store = ConfigStore(
                self.config_path,
                config,
                save_fn=self._config_save,
                publish_fn=lambda candidate: _publish_config(
                    controller, sensors, candidate),
                warning=warning,
            )
            def shutdown_from_ipc() -> bool:
                # Complete the final safe write before returning to the IPC
                # worker.  Delay the service event very briefly so that the
                # authenticated response can leave the socket before the main
                # thread closes the server.
                shutdown_requested.set()
                try:
                    return bool(controller.stop(safe=True))
                finally:
                    timer = threading.Timer(
                        _SHUTDOWN_ACK_GRACE_SECONDS, stop_event.set)
                    timer.daemon = True
                    timer.start()

            api = Api(controller, store=store, shutdown_fn=shutdown_from_ipc)
            server = self._server_factory(Dispatcher(api, token))

            controller.start()

            def serve() -> None:
                try:
                    server.serve_forever(stop_event)
                except BaseException as exc:  # service main observes and exits nonzero
                    server_errors.append(exc)
                    stop_event.set()

            server_thread = threading.Thread(
                target=serve,
                name="controller-ipc",
                daemon=True,
            )
            server_thread.start()

            persisted_controller_id = config.controller_id
            while True:
                if stop_event.wait(0.25):
                    break
                if server_errors:
                    _LOGGER.error("controller IPC failed: %s", server_errors[0])
                    exit_status = 1
                    break
                if not controller.is_alive():
                    if shutdown_requested.is_set():
                        continue
                    _LOGGER.critical(
                        "controller worker exited: %s",
                        controller.fatal_error or "unknown failure",
                    )
                    exit_status = 1
                    break

                # An auto backend may discover the only controller after boot.
                # Persist that identity once so every reconnect is pinned.
                discovered_id = getattr(fans, "controller_id", None)
                if discovered_id and discovered_id != persisted_controller_id:
                    store.transaction(
                        lambda candidate, value=discovered_id:
                        setattr(candidate, "controller_id", value)
                    )
                    persisted_controller_id = discovered_id
            if server_errors:
                _LOGGER.error("controller IPC failed: %s", server_errors[0])
                exit_status = 1
        except BaseException:
            _LOGGER.exception("headless controller failed")
            exit_status = 1
        finally:
            stop_event.set()
            if server is not None:
                server.close()
            if server_thread is not None:
                server_thread.join(2.0)
                if server_thread.is_alive():
                    _LOGGER.error("IPC worker did not stop")
                    exit_status = 1
            if controller is not None:
                try:
                    if not controller.stop(safe=True):
                        _LOGGER.critical(
                            "safe controller shutdown was not confirmed: %s",
                            controller.shutdown_error,
                        )
                        exit_status = 1
                except BaseException:
                    _LOGGER.exception("safe controller shutdown raised")
                    exit_status = 1
            for backend in (sensors, fans):
                if backend is not None:
                    try:
                        backend.close()
                    except BaseException:
                        _LOGGER.exception("backend close failed")
                        exit_status = 1
        return exit_status


def run_controller() -> int:
    guard = SingleInstance(r"Global\NeedZeroXtraTrash.Controller")
    try:
        if not guard.acquire():
            _LOGGER.error("another machine controller already owns the USB writer")
            return 3
    except OSError:
        _LOGGER.exception("could not acquire the global controller guard")
        return 3

    try:
        _prepare_manual_dev_runtime()
        configure_controller_logging()
    except BaseException:
        # A SYSTEM/background controller without protected runtime state and a
        # trustworthy diagnostic log must fail closed before hardware opens.
        _LOGGER.exception("protected controller runtime could not be prepared")
        guard.release()
        return 2

    stop_event = threading.Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    previous_handlers = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                previous_handlers[signum] = signal.signal(signum, request_stop)
            except (OSError, ValueError):
                pass
    try:
        return ControllerService(
            config_path=machine_config_path(),
            token_path=ipc_token_path(),
            config_save_fn=_secure_machine_config_save,
        ).run(stop_event)
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
        guard.release()
