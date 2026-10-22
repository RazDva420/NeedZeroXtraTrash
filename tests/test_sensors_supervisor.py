"""Deterministic sensor recovery and classification tests (no hardware)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from needzero.config import SensorMap
from needzero.sensors import SensorBackendSpec, SensorSupervisor
from needzero.sensors.base import SensorBackend, SensorReading, auto_select, resolve_temps
from needzero.sensors.windows_native import WindowsNativeSensorBackend


def _reading(identifier: str, hardware_type: str, name: str, value: float,
             hardware: str = "Fake hardware", sampled_at: float | None = None):
    kwargs = {} if sampled_at is None else {"sampled_at": sampled_at}
    return SensorReading(identifier, hardware, hardware_type, name, value, **kwargs)


class _StaticBackend(SensorBackend):
    def __init__(self, readings):
        self._readings = list(readings)
        self.available = False
        self.closed = False
        self.raise_on_read = False

    def open(self):
        self.available = True

    def close(self):
        self.available = False
        self.closed = True

    def read_all(self):
        if self.raise_on_read:
            raise OSError("backend disappeared")
        return list(self._readings)


def test_supervisor_retries_with_bounded_backoff_and_updates_availability():
    now = [0.0]
    attempts = []
    live = []

    class _NotReady(_StaticBackend):
        def open(self):
            raise OSError("not ready")

    def factory():
        attempts.append(now[0])
        backend = _NotReady([]) if len(attempts) <= 2 else _StaticBackend([
            _reading("/cpu/0", "Cpu", "CPU Package", 52),
        ])
        live.append(backend)
        return backend

    supervisor = SensorSupervisor(
        (SensorBackendSpec("recovering", factory, ("cpu",), always=True),),
        base_backoff=2,
        max_backoff=4,
        clock=lambda: now[0],
    )
    supervisor.open()  # attempt 1; retry at t=2
    assert attempts == [0.0]
    assert not supervisor.available
    assert supervisor.name == "unavailable"

    now[0] = 1.9
    assert supervisor.read_all() == []
    assert attempts == [0.0]

    now[0] = 2.0
    assert supervisor.read_all() == []  # attempt 2; retry at t=6 (capped at 4s)
    assert attempts == [0.0, 2.0]
    assert supervisor.diagnostics()["recovering"]["next_retry"] == 6.0

    now[0] = 5.9
    assert supervisor.read_all() == []
    now[0] = 6.0
    assert resolve_temps(supervisor.read_all()).cpu == 52
    assert supervisor.available
    assert supervisor.name == "recovering"

    live[-1].raise_on_read = True
    assert supervisor.read_all() == []
    assert not supervisor.available
    assert live[-1].closed
    assert supervisor.diagnostics()["recovering"]["next_retry"] == 8.0

    disappeared = live[-1]
    now[0] = 7.9
    assert supervisor.read_all() == []
    assert live[-1] is disappeared
    now[0] = 8.0
    assert resolve_temps(supervisor.read_all()).cpu == 52
    assert live[-1] is not disappeared
    assert supervisor.available
    assert supervisor.name == "recovering"
    assert supervisor.diagnostics()["recovering"]["failures"] == 0
    assert supervisor.diagnostics()["recovering"]["next_retry"] == 0.0
    supervisor.close()


def test_supervisor_treats_only_stale_values_as_backend_failure():
    stale = _reading(
        "/cpu/stale",
        "Cpu",
        "CPU Package",
        52,
        sampled_at=time.monotonic() - 60,
    )
    supervisor = SensorSupervisor(
        (SensorBackendSpec("stale", lambda: _StaticBackend([stale]),
                           ("cpu",), always=True),),
        base_backoff=1,
    )
    supervisor.open()
    assert not supervisor.available
    assert supervisor.read_all() == []
    assert supervisor.diagnostics()["stale"]["failures"] == 1
    supervisor.close()


def test_removed_chipset_does_not_activate_fallback_provider():
    now = [0.0]
    attempts = [0]
    lhm_readings = [
        _reading("/cpu/0", "Cpu", "CPU Package", 50),
        _reading("/gpu/0/core", "GpuNvidia", "GPU Core", 45, "GPU A"),
        _reading("/gpu/0/hot", "GpuNvidia", "GPU Hot Spot", 53, "GPU A"),
    ]

    class _LateHWiNFO(_StaticBackend):
        def open(self):
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("shared memory not created yet")
            super().open()

    supervisor = SensorSupervisor(
        (
            SensorBackendSpec("LHM", lambda: _StaticBackend(lhm_readings),
                              ("cpu", "gpu", "gpu_hotspot"), always=True),
            SensorBackendSpec("HWiNFO", lambda: _LateHWiNFO([
                _reading("/hwinfo/3/9", "Motherboard", "PCH Temperature", 61),
            ]), ("cpu", "gpu", "gpu_hotspot"),
                              id_prefixes=("/hwinfo/",)),
        ),
        base_backoff=2,
        max_backoff=8,
        clock=lambda: now[0],
    )
    supervisor.open()
    assert attempts[0] == 0
    assert resolve_temps(supervisor.read_all()).chipset is None

    now[0] = 2.0
    supervisor.read_all()
    assert attempts[0] == 0
    assert supervisor.name == "LHM"
    supervisor.close()


def test_manual_hwinfo_identifier_activates_and_retains_provider():
    now = [0.0]
    attempts = [0]
    all_roles = [
        _reading("/cpu/0", "Cpu", "CPU Package", 51),
        _reading("/gpu/0/core", "GpuNvidia", "GPU Core", 46, "GPU A"),
        _reading("/gpu/0/hot", "GpuNvidia", "GPU Hot Spot", 55, "GPU A"),
        _reading("/board/pch", "Motherboard", "PCH", 58),
    ]

    class _LateManual(_StaticBackend):
        def open(self):
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("HWiNFO is still starting")
            super().open()

    supervisor = SensorSupervisor(
        (
            SensorBackendSpec("LHM", lambda: _StaticBackend(all_roles),
                              ("cpu", "gpu", "gpu_hotspot"), always=True),
            SensorBackendSpec("HWiNFO", lambda: _LateManual([
                _reading("/hwinfo/1/2", "Cpu", "CPU Package", 49),
            ]), ("cpu", "gpu", "gpu_hotspot"),
                              id_prefixes=("/hwinfo/",)),
        ),
        base_backoff=1,
        max_backoff=4,
        clock=lambda: now[0],
    )
    supervisor.open()
    assert attempts[0] == 0  # LHM already has every automatic role.
    supervisor.set_manual_ids({"cpu": "/hwinfo/1/2"})
    supervisor.read_all()
    assert attempts[0] == 1  # A later manual ID activates and retains HWiNFO.
    now[0] = 1.0
    readings = supervisor.read_all()
    assert attempts[0] == 2
    assert resolve_temps(readings, SensorMap(cpu="/hwinfo/1/2")).cpu == 49
    supervisor.close()


def test_memory_junction_is_never_auto_selected_as_hotspot():
    memory_only = [
        _reading("/gpu/core", "GpuNvidia", "GPU Core", 50, "GPU A"),
        _reading("/gpu/memory", "GpuNvidia", "GPU Memory Junction", 78, "GPU A"),
    ]
    assert auto_select(memory_only)["gpu_hotspot"] is None
    assert resolve_temps(memory_only).gpu_hotspot is None

    with_hotspot = memory_only + [
        _reading("/gpu/hotspot", "GpuNvidia", "GPU Hot Spot", 62, "GPU A"),
    ]
    assert auto_select(with_hotspot)["gpu_hotspot"] == "/gpu/hotspot"
    mapped = resolve_temps(with_hotspot, SensorMap(gpu_hotspot="/gpu/memory"))
    assert mapped.gpu_hotspot == 62


def test_missing_manual_identifier_stays_unavailable_instead_of_falling_back():
    readings = [_reading("/cpu/auto", "Cpu", "CPU Package", 54)]
    assert resolve_temps(readings).cpu == 54
    assert resolve_temps(readings, SensorMap(cpu="/cpu/no-longer-present")).cpu is None
    assert resolve_temps(readings, SensorMap(cpu=["not", "a", "string"])).cpu is None


def test_windows_native_probe_is_nonblocking_and_cached():
    now = [0.0]
    entered = threading.Event()
    release = threading.Event()
    calls = [0]

    def reader():
        calls[0] += 1
        entered.set()
        assert release.wait(2.0)
        return [_reading("/windows/acpi/thermal/0", "Cpu", "CPU ACPI Thermal Zone",
                         47, sampled_at=now[0])]

    backend = WindowsNativeSensorBackend(ttl=10, reader=reader, clock=lambda: now[0])
    backend.open()
    assert entered.wait(1.0)

    returned = threading.Event()
    result = []

    def read_cache():
        result.extend(backend.read_all())
        returned.set()

    thread = threading.Thread(target=read_cache)
    thread.start()
    assert returned.wait(0.25), "read_all blocked behind the PowerShell/WMI probe"
    assert result == []
    release.set()
    thread.join(timeout=1.0)

    deadline = time.monotonic() + 1.0
    while not backend.available and time.monotonic() < deadline:
        time.sleep(0.005)
    assert backend.available
    cached = backend.read_all()
    assert [reading.value for reading in cached] == [47]
    assert calls[0] == 1

    now[0] = 9.9
    assert backend.read_all()[0].sampled_at == 0.0
    assert calls[0] == 1
    now[0] = 10.0
    backend.read_all()  # returns cache and schedules refresh; never waits for it
    deadline = time.monotonic() + 1.0
    while calls[0] < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert calls[0] == 2
    backend.close()


def _run_all():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
