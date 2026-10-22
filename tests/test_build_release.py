"""Executable regression tests for the locked Windows build pipeline."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_release as build


class BuildReleaseTests(unittest.TestCase):
    def test_exact_python_patch_is_required(self):
        build._check_python(
            os_name="nt", platform_name="win32", version_info=(3, 12, 14),
            implementation="CPython", pointer_size=8, machine="AMD64",
        )
        with self.assertRaisesRegex(build.BuildError, "exactly CPython 3.12.14"):
            build._check_python(
                os_name="nt", platform_name="win32", version_info=(3, 12, 12),
                implementation="CPython", pointer_size=8, machine="AMD64",
            )

    def test_source_manifest_ignores_interpreter_and_editor_debris(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "src" / "needzero"
            cache = package / "__pycache__"
            cache.mkdir(parents=True)
            (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "draft.tmp").write_text("ignore", encoding="utf-8")
            (cache / "module.pyc").write_bytes(b"ignore")
            manifest = build._source_manifest(root)
            self.assertEqual(list(manifest), ["src/needzero/module.py"])

    def test_actual_payload_digest_detects_injected_entry(self):
        baseline = {
            "schema": 1, "format": "pyinstaller-carchive-pyz",
            "options": [], "entries": [], "embedded_build_info": {
                "schema": 1, "version": "3.1", "channel": "dev",
            },
        }
        injected = json.loads(json.dumps(baseline))
        injected["entries"].append({
            "name": "unexpected.dll", "type": "b", "size": 1,
            "sha256": hashlib.sha256(b"x").hexdigest(), "compressed": False,
        })
        self.assertNotEqual(
            build._inventory_digest(baseline), build._inventory_digest(injected)
        )
        with self.assertRaisesRegex(build.BuildError, "added="):
            build.compare_payload_inventories(baseline, injected)

    def test_pair_publication_failure_restores_previous_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged_exe = root / "stage" / "new.exe"
            staged_manifest = root / "stage" / "new.manifest.json"
            destination = root / "dist" / "app.exe"
            destination_manifest = root / "dist" / "app.exe.manifest.json"
            staged_exe.parent.mkdir()
            destination.parent.mkdir()
            staged_exe.write_bytes(b"new executable")
            staged_manifest.write_text(json.dumps({
                "artifact": destination.name,
                "artifact_sha256": hashlib.sha256(b"new executable").hexdigest(),
            }), encoding="utf-8")
            destination.write_bytes(b"old executable")
            destination_manifest.write_bytes(b"old manifest")

            def fail_second_publish(source, target):
                if Path(source) == staged_manifest and Path(target) == destination_manifest:
                    raise OSError("simulated second publication failure")
                os.replace(source, target)

            with self.assertRaisesRegex(build.BuildError, "rolled back"):
                build._publish_pair(
                    staged_exe, staged_manifest, destination,
                    destination_manifest, replace_fn=fail_second_publish,
                )
            self.assertEqual(destination.read_bytes(), b"old executable")
            self.assertEqual(destination_manifest.read_bytes(), b"old manifest")
            self.assertFalse(list((root / "dist").glob(".*.rollback-*")))

    def test_post_publish_hash_failure_also_rolls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged_exe = root / "stage" / "new.exe"
            staged_manifest = root / "stage" / "new.manifest.json"
            destination = root / "dist" / "app.exe"
            destination_manifest = root / "dist" / "app.exe.manifest.json"
            staged_exe.parent.mkdir()
            destination.parent.mkdir()
            staged_exe.write_bytes(b"new executable")
            staged_manifest.write_text(json.dumps({
                "artifact": destination.name, "artifact_sha256": "0" * 64,
            }), encoding="utf-8")
            destination.write_bytes(b"old executable")
            destination_manifest.write_bytes(b"old manifest")
            with self.assertRaisesRegex(build.BuildError, "rolled back"):
                build._publish_pair(
                    staged_exe, staged_manifest, destination, destination_manifest
                )
            self.assertEqual(destination.read_bytes(), b"old executable")
            self.assertEqual(destination_manifest.read_bytes(), b"old manifest")

    def test_release_signing_preflight_precedes_any_clean(self):
        with mock.patch.object(build, "_check_python"), mock.patch.object(
            build, "validate_locks", return_value=({}, {}, {})
        ), mock.patch.object(
            build, "release_signing_preflight",
            side_effect=build.BuildError("certificate missing"),
        ), mock.patch.object(build, "_safe_clean") as clean:
            with self.assertRaisesRegex(build.BuildError, "certificate missing"):
                build.build("release")
            clean.assert_not_called()

    def test_local_signing_fallback_requires_explicit_opt_in(self):
        thumbprint = "A" * 40
        with mock.patch.dict(
            os.environ, {"NEEDZERO_SIGN_SHA1": thumbprint}, clear=False
        ), mock.patch.object(
            build, "_signtool", return_value=None
        ), mock.patch.object(
            build, "_signing_certificate_has_private_key", return_value=True
        ):
            os.environ.pop("NEEDZERO_LOCAL_SIGNING", None)
            with self.assertRaisesRegex(build.BuildError, "signtool"):
                build.release_signing_preflight()

    def test_default_build_uses_audited_local_wheelhouses(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEEDZERO_WHEELHOUSE", None)
            arguments = build._locked_package_source()
        self.assertIn("--no-index", arguments)
        self.assertIn(str(build.BOOTSTRAP_WHEELHOUSE), arguments)
        self.assertIn(str(build.RUNTIME_WHEELHOUSE), arguments)


if __name__ == "__main__":
    unittest.main(verbosity=2)
