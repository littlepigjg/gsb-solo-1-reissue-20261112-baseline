"""Runs the viewer logic smoke test when Node.js is available."""
import shutil
import subprocess
from pathlib import Path
import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js 不可用")
def test_viewer_logic_smoke():
    script = Path(__file__).parent / "viewer_smoke.mjs"
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
