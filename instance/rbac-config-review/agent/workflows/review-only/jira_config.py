"""Resolve per-repository Jira creation settings from deployment environment.

Resolution is consumed only in-process by the trusted `jira_create.py`
helper. This module has no CLI: printing resolved settings would expose
Vault-backed values in tool output and transcripts.
"""

from __future__ import annotations

import json
import os
import re

from review_config import ReviewConfigError, environment_variable_name, load_config

_TEAM_FIELD = re.compile(r"^customfield_[0-9]+$")

# Sandbox deliveries use nonreserved FABRIC_ names (Vault-sourced via the
# dispatcher's sandbox.env secret rendering); legacy deployments still supply
# the reserved BOT_ names through app-interface parameters. In the sandbox a
# setting configured from the Secret must arrive under its configured name:
# a missing delivery fails loudly instead of silently using a legacy value.
# Outside the sandbox the legacy BOT_ alias stays the fallback until cutover.
_SANDBOX_PREFIX = "FABRIC_"
_LEGACY_PREFIX = "BOT_"


def _sandbox_mode(env: dict[str, str]) -> bool:
    """True when the dispatcher runs us inside a sandbox."""
    return env.get("REHOR_INPUT_SOURCE", "").strip() == "sandbox"


def _lookup(env: dict[str, str], name: str) -> str:
    """Resolve one configured reference, honoring the delivery mode.

    Sandbox: only the configured name, never a legacy alias or default.
    Legacy: fall back to the BOT_ alias app-interface still supplies.
    """
    value = env.get(name, "").strip()
    if value:
        return value
    if _sandbox_mode(env):
        raise ReviewConfigError(f"Sandbox Jira setting is missing: {name}")
    if name.startswith(_SANDBOX_PREFIX):
        legacy = _LEGACY_PREFIX + name.removeprefix(_SANDBOX_PREFIX)
        return env.get(legacy, "").strip()
    return ""


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
        value = _lookup(env, name)
        if not value:
            raise ReviewConfigError(f"Required Jira setting is unavailable for {repo_key}: {name}")
        return value

    project = required("project_env")
    team_id = required("team_id_env")
    team_name = required("team_name_env")
    component_id = required("component_id_env") if "component_id_env" in profile else None
    if component_id is not None and not component_id.isdigit():
        raise ReviewConfigError(f"Invalid Jira component ID for {repo_key}")
    labels_name = environment_variable_name(profile.get("labels_env"))
    if labels_name is None:
        raise ReviewConfigError(f"Invalid Jira environment reference for {repo_key}: labels_env")
    # Labels are optional in legacy deployments (absent means no labels); in
    # the sandbox a configured labels delivery must be present, so an absent
    # value raises inside _lookup instead of silently defaulting to [].
    labels_text = _lookup(env, labels_name)
    if not labels_text and not _sandbox_mode(env):
        labels_text = "[]"
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
    team_field_name = environment_variable_name(jira.get("team_field_env"))
    team_field = ""
    if team_field_name is not None:
        # Vault-sourced Team field: required by name in the sandbox (a
        # missing delivery raises in _lookup); the legacy BOT_ alias and the
        # public literal team_field below apply only outside the sandbox.
        team_field = _lookup(env, team_field_name)
    if not team_field:
        team_field = jira.get("team_field") if isinstance(jira.get("team_field"), str) else ""
    if not _TEAM_FIELD.fullmatch(team_field):
        raise ReviewConfigError(f"Invalid Jira Team field for {repo_key}")
    resolved = {
        "repo_key": repo_key,
        "target_repository": target_repository,
        "project": project,
        "team_field": team_field,
        "team_id": team_id,
        "team_name": team_name,
        "labels": labels,
    }
    if component_id is not None:
        resolved["component_id"] = component_id
    return resolved
