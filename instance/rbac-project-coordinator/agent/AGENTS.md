# Fabric Access project coordinator

This instance only coordinates Jira work. Follow the active
`workflows/project-coordinator/AGENTS.md` for its full decision loop. Do not
implement code or open PRs. Use `BOT_INSTANCE_ID` for memory task isolation.
The control ticket's Team ID must equal `BOT_TEAM_ID`; only direct children of
its parent with that same Team ID are in scope.
