"""Shared footer stays off the status review and updates bot comments idempotently."""

import sys
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

import post_review  # noqa: E402
import review_comment_footer as footer  # noqa: E402


REPO, PR, REVIEW_ID = "org/repo", 42, 73
URL = f"https://github.com/{REPO}/pull/{PR}#pullrequestreview-{REVIEW_ID}"


def test_footer_preserves_question_and_approval_next_steps():
    question = "Question\n\n<!-- dev-review-bot:missing-context:Q-123456789abc -->"
    approval = "Approve?\n\n<!-- dev-review-bot:review-approval:RA-123456789abc -->"
    question_body = footer.with_footer(question, REPO, PR)
    assert "Answer Q-123456789abc: ..." in question_body
    assert "Pending the first review" in question_body
    linked = footer.with_footer(question_body, REPO, PR, URL)
    assert linked.count(footer.START) == 1
    assert URL in linked
    assert footer.with_footer(linked, REPO, PR, URL) == linked
    assert "Review bot: yes RA-123456789abc" in footer.with_footer(approval, REPO, PR)


def test_footer_refuses_status_review():
    body = "## Review 1\n\n<!-- dev-review-bot:fabric-access-review:a:b -->"
    with pytest.raises(ValueError, match="status review"):
        footer.with_footer(body, REPO, PR, URL)


def test_sync_updates_issue_and_inline_comments_then_stops(monkeypatch):
    issue = {"id": 101, "body": "Response\n\n<!-- dev-review-bot:verify-response:88 -->",
             "user": {"login": "review-bot"}}
    inline = {"id": 102, "body": "Finding\n\n<!-- dev-review-bot:fabric-access-finding:F1 -->",
              "user": {"login": "review-bot"}}
    human = {"id": 103, "body": "<!-- dev-review-bot:fabric-access-finding:F2 -->",
             "user": {"login": "author"}}
    calls = []

    def api(endpoint, method="GET", payload=None, paginate=False):
        if endpoint == "user":
            return {"login": "review-bot"}
        if endpoint == f"repos/{REPO}/pulls/{PR}/reviews/{REVIEW_ID}":
            return {"state": "COMMENTED", "user": {"login": "review-bot"},
                    "body": "<!-- dev-review-bot:fabric-access-review:a:b -->", "html_url": URL}
        if endpoint == f"repos/{REPO}/issues/{PR}/comments?per_page=100":
            assert paginate
            return [issue, human]
        if endpoint == f"repos/{REPO}/pulls/{PR}/comments?per_page=100":
            assert paginate
            return [inline]
        if method == "PATCH":
            calls.append(endpoint)
            target = issue if endpoint.endswith("/101") else inline
            target["body"] = payload["body"]
            return target
        raise AssertionError((endpoint, method))

    monkeypatch.setattr(post_review, "api", api)
    assert footer.sync(REPO, PR, REVIEW_ID)["updated"] == 2
    assert footer.sync(REPO, PR, REVIEW_ID)["updated"] == 0
    assert len(calls) == 2
    assert URL in issue["body"] and URL in inline["body"]
    assert URL not in human["body"]


def test_sync_keeps_linked_footer_when_no_review_id(monkeypatch):
    comment = {"id": 101, "body": footer.with_footer(
        "Response\n\n<!-- dev-review-bot:verify-response:88 -->", REPO, PR, URL),
        "user": {"login": "review-bot"}}

    def api(endpoint, method="GET", payload=None, paginate=False):
        if endpoint == "user":
            return {"login": "review-bot"}
        if endpoint == f"repos/{REPO}/issues/{PR}/comments?per_page=100":
            return [comment]
        if endpoint == f"repos/{REPO}/pulls/{PR}/comments?per_page=100":
            return []
        raise AssertionError((endpoint, method))

    monkeypatch.setattr(post_review, "api", api)
    assert footer.sync(REPO, PR)["unchanged"] == 1
