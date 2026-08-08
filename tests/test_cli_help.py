import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_EXAMPLE = "checkpoints/main/000007-master_candidate_selection.json"


@pytest.mark.parametrize("command", ["rewind", "branch"])
def test_rewind_and_branch_help_require_project_relative_checkpoint_path(command: str):
    result = subprocess.run(
        [sys.executable, "main.py", command, "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "工程内 checkpoint 的完整相对路径" in result.stdout
    assert CHECKPOINT_EXAMPLE in result.stdout
