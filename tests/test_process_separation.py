from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

from needzero import config as cfgmod
from needzero.client_api import ClientApi
from needzero.controller_service import ControllerService
from needzero.ipc import IpcClient, IpcServer


class _Backend:
    controller_id = None

    def close(self):
        pass


class _Server:
    def __init__(self, _dispatcher):
        self.closed = False

    def serve_forever(self, stop):
        stop.wait()

    def close(self):
        self.closed = True


class _Controller:
    stop_result = True
    alive_after_start = True

    def __init__(self, config, _sensors, _fans):
        self.config = config
        self.shutdown_error = None
        self.fatal_error = None
        self.alive = False

    def get_config(self):
        return self.config

    def set_config(self, config):
        self.config = config

    def snapshot(self):
        return {}

    def list_sensors(self):
        return []

    def start(self):
        self.alive = self.alive_after_start
        if not self.alive:
            self.fatal_error = "fatal fake failure"

    def is_alive(self):
        return self.alive

    def stop(self, safe=True):
        self.alive = False
        if not self.stop_result:
            self.shutdown_error = "safe write rejected"
        return self.stop_result

    def test_full(self, _seconds):
        pass


@pytest.mark.parametrize("enabled", [False, True])
def test_unchanged_autostart_save_does_not_run_installer(monkeypatch, enabled) -> None:
    from needzero import client_api

    mutations = []
    monkeypatch.setattr(client_api.autostart, "is_enabled", lambda: enabled)
    monkeypatch.setattr(client_api.autostart, "enable", lambda: mutations.append("enable"))
    monkeypatch.setattr(client_api.autostart, "disable", lambda: mutations.append("disable"))

    result = ClientApi(client=object()).set_autostart(enabled)

    assert result == {"ok": True, "enabled": enabled, "message": ""}
    assert mutations == []


def _service(tmp_path: Path, controller_factory=_Controller) -> ControllerService:
    config_path = tmp_path / "config.json"
    token_path = tmp_path / "ipc.token"
    token_path.write_text((b"x" * 32).hex(), encoding="ascii")
    cfgmod.save(cfgmod.Config.defaults(), config_path)
    return ControllerService(
        config_path=config_path,
        token_path=token_path,
        sensor_factory=lambda *_args, **_kwargs: _Backend(),
        fan_factory=lambda *_args, **_kwargs: _Backend(),
        controller_factory=controller_factory,
        server_factory=_Server,
    )


def test_safe_shutdown_failure_returns_nonzero(tmp_path: Path) -> None:
    class Unsafe(_Controller):
        stop_result = False

    stop = threading.Event()
    stop.set()
    assert _service(tmp_path, Unsafe).run(stop) == 1


def test_fatal_controller_exit_is_observable_and_nonzero(tmp_path: Path) -> None:
    class Fatal(_Controller):
        alive_after_start = False

    assert _service(tmp_path, Fatal).run(threading.Event()) == 1


def test_controller_refuses_to_overwrite_unpreserved_broken_config(
        tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    original = '{"emergency": null'
    config_path.write_text(original, encoding="utf-8")
    token_path = tmp_path / "ipc.token"
    token_path.write_text((b"x" * 32).hex(), encoding="ascii")

    def fail_replace(_source, _target):
        raise OSError("backup blocked")

    monkeypatch.setattr(cfgmod.os, "replace", fail_replace)
    service = ControllerService(
        config_path=config_path,
        token_path=token_path,
        sensor_factory=lambda *_args, **_kwargs: _Backend(),
        fan_factory=lambda *_args, **_kwargs: _Backend(),
        controller_factory=_Controller,
        server_factory=_Server,
    )

    assert service.run(threading.Event()) == 1
    assert config_path.read_text(encoding="utf-8") == original


def test_controller_dispatch_does_not_import_app_or_webview(monkeypatch) -> None:
    from needzero import entrypoint

    fake = types.ModuleType("needzero.controller_service")
    fake.run_controller = lambda: 17
    monkeypatch.setitem(sys.modules, "needzero.controller_service", fake)
    monkeypatch.delitem(sys.modules, "needzero.app", raising=False)
    monkeypatch.delitem(sys.modules, "webview", raising=False)

    assert entrypoint.main(["--controller"]) == 17
    assert "needzero.app" not in sys.modules
    assert "webview" not in sys.modules


def test_elevated_install_failure_returns_error_without_escaping(monkeypatch) -> None:
    from needzero import autostart, entrypoint

    def fail_install() -> None:
        raise RuntimeError("install failed")

    monkeypatch.setattr(autostart, "install_elevated", fail_install)
    assert entrypoint.main(["--install-autostart"]) == 1


def test_webview_start_failure_has_no_controller_lifecycle(monkeypatch) -> None:
    from needzero.app import DesktopApp

    class Client:
        def call(self, *_args):
            raise AssertionError("UI creation must not call controller shutdown")

    fake_webview = types.ModuleType("webview")

    def fail_create(*_args, **_kwargs):
        raise RuntimeError("WebView unavailable")

    fake_webview.create_window = fail_create
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    with pytest.raises(RuntimeError, match="WebView unavailable"):
        DesktopApp(Client()).run()


def test_close_button_quits_tray_while_minimize_keeps_it(monkeypatch) -> None:
    from needzero import app

    class Client:
        pass

    class Window:
        def __init__(self):
            self.destroyed = 0

        def destroy(self):
            self.destroyed += 1

    quit_signals = []
    desktop = app.DesktopApp(Client())
    desktop.window = Window()
    monkeypatch.setattr(
        app, "_signal_tray_quit", lambda: quit_signals.append(True) or True,
    )

    assert desktop.api.minimize_window()
    assert desktop.window.destroyed == 1
    assert quit_signals == []

    assert desktop.api.quit()
    assert desktop.window.destroyed == 2
    assert quit_signals == [True]


def test_tray_failure_returns_nonzero(monkeypatch) -> None:
    from needzero import app

    class Guard:
        def __init__(self, _name):
            pass

        def acquire(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(app, "SingleInstance", Guard)
    monkeypatch.setattr(app.TrayApp, "run", lambda self: (_ for _ in ()).throw(RuntimeError("tray failed")))
    assert app.run_tray() == 1


def test_ui_starts_session_tray_before_opening_window(monkeypatch) -> None:
    from needzero import app

    events = []

    class Guard:
        def __init__(self, _name):
            pass

        def acquire(self):
            return True

        def release(self):
            pass

    class Desktop:
        def run(self):
            events.append("ui")

    monkeypatch.setattr(app, "SingleInstance", Guard)
    monkeypatch.setattr(
        app, "_bootstrap_first_run_controller",
        lambda: events.append("controller") or "connected",
    )
    monkeypatch.setattr(
        app, "_ensure_tray_client", lambda: events.append("tray") or True,
    )
    monkeypatch.setattr(app, "DesktopApp", Desktop)

    assert app.run_ui() == 0
    assert events == ["controller", "tray", "ui"]


def test_existing_session_tray_is_not_started_twice(monkeypatch) -> None:
    from needzero import app

    class ExistingGuard:
        def __init__(self, name):
            assert name == app._TRAY_INSTANCE_NAME

        def acquire(self):
            return False

    monkeypatch.setattr(app, "SingleInstance", ExistingGuard)
    monkeypatch.setattr(
        app.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing tray must not be started twice")
        ),
    )

    assert app._ensure_tray_client() is True


def test_frozen_children_receive_fresh_pyinstaller_environment(monkeypatch) -> None:
    from needzero import runtime_paths

    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)
    environment = runtime_paths.independent_process_env()
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in runtime_paths.os.environ


def test_startup_bootstraps_controller_then_runs_tray(monkeypatch) -> None:
    from needzero import app

    events = []
    monkeypatch.setattr(
        app, "_bootstrap_first_run_controller",
        lambda: events.append("controller") or "connected",
    )
    monkeypatch.setattr(app, "run_tray", lambda: events.append("tray") or 0)
    assert app.run_startup() == 0
    assert events == ["controller", "tray"]


def test_first_run_release_installs_controller_before_webview(monkeypatch) -> None:
    import needzero
    from needzero import app

    calls = []
    fake_autostart = types.SimpleNamespace(
        is_enabled=lambda: False,
        enable=lambda: calls.append("enable"),
    )
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "BUILD_CHANNEL", "release")
    monkeypatch.setattr(app, "_controller_reachable", lambda: False)
    monkeypatch.setattr(app, "_show_native_message", lambda _message, _flags: app._IDYES)
    monkeypatch.setattr(needzero, "autostart", fake_autostart)
    monkeypatch.setitem(sys.modules, "needzero.autostart", fake_autostart)

    assert app._bootstrap_first_run_controller() == "installed"
    assert calls == ["enable"]
    assert "webview" not in sys.modules


def test_first_run_release_does_not_reinstall_enabled_controller(monkeypatch) -> None:
    import needzero
    from needzero import app

    fake_autostart = types.SimpleNamespace(
        is_enabled=lambda: True,
        enable=lambda: (_ for _ in ()).throw(AssertionError("must be a no-op")),
    )
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "BUILD_CHANNEL", "release")
    monkeypatch.setattr(app, "_controller_reachable", lambda: False)
    monkeypatch.setattr(needzero, "autostart", fake_autostart)
    monkeypatch.setitem(sys.modules, "needzero.autostart", fake_autostart)

    assert app._bootstrap_first_run_controller() == "installed-waiting"


def test_release_safely_replaces_older_dev_controller(monkeypatch) -> None:
    import needzero
    from needzero import app

    calls = []
    reachability = iter((True, False, False))

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def call(self, method):
            calls.append(method)
            if method == "get_state":
                return {"controller_version": "3.2-dev"}
            if method == "shutdown_controller":
                return True
            raise AssertionError(method)

    fake_autostart = types.SimpleNamespace(
        is_enabled=lambda: False,
        enable=lambda: calls.append("enable"),
    )
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "BUILD_CHANNEL", "release")
    monkeypatch.setattr(app, "ReloadingIpcClient", Client)
    monkeypatch.setattr(
        app, "_controller_reachable", lambda **_kw: next(reachability),
    )
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(app, "_show_native_message", lambda *_args: app._IDYES)
    monkeypatch.setattr(needzero, "autostart", fake_autostart)
    monkeypatch.setitem(sys.modules, "needzero.autostart", fake_autostart)

    assert app._bootstrap_first_run_controller() == "installed"
    assert calls == ["get_state", "shutdown_controller", "enable"]


def test_first_run_unsigned_dev_starts_session_controller_via_uac(monkeypatch) -> None:
    import needzero.windows_security as security
    from needzero import app

    launched = []
    reachability = iter((False, True))
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "BUILD_CHANNEL", "dev")
    monkeypatch.setattr(app, "_controller_reachable", lambda **_kw: next(reachability))
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        security, "launch_self_elevated", lambda arguments: launched.append(arguments)
    )

    assert app._bootstrap_first_run_controller() == "dev-started"
    assert launched == [["--controller"]]


def test_unsigned_dev_safely_replaces_older_controller(monkeypatch) -> None:
    import needzero.windows_security as security
    from needzero import app

    launched = []
    calls = []
    reachability = iter((True, False, False, True))

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def call(self, method):
            calls.append(method)
            if method == "get_state":
                return {}  # v3.1 and earlier did not publish a version
            if method == "shutdown_controller":
                return True
            raise AssertionError(method)

    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "BUILD_CHANNEL", "dev")
    monkeypatch.setattr(app, "ReloadingIpcClient", Client)
    monkeypatch.setattr(
        app, "_controller_reachable", lambda **_kw: next(reachability),
    )
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        security, "launch_self_elevated", lambda arguments: launched.append(arguments),
    )

    assert app._bootstrap_first_run_controller() == "dev-started"
    assert calls == ["get_state", "shutdown_controller"]
    assert launched == [["--controller"]]


def test_first_run_unsigned_dev_reports_cancelled_uac(monkeypatch) -> None:
    import needzero.windows_security as security
    from needzero import app

    messages = []
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "BUILD_CHANNEL", "dev")
    monkeypatch.setattr(app, "_controller_reachable", lambda **_kw: False)
    monkeypatch.setattr(
        security, "launch_self_elevated",
        lambda _arguments: (_ for _ in ()).throw(OSError("UAC cancelled")),
    )
    monkeypatch.setattr(
        app, "_show_native_message",
        lambda message, _flags: messages.append(message) or 1,
    )

    assert app._bootstrap_first_run_controller() == "dev-failed"
    assert any("UAC cancelled" in message for message in messages)


def test_first_run_install_failure_is_visible_and_webview_can_still_report(monkeypatch) -> None:
    import needzero
    from needzero import app

    messages = []
    fake_autostart = types.SimpleNamespace(
        is_enabled=lambda: False,
        enable=lambda: (_ for _ in ()).throw(RuntimeError("UAC cancelled")),
    )
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "BUILD_CHANNEL", "release")
    monkeypatch.setattr(app, "_controller_reachable", lambda: False)
    monkeypatch.setattr(
        app,
        "_show_native_message",
        lambda message, _flags: messages.append(message) or app._IDYES,
    )
    monkeypatch.setattr(needzero, "autostart", fake_autostart)
    monkeypatch.setitem(sys.modules, "needzero.autostart", fake_autostart)

    assert app._bootstrap_first_run_controller() == "failed"
    assert any("UAC cancelled" in message for message in messages)


def _patch_dev_runtime_security(monkeypatch, service_module, root: Path, reader_sid: str):
    config_path = root / "config.json"
    token_path = root / "ipc.token"
    protected = []
    monkeypatch.setattr(service_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_module, "BUILD_CHANNEL", "dev")
    monkeypatch.setattr(service_module, "is_elevated", lambda: True)
    monkeypatch.setattr(service_module, "current_user_sid", lambda: "S-1-5-21-1000")
    monkeypatch.setattr(service_module, "machine_dir", lambda: root)
    monkeypatch.setattr(service_module, "machine_config_path", lambda: config_path)
    monkeypatch.setattr(service_module, "ipc_token_path", lambda: token_path)
    monkeypatch.setattr(service_module, "ensure_machine_data_dir", lambda: root)
    monkeypatch.setattr(service_module, "assert_no_reparse_points", lambda path: Path(path))
    monkeypatch.setattr(
        service_module,
        "protect_machine_file",
        lambda path: protected.append(("machine", Path(path))),
    )
    monkeypatch.setattr(
        service_module,
        "protect_file_for_user",
        lambda path, sid: protected.append((sid, Path(path))),
    )
    monkeypatch.setattr(
        service_module,
        "ensure_ipc_token_access",
        lambda path, sid: protected.append(("access:" + sid, Path(path))),
    )
    monkeypatch.setattr(service_module, "protected_file_reader_sid", lambda _path: reader_sid)
    monkeypatch.setattr(service_module, "require_protected_file", lambda path, *_args: Path(path))
    monkeypatch.setattr(service_module, "require_protected_directory", lambda path, *_args: Path(path))
    return config_path, token_path, protected


def test_manual_dev_controller_bootstraps_fresh_protected_runtime(
        tmp_path: Path, monkeypatch) -> None:
    from needzero import controller_service as service

    config_path, token_path, protected = _patch_dev_runtime_security(
        monkeypatch, service, tmp_path, "S-1-5-21-1000")

    service._prepare_manual_dev_runtime()

    assert cfgmod.load(config_path).mode == "balanced"
    assert len(bytes.fromhex(token_path.read_text(encoding="ascii"))) == 32
    assert any(kind == "machine" for kind, _path in protected)
    assert any(str(kind).startswith("access:") for kind, _path in protected)


def test_manual_dev_controller_refuses_planted_foreign_token(
        tmp_path: Path, monkeypatch) -> None:
    from needzero import controller_service as service

    config_path, token_path, _protected = _patch_dev_runtime_security(
        monkeypatch, service, tmp_path, "S-1-5-21-9999")
    cfgmod.save(cfgmod.Config.defaults(), config_path)
    planted = (b"known-token" * 4)[:32].hex()
    token_path.write_text(planted, encoding="ascii")

    with pytest.raises(RuntimeError, match="another desktop identity"):
        service._prepare_manual_dev_runtime()

    assert token_path.read_text(encoding="ascii") == planted


def test_manual_dev_controller_requires_explicit_elevation(
        tmp_path: Path, monkeypatch) -> None:
    from needzero import controller_service as service

    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service, "BUILD_CHANNEL", "dev")
    monkeypatch.setattr(service, "is_elevated", lambda: False)
    monkeypatch.setattr(
        service,
        "ensure_machine_data_dir",
        lambda: (_ for _ in ()).throw(AssertionError("must fail before mutation")),
    )

    with pytest.raises(PermissionError, match="elevated terminal"):
        service._prepare_manual_dev_runtime()
    assert list(tmp_path.iterdir()) == []


def test_real_ipc_client_can_exit_while_controller_service_keeps_running(tmp_path: Path) -> None:
    token = b"q" * 32
    token_path = tmp_path / "ipc.token"
    token_path.write_text(token.hex(), encoding="ascii")
    config_path = tmp_path / "config.json"
    cfgmod.save(cfgmod.Config.defaults(), config_path)
    controllers = []
    servers = []

    def make_controller(config, sensors, fans):
        controller = _Controller(config, sensors, fans)
        controllers.append(controller)
        return controller

    def make_server(dispatcher):
        server = IpcServer(dispatcher, ("127.0.0.1", 0))
        servers.append(server)
        return server

    service = ControllerService(
        config_path=config_path,
        token_path=token_path,
        sensor_factory=lambda *_args, **_kwargs: _Backend(),
        fan_factory=lambda *_args, **_kwargs: _Backend(),
        controller_factory=make_controller,
        server_factory=make_server,
    )
    stop = threading.Event()
    result = []
    thread = threading.Thread(target=lambda: result.append(service.run(stop)))
    thread.start()
    deadline = time.monotonic() + 2
    while not servers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert servers and controllers

    client = IpcClient(token, servers[0].address)
    assert client.call("get_config")["mode"] == "balanced"
    del client  # UI/client process lifetime has no ownership of the controller
    assert controllers[0].is_alive()

    stop.set()
    thread.join(2)
    assert result == [0]


def test_authenticated_shutdown_ack_follows_confirmed_safe_stop(tmp_path: Path) -> None:
    token = b"s" * 32
    token_path = tmp_path / "ipc.token"
    token_path.write_text(token.hex(), encoding="ascii")
    config_path = tmp_path / "config.json"
    cfgmod.save(cfgmod.Config.defaults(), config_path)
    controllers = []
    servers = []

    class OrderedController(_Controller):
        def __init__(self, *args):
            super().__init__(*args)
            self.safe_stop_completed = False

        def stop(self, safe=True):
            result = super().stop(safe=safe)
            self.safe_stop_completed = bool(safe and result)
            return result

    def make_controller(config, sensors, fans):
        value = OrderedController(config, sensors, fans)
        controllers.append(value)
        return value

    def make_server(dispatcher):
        value = IpcServer(dispatcher, ("127.0.0.1", 0))
        servers.append(value)
        return value

    service = ControllerService(
        config_path=config_path,
        token_path=token_path,
        sensor_factory=lambda *_args, **_kwargs: _Backend(),
        fan_factory=lambda *_args, **_kwargs: _Backend(),
        controller_factory=make_controller,
        server_factory=make_server,
    )
    result = []
    thread = threading.Thread(
        target=lambda: result.append(service.run(threading.Event())))
    thread.start()
    deadline = time.monotonic() + 2
    while not servers and time.monotonic() < deadline:
        time.sleep(0.01)

    assert IpcClient(token, servers[0].address).call("shutdown_controller") is True
    assert controllers[0].safe_stop_completed is True
    thread.join(2)
    assert result == [0]


def test_slow_confirmed_shutdown_is_not_reported_as_worker_crash(tmp_path: Path) -> None:
    token = b"g" * 32
    token_path = tmp_path / "ipc.token"
    token_path.write_text(token.hex(), encoding="ascii")
    config_path = tmp_path / "config.json"
    cfgmod.save(cfgmod.Config.defaults(), config_path)
    servers = []
    stop_entered = threading.Event()
    release_stop = threading.Event()

    class SlowStop(_Controller):
        def stop(self, safe=True):
            self.alive = False
            stop_entered.set()
            if not release_stop.wait(2):
                raise TimeoutError("test did not release the safe stop")
            return True

    def make_server(dispatcher):
        value = IpcServer(dispatcher, ("127.0.0.1", 0))
        servers.append(value)
        return value

    service = ControllerService(
        config_path=config_path,
        token_path=token_path,
        sensor_factory=lambda *_args, **_kwargs: _Backend(),
        fan_factory=lambda *_args, **_kwargs: _Backend(),
        controller_factory=SlowStop,
        server_factory=make_server,
    )
    service_result = []
    service_thread = threading.Thread(
        target=lambda: service_result.append(service.run(threading.Event())))
    service_thread.start()
    deadline = time.monotonic() + 2
    while not servers and time.monotonic() < deadline:
        time.sleep(0.01)

    client_result = []
    client_thread = threading.Thread(
        target=lambda: client_result.append(
            IpcClient(token, servers[0].address, timeout=2).call("shutdown_controller")))
    client_thread.start()
    assert stop_entered.wait(1)
    time.sleep(0.35)  # cross at least one service watchdog interval
    assert service_thread.is_alive()
    release_stop.set()
    client_thread.join(2)
    service_thread.join(2)

    assert client_result == [True]
    assert service_result == [0]


def test_failed_safe_stop_returns_false_and_service_exits_nonzero(tmp_path: Path) -> None:
    token = b"f" * 32
    token_path = tmp_path / "ipc.token"
    token_path.write_text(token.hex(), encoding="ascii")
    config_path = tmp_path / "config.json"
    cfgmod.save(cfgmod.Config.defaults(), config_path)
    servers = []

    class Unsafe(_Controller):
        stop_result = False

    def make_server(dispatcher):
        value = IpcServer(dispatcher, ("127.0.0.1", 0))
        servers.append(value)
        return value

    service = ControllerService(
        config_path=config_path,
        token_path=token_path,
        sensor_factory=lambda *_args, **_kwargs: _Backend(),
        fan_factory=lambda *_args, **_kwargs: _Backend(),
        controller_factory=Unsafe,
        server_factory=make_server,
    )
    result = []
    thread = threading.Thread(
        target=lambda: result.append(service.run(threading.Event())))
    thread.start()
    deadline = time.monotonic() + 2
    while not servers and time.monotonic() < deadline:
        time.sleep(0.01)

    assert IpcClient(token, servers[0].address).call("shutdown_controller") is False
    thread.join(2)
    assert result == [1]
