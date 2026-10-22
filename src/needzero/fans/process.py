"""Deadline-bounded fan I/O in a disposable child process.

The Windows HID transport used by liquidctl has a bounded read operation but
its write call has no cancellation or timeout argument.  A Python helper
thread would therefore be unsafe: after the caller timed out, that thread
could still complete an old duty write after the shutdown safety write.  This
proxy makes the child process the sole owner of the USB handle.  On a missed
deadline the child is terminated and joined before a replacement is allowed,
so an obsolete writer cannot survive a reconnect.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from multiprocessing.connection import Connection
from typing import Callable, Dict, Optional, Tuple

from .base import FanBackend, FanStatus

_LOGGER = logging.getLogger(__name__)


class FanProcessError(RuntimeError):
    """The isolated fan worker failed or returned an invalid response."""


class FanProcessTimeout(FanProcessError):
    """The isolated fan worker missed an I/O deadline and was terminated."""


def _status_to_wire(status: Dict[str, FanStatus]) -> dict:
    return {
        channel: {
            "rpm": value.rpm,
            "duty": value.duty,
            "mode": value.mode,
        }
        for channel, value in status.items()
    }


def _liquidctl_worker(conn: Connection) -> None:
    """Own liquidctl and serve serialized requests from the parent process."""
    from .liquidctl_backend import LiquidctlFanBackend

    backend: Optional[LiquidctlFanBackend] = None
    try:
        while True:
            try:
                request_id, operation, payload = conn.recv()
            except EOFError:
                break

            try:
                if operation == "open":
                    if backend is None:
                        preferred = (payload or {}).get("preferred_controller_id")
                        backend = LiquidctlFanBackend(
                            preferred_controller_id=preferred)
                        backend.open()
                    value = {
                        "name": backend.name,
                        "available": backend.available,
                        "description": backend.description,
                        "channels": list(backend.channels),
                        "controller_id": backend.controller_id,
                    }
                elif operation == "set_many":
                    if backend is None:
                        raise RuntimeError("fan backend is not open")
                    value = backend.set_many(dict(payload or {}))
                elif operation == "read_status":
                    if backend is None:
                        raise RuntimeError("fan backend is not open")
                    value = _status_to_wire(backend.read_status())
                elif operation == "close":
                    if backend is not None:
                        backend.close()
                        backend = None
                    conn.send((request_id, True, None))
                    break
                else:
                    raise ValueError(f"unknown fan worker operation: {operation}")
                conn.send((request_id, True, value))
            except Exception as exc:  # noqa: BLE001 - exception crosses IPC as text
                try:
                    conn.send((request_id, False, f"{type(exc).__name__}: {exc}"))
                except (BrokenPipeError, EOFError, OSError):
                    break
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass
        conn.close()


class ProcessFanBackend(FanBackend):
    """Fan backend proxy with hard parent-side operation deadlines.

    ``worker_target`` and ``worker_args`` exist solely to allow deterministic
    process-isolation tests without importing liquidctl or touching hardware.
    Production callers use the defaults.
    """

    name = "liquidctl"

    def __init__(
        self,
        io_timeout: float = 6.0,
        open_timeout: float = 15.0,
        terminate_timeout: float = 2.0,
        preferred_controller_id: Optional[str] = None,
        worker_target: Optional[Callable[..., None]] = None,
        worker_args: Tuple = (),
    ) -> None:
        self.available = False
        self.description = "Isolated NZXT controller"
        self.channels = tuple(FanBackend.channels)
        self.connection_generation = 0
        self.last_error: Optional[str] = None
        self.preferred_controller_id = preferred_controller_id
        self.controller_id: Optional[str] = None

        self._io_timeout = max(0.05, float(io_timeout))
        self._open_timeout = max(self._io_timeout, float(open_timeout))
        self._terminate_timeout = max(0.05, float(terminate_timeout))
        self._worker_target = worker_target or _liquidctl_worker
        self._worker_args = tuple(worker_args)
        self._ctx = multiprocessing.get_context("spawn")
        self._process = None
        self._conn: Optional[Connection] = None
        self._request_id = 0
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._process is not None and self._process.is_alive() and self.available:
                return
            if not self._terminate_locked():
                raise FanProcessError(
                    "old fan I/O process is still alive; refusing a second USB owner")
            parent_conn, child_conn = self._ctx.Pipe(duplex=True)
            process = self._ctx.Process(
                target=self._worker_target,
                args=(child_conn, *self._worker_args),
                name="needzero-fan-io",
                daemon=True,
            )
            self._conn = parent_conn
            self._process = process
            try:
                process.start()
            except Exception:
                parent_conn.close()
                child_conn.close()
                self._conn = None
                self._process = None
                raise
            finally:
                child_conn.close()

            try:
                info = self._rpc_locked(
                    "open",
                    {"preferred_controller_id": self.preferred_controller_id},
                    self._open_timeout,
                )
                self.name = str(info.get("name") or "liquidctl")
                self.description = str(info.get("description") or "NZXT controller")
                channels = info.get("channels") or FanBackend.channels
                self.channels = tuple(str(channel) for channel in channels)
                self.controller_id = info.get("controller_id")
                if self.controller_id:
                    self.preferred_controller_id = self.controller_id
                self.available = bool(info.get("available", True))
                self.last_error = None
                self.connection_generation += 1
            except Exception:
                self._terminate_locked()
                raise

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and self._process is not None \
                    and self._process.is_alive():
                try:
                    self._rpc_locked("close", None, min(1.0, self._io_timeout))
                except Exception:  # noqa: BLE001 - termination is the fallback
                    pass
            if not self._terminate_locked():
                raise FanProcessError("fan I/O process did not stop; ownership retained")

    def force_reconnect(self) -> bool:
        """Terminate the old USB owner before making exactly one new attempt."""
        with self._lock:
            if not self._terminate_locked():
                self.last_error = "old fan I/O process could not be terminated"
                return False
            try:
                self.open()
                return self.available
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                return False

    def set_duty(self, channel: str, duty: float) -> bool:
        return self.set_many({channel: duty}).get(channel, False)

    def set_many(self, duties: Dict[str, float]) -> Dict[str, bool]:
        requested = {str(ch): float(duty) for ch, duty in duties.items()}
        with self._lock:
            if not self.available:
                return {channel: False for channel in requested}
            try:
                raw = self._rpc_locked("set_many", requested, self._io_timeout)
                self.last_error = None
                return {
                    channel: bool((raw or {}).get(channel, False))
                    for channel in requested
                }
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self._terminate_locked()
                return {channel: False for channel in requested}

    def read_status(self) -> Dict[str, FanStatus]:
        with self._lock:
            if not self.available:
                raise RuntimeError(self.last_error or "fan worker is unavailable")
            try:
                raw = self._rpc_locked("read_status", None, self._io_timeout)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self._terminate_locked()
                raise

            out: Dict[str, FanStatus] = {}
            for channel in self.channels:
                value = (raw or {}).get(channel) or {}
                out[channel] = FanStatus(
                    channel=channel,
                    rpm=value.get("rpm"),
                    duty=value.get("duty"),
                    mode=value.get("mode"),
                )
            return out

    def _rpc_locked(self, operation: str, payload, timeout: float):
        conn = self._conn
        process = self._process
        if conn is None or process is None or not process.is_alive():
            raise FanProcessError("fan worker is not running")

        self._request_id += 1
        request_id = self._request_id
        try:
            conn.send((request_id, operation, payload))
            if not conn.poll(timeout):
                raise FanProcessTimeout(
                    f"fan worker {operation} timed out after {timeout:.2f}s")
            response_id, ok, value = conn.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise FanProcessError(f"fan worker communication failed: {exc}") from exc
        if response_id != request_id:
            raise FanProcessError("fan worker returned an out-of-order response")
        if not ok:
            raise FanProcessError(str(value))
        return value

    def _terminate_locked(self) -> bool:
        """Stop and join the old writer; never start a peer while it is alive."""
        process = self._process
        conn = self._conn
        self.available = False

        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if process is None:
            self._conn = None
            return True
        if process.is_alive():
            process.terminate()
            process.join(self._terminate_timeout)
        if process.is_alive():
            try:
                process.kill()
            except (AttributeError, OSError):
                pass
            process.join(self._terminate_timeout)
        stopped = not process.is_alive()
        if stopped:
            self._conn = None
            self._process = None
            self.connection_generation += 1
            try:
                process.close()
            except (OSError, ValueError):
                pass
        else:
            # Retain the Process object so every later open/reconnect refuses
            # to create a second USB owner until termination is confirmed.
            self._conn = None
            self._process = process
            _LOGGER.critical("fan I/O process survived terminate/kill; reconnect blocked")
        return stopped


__all__ = [
    "ProcessFanBackend",
    "FanProcessError",
    "FanProcessTimeout",
]
