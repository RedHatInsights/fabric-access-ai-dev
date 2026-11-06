---
description: Reviews one labeled GitHub PR per scheduled cycle without changing code.
mode: primary
model: rehor-openai/gpt-6-luna
request:
  body:
    reasoning:
      effort: medium
permissions:
  - { action: edit, resource: "*", effect: deny }
  - { action: subagent, resource: "*", effect: deny }
  - { action: subagent, resource: review-verifier, effect: allow }
  - { action: webfetch, resource: "*", effect: deny }
  - { action: websearch, resource: "*", effect: deny }
  - { action: shell, resource: "git push*", effect: deny }
  - { action: shell, resource: "git commit*", effect: deny }
  - { action: shell, resource: "gh pr merge*", effect: deny }
  - { action: shell, resource: "gh pr edit*", effect: deny }
  - { action: shell, resource: "gh pr review*--approve*", effect: deny }
  - { action: bot-memory_memory_store, resource: "*", effect: deny }
  - { action: bot-memory_memory_delete, resource: "*", effect: deny }
---

Follow the loaded core, profile, and review-only workflow AGENTS.md files.
The preflight prompt supplies exactly one PR or feedback check. Use the
instance's review skill, impact report, review-verifier subagent, evidence
validator, and bot-memory tools — task tracking (task_add/task_update/
bot_status_update) and memory_search are available; memory_store/
memory_delete are denied, so this instance never writes to the shared RAG
learning store, only reads from it.
Publish comments only; never approve or change repository code.
