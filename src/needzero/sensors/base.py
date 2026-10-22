"""Sensor backend interface + shared auto-detection logic."""

from __future__ import annotations

import re
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..curves import Temps
from ..config import SensorMap

# Logical roles the app needs from the pile of raw sensors.
ROLES = ("cpu", "gpu", "gpu_hotspot")
MAX_SENSOR_AGE = 10.0


@dataclass
class SensorReading:
    identifier: str          # stable id used for manual mapping + persistence
    hardware: str            # e.g. "AMD Ryzen 7 5800X"
    hardware_type: str       # e.g. "Cpu", "GpuNvidia", "Motherboard"
    name: str                # e.g. "Core (Tctl/Tdie)", "GPU Hot Spot"
    value: Optional[float]   # current value, deg C (None if not readable)
    unit: str = "°C"
    sampled_at: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict:
        return {
            "identifier": self.identifier,
            "hardware": self.hardware,
            "hardware_type": self.hardware_type,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "age_seconds": max(0.0, round(time.monotonic() - self.sampled_at, 1)),
        }


class SensorBackend:
    """Base class. Concrete backends implement :meth:`read_all`."""

    name = "base"
    available = False

    def open(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def read_all(self) -> List[SensorReading]:
        """Return a fresh reading for every temperature sensor."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

_GPU_TYPES = {"GpuNvidia", "GpuAmd", "GpuIntel", "GpuAti"}

# Keyword patterns (case-insensitive) used to recognise each role by name.
_CPU_PATTERNS = [r"tctl", r"tdie", r"cpu package", r"core \(tctl", r"package",
                 r"core max", r"core average", r"cpu"]
_HOTSPOT_PATTERNS = [r"hot ?spot", r"\bjunction(?: temperature)?$"]
_NOT_HOTSPOT_PATTERNS = [r"memory", r"vram", r"vrm"]
_GPU_CORE_PATTERNS = [r"gpu core", r"gpu temperature", r"^gpu$", r"core"]
_NOT_GPU_CORE_PATTERNS = [r"hot ?spot", r"junction", r"memory", r"vram", r"vrm"]


def _match(name: str, patterns: List[str]) -> int:
    """Return the (1-based) index of the first matching pattern, else 0.

    Lower is a stronger/earlier match, so we can prefer more specific names.
    """
    low = name.lower()
    for i, pat in enumerate(patterns, start=1):
        if re.search(pat, low):
            return i
    return 0


def auto_select(readings: List[SensorReading]) -> Dict[str, Optional[str]]:
    """Pick a sensor identifier for each role from the raw readings."""
    now = time.monotonic()
    valid = [r for r in readings if _valid_temp(r.value, r.sampled_at, now)]
    cpu = [r for r in valid if r.hardware_type == "Cpu"]
    gpu = [r for r in valid if r.hardware_type in _GPU_TYPES]
    picked: Dict[str, Optional[str]] = {role: None for role in ROLES}

    picked["cpu"] = _best(cpu, _CPU_PATTERNS)
    # GPU hot spot must be found before GPU core so "core" doesn't grab it.
    picked["gpu_hotspot"] = _best(
        [r for r in gpu if not _match(r.name, _NOT_HOTSPOT_PATTERNS)],
        _HOTSPOT_PATTERNS,
    )
    hotspot = next((r for r in gpu if r.identifier == picked["gpu_hotspot"]), None)
    core_pool = gpu
    if hotspot is not None:
        same_gpu = [r for r in gpu if r.hardware == hotspot.hardware]
        if _best([r for r in same_gpu if r.identifier != picked["gpu_hotspot"]
                  and not _match(r.name, _NOT_GPU_CORE_PATTERNS)], _GPU_CORE_PATTERNS):
            core_pool = same_gpu
    picked["gpu"] = _best(
        [r for r in core_pool
         if r.identifier != picked["gpu_hotspot"] and not _match(r.name, _NOT_GPU_CORE_PATTERNS)],
        _GPU_CORE_PATTERNS,
    )
    return picked


def _best(readings: List[SensorReading], patterns: List[str]) -> Optional[str]:
    best_id = None
    best_rank = None
    for r in readings:
        rank = _match(r.name, patterns)
        if rank == 0:
            continue
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_id = r.identifier
    return best_id


def _valid_temp(value: Optional[float], sampled_at: Optional[float] = None,
                now: Optional[float] = None) -> bool:
    if value is None:
        return False
    try:
        v = float(value)
        stamp = float(sampled_at) if sampled_at is not None else None
    except (TypeError, ValueError):
        return False
    if not math.isfinite(v) or (stamp is not None and not math.isfinite(stamp)):
        return False
    if stamp is not None:
        current = time.monotonic() if now is None else now
        if stamp > current + 1.0 or current - stamp > MAX_SENSOR_AGE:
            return False
    return 0 < v < 130


def resolve_temps(readings: List[SensorReading],
                  sensor_map: Optional[SensorMap] = None) -> Temps:
    """Turn raw readings into a :class:`Temps` using manual + auto mapping."""
    by_id = {r.identifier: r for r in readings if isinstance(r.identifier, str)}
    auto = auto_select(readings)
    now = time.monotonic()

    def value_for(role: str, use_override: bool = True) -> Optional[float]:
        override = getattr(sensor_map, role, None) if sensor_map else None
        candidates = []
        if use_override and override is not None and override != "":
            # A manual mapping is an explicit choice.  If the identifier is no
            # longer present (for example because HWiNFO has stopped), silently
            # switching to a different sensor defeats that choice and can hide
            # a sensor fault from the control loop.
            if not isinstance(override, str) or override not in by_id:
                return None
            manual = by_id[override]
            if not _valid_temp(manual.value, manual.sampled_at, now):
                return None
            # Memory Junction/VRAM are not GPU core or GPU Hot Spot readings.
            # Reject those two unsafe role assignments, but retain the historic
            # auto-correction path for other manually selected readings.
            if role == "gpu_hotspot" and _match(manual.name, _NOT_HOTSPOT_PATTERNS):
                override = None
            elif role == "gpu" and _match(manual.name, _NOT_GPU_CORE_PATTERNS):
                override = None
            else:
                candidates.append(override)
        auto_ident = auto.get(role)
        if auto_ident and auto_ident not in candidates:
            candidates.append(auto_ident)
        for ident in candidates:
            if ident in by_id:
                reading = by_id[ident]
                value = reading.value
                if _valid_temp(value, reading.sampled_at, now):
                    return value
        return None

    gpu = value_for("gpu")
    hotspot = value_for("gpu_hotspot")
    if gpu is not None and hotspot is not None and hotspot < gpu:
        auto_gpu = value_for("gpu", use_override=False)
        auto_hotspot = value_for("gpu_hotspot", use_override=False)
        if auto_gpu is not None and auto_hotspot is not None and auto_hotspot >= auto_gpu:
            gpu, hotspot = auto_gpu, auto_hotspot
        else:
            hotspot = gpu

    return Temps(
        cpu=value_for("cpu"),
        gpu=gpu,
        gpu_hotspot=hotspot,
        chipset=None,
    )
