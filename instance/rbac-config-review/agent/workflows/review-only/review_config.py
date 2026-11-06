"""Validate the review queue, related repositories, and direct Markdown links."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

AGENT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = AGENT_DIR / "review-context.yaml"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


class ReviewConfigError(ValueError):
    pass


def github_repo_url(url: str) -> str:
    """Return owner/repo from an HTTPS GitHub repository URL."""
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or any(not _REPO_PART.fullmatch(part) for part in parts)
        or parts[1].removesuffix(".git") in ("", ".", "..")
    ):
        raise ReviewConfigError(f"Expected a plain GitHub repository URL: {url}")
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def github_markdown_url(url: str) -> tuple[str, str, str]:
    """Return (owner/repo, ref, path) for a direct GitHub Markdown file link.

    A branch containing "/" is ambiguous in a blob URL; use a commit permalink
    for that case.
    """
    parsed = urlsplit(url)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) < 5
        or parts[2] != "blob"
        or not _REPO_PART.fullmatch(parts[0])
        or not _REPO_PART.fullmatch(parts[1])
        or not _REPO_PART.fullmatch(parts[3])
        or parts[3] in (".", "..")
        or any("/" in part or "\\" in part for part in parts)
        or any(part in ("", ".", "..") for part in parts[4:])
        or not parts[-1].lower().endswith(".md")
    ):
        raise ReviewConfigError(f"Expected a direct GitHub .md file URL: {url}")
    return f"{parts[0]}/{parts[1]}", parts[3], "/".join(parts[4:])


def _entries(value: object, field: str) -> list[dict]:
    if not isinstance(value, list):
        raise ReviewConfigError(f"{field} must be a list")
    entries: list[dict] = []
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ReviewConfigError(f"{field} entries must be objects")
        ident = item.get("id")
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise ReviewConfigError(f"{field} entry id must be lowercase letters, digits, or hyphens")
        if ident in ids:
            raise ReviewConfigError(f"Duplicate {field} id: {ident}")
        ids.add(ident)
        entries.append(item)
    return entries


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.is_file():
        raise ReviewConfigError(f"Missing review context config: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ReviewConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewConfigError("review-context.yaml must be a mapping")
    label = data.get("review_label")
    if not isinstance(label, str) or not label.strip():
        raise ReviewConfigError("review_label must be a non-empty GitHub label")
    repositories = data.get("repositories")
    if not isinstance(repositories, dict):
        raise ReviewConfigError("repositories must be a mapping")
    for repo_key, context in repositories.items():
        if not isinstance(repo_key, str) or not repo_key:
            raise ReviewConfigError("repository keys must be non-empty strings")
        if not isinstance(context, dict):
            raise ReviewConfigError(f"repositories.{repo_key} must be a mapping")
        for related in _entries(context.get("related", []), f"{repo_key}.related"):
            url = related.get("url")
            relationship = related.get("relationship")
            if not isinstance(url, str):
                raise ReviewConfigError(f"{repo_key}.related.{related['id']} needs a URL")
            github_repo_url(url)
            if not isinstance(relationship, str) or not relationship.strip():
                raise ReviewConfigError(f"{repo_key}.related.{related['id']} needs a relationship")
        for doc in _entries(context.get("docs", []), f"{repo_key}.docs"):
            url = doc.get("url")
            if not isinstance(url, str):
                raise ReviewConfigError(f"{repo_key}.docs.{doc['id']} needs a URL")
            github_markdown_url(url)
    return data
