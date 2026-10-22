"""Deadline-bounded cached access to potentially blocking sensor providers."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .base import SensorBackend, SensorReading


_LOGGER = logging.getLogger(__name__)
DEFAULT_READ_DEADLINE_SECONDS = 5.0
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_CLOSE_JOIN_SECONDS = 0.5


class DeadlineSensorBackend(SensorBackend):
    """Keep sensor I/O off the control thread and expose only fresh cache.

    LHM/NVML calls can enter native code with no cancellation API.  A
    daemon worker owns those read-only providers; the control loop never waits
    for it and receives a visible error when no read has completed within the
    deadline.  The stale/error path therefore applies fail-safe cooling even
    if native sensor code remains blocked indefinitely.
    """

    def __init__(
        self,
        factory: Callable[[], SensorBackend],
        *,
        manual_ids=None,
        read_deadline: float = DEFAULT_READ_DEADLINE_SECONDS,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        close_join: float = DEFAULT_CLOSE_JOIN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._read_deadline = max(0.05, float(read_deadline))
        self._poll_interval = max(0.01, float(poll_interval))
        self._close_join = max(0.01, float(close_join))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backend: Optional[SensorBackend] = None
        self._readings: list[SensorReading] = []
        self._last_completed: Optional[float] = None
        self._started_at: Optional[float] = None
        self._pending_manual_ids = manual_ids
        self._manual_revision = 1
        self._applied_manual_revision = 0
        self._name = "sensor-worker"
        self._available = False
        self._last_error = "sensor worker has not completed its first read"

    @property
    def name(self) -> str:
        with self._lock:
            return self._name

    @property
    def available(self) -> bool:
        with self._lock:
            if self._last_completed is None:
                return False
            return (
                self._available
                and self._clock() - self._last_completed <= self._read_deadline
            )

    def open(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._started_at = self._clock()
            self._thread = threading.Thread(
                target=self._run,
                name="needzero-sensor-io",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._close_join)
            if thread.is_alive():
                _LOGGER.error(
                    "sensor worker did not stop before deadline; it remains "
                    "read-only and detached from the control loop"
                )

    def set_manual_ids(self, value) -> None:
        # Never acquire the provider/Supervisor lock from an IPC thread: it may
        # be held by a blocked native read. The worker applies this revision
        # before its next provider call.
        with self._lock:
            self._pending_manual_ids = value
            self._manual_revision += 1

    def read_all(self) -> list[SensorReading]:
        now = self._clock()
        with self._lock:
            reference = self._last_completed
            if reference is None:
                reference = self._started_at
            age = float("inf") if reference is None else max(0.0, now - reference)
            if self._last_completed is None or age > self._read_deadline:
                detail = self._last_error or "sensor read deadline exceeded"
                raise TimeoutError(
                    f"sensor cache is not fresh after {age:.1f}s: {detail}"
                )
            return list(self._readings)

    def diagnostics(self) -> dict:
        with self._lock:
            age = (
                None
                if self._last_completed is None
                else max(0.0, self._clock() - self._last_completed)
            )
            return {
                "deadline_worker": {
                    "available": self.available,
                    "last_completed_age": age,
                    "deadline_seconds": self._read_deadline,
                    "error": self._last_error,
                }
            }

    def _run(self) -> None:
        backend = None
        retry_delay = 1.0
        try:
            while not self._stop.is_set():
                if backend is None:
                    try:
                        backend = self._factory()
                        backend.open()
                        self._applied_manual_revision = 0
                        with self._lock:
                            self._backend = backend
                            self._name = getattr(backend, "name", "sensors")
                    except Exception as exc:  # provider init is retryable
                        self._record_error(exc)
                        if backend is not None:
                            try:
                                backend.close()
                            except Exception:  # noqa: BLE001
                                pass
                        backend = None
                        if self._stop.wait(retry_delay):
                            break
                        retry_delay = min(30.0, retry_delay * 2.0)
                        continue

                try:
                    with self._lock:
                        manual_revision = self._manual_revision
                        manual_ids = self._pending_manual_ids
                    if manual_revision != self._applied_manual_revision:
                        updater = getattr(backend, "set_manual_ids", None)
                        if callable(updater):
                            updater(manual_ids)
                        self._applied_manual_revision = manual_revision
                    readings = list(backend.read_all() or [])
                    completed = self._clock()
                    with self._lock:
                        self._readings = readings
                        self._last_completed = completed
                        self._name = getattr(backend, "name", "sensors")
                        self._available = bool(getattr(backend, "available", True))
                        self._last_error = ""
                    retry_delay = 1.0
                except Exception as exc:  # normal supervisor reconnect path
                    self._record_error(exc)
                    try:
                        backend.close()
                    except Exception:  # noqa: BLE001
                        pass
                    backend = None
                    with self._lock:
                        self._backend = None
                        self._available = False
                    if self._stop.wait(retry_delay):
                        break
                    retry_delay = min(30.0, retry_delay * 2.0)
                    continue

                self._stop.wait(self._poll_interval)
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self._backend = None
                self._available = False

    def _record_error(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._last_error = message
            self._available = False
        _LOGGER.warning("sensor provider failed: %s", message)


__all__ = ["DeadlineSensorBackend"]
