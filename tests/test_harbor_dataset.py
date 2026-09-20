import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

TASKS = Path(__file__).resolve().parents[1] / "evaluation" / "harbor" / "tasks"


@pytest.mark.parametrize(
    "task_name,source_name",
    [
        ("task-list", "todo.py"),
        ("expense-totals", "expenses.py"),
    ],
)
@pytest.mark.parametrize("oracle", [True, False])
def test_harbor_verifiers_reward_oracles_and_reject_broken_implementations(
    tmp_path,
    task_name,
    source_name,
    oracle,
):
    task = TASKS / task_name
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["environment"]["network_mode"] == "no-network"
    assert config["metadata"]["expected_capabilities"] and config["metadata"]["forbidden_behaviors"]
    app = tmp_path / "app"
    app.mkdir()
    if oracle:
        shutil.copyfile(task / "solution" / source_name, app / source_name)
    else:
        (app / source_name).write_text("print('{}')\n")
    reward = tmp_path / "reward"
    subprocess.run(
        ["bash", str(task / "tests" / "test.sh")],
        check=True,
        timeout=15,
        env={
            "PATH": f"{Path(sys.executable).parent}:{os.defpath}",
            "AIDLC_TASK_APP": str(app),
            "AIDLC_TASK_TESTS": str(task / "tests"),
            "AIDLC_REWARD_DIR": str(reward),
        },
        cwd=app,
    )
    assert (reward / "reward.txt").read_text().strip() == ("1" if oracle else "0"), (
        reward / "test-output.txt"
    ).read_text()
