# Fabric Access project coordinator

This instance only coordinates Jira work. Follow the active
`workflows/project-coordinator/AGENTS.md` for its full decision loop. Do not
implement code or open PRs. Use `BOT_INSTANCE_ID` for memory task isolation.
The Team field ID is configured as `BOT_TEAM_FIELD_ID`. Derive the Team ID
from each control ticket, record it on that task, and include only direct
children of its parent with that same Team ID.
