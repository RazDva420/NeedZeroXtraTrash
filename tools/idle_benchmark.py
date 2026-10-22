"""Measure the idle cost of the headless controller for sixty seconds.

This utility deliberately uses the mock sensor and fan backends.  It never
imports the desktop application, tray code or WebView, and it does not touch
physical cooling hardware.  Windows process counters are read directly via
``ctypes`` so running the benchmark does not add a monitoring dependency to
the application or its executable.

Run from the repository root::

    python tools/idle_benchmark.py --output qa/headless-idle.json

The default measurement window is 60 seconds.  A non-zero exit status means
the resource budget was missed, WebView was imported, or clean shutdown could
not be confirmed.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import mmap
import os
import platform
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

DEFAULT_DURATION_SECONDS = 60.0
DEFAULT_WARMUP_SECONDS = 2.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
CPU_LIMIT_PERCENT_ONE_CORE = 1.0
PRIVATE_WORKING_SET_LIMIT_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class BasicResourceSample:
    """Cheap counters suitable for periodic sampling during the run."""

    cpu_seconds: float
    working_set_bytes: int
    private_commit_bytes: int


@dataclass(frozen=True)
class ProcessShape:
    handle_count: int
    thread_count: int


class ResourceReader(Protocol):
    def sample_basic(self) -> BasicResourceSample:
        """Return cumulative CPU time and current process memory counters."""

    def private_working_set_bytes(self, working_set_hint: int = 0) -> int:
        """Return resident bytes that cannot be shared with another process."""


if os.name == "nt":
    from ctypes import wintypes

    class _ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]


class WindowsProcessResources:
    """Current-process CPU and memory counters backed only by Win32 APIs.

    ``GetProcessMemoryInfo`` supplies total working set and private commit.
    ``QueryWorkingSet`` supplies one entry per resident page; entries whose
    ``Shared`` bit is clear are summed to obtain private working set.  This is
    the physical-memory metric used for the 100 MiB acceptance budget.
    """

    _ERROR_BAD_LENGTH = 24
    _ERROR_INSUFFICIENT_BUFFER = 122
    _WORKING_SET_SHARED_BIT = 1 << 8
    _TH32CS_SNAPTHREAD = 0x00000004

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("The headless resource benchmark requires Windows")

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)

        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._handle = self._kernel32.GetCurrentProcess()
        self._kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        self._kernel32.CreateToolhelp32Snapshot.argtypes = [
            wintypes.DWORD, wintypes.DWORD]
        self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel32.Thread32First.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        self._kernel32.Thread32First.restype = wintypes.BOOL
        self._kernel32.Thread32Next.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        self._kernel32.Thread32Next.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self._kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        self._kernel32.GetProcessTimes.restype = wintypes.BOOL

        self._psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCountersEx),
            wintypes.DWORD,
        ]
        self._psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        query_working_set = getattr(self._kernel32, "K32QueryWorkingSet", None)
        if query_working_set is None:
            query_working_set = self._psapi.QueryWorkingSet
        query_working_set.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        query_working_set.restype = wintypes.BOOL
        self._query_working_set = query_working_set

    @staticmethod
    def _filetime_seconds(value: Any) -> float:
        ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
        return ticks / 10_000_000.0

    def _cpu_seconds(self) -> float:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not self._kernel32.GetProcessTimes(
                self._handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        return self._filetime_seconds(kernel) + self._filetime_seconds(user)

    def _memory(self) -> Any:
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not self._psapi.GetProcessMemoryInfo(
                self._handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters

    def sample_basic(self) -> BasicResourceSample:
        counters = self._memory()
        return BasicResourceSample(
            cpu_seconds=self._cpu_seconds(),
            working_set_bytes=int(counters.WorkingSetSize),
            private_commit_bytes=int(counters.PrivateUsage),
        )

    def private_working_set_bytes(self, working_set_hint: int = 0) -> int:
        """Count non-shareable resident pages in the current working set.

        QueryWorkingSet returns a pointer-sized flag word per resident page.
        The buffer is deliberately oversized from the inexpensive working-set
        counter and retried if the set grows between the two calls.
        """
        if working_set_hint <= 0:
            working_set_hint = int(self._memory().WorkingSetSize)
        page_size = int(mmap.PAGESIZE)
        capacity = max(4096, (working_set_hint // page_size) + 4096)
        word_size = ctypes.sizeof(ctypes.c_size_t)

        for _ in range(8):
            byte_count = (capacity + 1) * word_size
            if byte_count > 0xFFFFFFFF:
                raise OverflowError("working-set query buffer exceeds Win32 limit")
            buffer = ctypes.create_string_buffer(byte_count)
            ctypes.set_last_error(0)
            ok = self._query_working_set(
                self._handle, ctypes.byref(buffer), byte_count)
            entry_count = int(ctypes.c_size_t.from_buffer(buffer).value)
            if ok and entry_count <= capacity:
                entries = (ctypes.c_size_t * entry_count).from_buffer(
                    buffer, word_size)
                private_pages = sum(
                    1 for flags in entries
                    if not (int(flags) & self._WORKING_SET_SHARED_BIT)
                )
                return private_pages * page_size

            error = ctypes.get_last_error()
            if error not in (
                    self._ERROR_BAD_LENGTH, self._ERROR_INSUFFICIENT_BUFFER):
                raise ctypes.WinError(error)
            capacity = max(capacity * 2, entry_count + 4096)

        raise OSError("working set changed too quickly to sample")

    def process_shape(self) -> ProcessShape:
        handles = wintypes.DWORD()
        if not self._kernel32.GetProcessHandleCount(
                self._handle, ctypes.byref(handles)):
            raise ctypes.WinError(ctypes.get_last_error())

        snapshot = self._kernel32.CreateToolhelp32Snapshot(
            self._TH32CS_SNAPTHREAD, 0)
        invalid = ctypes.c_void_p(-1).value
        if not snapshot or int(snapshot) == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        threads = 0
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            ok = self._kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while ok:
                if int(entry.th32OwnerProcessID) == os.getpid():
                    threads += 1
                entry.dwSize = ctypes.sizeof(entry)
                ok = self._kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        return ProcessShape(int(handles.value), threads)


def cpu_percent_one_core(
        start_cpu_seconds: float,
        end_cpu_seconds: float,
        elapsed_seconds: float) -> float:
    """Return utilisation as a percentage of exactly one logical core."""
    if elapsed_seconds <= 0:
        raise ValueError("elapsed_seconds must be positive")
    used = max(0.0, float(end_cpu_seconds) - float(start_cpu_seconds))
    return (used / float(elapsed_seconds)) * 100.0


def evaluate_budget(
        cpu_percent: float,
        private_working_set_bytes: int,
        *,
        duration_seconds: float = DEFAULT_DURATION_SECONDS,
        cpu_limit_percent: float = CPU_LIMIT_PERCENT_ONE_CORE,
        private_working_set_limit_bytes: int = PRIVATE_WORKING_SET_LIMIT_BYTES,
        no_webview_imported: bool = True,
        shutdown_confirmed: bool = True) -> dict[str, Any]:
    """Evaluate strict acceptance operators and return serialisable checks."""
    checks = {
        "measurement_duration": {
            "actual_seconds": round(float(duration_seconds), 6),
            "operator": ">=",
            "required_seconds": DEFAULT_DURATION_SECONDS,
            "passed": float(duration_seconds) >= DEFAULT_DURATION_SECONDS,
        },
        "cpu_one_core": {
            "actual_percent": round(float(cpu_percent), 6),
            "operator": "<",
            "limit_percent": float(cpu_limit_percent),
            "passed": float(cpu_percent) < float(cpu_limit_percent),
        },
        "private_working_set": {
            "actual_bytes": int(private_working_set_bytes),
            "operator": "<=",
            "limit_bytes": int(private_working_set_limit_bytes),
            "passed": int(private_working_set_bytes)
            <= int(private_working_set_limit_bytes),
        },
        "webview_not_imported": {"passed": bool(no_webview_imported)},
        "safe_shutdown_confirmed": {"passed": bool(shutdown_confirmed)},
    }
    checks["passed"] = all(
        check["passed"] for check in checks.values()
        if isinstance(check, dict) and "passed" in check)
    return checks


def _webview_loaded() -> bool:
    return any(
        name == "webview" or name.startswith("webview.")
        for name in sys.modules
    )


def run_benchmark(
        *,
        duration_seconds: float = DEFAULT_DURATION_SECONDS,
        warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
        sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        resources: Optional[ResourceReader] = None) -> dict[str, Any]:
    """Run one headless benchmark and return the complete JSON document."""
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if warmup_seconds < 0:
        raise ValueError("warmup_seconds cannot be negative")
    if sample_interval_seconds <= 0:
        raise ValueError("sample_interval_seconds must be positive")

    from needzero import __version__
    from needzero import config as cfgmod
    from needzero.config import Config
    from needzero.controlloop import Controller
    from needzero.controller_service import ControllerService
    from needzero.fans.mock import MockFanBackend
    from needzero.ipc import IpcServer
    from needzero.sensors.mock import MockSensorBackend

    reader = resources or WindowsProcessResources()
    cfg = Config.defaults()
    cfg.poll_interval = 2.0
    cfg.sensor_backend = "mock"
    cfg.fan_backend = "mock"
    webview_before = _webview_loaded()
    shutdown_confirmed = False
    service_exit_status = None
    controller_holder = []
    service_result = []
    stop_event = threading.Event()

    with tempfile.TemporaryDirectory(prefix="needzero-idle-") as temp_dir:
        temp_root = Path(temp_dir)
        config_path = temp_root / "config.json"
        token_path = temp_root / "ipc.token"
        cfgmod.save(cfg, config_path)
        token_path.write_text((b"b" * 32).hex(), encoding="ascii")

        def make_controller(config, sensors, fans):
            controller = Controller(config, sensors, fans)
            controller_holder.append(controller)
            return controller

        service = ControllerService(
            config_path=config_path,
            token_path=token_path,
            sensor_factory=lambda *_args, **_kwargs: MockSensorBackend(),
            fan_factory=lambda *_args, **_kwargs: MockFanBackend(),
            controller_factory=make_controller,
            server_factory=lambda dispatcher: IpcServer(
                dispatcher, ("127.0.0.1", 0)),
        )
        service_thread = threading.Thread(
            target=lambda: service_result.append(service.run(stop_event)),
            name="benchmark-controller-service",
        )
        service_thread.start()
        startup_deadline = time.monotonic() + 3.0
        while time.monotonic() < startup_deadline:
            if controller_holder and controller_holder[0].is_alive():
                break
            if service_result:
                raise RuntimeError(
                    f"mock controller service exited early: {service_result[0]}")
            time.sleep(0.01)
        else:
            stop_event.set()
            service_thread.join(5.0)
            raise TimeoutError("mock controller service did not start")

        try:
            cold = reader.sample_basic()
            private_ws_cold = reader.private_working_set_bytes(
                cold.working_set_bytes)
            shape_cold = (
                reader.process_shape()
                if hasattr(reader, "process_shape") else None
            )
            if warmup_seconds:
                time.sleep(warmup_seconds)

            initial = reader.sample_basic()
            private_ws_initial = reader.private_working_set_bytes(
                initial.working_set_bytes)
            shape_initial = (
                reader.process_shape()
                if hasattr(reader, "process_shape") else None
            )
            started_at = time.perf_counter()
            deadline = started_at + duration_seconds
            working_set_peak = max(
                cold.working_set_bytes, initial.working_set_bytes)
            private_commit_peak = max(
                cold.private_commit_bytes, initial.private_commit_bytes)
            sample_count = 2

            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                time.sleep(min(sample_interval_seconds, remaining))
                sample = reader.sample_basic()
                sample_count += 1
                working_set_peak = max(
                    working_set_peak, sample.working_set_bytes)
                private_commit_peak = max(
                    private_commit_peak, sample.private_commit_bytes)

            finished_at = time.perf_counter()
            final = reader.sample_basic()
            sample_count += 1
            working_set_peak = max(working_set_peak, final.working_set_bytes)
            private_commit_peak = max(
                private_commit_peak, final.private_commit_bytes)
            private_ws_final = reader.private_working_set_bytes(
                final.working_set_bytes)
            shape_final = (
                reader.process_shape()
                if hasattr(reader, "process_shape") else None
            )
            private_ws_peak_observed = max(
                private_ws_cold, private_ws_initial, private_ws_final)
            elapsed = finished_at - started_at
            cpu_percent = cpu_percent_one_core(
                initial.cpu_seconds, final.cpu_seconds, elapsed)
        finally:
            stop_event.set()
            service_thread.join(5.0)
            service_exit_status = (
                service_result[0] if service_result else None)
            shutdown_confirmed = (
                not service_thread.is_alive() and service_exit_status == 0)

    webview_after = _webview_loaded()
    checks = evaluate_budget(
        cpu_percent,
        private_ws_peak_observed,
        duration_seconds=elapsed,
        no_webview_imported=not (webview_before or webview_after),
        shutdown_confirmed=shutdown_confirmed,
    )
    return {
        "schema_version": 1,
        "benchmark": "headless-controller-idle",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "application_version": __version__,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "pid": os.getpid(),
        "backends": {"sensors": "mock", "fans": "mock"},
        "service_exit_status": service_exit_status,
        "ui_or_webview_started": False,
        "measurement": {
            "requested_seconds": float(duration_seconds),
            "observed_seconds": round(elapsed, 6),
            "warmup_seconds": float(warmup_seconds),
            "sample_interval_seconds": float(sample_interval_seconds),
            "basic_sample_count": sample_count,
            "cpu_seconds": round(
                max(0.0, final.cpu_seconds - initial.cpu_seconds), 6),
            "cpu_percent_one_core": round(cpu_percent, 6),
            "working_set_peak_bytes": int(working_set_peak),
            "private_commit_peak_bytes": int(private_commit_peak),
            "cold_working_set_bytes": int(cold.working_set_bytes),
            "cold_private_commit_bytes": int(cold.private_commit_bytes),
            "cold_private_working_set_bytes": int(private_ws_cold),
            "private_working_set_initial_bytes": int(private_ws_initial),
            "private_working_set_final_bytes": int(private_ws_final),
            "private_working_set_peak_observed_bytes": int(
                private_ws_peak_observed),
            "private_working_set_method": (
                "Win32 QueryWorkingSet resident pages with Shared bit clear"),
            "handles_cold": (
                shape_cold.handle_count if shape_cold is not None else None),
            "handles_initial": (
                shape_initial.handle_count if shape_initial is not None else None),
            "handles_final": (
                shape_final.handle_count if shape_final is not None else None),
            "threads_cold": (
                shape_cold.thread_count if shape_cold is not None else None),
            "threads_initial": (
                shape_initial.thread_count if shape_initial is not None else None),
            "threads_final": (
                shape_final.thread_count if shape_final is not None else None),
        },
        "checks": checks,
        "passed": bool(checks["passed"]),
    }


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure idle CPU and memory of the headless controller")
    parser.add_argument(
        "--duration", type=float, default=DEFAULT_DURATION_SECONDS,
        help="measured idle window in seconds (default: 60)")
    parser.add_argument(
        "--warmup", type=float, default=DEFAULT_WARMUP_SECONDS,
        help="unmeasured controller warmup in seconds (default: 2)")
    parser.add_argument(
        "--sample-interval", type=float,
        default=DEFAULT_SAMPLE_INTERVAL_SECONDS,
        help="working-set sampling interval in seconds (default: 1)")
    parser.add_argument(
        "--output", type=Path,
        default=REPOSITORY_ROOT / "qa" / "headless-idle.json",
        help="JSON result path")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        report = run_benchmark(
            duration_seconds=args.duration,
            warmup_seconds=args.warmup,
            sample_interval_seconds=args.sample_interval,
        )
    except (OSError, ValueError) as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Result written to {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
