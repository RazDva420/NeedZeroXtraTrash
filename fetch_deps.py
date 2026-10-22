"""Fetch and verify the pinned LibreHardwareMonitor release payload."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request
import uuid
import zipfile


ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / "deps" / "librehardwaremonitor.lock.json"
DEFAULT_OUTPUT = ROOT / "lib"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


class DependencyIntegrityError(RuntimeError):
    pass


def load_lock(path: Path = LOCK_PATH) -> dict:
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyIntegrityError(f"could not read dependency lock: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("schema") != 1:
        raise DependencyIntegrityError("unsupported dependency lock schema")
    if not isinstance(lock.get("files"), dict) or not lock["files"]:
        raise DependencyIntegrityError("dependency lock has no payload files")
    archive_hash = lock.get("archive_sha256")
    if not isinstance(archive_hash, str) or len(archive_hash) != 64:
        raise DependencyIntegrityError("dependency lock has no archive SHA-256")
    for name, expected in lock["files"].items():
        if Path(name).name != name or not isinstance(expected, dict):
            raise DependencyIntegrityError(f"unsafe payload name: {name!r}")
        if not isinstance(expected.get("size"), int) or expected["size"] < 0:
            raise DependencyIntegrityError(f"invalid size for {name}")
        digest = expected.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise DependencyIntegrityError(f"invalid SHA-256 for {name}")
    return lock


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_payload_dir(directory: Path, lock: dict | None = None) -> None:
    """Reject missing, modified and extra payload files."""
    lock = lock or load_lock()
    directory = Path(directory)
    expected_names = set(lock["files"])
    if not directory.is_dir():
        raise DependencyIntegrityError(f"payload directory is missing: {directory}")
    actual_names = {item.name for item in directory.iterdir()}
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    if missing or extra:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("extra: " + ", ".join(extra))
        raise DependencyIntegrityError("payload allowlist mismatch (" + "; ".join(details) + ")")
    for name, expected in lock["files"].items():
        path = directory / name
        if not path.is_file():
            raise DependencyIntegrityError(f"payload is not a regular file: {name}")
        if path.stat().st_size != expected["size"]:
            raise DependencyIntegrityError(f"payload size mismatch: {name}")
        if _sha256_file(path).lower() != expected["sha256"].lower():
            raise DependencyIntegrityError(f"payload SHA-256 mismatch: {name}")


def _get(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "NeedZeroXtraTrash dependency fetcher"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_ARCHIVE_BYTES:
            raise DependencyIntegrityError("dependency archive exceeds size limit")
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise DependencyIntegrityError("dependency archive exceeds size limit")
    return data


def _extract_to_stage(blob: bytes, stage: Path, lock: dict) -> None:
    archive_digest = _sha256_bytes(blob)
    if archive_digest.lower() != lock["archive_sha256"].lower():
        raise DependencyIntegrityError(
            f"archive SHA-256 mismatch: expected {lock['archive_sha256']}, got {archive_digest}"
        )
    expected = lock["files"]
    found: dict[str, zipfile.ZipInfo] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for info in archive.infolist():
                name = Path(info.filename).name
                if name not in expected or info.is_dir():
                    continue
                if name in found:
                    raise DependencyIntegrityError(f"duplicate payload in archive: {name}")
                if info.file_size != expected[name]["size"]:
                    raise DependencyIntegrityError(f"archive payload size mismatch: {name}")
                found[name] = info
            missing = sorted(set(expected) - set(found))
            if missing:
                raise DependencyIntegrityError(
                    "archive is missing payloads: " + ", ".join(missing)
                )
            stage.mkdir(parents=True)
            for name in sorted(expected):
                data = archive.read(found[name])
                if _sha256_bytes(data).lower() != expected[name]["sha256"].lower():
                    raise DependencyIntegrityError(f"archive payload SHA-256 mismatch: {name}")
                (stage / name).write_bytes(data)
    except zipfile.BadZipFile as exc:
        raise DependencyIntegrityError("dependency archive is not a valid ZIP") from exc
    verify_payload_dir(stage, lock)


def install_archive(blob: bytes, output: Path, lock: dict | None = None) -> None:
    """Verify completely, then atomically replace the payload directory."""
    lock = lock or load_lock()
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    backup = output.with_name(f"{output.name}.previous-{uuid.uuid4().hex}")
    with tempfile.TemporaryDirectory(prefix="lhm-stage-", dir=output.parent) as temp:
        stage = Path(temp) / "payload"
        _extract_to_stage(blob, stage, lock)
        had_previous = output.exists()
        if had_previous:
            os.replace(output, backup)
        try:
            os.replace(stage, output)
        except Exception:
            if had_previous and backup.exists() and not output.exists():
                os.replace(backup, output)
            raise
    if backup.exists():
        shutil.rmtree(backup)
    verify_payload_dir(output, lock)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--archive", type=Path, help="use an already downloaded archive")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        lock = load_lock()
        if args.verify_only:
            verify_payload_dir(args.output, lock)
            print(f"Verified {len(lock['files'])} pinned payloads in {args.output}")
            return 0
        try:
            verify_payload_dir(args.output, lock)
            print(f"Pinned payload already verified in {args.output}")
            return 0
        except DependencyIntegrityError:
            pass
        if args.archive:
            blob = args.archive.read_bytes()
        else:
            print(f"Downloading pinned LibreHardwareMonitor {lock['version']} ...")
            blob = _get(lock["url"])
        install_archive(blob, args.output, lock)
        print(f"Installed and verified {len(lock['files'])} payloads in {args.output}")
        return 0
    except (DependencyIntegrityError, OSError, ValueError) as exc:
        print(f"Dependency verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
