"""Native Windows thermal-zone sensor backend.

This intentionally avoids LibreHardwareMonitor/pythonnet.  Windows exposes only
the thermal zones that firmware publishes through ACPI/WMI; many desktop boards
do not publish CPU temperatures here.  When nothing is exposed we return
no readings instead of guessing.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Callable, List, Optional

from .base import SensorBackend, SensorReading
from ..windows_security import sanitized_system_env, system_directory


class WindowsNativeSensorBackend(SensorBackend):
    name = "WindowsNative"

    def __init__(self, ttl: float = 8.0,
                 reader: Optional[Callable[[], List[SensorReading]]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = max(0.1, float(ttl))
        self._reader = reader or _read_acpi_thermal_zones
        self._clock = clock
        self._last_at = 0.0
        self._last: List[SensorReading] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._probe_pending = False
        self.available = False

    def open(self) -> None:
        if os.name != "nt" and self._reader is _read_acpi_thermal_zones:
            self.available = False
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker,
                name="windows-thermal-probe",
                daemon=True,
            )
            self._thread.start()
        self._request_probe(force=True)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._lock:
            self._thread = None
            self._probe_pending = False
            self._last = []
            self._last_at = 0.0
            self.available = False

    def read_all(self) -> List[SensorReading]:
        if self._thread is None:
            self.open()
        self._request_probe()
        with self._lock:
            return list(self._last)

    def _request_probe(self, force: bool = False) -> None:
        with self._lock:
            if self._thread is None or self._stop.is_set() or self._probe_pending:
                return
            now = self._clock()
            if not force and now - self._last_at < self._ttl:
                return
            self._probe_pending = True
            self._wake.set()

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                readings = list(self._reader() or [])
            except Exception:  # noqa: BLE001 - unavailable probe becomes empty cache
                readings = []
            with self._lock:
                self._last = readings
                self._last_at = self._clock()
                self.available = bool(readings)
                self._probe_pending = False


def _read_acpi_thermal_zones() -> List[SensorReading]:
    if os.name != "nt":
        return []
    system32 = system_directory()
    powershell = system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        return []
    cmd = [
        str(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        (
            "$rows=@();"
            "Get-CimInstance -Namespace root\\wmi -ClassName MSAcpi_ThermalZoneTemperature "
            "-ErrorAction SilentlyContinue | ForEach-Object {"
            "$c=[math]::Round(($_.CurrentTemperature/10)-273.15,1);"
            "$rows += [pscustomobject]@{Name=$_.InstanceName; Celsius=$c}"
            "};"
            "$rows | ConvertTo-Json -Compress"
        ),
    ]
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=4,
            startupinfo=startup,
            cwd=str(system32),
            env=sanitized_system_env(),
            creationflags=0x08000000,
        )
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    readings: List[SensorReading] = []
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or f"ACPI Thermal Zone {index}")
        try:
            value = float(row.get("Celsius"))
        except (TypeError, ValueError):
            continue
        if not 0 < value < 130:
            continue
        low = name.lower()
        is_cpu = any(key in low for key in ("cpu", "processor", "x86", "package", "tctl", "tdie"))
        readings.append(SensorReading(
            identifier=f"/windows/acpi/thermal/{index}",
            hardware="Windows ACPI",
            hardware_type="Cpu" if is_cpu else "Motherboard",
            name=("CPU ACPI Thermal Zone" if is_cpu else "ACPI Thermal Zone") + f" ({name})",
            value=round(value, 1),
        ))
    return readings
