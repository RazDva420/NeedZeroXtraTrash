"""Temperature sensor backends.

The NZXT controller itself cannot read CPU/GPU temperatures, so those
come from a separate in-process source.  ``auto`` prefers the bundled
LibreHardwareMonitor DLL when it can read real sensors, then falls back to
Windows ACPI thermal zones and NVML GPU core readings.
``mock`` simulates temperatures for UI tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Callable, Optional, Sequence, Tuple

from .base import ROLES, SensorBackend, SensorReading, _valid_temp, resolve_temps, auto_select


_LOGGER = logging.getLogger(__name__)


class CombinedSensorBackend(SensorBackend):
    def __init__(self, backends):
        self._backends = backends
        self.available = any(getattr(b, "available", False) for b in backends)
        self.name = "+".join(b.name for b in backends) if backends else "unavailable"

    def close(self):
        for backend in self._backends:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass

    def read_all(self):
        out = []
        for backend in self._backends:
            try:
                out.extend(backend.read_all())
            except Exception:  # noqa: BLE001
                pass
        return out


@dataclass(frozen=True)
class SensorBackendSpec:
    """A lazily opened sensor source and the roles it can satisfy."""

    label: str
    factory: Callable[[], SensorBackend]
    roles: Tuple[str, ...]
    id_prefixes: Tuple[str, ...] = ()
    always: bool = False
    empty_is_failure: bool = True


@dataclass
class _BackendSlot:
    spec: SensorBackendSpec
    backend: Optional[SensorBackend] = None
    failures: int = 0
    next_retry: float = 0.0
    healthy: bool = False
    last_error: str = ""


def _manual_identifiers(value) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, dict):
        values = value.values()
    elif any(hasattr(value, role) for role in ROLES):
        values = (getattr(value, role, None) for role in ROLES)
    else:
        try:
            values = iter(value)
        except TypeError:
            return set()
    return {item for item in values if isinstance(item, str) and item}


class SensorSupervisor(SensorBackend):
    """Keep required sensor sources connected and retry failed sources.

    Backends are opened lazily in priority order.  A source that fails to open,
    raises while reading, or unexpectedly becomes empty is discarded and
    retried with bounded exponential backoff.  The supervisor itself remains
    alive, so a provider can appear after application startup.
    """

    def __init__(
        self,
        specs: Sequence[SensorBackendSpec],
        manual_ids=None,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._slots = [_BackendSlot(spec) for spec in specs]
        self._clock = clock
        self._base_backoff = max(0.01, float(base_backoff))
        self._max_backoff = max(self._base_backoff, float(max_backoff))
        self._manual_ids = _manual_identifiers(manual_ids)
        self._lock = threading.RLock()
        self._closed = False

    @property
    def available(self) -> bool:
        with self._lock:
            return any(slot.healthy for slot in self._slots)

    @property
    def name(self) -> str:
        with self._lock:
            names = [slot.spec.label for slot in self._slots if slot.healthy]
        return "+".join(names) if names else "unavailable"

    def set_manual_ids(self, value) -> None:
        """Update identifiers that must keep their provider active."""
        with self._lock:
            self._manual_ids = _manual_identifiers(value)

    def open(self) -> None:
        with self._lock:
            if self._closed:
                for slot in self._slots:
                    slot.next_retry = 0.0
            self._closed = False
        # Preserve the old create_backend contract: make one immediate discovery
        # pass.  Slow WMI work remains asynchronous inside its backend.
        self.read_all()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for slot in self._slots:
                self._close_slot(slot)

    def read_all(self) -> list[SensorReading]:
        with self._lock:
            if self._closed:
                return []
            readings = []
            for slot in self._slots:
                if not self._needed(slot, readings):
                    continue
                backend = self._ensure_open(slot)
                if backend is None:
                    continue
                try:
                    current = list(backend.read_all() or [])
                except Exception as exc:  # noqa: BLE001 - retry is the recovery path
                    self._fail_slot(slot, exc)
                    continue
                has_fresh_value = any(
                    _valid_temp(reading.value, reading.sampled_at)
                    for reading in current
                )
                if not has_fresh_value and slot.spec.empty_is_failure:
                    self._fail_slot(
                        slot,
                        RuntimeError("sensor backend returned no fresh readings"),
                    )
                    continue
                slot.healthy = has_fresh_value
                if has_fresh_value:
                    slot.failures = 0
                    slot.next_retry = 0.0
                    slot.last_error = ""
                if current:
                    readings.extend(current)
            return readings

    def diagnostics(self) -> dict:
        """Return serialisable health details for snapshots and tests."""
        with self._lock:
            return {
                slot.spec.label: {
                    "connected": slot.backend is not None,
                    "available": slot.healthy,
                    "failures": slot.failures,
                    "next_retry": slot.next_retry,
                    "error": slot.last_error,
                }
                for slot in self._slots
            }

    def _needed(self, slot: _BackendSlot, readings) -> bool:
        if slot.backend is not None or slot.spec.always:
            return True
        if any(
            ident.startswith(prefix)
            for ident in self._manual_ids
            for prefix in slot.spec.id_prefixes
        ):
            return True
        selected = auto_select(readings)
        return any(not selected.get(role) for role in slot.spec.roles)

    def _ensure_open(self, slot: _BackendSlot) -> Optional[SensorBackend]:
        if slot.backend is not None:
            return slot.backend
        now = self._clock()
        if now < slot.next_retry:
            return None
        candidate = None
        try:
            candidate = slot.spec.factory()
            candidate.open()
            slot.backend = candidate
            return candidate
        except Exception as exc:  # noqa: BLE001 - retained slot will retry
            if candidate is not None:
                try:
                    candidate.close()
                except Exception:  # noqa: BLE001
                    pass
            self._schedule_retry(slot, exc)
            return None

    def _fail_slot(self, slot: _BackendSlot, exc: Exception) -> None:
        self._close_slot(slot)
        self._schedule_retry(slot, exc)

    def _close_slot(self, slot: _BackendSlot) -> None:
        backend = slot.backend
        slot.backend = None
        slot.healthy = False
        if backend is not None:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass

    def _schedule_retry(self, slot: _BackendSlot, exc: Exception) -> None:
        slot.failures += 1
        exponent = min(slot.failures - 1, 20)
        delay = min(self._max_backoff, self._base_backoff * (2 ** exponent))
        slot.next_retry = self._clock() + delay
        slot.last_error = str(exc)
        _LOGGER.warning("%s unavailable; retry in %.1fs (%s)",
                        slot.spec.label, delay, exc)


def _lhm_factory():
    from .lhm import LhmSensorBackend

    return LhmSensorBackend()


def _nvml_factory():
    from .nvml import NvmlSensorBackend

    return NvmlSensorBackend()


def _native_factory():
    from .windows_native import WindowsNativeSensorBackend

    return WindowsNativeSensorBackend()


_CONTROL_ROLES = ("cpu", "gpu", "gpu_hotspot")


def _auto_specs() -> tuple[SensorBackendSpec, ...]:
    return (
        SensorBackendSpec("LibreHardwareMonitor", _lhm_factory, _CONTROL_ROLES,
                          always=True),
        SensorBackendSpec("NVML", _nvml_factory, ("gpu",),
                          id_prefixes=("/nvml/",)),
        # The native source owns its own nonblocking cache, so an empty cache
        # while the first probe is running is not a disconnect.
        SensorBackendSpec("WindowsNative", _native_factory, ("cpu",),
                          id_prefixes=("/windows/",), empty_is_failure=False),
    )


def _explicit_spec(kind: str) -> Optional[SensorBackendSpec]:
    specs = {
        "lhm": SensorBackendSpec("LibreHardwareMonitor", _lhm_factory,
                                 _CONTROL_ROLES, always=True),
        "nvml": SensorBackendSpec("NVML", _nvml_factory, ("gpu",),
                                  id_prefixes=("/nvml/",), always=True),
        "native": SensorBackendSpec("WindowsNative", _native_factory,
                                     ("cpu",), id_prefixes=("/windows/",),
                                    always=True, empty_is_failure=False),
    }
    return specs.get(kind)


def create_backend(kind: str = "auto", sensor_map=None):
    """Return the best available sensor backend.

    ``kind`` may be ``auto`` (LHM + optional fallbacks), ``lhm``,
    ``native``, ``nvml`` or ``mock``.
    """
    from .mock import MockSensorBackend

    if kind == "mock":
        return MockSensorBackend()

    if kind in ("auto", "hwinfo"):  # migrate legacy backend without opening it
        from .deadline import DeadlineSensorBackend

        backend = DeadlineSensorBackend(
            lambda: SensorSupervisor(_auto_specs(), manual_ids=sensor_map),
            manual_ids=sensor_map,
        )
        backend.open()
        return backend

    explicit = _explicit_spec(kind)
    if explicit is not None:
        from .deadline import DeadlineSensorBackend

        backend = DeadlineSensorBackend(
            lambda: SensorSupervisor((explicit,), manual_ids=sensor_map),
            manual_ids=sensor_map,
        )
        backend.open()
        return backend

    return CombinedSensorBackend([])


__all__ = [
    "SensorBackend",
    "SensorReading",
    "resolve_temps",
    "auto_select",
    "create_backend",
    "CombinedSensorBackend",
    "SensorBackendSpec",
    "SensorSupervisor",
    "DeadlineSensorBackend",
]

from .deadline import DeadlineSensorBackend
