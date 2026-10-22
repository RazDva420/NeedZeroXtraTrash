"""Simulated sensor backend.

Produces smoothly-varying fake temperatures so the whole app (UI, control loop,
fan animation) can run and be demoed without any hardware or the LHM DLL.
"""

from __future__ import annotations

import math
import time
from typing import List

from .base import SensorBackend, SensorReading


class MockSensorBackend(SensorBackend):
    name = "mock"
    available = True

    def __init__(self) -> None:
        self._t0 = time.time()

    def _wave(self, base: float, amp: float, period: float, phase: float) -> float:
        t = time.time() - self._t0
        return base + amp * math.sin(2 * math.pi * (t / period) + phase)

    def read_all(self) -> List[SensorReading]:
        cpu = self._wave(58, 12, 40, 0.0)
        gpu = self._wave(52, 14, 55, 1.0)
        hotspot = gpu + 14 + self._wave(0, 2, 13, 2.0)
        return [
            SensorReading("/mock/cpu/temperature/0", "Mock CPU", "Cpu",
                          "Core (Tctl/Tdie)", round(cpu, 1)),
            SensorReading("/mock/cpu/temperature/1", "Mock CPU", "Cpu",
                          "CPU Package", round(cpu - 1, 1)),
            SensorReading("/mock/gpu/temperature/0", "Mock GPU", "GpuNvidia",
                          "GPU Core", round(gpu, 1)),
            SensorReading("/mock/gpu/temperature/1", "Mock GPU", "GpuNvidia",
                          "GPU Hot Spot", round(hotspot, 1)),
            SensorReading("/mock/gpu/temperature/2", "Mock GPU", "GpuNvidia",
                          "GPU Memory Junction", round(gpu + 8, 1)),
        ]
