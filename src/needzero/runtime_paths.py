"""Trusted runtime locations shared by the service and desktop client."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List

def program_data_root() -> Path:
    """Return the OS-known ProgramData folder without trusting environment data."""
    if os.name != "nt":
        return Path.home() / ".local" / "share"
    from .windows_security import program_data_directory
    return program_data_directory()


def machine_dir() -> Path:
    return program_data_root() / "NeedZeroXtraTrash"


def machine_config_path() -> Path:
    return machine_dir() / "config.json"


def ipc_token_path() -> Path:
    return machine_dir() / "ipc.token"


def controller_log_path() -> Path:
    return machine_dir() / "controller.log"


def install_manifest_path() -> Path:
    return machine_dir() / "install.json"


def executable_command(*arguments: str) -> List[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *arguments]
    return [str(Path(sys.executable).resolve()), "-m", "needzero", *arguments]


def independent_process_env() -> Dict[str, str]:
    """Environment for a long-lived child of a PyInstaller one-file process."""
    environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment
