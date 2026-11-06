# Unified Review JSON Schema

General-purpose JSON schema for the AI-assisted code review automation cycle. Defines a common format for review findings, verification results, and future review stages — enabling tools, CI bots, and apps to interoperate across the entire review lifecycle.

The schema is designed to be extensible: new `review_type` values can be added for additional stages (e.g., `re-review`, `regression-check`, `security-audit`) without breaking existing consumers.

## Schema Overview

```
review.schema.json          JSON Schema (Draft 2020-12) — source of truth
validate.py                 CLI validation tool
examples/
  review.json               Valid example — initial review output
  verify.json               Valid example — verification output
```

## Review Types

The `review_type` field is the discriminator. New types can be added as the review automation cycle grows.

| `review_type` | Stage | Purpose | Current Producer |
|----------------|-------|---------|------------------|
| `review` | Initial review | Code review producing findings with severity, evidence, suggestions | `dimensional-review` skill |
| `verify` | Follow-up | Check whether findings were addressed after author pushes changes | `verify-review-findings` skill |
| *(future)* `re-review` | Second pass | Full re-review after significant changes, producing new findings | — |
| *(future)* `security-audit` | Specialized | Focused security-only review with compliance tracking | — |
| *(future)* `regression-check` | CI gate | Automated check that previously-fixed findings haven't regressed | — |

## Structure

Both types share a common envelope:

```json
{
  "review": {
    "review_type": "review | verify",
    "pr": { ... },
    "generated": "ISO 8601 timestamp",
    "total_deduped_findings": 18,
    "findings": [ ... ]
  }
}
```

### `review` type adds:

| Field | Type | Description |
|-------|------|-------------|
| `dimensions_dispatched` | integer | Number of review dimensions that ran |
| `total_raw_findings` | integer | Total findings before deduplication |
| `consensus_findings` | integer | Findings agreed by 3+ dimensions |

### `verify` type adds:

| Field | Type | Description |
|-------|------|-------------|
| `checked_commit` | string | Short SHA of the commit checked against |
| `original_review` | object | Reference to the original review |
| `summary` | object | Status counts (addressed, partially_addressed, etc.) |

## Field Reference

### `review` (root)

| Field | Type | Required | Both Types | Description |
|-------|------|----------|:----------:|-------------|
| `review_type` | string | yes | yes | `"review"` or `"verify"` |
| `pr` | object | yes | yes | Pull request metadata |
| `generated` | string (date-time) | yes | yes | When this review was generated |
| `total_deduped_findings` | integer | no | yes | Unique findings after dedup |
| `findings` | array | yes | yes | List of finding objects |
| `dimensions_dispatched` | integer | yes (R) | R only | Dimensions that ran |
| `total_raw_findings` | integer | yes (R) | R only | Findings before dedup |
| `consensus_findings` | integer | yes (R) | R only | Findings with 3+ dimension agreement |
| `checked_commit` | string | yes (V) | V only | Short SHA checked against |
| `original_review` | object | yes (V) | V only | Reference to original review |
| `summary` | object | yes (V) | V only | Verification status counts |

*R = review, V = verify*

### `review.pr`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `number` | integer | yes | PR number |
| `title` | string | yes | PR title |
| `author` | string | no | PR author username |
| `url` | string (uri) | yes | Full PR URL |

### `review.findings[]`

Base fields present in both review types:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Finding number (e.g., `"1"`, `"2"`) |
| `file` | string | yes | File path relative to repo root |
| `line` | integer | no | Approximate line number |
| `severity` | enum | yes | `critical`, `high`, `medium`, `low`, `info` |
| `dimension` | string | no | Primary review dimension (e.g., `"correctness"`) |
| `source_skill` | string | no | Plugin identifier (e.g., `"productivity-utils:code-review"`) |
| `source_repo` | string | no | Repo containing the source skill |
| `finding` | string | yes | Description of the issue |
| `impact` | string | no | Concrete consequence for affected users or systems if the issue remains unfixed; required by dimensional-review |
| `evidence` | string | no | Code or context supporting the finding |
| `suggestion` | string | no | Suggested fix or improvement |
| `confidence` | number | yes | Confidence score (0.0 to 1.0) |
| `agreed_by` | string[] | no | Dimension names that independently found this |
| `agreed_by_skills` | string[] | no | Source skill IDs for each agreeing dimension |
| `agreed_count` | integer | no | Number of agreeing dimensions |
| `alternatives` | object[] | no | Alternative approaches (see below) |

Verify-specific fields (added when `review_type: "verify"`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | enum | no | `addressed`, `partially_addressed`, `not_addressed`, `out_of_scope`, `superseded` |
| `status_evidence` | string | no | One sentence explaining the classification |
| `still_needed` | string/null | no | What remains (for `partially_addressed`, null otherwise) |

### `review.findings[].alternatives[]`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approach` | string | yes | Description of the alternative approach |
| `trade_off` | string | yes | When this approach is appropriate |
| `recommended` | boolean | no | Whether this is the recommended approach |

### `review.original_review` (verify only)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `review_type` | string | yes | Always `"review"` |
| `generated` | string (date-time) | no | When the original review was generated |
| `dimensions_dispatched` | integer | no | Dimensions in the original review |
| `total_findings` | integer | yes | Total findings in the original review |

### `review.summary` (verify only)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `addressed` | integer | yes | Findings fully resolved |
| `partially_addressed` | integer | yes | Findings partially fixed |
| `not_addressed` | integer | yes | Findings with no relevant changes |
| `out_of_scope` | integer | yes | Findings outside the PR's intent |
| `superseded` | integer | yes | Findings made moot by refactoring |

## Validation

Requires Python 3.10+ and `jsonschema`:

```bash
pip install jsonschema
```

### Validate a file

```bash
python3 schemas/review/validate.py review-output.json
# OK   review-output.json (review_type: review)
```

### Validate multiple files

```bash
python3 schemas/review/validate.py *.json
```

### Validate from stdin

```bash
cat review.json | python3 schemas/review/validate.py /dev/stdin
```

### Validate the examples

```bash
python3 schemas/review/validate.py schemas/review/examples/*.json
# OK   schemas/review/examples/review.json (review_type: review)
# OK   schemas/review/examples/verify.json (review_type: verify)
```

### Programmatic validation (Python)

```python
import json
from jsonschema import Draft202012Validator

schema = json.load(open("schemas/review/review.schema.json"))
data = json.load(open("review-output.json"))

validator = Draft202012Validator(schema)
errors = list(validator.iter_errors(data))
if errors:
    for e in errors:
        print(f"{'.'.join(str(p) for p in e.absolute_path)}: {e.message}")
else:
    print("Valid")
```

## Workflow

```
Initial review (any review skill)
    │
    ├── produces review-output.json  (review_type: "review")
    │
    ▼
PR author pushes changes
    │
    ▼
Verification (verify-review-findings skill)
    │
    ├── reads review-output.json as input
    └── produces verify-output.json  (review_type: "verify")
```

## Consumers

Any tool, skill, or app that produces or consumes review findings can use this schema.

| Consumer | Produces | Consumes |
|----------|----------|----------|
| `dimensional-review` skill | `review_type: "review"` | — |
| `verify-review-findings` skill | `review_type: "verify"` | `review_type: "review"` |
| CI bots / GitHub Actions | — | Either type (for gating, reporting) |
| PRReviewer app | — | Either type (for UI display) |
| Custom integrations | Either type | Either type |

## Extending the Schema

To add a new `review_type`:

1. Add the type to the `review_type` enum in `review.schema.json`
2. Add an `if/then` block under `review.allOf` for type-specific required fields
3. Add type-specific `$defs` if needed
4. Add an example in `examples/`
5. Update this README's Review Types table and Field Reference
