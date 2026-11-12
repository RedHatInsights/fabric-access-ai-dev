"""Resolve per-repository Jira creation settings from deployment environment."""

from __future__ import annotations

import argparse
import json
import os
import re

from review_config import ReviewConfigError, environment_variable_name, load_config

_TEAM_FIELD = re.compile(r"^customfield_[0-9]+$")


def resolve(repo_key: str, target_repository: str,
            environ: dict[str, str] | None = None) -> dict:
    """Resolve the Jira profile assigned to a target repository."""
    env = os.environ if environ is None else environ
    config = load_config()
    repositories = config.get("repositories")
    context = repositories.get(repo_key) if isinstance(repositories, dict) else None
    jira = context.get("jira") if isinstance(context, dict) else None
    if not isinstance(jira, dict):
        raise ReviewConfigError(f"Jira ticket creation is not configured for {repo_key}")
    profiles = jira.get("profiles")
    if not isinstance(profiles, dict):
        raise ReviewConfigError(f"Jira profiles are not configured for {repo_key}")
    profile = next((item for item in profiles.values()
                    if isinstance(item, dict) and item.get("target_repository") == target_repository), None)
    if profile is None:
        raise ReviewConfigError(f"No Jira team profile is configured for {target_repository}")

    def required(field: str) -> str:
        name = environment_variable_name(profile.get(field))
        if name is None:
            raise ReviewConfigError(f"Invalid Jira environment reference for {repo_key}: {field}")
        value = env.get(name, "").strip()
        if not value:
            raise ReviewConfigError(f"Required Jira setting is unavailable for {repo_key}: {name}")
        return value

    project = required("project_env")
    team_id = required("team_id_env")
    labels_name = environment_variable_name(profile.get("labels_env"))
    if labels_name is None:
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
    labels = [label.strip() for label in labels]
    if len(labels) != len(set(labels)):
        raise ReviewConfigError("Jira labels must be unique after trimming whitespace")
    team_field = jira.get("team_field")
    if not isinstance(team_field, str) or not _TEAM_FIELD.fullmatch(team_field):
        raise ReviewConfigError(f"Invalid Jira Team field for {repo_key}")
    return {
        "repo_key": repo_key,
        "target_repository": target_repository,
        "project": project,
        "team_field": team_field,
        "team_id": team_id,
        "team_name": profile["team_name"],
        "labels": labels,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-key", required=True)
    parser.add_argument("--target-repository", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(resolve(args.repo_key, args.target_repository)))
    except ReviewConfigError as exc:
        parser.exit(1, f"Cannot prepare Jira creation: {exc}\n")


if __name__ == "__main__":
    main()
