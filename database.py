import aiosqlite
import json
import uuid
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id           TEXT PRIMARY KEY,
                type         TEXT,
                status       TEXT DEFAULT 'PENDING',
                dependencies TEXT DEFAULT '[]',
                locked_by    TEXT,
                locked_at    TIMESTAMP,
                artifacts    TEXT DEFAULT '{}',
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_findings (
                id         TEXT PRIMARY KEY,
                task_id    TEXT,
                agent      TEXT,
                severity   TEXT,
                content    TEXT,
                accepted   INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS execution_log (
                id         TEXT PRIMARY KEY,
                task_id    TEXT,
                event_type TEXT,
                agent      TEXT,
                payload    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def create_task(task_id: str, task_type: str, artifacts: dict):
    # Upsert: re-running review on the same PR resets the task to PENDING
    # instead of crashing on UNIQUE constraint.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tasks (id, type, artifacts)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type       = excluded.type,
                artifacts  = excluded.artifacts,
                status     = 'PENDING',
                updated_at = CURRENT_TIMESTAMP
            """,
            (task_id, task_type, json.dumps(artifacts))
        )
        await db.commit()


async def clear_unreviewed_findings(task_id: str) -> int:
    # Re-review of the same PR clears stale untriaged findings while keeping
    # human-accepted / rejected ones as history.
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM task_findings WHERE task_id=? AND accepted IS NULL",
            (task_id,)
        )
        await db.commit()
        return cursor.rowcount or 0


async def update_task_status(task_id: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now().isoformat(), task_id)
        )
        await db.commit()


async def get_all_tasks():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, type, status, created_at FROM tasks ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_task(task_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_finding(task_id: str, agent: str, severity: str, content: dict):
    finding_id = str(uuid.uuid4())[:8]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO task_findings (id, task_id, agent, severity, content) VALUES (?, ?, ?, ?, ?)",
            (finding_id, task_id, agent, severity, json.dumps(content))
        )
        await db.commit()
    return finding_id


async def get_pending_findings(task_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM task_findings WHERE task_id=? AND accepted IS NULL ORDER BY created_at",
            (task_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_finding_accepted(finding_id: str, accepted: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE task_findings SET accepted=? WHERE id=?",
            (1 if accepted else 0, finding_id)
        )
        await db.commit()


async def log_execution(task_id: str, event_type: str, agent: str, payload: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO execution_log (id, task_id, event_type, agent, payload) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, event_type, agent, json.dumps(payload))
        )
        await db.commit()


async def get_execution_log(task_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM execution_log WHERE task_id=? ORDER BY created_at",
            (task_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_agent_reasoning(task_id: str) -> dict:
    """
    Return per-agent reasoning chains for a task, keyed by agent name.

    Reads 'agent_result' rows from execution_log and extracts the reasoning
    and memory_injected payloads written by the orchestrator. This is the
    bridge that turns hidden agent reasoning into observable state for the
    review display, the reflect command, and the logs command.

    Returns:
        {
          "SecurityAgent": {
            "reasoning": {
              "codebase_understanding": "...",
              "rejected_candidates": [...],
              "confidence_per_finding": {...},
            },
            "memory_injected": {"findings_count": 3, "corrections_count": 1},
            "latency_ms": 18400,
            "finding_count": 1,
            "status": "ok",
          },
          ...
        }
    """
    rows = await get_execution_log(task_id)
    by_agent: dict = {}

    for row in rows:
        if row.get("event_type") != "agent_result":
            continue

        agent = row.get("agent", "")
        if not agent:
            continue

        try:
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        except (json.JSONDecodeError, TypeError):
            payload = {}

        # If an agent retried, the last agent_result row wins (latest attempt).
        by_agent[agent] = {
            "reasoning":       payload.get("reasoning", {}) or {},
            "memory_injected": payload.get("memory_injected", {}) or {},
            "latency_ms":      payload.get("latency_ms"),
            "finding_count":   payload.get("finding_count"),
            "status":          payload.get("status", "ok"),
        }

    return by_agent
