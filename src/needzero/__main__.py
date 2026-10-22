"""Frozen/source entry point."""

from __future__ import annotations

import multiprocessing
import sys


if __name__ == "__main__":
    # Required before argument parsing so PyInstaller child processes used for
    # bounded USB ownership do not recursively launch the application.
    multiprocessing.freeze_support()
    try:
        from .entrypoint import main
    except ImportError:
        from needzero.entrypoint import main
    sys.exit(main())
