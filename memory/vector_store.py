import os
import math
import json
import threading
import chromadb
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

CHROMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".ai-workspace", "chroma_db"
)

_client = None
_findings_collection = None
_corrections_collection = None
_planning_collection = None
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


def add_finding(finding_id: str, finding: dict, accepted: bool):
    """
    Store a finding in ChromaDB after human accept/reject.
    The document text is what gets embedded for semantic search.
    """
    collection = get_findings_collection()

    document = f"{finding.get('title', '')}. {finding.get('detail', '')}. {finding.get('suggestion', '')}"

    collection.add(
        ids=[finding_id],
        documents=[document],
        metadatas=[{
            "agent":        finding.get("agent", ""),
            "severity":     finding.get("severity", "low"),
            "category":     finding.get("category", ""),
            "accepted":     str(accepted).lower(),
            "file":         finding.get("file", "") or "",
            "task_id":      finding.get("task_id", ""),
            "timestamp":    datetime.now().timestamp(),
        }]
    )


def add_correction(correction_id: str, note: str, example: str,
                   correction_type: str):
    """
    Store a correction (LLM misunderstanding) in ChromaDB.
    Triggered when human rejects a finding with a reason.
    """
    collection = get_corrections_collection()

    document = f"{note}. Example: {example}"

    collection.add(
        ids=[correction_id],
        documents=[document],
        metadatas=[{
            "type":      correction_type,
            "timestamp": datetime.now().timestamp(),
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
):
    """
    Record an approved `build` invocation for future planner runs to
    retrieve. The document text is what gets embedded — make it rich
    enough that "add notes to Pet" can semantically hit "add notes to
    Visit" without exact-string overlap.
    """
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

    # ChromaDB metadata values must be primitives — flatten lists with commas.
    collection.add(
        ids=[plan_id],
        documents=[document],
        metadatas=[{
            "requirement":     requirement[:500],
            "needed_clarify":  bool(needed_clarify),
            "node_count":      int(node_count),
            "node_types":      ",".join(node_types),
            "edits_count":     len(edits),
            "edits_summary":   "; ".join(edits)[:500],
            "approved":        bool(approved),
            "timestamp":       datetime.now().timestamp(),
        }]
    )


def query_relevant_plans(query_text: str, top_k: int = 3) -> list[dict]:
    """
    Semantic search for prior approved builds similar to the current
    requirement. Returns top-K by similarity with time decay applied.
    Empty list on first call (planning_memory empty).
    """
    collection = get_planning_collection()
    try:
        total = collection.count()
        if total == 0:
            return []
        raw = collection.query(
            query_texts=[query_text],
            n_results=min(top_k, total),
        )
        return _apply_time_decay(raw)
    except Exception:
        return []


def format_plans_for_prompt(plans: list[dict]) -> str:
    """
    Render past-build hits as a compact prompt-friendly block. Returns
    empty string when there are no hits, so callers can guard with `if`.
    """
    if not plans:
        return ""
    lines = ["## Past similar builds in this repo"]
    for p in plans[:3]:
        meta  = p.get("metadata", {}) or {}
        req   = meta.get("requirement", "")[:160]
        nc    = meta.get("node_count", "?")
        nt    = meta.get("node_types", "")
        clar  = "clarified first" if meta.get("needed_clarify") else "no clarify"
        edits = meta.get("edits_summary", "")
        edits_part = f"; user edits: {edits[:160]}" if edits else ""
        lines.append(f"- \"{req}\" → {clar}; {nc} nodes ({nt}){edits_part}")
    lines.append(
        "\nWhen the current requirement is similar to a past build, follow "
        "the same node mix — especially edits the user made — unless there "
        "is a clear reason not to."
    )
    return "\n".join(lines)


def _apply_time_decay(results: dict, decay_rate: float = 0.05) -> list[dict]:
    """
    Apply time decay to ChromaDB results.
    Older entries get lower effective scores.
    Nothing is deleted — decay makes old entries rank lower naturally.
    """
    now = datetime.now().timestamp()
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
        })

    return sorted(scored, key=lambda x: x["similarity"], reverse=True)


def query_relevant_memory(
    agent_name: str,
    query_text: str,
    top_k_findings: int = 5,
    top_k_corrections: int = 3,
) -> dict:
    """
    Semantic search for relevant memory given current diff/context.
    Returns top-K findings and corrections by semantic similarity + time decay.
    """
    findings_col    = get_findings_collection()
    corrections_col = get_corrections_collection()

    # Query findings for this specific agent
    try:
        findings_count = findings_col.count()
        if findings_count > 0:
            raw_findings = findings_col.query(
                query_texts=[query_text],
                n_results=min(top_k_findings, findings_count),
                where={"agent": agent_name}
            )
            relevant_findings = _apply_time_decay(raw_findings)
        else:
            relevant_findings = []
    except Exception:
        relevant_findings = []

    # Query corrections (all agents — corrections are repo-wide)
    try:
        corrections_count = corrections_col.count()
        if corrections_count > 0:
            raw_corrections = corrections_col.query(
                query_texts=[query_text],
                n_results=min(top_k_corrections, corrections_count),
            )
            relevant_corrections = _apply_time_decay(raw_corrections)
        else:
            relevant_corrections = []
    except Exception:
        relevant_corrections = []

    return {
        "relevant_findings":    relevant_findings,
        "relevant_corrections": relevant_corrections,
        "findings_count":       len(relevant_findings),
        "corrections_count":    len(relevant_corrections),
    }


def format_memory_for_prompt(memory: dict) -> str:
    """
    Format retrieved memory into a compact string for prompt injection.
    Stays within ~800 token budget.
    """
    lines = []

    findings = memory.get("relevant_findings", [])
    corrections = memory.get("relevant_corrections", [])

    if findings:
        lines.append("Relevant findings from past reviews on this repo:")
        for f in findings[:5]:
            meta     = f.get("metadata", {})
            accepted = meta.get("accepted", "unknown")
            label    = "ACCEPTED" if accepted == "true" else "REJECTED"
            lines.append(f"  [{label}] {f['document'][:200]}")
        lines.append("")

    if corrections:
        lines.append("Known corrections about this codebase (do not repeat these mistakes):")
        for c in corrections[:3]:
            lines.append(f"  - {c['document'][:200]}")
        lines.append("")

    if not lines:
        return "No relevant memory yet."

    return "\n".join(lines)


def get_stats() -> dict:
    """Return basic stats about what's stored in memory."""
    try:
        findings_count    = get_findings_collection().count()
        corrections_count = get_corrections_collection().count()
        planning_count    = get_planning_collection().count()
        return {
            "findings_in_memory":    findings_count,
            "corrections_in_memory": corrections_count,
            "planning_in_memory":    planning_count,
        }
    except Exception:
        return {
            "findings_in_memory":    0,
            "corrections_in_memory": 0,
            "planning_in_memory":    0,
        }
