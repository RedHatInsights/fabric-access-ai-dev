"""Summary review publication uses a validated JSON body and stable review ID."""

import json
import sys
from pathlib import Path

import pytest
from subprocess import CompletedProcess

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

import post_review  # noqa: E402


HEAD, BASE = "a" * 40, "b" * 40
REPO, PR = "org/repo", 42
BODY = f"## Review #1\n\n<!-- dev-review-bot:fabric-access-review:{HEAD}:{BASE} -->\n"


def publication(tmp_path):
    path = tmp_path / "publication.json"
    path.write_text(json.dumps({"body": BODY}))
    return path


def test_first_review_posts_json_and_retry_recovers_same_review(tmp_path, monkeypatch):
    calls = []
    existing = []

    def api(endpoint, method="GET", payload=None, paginate=False):
        calls.append((endpoint, method, payload, paginate))
        if endpoint == f"repos/{REPO}/pulls/{PR}":
            return {"state": "open", "head": {"sha": HEAD}, "base": {"sha": BASE}}
        if endpoint == "user":
            return {"login": "review-bot"}
        if paginate:
            return existing
        if method == "POST":
            assert payload == {"commit_id": HEAD, "event": "COMMENT", "body": BODY}
            created = {"id": 73, "state": "COMMENTED", "html_url": "https://github.com/org/repo/pull/42#pullrequestreview-73",
                       "user": {"login": "review-bot"}, "body": BODY}
            existing.append(created)
            return created
        raise AssertionError((endpoint, method))

    monkeypatch.setattr(post_review, "api", api)
    result = post_review.publish(REPO, PR, HEAD, BASE, publication(tmp_path))
    assert result["action"] == "posted"
    assert result["review_id"] == 73
    again = post_review.publish(REPO, PR, HEAD, BASE, publication(tmp_path))
    assert again["action"] == "recovered"
    assert again["review_id"] == 73
    assert len([call for call in calls if call[1] == "POST"]) == 1
    assert not list(tmp_path.glob("*.md"))


def test_existing_review_is_updated_in_place(tmp_path, monkeypatch):
    calls = []

    def api(endpoint, method="GET", payload=None, paginate=False):
        calls.append((endpoint, method, payload))
        if method == "GET":
            return {"state": "open", "head": {"sha": HEAD}, "base": {"sha": BASE}}
        return {"id": 73, "html_url": "https://github.com/org/repo/pull/42#pullrequestreview-73"}

    monkeypatch.setattr(post_review, "api", api)
    result = post_review.publish(REPO, PR, HEAD, BASE, publication(tmp_path), review_id=73)
    assert result["action"] == "updated"
    assert calls[-1] == (f"repos/{REPO}/pulls/{PR}/reviews/73", "PUT", {"body": BODY})


def test_changed_pr_and_duplicate_reviews_fail_closed(tmp_path, monkeypatch):
    path = publication(tmp_path)
    monkeypatch.setattr(post_review, "api", lambda *_args, **_kwargs: {
        "state": "open", "head": {"sha": "c" * 40}, "base": {"sha": BASE}
    })
    with pytest.raises(RuntimeError, match="head or base changed"):
        post_review.publish(REPO, PR, HEAD, BASE, path)

    def duplicate_api(endpoint, method="GET", payload=None, paginate=False):
        if endpoint == f"repos/{REPO}/pulls/{PR}":
            return {"state": "open", "head": {"sha": HEAD}, "base": {"sha": BASE}}
        if endpoint == "user":
            return {"login": "review-bot"}
        return [{"id": ident, "body": BODY, "user": {"login": "review-bot"}} for ident in (1, 2)]

    monkeypatch.setattr(post_review, "api", duplicate_api)
    with pytest.raises(RuntimeError, match="Multiple bot reviews"):
        post_review.publish(REPO, PR, HEAD, BASE, path)


def test_github_api_receives_payload_as_fields_without_a_tempfile(monkeypatch):
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return CompletedProcess(command, 0, stdout='{"id":73}', stderr="")

    monkeypatch.setattr(post_review.subprocess, "run", fake_run)
    assert post_review.api(f"repos/{REPO}/pulls/{PR}/reviews", "POST",
                           {"event": "COMMENT", "body": BODY}) == {"id": 73}
    assert captured["command"] == [
        "gh", "api", f"repos/{REPO}/pulls/{PR}/reviews", "--method", "POST",
        "--field", "event=COMMENT", "--field", f"body={BODY}",
    ]
