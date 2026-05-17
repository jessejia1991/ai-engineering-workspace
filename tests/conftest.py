"""
Test isolation. Tool runtime state (DB, ChromaDB, profile, generated tests)
defaults to ~/.ai-workspace — a real, user-owned directory. Tests must NOT
touch it. Redirect state to a throwaway dir here, before any tool module
(and paths.py, which reads AI_WORKSPACE_HOME at import time) is imported.

pytest loads conftest.py before collecting test modules, so this runs first.
"""

import os
import tempfile

os.environ.setdefault(
    "AI_WORKSPACE_HOME",
    os.path.join(tempfile.gettempdir(), "ai-workspace-test"),
)
