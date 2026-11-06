#!/usr/bin/env python3
"""Run one deterministic GitHub review queue scan."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from review_queue import main

main()
