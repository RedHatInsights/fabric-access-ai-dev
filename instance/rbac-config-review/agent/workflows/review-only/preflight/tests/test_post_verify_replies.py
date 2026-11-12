"""Verification acknowledgements follow the updated status review exactly once."""

import sys
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

import post_verify_replies as replies  # noqa: E402


REPO, PR, REVIEW_ID = "org/repo", 42, 73
HEAD, BASE = "a" * 40, "b" * 40
COMMAND_IDS = [101, 102]


def request(ident):
    return {
        "id": ident, "body": "/review-bot verify",
        "created_at": "2026-11-12T13:00:00Z", "updated_at": "2026-11-12T13:00:00Z",
        "user": {"type": "User", "login": "reviewer"},
    }


def status_body(ids=COMMAND_IDS):
    links = " ".join(
        f"[request #{ident}](https://github.com/{REPO}/pull/{PR}#issuecomment-{ident})"
        for ident in ids
    )
    return (
        f"## Review 1\n\nSummary.\n\n**Last verification:** 2026-11-12 13:00:00 UTC; "
        f"requested by {links}.\n\n"
        f"<!-- dev-review-bot:fabric-access-review:{HEAD}:{BASE} -->"
    )


def fake_api(comments, posts, ids=COMMAND_IDS):
    def api(endpoint, method="GET", payload=None, paginate=False):
        if endpoint == f"repos/{REPO}/pulls/{PR}":
            return {"state": "open", "head": {"sha": HEAD}, "base": {"sha": BASE}}
        if endpoint == "user":
            return {"login": "review-bot"}
        if endpoint == f"repos/{REPO}/pulls/{PR}/reviews/{REVIEW_ID}":
            return {"state": "COMMENTED", "user": {"login": "review-bot"}, "body": status_body(ids)}
        if endpoint == f"repos/{REPO}/issues/{PR}/comments?per_page=100" and paginate:
            return comments
        if endpoint == f"repos/{REPO}/issues/{PR}/comments" and method == "POST":
            posts.append(payload["body"])
            created = {"id": 200 + len(posts), "body": payload["body"],
                       "user": {"login": "review-bot"}}
            comments.append(created)
            return created
        raise AssertionError((endpoint, method, payload, paginate))
    return api


def test_each_command_gets_one_linked_reply_even_after_retry(monkeypatch):
    comments = [request(ident) for ident in COMMAND_IDS]
    posts = []
    monkeypatch.setattr(replies, "api", fake_api(comments, posts))

    assert replies.publish(REPO, PR, HEAD, BASE, REVIEW_ID, COMMAND_IDS) == {
        "posted": 2, "unchanged": 0,
    }
    assert replies.publish(REPO, PR, HEAD, BASE, REVIEW_ID, COMMAND_IDS) == {
        "posted": 0, "unchanged": 2,
    }
    assert len(posts) == 2
    for ident, body in zip(COMMAND_IDS, posts):
        assert f"#issuecomment-{ident}" in body
        assert f"#pullrequestreview-{REVIEW_ID}" in body
        assert f"<!-- dev-review-bot:verify-response:{ident} -->" in body


def test_reply_requires_status_update_for_every_command(monkeypatch):
    posts = []
    monkeypatch.setattr(replies, "api", fake_api(
        [request(ident) for ident in COMMAND_IDS], posts, ids=[COMMAND_IDS[0]]
    ))
    with pytest.raises(RuntimeError, match="does not confirm"):
        replies.publish(REPO, PR, HEAD, BASE, REVIEW_ID, COMMAND_IDS)
    assert posts == []


def test_edited_request_is_not_acknowledged(monkeypatch):
    comments = [request(COMMAND_IDS[0])]
    comments[0]["updated_at"] = "2026-11-12T13:01:00Z"
    posts = []
    monkeypatch.setattr(replies, "api", fake_api(comments, posts, ids=[COMMAND_IDS[0]]))
    with pytest.raises(RuntimeError, match="missing or was edited"):
        replies.publish(REPO, PR, HEAD, BASE, REVIEW_ID, [COMMAND_IDS[0]])
    assert posts == []
