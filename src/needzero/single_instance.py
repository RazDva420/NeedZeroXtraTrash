"""Windows named-mutex guards preventing concurrent controller writers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time

_MUTEX_NAME = r"Local\NeedZeroXtraTrash.ControlLoop"
_CONTROLLER_MUTEX_NAME = r"Global\NeedZeroXtraTrash.Controller"
# Protected kernel-object DACL: only LocalSystem and builtin Administrators can
# open/signal the machine-wide controller guard.  A pre-created foreign object
# can at worst make startup fail closed; it can never create a second writer.
_CONTROLLER_MUTEX_SDDL = "O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)"
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_GENERIC_READ_WRITE = 0xC0000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, ctypes.c_wchar_p]
    library.CreateMutexW.restype = ctypes.c_void_p
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_bool
    return library


def _create_protected_controller_mutex(name: str):
    """Create/open the global guard with a SYSTEM/Administrators-only DACL."""
    kernel32 = _kernel32()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_bool
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _CONTROLLER_MUTEX_SDDL, 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        security = _SECURITY_ATTRIBUTES(
            ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False
        )
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(ctypes.byref(security), False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return handle
    finally:
        kernel32.LocalFree(descriptor)


def _prepare_protected_controller_lock() -> Path:
    """Create/validate the release writer lock below protected ProgramData."""
    from .runtime_paths import ipc_token_path, machine_dir
    from .windows_security import (
        assert_no_reparse_points,
        protect_machine_file,
        protected_file_reader_sid,
        require_protected_directory,
        require_protected_file,
    )

    directory = machine_dir()
    reader_sid = protected_file_reader_sid(ipc_token_path())
    if reader_sid is None:
        raise RuntimeError("controller IPC token has no desktop reader")
    require_protected_directory(directory, reader_sid)
    path = assert_no_reparse_points(directory / "controller.lock")
    created = False
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        created = True
        protect_machine_file(path)
    if not created:
        # A simultaneous legitimate controller may be between exclusive create
        # and ACL application.  Only SYSTEM/Admins can write the verified
        # parent, so a short bounded wait cannot be influenced by desktop users.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                require_protected_file(path)
                break
            except (FileNotFoundError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    else:
        require_protected_file(path)
    return path


def _open_exclusive_controller_file(path: Path):
    kernel32 = _kernel32()
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ_WRITE,
        0,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if error in (_ERROR_ACCESS_DENIED, _ERROR_SHARING_VIOLATION):
            return None
        raise ctypes.WinError(error)
    return handle


class SingleInstance:
    def __init__(self, name: str = _MUTEX_NAME) -> None:
        self._name = name
        self._handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        if self._name == _CONTROLLER_MUTEX_NAME:
            from . import BUILD_CHANNEL

            if BUILD_CHANNEL == "release":
                try:
                    path = _prepare_protected_controller_lock()
                    self._handle = _open_exclusive_controller_file(path)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise OSError("protected controller lock is unavailable") from exc
            else:
                self._handle = _create_protected_controller_mutex(self._name)
            return self._handle is not None
        kernel32 = _kernel32()
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None or os.name != "nt":
            return
        _kernel32().CloseHandle(self._handle)
        self._handle = None


def show_already_running_message(
        message: str = "NeedZeroXtraTrash is already running in this Windows session.") -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "NeedZeroXtraTrash",
            0x00000030,
        )
