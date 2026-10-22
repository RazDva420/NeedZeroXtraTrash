"""Clean, locked DEV/RELEASE builder for NeedZeroXtraTrash on Windows x64."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import venv
import uuid


ROOT = Path(__file__).resolve().parent
VERSION_PATH = ROOT / "version.txt"
BOOTSTRAP_LOCK = ROOT / "requirements-bootstrap.lock"
RUNTIME_LOCK = ROOT / "requirements.lock"
LHM_LOCK = ROOT / "deps" / "librehardwaremonitor.lock.json"
SPEC_PATH = ROOT / "NeedZeroXtraTrash.spec"
BOOTSTRAP_WHEELHOUSE = ROOT / "build" / "lock-bootstrap"
RUNTIME_WHEELHOUSE = ROOT / "build" / "lock-wheels"
PACKAGE_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;]+)\s+--hash=sha256:([0-9a-f]{64})$"
)
CERT_RE = re.compile(r"[0-9A-Fa-f]{40}")
SOURCE_DATE_EPOCH = "946684800"
REQUIRED_PYTHON = (3, 12, 14)
MANIFEST_SCHEMA = 2
_IGNORED_SOURCE_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
_IGNORED_SOURCE_SUFFIXES = {
    ".pyc", ".pyo", ".tmp", ".temp", ".bak", ".orig", ".rej", ".swp",
}


class BuildError(RuntimeError):
    pass


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
        "dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask",
        "dwFileFlags", "dwFileOS", "dwFileType", "dwFileSubtype",
        "dwFileDateMS", "dwFileDateLS",
    )]


class _CRYPT_HASH_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_base_version() -> str:
    value = VERSION_PATH.read_text(encoding="utf-8").strip()
    parts = value.split(".")
    if not (2 <= len(parts) <= 4 and all(part.isdigit() for part in parts)):
        raise BuildError(f"invalid version.txt: {value!r}")
    return value


def display_version(mode: str) -> str:
    base = read_base_version()
    return base if mode == "release" else f"{base}-dev"


def artifact_name(mode: str) -> str:
    base = read_base_version()
    suffix = "" if mode == "release" else "-UNSIGNED-DEV"
    return f"NeedZeroXtraTrash v{base}{suffix}.exe"


def validate_requirements_lock(path: Path) -> dict[str, tuple[str, str]]:
    packages: dict[str, tuple[str, str]] = {}
    allowed_options = {
        "--only-binary=:all:",
        "--no-binary=crcmod,docopt,proxy-tools",
    }
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--"):
            if line not in allowed_options:
                raise BuildError(f"unsupported lock option at {path.name}:{line_number}")
            continue
        match = PACKAGE_RE.fullmatch(line)
        if match is None:
            raise BuildError(f"unhashed or unpinned requirement at {path.name}:{line_number}")
        name, version, digest = match.groups()
        canonical = _canonical_name(name)
        if canonical in packages:
            raise BuildError(f"duplicate locked package: {name}")
        packages[canonical] = (version, digest)
    if not packages:
        raise BuildError(f"empty dependency lock: {path}")
    return packages


def validate_locks() -> tuple[dict, dict, dict]:
    bootstrap = validate_requirements_lock(BOOTSTRAP_LOCK)
    runtime = validate_requirements_lock(RUNTIME_LOCK)
    declared = {}
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line or ";" in line:
            raise BuildError("requirements.txt must contain only exact top-level pins")
        name, version = line.split("==", 1)
        declared[_canonical_name(name)] = version
    for name, version in declared.items():
        if runtime.get(name, (None,))[0] != version:
            raise BuildError(f"requirements.txt and requirements.lock disagree for {name}")
    if runtime.get("crcmod", (None,))[0] != "1.7":
        raise BuildError("requirements.lock must pin crcmod 1.7 for the pure-Python build")
    if runtime.get("pyinstaller", (None,))[0] != "6.21.0":
        raise BuildError("requirements.lock must pin the audited PyInstaller 6.21.0")
    try:
        lhm = json.loads(LHM_LOCK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"invalid LHM dependency lock: {exc}") from exc
    if lhm.get("schema") != 1 or not isinstance(lhm.get("files"), dict):
        raise BuildError("unsupported LHM dependency lock")
    return bootstrap, runtime, lhm


def _check_python(
    *,
    os_name: str | None = None,
    platform_name: str | None = None,
    version_info=None,
    implementation: str | None = None,
    pointer_size: int | None = None,
    machine: str | None = None,
) -> None:
    """Require the exact audited interpreter, not merely a 3.12 family build."""
    os_name = os.name if os_name is None else os_name
    platform_name = sys.platform if platform_name is None else platform_name
    version_info = sys.version_info if version_info is None else version_info
    implementation = (
        platform.python_implementation() if implementation is None else implementation
    )
    pointer_size = struct.calcsize("P") if pointer_size is None else pointer_size
    machine = platform.machine() if machine is None else machine
    if os_name != "nt" or platform_name != "win32":
        raise BuildError("Windows is required for the Windows EXE build")
    if tuple(version_info[:3]) != REQUIRED_PYTHON:
        required = ".".join(str(part) for part in REQUIRED_PYTHON)
        raise BuildError(f"the locked build requires exactly CPython {required}")
    if implementation != "CPython":
        raise BuildError("the locked build requires CPython")
    if pointer_size != 8 or machine.lower() not in {"amd64", "x86_64"}:
        raise BuildError("the locked build requires 64-bit x86 CPython")


def _safe_clean(path: Path) -> None:
    path = Path(path).resolve()
    build_root = (ROOT / "build").resolve()
    if path == build_root or build_root not in path.parents:
        raise BuildError(f"refusing to clean unsafe path: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def _build_env(stage: Path, mode: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in list(env):
        if name.upper().startswith("PIP_") or name.upper() in {
            "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "NZXT_LHM_DLL",
        }:
            env.pop(name, None)
    env.update({
        "NEEDZERO_BUILD_CHANNEL": mode,
        "PIP_CACHE_DIR": str(stage / "pip-cache"),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_INDEX_URL": "https://pypi.org/simple",
        "PIP_NO_INPUT": "1",
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
    })
    return env


def _locked_package_source() -> list[str]:
    """Use the audited local artifact cache when it is available.

    ``--require-hashes`` remains authoritative: the wheelhouse only removes the
    mutable network from an otherwise identical locked install.  Both folders
    are required because pip/setuptools/wheel bootstrap artifacts are kept
    separate from runtime wheels and source distributions.
    """
    override = os.environ.get("NEEDZERO_WHEELHOUSE", "").strip()
    if override:
        root = Path(override)
        if not root.is_absolute() or not root.is_dir():
            raise BuildError("NEEDZERO_WHEELHOUSE must be an existing absolute directory")
        return ["--no-index", "--find-links", str(root)]
    if BOOTSTRAP_WHEELHOUSE.is_dir() and RUNTIME_WHEELHOUSE.is_dir():
        return [
            "--no-index",
            "--find-links", str(BOOTSTRAP_WHEELHOUSE),
            "--find-links", str(RUNTIME_WHEELHOUSE),
        ]
    return []


def _run(command: list[str], *, env: dict[str, str]) -> None:
    printable = " ".join(command[:4])
    print(f"> {printable}{' ...' if len(command) > 4 else ''}")
    proc = subprocess.run(command, cwd=ROOT, env=env)
    if proc.returncode != 0:
        raise BuildError(f"command failed with exit code {proc.returncode}: {command[0]}")


def _signtool() -> Path | None:
    from src.needzero.windows_security import program_files_x86_directory

    root = program_files_x86_directory() / "Windows Kits" / "10" / "bin"
    candidates = sorted(root.glob("*/x64/signtool.exe"), reverse=True)
    return candidates[0] if candidates else None


def _windows_powershell() -> Path:
    from src.needzero.windows_security import system_directory

    executable = system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise BuildError("Windows PowerShell signing fallback was not found")
    return executable


def _windows_powershell_env(base: dict[str, str] | None = None) -> dict[str, str]:
    from src.needzero.windows_security import sanitized_system_env

    env = dict(os.environ if base is None else base)
    env.update(sanitized_system_env())
    return env


def _powershell_certificate_has_private_key(thumbprint: str) -> bool:
    script = (
        "$ErrorActionPreference='Stop';"
        "$cert=Get-Item -LiteralPath "
        "('Cert:\\CurrentUser\\My\\'+$env:NEEDZERO_CERT_THUMBPRINT);"
        "if(-not $cert.HasPrivateKey){exit 1}"
    )
    env = _windows_powershell_env()
    env["NEEDZERO_CERT_THUMBPRINT"] = thumbprint
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-NonInteractive", "-Command",
         script],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _signing_certificate_has_private_key(thumbprint: str) -> bool:
    """Read-only CurrentUser/My certificate+private-key preflight."""
    if os.name != "nt":
        return False
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CertOpenSystemStoreW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    crypt32.CertOpenSystemStoreW.restype = ctypes.c_void_p
    crypt32.CertFindCertificateInStore.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    crypt32.CertFindCertificateInStore.restype = ctypes.c_void_p
    crypt32.CryptAcquireCertificatePrivateKey.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.BOOL),
    ]
    crypt32.CryptAcquireCertificatePrivateKey.restype = wintypes.BOOL
    crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
    crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]

    store = crypt32.CertOpenSystemStoreW(None, "MY")
    if not store:
        return False
    certificate = None
    try:
        digest = bytes.fromhex(thumbprint)
        digest_buffer = (ctypes.c_ubyte * len(digest)).from_buffer_copy(digest)
        blob = _CRYPT_HASH_BLOB(len(digest), digest_buffer)
        certificate = crypt32.CertFindCertificateInStore(
            store,
            0x00000001 | 0x00010000,  # X509_ASN_ENCODING | PKCS_7_ASN_ENCODING
            0,
            0x00010000,  # CERT_FIND_SHA1_HASH
            ctypes.byref(blob),
            None,
        )
        if not certificate:
            return False
        key_handle = ctypes.c_void_p()
        key_spec = wintypes.DWORD()
        caller_must_free = wintypes.BOOL()
        acquired = crypt32.CryptAcquireCertificatePrivateKey(
            certificate,
            0x00000040 | 0x00000004 | 0x00010000,  # SILENT | COMPARE_KEY | ALLOW_NCRYPT_KEY
            None,
            ctypes.byref(key_handle),
            ctypes.byref(key_spec),
            ctypes.byref(caller_must_free),
        )
        if not acquired:
            return False
        if caller_must_free.value:
            if key_spec.value == 0xFFFFFFFF:  # CERT_NCRYPT_KEY_SPEC
                ncrypt = ctypes.WinDLL("ncrypt", use_last_error=True)
                ncrypt.NCryptFreeObject.argtypes = [ctypes.c_void_p]
                ncrypt.NCryptFreeObject(key_handle)
            else:
                advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
                advapi32.CryptReleaseContext.argtypes = [ctypes.c_void_p, wintypes.DWORD]
                advapi32.CryptReleaseContext(key_handle, 0)
        return True
    finally:
        if certificate:
            crypt32.CertFreeCertificateContext(certificate)
        crypt32.CertCloseStore(store, 0)


def release_signing_preflight() -> tuple[Path | None, str]:
    thumbprint = os.environ.get("NEEDZERO_SIGN_SHA1", "").replace(" ", "")
    if CERT_RE.fullmatch(thumbprint) is None:
        raise BuildError("RELEASE requires NEEDZERO_SIGN_SHA1 with a 40-hex certificate thumbprint")
    signtool = _signtool()
    if signtool is None and os.environ.get("NEEDZERO_LOCAL_SIGNING") != "1":
        raise BuildError("Windows SDK x64 signtool.exe was not found in Program Files")
    if signtool is None:
        _windows_powershell()
    has_private_key = _signing_certificate_has_private_key(thumbprint)
    if signtool is None and not has_private_key:
        has_private_key = _powershell_certificate_has_private_key(thumbprint)
    if not has_private_key:
        raise BuildError(
            "RELEASE signing certificate/private key was not found in CurrentUser\\My"
        )
    return signtool, thumbprint.upper()


def _file_version(path: Path) -> tuple[tuple[int, int, int, int], str, str]:
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    ignored = wintypes.DWORD()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        raise BuildError("built EXE has no Windows version resource")
    buffer = ctypes.create_string_buffer(size)
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise BuildError("could not read Windows version resource")
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL

    def query(subblock: str) -> tuple[ctypes.c_void_p, int]:
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(buffer, subblock, ctypes.byref(pointer), ctypes.byref(length)):
            raise BuildError(f"missing version resource field: {subblock}")
        return pointer, int(length.value)

    root_pointer, _ = query("\\")
    fixed = ctypes.cast(root_pointer, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
    if fixed.dwSignature != 0xFEEF04BD:
        raise BuildError("invalid fixed version resource")
    numeric = (
        fixed.dwFileVersionMS >> 16,
        fixed.dwFileVersionMS & 0xFFFF,
        fixed.dwFileVersionLS >> 16,
        fixed.dwFileVersionLS & 0xFFFF,
    )
    file_pointer, _ = query(r"\StringFileInfo\040904B0\FileVersion")
    product_pointer, _ = query(r"\StringFileInfo\040904B0\ProductVersion")
    return numeric, ctypes.wstring_at(file_pointer), ctypes.wstring_at(product_pointer)


def _verify_version_resource(executable: Path, mode: str) -> None:
    parts = [int(part) for part in read_base_version().split(".")]
    expected_numeric = tuple((parts + [0, 0, 0, 0])[:4])
    numeric, file_version, product_version = _file_version(executable)
    expected_display = display_version(mode)
    if numeric != expected_numeric or file_version != expected_display or product_version != expected_display:
        raise BuildError(
            f"version metadata mismatch: {numeric!r}, {file_version!r}, {product_version!r}"
        )


def _is_manifest_source(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    lowered_parts = {part.lower() for part in relative.parts}
    if lowered_parts & _IGNORED_SOURCE_PARTS:
        return False
    lowered_name = path.name.lower()
    if lowered_name.endswith("~") or path.suffix.lower() in _IGNORED_SOURCE_SUFFIXES:
        return False
    return True


def _source_manifest(root: Path | None = None) -> dict[str, str]:
    """Hash deterministic build inputs while excluding interpreter/editor debris."""
    root = (ROOT if root is None else Path(root)).resolve()
    source_root = root / "src"
    files = []
    if source_root.is_dir():
        files.extend(
            path for path in source_root.rglob("*")
            if path.is_file() and _is_manifest_source(path, root)
        )
    explicit = [
        root / "build_release.py", root / "fetch_deps.py",
        root / "NeedZeroXtraTrash.spec", root / "build.bat",
        root / "version.txt", root / "requirements-bootstrap.lock",
        root / "requirements.lock", root / "requirements.txt",
        root / "deps" / "librehardwaremonitor.lock.json",
        root / "icon.ico", root / "icon.png",
    ]
    files.extend(path for path in explicit if path.is_file())
    entries: dict[str, str] = {}
    for path in sorted(set(files), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink():
            raise BuildError(f"source manifest refuses symlink: {path}")
        relative = path.relative_to(root).as_posix()
        if relative in entries:
            raise BuildError(f"duplicate source manifest path: {relative}")
        entries[relative] = _sha256(path)
    return entries


def _normalized_archive_name(name: str) -> str:
    normalized = str(name).replace("\\", "/")
    if not normalized or normalized.startswith("/") or "../" in f"/{normalized}/":
        raise BuildError(f"unsafe PyInstaller payload name: {name!r}")
    return normalized


def inspect_pyinstaller_payload(executable: Path, reader_factory=None) -> dict:
    """Inventory the actual CArchive and nested PYZ stored in a built EXE.

    This is intentionally performed on the completed artifact.  A spec-file list
    cannot prove what hooks or transitive imports actually placed in the bundle.
    """
    executable = Path(executable)
    if reader_factory is None:
        try:
            from PyInstaller.archive.readers import CArchiveReader
        except ImportError as exc:
            raise BuildError("PyInstaller archive reader is unavailable") from exc
        reader_factory = CArchiveReader
    try:
        archive = reader_factory(str(executable))
    except Exception as exc:
        raise BuildError(f"could not open built PyInstaller CArchive: {exc}") from exc

    entries = []
    seen = set()
    embedded_build_info = None
    for original_name, toc in sorted(
        archive.toc.items(), key=lambda item: (str(item[0]).casefold(), str(item[0]))
    ):
        name = _normalized_archive_name(original_name)
        if name in seen:
            raise BuildError(f"duplicate normalized CArchive entry: {name}")
        seen.add(name)
        try:
            _offset, _stored_size, uncompressed_size, compressed, typecode = toc
            data = archive.extract(original_name)
        except Exception as exc:
            raise BuildError(f"could not extract CArchive entry {name!r}: {exc}") from exc
        if not isinstance(data, (bytes, bytearray)):
            raise BuildError(f"CArchive entry has no byte payload: {name}")
        data = bytes(data)
        entry = {
            "name": name,
            "type": str(typecode),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "compressed": bool(compressed),
        }
        if int(uncompressed_size) != len(data):
            raise BuildError(f"CArchive size mismatch while inventorying {name}")
        if name == "build-info.json":
            try:
                embedded_build_info = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BuildError("packaged build-info.json is malformed") from exc
        if str(typecode) == "z":
            try:
                pyz = archive.open_embedded_archive(original_name)
            except Exception as exc:
                raise BuildError(f"could not open embedded PYZ {name!r}: {exc}") from exc
            pyz_entries = []
            pyz_seen = set()
            for pyz_original, pyz_toc in sorted(
                pyz.toc.items(), key=lambda item: (str(item[0]).casefold(), str(item[0]))
            ):
                pyz_name = _normalized_archive_name(pyz_original)
                if pyz_name in pyz_seen:
                    raise BuildError(f"duplicate normalized PYZ entry: {pyz_name}")
                pyz_seen.add(pyz_name)
                try:
                    pyz_type, _pyz_offset, pyz_stored_size = pyz_toc
                    raw = pyz.extract(pyz_original, raw=True)
                except Exception as exc:
                    raise BuildError(f"could not extract PYZ entry {pyz_name!r}: {exc}") from exc
                if raw is None:
                    raw_size, raw_hash = 0, None
                elif isinstance(raw, (bytes, bytearray)):
                    raw = bytes(raw)
                    raw_size = len(raw)
                    raw_hash = hashlib.sha256(raw).hexdigest()
                else:
                    raise BuildError(f"PYZ entry has unsupported payload: {pyz_name}")
                pyz_entries.append({
                    "name": pyz_name,
                    "type": int(pyz_type),
                    "size": raw_size,
                    "sha256": raw_hash,
                    "stored_size": int(pyz_stored_size),
                })
            entry["pyz_entries"] = pyz_entries
        entries.append(entry)

    inventory = {
        "schema": 1,
        "format": "pyinstaller-carchive-pyz",
        "options": sorted(str(option) for option in getattr(archive, "options", [])),
        "entries": entries,
        "embedded_build_info": embedded_build_info,
    }
    return inventory


def _inventory_digest(inventory: dict) -> str:
    canonical = json.dumps(
        inventory, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _inventory_index(inventory: dict) -> dict[str, tuple]:
    indexed = {}
    for entry in inventory.get("entries", []):
        key = f"carchive:{entry.get('type')}:{entry.get('name')}"
        indexed[key] = (entry.get("size"), entry.get("sha256"))
        for nested in entry.get("pyz_entries", []):
            nested_key = f"pyz:{nested.get('type')}:{nested.get('name')}"
            indexed[nested_key] = (nested.get("size"), nested.get("sha256"))
    return indexed


def compare_payload_inventories(reference: dict, candidate: dict) -> None:
    if _inventory_digest(reference) == _inventory_digest(candidate):
        return
    expected = _inventory_index(reference)
    actual = _inventory_index(candidate)
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    changed = sorted(
        name for name in set(expected) & set(actual) if expected[name] != actual[name]
    )
    details = []
    if added:
        details.append("added=" + ",".join(added[:8]))
    if removed:
        details.append("removed=" + ",".join(removed[:8]))
    if changed:
        details.append("changed=" + ",".join(changed[:8]))
    if reference.get("embedded_build_info") != candidate.get("embedded_build_info"):
        details.append("embedded_build_info changed")
    raise BuildError("packaged payload differs from reference: " + "; ".join(details))


def _validate_packaged_inventory(inventory: dict, mode: str) -> None:
    if inventory.get("schema") != 1 or inventory.get("format") != "pyinstaller-carchive-pyz":
        raise BuildError("unsupported packaged payload inventory")
    expected_info = {
        "schema": 1,
        "version": read_base_version(),
        "channel": mode,
    }
    if inventory.get("embedded_build_info") != expected_info:
        raise BuildError("embedded build identity does not match version.txt/build mode")
    names = set()
    for entry in inventory.get("entries", []):
        names.add(str(entry.get("name")))
        names.update(str(nested.get("name")) for nested in entry.get("pyz_entries", []))
    if "crcmod._crcfunext" in names:
        raise BuildError("non-deterministic compiled crcmod extension was packaged")
    if "crcmod" in names and "crcmod._crcfunpy" not in names:
        raise BuildError("crcmod pure-Python implementation is missing from the package")


def _load_reference_inventory(path: Path) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"could not read reference build manifest: {exc}") from exc
    inventory = document.get("packaged_payload")
    if not isinstance(inventory, dict):
        raise BuildError("reference manifest has no packaged_payload inventory")
    return inventory


def _write_manifest(
    path: Path,
    executable: Path,
    mode: str,
    runtime_lock: dict,
    lhm_lock: dict,
    inventory: dict,
    signed: bool,
) -> None:
    source_inputs = _source_manifest()
    dependency_inputs = {
        "python_dependencies": {
            name: {"version": value[0], "sha256": value[1]}
            for name, value in sorted(runtime_lock.items())
        },
        "lhm": lhm_lock,
        "distribution_notices": [
            {
                "component": "Calculator font",
                "notice": "Free personal use; commercial use requires a separate author license.",
                "notice_path": "src/needzero/webui/assets/fonts/LICENSE-Calculator.txt",
                "commercial_distribution_blocker": True,
            },
            {
                "component": "Nunito font",
                "notice": "SIL Open Font License 1.1",
                "notice_path": "src/needzero/webui/assets/fonts/OFL-Nunito.txt",
                "commercial_distribution_blocker": False,
            },
        ],
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "product": "NeedZeroXtraTrash",
        "version": display_version(mode),
        "channel": mode,
        "artifact": executable.name,
        "artifact_sha256": _sha256(executable),
        "artifact_size": executable.stat().st_size,
        "authenticode": "valid" if signed else "unsigned-dev",
        "python": ".".join(str(part) for part in REQUIRED_PYTHON),
        "python_architecture": "x64",
        "python_implementation": platform.python_implementation(),
        "bootstrap_lock_sha256": _sha256(BOOTSTRAP_LOCK),
        "runtime_lock_sha256": _sha256(RUNTIME_LOCK),
        "source_manifest_sha256": hashlib.sha256(json.dumps(
            source_inputs, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
        "payload_manifest_sha256": _inventory_digest(inventory),
        "source_inputs": source_inputs,
        "dependency_inputs": dependency_inputs,
        "packaged_payload": inventory,
    }
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _collect_packaged_inventory(
    python: Path,
    executable: Path,
    output: Path,
    mode: str,
    env: dict[str, str],
) -> dict:
    _run([
        str(python), str(Path(__file__).resolve()),
        "--inventory-exe", str(executable),
        "--inventory-output", str(output),
        "--inventory-mode", mode,
    ], env=env)
    try:
        inventory = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"could not read packaged payload inventory: {exc}") from exc
    _validate_packaged_inventory(inventory, mode)
    return inventory


def _publish_pair(
    staged_executable: Path,
    staged_manifest: Path,
    destination: Path,
    destination_manifest: Path,
    *,
    replace_fn=None,
) -> None:
    """Publish EXE+manifest as one rollback-capable filesystem transaction."""
    replace_fn = os.replace if replace_fn is None else replace_fn
    staged_executable = Path(staged_executable)
    staged_manifest = Path(staged_manifest)
    destination = Path(destination)
    destination_manifest = Path(destination_manifest)
    if destination.parent != destination_manifest.parent:
        raise BuildError("artifact and manifest must be published to the same directory")
    if not staged_executable.is_file() or not staged_manifest.is_file():
        raise BuildError("staged artifact pair is incomplete")
    destination.parent.mkdir(parents=True, exist_ok=True)

    transaction = uuid.uuid4().hex
    pairs = [
        (staged_executable, destination),
        (staged_manifest, destination_manifest),
    ]
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        for _staged, target in pairs:
            if target.exists():
                backup = target.with_name(f".{target.name}.rollback-{transaction}")
                if backup.exists():
                    raise BuildError(f"rollback path unexpectedly exists: {backup}")
                replace_fn(target, backup)
                backups[target] = backup
        for staged, target in pairs:
            replace_fn(staged, target)
            published.append(target)
        try:
            published_manifest = json.loads(
                destination_manifest.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise BuildError(f"published manifest is unreadable: {exc}") from exc
        if published_manifest.get("artifact") != destination.name:
            raise BuildError("published manifest names a different artifact")
        if published_manifest.get("artifact_sha256") != _sha256(destination):
            raise BuildError("published artifact hash does not match its manifest")
    except Exception as publish_error:
        rollback_errors = []
        for target in reversed(published):
            backup = backups.get(target)
            try:
                if backup is not None and backup.exists():
                    replace_fn(backup, target)
                    backups.pop(target, None)
                elif target.exists():
                    target.unlink()
            except Exception as exc:
                rollback_errors.append(f"{target.name}: {exc}")
        for target, backup in list(backups.items())[::-1]:
            try:
                if backup.exists():
                    replace_fn(backup, target)
                backups.pop(target, None)
            except Exception as exc:
                rollback_errors.append(f"{target.name}: {exc}")
        if rollback_errors:
            raise BuildError(
                f"publication failed ({publish_error}); rollback failed: "
                + "; ".join(rollback_errors)
            ) from publish_error
        raise BuildError(f"publication failed and was rolled back: {publish_error}") from publish_error
    for backup in backups.values():
        if backup.exists():
            backup.unlink()


def compare_manifest_files(first: Path, second: Path) -> None:
    compare_payload_inventories(
        _load_reference_inventory(first), _load_reference_inventory(second)
    )


def build(mode: str, reference_manifest: Path | None = None) -> Path:
    if mode not in {"dev", "release"}:
        raise BuildError("build mode must be DEV or RELEASE")
    _check_python()
    _bootstrap, runtime_lock, lhm_lock = validate_locks()
    reference_inventory = (
        _load_reference_inventory(reference_manifest)
        if reference_manifest is not None else None
    )
    # RELEASE authorization/signing prerequisites are checked before cleaning or
    # creating any build/output path.
    signing = release_signing_preflight() if mode == "release" else None
    if mode == "dev" and os.environ.get("NEEDZERO_SIGN_SHA1"):
        raise BuildError("DEV artifacts are deliberately unsigned; unset NEEDZERO_SIGN_SHA1")

    stage = ROOT / "build" / f"staging-{mode}"
    _safe_clean(stage)
    generated = ROOT / "build" / "generated"
    _safe_clean(generated)
    env = _build_env(stage, mode)
    toolchain = stage / "toolchain"
    venv.EnvBuilder(with_pip=True, clear=True).create(toolchain)
    python = toolchain / "Scripts" / "python.exe"
    package_source = _locked_package_source()
    common = [
        str(python), "-m", "pip", "install", "--require-hashes",
        *package_source,
    ]
    _run([*common, "-r", str(BOOTSTRAP_LOCK)], env=env)
    _run([*common, "--no-build-isolation", "-r", str(RUNTIME_LOCK)], env=env)
    _run([str(python), "-m", "pip", "check"], env=env)

    from fetch_deps import DependencyIntegrityError, load_lock, verify_payload_dir
    try:
        verify_payload_dir(ROOT / "lib", load_lock())
    except DependencyIntegrityError:
        _run([str(python), str(ROOT / "fetch_deps.py")], env=env)
        verify_payload_dir(ROOT / "lib", load_lock())

    output_dir = stage / "out"
    work_dir = stage / "pyinstaller"
    output_dir.mkdir()
    _run([
        str(python), "-m", "PyInstaller", "--clean", "--noconfirm",
        "--distpath", str(output_dir), "--workpath", str(work_dir), str(SPEC_PATH),
    ], env=env)
    executable = output_dir / artifact_name(mode)
    if not executable.is_file() or set(output_dir.iterdir()) != {executable}:
        raise BuildError("PyInstaller staging must contain exactly the expected EXE")
    _verify_version_resource(executable, mode)

    signed = False
    if signing is not None:
        signtool, thumbprint = signing
        if signtool is None or os.environ.get("NEEDZERO_LOCAL_SIGNING") == "1":
            script = (
                "$ErrorActionPreference='Stop';"
                "$cert=Get-Item -LiteralPath "
                "('Cert:\\CurrentUser\\My\\'+$env:NEEDZERO_CERT_THUMBPRINT);"
                "$result=Set-AuthenticodeSignature "
                "-LiteralPath $env:NEEDZERO_SIGN_TARGET "
                "-Certificate $cert -HashAlgorithm SHA256;"
                "if($result.Status -ne 'Valid'){throw ('signing failed: '+$result.Status)}"
            )
            signing_env = _windows_powershell_env(env)
            signing_env["NEEDZERO_CERT_THUMBPRINT"] = thumbprint
            signing_env["NEEDZERO_SIGN_TARGET"] = str(executable)
            _run([
                str(_windows_powershell()), "-NoProfile", "-NonInteractive",
                "-Command", script,
            ], env=signing_env)
        else:
            _run([
                str(signtool), "sign", "/sha1", thumbprint, "/fd", "SHA256",
                "/td", "SHA256", "/tr", "https://timestamp.digicert.com", str(executable),
            ], env=env)
            _run([str(signtool), "verify", "/pa", "/all", "/v", str(executable)], env=env)
        from src.needzero.windows_security import authenticode_is_valid
        if not authenticode_is_valid(executable):
            raise BuildError("WinVerifyTrust rejected the signed RELEASE artifact")
        signed = True

    inventory_path = stage / "packaged-payload.json"
    inventory = _collect_packaged_inventory(
        python, executable, inventory_path, mode, env
    )
    if reference_inventory is not None:
        compare_payload_inventories(reference_inventory, inventory)

    # Prove that neither Windows metadata nor the embedded application identity
    # depends on the filename chosen by a distributor/user.
    renamed_probe = stage / "renamed-version-probe.exe"
    renamed_inventory_path = stage / "renamed-version-probe.inventory.json"
    shutil.copy2(executable, renamed_probe)
    try:
        _verify_version_resource(renamed_probe, mode)
        renamed_inventory = _collect_packaged_inventory(
            python, renamed_probe, renamed_inventory_path, mode, env
        )
        compare_payload_inventories(inventory, renamed_inventory)
    finally:
        renamed_probe.unlink(missing_ok=True)
        renamed_inventory_path.unlink(missing_ok=True)

    staged_manifest = stage / f"{executable.name}.manifest.json"
    _write_manifest(
        staged_manifest, executable, mode, runtime_lock, lhm_lock, inventory, signed
    )
    dist = ROOT / "dist"
    destination = dist / executable.name
    destination_manifest = dist / staged_manifest.name
    _publish_pair(executable, staged_manifest, destination, destination_manifest)
    print(f"Built: {destination}")
    print(f"SHA-256: {_sha256(destination)}")
    print(f"Authenticode: {'valid' if signed else 'UNSIGNED DEV (not a release)'}")
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dev", "release"), default="dev")
    parser.add_argument(
        "--compare-payload-manifest", type=Path,
        help="fail unless this clean build has the same actual CArchive/PYZ payload",
    )
    parser.add_argument(
        "--compare-manifests", nargs=2, type=Path, metavar=("FIRST", "SECOND"),
        help="compare actual packaged payload inventories without building",
    )
    parser.add_argument("--inventory-exe", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--inventory-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--inventory-mode", choices=("dev", "release"), help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    try:
        if args.inventory_exe is not None:
            if args.inventory_output is None or args.inventory_mode is None:
                raise BuildError("inventory worker requires output and mode")
            inventory = inspect_pyinstaller_payload(args.inventory_exe)
            _validate_packaged_inventory(inventory, args.inventory_mode)
            args.inventory_output.write_text(
                json.dumps(inventory, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return 0
        if args.inventory_output is not None or args.inventory_mode is not None:
            raise BuildError("inventory output/mode require --inventory-exe")
        if args.compare_manifests is not None:
            compare_manifest_files(*args.compare_manifests)
            print("Packaged payload inventories are identical")
            return 0
        build(args.mode, args.compare_payload_manifest)
        return 0
    except (BuildError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
