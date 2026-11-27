# Fabric Access project coordinator

This instance runs the Jira coordinator and the technical planner. Follow
the routing in `workflows/project-coordinator/AGENTS.md`. Do not implement
code or open PRs. Use `BOT_INSTANCE_ID` for coordinator memory task isolation.
The Team field ID is `FABRIC_TEAM_FIELD_ID` in the sandbox (Vault-sourced
and required: stop and report a configuration blocker when missing) and the
legacy `BOT_TEAM_FIELD_ID` in app-interface deployments. Derive the Team ID
from each control ticket, record it on that task, and include only direct
children of its parent with that same Team ID in coordination.
