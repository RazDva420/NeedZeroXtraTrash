"""Mode dispatcher that keeps controller, tray and WebView import graphs apart."""

from __future__ import annotations

import argparse
import logging
import re

from . import APP_NAME, __version__


def _configure_logging(level: str) -> None:
    logging.basicConfig(**{
        "level": getattr(logging, str(level).upper(), logging.INFO),
        "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
    })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description=APP_NAME)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--controller", action="store_true", help="run the headless controller")
    modes.add_argument("--tray", action="store_true", help="run the lightweight tray client")
    modes.add_argument("--ui", action="store_true", help="open the desktop client")
    modes.add_argument("--startup", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--install-autostart", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--uninstall-autostart", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config-backup-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--log", default="INFO", help="log level")
    args = parser.parse_args(argv)
    if args.config_backup_sha256 is not None:
        if not args.uninstall_autostart:
            parser.error("--config-backup-sha256 requires --uninstall-autostart")
        if not re.fullmatch(r"[0-9A-Fa-f]{64}", args.config_backup_sha256):
            parser.error("--config-backup-sha256 must be a 64-character SHA-256")
    _configure_logging(args.log)

    if args.controller:
        from .controller_service import run_controller
        return run_controller()
    if args.tray:
        from .app import run_tray
        return run_tray()
    if args.startup:
        from .app import run_startup
        return run_startup()
    if args.install_autostart:
        from . import autostart
        try:
            autostart.install_elevated()
            return 0
        except Exception:
            logging.exception("elevated autostart installation failed")
            return 1
    if args.uninstall_autostart:
        from . import autostart
        try:
            autostart.uninstall_elevated(args.config_backup_sha256)
            return 0
        except Exception:
            logging.exception("elevated autostart removal failed")
            return 1

    from .app import run_ui
    return run_ui()
