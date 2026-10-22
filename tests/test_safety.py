"""Adversarial A1-A3/B4 tests. No physical sensor or USB access is performed."""

from __future__ import annotations

import multiprocessing
import queue
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from needzero.config import Config
from needzero.controlloop import Controller
from needzero.fans.base import FanBackend, FanStatus
from needzero.fans.auto import AutoFanBackend
from needzero.fans.liquidctl_backend import LiquidctlFanBackend
from needzero.fans.process import ProcessFanBackend
from needzero.sensors.base import SensorBackend, SensorReading
from needzero.sensors.deadline import DeadlineSensorBackend


def _readings(cpu=60, gpu=55, hotspot=70, chipset=50, *, hotspot_at=None):
    out = []
    if cpu is not None:
        out.append(SensorReading("/cpu", "CPU", "Cpu", "CPU Package", cpu))
    if gpu is not None:
        out.append(SensorReading("/gpu", "GPU", "GpuNvidia", "GPU Core", gpu))
    if hotspot is not None:
        kwargs = {} if hotspot_at is None else {"sampled_at": hotspot_at}
        out.append(SensorReading(
            "/hotspot", "GPU", "GpuNvidia", "GPU Hot Spot", hotspot, **kwargs))
    if chipset is not None:
        out.append(SensorReading(
            "/chipset", "Board", "Motherboard", "Chipset", chipset))
    return out


class SequenceSensors(SensorBackend):
    name = "sequence"
    available = True

    def __init__(self, frames):
        self.frames = list(frames)
        self.index = 0

    def read_all(self):
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        if isinstance(frame, BaseException):
            raise frame
        return frame


class DeterministicFan(FanBackend):
    name = "fake"
    available = True

    def __init__(self, status_fn=None, accept_fn=None):
        self.commands = []
        self.commanded = {channel: 30.0 for channel in self.channels}
        self.status_fn = status_fn
        self.accept_fn = accept_fn
        self.reconnects = 0

    def set_duty(self, channel, duty):
        return self.set_many({channel: duty}).get(channel, False)

    def set_many(self, duties):
        duties = {channel: float(duty) for channel, duty in duties.items()}
        self.commands.append(duties)
        results = {}
        for channel, duty in duties.items():
            accepted = True if self.accept_fn is None else bool(
                self.accept_fn(channel, duty, len(self.commands)))
            results[channel] = accepted
            if accepted:
                self.commanded[channel] = duty
        return results

    def read_status(self):
        if self.status_fn:
            return self.status_fn(self)
        return {
            channel: FanStatus(
                channel, rpm=1000, duty=self.commanded[channel], mode="PWM")
            for channel in self.channels
        }


def test_missing_required_max_and_single_source_raise_sensor_fault_floor():
    cases = [
        ("max", None, 35),
        ("max", 35, None),
        ("cpu", None, 35),
        ("gpu", 35, None),
    ]
    for source, cpu, gpu in cases:
        cfg = Config.defaults()
        for group in cfg.groups.values():
            group.source = source
        fan = DeterministicFan()
        ctrl = Controller(cfg, SequenceSensors([_readings(cpu=cpu, gpu=gpu)]), fan)
        ctrl._cycle()
        snapshot = ctrl.snapshot()
        assert snapshot["sensor_fault"] is True
        assert snapshot["sensor_fault_reason"]
        assert any(
            health["required"] and not health["healthy"]
            for health in snapshot["sensor_health"].values()
        )
        assert all(group["target_duty"] >= 50 for group in snapshot["groups"].values())


def test_stale_cpu_and_gpu_are_faults_for_every_source_that_requires_them():
    stale = time.monotonic() - 60
    cases = [
        ("cpu", "cpu"),
        ("gpu", "gpu"),
        ("max", "cpu"),
        ("max", "gpu"),
    ]
    for source, stale_role in cases:
        readings = _readings(cpu=None, gpu=None)
        readings.extend([
            SensorReading(
                "/cpu", "CPU", "Cpu", "CPU Package", 60,
                sampled_at=stale if stale_role == "cpu" else time.monotonic(),
            ),
            SensorReading(
                "/gpu", "GPU", "GpuNvidia", "GPU Core", 55,
                sampled_at=stale if stale_role == "gpu" else time.monotonic(),
            ),
        ])
        cfg = Config.defaults()
        for group in cfg.groups.values():
            group.source = source
        ctrl = Controller(cfg, SequenceSensors([readings]), DeterministicFan())
        ctrl._cycle()
        snapshot = ctrl.snapshot()
        assert snapshot["sensor_fault"] is True
        assert all(
            group["target_duty"] >= 50
            for group in snapshot["groups"].values()
        )


def test_stale_reading_and_backend_exception_use_the_same_sensor_fault_path():
    stale = time.monotonic() - 60
    stale_cpu = _readings(cpu=None, gpu=55)
    stale_cpu.append(SensorReading(
        "/cpu-stale", "CPU", "Cpu", "CPU Package", 60,
        sampled_at=stale,
    ))
    for frame in (
        stale_cpu,
        OSError("sensor transport lost"),
    ):
        ctrl = Controller(Config.defaults(), SequenceSensors([frame]), DeterministicFan())
        ctrl._cycle()
        snapshot = ctrl.snapshot()
        assert snapshot["sensor_fault"] is True
        assert all(group["target_duty"] >= 50 for group in snapshot["groups"].values())


def test_emergency_is_latched_across_none_and_clears_after_three_cool_samples():
    cfg = Config.defaults()
    cfg.emergency.gpu_hotspot_threshold = 100
    frames = [
        _readings(hotspot=105),
        _readings(hotspot=None),
        _readings(hotspot=95),
        _readings(hotspot=95),
        _readings(hotspot=95),
    ]
    ctrl = Controller(cfg, SequenceSensors(frames), DeterministicFan())

    states = []
    for _frame in frames:
        ctrl._cycle()
        states.append(ctrl.snapshot()["emergency_latched"])
    assert states == [True, True, True, True, False]


def test_stale_emergency_sample_does_not_clear_latch():
    cfg = Config.defaults()
    cfg.emergency.gpu_hotspot_threshold = 100
    stale = time.monotonic() - 60
    ctrl = Controller(cfg, SequenceSensors([
        _readings(hotspot=105),
        _readings(hotspot=70, hotspot_at=stale),
    ]), DeterministicFan())
    ctrl._cycle()
    ctrl._cycle()
    assert ctrl.snapshot()["emergency_latched"] is True


def test_sensor_backend_exception_does_not_clear_emergency_latch():
    cfg = Config.defaults()
    cfg.emergency.gpu_hotspot_threshold = 100
    ctrl = Controller(cfg, SequenceSensors([
        _readings(hotspot=105),
        OSError("backend disappeared"),
    ]), DeterministicFan())
    ctrl._cycle()
    ctrl._cycle()
    snapshot = ctrl.snapshot()
    assert snapshot["emergency_latched"] is True
    assert snapshot["sensor_fault"] is True


def test_unavailable_optional_emergency_probe_does_not_raise_false_fault():
    cfg = Config.defaults()
    ctrl = Controller(
        cfg,
        SequenceSensors([_readings(cpu=45, gpu=50, hotspot=66, chipset=None)]),
        DeterministicFan(),
    )

    ctrl._cycle()
    snapshot = ctrl.snapshot()
    assert snapshot["sensor_fault"] is False
    assert snapshot["sensor_health"]["gpu_hotspot"]["required"] is True
    assert "chipset" not in snapshot["sensor_health"]


def test_observed_hotspot_loss_is_required_and_raises_safe_floor():
    cfg = Config.defaults()
    ctrl = Controller(
        cfg,
        SequenceSensors([
            _readings(cpu=35, gpu=35, hotspot=70, chipset=50),
            _readings(cpu=35, gpu=35, hotspot=None, chipset=50),
        ]),
        DeterministicFan(),
    )

    ctrl._cycle()
    assert ctrl.snapshot()["sensor_fault"] is False
    ctrl._cycle()
    snapshot = ctrl.snapshot()
    assert snapshot["sensor_fault"] is True
    assert snapshot["sensor_health"]["gpu_hotspot"]["required"] is True
    assert snapshot["sensor_health"]["gpu_hotspot"]["healthy"] is False
    assert all(
        group["effective_target_duty"] >= 50
        for group in snapshot["groups"].values()
    )


def test_legacy_chipset_mapping_is_ignored_by_control_health():
    cfg = Config.defaults()
    cfg.sensors.chipset = "/configured-chipset"
    ctrl = Controller(
        cfg,
        SequenceSensors([_readings(cpu=35, gpu=35, hotspot=70, chipset=None)]),
        DeterministicFan(),
    )

    ctrl._cycle()
    snapshot = ctrl.snapshot()
    assert snapshot["sensor_fault"] is False
    assert "chipset" not in snapshot["sensor_health"]


def test_latched_emergency_probe_loss_is_reported_as_sensor_fault():
    cfg = Config.defaults()
    cfg.emergency.gpu_hotspot_threshold = 100
    ctrl = Controller(cfg, SequenceSensors([
        _readings(hotspot=105),
        _readings(hotspot=None),
    ]), DeterministicFan())
    ctrl._cycle()
    ctrl._cycle()
    snapshot = ctrl.snapshot()
    assert snapshot["emergency_latched"] is True
    assert snapshot["sensor_fault"] is True
    assert "GPU Hotspot" in snapshot["sensor_fault_reason"]


def test_duty_mismatch_retries_then_faults_and_escalates_other_channels():
    clock = [0.0]

    def status(fan):
        return {
            channel: FanStatus(
                channel,
                rpm=1000,
                duty=(30 if channel == "fan1" else fan.commanded[channel]),
                mode="PWM",
            )
            for channel in fan.channels
        }

    fan = DeterministicFan(status_fn=status)
    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings(cpu=60, gpu=55)]), fan,
        clock=lambda: clock[0])
    for _index in range(4):
        ctrl._cycle()
        clock[0] += 1.1

    snapshot = ctrl.snapshot()
    assert snapshot["groups"]["front"]["fan_fault"] is True
    assert snapshot["groups"]["front"]["duty_commanded"] is None
    assert snapshot["groups"]["front"]["duty_reported"] == 30

    ctrl._cycle()  # immediately apply fan-fault escalation
    assert fan.commanded["fan2"] == 100
    assert fan.commanded["fan3"] == 100


def test_unknown_duty_stays_none_and_is_periodically_reasserted():
    clock = [0.0]

    def status(fan):
        return {
            channel: FanStatus(channel, rpm=1000, duty=None, mode="PWM")
            for channel in fan.channels
        }

    fan = DeterministicFan(status_fn=status)
    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]), fan,
        clock=lambda: clock[0])
    ctrl._cycle()
    assert ctrl.snapshot()["groups"]["front"]["duty_reported"] is None
    first_count = len(fan.commands)
    clock[0] = 31
    ctrl._cycle()
    assert len(fan.commands) == first_count + 1
    assert ctrl.snapshot()["groups"]["front"]["duty_reported"] is None


def test_zero_rpm_fault_waits_for_spinup_grace_and_three_cycles():
    clock = [0.0]

    def status(fan):
        return {
            channel: FanStatus(channel, rpm=0, duty=None, mode="PWM")
            for channel in fan.channels
        }

    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]),
        DeterministicFan(status_fn=status), clock=lambda: clock[0])
    for now in (0.0, 1.0, 4.9):
        clock[0] = now
        ctrl._cycle()
        assert ctrl.snapshot()["fan_fault"] is False
    for now in (5.1, 6.1, 7.1):
        clock[0] = now
        ctrl._cycle()
    assert ctrl.snapshot()["fan_fault"] is True


def test_missing_duty_and_rpm_telemetry_becomes_explicit_fault():
    clock = [0.0]

    def status(fan):
        return {
            channel: FanStatus(channel, rpm=None, duty=None, mode="PWM")
            for channel in fan.channels
        }

    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]),
        DeterministicFan(status_fn=status), clock=lambda: clock[0])
    ctrl._cycle()
    for now in (5.1, 6.1, 7.1):
        clock[0] = now
        ctrl._cycle()
    assert ctrl.snapshot()["fan_fault"] is True
    assert "telemetry" in ctrl.snapshot()["fan_fault_reason"]


def test_transport_write_failures_are_bounded_then_visible_as_fault():
    clock = [0.0]
    fan = DeterministicFan(
        accept_fn=lambda channel, duty, call: channel != "fan1")
    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]), fan,
        clock=lambda: clock[0])
    ctrl._cycle()
    assert ctrl.snapshot()["fan_fault"] is False
    first_count = len(fan.commands)
    ctrl._cycle()  # same instant: retry backoff prevents a write storm
    assert len(fan.commands) == first_count
    clock[0] = 1.1
    ctrl._cycle()
    assert ctrl.snapshot()["groups"]["front"]["fan_fault"] is True


class _ReconnectWriteFailure(FanBackend):
    name = "reconnect-write-failure"
    available = True

    def open(self):
        pass

    def close(self):
        pass

    def set_duty(self, channel, duty):
        return False

    def set_many(self, duties):
        return {channel: False for channel in duties}

    def read_status(self):
        return {channel: FanStatus(channel) for channel in self.channels}


def test_reconnect_generation_does_not_erase_repeated_write_failures():
    clock = [0.0]
    fans = AutoFanBackend(
        backend_factory=_ReconnectWriteFailure,
        retry_interval=1.0,
        clock=lambda: clock[0],
    )
    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]), fans,
        clock=lambda: clock[0],
    )

    ctrl._cycle()
    assert ctrl.snapshot()["fan_fault"] is False
    clock[0] = 1.1
    ctrl._cycle()
    assert ctrl.snapshot()["fan_fault"] is True
    assert ctrl.snapshot()["fan_fault_reasons"]


class _ReconnectStatusFailure(_ReconnectWriteFailure):
    name = "reconnect-status-failure"

    def set_many(self, duties):
        return {channel: True for channel in duties}

    def read_status(self):
        raise OSError("status transport failed")


def test_reconnect_generation_does_not_erase_missing_telemetry_failures():
    clock = [0.0]
    fans = AutoFanBackend(
        backend_factory=_ReconnectStatusFailure,
        retry_interval=1.0,
        clock=lambda: clock[0],
    )
    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]), fans,
        clock=lambda: clock[0],
    )

    for now in (0.0, 5.1, 6.2, 7.3):
        clock[0] = now
        ctrl._cycle()
    assert ctrl.snapshot()["fan_fault"] is True
    assert all(
        "status unavailable" in reason
        for reason in ctrl.snapshot()["fan_fault_reasons"].values()
    )


class BlockingSensors(SensorBackend):
    name = "blocking"
    available = True

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def read_all(self):
        self.entered.set()
        self.release.wait(5)
        return _readings()


def test_blocked_sensor_cannot_write_after_shutdown_barrier():
    sensors = BlockingSensors()
    fan = DeterministicFan()
    ctrl = Controller(Config.defaults(), sensors, fan, shutdown_timeout=0.1)
    ctrl.start()
    assert sensors.entered.wait(1)
    assert ctrl.stop(safe=True) is False  # worker is observably still blocked
    assert fan.commands and all(duty == 100 for duty in fan.commands[-1].values())
    command_count = len(fan.commands)
    sensors.release.set()
    if ctrl._thread:
        ctrl._thread.join(1)
    assert len(fan.commands) == command_count


class LowThenBlockingSensors(SensorBackend):
    name = "low-then-blocking"
    available = True

    def __init__(self):
        self.calls = 0
        self.blocked = threading.Event()
        self.release = threading.Event()

    def read_all(self):
        self.calls += 1
        if self.calls == 1:
            return _readings(cpu=35, gpu=35, hotspot=60, chipset=45)
        self.blocked.set()
        self.release.wait(2)
        return _readings(cpu=35, gpu=35, hotspot=60, chipset=45)


def test_hung_real_sensor_read_expires_cache_without_blocking_control_cycle():
    provider = LowThenBlockingSensors()
    sensors = DeadlineSensorBackend(
        lambda: provider,
        read_deadline=0.12,
        poll_interval=0.01,
        close_join=0.05,
    )
    sensors.open()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            sensors.read_all()
            break
        except TimeoutError:
            time.sleep(0.005)
    else:
        raise AssertionError("sensor worker never published its first reading")
    assert provider.blocked.wait(1)

    fan = DeterministicFan()
    ctrl = Controller(Config.defaults(), sensors, fan)
    ctrl._cycle()
    assert ctrl.snapshot()["sensor_fault"] is False

    time.sleep(0.14)
    started = time.monotonic()
    ctrl._cycle()
    elapsed = time.monotonic() - started
    snapshot = ctrl.snapshot()
    assert elapsed < 0.1
    assert snapshot["sensor_fault"] is True
    assert all(
        group["effective_target_duty"] >= 50
        for group in snapshot["groups"].values()
    )

    provider.release.set()
    sensors.close()


def test_fatal_control_worker_exit_is_observable_to_service_watchdog():
    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]), DeterministicFan())

    def crash():
        raise RuntimeError("fatal fake")

    ctrl._run_impl = crash
    ctrl.start()
    if ctrl._thread:
        ctrl._thread.join(1)
    assert not ctrl.is_alive()
    assert ctrl.fatal_error == "RuntimeError: fatal fake"
    assert ctrl.stop(safe=False) is True


def test_shutdown_does_not_lower_emergency_and_confirms_readback():
    cfg = Config.defaults()
    fan = DeterministicFan()
    ctrl = Controller(cfg, SequenceSensors([_readings(hotspot=105)]), fan)
    ctrl._cycle()
    assert ctrl.snapshot()["emergency_latched"] is True
    assert ctrl.stop(safe=True) is True
    assert all(duty == 100 for duty in fan.commands[-1].values())


def test_shutdown_uses_max_of_fifty_and_last_confirmed_duty():
    for temperature, expected in ((40, 50), (70, 70)):
        fan = DeterministicFan()
        ctrl = Controller(
            Config.defaults(),
            SequenceSensors([_readings(cpu=temperature, gpu=temperature)]),
            fan,
        )
        ctrl._cycle()
        assert ctrl.stop(safe=True) is True
        assert all(duty == expected for duty in fan.commands[-1].values())


class ReconnectOnDemandFan(DeterministicFan):
    def __init__(self):
        super().__init__()
        self.connected = False

    def set_many(self, duties):
        if not self.connected:
            self.commands.append(dict(duties))
            return {channel: False for channel in duties}
        return super().set_many(duties)

    def force_reconnect(self):
        self.reconnects += 1
        self.connected = True
        return True


def test_shutdown_gets_one_forced_reconnect_attempt():
    fan = ReconnectOnDemandFan()
    ctrl = Controller(Config.defaults(), SequenceSensors([_readings()]), fan)
    assert ctrl.stop(safe=True) is True
    assert fan.reconnects == 1
    assert all(duty == 100 for duty in fan.commands[-1].values())


def test_unconfirmed_shutdown_write_is_failure():
    def status(fan):
        return {
            channel: FanStatus(channel, rpm=1000, duty=None, mode="PWM")
            for channel in fan.channels
        }

    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]),
        DeterministicFan(status_fn=status))
    assert ctrl.stop(safe=True) is False
    assert "not confirmed" in ctrl.shutdown_error


def test_liquidctl_never_substitutes_last_command_for_unknown_duty():
    class RpmOnlyDevice:
        def get_status(self):
            return [("Fan 1 speed", 900, "rpm")]

    backend = LiquidctlFanBackend()
    backend._dev = RpmOnlyDevice()
    backend.channels = ("fan1",)
    backend._last_duty["fan1"] = 80
    status = backend.read_status()["fan1"]
    assert status.rpm == 900
    assert status.duty is None


def _blocking_process_worker(conn, late_marker):
    try:
        while True:
            request_id, operation, payload = conn.recv()
            if operation == "open":
                conn.send((request_id, True, {
                    "name": "fake-process",
                    "available": True,
                    "description": "blocking fake",
                    "channels": ["fan1", "fan2", "fan3"],
                    "controller_id": "fake:A",
                }))
            elif operation == "set_many":
                time.sleep(60)
                late_marker.set()
                conn.send((request_id, True, {channel: True for channel in payload}))
            elif operation == "close":
                conn.send((request_id, True, None))
                return
    finally:
        conn.close()


def test_process_proxy_terminates_a_blocked_writer_before_it_can_finish_late():
    ctx = multiprocessing.get_context("spawn")
    late_marker = ctx.Event()
    backend = ProcessFanBackend(
        io_timeout=0.15,
        open_timeout=1.0,
        terminate_timeout=0.5,
        worker_target=_blocking_process_worker,
        worker_args=(late_marker,),
    )
    backend.open()
    started = time.monotonic()
    assert backend.set_many({"fan1": 50}) == {"fan1": False}
    assert time.monotonic() - started < 2
    time.sleep(0.2)
    assert not late_marker.is_set()
    assert not backend.available


def _two_generation_fan_worker(
        conn, generation, first_write_entered, late_write, safe_writes):
    with generation.get_lock():
        generation.value += 1
        own_generation = generation.value
    reported = {"fan1": 30.0, "fan2": 30.0, "fan3": 30.0}
    try:
        while True:
            request_id, operation, payload = conn.recv()
            if operation == "open":
                conn.send((request_id, True, {
                    "name": "two-generation-fake",
                    "available": True,
                    "description": "blocked-old/safe-new",
                    "channels": ["fan1", "fan2", "fan3"],
                    "controller_id": "fake:stable",
                }))
            elif operation == "set_many":
                if own_generation == 1:
                    first_write_entered.set()
                    time.sleep(60)
                    late_write.set()
                else:
                    reported.update({key: float(value) for key, value in payload.items()})
                    safe_writes.put(dict(payload))
                    conn.send((request_id, True, {
                        channel: True for channel in payload
                    }))
            elif operation == "read_status":
                conn.send((request_id, True, {
                    channel: {"rpm": 1200, "duty": duty, "mode": "PWM"}
                    for channel, duty in reported.items()
                }))
            elif operation == "close":
                conn.send((request_id, True, None))
                return
    finally:
        conn.close()


def test_stop_during_blocked_usb_write_kills_old_generation_before_safe_write():
    ctx = multiprocessing.get_context("spawn")
    generation = ctx.Value("i", 0)
    first_write_entered = ctx.Event()
    late_write = ctx.Event()
    safe_writes = ctx.Queue()
    fan = ProcessFanBackend(
        io_timeout=0.15,
        open_timeout=1.0,
        terminate_timeout=0.3,
        worker_target=_two_generation_fan_worker,
        worker_args=(generation, first_write_entered, late_write, safe_writes),
    )
    fan.open()
    ctrl = Controller(
        Config.defaults(), SequenceSensors([_readings()]), fan,
        shutdown_timeout=1.0,
    )
    cycle = threading.Thread(target=ctrl._cycle)
    cycle.start()
    assert first_write_entered.wait(1)

    assert ctrl.stop(safe=True) is True
    cycle.join(2)
    assert not cycle.is_alive()
    assert generation.value == 2
    safe = safe_writes.get(timeout=1)
    assert safe and all(float(duty) == 100.0 for duty in safe.values())
    time.sleep(0.2)
    assert not late_write.is_set()
    try:
        unexpected = safe_writes.get_nowait()
    except queue.Empty:
        unexpected = None
    assert unexpected is None
    fan.close()


class FakeLiquidctlDevice:
    description = "NZXT RGB & Fan Controller"
    vendor_id = 0x1E71
    product_id = 0x2009
    _speed_channels = {"fan1": 1, "fan2": 2, "fan3": 3}

    def __init__(self, serial):
        self.serial_number = serial
        self.connected = False

    def connect(self):
        self.connected = True

    def initialize(self):
        return []

    def disconnect(self):
        self.connected = False


class AddressOnlyLiquidctlDevice(FakeLiquidctlDevice):
    def __init__(self, address):
        super().__init__(serial=None)
        self.address = address
        self.bus = 1


def _fake_liquidctl_module(devices):
    module = types.ModuleType("liquidctl")
    module.find_liquidctl_devices = lambda: list(devices)
    return module


def test_stable_controller_selection_ignores_enumeration_order():
    first_a = FakeLiquidctlDevice("A")
    first_b = FakeLiquidctlDevice("B")
    selected_id = LiquidctlFanBackend.device_identity(first_a)
    with patch.dict(sys.modules, {
        "liquidctl": _fake_liquidctl_module([first_a, first_b])}):
        backend = LiquidctlFanBackend(preferred_controller_id=selected_id)
        backend.open()
        assert backend._dev is first_a
        backend.close()

    second_a = FakeLiquidctlDevice("A")
    second_b = FakeLiquidctlDevice("B")
    with patch.dict(sys.modules, {
        "liquidctl": _fake_liquidctl_module([second_b, second_a])}):
        backend = LiquidctlFanBackend(preferred_controller_id=selected_id)
        backend.open()
        assert backend._dev is second_a
        assert backend.controller_id == selected_id
        backend.close()


def test_missing_selected_controller_never_switches_to_neighbor():
    selected_id = LiquidctlFanBackend.device_identity(FakeLiquidctlDevice("A"))
    only_b = FakeLiquidctlDevice("B")
    with patch.dict(sys.modules, {
        "liquidctl": _fake_liquidctl_module([only_b])}):
        backend = LiquidctlFanBackend(preferred_controller_id=selected_id)
        try:
            backend.open()
        except RuntimeError as exc:
            assert "refusing to switch" in str(exc)
        else:
            raise AssertionError("backend silently switched to controller B")
        assert not only_b.connected


def test_multiple_controllers_require_explicit_selection():
    devices = [FakeLiquidctlDevice("A"), FakeLiquidctlDevice("B")]
    with patch.dict(sys.modules, {
        "liquidctl": _fake_liquidctl_module(devices)}):
        try:
            LiquidctlFanBackend().open()
        except RuntimeError as exc:
            assert "Multiple supported" in str(exc)
        else:
            raise AssertionError("backend silently selected the first controller")


def test_address_only_controller_is_not_persisted_as_stable_identity():
    first = AddressOnlyLiquidctlDevice(address=4)
    second = AddressOnlyLiquidctlDevice(address=9)
    assert LiquidctlFanBackend.device_identity(first) is None
    assert LiquidctlFanBackend.device_identity(second) is None
    for device in (first, second):
        with patch.dict(sys.modules, {
            "liquidctl": _fake_liquidctl_module([device])}):
            try:
                LiquidctlFanBackend().open()
            except RuntimeError as exc:
                assert "unstable USB address" in str(exc)
            else:
                raise AssertionError("address-only controller was auto-selected")
            assert not device.connected


def _run_all():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} safety tests passed")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _run_all()
