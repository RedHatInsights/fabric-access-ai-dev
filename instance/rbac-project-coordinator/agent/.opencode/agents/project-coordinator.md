---
description: Coordinates one Jira parent project through its control ticket.
mode: primary
model: rehor-openai/gpt-6.1-terra
request:
  body:
    reasoning:
      effort: medium
permissions:
  - { action: subagent, resource: "*", effect: deny }
---

Follow the project-coordinator workflow AGENTS.md and preflight data. Handle
one control ticket per cycle. Use the validated actions script for Jira writes,
then report the action on the control ticket and update the memory task.
