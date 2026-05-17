"""
ChromaDB-backed semantic memory, repo-scoped.

Three collections:
  - findings_memory     per-agent: which past findings did humans accept/reject
  - corrections_memory  cross-agent: explicit "don't repeat this mistake" notes
  - planning_memory     repo-wide: requirement → final node mix + user edits

Every entry carries:
  - repo_id           the repo it was learned on (required)
  - last_accessed_at  bumped on every successful retrieval (LRU signal)
  - pinned            bool — protects against `memory prune`

Retrieval is two-phase:
  Phase 1: where={repo_id: active}        → tagged origin="own"
  Phase 2: where={repo_id: {"$ne": active}} → tagged origin="cross"
fills to top_k. Cross-repo hits exist because some engineering lessons
("trivial getter/setter — skip unit tests") generalize across projects.
The own/cross marker is preserved into `format_memory_for_prompt` so the
LLM can weight the two pools differently.
"""

import os
import math
import threading
import chromadb
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ChromaDB lives under ~/.ai-workspace (see paths.py) — shared across clones.
from paths import CHROMA_DIR as _CHROMA_DIR
CHROMA_PATH = str(_CHROMA_DIR)

_client = None
_findings_collection = None
_corrections_collection = None
_planning_collection = None
_test_catalog_collection = None
_init_lock = threading.RLock()


def get_client():
    global _client
    if _client is None:
        with _init_lock:
            if _client is None:
                os.makedirs(CHROMA_PATH, exist_ok=True)
                _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client


def get_findings_collection():
    global _findings_collection
    if _findings_collection is None:
        with _init_lock:
            if _findings_collection is None:
                _findings_collection = get_client().get_or_create_collection(
                    name="findings_memory",
                    metadata={"hnsw:space": "cosine"}
                )
    return _findings_collection


def get_corrections_collection():
    global _corrections_collection
    if _corrections_collection is None:
        with _init_lock:
            if _corrections_collection is None:
                _corrections_collection = get_client().get_or_create_collection(
                    name="corrections_memory",
                    metadata={"hnsw:space": "cosine"}
                )
    return _corrections_collection


def get_test_catalog_collection():
    """
    5th memory layer (verify slice): catalog of generated e2e tests.
    Each entry = one test file. Document text = "Tests <flow description>".
    Metadata: repo_id, test_id, apis_covered (comma-separated),
    file_path, generated_at, last_run_at, last_status (PASS/FAIL/UNKNOWN).

    Strict repo-isolation at the query layer — a test for repo A doesn't
    generalize to repo B (different APIs, different payloads). Cross-repo
    fallback that other layers use is disabled here at the caller level.
    """
    global _test_catalog_collection
    if _test_catalog_collection is None:
        with _init_lock:
            if _test_catalog_collection is None:
                _test_catalog_collection = get_client().get_or_create_collection(
                    name="test_catalog",
                    metadata={"hnsw:space": "cosine"}
                )
    return _test_catalog_collection


def get_planning_collection():
    """
    4th memory layer (P2): every approved `build` invocation deposits a
    semantically-searchable record of its requirement, clarify Q&A (if any),
    final node mix, and user edits. Future builds query this layer for
    similar past requirements to (a) skip unnecessary clarify rounds and
    (b) inherit decomposition patterns the user previously preferred.
    """
    global _planning_collection
    if _planning_collection is None:
        with _init_lock:
            if _planning_collection is None:
                _planning_collection = get_client().get_or_create_collection(
                    name="planning_memory",
                    metadata={"hnsw:space": "cosine"}
                )
    return _planning_collection


# ===================================================================
# Add — every entry is tagged with a repo_id + lifecycle metadata.
# ===================================================================

def _now_ts() -> float:
    return datetime.now().timestamp()


def add_finding(finding_id: str, finding: dict, accepted: bool, repo_id: str):
    """Store a finding after human accept/reject. repo_id is required —
    no global scope at write time; cross-repo retrieval happens at query
    time via the phase-2 fallback."""
    collection = get_findings_collection()
    document = f"{finding.get('title', '')}. {finding.get('detail', '')}. {finding.get('suggestion', '')}"
    now = _now_ts()
    collection.add(
        ids=[finding_id],
        documents=[document],
        metadatas=[{
            "repo_id":          repo_id,
            "agent":            finding.get("agent", ""),
            "severity":         finding.get("severity", "low"),
            "category":         finding.get("category", ""),
            "accepted":         str(accepted).lower(),
            "file":             finding.get("file", "") or "",
            "task_id":          finding.get("task_id", ""),
            "timestamp":        now,
            "last_accessed_at": now,
            "pinned":           False,
        }]
    )


def add_correction(correction_id: str, note: str, example: str,
                   correction_type: str, repo_id: str,
                   pinned: bool = False):
    """Store a correction (LLM misunderstanding). repo_id is required.
    `pinned=True` protects against `memory prune` — used when the user
    flags a correction as "must not be evicted" during reflect."""
    collection = get_corrections_collection()
    document = f"{note}. Example: {example}"
    now = _now_ts()
    collection.add(
        ids=[correction_id],
        documents=[document],
        metadatas=[{
            "repo_id":          repo_id,
            "type":             correction_type,
            "timestamp":        now,
            "last_accessed_at": now,
            "pinned":           bool(pinned),
        }]
    )


def add_plan(
    plan_id: str,
    requirement: str,
    needed_clarify: bool,
    clarify_qa: str,
    node_count: int,
    node_types: list[str],
    edits: list[str],
    approved: bool,
    repo_id: str,
):
    """Record an approved `build` invocation. repo_id is required."""
    collection = get_planning_collection()
    parts = [f"Requirement: {requirement}"]
    if needed_clarify and clarify_qa:
        parts.append(f"Clarify Q&A: {clarify_qa}")
    parts.append(f"Plan: {node_count} nodes — {', '.join(node_types) or 'unknown'}")
    if edits:
        parts.append(f"User edits: {'; '.join(edits)}")
    else:
        parts.append("User edits: none")
    document = "\n".join(parts)
    now = _now_ts()
    collection.add(
        ids=[plan_id],
        documents=[document],
        metadatas=[{
            "repo_id":         repo_id,
            "requirement":     requirement[:500],
            "needed_clarify":  bool(needed_clarify),
            "node_count":      int(node_count),
            "node_types":      ",".join(node_types),
            "edits_count":     len(edits),
            "edits_summary":   "; ".join(edits)[:500],
            "approved":        bool(approved),
            "timestamp":       now,
            "last_accessed_at": now,
            "pinned":          False,
        }]
    )


# ===================================================================
# Test catalog (verify slice) — 5th memory layer.
# ===================================================================

def add_test_to_catalog(
    test_id: str,
    description: str,
    apis_covered: list[str],
    file_path: str,
    repo_id: str,
    generated_at: str | None = None,
    source_files: list[str] | None = None,
) -> None:
    """
    Register a generated test. document = "Tests <description>" so semantic
    search by intent ("notes field validation") finds it.
    apis_covered comes in as ["POST /api/pets", "GET /api/pets/{id}"] —
    flattened to a comma-separated string because ChromaDB metadata is
    primitive-typed. source_files is the repo-relative controller file(s)
    that implement those APIs — it powers file→test impact lookup
    (query_tests_by_files) and controller-level dedup in `verify generate`.
    """
    coll = get_test_catalog_collection()
    now = _now_ts()
    coll.add(
        ids=[test_id],
        documents=[f"Tests {description}"],
        metadatas=[{
            "repo_id":       repo_id,
            "test_id":       test_id,
            "apis_covered":  ",".join(apis_covered) if apis_covered else "",
            "source_files":  ",".join(source_files) if source_files else "",
            "file_path":     file_path,
            "generated_at":  generated_at or datetime.now().isoformat(),
            "last_run_at":   "",
            "last_status":   "UNKNOWN",
            "timestamp":     now,
        }]
    )


def update_test_run_status(test_id: str, status: str,
                           last_run_at: str | None = None) -> None:
    """Bump last_run_at + last_status on a catalog entry. Swallow errors —
    catalog updates are best-effort, must not block `verify run`."""
    try:
        coll = get_test_catalog_collection()
        coll.update(
            ids=[test_id],
            metadatas=[{
                "last_run_at": last_run_at or datetime.now().isoformat(),
                "last_status": status,
            }],
        )
    except Exception:
        pass


def list_catalog_entries(repo_id: str | None = None) -> list[dict]:
    """All entries (id + document + metadata), optionally filtered to one
    repo. Used by `verify list`."""
    coll = get_test_catalog_collection()
    try:
        where = {"repo_id": repo_id} if repo_id else None
        res = coll.get(where=where, include=["documents", "metadatas"])
    except Exception:
        return []
    out = []
    docs  = res.get("documents") or []
    metas = res.get("metadatas") or []
    for i, eid in enumerate(res.get("ids", []) or []):
        out.append({
            "id":       eid,
            "document": docs[i]  if i < len(docs)  else "",
            "metadata": metas[i] if i < len(metas) else {},
        })
    return out


def query_tests_by_apis(apis: list[str], repo_id: str) -> list[dict]:
    """
    Deterministic impact selection: given a list of API specs
    (["POST /api/pets", ...]), return catalog entries whose
    `apis_covered` overlaps. ChromaDB doesn't do substring filters
    well, so we pull all entries for the repo and filter in Python.
    """
    if not apis:
        return []
    needle = set(apis)
    entries = list_catalog_entries(repo_id=repo_id)
    hits = []
    for e in entries:
        covered_raw = (e.get("metadata") or {}).get("apis_covered", "")
        covered = {c.strip() for c in covered_raw.split(",") if c.strip()}
        if covered & needle:
            hits.append(e)
    return hits


def path_overlap(a: str, b: str) -> bool:
    """True when two repo-relative paths point at the same file even if one
    side carries a different prefix depth (basename-suffix match)."""
    a, b = a.strip().strip("/"), b.strip().strip("/")
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def query_tests_by_files(changed_files: list[str], repo_id: str) -> list[dict]:
    """
    File→test impact selection: given repo-relative changed file paths,
    return catalog entries whose `source_files` overlaps. This is the
    direct "which tests verify this file" lookup — powers `verify impact`
    and `verify run --diff`.
    """
    if not changed_files:
        return []
    changed = [c.strip() for c in changed_files if c.strip()]
    hits = []
    for e in list_catalog_entries(repo_id=repo_id):
        src_raw = (e.get("metadata") or {}).get("source_files", "")
        srcs = [s.strip() for s in src_raw.split(",") if s.strip()]
        if any(path_overlap(s, c) for s in srcs for c in changed):
            hits.append(e)
    return hits


def query_tests_by_description(query_text: str, repo_id: str,
                               top_k: int = 5) -> list[dict]:
    """Semantic search over test_catalog documents. Strict repo-isolation."""
    coll = get_test_catalog_collection()
    try:
        total = coll.count()
        if total == 0:
            return []
        raw = coll.query(
            query_texts=[query_text],
            n_results=min(top_k, total),
            where={"repo_id": repo_id},
        )
        return _apply_time_decay(raw, origin="own")
    except Exception:
        return []


def delete_test_from_catalog(test_id: str) -> bool:
    """Used when `verify generate` regenerates an existing test (replaces)."""
    try:
        coll = get_test_catalog_collection()
        coll.delete(ids=[test_id])
        return True
    except Exception:
        return False


def clear_catalog(repo_id: str) -> int:
    """
    Delete every test_catalog entry for a repo. Returns the count removed.
    Used by `verify catalog clear` to wipe accumulated generations before a
    clean re-generate (stale / duplicate-coverage test files build up
    across runs otherwise).
    """
    try:
        coll = get_test_catalog_collection()
        ids = [e["id"] for e in list_catalog_entries(repo_id=repo_id) if e.get("id")]
        if ids:
            coll.delete(ids=ids)
        return len(ids)
    except Exception:
        return 0


# ===================================================================
# Two-phase repo-scoped retrieval.
# ===================================================================

def _merge_where(base_where: dict | None, extra: dict) -> dict:
    """ChromaDB requires explicit `$and` when combining filters."""
    if not base_where:
        return extra
    return {"$and": [base_where, extra]}


def _two_phase_query(
    collection,
    query_text: str,
    top_k: int,
    repo_id: str | None,
    base_where: dict | None = None,
) -> list[dict]:
    """
    Phase 1: own-repo (`repo_id == active`). Tagged origin='own'.
    Phase 2: cross-repo fallback if phase 1 didn't fill top_k. Tagged origin='cross'.
    When repo_id is None (shouldn't happen in normal flow — guards block before
    here — but defensive for memory tools that operate outside a session),
    just runs a single pass with no repo filter.
    """
    try:
        total = collection.count()
    except Exception:
        return []
    if total == 0:
        return []

    out: list[dict] = []

    if repo_id is None:
        try:
            raw = collection.query(
                query_texts=[query_text],
                n_results=min(top_k, total),
                where=base_where if base_where else None,
            )
            out.extend(_apply_time_decay(raw, origin="unscoped"))
        except Exception:
            return []
        _bump_access(collection, [r["id"] for r in out])
        return out

    # Phase 1: own-repo
    try:
        own_where = _merge_where(base_where, {"repo_id": repo_id})
        raw_own = collection.query(
            query_texts=[query_text],
            n_results=min(top_k, total),
            where=own_where,
        )
        out.extend(_apply_time_decay(raw_own, origin="own"))
    except Exception:
        pass

    # Phase 2: cross-repo fallback (only fill remaining slots)
    remaining = top_k - len(out)
    if remaining > 0:
        try:
            cross_where = _merge_where(base_where, {"repo_id": {"$ne": repo_id}})
            raw_cross = collection.query(
                query_texts=[query_text],
                n_results=min(remaining, total),
                where=cross_where,
            )
            out.extend(_apply_time_decay(raw_cross, origin="cross"))
        except Exception:
            pass

    # Bump access time on everything we returned. Best-effort — failure
    # never blocks the actual retrieval result.
    _bump_access(collection, [r["id"] for r in out])
    return out


def _bump_access(collection, ids: list[str]) -> None:
    """Update last_accessed_at on retrieved items. ChromaDB's update merges
    metadata, so we only need to send the changed key. Swallow any
    exception — observability/LRU is best-effort."""
    if not ids:
        return
    now = _now_ts()
    try:
        collection.update(
            ids=ids,
            metadatas=[{"last_accessed_at": now} for _ in ids],
        )
    except Exception:
        pass


def query_relevant_plans(query_text: str, top_k: int = 3,
                         repo_id: str | None = None) -> list[dict]:
    """Semantic search over planning_memory, repo-scoped + cross-repo fallback."""
    return _two_phase_query(
        get_planning_collection(),
        query_text=query_text,
        top_k=top_k,
        repo_id=repo_id,
    )


def query_relevant_memory(
    agent_name: str,
    query_text: str,
    top_k_findings: int = 5,
    top_k_corrections: int = 3,
    repo_id: str | None = None,
) -> dict:
    """
    Returns relevant findings (filtered to this agent) + corrections, each
    tagged with origin='own'/'cross'/'unscoped' for downstream rendering.
    """
    findings = _two_phase_query(
        get_findings_collection(),
        query_text=query_text,
        top_k=top_k_findings,
        repo_id=repo_id,
        base_where={"agent": agent_name},
    )
    corrections = _two_phase_query(
        get_corrections_collection(),
        query_text=query_text,
        top_k=top_k_corrections,
        repo_id=repo_id,
    )
    return {
        "relevant_findings":    findings,
        "relevant_corrections": corrections,
        "findings_count":       len(findings),
        "corrections_count":    len(corrections),
    }


def _apply_time_decay(results: dict, decay_rate: float = 0.05,
                      origin: str = "own") -> list[dict]:
    """
    Apply time decay to ChromaDB results.
    Older entries get lower effective scores.
    Nothing is deleted — decay makes old entries rank lower naturally.
    Items are tagged with `origin` so downstream rendering can show
    whether a hit came from the active repo or the cross-repo pool.
    """
    now = _now_ts()
    scored = []

    ids        = results.get("ids", [[]])[0]
    documents  = results.get("documents", [[]])[0]
    metadatas  = results.get("metadatas", [[]])[0]
    distances  = results.get("distances", [[]])[0]

    for i, doc_id in enumerate(ids):
        metadata  = metadatas[i] if i < len(metadatas) else {}
        distance  = distances[i] if i < len(distances) else 1.0
        timestamp = float(metadata.get("timestamp", now))

        days_ago  = (now - timestamp) / 86400
        decay     = math.exp(-decay_rate * days_ago)

        # lower distance = more similar; apply decay to similarity
        similarity = (1 - distance) * decay

        scored.append({
            "id":         doc_id,
            "document":   documents[i] if i < len(documents) else "",
            "metadata":   metadata,
            "similarity": similarity,
            "origin":     origin,
        })

    return sorted(scored, key=lambda x: x["similarity"], reverse=True)


# ===================================================================
# Prompt formatting — preserves origin markers.
# ===================================================================

def _origin_marker(item: dict) -> str:
    o = item.get("origin", "own")
    if o == "own":
        return "[own-repo]"
    if o == "cross":
        return "[cross-repo]"
    return ""


def format_memory_for_prompt(memory: dict) -> str:
    """
    Format retrieved memory into a compact string for prompt injection.
    `[own-repo]` / `[cross-repo]` markers let the LLM weight pools.
    Stays within ~800 token budget.
    """
    lines = []

    findings = memory.get("relevant_findings", [])
    corrections = memory.get("relevant_corrections", [])

    if findings:
        lines.append("Relevant findings from past reviews:")
        for f in findings[:5]:
            meta     = f.get("metadata", {})
            accepted = meta.get("accepted", "unknown")
            label    = "ACCEPTED" if accepted == "true" else "REJECTED"
            marker   = _origin_marker(f)
            lines.append(f"  {marker} [{label}] {f['document'][:200]}")
        lines.append("")

    if corrections:
        lines.append("Known corrections (do not repeat these mistakes):")
        for c in corrections[:3]:
            marker = _origin_marker(c)
            lines.append(f"  {marker} {c['document'][:200]}")
        lines.append("")

    if not lines:
        return "No relevant memory yet."

    lines.append(
        "[own-repo] = learned on this codebase; [cross-repo] = general "
        "engineering lesson from another project. Weight own-repo evidence "
        "more heavily when they conflict."
    )
    return "\n".join(lines)


def format_plans_for_prompt(plans: list[dict]) -> str:
    """Render past-build hits as a compact prompt-friendly block."""
    if not plans:
        return ""
    lines = ["## Past similar builds"]
    for p in plans[:3]:
        meta  = p.get("metadata", {}) or {}
        req   = meta.get("requirement", "")[:160]
        nc    = meta.get("node_count", "?")
        nt    = meta.get("node_types", "")
        clar  = "clarified first" if meta.get("needed_clarify") else "no clarify"
        edits = meta.get("edits_summary", "")
        edits_part = f"; user edits: {edits[:160]}" if edits else ""
        marker = _origin_marker(p)
        lines.append(f"- {marker} \"{req}\" → {clar}; {nc} nodes ({nt}){edits_part}")
    lines.append(
        "\nWhen the current requirement is similar to a past build, follow "
        "the same node mix — especially edits the user made — unless there "
        "is a clear reason not to. [own-repo] history outranks [cross-repo]."
    )
    return "\n".join(lines)


# ===================================================================
# Stats — optionally per-repo. Used by `memory stats` CLI.
# ===================================================================

def get_stats(repo_id: str | None = None) -> dict:
    """Counts per collection. If repo_id is given, restrict to that repo."""
    def _count(coll) -> int:
        try:
            if repo_id is None:
                return coll.count()
            # No native count-by-where in chromadb; use get() with ids only.
            res = coll.get(where={"repo_id": repo_id}, include=[])
            return len(res.get("ids", []))
        except Exception:
            return 0

    return {
        "findings_in_memory":    _count(get_findings_collection()),
        "corrections_in_memory": _count(get_corrections_collection()),
        "planning_in_memory":    _count(get_planning_collection()),
        "repo_id":               repo_id,
    }


def get_stats_by_repo() -> dict[str, dict]:
    """Group counts by repo_id across all 3 collections. Used by `memory stats`
    when no --repo arg is passed, and by `repo list` to show entry counts."""
    by_repo: dict[str, dict] = {}

    def _accumulate(coll, key: str) -> None:
        try:
            res = coll.get(include=["metadatas"])
            for m in res.get("metadatas", []) or []:
                rid = (m or {}).get("repo_id") or "_unknown"
                by_repo.setdefault(rid, {
                    "findings_in_memory":    0,
                    "corrections_in_memory": 0,
                    "planning_in_memory":    0,
                })
                by_repo[rid][key] += 1
        except Exception:
            pass

    _accumulate(get_findings_collection(),    "findings_in_memory")
    _accumulate(get_corrections_collection(), "corrections_in_memory")
    _accumulate(get_planning_collection(),    "planning_in_memory")
    return by_repo


# ===================================================================
# Maintenance helpers — used by `memory prune` and `memory compact`.
# ===================================================================

def list_entries(collection_name: str, repo_id: str | None = None) -> list[dict]:
    """Return all entries (id + document + metadata) from one collection,
    optionally filtered to a repo. Used by prune / compact / stats — not for
    hot retrieval paths."""
    coll = {
        "findings":     get_findings_collection(),
        "corrections":  get_corrections_collection(),
        "planning":     get_planning_collection(),
        "test_catalog": get_test_catalog_collection(),
    }.get(collection_name)
    if coll is None:
        raise ValueError(f"unknown collection: {collection_name}")

    try:
        where = {"repo_id": repo_id} if repo_id else None
        res = coll.get(where=where, include=["documents", "metadatas"])
    except Exception:
        return []

    out = []
    for i, eid in enumerate(res.get("ids", []) or []):
        out.append({
            "id":       eid,
            "document": (res.get("documents") or [None])[i] if i < len(res.get("documents") or []) else "",
            "metadata": (res.get("metadatas") or [None])[i] if i < len(res.get("metadatas") or []) else {},
        })
    return out


def delete_entries(collection_name: str, ids: list[str]) -> int:
    """Delete by id. Used by prune (LRU eviction) and compact (cluster
    consolidation). Returns count deleted."""
    if not ids:
        return 0
    coll = {
        "findings":     get_findings_collection(),
        "corrections":  get_corrections_collection(),
        "planning":     get_planning_collection(),
        "test_catalog": get_test_catalog_collection(),
    }.get(collection_name)
    if coll is None:
        raise ValueError(f"unknown collection: {collection_name}")
    try:
        coll.delete(ids=ids)
        return len(ids)
    except Exception:
        return 0


def delete_repo_entries(repo_id: str) -> dict[str, int]:
    """Wipe everything tagged with this repo_id across all 4 collections.
    Used by `repo remove` when the user opts to purge cached memory."""
    counts = {}
    for name in ("findings", "corrections", "planning", "test_catalog"):
        entries = list_entries(name, repo_id=repo_id)
        ids = [e["id"] for e in entries]
        counts[name] = delete_entries(name, ids)
    return counts
