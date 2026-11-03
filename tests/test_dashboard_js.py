"""Runs the jsdom dashboard tests (tests/js) when Node and the JS deps are available."""
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None or not (JS / "node_modules" / "jsdom").exists(),
                    reason="needs node + `cd tests/js && npm install`")
def test_dashboard_javascript():
    r = subprocess.run(["node", "--test"], cwd=JS, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
