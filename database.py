import aiosqlite
import json
import uuid
import os
from datetime import datetime

# State lives under ~/.ai-workspace (see paths.py), not inside the clone,
# so a dev checkout and the CI checkout share one DB.
from paths import DB_PATH as _DB_PATH
DB_PATH = str(_DB_PATH)


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
        # P2: task graphs from `build <requirement>` breakdown. Whole graph
        # serialized as JSON in `nodes_json` (small, self-contained, easy to
        # version). `approved` distinguishes drafts vs. user-confirmed graphs.
        # P4: `contract_json` holds the Criterion list produced by the
        # multi-expert plan phase — null on graphs created before P4 landed
        # (forward-compatible with old graphs in workspace.db).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_graphs (
                id               TEXT PRIMARY KEY,
                root_requirement TEXT,
                nodes_json       TEXT,
                contract_json    TEXT,
                current_node_id  TEXT,
                approved         INTEGER DEFAULT 0,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate existing schema (column added in P4): silently add the
        # column if it's missing so workspace.db files written by P2 keep
        # working without manual migration.
        try:
            await db.execute("ALTER TABLE task_graphs ADD COLUMN contract_json TEXT")
        except Exception:
            pass  # column already exists

        # Memory slice: repo registry. Each row = one repo this workspace
        # has memory for. At most one row carries is_active=1 (enforced by
        # set_active_repo, not a DB constraint — sqlite has no UNIQUE WHERE
        # predicate). All ChromaDB add/query calls scope by `id` from here.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS repo_registry (
                id           TEXT PRIMARY KEY,
                repo_path    TEXT NOT NULL,
                display_name TEXT,
                is_active    INTEGER DEFAULT 0,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Observability slice (post-P4): Langfuse-style observations table.
        # One row per LLM call (and future: tool_call / span / event).
        # Discriminator `type` lets one table cover all kinds; `parent_observation_id`
        # gives a tree shape for `trace show`. Column names align with the
        # OpenTelemetry GenAI semconv where they map, so an OTLP exporter
        # later is rename-free. `replayed_from_id` links a replay back to
        # its source generation (prompt-level replay only).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id                     TEXT PRIMARY KEY,
                trace_id               TEXT NOT NULL,
                parent_observation_id  TEXT,
                type                   TEXT NOT NULL,
                agent_name             TEXT,
                model                  TEXT,
                provider               TEXT,
                operation              TEXT,
                messages_json          TEXT,
                response_json          TEXT,
                input_tokens           INTEGER,
                output_tokens          INTEGER,
                latency_ms             INTEGER,
                finish_reason          TEXT,
                error_message          TEXT,
                replayed_from_id       TEXT,
                created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_trace ON observations(trace_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_parent ON observations(parent_observation_id)"
        )

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


async def save_finding(task_id: str, agent: str, severity: str, content: dict,
                       finding_id: str | None = None):
    """
    Persist a finding. If finding_id is provided (the normal path from
    BaseAgent / runner), reuse it so the AgentFinding's id matches the DB
    row id — that's the only id GitHub-posted comments use, and `apply`
    looks up by it. Caller passes `f.finding_id`; legacy callers that
    don't pass anything still get a fresh id.
    """
    fid = finding_id or str(uuid.uuid4())[:8]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO task_findings (id, task_id, agent, severity, content) VALUES (?, ?, ?, ?, ?)",
            (fid, task_id, agent, severity, json.dumps(content))
        )
        await db.commit()
    return fid


async def get_pending_findings(task_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM task_findings WHERE task_id=? AND accepted IS NULL ORDER BY created_at",
            (task_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_finding(finding_id: str) -> dict | None:
    """Look up a single finding by id. Used by `apply` to retrieve the
    suggestion + file/line that the user is asking the AI to apply."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM task_findings WHERE id=?", (finding_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


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


# ---------- Task graph CRUD (P2) ----------

async def save_graph(graph) -> None:
    """
    Upsert a TaskGraph. Accepts either a Pydantic TaskGraph or a dict shaped
    like one (the CLI may serialize before calling). Whole node list is
    persisted as JSON in nodes_json; contract (if any) in contract_json.
    Graphs are small enough that one row per graph is cheaper than
    normalized tables.
    """
    if hasattr(graph, "model_dump"):
        data = graph.model_dump()
    else:
        data = dict(graph)

    nodes_json    = json.dumps(data.get("nodes", []))
    contract_data = data.get("contract")
    contract_json = json.dumps(contract_data) if contract_data else None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO task_graphs
                (id, root_requirement, nodes_json, contract_json,
                 current_node_id, approved)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                root_requirement = excluded.root_requirement,
                nodes_json       = excluded.nodes_json,
                contract_json    = excluded.contract_json,
                current_node_id  = excluded.current_node_id,
                approved         = excluded.approved,
                updated_at       = CURRENT_TIMESTAMP
            """,
            (
                data["graph_id"],
                data.get("root_requirement", ""),
                nodes_json,
                contract_json,
                data.get("current_node_id"),
                1 if data.get("approved") else 0,
            ),
        )
        await db.commit()


async def load_graph(graph_id: str) -> dict | None:
    """
    Return a graph as a dict (with nodes + contract parsed back from JSON),
    or None. Returning a dict instead of a TaskGraph avoids a circular
    import with models.py and keeps database.py free of pydantic —
    callers reconstruct via TaskGraph(**data).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM task_graphs WHERE id=?", (graph_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["nodes"] = json.loads(d.pop("nodes_json"))
            except (json.JSONDecodeError, TypeError):
                d["nodes"] = []
            # contract is optional and may be missing on pre-P4 rows
            contract_raw = d.pop("contract_json", None)
            if contract_raw:
                try:
                    d["contract"] = json.loads(contract_raw)
                except (json.JSONDecodeError, TypeError):
                    d["contract"] = None
            else:
                d["contract"] = None
            d["graph_id"] = d.pop("id")
            d["approved"] = bool(d.get("approved"))
            return d


async def list_graphs() -> list[dict]:
    """Return a summary row per graph (no nodes_json) for shell listing."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, root_requirement, current_node_id, approved,
                      created_at, updated_at
               FROM task_graphs ORDER BY created_at DESC"""
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


# ---------- Repo registry CRUD (memory slice) ----------

async def list_repos() -> list[dict]:
    """Return all registered repos, oldest first. `repo list` consumes this."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM repo_registry ORDER BY created_at"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_repo(repo_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM repo_registry WHERE id=?", (repo_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def add_repo(repo_id: str, repo_path: str, display_name: str | None = None) -> None:
    """Insert (or update path/name on conflict). Does NOT touch is_active —
    `set_active_repo` is the only call that flips that bit."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO repo_registry (id, repo_path, display_name)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                repo_path    = excluded.repo_path,
                display_name = excluded.display_name
            """,
            (repo_id, repo_path, display_name or repo_id),
        )
        await db.commit()


async def set_active_repo(repo_id: str | None) -> None:
    """Atomically flip the active row. Passing None deactivates everything
    (no repo is current). One transaction so we never end up with two
    active rows."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE repo_registry SET is_active = 0")
        if repo_id is not None:
            await db.execute(
                "UPDATE repo_registry SET is_active = 1 WHERE id = ?",
                (repo_id,),
            )
        await db.commit()


async def get_active_repo() -> dict | None:
    """Single source of truth for 'which repo is currently in scope'."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM repo_registry WHERE is_active = 1 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def remove_repo(repo_id: str) -> bool:
    """Delete the registry row only — ChromaDB cleanup is a separate
    decision made by the CLI (`repo remove` asks the user before purging
    memory entries). Returns True if a row was deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM repo_registry WHERE id=?", (repo_id,)
        )
        await db.commit()
        return (cursor.rowcount or 0) > 0


# ---------- Observations CRUD (observability slice) ----------

async def save_observation(
    *,
    observation_id: str,
    trace_id: str,
    type: str,
    parent_observation_id: str | None = None,
    agent_name: str | None = None,
    model: str | None = None,
    provider: str | None = "anthropic",
    operation: str | None = "chat",
    messages_json: str | None = None,
    response_json: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    finish_reason: str | None = None,
    error_message: str | None = None,
    replayed_from_id: str | None = None,
) -> None:
    """
    Insert one observation row. Caller picks the id (so the wrapper can
    return it to its caller for replay linking) and stamps the type. Most
    fields are optional — error paths write rows with response_json=None
    and finish_reason='error' so failed calls remain visible in trace show.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO observations
                (id, trace_id, parent_observation_id, type, agent_name,
                 model, provider, operation, messages_json, response_json,
                 input_tokens, output_tokens, latency_ms, finish_reason,
                 error_message, replayed_from_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id, trace_id, parent_observation_id, type, agent_name,
                model, provider, operation, messages_json, response_json,
                input_tokens, output_tokens, latency_ms, finish_reason,
                error_message, replayed_from_id,
            ),
        )
        await db.commit()


async def get_observations_by_trace(trace_id: str) -> list[dict]:
    """Return all observations for a trace, oldest first. `trace show` uses
    this and builds the tree client-side via parent_observation_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM observations WHERE trace_id=? ORDER BY created_at",
            (trace_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_observation(observation_id: str) -> dict | None:
    """Single-row fetch — used by `trace replay`."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM observations WHERE id=?",
            (observation_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_node_status(graph_id: str, node_id: str, new_status: str) -> bool:
    """
    Update one node's status inside a stored graph. Returns True if the node
    was found. Done by load → mutate JSON → save_graph, since graphs are
    small (< 50 nodes). Cheaper than maintaining a normalized nodes table.
    """
    g = await load_graph(graph_id)
    if not g:
        return False
    changed = False
    for n in g["nodes"]:
        if n.get("id") == node_id:
            n["status"] = new_status
            changed = True
            break
    if not changed:
        return False
    await save_graph(g)
    return True
