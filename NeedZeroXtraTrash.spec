# -*- mode: python ; coding: utf-8 -*-
"""Deterministic PyInstaller inputs for the Windows x64 application."""

import hashlib
import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


ROOT = Path.cwd().resolve()
BASE_VERSION = (ROOT / "version.txt").read_text(encoding="utf-8").strip()
parts = BASE_VERSION.split(".")
if not (2 <= len(parts) <= 4 and all(part.isdigit() for part in parts)):
    raise SystemExit(f"invalid version.txt: {BASE_VERSION!r}")
version_tuple = tuple(int(part) for part in parts) + (0,) * (4 - len(parts))

BUILD_CHANNEL = os.environ.get("NEEDZERO_BUILD_CHANNEL", "dev").strip().lower()
if BUILD_CHANNEL not in ("dev", "release"):
    raise SystemExit("NEEDZERO_BUILD_CHANNEL must be dev or release")
DISPLAY_VERSION = BASE_VERSION if BUILD_CHANNEL == "release" else f"{BASE_VERSION}-dev"
APP_NAME = (
    f"NeedZeroXtraTrash v{BASE_VERSION}"
    if BUILD_CHANNEL == "release"
    else f"NeedZeroXtraTrash v{BASE_VERSION}-UNSIGNED-DEV"
)

generated = ROOT / "build" / "generated"
generated.mkdir(parents=True, exist_ok=True)
build_info_path = generated / "build-info.json"
build_info_path.write_text(
    json.dumps(
        {"schema": 1, "version": BASE_VERSION, "channel": BUILD_CHANNEL},
        sort_keys=True,
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
version_info_path = generated / "version-info.txt"
version_info_path.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple!r},
    prodvers={version_tuple!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'NeedZeroXtraTrash'),
         StringStruct('FileDescription', 'NeedZeroXtraTrash fan controller ({BUILD_CHANNEL})'),
         StringStruct('FileVersion', '{DISPLAY_VERSION}'),
         StringStruct('InternalName', 'NeedZeroXtraTrash'),
         StringStruct('OriginalFilename', '{APP_NAME}.exe'),
         StringStruct('ProductName', 'NeedZeroXtraTrash'),
         StringStruct('ProductVersion', '{DISPLAY_VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)""",
    encoding="utf-8",
)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


dependency_lock = json.loads(
    (ROOT / "deps" / "librehardwaremonitor.lock.json").read_text(encoding="utf-8")
)
lib_dir = ROOT / "lib"
expected_lib = dependency_lock["files"]
actual_lib = {item.name for item in lib_dir.iterdir()} if lib_dir.is_dir() else set()
if actual_lib != set(expected_lib):
    raise SystemExit(
        "lib payload allowlist mismatch; run fetch_deps.py before building"
    )
for filename, expected in expected_lib.items():
    path = lib_dir / filename
    if (
        not path.is_file()
        or path.stat().st_size != expected["size"]
        or file_sha256(path).lower() != expected["sha256"].lower()
    ):
        raise SystemExit(f"locked lib payload failed verification: {filename}")


datas = [
    ("src/needzero/webui/index.html", "webui"),
    ("src/needzero/webui/assets/app-logo.png", "webui/assets"),
    ("src/needzero/webui/assets/case_video_h264.mp4", "webui/assets"),
    ("src/needzero/webui/assets/fonts/Calculator.ttf", "webui/assets/fonts"),
    ("src/needzero/webui/assets/fonts/LICENSE-Calculator.txt", "webui/assets/fonts"),
    ("src/needzero/webui/assets/fonts/Nunito-Variable.ttf", "webui/assets/fonts"),
    ("src/needzero/webui/assets/fonts/OFL-Nunito.txt", "webui/assets/fonts"),
    ("src/needzero/webui/assets/icons/cpu-future.png", "webui/assets/icons"),
    ("src/needzero/webui/assets/icons/gpu-future.png", "webui/assets/icons"),
    ("src/needzero/webui/assets/icons/hotspot-future.png", "webui/assets/icons"),
    (str(build_info_path), "."),
]
datas.extend((str(lib_dir / filename), "lib") for filename in sorted(expected_lib))

# Include only Windows x64 pywebview data.  Cross-platform/android/x86 payloads
# are not runtime dependencies of this application.
for source, destination in collect_data_files("webview", includes=["js/**", "lib/**"]):
    normalized = source.replace("\\", "/").lower()
    if any(part in normalized for part in (
        "pywebview-android.jar", "win-arm64", "win-x86", "interop.x86.dll"
    )):
        continue
    datas.append((source, destination))

hiddenimports = [
    "clr",
    # crcmod's optional C extension is built locally from its source-only
    # distribution and is therefore not a reproducible payload.  Force the
    # deterministic upstream pure-Python implementation into the archive.
    "crcmod._crcfunpy",
    "pystray._win32",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]
icon_path = "icon.ico" if (ROOT / "icon.ico").is_file() else None
if (ROOT / "icon.png").is_file():
    datas.append(("icon.png", "."))

a = Analysis(
    ["src/needzero/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        "needzero.sensors.hwinfo",
        "cryptography", "numpy", "tkinter", "crcmod._crcfunext",
        "pystray._appindicator", "pystray._darwin", "pystray._gtk", "pystray._xorg",
        "webview.platforms.android", "webview.platforms.cocoa", "webview.platforms.gtk",
        "webview.platforms.qt",
        "PIL.AvifImagePlugin", "PIL.ImageCms", "PIL.ImageFont", "PIL.ImageTk",
        "PIL.Jpeg2KImagePlugin", "PIL.JpegImagePlugin", "PIL.TiffImagePlugin",
        "PIL.WebPImagePlugin", "PIL._avif", "PIL._imagingcms", "PIL._imagingft",
        "PIL._imagingmath", "PIL._imagingmorph", "PIL._imagingtk", "PIL._webp",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    uac_admin=False,
    icon=icon_path,
    version=str(version_info_path),
)
