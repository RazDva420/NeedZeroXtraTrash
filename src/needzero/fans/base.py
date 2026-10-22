"""Fan backend interface."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional


_LOGGER = logging.getLogger(__name__)


@dataclass
class FanStatus:
    channel: str                    # fan1 / fan2 / fan3
    rpm: Optional[int] = None
    duty: Optional[float] = None    # % actually reported by the device
    mode: Optional[str] = None      # "PWM" / "DC" / None


class FanBackend:
    name = "base"
    available = False
    description = ""
    channels = ("fan1", "fan2", "fan3")

    def open(self) -> None:  # pragma: no cover
        pass

    def close(self) -> None:  # pragma: no cover
        pass

    def set_duty(self, channel: str, duty: float) -> bool:
        """Write one channel and report transport-level call acceptance.

        ``True`` is not readback confirmation.  Callers must keep commanded and
        reported duty separate and verify with :meth:`read_status` when the
        device exposes duty telemetry.
        """
        raise NotImplementedError

    def set_many(self, duties: Dict[str, float]) -> Dict[str, bool]:
        """Attempt every write and report success per channel."""
        results: Dict[str, bool] = {}
        for ch, duty in duties.items():
            try:
                results[ch] = bool(self.set_duty(ch, duty))
            except Exception as exc:  # noqa: BLE001 - one bad channel must not block the rest
                _LOGGER.warning("fan write %s failed: %s", ch, exc)
                results[ch] = False
        return results

    def read_status(self) -> Dict[str, FanStatus]:
        raise NotImplementedError

    def force_reconnect(self) -> bool:
        """Make one immediate reconnect attempt, bypassing normal backoff."""
        return False
