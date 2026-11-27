---
name: restore-review-ledger
description: Materialize the tracked review ledger inside the runner when review-only preflight sets ledger_restore to runner.
---

# Restore the tracked review ledger

For a selected tracked PR with `work.ledger_restore = "runner"`, run from
`$HOME/app`. In a sandbox, replace `<review-workflow-dir>` below with
`$HOME/app/instance/rbac-config-review/agent/workflows/review-only`. In the
legacy runner, use `$HOME/app/data/remote-config/instance/rbac-config-review/agent/workflows/review-only`.
Do not depend on `REHOR_SCRIPT_DIR` or assume remote config exists in a sandbox.
Use the immutable work reference from preflight:

```sh
python3 "<review-workflow-dir>/restore_review_ledger.py" \
  --repo '<work.repo>' --pr '<work.number>' \
  --head-sha '<work.head_sha>' --base-sha '<work.base_sha>' \
  --review-id '<work.status_review_id>' --task-key '<work.task_key>'
```

`repo` is `owner/name`, not a repo key; `number` and `status_review_id` are
positive integers. The task key must be `review-only:github:<repo>#<number>`.
The runner reads that exact Bot Memory task, confirms ownership by
`BOT_INSTANCE_ID` and its tracked `metadata.review_id`, checks both live PR
SHAs, and reuses the bot-owned status-review ledger decoder. A submitted
review's ledger may be from an earlier pair; its embedded revision must match
its own status marker. Do not add a local path, a body, or ledger JSON to the
preflight reference.

Append `--pending` **only** for the existing publication retry path when the
task has a `pending_publication` for this exact head/base. The CLI checks
the exact saved prepared body, its revision, and counter. It will not reuse a
tracked pending GitHub draft with a body, comments, or an older commit. This
restores only the ledger, not the rest of the publication artifacts. If any
required retry artifact is unavailable in this runner, stop and report it;
do not rebuild those artifacts, run reviewers, or advance the counter.

On exit 0 the command prints one JSON object: `ledger_path` (absolute local
file), `update_count` (integer), and `source` (`review` or `pending`). Use the
path directly wherever the workflow calls for `work.ledger_path`, and use the
returned count as `work.update_count`. Do not copy ledger JSON through the
model or persist this path in task state. A nonzero exit or missing output
artifact blocks the work item. Do not fall back to a fresh review or legacy
task-local path. The helper does not write to GitHub or Bot Memory.
