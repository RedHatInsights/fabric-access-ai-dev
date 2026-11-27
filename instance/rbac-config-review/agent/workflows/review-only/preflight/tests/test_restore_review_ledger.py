"""Run the restoration CLI with the real codec and mocked remote reads."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import restore_review_ledger as restore  # noqa: E402
from review_ledger import append_to_body  # noqa: E402


REPO = "org/rbac"
PR = 7
REVIEW = 18
TASK = f"review-only:github:{REPO}#{PR}"
HEAD, BASE, OLD = "a" * 40, "b" * 40, "c" * 40
PR_ENDPOINT = f"repos/{REPO}/pulls/{PR}"
REVIEW_ENDPOINT = f"{PR_ENDPOINT}/reviews/{REVIEW}"
FINDINGS = [{"id": "F1", "status": "addressed", "severity": "nit", "source_dimensions": []}]


def body(head=HEAD, base=BASE, count=2):
    return append_to_body(
        f"## Review {count + 1}\n\nAdjust defaults.\n\n"
        f"<!-- dev-review-bot:fabric-access-review:{head}:{base} -->",
        REPO, PR, head, base, count, FINDINGS,
    )


@pytest.fixture
def runner(monkeypatch, tmp_path):
    review = {"state": "COMMENTED", "user": {"login": "review-bot"}, "body": body(OLD, OLD)}
    live = {"state": "open", "head": {"sha": HEAD}, "base": {"sha": BASE}}
    responses = {PR_ENDPOINT: live, REVIEW_ENDPOINT: review, f"{REVIEW_ENDPOINT}/comments": [[]]}
    commands = []

    def gh(command, **_kwargs):
        commands.append(command)
        assert command[:2] == ["gh", "api"]
        assert command[3:] == (["--paginate", "--slurp"] if command[2].endswith("/comments") else [])
        return subprocess.CompletedProcess(command, 0, json.dumps(responses[command[2]]), "")

    pending = {
        "repo": REPO, "pr": PR, "head_sha": HEAD, "base_sha": BASE, "operation": "review",
        "publication_body": body(), "publication_json": {"body": body()}, "update_count": 2,
    }
    task = {"external_key": TASK, "source_type": "github", "instance_id": "review-instance",
            "status": "in_progress", "metadata": {"review_id": REVIEW, "pending_publication": pending}}
    memory_call, cleanup = Mock(return_value=task), Mock()
    monkeypatch.setattr(restore, "resolve_memory_tools", lambda: (memory_call, cleanup))
    monkeypatch.setattr(subprocess, "run", gh)
    monkeypatch.setenv("GH_USER_NAME", "Review-Bot")
    monkeypatch.setenv("BOT_INSTANCE_ID", "review-instance")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["restore_review_ledger.py", "--repo", REPO, "--pr", str(PR),
                                     "--head-sha", HEAD, "--base-sha", BASE, "--review-id", str(REVIEW),
                                     "--task-key", TASK])
    return task, responses, commands, memory_call, cleanup


def test_restores_tracked_historical_ledger_read_only(runner, capsys):
    _, _, commands, memory_call, cleanup = runner
    restore.main()
    output = json.loads(capsys.readouterr().out)
    assert output == {"ledger_path": output["ledger_path"], "source": "review", "update_count": 2}
    path = Path(output["ledger_path"])
    assert path.is_absolute() and path.parent.name == "restored-ledgers"
    assert json.loads(path.read_text()) == FINDINGS
    assert [command[2] for command in commands] == [PR_ENDPOINT, REVIEW_ENDPOINT, PR_ENDPOINT]
    memory_call.assert_called_once_with("task_get", {"external_key": TASK, "source_type": "github"})
    cleanup.assert_called_once_with()


def test_restores_validated_body_for_pending_retry(runner, capsys):
    _, responses, commands, _, _ = runner
    responses[REVIEW_ENDPOINT].update(state="PENDING", body="", commit_id=HEAD)
    sys.argv.append("--pending")
    restore.main()
    output = json.loads(capsys.readouterr().out)
    assert output["source"] == "pending"
    assert json.loads(Path(output["ledger_path"]).read_text()) == FINDINGS
    assert f"{REVIEW_ENDPOINT}/comments" in [command[2] for command in commands]


@pytest.mark.parametrize("failure", ["wrong-task", "paused", "wrong-id", "wrong-author", "base-changed",
                                     "head-changed", "missing", "malformed", "wrong-repo", "pending-mismatch",
                                     "pending-body-mismatch", "pending-comments", "pending-stale-draft"])
def test_fails_closed_without_materializing_anything(runner, capsys, failure):
    task, responses, _, _, _ = runner
    review = responses[REVIEW_ENDPOINT]
    pending = task["metadata"]["pending_publication"]
    if failure == "wrong-task":
        task["instance_id"] = "other-instance"
    elif failure == "paused":
        task["status"] = "paused"
    elif failure == "wrong-id":
        task["metadata"]["review_id"] = REVIEW + 1
    elif failure == "wrong-author":
        review["user"]["login"] = "another-user"
        sys.argv.append("--pending")
    elif failure in {"base-changed", "head-changed"}:
        responses[PR_ENDPOINT][failure.split("-")[0]]["sha"] = OLD
    elif failure == "missing":
        review["body"] = "Legacy review; no ledger."
    elif failure == "malformed":
        review["body"] = "<!-- dev-review-bot:review-ledger:v1:! -->"
        sys.argv.append("--pending")
    elif failure == "wrong-repo":
        review["body"] = append_to_body(
            f"## Review 1\n\n<!-- dev-review-bot:fabric-access-review:{HEAD}:{BASE} -->",
            "other/repo", PR, HEAD, BASE, 0, [],
        )
    else:
        sys.argv.append("--pending")
        if failure == "pending-mismatch":
            pending["update_count"] = 3
        elif failure == "pending-body-mismatch":
            pending["publication_json"]["body"] = "different"
        else:
            review.update(state="PENDING", body="", commit_id=HEAD)
            if failure == "pending-comments":
                responses[f"{REVIEW_ENDPOINT}/comments"] = [[{"id": 1}]]
            else:
                review["commit_id"] = OLD

    with pytest.raises(SystemExit) as exc:
        restore.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out and "Ledger restoration blocked" in captured.err
    assert not Path("data").exists()


@pytest.mark.parametrize("failure", [None, "wrong-owner", "changed-head", "legacy"])
def test_selected_profile_invokes_helper_without_pythonpath(tmp_path, failure):
    home = tmp_path / "private" / "home"
    app = home / "app"
    profile_root = app / "data/remote-config" if failure == "legacy" else app
    workflow = profile_root / "instance/rbac-config-review/agent/workflows/review-only"
    shutil.copytree(Path(restore.__file__).parent, workflow)
    agent = workflow.parents[1]
    shutil.copy2(Path(__file__).resolve().parents[4] / "instance.yaml", agent / "instance.yaml")
    bot = app / "bot"
    bot.mkdir()
    (bot / "__init__.py").write_text("")
    repo = Path(__file__).resolve().parents[7]
    shutil.copy2(repo / "dev-bot/bot/coordinator_bridge.py", bot / "coordinator_bridge.py")
    task = {"external_key": TASK, "source_type": "github", "instance_id": (
        "other-instance" if failure == "wrong-owner" else "review-instance"
    ), "status": "pr_open", "metadata": {"review_id": REVIEW}}
    (bot / "memory_mcp.py").write_text(
        "import json\n"
        f"def memory_call(*args): return json.loads({json.dumps(json.dumps(task))})\n"
        "def memory_cleanup(): pass\n"
    )
    responses = {
        PR_ENDPOINT: {"state": "open", "head": {"sha": OLD if failure == "changed-head" else HEAD},
                      "base": {"sha": BASE}},
        REVIEW_ENDPOINT: {"state": "COMMENTED", "user": {"login": "review-bot"}, "body": body(OLD, OLD)},
    }
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"responses = json.loads({json.dumps(json.dumps(responses))})\n"
        "print(json.dumps(responses[sys.argv[2]]))\n"
    )
    gh.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_dir}:{os.defpath}", "REHOR_INPUT_SOURCE": "sandbox",
           "BOT_CONFIG_PATH": "instance/rbac-config-review", "BOT_INSTANCE_ID": "review-instance",
           "GH_USER_NAME": "review-bot"}
    if failure == "legacy":
        env.pop("REHOR_INPUT_SOURCE")
    else:
        selected = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; from bot.coordinator_bridge import _baked_profile; "
             "print(_baked_profile(Path.cwd())[0])"],
            cwd=app, env=env, capture_output=True, text=True, check=False,
        )
        assert selected.returncode == 0, selected.stderr
        assert Path(selected.stdout.strip()) == agent
        assert not (app / "data/remote-config").exists()
    context_help = subprocess.run(
        [sys.executable, str(workflow / "build_context.py"), "--help"],
        cwd=app, env=env, capture_output=True, text=True, check=False,
    )
    assert context_help.returncode == 0, context_help.stderr
    command = [sys.executable, str(workflow / "restore_review_ledger.py"), "--repo", REPO,
               "--pr", str(PR), "--head-sha", HEAD, "--base-sha", BASE,
               "--review-id", str(REVIEW), "--task-key", TASK]
    result = subprocess.run(command, cwd=app, env=env, capture_output=True, text=True, check=False)
    if failure in {"wrong-owner", "changed-head"}:
        assert result.returncode != 0
        assert "Ledger restoration blocked" in result.stderr
        assert not (app / "data").exists()
    else:
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert Path(output["ledger_path"]).is_relative_to(app / "data")
        assert json.loads(Path(output["ledger_path"]).read_text()) == FINDINGS
