"""Pure control math: curve interpolation and the emergency override.

No hardware or Windows dependencies live here so the decision logic is fully
unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config, GroupConfig, SOURCE_CPU, SOURCE_GPU


@dataclass
class Temps:
    """A snapshot of the temperatures used for control decisions.

    Any field may be ``None`` when the corresponding sensor is unavailable.
    """

    cpu: Optional[float] = None            # CPU (Tctl/Tdie)
    gpu: Optional[float] = None            # GPU Temperature
    gpu_hotspot: Optional[float] = None    # GPU Hot Spot
    chipset: Optional[float] = None        # legacy field; no longer used

    def control_value(self, source: str) -> Optional[float]:
        """The temperature a group should react to for the given source."""
        if source == SOURCE_CPU:
            return self.cpu
        if source == SOURCE_GPU:
            return self.gpu
        # SOURCE_MAX still uses the available value for curve interpolation.
        # ``source_fault_roles`` separately marks MAX unhealthy unless *both*
        # inputs are present, so a missing sensor can never lower the safety
        # floor merely because the other input happens to be cool.
        vals = [v for v in (self.cpu, self.gpu) if v is not None]
        return max(vals) if vals else None


@dataclass
class GroupDecision:
    """The computed outcome for one fan group in a single control cycle."""

    group: str
    channel: str
    source: str
    control_temp: Optional[float]
    curve_duty: float          # duty straight from the curve (before floor)
    target_duty: float         # final duty actually sent to the fan
    emergency: bool
    sensor_fault: bool = False
    sensor_fault_reason: Optional[str] = None


def source_fault_roles(temps: Temps, source: str) -> Tuple[str, ...]:
    """Return required control roles that are unavailable for ``source``.

    MAX is deliberately stricter than :meth:`Temps.control_value`: both CPU
    and GPU are required even though the surviving value can still be used to
    interpolate the curve before applying the 50% fault floor.
    """
    if source == SOURCE_CPU:
        return ("cpu",) if temps.cpu is None else ()
    if source == SOURCE_GPU:
        return ("gpu",) if temps.gpu is None else ()
    missing = []
    if temps.cpu is None:
        missing.append("cpu")
    if temps.gpu is None:
        missing.append("gpu")
    return tuple(missing)


def interpolate(curve: Sequence[Tuple[float, float]], temp: float) -> float:
    """Piecewise-linear interpolation of duty (%) for ``temp`` (deg C).

    Below the first point the first duty is used; above the last point the last
    duty is used (the curve is clamped, not extrapolated).
    """
    pts = sorted(curve, key=lambda p: p[0])
    if not pts:
        return 0.0
    if temp <= pts[0][0]:
        return float(pts[0][1])
    if temp >= pts[-1][0]:
        return float(pts[-1][1])
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return float(d1)
            frac = (temp - t0) / (t1 - t0)
            return float(d0 + frac * (d1 - d0))
    return float(pts[-1][1])


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def emergency_tripped(cfg: Config, temps: Temps) -> bool:
    em = cfg.emergency
    if not em.enabled:
        return False
    if temps.gpu_hotspot is not None and temps.gpu_hotspot >= em.gpu_hotspot_threshold:
        return True
    return False


def decide_group(cfg: Config, group: GroupConfig, group_name: str,
                 temps: Temps, emergency: bool) -> GroupDecision:
    """Compute the target duty for a single group."""
    control_temp = temps.control_value(group.source)
    missing_roles = source_fault_roles(temps, group.source)

    if control_temp is None:
        # No usable sensor -> fail safe to a moderate fixed duty.
        curve_duty = max(cfg.min_duty, 50.0)
    else:
        curve_duty = interpolate(group.sanitized_curve(), control_temp)

    target = max(curve_duty, cfg.min_duty)
    if missing_roles:
        target = max(target, 50.0)

    if emergency:
        target = max(target, cfg.emergency.duty)

    target = _clamp(round(target), 0, 100)
    return GroupDecision(
        group=group_name,
        channel=group.channel,
        source=group.source,
        control_temp=control_temp,
        curve_duty=round(curve_duty, 1),
        target_duty=target,
        emergency=emergency,
        sensor_fault=bool(missing_roles),
        sensor_fault_reason=(
            "required " + "/".join(role.upper() for role in missing_roles)
            + " sensor unavailable or stale"
            if missing_roles else None
        ),
    )


def decide_all(cfg: Config, temps: Temps,
               emergency: Optional[bool] = None) -> Dict[str, GroupDecision]:
    """Compute target duties for every configured group."""
    if emergency is None:
        emergency = emergency_tripped(cfg, temps)
    out: Dict[str, GroupDecision] = {}
    for name, group in cfg.groups.items():
        out[name] = decide_group(cfg, group, name, temps, emergency)
    return out
