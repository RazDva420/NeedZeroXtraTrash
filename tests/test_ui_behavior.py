from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_real_ui_javascript_recovers_and_never_reports_false_save_success():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the executable UI behavior smoke test")
    completed = subprocess.run(
        [node, str(ROOT / "tests" / "ui_behavior_smoke.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
