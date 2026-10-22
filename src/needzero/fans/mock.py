"""Simulated fan backend: derives a plausible RPM from the commanded duty."""

from __future__ import annotations

import random
from typing import Dict

from .base import FanBackend, FanStatus


class MockFanBackend(FanBackend):
    name = "mock"
    available = True
    description = "Simulated NZXT RGB & Fan Controller"

    def __init__(self) -> None:
        self._duty: Dict[str, float] = {ch: 30.0 for ch in self.channels}

    def set_duty(self, channel: str, duty: float) -> bool:
        self._duty[channel] = max(0.0, min(100.0, float(duty)))
        return True

    def read_status(self) -> Dict[str, FanStatus]:
        out: Dict[str, FanStatus] = {}
        for ch in self.channels:
            duty = self._duty.get(ch, 0.0)
            # ~1850 rpm at 100 %, a little jitter, and no spin below ~15 %.
            rpm = 0 if duty < 15 else int(duty * 18 + random.uniform(-25, 25))
            out[ch] = FanStatus(channel=ch, rpm=max(0, rpm), duty=round(duty, 0),
                                mode="PWM")
        return out
