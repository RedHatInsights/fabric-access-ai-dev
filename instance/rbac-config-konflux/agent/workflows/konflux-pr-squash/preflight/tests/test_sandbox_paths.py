"""Check the documented workflow script path from a private checkout."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_private_konflux_script_resolves_from_repo_checkout(tmp_path):
    home = tmp_path / "home"
    app = home / "app"
    workflow = app / "instance/rbac-config-konflux/agent/workflows/konflux-pr-squash"
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "skills", workflow / "skills")
    checkout = app / "repos/demo"
    checkout.mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(workflow / "skills/konflux-pr-squash.py"), "--help"],
        cwd=checkout, env={"HOME": str(home), "PATH": os.defpath, "REHOR_INPUT_SOURCE": "sandbox"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--repo" in result.stdout
