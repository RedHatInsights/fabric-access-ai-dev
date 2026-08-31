#!/usr/bin/env python3
"""GH PR status for this workflow's tasks only (pr-label:* keys)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gh_pr_status
from labeled_prs import own_tasks

_orig_get_tasks = gh_pr_status.get_tasks
gh_pr_status.get_tasks = lambda: own_tasks(_orig_get_tasks())
gh_pr_status.main()
