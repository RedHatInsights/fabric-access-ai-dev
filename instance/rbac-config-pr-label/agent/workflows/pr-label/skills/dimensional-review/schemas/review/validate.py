#!/usr/bin/env python3
"""Validate a review JSON file against the unified review schema.

Usage:
    python schemas/review/validate.py <file.json> [<file2.json> ...]
    cat review.json | python schemas/review/validate.py /dev/stdin
"""

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator, ValidationError
except ImportError:
    print("ERROR: jsonschema is required. Install with: pip install jsonschema", file=sys.stderr)
    sys.exit(2)

SCHEMA_PATH = Path(__file__).parent / "review.schema.json"


def validate_file(filepath: str) -> list[str]:
    path = Path(filepath)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [f"Invalid JSON: {e}"]
    except FileNotFoundError:
        return [f"File not found: {filepath}"]

    schema = json.loads(SCHEMA_PATH.read_text())
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    return [f"  {'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    exit_code = 0
    for filepath in sys.argv[1:]:
        errors = validate_file(filepath)
        if errors:
            print(f"FAIL {filepath}")
            for err in errors:
                print(err)
            exit_code = 1
        else:
            review_type = json.loads(Path(filepath).read_text())["review"]["review_type"]
            print(f"OK   {filepath} (review_type: {review_type})")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
