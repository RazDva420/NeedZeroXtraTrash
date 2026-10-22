"""Narrow controller API shared by IPC and deterministic tests."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, List, Optional

from . import config as cfgmod
from .config import Config, ConfigError, MODES, VALID_SOURCES, flat_curve, load_mode_preset, save_active_mode_preset
from .config_store import ConfigStore
from .controlloop import Controller


_GROUPS = frozenset(cfgmod.DEFAULT_GROUP_ORDER)
_CHANNELS = frozenset(cfgmod.DEFAULT_GROUP_CHANNELS.values())
_SENSOR_ROLES = frozenset(("cpu", "gpu", "gpu_hotspot"))


def _finite_number(data: dict, key: str, low: float, high: float) -> float:
    value = data.get(key)
    if isinstance(value, bool):
        raise ConfigError("Settings validation failed", {key: "must be a number"})
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("Settings validation failed", {key: "must be a number"}) from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ConfigError(
            "Settings validation failed",
            {key: f"must be between {low:g} and {high:g}"},
        )
    return number


def _strict_curve(points) -> List[List[float]]:
    if not isinstance(points, list) or not 2 <= len(points) <= 32:
        raise ConfigError("Curve validation failed", {"curve": "requires 2 to 32 points"})
    clean = []
    for index, point in enumerate(points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ConfigError("Curve validation failed", {"curve": f"point {index} is invalid"})
        try:
            temperature = float(point[0])
            duty = float(point[1])
        except (TypeError, ValueError) as exc:
            raise ConfigError("Curve validation failed", {"curve": f"point {index} is not numeric"}) from exc
        if not math.isfinite(temperature) or not math.isfinite(duty):
            raise ConfigError("Curve validation failed", {"curve": f"point {index} is not finite"})
        if not 0 <= temperature <= 120 or not 0 <= duty <= 100:
            raise ConfigError("Curve validation failed", {"curve": f"point {index} is out of range"})
        clean.append([temperature, duty])
    normalized = cfgmod.sanitize_curve_points(clean)
    if len(normalized) < 2:
        raise ConfigError("Curve validation failed", {"curve": "requires distinct temperatures"})
    return normalized


class Api:
    def __init__(
        self,
        controller: Controller,
        *,
        store: Optional[ConfigStore] = None,
        config_path: Optional[Path] = None,
        shutdown_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._ctrl = controller
        self._shutdown_fn = shutdown_fn
        self._store = store or ConfigStore(
            config_path or cfgmod.config_path(),
            controller.get_config(),
            publish_fn=controller.set_config,
        )

    def _mutate(self, fn) -> dict:
        config, _revision = self._store.transaction(fn)
        return config.to_dict()

    def get_state(self) -> dict:
        return self._ctrl.snapshot()

    def get_config(self) -> dict:
        config, revision, warning = self._store.get()
        result = config.to_dict()
        result["_status"] = {"revision": revision, "warning": warning}
        return result

    def list_sensors(self) -> List[dict]:
        return self._ctrl.list_sensors()

    def set_curve(self, group: str, points) -> dict:
        if group not in _GROUPS:
            raise ConfigError("Curve validation failed", {"group": "unknown group"})
        clean = _strict_curve(points)

        def apply(config: Config) -> None:
            config.groups[group].curve = clean

        return self._mutate(apply)

    def set_source(self, group: str, source: str) -> dict:
        fields = {}
        if group not in _GROUPS:
            fields["group"] = "unknown group"
        if source not in VALID_SOURCES:
            fields["source"] = "unsupported source"
        if fields:
            raise ConfigError("Source validation failed", fields)
        return self._mutate(lambda config: setattr(config.groups[group], "source", source))

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise ConfigError("Mode validation failed", {"mode": "unsupported mode"})
        return self._mutate(lambda config: load_mode_preset(config, mode))

    def apply_settings(self, data: dict) -> dict:
        """Validate and persist the entire Settings form as one transaction."""
        if not isinstance(data, dict):
            raise ConfigError("Settings validation failed", {"settings": "must be an object"})
        allowed = {"channels", "min_duty", "poll_interval", "emergency", "sensors"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigError("Settings validation failed", {unknown[0]: "unknown setting"})

        channels = data.get("channels")
        if not isinstance(channels, dict) or set(channels) != _GROUPS:
            raise ConfigError("Settings validation failed", {"channels": "all groups are required"})
        values = list(channels.values())
        if set(values) != _CHANNELS or len(set(values)) != len(values):
            raise ConfigError("Settings validation failed", {"channels": "fan1, fan2 and fan3 must be unique"})

        minimum = _finite_number(data, "min_duty", 0, 100)
        poll = _finite_number(data, "poll_interval", 0.5, 30)

        emergency = data.get("emergency")
        required_emergency = {"enabled", "gpu_hotspot_threshold", "duty"}
        if not isinstance(emergency, dict) or set(emergency) != required_emergency:
            raise ConfigError("Settings validation failed", {"emergency": "all emergency fields are required"})
        if not isinstance(emergency["enabled"], bool):
            raise ConfigError("Settings validation failed", {"emergency.enabled": "must be boolean"})
        hotspot = _finite_number(emergency, "gpu_hotspot_threshold", 60, 120)
        emergency_duty = _finite_number(emergency, "duty", 50, 100)

        sensors = data.get("sensors")
        if not isinstance(sensors, dict) or set(sensors) != _SENSOR_ROLES:
            raise ConfigError("Settings validation failed", {"sensors": "all sensor roles are required"})
        sensor_values = {}
        for role, identifier in sensors.items():
            if identifier is None or identifier == "":
                sensor_values[role] = None
            elif isinstance(identifier, str) and identifier.strip().startswith("/hwinfo/"):
                raise ConfigError("Settings validation failed", {f"sensors.{role}": "HWiNFO is no longer supported"})
            elif (isinstance(identifier, str) and len(identifier) <= 512
                  and identifier.strip() and not any(ord(ch) < 32 for ch in identifier)):
                sensor_values[role] = identifier.strip()
            else:
                raise ConfigError("Settings validation failed", {f"sensors.{role}": "invalid sensor identifier"})

        def apply(config: Config) -> None:
            for group, channel in channels.items():
                config.groups[group].channel = channel
            config.min_duty = minimum
            config.poll_interval = poll
            config.emergency.enabled = emergency["enabled"]
            config.emergency.gpu_hotspot_threshold = hotspot
            config.emergency.duty = emergency_duty
            for role, identifier in sensor_values.items():
                setattr(config.sensors, role, identifier)
        return self._mutate(apply)

    def reset_defaults(self) -> dict:
        def apply(config: Config) -> None:
            curve = flat_curve(40)
            for group in config.groups.values():
                group.curve = [list(point) for point in curve]
            save_active_mode_preset(config)

        return self._mutate(apply)

    def apply_now(self) -> bool:
        self._mutate(save_active_mode_preset)
        return True

    def test_full(self, seconds: float = 10.0) -> bool:
        try:
            duration = float(seconds)
        except (TypeError, ValueError) as exc:
            raise ConfigError("Fan test validation failed", {"seconds": "must be numeric"}) from exc
        if not 1 <= duration <= 10:
            raise ConfigError("Fan test validation failed", {"seconds": "must be between 1 and 10"})
        self._ctrl.test_full(duration)
        return True

    def get_autostart(self) -> bool:
        """Return the SYSTEM-validated installation state to the desktop UI."""
        from .autostart import installed_status

        return bool(installed_status())

    def shutdown_controller(self) -> bool:
        """Perform the controller's confirmed A3 stop before acknowledging.

        Desktop clients do not expose this method.  The controller service
        injects the callback, while an in-process/test API fails closed.
        """
        if self._shutdown_fn is None:
            return False
        return bool(self._shutdown_fn())
