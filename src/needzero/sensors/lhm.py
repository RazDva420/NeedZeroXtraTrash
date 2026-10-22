"""LibreHardwareMonitor sensor backend (self-contained, no external program).

Loads the allowlisted ``LibreHardwareMonitorLib.dll`` payload in-process via
pythonnet.  In a packaged installation this happens only inside the protected
SYSTEM boot controller; the unelevated UI never loads hardware DLLs or drivers.
Elevated source runs deliberately refuse project/cwd DLL paths, so hardware
validation must use a verified packaged build rather than trusting a mutable
development directory across the privilege boundary.

The DLLs (``LibreHardwareMonitorLib.dll`` + ``HidSharp.dll`` + ``PawnIOLib.dll``) are fetched into
``lib/`` by ``fetch_deps.py`` and bundled by the PyInstaller build.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

from .base import SensorBackend, SensorReading
from ..windows_security import is_elevated

_LOGGER = logging.getLogger(__name__)
_DLL = "LibreHardwareMonitorLib.dll"


def _candidate_dirs(dev_override: Optional[Path] = None) -> List[Path]:
    """Return trusted LHM locations for the current execution mode.

    A frozen build loads only its PyInstaller payload.  It must never search an
    environment-provided path, cwd, or files beside the elevated executable.
    Source development may opt into one explicit directory, but only from a
    non-elevated process.
    """
    if getattr(sys, "frozen", False):
        mp = getattr(sys, "_MEIPASS", None)
        return [Path(mp) / "lib"] if mp else []

    dirs: List[Path] = []
    if sys.platform == "win32" and is_elevated():
        if dev_override is not None:
            raise PermissionError("LHM development override is disabled when elevated")
        return []
    if dev_override is not None:
        override = Path(dev_override).expanduser()
        if not override.is_absolute():
            raise ValueError("LHM development override must be an absolute directory")
        dirs.append(override.resolve())
    here = Path(__file__).resolve()
    dirs.append(here.parents[3] / "lib")   # project root/lib
    return dirs


def _find_dll(dev_override: Optional[Path] = None) -> Optional[Path]:
    for d in _candidate_dirs(dev_override):
        cand = d / _DLL
        if cand.is_file():
            return cand
    return None


class LhmSensorBackend(SensorBackend):
    name = "LibreHardwareMonitor"

    def __init__(self, dev_dll_dir: Optional[Path] = None) -> None:
        self.available = False
        self._computer = None
        self._temp_type = None
        self._dev_dll_dir = dev_dll_dir

    def open(self) -> None:
        dll = _find_dll(self._dev_dll_dir)
        if not dll:
            raise FileNotFoundError(
                f"{_DLL} not found. Run 'python fetch_deps.py' to download it "
                "into the lib/ folder.")

        import clr  # pythonnet

        for dep in (
            "System.Runtime.CompilerServices.Unsafe.dll",
            "System.Buffers.dll",
            "System.Numerics.Vectors.dll",
            "System.Memory.dll",
            "System.Reactive.dll",
            "Serilog.dll",
            "Serilog.Extensions.Logging.dll",
            "PawnIOLib.dll",
            "CapFrameX.Monitoring.Contracts.dll",
            "CapFrameX.Extensions.dll",
            "HidSharp.dll",
        ):
            dep_path = dll.parent / dep
            if dep_path.is_file():
                try:
                    clr.AddReference(str(dep_path))
                except Exception:  # noqa: BLE001
                    pass
        clr.AddReference(str(dll))
        from LibreHardwareMonitor.Hardware import Computer, SensorType  # type: ignore

        self._temp_type = SensorType.Temperature
        computer = Computer()
        computer.IsCpuEnabled = True
        computer.IsGpuEnabled = True
        computer.IsMotherboardEnabled = True
        computer.IsControllerEnabled = True
        computer.IsMemoryEnabled = False
        computer.IsStorageEnabled = False
        computer.IsNetworkEnabled = False
        computer.Open()
        self._computer = computer
        self.available = True
        _LOGGER.info("LibreHardwareMonitor loaded from %s", dll)

    def close(self) -> None:
        if self._computer is not None:
            try:
                self._computer.Close()
            except Exception:  # noqa: BLE001
                pass
        self._computer = None
        self.available = False

    @staticmethod
    def _update(hw) -> bool:
        try:
            hw.Update()
            return True
        except Exception:  # noqa: BLE001
            return False

    def read_all(self) -> List[SensorReading]:
        if not self._computer:
            return []
        out: List[SensorReading] = []

        def collect(hw) -> None:
            if not self._update(hw):
                return
            sampled_at = time.monotonic()
            htype = str(hw.HardwareType).split(".")[-1]
            hname = str(hw.Name)
            for s in hw.Sensors:
                if s.SensorType != self._temp_type:
                    continue
                value = None
                try:
                    if s.Value is not None:
                        value = round(float(s.Value), 1)
                except Exception:  # noqa: BLE001
                    value = None
                if value is not None and value <= 0:
                    # Ryzen SMU/MSR conflicts often surface as exactly 0.0 C.
                    # Treat that as unreadable so the UI never presents it as a real temperature.
                    value = None
                out.append(SensorReading(
                    identifier=str(s.Identifier),
                    hardware=hname,
                    hardware_type=htype,
                    name=str(s.Name),
                    value=value,
                    sampled_at=sampled_at,
                ))
            for sub in hw.SubHardware:
                collect(sub)

        for hw in self._computer.Hardware:
            collect(hw)
        return out
