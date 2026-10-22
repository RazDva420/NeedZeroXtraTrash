"""Configuration model + JSON persistence for NeedZeroXtraTrash.

The controller config lives in protected machine data (normally
``%ProgramData%/NeedZeroXtraTrash/config.json``). Legacy per-user config is
migrated explicitly during installation. The model remains importable and
testable on Linux/macOS.

Everything here is plain-Python and dependency-free so the control logic can be
unit-tested without any hardware present.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# The fan curve requested in the spec, expressed as (temperature_C, duty_%).
# Values below the first point clamp to 30 %, above the last clamp to 100 %.
DEFAULT_CURVE: List[Tuple[float, float]] = [
    (40, 30),
    (50, 40),
    (60, 55),
    (70, 70),
    (80, 85),
    (85, 100),
]

# Which temperature a group reacts to.
SOURCE_CPU = "cpu"          # CPU (Tctl/Tdie)
SOURCE_GPU = "gpu"          # GPU Temperature
SOURCE_MAX = "max"          # MAX(CPU Tctl/Tdie, GPU Temperature) - spec default

VALID_SOURCES = (SOURCE_CPU, SOURCE_GPU, SOURCE_MAX)

# Fan modes shown in the UI; each mode selects a stored per-group curve preset.
MODES = ("silent", "balanced", "performance")

MODE_PRESET_CURVES: Dict[str, List[Tuple[float, float]]] = {
    "silent": [
        (40, 30), (50, 35), (60, 45), (70, 60), (80, 80), (85, 100),
    ],
    "balanced": DEFAULT_CURVE,
    "performance": [
        (35, 40), (45, 50), (55, 65), (65, 80), (75, 95), (80, 100),
    ],
}

# The three physical fan groups and their default liquidctl channel mapping.
# (This mapping mirrors how the fans are physically plugged into the
# controller; it can be changed in the app's settings.)
DEFAULT_GROUP_ORDER = ["front", "top", "rear"]
DEFAULT_GROUP_LABELS = {"front": "Front Intake", "top": "Top Exhaust", "rear": "Rear Exhaust"}
DEFAULT_GROUP_CHANNELS = {"front": "fan1", "top": "fan2", "rear": "fan3"}
SCHEMA_VERSION = 2


class ConfigError(ValueError):
    """Configuration rejected at an API trust boundary."""

    def __init__(self, message: str, fields: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


class ConfigReadError(OSError):
    """Existing configuration could not be read safely; do not overwrite it."""


def _default_curve() -> List[List[float]]:
    return [[float(t), float(d)] for t, d in DEFAULT_CURVE]


def flat_curve(duty: float = 40.0) -> List[List[float]]:
    """Return the default temperature points with a fixed duty."""
    duty = _clamp(duty, 0, 100)
    return [[float(t), float(duty)] for t, _ in DEFAULT_CURVE]


def _mode_curve(mode: str) -> List[List[float]]:
    return [[float(t), float(d)] for t, d in MODE_PRESET_CURVES.get(mode, DEFAULT_CURVE)]


def _default_mode_presets() -> Dict[str, Dict[str, List[List[float]]]]:
    return {
        mode: {name: _mode_curve(mode) for name in DEFAULT_GROUP_ORDER}
        for mode in MODES
    }


def capture_group_curves(groups: Dict[str, "GroupConfig"]) -> Dict[str, List[List[float]]]:
    return {
        name: sanitize_curve_points(group.curve)
        for name, group in groups.items()
        if name in DEFAULT_GROUP_ORDER
    }


def save_active_mode_preset(cfg: "Config") -> None:
    cfg.mode_presets[cfg.mode] = capture_group_curves(cfg.groups)


def load_mode_preset(cfg: "Config", mode: str) -> None:
    if mode not in MODES:
        return
    cfg.mode = mode
    preset = cfg.mode_presets.get(mode) or _default_mode_presets()[mode]
    for name, group in cfg.groups.items():
        if name in preset:
            group.curve = sanitize_curve_points(preset[name])


def sanitize_curve_points(points) -> List[List[float]]:
    """Clamp, sort, de-duplicate and enforce a non-decreasing fan curve."""
    clean = []
    try:
        for p in points:
            t = float(p[0])
            d = float(p[1])
            if not math.isfinite(t) or not math.isfinite(d):
                raise ValueError("curve point is not finite")
            t = _clamp(t, 0, 120)
            d = _clamp(d, 0, 100)
            clean.append([round(t, 1), round(d, 1)])
    except (TypeError, ValueError, IndexError):
        return _default_curve()

    if len(clean) < 2:
        return _default_curve()

    # dict keeps the last duty for duplicate temperatures.
    clean = [[t, d] for t, d in sorted(dict(clean).items())]
    if len(clean) < 2:
        return _default_curve()

    for k in range(1, len(clean)):
        if clean[k][1] < clean[k - 1][1]:
            clean[k][1] = clean[k - 1][1]
    return clean


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GroupConfig:
    """One controllable fan group (front / top / rear)."""

    label: str
    channel: str                       # liquidctl channel: fan1 / fan2 / fan3
    source: str = SOURCE_MAX           # cpu | gpu | max
    curve: List[List[float]] = field(default_factory=_default_curve)

    def sanitized_curve(self) -> List[Tuple[float, float]]:
        pts = [(float(t), float(d)) for t, d in self.curve]
        pts.sort(key=lambda p: p[0])
        return pts


@dataclass
class EmergencyConfig:
    """Force fans high when the GPU hot-spot sensor crosses its threshold."""

    enabled: bool = True
    gpu_hotspot_threshold: float = 100.0    # deg C
    chipset_threshold: float = 90.0         # legacy JSON field; no longer used
    duty: float = 100.0                     # % applied to every group when tripped


@dataclass
class SensorMap:
    """Manual override of which raw sensor feeds each logical role.

    ``None`` means "auto-detect".  Values are opaque sensor identifiers as
    reported by the active sensor backend (``SensorReading.identifier``).
    """

    cpu: Optional[str] = None
    gpu: Optional[str] = None
    gpu_hotspot: Optional[str] = None
    chipset: Optional[str] = None            # legacy JSON field; no longer used


@dataclass
class Config:
    schema_version: int = SCHEMA_VERSION
    poll_interval: float = 2.0          # seconds between control cycles
    mode: str = "balanced"              # silent | balanced | performance
    min_duty: float = 20.0              # never drive a group below this
    deadband: float = 2.0               # skip USB writes smaller than this (%)
    frameless: bool = True              # draw our own Win95 title bar
    sensor_backend: str = "auto"        # auto | lhm | native | nvml | mock
    fan_backend: str = "auto"           # auto | liquidctl | mock
    controller_id: Optional[str] = None  # stable VID:PID:serial/path identity
    groups: Dict[str, GroupConfig] = field(default_factory=dict)
    mode_presets: Dict[str, Dict[str, List[List[float]]]] = field(default_factory=dict)
    emergency: EmergencyConfig = field(default_factory=EmergencyConfig)
    sensors: SensorMap = field(default_factory=SensorMap)

    # -- construction -----------------------------------------------------

    @classmethod
    def defaults(cls) -> "Config":
        groups = {
            name: GroupConfig(
                label=DEFAULT_GROUP_LABELS[name],
                channel=DEFAULT_GROUP_CHANNELS[name],
            )
            for name in DEFAULT_GROUP_ORDER
        }
        return cls(groups=groups, mode_presets=_default_mode_presets())

    # -- (de)serialization ------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        cfg = cls.defaults()
        if not isinstance(data, dict):
            return cfg
        for key in ("schema_version", "poll_interval", "mode", "min_duty", "deadband",
                    "frameless", "sensor_backend", "fan_backend", "controller_id"):
            if key in data:
                setattr(cfg, key, data[key])

        groups = data.get("groups")
        if isinstance(groups, dict):
            for name, gd in groups.items():
                if name not in cfg.groups or not isinstance(gd, dict):
                    continue
                g = cfg.groups[name]
                g.channel = gd.get("channel", g.channel)
                g.source = gd.get("source", g.source)
                if isinstance(gd.get("curve"), list) and gd["curve"]:
                    g.curve = gd["curve"]

        presets = data.get("mode_presets")
        if isinstance(presets, dict):
            for mode, groupset in presets.items():
                if mode not in MODES or not isinstance(groupset, dict):
                    continue
                cfg.mode_presets.setdefault(mode, {})
                for name, points in groupset.items():
                    if name in cfg.groups and isinstance(points, list) and points:
                        cfg.mode_presets[mode][name] = sanitize_curve_points(points)
        else:
            cfg.mode_presets[cfg.mode if cfg.mode in MODES else "balanced"] = capture_group_curves(cfg.groups)

        em = data.get("emergency")
        if isinstance(em, dict):
            cfg.emergency = EmergencyConfig(
                enabled=em.get("enabled", cfg.emergency.enabled),
                gpu_hotspot_threshold=em.get(
                    "gpu_hotspot_threshold", cfg.emergency.gpu_hotspot_threshold),
                chipset_threshold=em.get(
                    "chipset_threshold", cfg.emergency.chipset_threshold),
                duty=em.get("duty", cfg.emergency.duty),
            )

        sm = data.get("sensors")
        if isinstance(sm, dict):
            cfg.sensors = SensorMap(
                cpu=sm.get("cpu"),
                gpu=sm.get("gpu"),
                gpu_hotspot=sm.get("gpu_hotspot"),
                chipset=sm.get("chipset"),
            )

        cfg.validate()
        return cfg

    # -- validation -------------------------------------------------------

    def validate(self) -> None:
        self.schema_version = SCHEMA_VERSION
        if self.mode not in MODES:
            self.mode = "balanced"
        self.min_duty = _clamp(self.min_duty, 0, 100, 20)
        self.deadband = _clamp(self.deadband, 0, 25, 2)
        self.poll_interval = _clamp(self.poll_interval, 0.5, 30, 2)
        if self.sensor_backend not in ("auto", "lhm", "native", "nvml", "mock"):
            self.sensor_backend = "auto"
        if self.fan_backend not in ("auto", "liquidctl", "mock"):
            self.fan_backend = "auto"
        if self.controller_id is not None:
            if not isinstance(self.controller_id, str) or not self.controller_id.strip():
                self.controller_id = None
            else:
                self.controller_id = self.controller_id.strip()

        for role in ("cpu", "gpu", "gpu_hotspot", "chipset"):
            value = getattr(self.sensors, role, None)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                setattr(self.sensors, role, None)
            elif isinstance(value, str):
                value = value.strip()
                setattr(self.sensors, role,
                        None if value.startswith("/hwinfo/") else value)
        # Preserve old JSON readability without retaining the removed Chipset
        # sensor as an active backend requirement.
        self.sensors.chipset = None

        allowed_channels = tuple(DEFAULT_GROUP_CHANNELS.values())
        unused_channels = list(allowed_channels)
        for name in DEFAULT_GROUP_ORDER:
            g = self.groups[name]
            g.label = DEFAULT_GROUP_LABELS[name]
            if g.source not in VALID_SOURCES:
                g.source = SOURCE_MAX
            g.curve = sanitize_curve_points(g.curve)
            channel = g.channel if isinstance(g.channel, str) else ""
            if channel not in unused_channels:
                channel = unused_channels[0]
            g.channel = channel
            unused_channels.remove(channel)

        em = self.emergency
        if not isinstance(em.enabled, bool):
            em.enabled = True
        em.gpu_hotspot_threshold = _clamp(
            em.gpu_hotspot_threshold, 60, 120, 100)
        em.chipset_threshold = _clamp(em.chipset_threshold, 50, 120, 90)
        # An emergency override below 50% is not a safe cooling response.
        em.duty = _clamp(em.duty, 50, 100, 100)
        if not isinstance(self.mode_presets, dict):
            self.mode_presets = {}
        defaults = _default_mode_presets()
        for mode in MODES:
            groupset = self.mode_presets.get(mode)
            if not isinstance(groupset, dict):
                groupset = defaults[mode]
            fixed = {}
            for name in self.groups:
                fixed[name] = sanitize_curve_points(groupset.get(name, defaults[mode].get(name, _default_curve())))
            self.mode_presets[mode] = fixed


def _clamp(value: float, lo: float, hi: float, default=None) -> float:
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("not finite")
    except (TypeError, ValueError):
        if default is None:
            raise
        number = float(default)
    return max(lo, min(hi, number))


# ---------------------------------------------------------------------------
# Location + load/save
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "NeedZeroXtraTrash"
    return Path.home() / ".config" / "NeedZeroXtraTrash"


def config_path() -> Path:
    return config_dir() / "config.json"


def _broken_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return path.with_name(f"{path.name}.broken-{stamp}")


def load_with_warning(path: Optional[Path] = None) -> Tuple[Config, Optional[str], Optional[Path]]:
    path = path or config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise TypeError("configuration root must be a JSON object")
        return Config.from_dict(data), None, None
    except FileNotFoundError:
        return Config.defaults(), None, None
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        backup = _broken_path(path)
        try:
            os.replace(path, backup)
        except OSError as backup_exc:
            raise ConfigReadError(
                f"Invalid config could not be preserved before recovery: "
                f"{backup_exc} (parse error: {exc})"
            ) from backup_exc
        return Config.defaults(), f"Invalid config moved to {backup.name}: {exc}", backup
    except OSError as exc:
        raise ConfigReadError(f"Could not read existing config {path}: {exc}") from exc


def load(path: Optional[Path] = None) -> Config:
    return load_with_warning(path)[0]


def save(
    cfg: Config,
    path: Optional[Path] = None,
    *,
    prepare_temp: Optional[Callable[[Path], None]] = None,
) -> None:
    """Durably replace a config, optionally securing the staged file first.

    Machine configuration uses ``prepare_temp`` to apply and verify its exact
    owner/DACL before the atomic replace. This avoids the insecure interval
    created by protecting the destination only after publication. Ordinary
    per-user/test configurations keep the dependency-free default.
    """
    path = path or config_path()
    cfg.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as fh:
            tmp_path = Path(fh.name)
            json.dump(cfg.to_dict(), fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if prepare_temp is not None:
            prepare_temp(tmp_path)
        os.replace(tmp_path, path)  # atomic on the same filesystem
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def migrate_config(source: Path, target: Path) -> Optional[Path]:
    """Copy a valid legacy config to a machine path and retain a timestamped backup."""
    if target.exists() or not source.is_file():
        return None
    cfg, warning, _broken = load_with_warning(source)
    if warning:
        raise ConfigError(warning)
    backup = source.with_name(
        f"{source.name}.migrated-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}")
    shutil.copy2(source, backup)
    save(cfg, target)
    return backup


def clone(cfg: Config) -> Config:
    # asdict() already deep-copies, so round-tripping through it is safe.
    return Config.from_dict(cfg.to_dict())
