import os
import math
import json
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


def get_client():
    global _client
    if _client is None:
        os.makedirs(CHROMA_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client


def get_findings_collection():
    global _findings_collection
    if _findings_collection is None:
        _findings_collection = get_client().get_or_create_collection(
            name="findings_memory",
            metadata={"hnsw:space": "cosine"}
        )
    return _findings_collection


def get_corrections_collection():
    global _corrections_collection
    if _corrections_collection is None:
        _corrections_collection = get_client().get_or_create_collection(
            name="corrections_memory",
            metadata={"hnsw:space": "cosine"}
        )
    return _corrections_collection


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
        return {
            "findings_in_memory":    findings_count,
            "corrections_in_memory": corrections_count,
        }
    except Exception:
        return {"findings_in_memory": 0, "corrections_in_memory": 0}
