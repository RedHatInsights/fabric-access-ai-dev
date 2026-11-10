"""Regression coverage for idempotent inline comment publishing."""

import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

import post_inline_comments as publisher  # noqa: E402


def test_existing_finding_on_later_page_is_not_reposted(tmp_path, monkeypatch, capsys):
    head = "a" * 40
    body = (
        "**[F1]** Claim\n\n**Dimension:** correctness\n\n**Location:** file.py:3\n\n**Trigger:** Input\n\n"
        "**Impact:** Failure\n\n**Evidence:** Source\n\n**Suggested fix:** Fix\n\n"
        "<!-- dev-review-bot:fabric-access-finding:F1 -->"
    )
    comment = {"id": "F1", "path": "file.py", "line": 3, "side": "RIGHT", "body": body}
    comments_file = tmp_path / "comments.json"
    comments_file.write_text(json.dumps([comment]))
    existing = {**comment, "id": 42, "commit_id": head}
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[-2:] == ["--jq", ".head.sha"]:
            return CompletedProcess(command, 0, stdout=head + "\n", stderr="")
        assert command[-2:] == ["--paginate", "--slurp"]
        return CompletedProcess(command, 0, stdout=json.dumps([[], [existing]]), stderr="")

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "post_inline_comments.py", "--repo", "org/repo", "--pr", "7",
        "--head-sha", head, "--comments", str(comments_file),
    ])

    publisher.main()

    assert json.loads(capsys.readouterr().out) == {"posted": 0, "updated": 0, "unchanged": 1}
    assert len(calls) == 2


def test_skill_formatted_inline_comment_can_be_published(tmp_path, monkeypatch, capsys):
    head = "a" * 40
    body = "🟠 [F1] Keep the alias.\n\n**Dimension:** correctness\n\n<!-- dev-review-bot:fabric-access-finding:F1 -->"
    comments_file = tmp_path / "comments.json"
    comments_file.write_text(json.dumps([{"id": "F1", "path": "file.py", "line": 3,
                                          "side": "RIGHT", "format": "custom", "body": body}]))
    calls = []
    monkeypatch.setattr(publisher, "current_head", lambda *_args: head)

    def fake_api(endpoint, method, payload=None):
        calls.append((method, payload))
        return [] if method == "GET" else {"id": 123}

    monkeypatch.setattr(publisher, "api", fake_api)
    monkeypatch.setattr(sys, "argv", ["post_inline_comments.py", "--repo", "org/repo", "--pr", "7",
                                      "--head-sha", head, "--comments", str(comments_file)])
    publisher.main()
    assert json.loads(capsys.readouterr().out)["posted"] == 1
    assert calls[-1][1]["body"] == body
