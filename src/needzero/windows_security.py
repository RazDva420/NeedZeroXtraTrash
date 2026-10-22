"""Trusted Windows paths, token identity and protected machine directories.

This module deliberately avoids ``PATH`` and user-controlled environment
variables at privilege boundaries.  It is import-safe on non-Windows hosts so
the pure validation code can still be tested there.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Iterable, Optional


APP_DIR_NAME = "NeedZeroXtraTrash"
_CSIDL_APPDATA = 0x001A
_CSIDL_COMMON_APPDATA = 0x0023
_CSIDL_PROGRAM_FILES = 0x0026
_CSIDL_PROGRAM_FILES_X86 = 0x002A
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_TOKEN_ELEVATION = 20
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SW_SHOWNORMAL = 1
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF
_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_SAFER_FLAG = 0x00000100
_WTD_CACHE_ONLY_URL_RETRIEVAL = 0x00001000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_INSUFFICIENT_BUFFER = 122
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_GENERIC_READ = 0x00120089
_FILE_GENERIC_READ_EXECUTE = 0x001200A9
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER_STRUCT(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _TOKEN_ELEVATION_STRUCT(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pcwszFilePath", wintypes.LPCWSTR),
        ("hFile", wintypes.HANDLE),
        ("pgKnownSubject", ctypes.POINTER(_GUID)),
    ]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD),
        ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
        ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPCWSTR),
        ("dwProvFlags", wintypes.DWORD),
        ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("this operation is only available on Windows")


def system_directory() -> Path:
    """Return the real System32 directory via ``GetSystemDirectoryW``."""
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


def system_executable(name: str) -> Path:
    """Resolve a system executable without consulting ``PATH`` or cwd."""
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError("system executable must be a plain filename")
    path = system_directory() / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def windows_powershell_executable() -> Path:
    """Return the inbox Windows PowerShell executable below System32."""
    path = system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _file_attributes(path: Path) -> int | None:
    """Return Win32 file attributes, or ``None`` for a missing component."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetFileAttributesW.restype = wintypes.DWORD
    attributes = int(kernel32.GetFileAttributesW(str(path)))
    if attributes != _INVALID_FILE_ATTRIBUTES:
        return attributes
    error = ctypes.get_last_error()
    if error in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        return None
    raise ctypes.WinError(error)


def assert_no_reparse_points(path: Path) -> Path:
    """Reject symlinks/junctions in an absolute privileged filesystem path.

    The check is intentionally lexical: resolving the path first would follow a
    planted junction before we had a chance to reject it.  Callers repeat this
    check after directory creation and immediately before protected file work.
    """
    _require_windows()
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("a privileged filesystem path must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        attributes = _file_attributes(current)
        if attributes is None:
            # Descendants cannot exist when this component does not exist.
            break
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError(f"refusing reparse point in privileged path: {current}")
    return path


def _known_folder(csidl: int) -> Path:
    _require_windows()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHGetFolderPathW.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    shell32.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(32768)
    result = shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise OSError(result, f"SHGetFolderPathW failed for CSIDL {csidl:#x}")
    return Path(buffer.value)


def program_files_directory() -> Path:
    return _known_folder(_CSIDL_PROGRAM_FILES)


def program_files_x86_directory() -> Path:
    return _known_folder(_CSIDL_PROGRAM_FILES_X86)


def program_data_directory() -> Path:
    return _known_folder(_CSIDL_COMMON_APPDATA)


def roaming_appdata_directory() -> Path:
    return _known_folder(_CSIDL_APPDATA)


def install_dir() -> Path:
    """Stable protected install directory used by the scheduled tasks."""
    return program_files_directory() / APP_DIR_NAME


def machine_data_dir() -> Path:
    """Machine-owned state directory; callers may create it with the helper below."""
    return program_data_directory() / APP_DIR_NAME


def sanitized_system_env() -> dict[str, str]:
    """Minimal environment for elevated Microsoft system executables.

    In particular this drops user-provided ``PATH``, ``PSModulePath`` and DLL
    override variables while retaining the values PowerShell needs to load its
    protected built-in modules.
    """
    system32 = system_directory()
    windows = system32.parent
    return {
        "SystemRoot": str(windows),
        "WINDIR": str(windows),
        "ComSpec": str(system32 / "cmd.exe"),
        "PATH": str(system32),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PSModulePath": str(
            windows / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
        ),
    }


def authenticode_is_valid(path: Path) -> bool:
    """Verify the embedded Authenticode signature with ``WinVerifyTrust``.

    Verification is non-interactive and cache-only so enabling autostart cannot
    hang on an unavailable network.  Release creation performs the additional
    timestamp/revocation verification before publishing the artifact.
    """
    _require_windows()
    path = assert_no_reparse_points(Path(path))
    if not path.is_file():
        return False
    wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    action = _GUID(
        0x00AAC56B,
        0xCD44,
        0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file_info = _WINTRUST_FILE_INFO()
    file_info.cbStruct = ctypes.sizeof(file_info)
    file_info.pcwszFilePath = str(path)
    data = _WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(data)
    data.dwUIChoice = _WTD_UI_NONE
    data.fdwRevocationChecks = _WTD_REVOKE_NONE
    data.dwUnionChoice = _WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = _WTD_STATEACTION_VERIFY
    data.dwProvFlags = _WTD_SAFER_FLAG | _WTD_CACHE_ONLY_URL_RETRIEVAL
    wintrust.WinVerifyTrust.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
    ]
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    result = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    data.dwStateAction = _WTD_STATEACTION_CLOSE
    wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    return result == 0


def _open_process_token() -> tuple[object, wintypes.HANDLE]:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, token


def is_elevated() -> bool:
    """Read elevation from the current access token, not environment variables."""
    kernel32, token = _open_process_token()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    value = _TOKEN_ELEVATION_STRUCT()
    returned = wintypes.DWORD()
    try:
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_ELEVATION,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(returned),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(value.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)


def current_user_sid() -> str:
    """Return the current token user SID in canonical string form."""
    kernel32, token = _open_process_token()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    try:
        advapi32.GetTokenInformation(
            token, _TOKEN_USER, None, 0, ctypes.byref(needed)
        )
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            needed,
            ctypes.byref(needed),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER_STRUCT)).contents
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            token_user.User.Sid, ctypes.byref(sid_text)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def account_name_for_sid(sid: str) -> str:
    """Return Task Scheduler's canonical ``DOMAIN\\user`` name for a SID."""
    _require_windows()
    if not isinstance(sid, str) or not sid:
        raise ValueError("SID must be a non-empty string")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.LookupAccountSidW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.LookupAccountSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    sid_pointer = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(sid_pointer)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        name_size = wintypes.DWORD()
        domain_size = wintypes.DWORD()
        sid_type = wintypes.DWORD()
        ctypes.set_last_error(0)
        advapi32.LookupAccountSidW(
            None, sid_pointer, None, ctypes.byref(name_size), None,
            ctypes.byref(domain_size), ctypes.byref(sid_type),
        )
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(ctypes.get_last_error())
        name = ctypes.create_unicode_buffer(name_size.value)
        domain = ctypes.create_unicode_buffer(domain_size.value)
        if not advapi32.LookupAccountSidW(
            None, sid_pointer, name, ctypes.byref(name_size), domain,
            ctypes.byref(domain_size), ctypes.byref(sid_type),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return f"{domain.value}\\{name.value}" if domain.value else name.value
    finally:
        kernel32.LocalFree(sid_pointer)


def read_exact_dacl(path: Path) -> tuple[bool, frozenset[tuple[str, int, int]]]:
    """Return DACL protection and every allow ACE as ``(SID, mask, flags)``.

    Any non-allow ACE is rejected so callers can compare the complete DACL,
    rather than assuming that ``icacls /grant:r`` removed foreign explicit ACEs.
    """
    _require_windows()
    path = assert_no_reparse_points(Path(path))
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise OSError(result, f"GetNamedSecurityInfoW failed for {path}")
    try:
        if not dacl.value:
            raise RuntimeError(f"privileged path has a null DACL: {path}")
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        size = _ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(size),
            ctypes.sizeof(size),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        entries = set()
        for index in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
            if header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
                raise RuntimeError(f"unexpected non-allow ACE on privileged path: {path}")
            mask = wintypes.DWORD.from_address(ace.value + 4).value
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(ace.value + 8), ctypes.byref(sid_text)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                entries.add((sid_text.value, int(mask), int(header.AceFlags)))
            finally:
                kernel32.LocalFree(sid_text)
        return bool(control.value & _SE_DACL_PROTECTED), frozenset(entries)
    finally:
        kernel32.LocalFree(descriptor)


def read_owner_sid(path: Path) -> str:
    """Return the canonical owner SID for a privileged filesystem object."""
    _require_windows()
    path = assert_no_reparse_points(Path(path))
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise OSError(result, f"GetNamedSecurityInfoW owner query failed for {path}")
    try:
        if not owner.value:
            raise RuntimeError(f"privileged path has no owner: {path}")
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.LocalFree(descriptor)


def _run_acl_command(
    path: Path,
    arguments: list[str],
    runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    proc = runner(
        [str(system_executable("icacls.exe")), str(path), *arguments],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(system_directory()),
        env=sanitized_system_env(),
        creationflags=0x08000000,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "icacls failed").strip())


def _apply_exact_dacl(
    path: Path,
    grants: list[str],
    expected: frozenset[tuple[str, int, int]],
    *,
    runner: Callable[..., subprocess.CompletedProcess],
    dacl_reader: Callable[[Path], tuple[bool, frozenset[tuple[str, int, int]]]],
    owner_reader: Callable[[Path], str],
) -> None:
    # A hostile pre-creator who remains owner can restore their own ACE later.
    # Transfer ownership first; /reset removes every existing explicit ACE and
    # /inheritance:r then removes inherited ACEs before exact grants are added.
    _run_acl_command(path, ["/setowner", f"*{_ADMINISTRATORS_SID}"], runner)
    _run_acl_command(path, ["/reset"], runner)
    _run_acl_command(path, ["/inheritance:r", "/grant:r", *grants], runner)
    owner = owner_reader(path)
    protected, actual = dacl_reader(path)
    if owner != _ADMINISTRATORS_SID or not protected or actual != expected:
        raise RuntimeError(
            f"privileged path security verification failed: {path}; "
            f"owner={owner!r}; actual={sorted(actual)!r}"
        )


def _directory_acl(reader_sid: str | None = None) -> frozenset[tuple[str, int, int]]:
    entries = {
        (_SYSTEM_SID, _FILE_ALL_ACCESS, _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE),
        (_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE),
    }
    if reader_sid is not None:
        entries.add((_validate_sid(reader_sid), _FILE_GENERIC_READ_EXECUTE, 0))
    return frozenset(entries)


def _machine_file_acl(reader_sid: str | None = None) -> frozenset[tuple[str, int, int]]:
    entries = {
        (_SYSTEM_SID, _FILE_ALL_ACCESS, 0),
        (_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0),
    }
    if reader_sid is not None:
        entries.add((_validate_sid(reader_sid), _FILE_GENERIC_READ, 0))
    return frozenset(entries)


def _executable_file_acl(reader_sid: str) -> frozenset[tuple[str, int, int]]:
    return frozenset({
        (_SYSTEM_SID, _FILE_ALL_ACCESS, 0),
        (_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0),
        (_validate_sid(reader_sid), _FILE_GENERIC_READ_EXECUTE, 0),
    })


def require_protected_directory(path: Path, reader_sid: str | None = None) -> Path:
    """Fail unless an existing directory has exactly the intended protected DACL."""
    path = assert_no_reparse_points(Path(path))
    if not path.is_dir():
        raise FileNotFoundError(path)
    owner, (protected, actual) = read_owner_sid(path), read_exact_dacl(path)
    expected = _directory_acl(reader_sid)
    if owner not in {_SYSTEM_SID, _ADMINISTRATORS_SID} or not protected or actual != expected:
        raise RuntimeError(f"protected directory has an unexpected DACL: {path}")
    return path


def require_protected_file(path: Path, reader_sid: str | None = None) -> Path:
    """Fail unless an existing file has exactly the intended protected DACL."""
    path = assert_no_reparse_points(Path(path))
    if not path.is_file():
        raise FileNotFoundError(path)
    owner, (protected, actual) = read_owner_sid(path), read_exact_dacl(path)
    expected = _machine_file_acl(reader_sid)
    if owner not in {_SYSTEM_SID, _ADMINISTRATORS_SID} or not protected or actual != expected:
        raise RuntimeError(f"protected file has an unexpected DACL: {path}")
    return path


def require_protected_executable(path: Path, reader_sid: str) -> Path:
    """Require a machine-owned executable with one read/execute desktop SID."""
    path = assert_no_reparse_points(Path(path))
    if not path.is_file():
        raise FileNotFoundError(path)
    owner, (protected, actual) = read_owner_sid(path), read_exact_dacl(path)
    expected = _executable_file_acl(reader_sid)
    if owner not in {_SYSTEM_SID, _ADMINISTRATORS_SID} or not protected or actual != expected:
        raise RuntimeError(f"protected executable has an unexpected DACL: {path}")
    return path


def protected_file_reader_sid(path: Path) -> str | None:
    """Return the sole read-only desktop SID on an exactly protected file.

    SYSTEM and Administrators are mandatory.  Any other mask, inheritance flag,
    foreign owner, or more than one desktop reader fails closed.
    """
    path = assert_no_reparse_points(Path(path))
    if not path.is_file():
        raise FileNotFoundError(path)
    owner, (protected, actual) = read_owner_sid(path), read_exact_dacl(path)
    if owner not in {_SYSTEM_SID, _ADMINISTRATORS_SID} or not protected:
        raise RuntimeError(f"protected file has an unexpected owner/DACL: {path}")
    machine_entries = _machine_file_acl()
    if not machine_entries.issubset(actual):
        raise RuntimeError(f"protected file is missing machine principals: {path}")
    readers = actual - machine_entries
    if not readers:
        return None
    if len(readers) != 1:
        raise RuntimeError(f"protected file has multiple/foreign readers: {path}")
    sid, mask, flags = next(iter(readers))
    if mask != _FILE_GENERIC_READ or flags != 0:
        raise RuntimeError(f"protected file reader is over-privileged: {path}")
    _validate_sid(sid)
    return sid


def ensure_protected_directory(
    path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dacl_reader: Callable[[Path], tuple[bool, frozenset[tuple[str, int, int]]]] = read_exact_dacl,
    owner_reader: Callable[[Path], str] = read_owner_sid,
) -> Path:
    """Create a SYSTEM/Administrators-only directory and verify ``icacls``.

    SIDs are used instead of localized account names.  Callers must already be
    elevated.  Tests inject a deterministic runner and never touch machine ACLs.
    """
    _require_windows()
    path = assert_no_reparse_points(Path(path))
    path.mkdir(parents=True, exist_ok=True)
    assert_no_reparse_points(path)
    _apply_exact_dacl(
        path,
        [f"*{_SYSTEM_SID}:(OI)(CI)F", f"*{_ADMINISTRATORS_SID}:(OI)(CI)F"],
        _directory_acl(),
        runner=runner,
        dacl_reader=dacl_reader,
        owner_reader=owner_reader,
    )
    return path


def _validate_sid(sid: str) -> str:
    if not isinstance(sid, str) or not sid.startswith("S-1-"):
        raise ValueError("a canonical Windows SID is required")
    if any(character not in "S-0123456789" for character in sid):
        raise ValueError("a canonical Windows SID is required")
    return sid


def grant_directory_traverse(
    path: Path,
    sid: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dacl_reader: Callable[[Path], tuple[bool, frozenset[tuple[str, int, int]]]] = read_exact_dacl,
    owner_reader: Callable[[Path], str] = read_owner_sid,
) -> Path:
    """Grant one desktop user non-inheriting access to reach an IPC token file."""
    _require_windows()
    sid = _validate_sid(sid)
    path = assert_no_reparse_points(Path(path))
    if not path.is_dir():
        raise FileNotFoundError(path)
    _apply_exact_dacl(
        path,
        [
            f"*{_SYSTEM_SID}:(OI)(CI)F",
            f"*{_ADMINISTRATORS_SID}:(OI)(CI)F",
            f"*{sid}:(RX)",
        ],
        _directory_acl(sid),
        runner=runner,
        dacl_reader=dacl_reader,
        owner_reader=owner_reader,
    )
    return path


def protect_file_for_user(
    path: Path,
    sid: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dacl_reader: Callable[[Path], tuple[bool, frozenset[tuple[str, int, int]]]] = read_exact_dacl,
    owner_reader: Callable[[Path], str] = read_owner_sid,
) -> Path:
    """Protect one file while granting read-only access to the installer SID."""
    _require_windows()
    sid = _validate_sid(sid)
    path = assert_no_reparse_points(Path(path))
    if not path.is_file():
        raise FileNotFoundError(path)
    _apply_exact_dacl(
        path,
        [f"*{_SYSTEM_SID}:F", f"*{_ADMINISTRATORS_SID}:F", f"*{sid}:R"],
        _machine_file_acl(sid),
        runner=runner,
        dacl_reader=dacl_reader,
        owner_reader=owner_reader,
    )
    return path


def protect_executable_for_user(
    path: Path,
    sid: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dacl_reader: Callable[[Path], tuple[bool, frozenset[tuple[str, int, int]]]] = read_exact_dacl,
    owner_reader: Callable[[Path], str] = read_owner_sid,
) -> Path:
    """Allow one desktop SID to read and execute, but never modify, the EXE."""
    _require_windows()
    sid = _validate_sid(sid)
    path = assert_no_reparse_points(Path(path))
    if not path.is_file():
        raise FileNotFoundError(path)
    _apply_exact_dacl(
        path,
        [f"*{_SYSTEM_SID}:F", f"*{_ADMINISTRATORS_SID}:F", f"*{sid}:(RX)"],
        _executable_file_acl(sid),
        runner=runner,
        dacl_reader=dacl_reader,
        owner_reader=owner_reader,
    )
    return path


def protect_machine_file(
    path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dacl_reader: Callable[[Path], tuple[bool, frozenset[tuple[str, int, int]]]] = read_exact_dacl,
    owner_reader: Callable[[Path], str] = read_owner_sid,
) -> Path:
    """Restrict a machine-owned file to LocalSystem and Administrators."""
    _require_windows()
    path = assert_no_reparse_points(Path(path))
    if not path.is_file():
        raise FileNotFoundError(path)
    _apply_exact_dacl(
        path,
        [f"*{_SYSTEM_SID}:F", f"*{_ADMINISTRATORS_SID}:F"],
        _machine_file_acl(),
        runner=runner,
        dacl_reader=dacl_reader,
        owner_reader=owner_reader,
    )
    return path


def ensure_ipc_token_access(
    token_path: Path,
    sid: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    dacl_reader: Callable[[Path], tuple[bool, frozenset[tuple[str, int, int]]]] = read_exact_dacl,
    owner_reader: Callable[[Path], str] = read_owner_sid,
) -> Path:
    """Allow only SYSTEM/Admins and one installer user to read the IPC token."""
    token_path = Path(token_path)
    grant_directory_traverse(
        token_path.parent,
        sid,
        runner=runner,
        dacl_reader=dacl_reader,
        owner_reader=owner_reader,
    )
    return protect_file_for_user(
        token_path,
        sid,
        runner=runner,
        dacl_reader=dacl_reader,
        owner_reader=owner_reader,
    )


def ensure_machine_data_dir(
    *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> Path:
    return ensure_protected_directory(machine_data_dir(), runner=runner)


def ensure_install_dir(
    *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> Path:
    return ensure_protected_directory(install_dir(), runner=runner)


def run_self_elevated(
    arguments: Iterable[str], *, timeout_seconds: Optional[float] = 120.0
) -> int:
    """Run the frozen executable with UAC ``runas`` and return its exit code."""
    _require_windows()
    if not getattr(sys, "frozen", False):
        raise RuntimeError("elevated self-operation requires the packaged EXE")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    params = subprocess.list2cmdline([str(value) for value in arguments])
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(Path(sys.executable).resolve())
    info.lpParameters = params
    info.lpDirectory = str(Path(sys.executable).resolve().parent)
    info.nShow = _SW_SHOWNORMAL
    previous_reset = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
    if getattr(sys, "frozen", False):
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if previous_reset is None:
            os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        else:
            os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = previous_reset
    try:
        timeout_ms = (
            _INFINITE
            if timeout_seconds is None
            else max(0, min(int(timeout_seconds * 1000), _INFINITE - 1))
        )
        wait_result = kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
        if wait_result == _WAIT_TIMEOUT:
            raise TimeoutError("elevated operation timed out")
        if wait_result != _WAIT_OBJECT_0:
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def launch_self_elevated(arguments: Iterable[str]) -> None:
    """Start the frozen executable through UAC without waiting for its exit."""
    _require_windows()
    if not getattr(sys, "frozen", False):
        raise RuntimeError("elevated self-operation requires the packaged EXE")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.lpVerb = "runas"
    info.lpFile = str(Path(sys.executable).resolve())
    info.lpParameters = subprocess.list2cmdline([str(value) for value in arguments])
    info.lpDirectory = str(Path(sys.executable).resolve().parent)
    info.nShow = _SW_SHOWNORMAL
    previous_reset = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
    if getattr(sys, "frozen", False):
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if previous_reset is None:
            os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        else:
            os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = previous_reset
