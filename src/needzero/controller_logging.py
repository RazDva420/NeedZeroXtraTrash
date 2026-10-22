"""Bounded protected logging for the headless machine controller."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .runtime_paths import controller_log_path, ipc_token_path
from .windows_security import (
    assert_no_reparse_points,
    ensure_protected_directory,
    protected_file_reader_sid,
    protect_machine_file,
    require_protected_directory,
)


_HANDLER_MARKER = "_needzero_controller_log"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3


class _ProtectedRotatingFileHandler(RotatingFileHandler):
    """Reapply the exact machine-file ACL whenever rotation creates a file."""

    def __init__(self, *args, file_protector, path_checker, **kwargs):
        self._file_protector = file_protector
        self._path_checker = path_checker
        super().__init__(*args, **kwargs)

    def protect_files(self) -> None:
        base = Path(self.baseFilename)
        for candidate in (
            base,
            *(Path(f"{base}.{index}") for index in range(1, self.backupCount + 1)),
        ):
            if candidate.is_file():
                self._path_checker(candidate)
                self._file_protector(candidate)

    def doRollover(self) -> None:
        super().doRollover()
        self.protect_files()


def configure_controller_logging(
    path: Path | None = None,
    *,
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    directory_protector: Callable[[Path], Path] | None = None,
    file_protector: Callable[[Path], Path] = protect_machine_file,
    path_checker: Callable[[Path], Path] = assert_no_reparse_points,
) -> RotatingFileHandler:
    """Attach one bounded UTF-8 file handler to the process root logger.

    The caller is the LocalSystem controller.  Directory and file protectors
    are injectable so tests never modify host ACLs.
    """
    if max_bytes <= 0 or backup_count < 1:
        raise ValueError("controller log rotation limits must be positive")
    default_path = path is None
    path = Path(path or controller_log_path())
    if not path.is_absolute():
        raise ValueError("controller log path must be absolute")
    if directory_protector is not None:
        directory_protector(path.parent)
    elif default_path:
        # The installer intentionally leaves one non-inheriting RX ACE on the
        # ProgramData directory so the desktop client can reach ipc.token.
        # Logging must validate and preserve it, never reset the parent DACL.
        reader_sid = protected_file_reader_sid(ipc_token_path())
        if reader_sid is None:
            raise RuntimeError("controller IPC token has no desktop reader")
        require_protected_directory(path.parent, reader_sid)
    else:
        ensure_protected_directory(path.parent)
    path_checker(path)

    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, _HANDLER_MARKER, False):
            protector = getattr(existing, "protect_files", None)
            if callable(protector):
                protector()
            return existing

    handler = _ProtectedRotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=False,
        file_protector=file_protector,
        path_checker=path_checker,
    )
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    try:
        handler.protect_files()
    except BaseException:
        handler.close()
        raise
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return handler
