"""
Canonical on-disk location for the tool's RUNTIME STATE.

State — the SQLite DB, ChromaDB semantic memory, the scan profile, and the
generated e2e tests — must live OUTSIDE any single clone of the tool. The
tool is cloned in more than one place (a developer checkout, plus a fresh
checkout the CI workflow provisions); if state were stored inside a clone,
each clone would accumulate its own DB / memory / catalog and the loops
(memory feedback, test catalog reuse) would never close.

So all of it lives under one home-relative directory, shared by every
clone run by the same user. Override with $AI_WORKSPACE_HOME (used by the
test suite so it never touches a real ~/.ai-workspace).
"""

import os
from pathlib import Path

STATE_DIR = Path(
    os.environ.get("AI_WORKSPACE_HOME") or "~/.ai-workspace"
).expanduser().resolve()

# Created on import — every state path below assumes the directory exists.
STATE_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH             = STATE_DIR / "workspace.db"
CHROMA_DIR          = STATE_DIR / "chroma_db"
PROFILE_FILE        = STATE_DIR / "repo-context.json"
CORRECTIONS_FILE    = STATE_DIR / "corrections.json"
GENERATED_TESTS_DIR = STATE_DIR / "generated-tests"
