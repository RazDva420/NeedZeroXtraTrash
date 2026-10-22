"""Reconnectable fan backend used by the default configuration."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

from .base import FanBackend, FanStatus

_LOGGER = logging.getLogger(__name__)


def _new_liquidctl_backend():
    # The process proxy is the sole production owner of the USB handle.  It
    # can be terminated on a missed HID-write deadline, unlike a Python thread.
    from .process import ProcessFanBackend

    return ProcessFanBackend()


class AutoFanBackend(FanBackend):
    """Use liquidctl when available and retry discovery while it is not."""

    def __init__(self, backend_factory: Optional[Callable[[], FanBackend]] = None,
                 retry_interval: float = 10.0, clock: Callable[[], float] = time.monotonic,
                 preferred_controller_id: Optional[str] = None) -> None:
        self._factory = backend_factory or _new_liquidctl_backend
        self._retry_interval = max(1.0, float(retry_interval))
        self._clock = clock
        self._next_retry = 0.0
        self._real: Optional[FanBackend] = None
        self._close_pending = False
        self._lock = threading.RLock()
        self.connection_generation = 0
        self.last_error: Optional[str] = None
        self.controller_id: Optional[str] = preferred_controller_id

    @property
    def name(self) -> str:
        with self._lock:
            return self._real.name if self._real is not None else "unavailable"

    @property
    def available(self) -> bool:
        with self._lock:
            return (not self._close_pending and self._real is not None
                    and bool(self._real.available))

    @property
    def description(self) -> str:
        with self._lock:
            if self._real is not None:
                return self._real.description
            return self.last_error or "Waiting for NZXT controller; retrying automatically"

    @property
    def channels(self) -> tuple:
        with self._lock:
            return (tuple(self._real.channels) if self._real is not None
                    else tuple(FanBackend.channels))

    def open(self) -> None:
        with self._lock:
            self._maybe_connect(force=True)

    def close(self) -> None:
        with self._lock:
            if not self._drop_real(schedule_retry=False):
                raise RuntimeError(self.last_error)

    def force_reconnect(self) -> bool:
        """Bypass discovery backoff for one bounded shutdown/recovery attempt."""
        with self._lock:
            if not self._drop_real(schedule_retry=False):
                return False
            return self._maybe_connect(force=True)

    def _maybe_connect(self, force: bool = False) -> bool:
        if self._real is not None and not self._close_pending:
            return True
        now = self._clock()
        if not force and now < self._next_retry:
            return False
        self._next_retry = now + self._retry_interval
        if self._close_pending and not self._drop_real():
            return False
        candidate = None
        try:
            candidate = self._factory()
            self._real = candidate
            if self.controller_id and hasattr(candidate, "preferred_controller_id"):
                candidate.preferred_controller_id = self.controller_id
            candidate.open()
            selected_id = getattr(candidate, "controller_id", None)
            if selected_id:
                self.controller_id = selected_id
            self.connection_generation += 1
            self.last_error = None
            _LOGGER.info("NZXT controller connected after discovery/retry")
            return True
        except Exception as exc:  # noqa: BLE001 - retry is the recovery path
            if candidate is not None:
                self._drop_real()
            _LOGGER.warning("NZXT controller unavailable; retry in %.0fs (%s)",
                            self._retry_interval, exc)
            if not self._close_pending:
                self.last_error = str(exc)
            return False

    def _drop_real(self, schedule_retry: bool = True) -> bool:
        if self._real is None:
            return True
        self._close_pending = True
        self._next_retry = (self._clock() + self._retry_interval
                            if schedule_retry else 0.0)
        try:
            self._real.close()
        except Exception as exc:
            self.last_error = f"USB owner could not be stopped: {exc}"
            _LOGGER.error("%s", self.last_error)
            return False
        self._real = None
        self._close_pending = False
        self.connection_generation += 1
        return True

    def set_duty(self, channel: str, duty: float) -> bool:
        return self.set_many({channel: duty}).get(channel, False)

    def set_many(self, duties: Dict[str, float]) -> Dict[str, bool]:
        with self._lock:
            if not self._maybe_connect():
                return {ch: False for ch in duties}

            try:
                results = self._real.set_many(duties) if self._real is not None else {}
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("NZXT write raised an exception: %s", exc)
                self.last_error = str(exc)
                self._drop_real()
                return {ch: False for ch in duties}
            normalized = {ch: bool(results.get(ch, False)) for ch in duties}
            if not all(normalized.values()):
                _LOGGER.error("NZXT write failed; connection will be rediscovered")
                failed = ", ".join(ch for ch, ok in normalized.items() if not ok)
                self.last_error = f"fan write failed: {failed}"
                # Preserve the real per-channel result for diagnostics.  The
                # generation change still invalidates cached confirmations.
                self._drop_real()
            return normalized

    def read_status(self) -> Dict[str, FanStatus]:
        with self._lock:
            if self._real is not None and not self._close_pending:
                try:
                    return self._real.read_status()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("NZXT status read failed: %s", exc)
                    self.last_error = str(exc)
                    _LOGGER.error("NZXT connection lost while reading status")
                    self._drop_real()
                    # A transport failure is not ordinary missing telemetry.
                    # Propagate it so the controller's bounded status-failure
                    # counter can reach fan_fault across reconnect attempts.
                    raise
            raise RuntimeError(
                self.last_error
                or "NZXT controller is unavailable; status cannot be read"
            )
