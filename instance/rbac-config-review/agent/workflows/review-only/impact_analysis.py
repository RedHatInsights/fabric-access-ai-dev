"""Derive candidate cross-repository impacts from a pinned review snapshot.

The report identifies changed identifiers and concrete references. It does not
claim that a reference is broken; that requires review and verification.
"""

from __future__ import annotations

import re
from pathlib import Path

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{5,}")
MATCH = re.compile(r"^(?:HEAD:)?(.+?):(\d+):(.*)$")


def identifiers(line: str) -> set[str]:
    return {
        token for token in IDENTIFIER.findall(line)
        if len(token) <= 100 and any(char in token for char in "._/:-")
    }


def changed_lines(patch: str) -> list[dict]:
    """Parse changed lines with their old or new file line numbers."""
    result: list[dict] = []
    old_file = new_file = None
    old_line = new_line = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            old_file = new_file = None
            old_line = new_line = None
        elif line.startswith("--- a/"):
            old_file = line[6:]
        elif line.startswith("--- /dev/null"):
            old_file = None
        elif line.startswith("+++ b/"):
            new_file = line[6:]
        elif line.startswith("+++ /dev/null"):
            new_file = None
        elif line.startswith("@@ "):
            match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_line, new_line = map(int, match.groups())
        elif old_line is not None and new_line is not None:
            if line.startswith("+") and new_file:
                result.append({"file": new_file, "line": new_line, "side": "added", "text": line[1:]})
                new_line += 1
            elif line.startswith("-") and old_file:
                result.append({"file": old_file, "line": old_line, "side": "removed", "text": line[1:]})
                old_line += 1
            elif line.startswith(" "):
                old_line += 1
                new_line += 1
    return result


def make_impact_report(manifest: dict, patch: str) -> dict:
    lines = changed_lines(patch)
    added = set().union(*(identifiers(item["text"]) for item in lines if item["side"] == "added"))
    removed = set().union(*(identifiers(item["text"]) for item in lines if item["side"] == "removed"))
    changed = added | removed
    identifiers_changed = [
        {
            "identifier": token,
            "change": "retained" if token in added and token in removed else "added" if token in added else "removed",
            "locations": [
                {"file": item["file"], "line": item["line"], "side": item["side"]}
                for item in lines if token in identifiers(item["text"])
            ],
        }
        for token in sorted(changed)
    ]
    usages: list[dict] = []
    unavailable: list[dict] = []
    for related in manifest["related"]:
        if related.get("error"):
            unavailable.append({"id": related["id"], "error": related["error"]})
            continue
        for raw in related.get("matches", []):
            match = MATCH.match(raw)
            if not match:
                continue
            file, line, excerpt = match.groups()
            for token in sorted(changed & identifiers(excerpt)):
                usages.append({
                    "identifier": token,
                    "repo": related["repo"],
                    "sha": related["sha"],
                    "file": file,
                    "line": int(line),
                    "excerpt": excerpt[:300],
                    "status": "candidate",
                })
    for doc in manifest["docs"]:
        if doc.get("error"):
            unavailable.append({"id": doc["id"], "error": doc["error"]})
    return {
        "pr": {key: manifest["pr"][key] for key in ("repo", "number", "head_sha", "base_sha")},
        "changed_files": sorted({item["file"] for item in lines}),
        "changed_identifiers": identifiers_changed,
        "consumer_usages": usages,
        "unavailable_sources": unavailable,
        "scope": "Candidates from changed identifiers and pinned related-repo search; not confirmed breakage.",
    }


def load_report(manifest: dict) -> dict:
    return make_impact_report(manifest, Path(manifest["pr"]["diff_path"]).read_text())
