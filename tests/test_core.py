"""Core-logic tests that run without any hardware (mock backends only).

Run:  python -m pytest tests/  -q      (from the project root)
  or: python tests/test_core.py        (plain, no pytest needed)
"""

import sys
import tempfile
import time
import uuid
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from needzero import config as cfgmod
from needzero.config import Config
from needzero.config import ConfigError
from needzero.curves import Temps, interpolate, decide_all, emergency_tripped
from needzero.sensors import CombinedSensorBackend
from needzero.sensors.base import SensorReading, auto_select, resolve_temps
from needzero.sensors.mock import MockSensorBackend
from needzero.fans.auto import AutoFanBackend
from needzero.fans.liquidctl_backend import LiquidctlFanBackend
from needzero.fans.mock import MockFanBackend
from needzero.controlloop import Controller
from needzero.api import Api
from needzero.single_instance import SingleInstance


class MemoryApi(Api):
    def _mutate(self, fn):
        cfg = cfgmod.clone(self._ctrl.get_config())
        fn(cfg)
        cfg.validate()
        self._ctrl.set_config(cfg)
        return cfg.to_dict()


# ---- curve interpolation matches the spec ---------------------------------

def test_spec_curve_points():
    curve = [(40, 30), (50, 40), (60, 55), (70, 70), (80, 85), (85, 100)]
    assert interpolate(curve, 30) == 30          # clamp below
    assert interpolate(curve, 40) == 30
    assert interpolate(curve, 50) == 40
    assert interpolate(curve, 60) == 55
    assert interpolate(curve, 70) == 70
    assert interpolate(curve, 80) == 85
    assert interpolate(curve, 85) == 100
    assert interpolate(curve, 95) == 100         # clamp above
    # midpoint interpolation
    assert interpolate(curve, 55) == 47.5


# ---- MAX(CPU, GPU) source selection ---------------------------------------

def test_max_source():
    t = Temps(cpu=55, gpu=70)
    assert t.control_value("cpu") == 55
    assert t.control_value("gpu") == 70
    assert t.control_value("max") == 70
    assert Temps(cpu=None, gpu=48).control_value("max") == 48
    assert Temps(cpu=None, gpu=None).control_value("max") is None


# ---- full decision: mode, floor, emergency --------------------------------

def test_decision_balanced_and_floor():
    cfg = Config.defaults()
    cfg.mode = "balanced"
    d = decide_all(cfg, Temps(cpu=60, gpu=50))   # max=60 -> curve 55%
    assert d["front"].control_temp == 60
    assert d["front"].curve_duty == 55
    assert d["front"].target_duty == 55

def test_min_duty_floor():
    cfg = Config.defaults()
    d = decide_all(cfg, Temps(cpu=25, gpu=25))   # curve -> 30, floor 20 -> 30
    assert d["front"].target_duty == 30
    cfg.min_duty = 45                            # floor overrides low curve
    d = decide_all(cfg, Temps(cpu=25, gpu=25))
    assert d["front"].target_duty == 45

def test_mode_presets_load_and_apply():
    cfg = Config.defaults()
    ctrl = Controller(cfg, MockSensorBackend(), MockFanBackend())
    api = MemoryApi(ctrl)
    api.set_curve("front", [[40, 42], [85, 90]])
    api.apply_now()
    api.set_mode("performance")
    assert ctrl.get_config().groups["front"].curve != [[40, 42], [85, 90]]
    api.set_mode("balanced")
    assert ctrl.get_config().groups["front"].curve == [[40, 42], [85, 90]]


def test_reset_makes_curves_flat_40():
    cfg = Config.defaults()
    ctrl = Controller(cfg, MockSensorBackend(), MockFanBackend())
    api = MemoryApi(ctrl)
    api.set_curve("front", [[40, 35], [85, 95]])
    api.reset_defaults()
    for group in ctrl.get_config().groups.values():
        assert [d for _, d in group.curve] == [40, 40, 40, 40, 40, 40]


def test_settings_api_roundtrip():
    cfg = Config.defaults()
    ctrl = Controller(cfg, MockSensorBackend(), MockFanBackend())
    api = MemoryApi(ctrl)
    api.apply_settings({
        "channels": {"front": "fan3", "top": "fan2", "rear": "fan1"},
        "sensors": {"cpu": "/cpu/0", "gpu": None, "gpu_hotspot": None},
        "min_duty": 33,
        "poll_interval": 1.5,
        "emergency": {"enabled": False, "gpu_hotspot_threshold": 99, "duty": 77},
    })
    back = ctrl.get_config()
    assert back.groups["front"].channel == "fan3"
    assert back.sensors.cpu == "/cpu/0"
    assert back.min_duty == 33
    assert back.poll_interval == 1.5
    assert not back.emergency.enabled
    assert back.emergency.gpu_hotspot_threshold == 99
    assert back.emergency.duty == 77


def test_duplicate_fan_channels_are_repaired_and_atomic_api_is_safe():
    cfg = Config.defaults()
    for group in cfg.groups.values():
        group.channel = "fan1"
    cfg.validate()
    assert {g.channel for g in cfg.groups.values()} == {"fan1", "fan2", "fan3"}

    ctrl = Controller(cfg, MockSensorBackend(), MockFanBackend())
    api = MemoryApi(ctrl)
    settings = {
        "channels": {"front": "fan3", "top": "fan1", "rear": "fan2"},
        "sensors": {"cpu": None, "gpu": None, "gpu_hotspot": None},
        "min_duty": cfg.min_duty, "poll_interval": cfg.poll_interval,
        "emergency": {"enabled": cfg.emergency.enabled,
                      "gpu_hotspot_threshold": cfg.emergency.gpu_hotspot_threshold,
                      "duty": cfg.emergency.duty},
    }
    api.apply_settings(settings)
    assert [ctrl.get_config().groups[g].channel for g in ("front", "top", "rear")] == [
        "fan3", "fan1", "fan2"]
    settings["channels"] = {"front": "fan1", "top": "fan1", "rear": "fan1"}
    try:
        api.apply_settings(settings)
    except ConfigError:
        pass
    else:
        raise AssertionError("duplicate channels were accepted")
    assert [ctrl.get_config().groups[g].channel for g in ("front", "top", "rear")] == [
        "fan3", "fan1", "fan2"]


def test_corrupt_null_config_is_sanitized_and_emergency_is_bounded():
    cfg = Config.from_dict({
        "min_duty": None,
        "deadband": None,
        "poll_interval": None,
        "groups": {"front": {"curve": [[40, None], [80, 90]]}},
        "emergency": {
            "enabled": None,
            "gpu_hotspot_threshold": 0,
            "chipset_threshold": 999,
            "duty": 0,
        },
    })
    assert cfg.min_duty == 20
    assert cfg.deadband == 2
    assert cfg.poll_interval == 2
    assert cfg.groups["front"].curve == cfgmod.Config.defaults().groups["front"].curve
    assert cfg.emergency.enabled is True
    assert cfg.emergency.gpu_hotspot_threshold == 60
    assert cfg.emergency.chipset_threshold == 120
    assert cfg.emergency.duty == 50

def test_emergency_override():
    cfg = Config.defaults()
    cfg.emergency.gpu_hotspot_threshold = 100
    normal = Temps(cpu=45, gpu=45, gpu_hotspot=80, chipset=50)
    assert not emergency_tripped(cfg, normal)
    hot = Temps(cpu=45, gpu=45, gpu_hotspot=105, chipset=50)
    assert emergency_tripped(cfg, hot)
    d = decide_all(cfg, hot)
    assert all(g.emergency and g.target_duty == 100 for g in d.values())


# ---- sensor auto-detection on the mock readings ---------------------------

def test_auto_select_and_resolve():
    readings = MockSensorBackend().read_all()
    picked = auto_select(readings)
    ids = {r.identifier: r for r in readings}
    assert "Tctl" in ids[picked["cpu"]].name
    assert ids[picked["gpu"]].name == "GPU Core"
    assert "Hot Spot" in ids[picked["gpu_hotspot"]].name
    assert "chipset" not in picked
    temps = resolve_temps(readings)
    assert temps.cpu is not None and temps.gpu is not None
    assert temps.gpu_hotspot > temps.gpu           # hotspot hotter than core


def test_gpu_core_matches_hotspot_gpu():
    readings = [
        SensorReading("/igpu/temp/0", "AMD Radeon Graphics", "GpuAmd", "GPU Core", 68),
        SensorReading("/dgpu/temp/0", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Core", 52),
        SensorReading("/dgpu/temp/2", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Hot Spot", 61),
    ]
    picked = auto_select(readings)
    assert picked["gpu"] == "/dgpu/temp/0"
    assert picked["gpu_hotspot"] == "/dgpu/temp/2"


def test_nvml_core_used_when_hotspot_source_has_no_core():
    readings = [
        SensorReading("/external/gpu/hotspot", "NVIDIA GeForce RTX 4070", "GpuNvidia",
                      "GPU Hot Spot", 61),
        SensorReading("/nvml/gpu/0/temperature/0", "NVIDIA GeForce RTX 4070", "GpuNvidia",
                      "GPU Core", 52),
    ]
    picked = auto_select(readings)
    assert picked["gpu"] == "/nvml/gpu/0/temperature/0"
    assert picked["gpu_hotspot"] == "/external/gpu/hotspot"


def test_invalid_zero_temperature_is_ignored():
    readings = [
        SensorReading("/cpu/bad", "AMD Ryzen", "Cpu", "Core (Tctl/Tdie)", 0),
        SensorReading("/cpu/package", "AMD Ryzen", "Cpu", "CPU Package", 54),
    ]
    assert auto_select(readings)["cpu"] == "/cpu/package"
    assert resolve_temps(readings).cpu == 54


def test_stale_sensor_reading_is_rejected():
    stale = SensorReading(
        "/cpu/stale", "AMD Ryzen", "Cpu", "Core (Tctl/Tdie)", 62,
        sampled_at=time.monotonic() - 60,
    )
    assert auto_select([stale])["cpu"] is None
    assert resolve_temps([stale]).cpu is None

    fresh_same_value = SensorReading(
        "/cpu/fresh", "AMD Ryzen", "Cpu", "Core (Tctl/Tdie)", 62,
    )
    assert resolve_temps([fresh_same_value]).cpu == 62


def test_bad_manual_gpu_mapping_falls_back_to_sane_auto():
    readings = [
        SensorReading("/gpu/core", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Core", 50),
        SensorReading("/gpu/hotspot", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Hot Spot", 58),
        SensorReading("/gpu/memory", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Memory Junction", 72),
    ]
    cfg = Config.defaults()
    cfg.sensors.gpu = "/gpu/memory"
    cfg.sensors.gpu_hotspot = "/gpu/hotspot"
    temps = resolve_temps(readings, cfg.sensors)
    assert temps.gpu == 50
    assert temps.gpu_hotspot == 58


def test_combined_backend_uses_fallback_cpu_with_lhm_gpu_hotspot():
    readings = [
        SensorReading("/lhm/cpu/tctl", "AMD Ryzen", "Cpu", "Core (Tctl/Tdie)", None),
        SensorReading("/lhm/gpu/core", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Core", 53),
        SensorReading("/lhm/gpu/hotspot", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Hot Spot", 61),
        SensorReading("/hwinfo/cpu/tctl", "AMD Ryzen", "Cpu", "CPU (Tctl/Tdie)", 57),
    ]
    temps = resolve_temps(readings)
    assert temps.cpu == 57
    assert temps.gpu == 53
    assert temps.gpu_hotspot == 61

def test_missing_manual_sensor_mapping_stays_unavailable():
    readings = [
        SensorReading("/gpu/core", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Core", 50),
        SensorReading("/gpu/hotspot", "NVIDIA GeForce RTX 4070", "GpuNvidia", "GPU Hot Spot", 58),
    ]
    cfg = Config.defaults()
    cfg.sensors.gpu = "/old/mock/gpu"
    cfg.sensors.gpu_hotspot = "/old/mock/hotspot"
    temps = resolve_temps(readings, cfg.sensors)
    assert temps.gpu is None
    assert temps.gpu_hotspot is None


# ---- config round-trips through JSON --------------------------------------

def test_temperature_indicators_are_kept():
    t = Temps(cpu=55, gpu=70, gpu_hotspot=88)
    assert t.control_value("max") == 70
    assert t.gpu_hotspot == 88


def test_logs_keep_scrollable_history():
    html = (Path(__file__).resolve().parents[1] / "src" / "needzero" / "webui" / "index.html").read_text(encoding="utf-8")
    assert "overflow-y: auto" in html
    assert "host.children.length > 200" in html
    assert "host.scrollTop = host.scrollHeight" in html


def test_webview_api_waits_for_pywebview_without_duplicate_mock_runtime():
    html = (Path(__file__).resolve().parents[1] / "src" / "needzero" / "webui" / "index.html").read_text(encoding="utf-8")
    assert "function MockApi()" not in html
    assert "previewMode" not in html
    assert "NICKNAMES" not in html


def test_monotonic_curve_enforced():
    cfg = Config.defaults()
    # a deliberately non-monotonic curve: 30 -> 60 -> 45 -> 80
    cfg.groups["front"].curve = [[40, 30], [50, 60], [60, 45], [70, 80]]
    cfg.validate()
    duties = [d for _, d in cfg.groups["front"].curve]
    assert duties == sorted(duties)          # non-decreasing
    assert duties == [30, 60, 60, 80]        # the 45 was lifted to 60


def test_curve_always_keeps_at_least_two_points():
    assert len(cfgmod.sanitize_curve_points([])) >= 2
    assert len(cfgmod.sanitize_curve_points([[50, 40]])) >= 2
    assert cfgmod.sanitize_curve_points([[40, 30], [40, 50], [80, 90]]) == [
        [40, 50], [80, 90]]


def test_sensor_backend_choices_are_preserved():
    cfg = Config.defaults()
    for backend in ("auto", "lhm", "native", "nvml", "mock"):
        cfg.sensor_backend = backend
        cfg.validate()
        assert cfg.sensor_backend == backend
    cfg.sensor_backend = "bogus"
    cfg.validate()
    assert cfg.sensor_backend == "auto"


def test_config_roundtrip(tmp_path=None):
    d = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    p = d / "config.json"
    cfg = Config.defaults()
    cfg.mode = "silent"
    cfg.groups["top"].source = "gpu"
    cfg.groups["rear"].curve = [[30, 25], [60, 60], [90, 100]]
    cfgmod.save(cfg, p)
    back = cfgmod.load(p)
    assert back.mode == "silent"
    assert back.groups["top"].source == "gpu"
    assert back.groups["rear"].curve == [[30, 25], [60, 60], [90, 100]]


# ---- the whole control loop produces a coherent snapshot ------------------

def test_controller_cycle_snapshot():
    cfg = Config.defaults()
    ctrl = Controller(cfg, MockSensorBackend(), MockFanBackend())
    ctrl._cycle()                                 # one synchronous cycle
    snap = ctrl.snapshot()
    assert set(snap["groups"]) == {"front", "top", "rear"}
    for g in snap["groups"].values():
        assert 0 <= g["target_duty"] <= 100
        assert g["rpm"] is not None
    assert snap["backends"]["sensors"] == "mock"
    assert snap["temps"]["cpu"] is not None


class _FailingWriteFan(MockFanBackend):
    def __init__(self):
        super().__init__()
        self.calls = []

    def set_many(self, duties):
        self.calls.append(dict(duties))
        return {channel: False for channel in duties}


def test_failed_fan_write_is_retried_after_bounded_backoff():
    fan = _FailingWriteFan()
    clock = [0.0]
    ctrl = Controller(Config.defaults(), MockSensorBackend(), fan,
                      clock=lambda: clock[0])
    ctrl._cycle()
    ctrl._cycle()
    assert len(fan.calls) == 1
    clock[0] = 1.0
    ctrl._cycle()
    assert len(fan.calls) == 2
    assert set(fan.calls[0]) == {"fan1", "fan2", "fan3"}
    assert set(fan.calls[1]) == {"fan1", "fan2", "fan3"}
    assert all(group["duty_commanded"] is None for group in ctrl.snapshot()["groups"].values())


def test_liquidctl_write_error_is_reported_as_failure():
    class BrokenDevice:
        def set_fixed_speed(self, channel, duty):
            raise OSError("USB write failed")

    backend = LiquidctlFanBackend()
    backend._dev = BrokenDevice()
    assert backend.set_duty("fan1", 50) is False
    assert "fan1" not in backend._last_duty


def test_liquidctl_status_error_is_propagated():
    class BrokenDevice:
        def get_status(self):
            raise OSError("USB read failed")

    backend = LiquidctlFanBackend()
    backend._dev = BrokenDevice()
    try:
        backend.read_status()
    except OSError as exc:
        assert str(exc) == "USB read failed"
    else:
        raise AssertionError("status read failure was swallowed")


def test_auto_fan_backend_reconnects_after_initial_discovery_failure():
    now = [0.0]
    attempts = [0]

    class NotReady(MockFanBackend):
        def open(self):
            raise OSError("USB not ready")

    def factory():
        attempts[0] += 1
        return NotReady() if attempts[0] == 1 else MockFanBackend()

    backend = AutoFanBackend(factory, retry_interval=5, clock=lambda: now[0])
    backend.open()
    assert backend.set_many({"fan1": 40}) == {"fan1": False}
    now[0] = 6
    assert backend.set_many({"fan1": 40}) == {"fan1": True}
    assert backend.available
    assert attempts[0] == 2


def test_auto_fan_backend_drops_a_lost_status_connection():
    class LostStatusFan(MockFanBackend):
        def read_status(self):
            raise OSError("USB disconnected")

    backend = AutoFanBackend(lambda: LostStatusFan(), retry_interval=5)
    backend.open()
    generation = backend.connection_generation
    try:
        backend.read_status()
    except OSError as exc:
        assert "USB disconnected" in str(exc)
    else:
        raise AssertionError("status transport error was hidden")
    assert not backend.available
    assert backend.connection_generation == generation + 1
    try:
        backend.read_status()
    except RuntimeError as exc:
        assert "USB disconnected" in str(exc)
    else:
        raise AssertionError("disconnected status was presented as telemetry")


def test_empty_combined_sensor_backend_is_unavailable():
    backend = CombinedSensorBackend([])
    assert not backend.available
    assert backend.name == "unavailable"
    assert backend.read_all() == []


def test_single_instance_mutex_blocks_a_second_writer_on_windows():
    if os.name != "nt":
        return
    name = rf"Local\NeedZeroXtraTrash.Test.{uuid.uuid4()}"
    first, second = SingleInstance(name), SingleInstance(name)
    try:
        assert first.acquire()
        assert not second.acquire()
    finally:
        first.release()
        second.release()


def test_reported_version_matches_version_file():
    from needzero import BASE_VERSION, BUILD_CHANNEL, __version__

    expected = (Path(__file__).resolve().parents[1] / "version.txt").read_text(
        encoding="utf-8").strip()
    assert BASE_VERSION == expected
    assert __version__ == (
        expected if BUILD_CHANNEL == "release" else f"{expected}-dev"
    )


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
