"""Safety-first background control loop.

Temperature decisions, fan command verification and the shutdown write barrier
live here. Hardware I/O remains in fan/sensor backends so this state machine
can be exercised with deterministic fakes.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set

from . import __version__
from .config import Config
from .curves import GroupDecision, Temps, decide_all
from .fans.base import FanBackend, FanStatus
from .sensors.base import SensorBackend, SensorReading, resolve_temps

_LOGGER = logging.getLogger(__name__)

SAFE_EXIT_DUTY = 50.0
UNKNOWN_SAFE_DUTY = 100.0
EMERGENCY_CLEAR_HYSTERESIS_C = 5.0
EMERGENCY_CLEAR_SAMPLES = 3
DUTY_MISMATCH_CYCLES = 2
DUTY_MISMATCH_RETRIES = 1
WRITE_FAILURES_BEFORE_FAULT = 2
ZERO_RPM_CYCLES = 3
UNKNOWN_TELEMETRY_CYCLES = 3
SPINUP_GRACE_SECONDS = 5.0
DUTY_REASSERT_SECONDS = 30.0
FAULT_CLEAR_CYCLES = 3
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0
DEFAULT_SHUTDOWN_TIMEOUT = 8.0


@dataclass
class _ChannelState:
    desired: Optional[float] = None
    commanded: Optional[float] = None
    reported: Optional[float] = None
    confirmed: Optional[float] = None
    command_at: float = 0.0
    target_started_at: float = 0.0
    next_write_at: float = 0.0
    next_reassert_at: float = 0.0
    write_failures: int = 0
    mismatch_cycles: int = 0
    mismatch_retries: int = 0
    zero_rpm_cycles: int = 0
    unknown_telemetry_cycles: int = 0
    status_failures: int = 0
    recovery_cycles: int = 0
    fault: bool = False
    fault_reason: Optional[str] = None

    def connection_changed(self) -> None:
        """Invalidate transport-bound confirmation without erasing failures.

        A reconnect is not evidence of recovery.  Keeping the bounded failure,
        mismatch and telemetry counters prevents a device that repeatedly
        reconnects and fails from evading ``fan_fault`` forever.  A genuinely
        successful write/readback clears the relevant counters normally.
        """
        self.commanded = None
        self.reported = None
        self.confirmed = None
        self.command_at = 0.0
        self.next_write_at = 0.0
        self.next_reassert_at = 0.0


class Controller:
    def __init__(
        self,
        cfg: Config,
        sensors: SensorBackend,
        fans: FanBackend,
        clock: Callable[[], float] = time.monotonic,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        self._cfg = cfg
        self._sensors = sensors
        self._fans = fans
        self._clock = clock
        self._shutdown_timeout = max(0.05, float(shutdown_timeout))

        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._epoch = 0
        self._stopping = False
        self._stop_result: Optional[bool] = None
        self.shutdown_error: Optional[str] = None
        self._fatal_error: Optional[str] = None

        self._channels: Dict[str, _ChannelState] = {}
        self._fan_generation = getattr(self._fans, "connection_generation", 0)
        self._test_until = 0.0
        self._was_testing = False

        self._sensor_fault = False
        self._sensor_fault_reasons: List[str] = []
        self._sensor_health: Dict[str, dict] = {}
        self._seen_emergency_roles: Set[str] = set()
        self._emergency_latched = False
        self._emergency_roles: Set[str] = set()
        self._emergency_clear_count = 0
        self._emergency_reason: Optional[str] = None

        self._snapshot: dict = {
            "time": 0,
            "temps": {},
            "groups": {},
            "backends": {},
            "sensor_fault": False,
            "sensor_fault_reasons": [],
            "sensor_health": {},
            "emergency_latched": False,
            "fan_fault": False,
            "fan_fault_reasons": {},
        }
        self._last_readings: List[SensorReading] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self._stop_lock:
            if self._thread and self._thread.is_alive():
                return
            with self._lock:
                self._epoch += 1
                self._stopping = False
                self._stop_result = None
                self.shutdown_error = None
                self._fatal_error = None
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="nzxt-control", daemon=True)
            self._thread.start()

    def stop(self, safe: bool = True) -> bool:
        """Stop control and, when requested, confirm a final safe write.

        ``False`` means the worker did not stop or at least one final duty
        could not be confirmed; callers must not report a clean shutdown.
        """
        with self._stop_lock:
            if self._stop_result is not None:
                return self._stop_result

            with self._lock:
                self._stopping = True
                self._epoch += 1  # invalidate every already-started cycle
            self._stop.set()
            self._wake.set()

            thread = self._thread
            if thread and thread is not threading.current_thread():
                thread.join(timeout=self._shutdown_timeout)
            worker_stopped = not thread or not thread.is_alive()

            safe_ok = True
            if safe:
                safe_ok = self._write_shutdown_duties()

            reasons = []
            if not worker_stopped:
                reasons.append("control worker did not stop before deadline")
            if not safe_ok:
                reasons.append("final safe fan duty was not confirmed")
            self.shutdown_error = "; ".join(reasons) or None
            self._stop_result = worker_stopped and safe_ok
            return self._stop_result

    # -- external control -------------------------------------------------

    def apply_now(self) -> None:
        self._wake.set()

    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def fatal_error(self) -> Optional[str]:
        with self._lock:
            return self._fatal_error

    def test_full(self, seconds: float = 10.0) -> None:
        with self._lock:
            self._test_until = self._clock() + max(0.0, float(seconds))
        self._wake.set()

    def get_config(self) -> Config:
        with self._lock:
            return self._cfg

    def set_config(self, cfg: Config) -> None:
        with self._lock:
            self._cfg = cfg
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def list_sensors(self) -> List[dict]:
        with self._lock:
            return [reading.as_dict() for reading in self._last_readings]

    # -- main loop --------------------------------------------------------

    def _run(self) -> None:
        try:
            self._run_impl()
        except BaseException as exc:  # noqa: BLE001 - service must observe fatal exit
            message = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._fatal_error = message
            _LOGGER.exception("control worker terminated unexpectedly")

    def _run_impl(self) -> None:
        _LOGGER.info("Control loop started (sensors=%s, fans=%s)",
                     self._sensors.name, self._fans.name)
        while not self._stop.is_set():
            with self._lock:
                epoch = self._epoch
            try:
                self._cycle(epoch)
            except Exception:  # noqa: BLE001 - keep cooling loop observable/alive
                _LOGGER.exception("control cycle failed")
                self._apply_failsafe(epoch)

            with self._lock:
                interval = max(0.5, float(self._cfg.poll_interval))
                test_until = self._test_until
            now = self._clock()
            if test_until > now:
                interval = min(interval, max(0.05, test_until - now))
            self._wake.wait(interval)
            self._wake.clear()

    def _cycle(self, epoch: Optional[int] = None) -> None:
        if epoch is None:
            with self._lock:
                epoch = self._epoch
        if not self._cycle_active(epoch):
            return

        with self._lock:
            cfg = self._cfg
        self._sync_fan_generation()

        sensor_error = None
        try:
            readings = self._sensors.read_all()
        except Exception as exc:  # noqa: BLE001 - a sensor fault is control input
            readings = []
            sensor_error = f"sensor backend error: {type(exc).__name__}: {exc}"
            _LOGGER.error("%s", sensor_error)

        # A read can finish after stop(); do not update state or approach the
        # write path unless this exact cycle generation is still current.
        if not self._cycle_active(epoch):
            return

        temps = resolve_temps(readings, cfg.sensors)
        with self._lock:
            self._update_emergency_latch_locked(cfg, temps)
            if cfg.emergency.enabled:
                for role in ("gpu_hotspot",):
                    if self._emergency_value(temps, role) is not None:
                        self._seen_emergency_roles.add(role)
            emergency_latched = self._emergency_latched

        decisions = decide_all(cfg, temps, emergency=emergency_latched)
        sensor_reasons = []
        if sensor_error:
            sensor_reasons.append(sensor_error)
        for name, decision in decisions.items():
            if decision.sensor_fault and decision.sensor_fault_reason:
                sensor_reasons.append(f"{name}: {decision.sensor_fault_reason}")
        with self._lock:
            required_roles = set()
            for group in cfg.groups.values():
                if group.source in ("cpu", "max"):
                    required_roles.add("cpu")
                if group.source in ("gpu", "max"):
                    required_roles.add("gpu")
            if cfg.emergency.enabled:
                # Not every board exposes every optional emergency probe.
                # Once a probe has been observed (or explicitly mapped), its
                # disappearance is a real sensor loss and must fail safe.
                for role in ("gpu_hotspot",):
                    if (role in self._seen_emergency_roles
                            or getattr(cfg.sensors, role) is not None):
                        required_roles.add(role)
            required_roles.update(self._emergency_roles)
            values = {
                "cpu": temps.cpu,
                "gpu": temps.gpu,
                "gpu_hotspot": temps.gpu_hotspot,
            }
            labels = {
                "cpu": "CPU",
                "gpu": "GPU",
                "gpu_hotspot": "GPU Hotspot",
            }
            for role in sorted(required_roles):
                if values.get(role) is None:
                    sensor_reasons.append(
                        f"{labels[role]}: unavailable, invalid, or stale")
            sensor_reasons = list(dict.fromkeys(sensor_reasons))
            self._sensor_health = {
                role: {
                    "required": role in required_roles,
                    "healthy": value is not None,
                    "reason": (None if value is not None
                               else "unavailable, invalid, or stale"),
                }
                for role, value in values.items()
            }
            self._sensor_fault = bool(sensor_reasons)
            self._sensor_fault_reasons = sensor_reasons

        now = self._clock()
        with self._lock:
            testing = now < self._test_until
            test_ended = self._was_testing and not testing
            faulty_channels = {
                channel for channel, state in self._channels.items() if state.fault
            }

        desired: Dict[str, float] = {}
        for decision in decisions.values():
            desired[decision.channel] = (
                100.0 if testing else float(decision.target_duty))

        # A missing emergency probe is not represented by the individual
        # CPU/GPU curve decisions, so enforce the same safety floor globally.
        if self._sensor_fault:
            for channel in desired:
                desired[channel] = max(desired[channel], SAFE_EXIT_DUTY)

        # A failed fan cannot cool its zone. Raise every other channel to the
        # configured emergency duty while bounded recovery continues.
        if faulty_channels:
            for channel in desired:
                if channel not in faulty_channels:
                    desired[channel] = max(desired[channel], float(cfg.emergency.duty))

        with self._lock:
            to_write = self._select_writes_locked(
                desired, cfg.deadband, now, test_ended)

        if to_write:
            results = self._write_if_current(to_write, epoch)
            if results is None:
                return
            self._sync_fan_generation()
            with self._lock:
                self._record_write_results_locked(to_write, results, now)

        with self._lock:
            self._was_testing = testing

        status_read_failed = False
        try:
            fan_status = self._read_status_if_current(epoch)
        except Exception as exc:  # noqa: BLE001
            status_read_failed = True
            _LOGGER.error("fan status read failed: %s", exc)
            fan_status = {channel: FanStatus(channel=channel) for channel in desired}
            with self._lock:
                for channel in desired:
                    state = self._state_locked(channel)
                    state.status_failures += 1
                    if state.status_failures >= WRITE_FAILURES_BEFORE_FAULT:
                        self._set_fan_fault_locked(
                            state, f"status unavailable: {type(exc).__name__}: {exc}")
        if fan_status is None:
            return

        self._sync_fan_generation()
        with self._lock:
            if not status_read_failed:
                self._verify_status_locked(fan_status, cfg.deadband, now)
            if any(state.fault and channel not in faulty_channels
                   for channel, state in self._channels.items()):
                self._wake.set()

        self._store_snapshot(cfg, temps, decisions, fan_status, readings, testing)

    # -- emergency + sensor safety ---------------------------------------

    @staticmethod
    def _emergency_value(temps: Temps, role: str) -> Optional[float]:
        return temps.gpu_hotspot

    @staticmethod
    def _emergency_threshold(cfg: Config, role: str) -> float:
        return float(cfg.emergency.gpu_hotspot_threshold)

    def _update_emergency_latch_locked(self, cfg: Config, temps: Temps) -> None:
        if not cfg.emergency.enabled:
            self._emergency_latched = False
            self._emergency_roles.clear()
            self._emergency_clear_count = 0
            self._emergency_reason = None
            return

        tripped = set()
        for role in ("gpu_hotspot",):
            value = self._emergency_value(temps, role)
            if value is not None and value >= self._emergency_threshold(cfg, role):
                tripped.add(role)

        if tripped:
            self._emergency_latched = True
            self._emergency_roles.update(tripped)
            self._emergency_clear_count = 0
            labels = {"gpu_hotspot": "GPU Hotspot"}
            self._emergency_reason = ", ".join(
                f"{labels[role]} threshold exceeded" for role in sorted(self._emergency_roles))
            return

        if not self._emergency_latched:
            return

        clearable = bool(self._emergency_roles)
        for role in self._emergency_roles:
            value = self._emergency_value(temps, role)
            threshold = self._emergency_threshold(cfg, role)
            if value is None or value > threshold - EMERGENCY_CLEAR_HYSTERESIS_C:
                clearable = False
                break

        if clearable:
            self._emergency_clear_count += 1
            if self._emergency_clear_count >= EMERGENCY_CLEAR_SAMPLES:
                self._emergency_latched = False
                self._emergency_roles.clear()
                self._emergency_clear_count = 0
                self._emergency_reason = None
        else:
            self._emergency_clear_count = 0

    # -- fan command + verification state machine ------------------------

    def _state_locked(self, channel: str) -> _ChannelState:
        state = self._channels.get(channel)
        if state is None:
            state = _ChannelState()
            self._channels[channel] = state
        return state

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return min(RETRY_MAX_SECONDS,
                   RETRY_BASE_SECONDS * (2 ** min(5, max(0, attempt - 1))))

    def _select_writes_locked(self, desired: Dict[str, float], deadband: float,
                              now: float, test_ended: bool) -> Dict[str, float]:
        selected = {}
        for channel, duty in desired.items():
            state = self._state_locked(channel)
            target_changed = (state.desired is None
                              or abs(float(duty) - state.desired) >= deadband)
            if target_changed:
                state.desired = float(duty)
                state.target_started_at = now
                state.next_write_at = 0.0
                state.mismatch_cycles = 0
                state.mismatch_retries = 0
                state.zero_rpm_cycles = 0
                state.unknown_telemetry_cycles = 0

            command_changed = (state.commanded is None
                               or abs(float(duty) - state.commanded) >= deadband)
            periodic_unknown_reassert = (
                state.commanded is not None
                and state.reported is None
                and now >= state.next_reassert_at
            )
            if now >= state.next_write_at and (
                    command_changed or periodic_unknown_reassert or test_ended):
                selected[channel] = float(duty)
        return selected

    def _record_write_results_locked(self, duties: Dict[str, float],
                                     results: Dict[str, bool], now: float) -> None:
        for channel, duty in duties.items():
            state = self._state_locked(channel)
            if results.get(channel, False):
                state.commanded = float(duty)
                state.command_at = now
                state.next_reassert_at = now + DUTY_REASSERT_SECONDS
                state.next_write_at = 0.0
                state.write_failures = 0
                # Transport-level write acceptance is not a successful status
                # read. Only _verify_status_locked may clear status failures.
            else:
                state.commanded = None
                state.write_failures += 1
                state.next_write_at = now + self._retry_delay(state.write_failures)
                if state.write_failures >= WRITE_FAILURES_BEFORE_FAULT:
                    self._set_fan_fault_locked(state, "fan command was not accepted")

    def _verify_status_locked(self, fan_status: Dict[str, FanStatus],
                              deadband: float, now: float) -> None:
        for channel, state in self._channels.items():
            status = fan_status.get(channel)
            state.status_failures = 0
            state.reported = status.duty if status is not None else None
            if state.commanded is None:
                continue

            healthy = False
            if state.reported is not None:
                try:
                    reported = float(state.reported)
                except (TypeError, ValueError):
                    reported = math.nan
                if math.isfinite(reported) and abs(reported - state.commanded) <= deadband:
                    state.confirmed = reported
                    state.mismatch_cycles = 0
                    state.mismatch_retries = 0
                    state.unknown_telemetry_cycles = 0
                    healthy = True
                else:
                    state.mismatch_cycles += 1
                    state.recovery_cycles = 0
                    if state.mismatch_cycles >= DUTY_MISMATCH_CYCLES:
                        state.mismatch_cycles = 0
                        state.mismatch_retries += 1
                        state.commanded = None
                        state.next_write_at = now + self._retry_delay(
                            state.mismatch_retries)
                        if state.mismatch_retries > DUTY_MISMATCH_RETRIES:
                            self._set_fan_fault_locked(
                                state,
                                f"reported duty {state.reported} does not match command",
                            )
            else:
                state.mismatch_cycles = 0
                rpm = status.rpm if status is not None else None
                in_spinup = now - state.target_started_at < SPINUP_GRACE_SECONDS
                if state.desired is not None and state.desired >= 50 and not in_spinup:
                    if rpm == 0:
                        state.zero_rpm_cycles += 1
                        state.unknown_telemetry_cycles = 0
                        state.recovery_cycles = 0
                        if state.zero_rpm_cycles >= ZERO_RPM_CYCLES:
                            self._set_fan_fault_locked(
                                state, "fan RPM remained zero after spin-up grace")
                    elif rpm is None:
                        state.unknown_telemetry_cycles += 1
                        state.zero_rpm_cycles = 0
                        state.recovery_cycles = 0
                        if state.unknown_telemetry_cycles >= UNKNOWN_TELEMETRY_CYCLES:
                            self._set_fan_fault_locked(
                                state, "neither duty nor RPM telemetry is available")
                    else:
                        state.zero_rpm_cycles = 0
                        state.unknown_telemetry_cycles = 0
                        healthy = rpm > 0
                elif rpm is not None and rpm > 0:
                    healthy = True
                    state.zero_rpm_cycles = 0
                    state.unknown_telemetry_cycles = 0

            # PWM readback confirms the command, not physical rotation.
            if state.reported is not None:
                rpm = status.rpm if status is not None else None
                if rpm == 0 and state.desired is not None and state.desired >= 50:
                    healthy = False
                    if now - state.target_started_at >= SPINUP_GRACE_SECONDS:
                        state.zero_rpm_cycles += 1
                        if state.zero_rpm_cycles >= ZERO_RPM_CYCLES:
                            self._set_fan_fault_locked(
                                state, "fan RPM remained zero after spin-up grace")
                elif rpm is not None and rpm > 0:
                    state.zero_rpm_cycles = 0
                elif state.zero_rpm_cycles:
                    healthy = False  # missing RPM is not proof a stalled fan recovered

            if state.fault:
                if healthy:
                    state.recovery_cycles += 1
                    if state.recovery_cycles >= FAULT_CLEAR_CYCLES:
                        state.fault = False
                        state.fault_reason = None
                        state.recovery_cycles = 0
                else:
                    state.recovery_cycles = 0

    def _set_fan_fault_locked(self, state: _ChannelState, reason: str) -> None:
        if not state.fault or state.fault_reason != reason:
            _LOGGER.error("fan fault: %s", reason)
        state.fault = True
        state.fault_reason = reason

    def _sync_fan_generation(self) -> None:
        generation = getattr(self._fans, "connection_generation", 0)
        with self._lock:
            if generation == self._fan_generation:
                return
            for state in self._channels.values():
                state.connection_changed()
            self._fan_generation = generation

    # -- write barrier + final safe write --------------------------------

    def _cycle_active(self, epoch: int) -> bool:
        with self._lock:
            return (not self._stopping and not self._stop.is_set()
                    and epoch == self._epoch)

    def _write_if_current(self, duties: Dict[str, float], epoch: int) -> Optional[Dict[str, bool]]:
        if not self._io_lock.acquire(timeout=self._shutdown_timeout):
            raise TimeoutError("fan I/O serialization deadline exceeded")
        try:
            if not self._cycle_active(epoch):
                return None
            return self._fans.set_many(duties) or {}
        finally:
            self._io_lock.release()

    def _read_status_if_current(self, epoch: int) -> Optional[Dict[str, FanStatus]]:
        if not self._io_lock.acquire(timeout=self._shutdown_timeout):
            raise TimeoutError("fan status serialization deadline exceeded")
        try:
            if not self._cycle_active(epoch):
                return None
            return self._fans.read_status()
        finally:
            self._io_lock.release()

    def _apply_failsafe(self, epoch: int) -> None:
        """A cycle exception is unknown state, therefore force 100% safely."""
        if not self._cycle_active(epoch):
            return
        duties = {channel: UNKNOWN_SAFE_DUTY for channel in self._fans.channels}
        try:
            results = self._write_if_current(duties, epoch)
            if results is None:
                return
            with self._lock:
                self._record_write_results_locked(
                    duties, results, self._clock())
        except Exception:  # noqa: BLE001
            _LOGGER.exception("fail-safe fan write failed")

    def _shutdown_duties_locked(self) -> Dict[str, float]:
        hazardous = (self._emergency_latched or self._sensor_fault
                     or any(state.fault for state in self._channels.values()))
        duties = {}
        channels = set(self._fans.channels) | set(self._channels)
        for channel in channels:
            state = self._channels.get(channel)
            confirmed = state.confirmed if state is not None else None
            if hazardous or confirmed is None:
                duties[channel] = UNKNOWN_SAFE_DUTY
            else:
                duties[channel] = max(SAFE_EXIT_DUTY, float(confirmed))
        return duties

    @staticmethod
    def _safe_write_confirmed(duties: Dict[str, float], results: Dict[str, bool],
                              status: Dict[str, FanStatus], deadband: float) -> bool:
        for channel, duty in duties.items():
            if not results.get(channel, False):
                return False
            reading = status.get(channel)
            reported = reading.duty if reading else None
            try:
                if reported is None:
                    return False
                value = float(reported)
                if not math.isfinite(value) or not 0 <= value <= 100:
                    return False
                if abs(value - duty) > deadband:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _write_shutdown_duties(self) -> bool:
        with self._lock:
            duties = self._shutdown_duties_locked()
            deadband = float(self._cfg.deadband)

        if not self._io_lock.acquire(timeout=self._shutdown_timeout):
            _LOGGER.critical("could not enter final fan write barrier")
            return False
        try:
            for attempt in range(2):
                if attempt:
                    try:
                        if not self._fans.force_reconnect():
                            return False
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.error("forced fan reconnect failed: %s", exc)
                        return False
                try:
                    results = self._fans.set_many(duties) or {}
                    status = self._fans.read_status()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.error("final safe fan write/readback failed: %s", exc)
                    continue
                if self._safe_write_confirmed(duties, results, status, deadband):
                    return True
            return False
        finally:
            self._io_lock.release()

    # -- snapshot ---------------------------------------------------------

    def _store_snapshot(self, cfg: Config, temps: Temps,
                        decisions: Dict[str, GroupDecision], fan_status: Dict[str, FanStatus],
                        readings: List[SensorReading], testing: bool) -> None:
        with self._lock:
            groups = {}
            for name, decision in decisions.items():
                status = fan_status.get(decision.channel)
                channel_state = self._state_locked(decision.channel)
                groups[name] = {
                    "channel": decision.channel,
                    "label": cfg.groups[name].label,
                    "source": decision.source,
                    "control_temp": decision.control_temp,
                    "curve_duty": decision.curve_duty,
                    "target_duty": decision.target_duty,
                    "effective_target_duty": channel_state.desired,
                    "duty_commanded": channel_state.commanded,
                    "duty_reported": status.duty if status else None,
                    "duty_confirmed": channel_state.confirmed,
                    "emergency": decision.emergency,
                    "sensor_fault": decision.sensor_fault,
                    "sensor_fault_reason": decision.sensor_fault_reason,
                    "fan_fault": channel_state.fault,
                    "fan_fault_reason": channel_state.fault_reason,
                    "rpm": status.rpm if status else None,
                    "mode": status.mode if status else None,
                    "curve": [list(point) for point in cfg.groups[name].sanitized_curve()],
                }

            fan_fault_reasons = {
                channel: state.fault_reason
                for channel, state in self._channels.items()
                if state.fault
            }
            snapshot = {
                "time": time.time(),
                "controller_version": __version__,
                "temps": {
                    "cpu": temps.cpu,
                    "gpu": temps.gpu,
                    "gpu_hotspot": temps.gpu_hotspot,
                },
                "emergency": self._emergency_latched,
                "emergency_latched": self._emergency_latched,
                "emergency_reason": self._emergency_reason,
                "sensor_fault": self._sensor_fault,
                "sensor_fault_reason": "; ".join(self._sensor_fault_reasons) or None,
                "sensor_fault_reasons": list(self._sensor_fault_reasons),
                "sensor_health": {
                    role: dict(health) for role, health in self._sensor_health.items()
                },
                "fan_fault": bool(fan_fault_reasons),
                "fan_fault_reason": "; ".join(
                    f"{channel}: {reason}" for channel, reason in fan_fault_reasons.items()
                ) or None,
                "fan_fault_reasons": fan_fault_reasons,
                "fatal_error": self._fatal_error,
                "testing": testing,
                "mode": cfg.mode,
                "min_duty": cfg.min_duty,
                "groups": groups,
                "backends": {
                    "sensors": self._sensors.name,
                    "sensors_available": bool(getattr(self._sensors, "available", False)),
                    "fans": self._fans.name,
                    "fans_available": bool(getattr(self._fans, "available", False)),
                    "fan_description": getattr(self._fans, "description", ""),
                    "controller_id": getattr(self._fans, "controller_id", None),
                    "channels": list(self._fans.channels),
                    "fan_error": getattr(self._fans, "last_error", None),
                },
            }
            self._snapshot = snapshot
            self._last_readings = readings
