"""NVIDIA NVML sensor backend.

Uses ``nvml.dll`` from the installed NVIDIA driver directly through ctypes.
This gives a lightweight GPU temperature source without LibreHardwareMonitor.
NVML's public temperature API exposes the GPU core temperature on this path.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import byref, c_uint, c_void_p, create_string_buffer
from pathlib import Path
from typing import List, Optional

from .base import SensorBackend, SensorReading

NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0


class NvmlSensorBackend(SensorBackend):
    name = "NVML"

    def __init__(self) -> None:
        self.available = False
        self._dll = None

    def open(self) -> None:
        if os.name != "nt":
            raise OSError("NVML is only supported on Windows in this build")
        self._dll = _load_nvml()
        init = getattr(self._dll, "nvmlInit_v2", None) or self._dll.nvmlInit
        _check(init(), "nvmlInit")
        self.available = True

    def close(self) -> None:
        if self._dll is not None:
            try:
                self._dll.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass
        self._dll = None
        self.available = False

    def read_all(self) -> List[SensorReading]:
        if self._dll is None:
            return []
        count = c_uint()
        if _call(self._dll.nvmlDeviceGetCount_v2, byref(count)) != NVML_SUCCESS:
            return []

        out: List[SensorReading] = []
        get_handle = self._dll.nvmlDeviceGetHandleByIndex_v2
        get_name = self._dll.nvmlDeviceGetName
        get_temp = self._dll.nvmlDeviceGetTemperature

        for index in range(count.value):
            handle = c_void_p()
            if _call(get_handle, c_uint(index), byref(handle)) != NVML_SUCCESS:
                continue
            name = _device_name(get_name, handle) or f"NVIDIA GPU {index}"
            value: Optional[float] = None
            temp = c_uint()
            if _call(get_temp, handle, c_uint(NVML_TEMPERATURE_GPU), byref(temp)) == NVML_SUCCESS:
                value = float(temp.value)
            out.append(SensorReading(
                identifier=f"/nvml/gpu/{index}/temperature/0",
                hardware=name,
                hardware_type="GpuNvidia",
                name="GPU Core",
                value=value,
            ))
        return out


def _load_nvml():
    candidates = [
        "nvml.dll",
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "nvml.dll"),
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
            "NVIDIA Corporation" / "NVSMI" / "nvml.dll"),
    ]
    last = None
    for path in candidates:
        try:
            return ctypes.WinDLL(path)
        except OSError as exc:
            last = exc
    raise OSError(f"nvml.dll not found: {last}")


def _call(fn, *args) -> int:
    try:
        return int(fn(*args))
    except Exception:  # noqa: BLE001
        return -1


def _check(code: int, label: str) -> None:
    if int(code) != NVML_SUCCESS:
        raise OSError(f"{label} failed with NVML code {code}")


def _device_name(fn, handle) -> str:
    buf = create_string_buffer(96)
    if _call(fn, handle, buf, c_uint(len(buf))) != NVML_SUCCESS:
        return ""
    return buf.value.decode("utf-8", errors="replace").strip()
