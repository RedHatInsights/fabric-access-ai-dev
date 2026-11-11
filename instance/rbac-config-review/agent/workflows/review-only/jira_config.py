"""Resolve per-repository Jira creation settings from deployment environment."""

from __future__ import annotations

import argparse
import json
import os
import re

from review_config import ReviewConfigError, load_config

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def resolve(repo_key: str, environ: dict[str, str] | None = None) -> dict:
    """Return configured Jira destination details without logging their values."""
    env = os.environ if environ is None else environ
    config = load_config()
    repositories = config.get("repositories")
    context = repositories.get(repo_key) if isinstance(repositories, dict) else None
    settings = context.get("jira") if isinstance(context, dict) else None
    if not isinstance(settings, dict):
        raise ReviewConfigError(f"Jira ticket creation is not configured for {repo_key}")

    def required(field: str) -> str:
        name = settings.get(field)
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ReviewConfigError(f"Invalid Jira environment reference for {repo_key}: {field}")
        value = env.get(name, "").strip()
        if not value:
            raise ReviewConfigError(f"Required Jira setting is unavailable for {repo_key}: {name}")
        return value

    project = required("project_env")
    team_id = required("team_id_env")
    labels_name = settings.get("labels_env")
    if not isinstance(labels_name, str) or not _ENV_NAME.fullmatch(labels_name):
        raise ReviewConfigError(f"Invalid Jira environment reference for {repo_key}: labels_env")
    labels_text = env.get(labels_name, "[]").strip() or "[]"
    try:
        labels = json.loads(labels_text)
    except json.JSONDecodeError as exc:
        raise ReviewConfigError("Jira labels must be a JSON array of strings") from exc
    if (not isinstance(labels, list)
            or any(not isinstance(label, str) or not label.strip() for label in labels)
            or len(labels) != len(set(labels))):
        raise ReviewConfigError("Jira labels must be a unique JSON array of non-empty strings")
    return {"repo_key": repo_key, "project": project, "team_id": team_id, "labels": labels}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-key", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(resolve(args.repo_key)))
    except ReviewConfigError as exc:
        parser.exit(1, f"Cannot prepare Jira creation: {exc}\n")


if __name__ == "__main__":
    main()
