import shutil
import subprocess
from pathlib import Path

import pytest


def test_slow_network_does_not_block_local_controls_or_dialogs():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/ui_responsiveness_harness.cjs"), str(root / "web/index.html")],
        capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
