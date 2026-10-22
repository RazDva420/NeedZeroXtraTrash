"""NeedZeroXtraTrash - case fan control for the NZXT RGB & Fan Controller.

Fan output is driven through liquidctl; temperatures are read in-process from
native Windows/NVML sources.  The control loop maps temperature to fan duty
using user-editable curves, with an emergency override on hot sensors.
"""

import json
import sys
from pathlib import Path


def _runtime_build_info() -> tuple[str, str]:
    if getattr(sys, "frozen", False):
        try:
            payload = json.loads(
                (Path(getattr(sys, "_MEIPASS")) / "build-info.json").read_text(
                    encoding="utf-8"
                )
            )
            version = str(payload["version"])
            channel = str(payload["channel"])
            if channel in ("dev", "release") and _valid_version(version):
                return version, channel
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # A malformed/missing build marker must never silently gain release
            # privileges such as machine autostart installation.
            return "4.6", "dev"
    try:
        value = (Path(__file__).resolve().parents[2] / "version.txt").read_text(
            encoding="utf-8").strip()
        if _valid_version(value):
            return value, "dev"
    except OSError:
        pass
    return "4.6", "dev"


def _valid_version(value: str) -> bool:
    parts = value.split(".")
    return 2 <= len(parts) <= 4 and all(part.isdigit() for part in parts)


BASE_VERSION, BUILD_CHANNEL = _runtime_build_info()
__version__ = BASE_VERSION if BUILD_CHANNEL == "release" else f"{BASE_VERSION}-dev"
BUILD_DISPLAY_VERSION = __version__
APP_NAME = "NeedZeroXtraTrash"
