---
name: dimensional-review
description: Dispatches the pinned review plan and merges dimension findings for evidence verification
---

# Dimensional review

## Dispatch

Use the manifest and `review-plan.json` from `build_context.py`. The plan
already resolves applicability, required repository skills, guides, and model
routing. Its pass order is authoritative.

Dispatch each pass to its `review_agent` with the dimension name, `guide_path`,
and manifest path. The worker reads its guide, the matching plan entry in the
manifest, and the pinned sources it needs. A recipe pass also receives its
`context_path` when the plan lists one. Keep the full pinned diff, checkout,
impact report, and declared related sources available. Let the architecture
and cross-repository workers use the declared relationships and impact
candidates in their guides. A candidate match needs supporting evidence.

Run batches of at most four in plan order. Wait for every result in a batch
and record it under the name of the dispatched dimension before starting the
next batch. Use the plan's model assignment. Report missing guides,
unavailable workers, and malformed results as failed passes.

## Merge

Write the successful raw worker results to `dimension-results.json` as an
object keyed by the dispatched dimension. An omitted entry represents a
failed or missing pass. Run the merge command with the plan and results paths.
Resolve `<review-workflow-dir>` as specified by the review-only workflow and
run from `$HOME/app`.

```sh
python3 "<review-workflow-dir>/merge_review_results.py" \
  --plan <review-plan.json> --results <dimension-results.json> \
  --output <merged-results.json>
```

Use the merged file's `completed_dimensions`, `failed_dimensions`, and
`findings`. Record pass failures in task state. If no dimension completed,
stop before publication. For custom formatting retain each formatting
pass's original JSON and require every formatting pass to complete.

The script uses the catalog's title similarity, line proximity, and
consensus thresholds. A group contains one finding per dimension and each
pair must match. It uses the longest evidence as the canonical finding,
keeps the highest severity in the group, and ranks by agreement, severity,
and confidence. Findings below 0.7 confidence are filtered before grouping.

## Hand off to verification

Use at most seven actionable merged findings and record when the limit
excludes candidates. Use each finding's `agreed_by` for
`source_dimensions` and carry forward its claim, impact, and suggested fix.
Use pinned sources to build the scenario, supporting references, expected
result, and actual result. The workflow owns stable finding IDs, verification,
evidence validation, and publication. A dimension result is a candidate for
that pipeline.

## Catalog

Add guides under `reviewers/` and register their paths, attribution, and
applicability in `dimensions.yaml`. Most dimensions set `model_preference` to
`terra` or `luna`; the `package-upgrades` preference selects its dedicated
Luna reviewer, which can inspect official upstream release notes. An omitted
preference uses Terra. The planner includes applicable
default entries for the selected level and always includes required repository
skills. `--focus` selects explicit dimensions plus those required skills
when preparing a review plan. Dynamic guides come from configured,
commit-pinned recipes.

`learning-extraction` is opt-in. The exact `/review-bot learning-extraction`
command uses a single-pass learning plan and the contextual command reply
path. This instance reads shared review memory and returns lessons in the
PR reply.
