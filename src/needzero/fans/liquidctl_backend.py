"""liquidctl backend for the NZXT RGB & Fan Controller (AC-2RGBC-B1).

The controller is driven by liquidctl's ``SmartDevice2`` driver: three fan
channels (``fan1``/``fan2``/``fan3``), fixed-duty control only, with RPM/duty
read back from ``get_status()``.  The curve -> duty maths therefore run in
software (see :mod:`needzero.controlloop`) and this backend only pushes fixed
duties and reports state.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, Optional

from .base import FanBackend, FanStatus

_LOGGER = logging.getLogger(__name__)

# Descriptions (from liquidctl) that expose 3 controllable fan channels.
_SUPPORTED = re.compile(r"(RGB & Fan Controller|Smart Device V2)", re.I)
_FAN_RE = re.compile(r"Fan (\d+) (speed|duty|control mode)", re.I)


class LiquidctlFanBackend(FanBackend):
    name = "liquidctl"

    def __init__(self, preferred_controller_id: Optional[str] = None) -> None:
        self.available = False
        self.description = ""
        self.preferred_controller_id = preferred_controller_id
        self.controller_id: Optional[str] = None
        self._dev = None
        self._lock = threading.Lock()
        self._last_duty: Dict[str, float] = {}

    # -- lifecycle --------------------------------------------------------

    def open(self) -> None:
        from liquidctl import find_liquidctl_devices  # lazy: Windows/USB only

        supported = []
        for dev in find_liquidctl_devices():
            desc = getattr(dev, "description", "") or ""
            if _SUPPORTED.search(desc):
                supported.append((self.device_identity(dev), dev))

        chosen = None
        chosen_id = None
        if self.preferred_controller_id:
            for identity, dev in supported:
                if identity == self.preferred_controller_id:
                    chosen_id, chosen = identity, dev
                    break
            if chosen is None:
                raise RuntimeError(
                    "Configured NZXT controller is unavailable; refusing to switch "
                    f"to another device ({self.preferred_controller_id}).")
        elif len(supported) == 1:
            chosen_id, chosen = supported[0]
            if chosen_id is None:
                raise RuntimeError(
                    "The NZXT controller exposes only an unstable USB address; "
                    "refusing automatic control without a stable serial/path/port."
                )
        elif len(supported) > 1:
            identities = ", ".join(
                identity or "<unstable USB address>"
                for identity, _dev in supported
            )
            raise RuntimeError(
                "Multiple supported NZXT controllers found; select a stable "
                f"controller id first: {identities}")

        if chosen is None:
            raise RuntimeError(
                "No NZXT RGB & Fan Controller / Smart Device V2 found by liquidctl.")

        chosen.connect()
        try:
            chosen.initialize()  # required after power-on; detects fans
        except Exception as exc:  # noqa: BLE001 - keep going, status may still work
            _LOGGER.warning("initialize() failed: %s", exc)

        self._dev = chosen
        self.controller_id = chosen_id
        self.preferred_controller_id = chosen_id
        self.description = getattr(chosen, "description", "NZXT controller")
        self.channels = self._detect_channels(chosen)
        self.available = True
        _LOGGER.info("Connected to %s (%s)", self.description, ", ".join(self.channels))

    @staticmethod
    def device_identity(dev) -> Optional[str]:
        """Return the strongest stable identity exposed by liquidctl."""
        def value(name):
            try:
                result = getattr(dev, name, None)
                return result() if callable(result) else result
            except Exception:  # noqa: BLE001 - USB descriptor may be unreadable
                return None

        vid = value("vendor_id")
        pid = value("product_id")
        try:
            prefix = f"{int(vid):04x}:{int(pid):04x}"
        except (TypeError, ValueError):
            prefix = "unknown"
        serial = value("serial_number")
        if serial:
            return f"{prefix}:serial:{serial}"

        device = getattr(dev, "device", None)
        path = (getattr(device, "path", None) if device is not None else None)
        if path is None and device is not None:
            hidinfo = getattr(device, "hidinfo", None)
            if isinstance(hidinfo, dict):
                path = hidinfo.get("path")
        if isinstance(path, bytes):
            path = path.hex()
        if path:
            return f"{prefix}:path:{path}"

        port = value("port")
        bus = value("bus")
        if port is not None:
            return f"{prefix}:port:{bus}:{port}"
        # USB address changes across re-enumeration. Persisting it as though it
        # were stable can either brick reconnect or select a neighbour later,
        # so an address-only device is not eligible for automatic control.
        return None

    @staticmethod
    def _detect_channels(dev) -> tuple:
        speed = getattr(dev, "_speed_channels", None)
        if isinstance(speed, dict) and speed:
            return tuple(sorted(speed.keys()))
        return ("fan1", "fan2", "fan3")

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._dev = None
            self.available = False

    # -- control ----------------------------------------------------------

    def set_duty(self, channel: str, duty: float) -> bool:
        if self._dev is None:
            return False
        duty = int(max(0, min(100, round(duty))))
        with self._lock:
            try:
                self._dev.set_fixed_speed(channel, duty)
                self._last_duty[channel] = duty
                return True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("set_fixed_speed(%s, %d) failed: %s", channel, duty, exc)
                return False

    def set_many(self, duties: Dict[str, float]) -> Dict[str, bool]:
        # Write channels one at a time with a small gap. Some controller
        # revisions drop back-to-back writes (liquidctl issue #775).
        results: Dict[str, bool] = {}
        for ch, duty in duties.items():
            results[ch] = self.set_duty(ch, duty)
            time.sleep(0.04)
        return results

    # -- monitoring -------------------------------------------------------

    def read_status(self) -> Dict[str, FanStatus]:
        result: Dict[str, FanStatus] = {
            ch: FanStatus(channel=ch) for ch in self.channels
        }
        if self._dev is None:
            raise RuntimeError("NZXT controller is not connected")

        with self._lock:
            status = self._dev.get_status()

        for key, value, _unit in status:
            m = _FAN_RE.match(str(key))
            if not m:
                continue
            ch = f"fan{m.group(1)}"
            if ch not in result:
                result[ch] = FanStatus(channel=ch)
            field = m.group(2).lower()
            if field == "speed":
                try:
                    result[ch].rpm = int(value)
                except (TypeError, ValueError):
                    pass
            elif field == "duty":
                try:
                    result[ch].duty = float(value)
                except (TypeError, ValueError):
                    pass
            elif field == "control mode":
                result[ch].mode = str(value) if value else None

        return result
