"""
`memory compact` — LLM-driven cluster merge of corrections.

Phoenix / LangSmith / Braintrust use the same shape for their playground
features: load a thing, propose a transformation, human approves per item.
This is the memory-side analogue:

  1. Pull all corrections (optionally filtered to one repo).
  2. Embed-and-cluster by cosine similarity (threshold 0.85 default).
  3. For each cluster of >= 2 items, ask the LLM to produce one merged
     correction that preserves all distinct information.
  4. Show the cluster + merged preview; human accepts (y) / skips (N) /
     quits (q). On accept, delete originals and add the merged item.

Safety:
  - Per-cluster confirm — never bulk-applied.
  - Pinned entries are excluded from clustering (the user has signalled
    they should remain as-is, not be transformed).
  - `q` stops the loop but commits already-accepted merges (nothing rolls
    back). Acceptable since each accept is human-gated.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional
from datetime import datetime

import numpy as np
from rich.console import Console
from rich.panel import Panel
from rich import box

from agents.llm_client import client as llm_client, set_trace_context
from database import init_db, get_active_repo
from memory.vector_store import (
    list_entries, delete_entries,
    add_correction,
    get_corrections_collection,
)


console = Console()

# Calibrated against ChromaDB's default ONNX MiniLM embedder
# (all-MiniLM-L6-v2). Semantically equivalent corrections typically score
# 0.5–0.7 with this model. The Langfuse/LangSmith style 0.85 default
# assumes higher-fidelity embedders; on MiniLM that's too aggressive.
DEFAULT_SIMILARITY_THRESHOLD = 0.5
COMPACT_MODEL_DEFAULT = "claude-sonnet-4-6"


async def run_compact(rest: list[str]) -> None:
    """Entry point — `memory compact ...`."""
    # Local arg parsing — same shape as memory_cmd._parse_kv_args but kept
    # local so this file is self-contained.
    opts: dict = {}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--repo" and i + 1 < len(rest):
            opts["repo"] = rest[i + 1]; i += 2
        elif a == "--threshold" and i + 1 < len(rest):
            opts["threshold"] = float(rest[i + 1]); i += 2
        elif a == "--dry-run":
            opts["dry_run"] = True; i += 1
        elif a == "--auto-yes":
            opts["auto_yes"] = True; i += 1
        else:
            console.print(f"[red]Unknown flag: {a}[/red]"); return

    await init_db()

    repo_id = opts.get("repo")
    if not repo_id:
        active = await get_active_repo()
        if active:
            repo_id = active["id"]
    threshold = opts.get("threshold", DEFAULT_SIMILARITY_THRESHOLD)
    dry_run   = bool(opts.get("dry_run", False))
    auto_yes  = bool(opts.get("auto_yes", False))

    # 1. Load corrections (filtered by repo if active)
    entries = list_entries("corrections", repo_id=repo_id)
    # Exclude pinned — those are intentionally frozen.
    entries = [e for e in entries if not (e.get("metadata") or {}).get("pinned")]

    if len(entries) < 2:
        console.print(
            f"[dim]Not enough corrections to compact "
            f"(found {len(entries)}, need ≥ 2 unpinned).[/dim]"
        )
        return

    console.print(Panel.fit(
        f"[bold]memory compact[/bold]  "
        f"repo={repo_id or 'ALL'} · {len(entries)} unpinned corrections · "
        f"similarity ≥ {threshold} · "
        f"{'[yellow]DRY RUN[/yellow]' if dry_run else '[green]LIVE[/green]'}",
        border_style="blue",
    ))

    # 2. Cluster by embedding similarity. Use ChromaDB's own embeddings.
    clusters = _cluster_by_similarity(entries, threshold)
    multi_clusters = [c for c in clusters if len(c) >= 2]

    if not multi_clusters:
        console.print(
            f"[dim]No clusters of ≥ 2 found at threshold {threshold}. "
            f"Try a lower threshold (e.g. --threshold 0.75).[/dim]"
        )
        return

    console.print(
        f"[bold]{len(multi_clusters)} cluster(s)[/bold] "
        f"covering [bold]{sum(len(c) for c in multi_clusters)}[/bold] entries "
        f"(out of {len(entries)} unpinned).\n"
    )

    # 3. Per-cluster LLM merge + human gate
    merged_count = 0
    skipped_count = 0
    for idx, cluster in enumerate(multi_clusters, 1):
        console.print(Panel(
            "\n".join(f"  [dim]{i+1}.[/dim] {e['document'][:200]}"
                      for i, e in enumerate(cluster)),
            title=f"Cluster {idx} of {len(multi_clusters)} ({len(cluster)} items)",
            border_style="cyan",
        ))

        with console.status("[bold blue]Asking LLM to merge...[/bold blue]"):
            try:
                merged_text = await _llm_merge(cluster, repo_id=repo_id)
            except Exception as e:
                console.print(f"[red]LLM merge failed: {e}[/red]")
                skipped_count += 1
                continue

        console.print(Panel(
            merged_text,
            title="Proposed merged correction",
            border_style="green",
        ))

        # Decide
        if dry_run:
            console.print("[yellow]Dry-run — would commit if --dry-run dropped.[/yellow]\n")
            merged_count += 1
            continue

        if auto_yes:
            decision = "y"
            console.print("[dim](auto-yes)[/dim]")
        else:
            try:
                decision = console.input("Commit this merge? [y/N/q]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Cancelled.[/dim]")
                break

        if decision == "q":
            console.print("[dim]Quit — leaving remaining clusters as-is.[/dim]")
            break
        if decision != "y":
            console.print("[dim]Skipped.[/dim]\n")
            skipped_count += 1
            continue

        # Commit: write merged, then delete originals.
        new_id = "merged-" + uuid.uuid4().hex[:8]
        old_ids = [e["id"] for e in cluster]
        try:
            # Inherit `type` from first cluster member; downstream callers don't
            # care about the exact type for corrections, but keep it consistent.
            first_meta = cluster[0].get("metadata") or {}
            add_correction(
                correction_id=new_id,
                note=merged_text,
                example=f"compacted from {len(cluster)} entries: {', '.join(old_ids)}",
                correction_type=first_meta.get("type", "compact"),
                repo_id=first_meta.get("repo_id") or (repo_id or "_unknown"),
                pinned=False,
            )
            n_deleted = delete_entries("corrections", old_ids)
            console.print(
                f"[green]✓ Merged into {new_id}; "
                f"deleted {n_deleted} originals.[/green]\n"
            )
            merged_count += 1
        except Exception as e:
            console.print(f"[red]Commit failed: {e}[/red]\n")
            skipped_count += 1

    # 4. Summary
    console.print(Panel.fit(
        f"[bold]compact done[/bold]   "
        f"[green]{merged_count} cluster(s) merged[/green] · "
        f"[dim]{skipped_count} skipped[/dim]" +
        ("  [yellow](dry-run, no changes applied)[/yellow]" if dry_run else ""),
        border_style="blue",
    ))


# ---------- clustering --------------------------------------------------

def _cluster_by_similarity(entries: list[dict], threshold: float) -> list[list[dict]]:
    """
    Simple greedy single-link clustering by cosine similarity on the entries'
    own ChromaDB embeddings. Cheap and good enough for tens-to-low-hundreds
    of corrections; for larger pools an HDBSCAN-style approach would scale
    better, but isn't worth the dependency at this stage.
    """
    ids = [e["id"] for e in entries]
    try:
        coll = get_corrections_collection()
        got = coll.get(ids=ids, include=["embeddings"])
        raw_embs = got.get("embeddings")
    except Exception:
        return [[e] for e in entries]

    # ChromaDB returns embeddings as a numpy array (truthiness on multi-element
    # arrays raises, so we can't use `... or []`). None or empty → bail out.
    if raw_embs is None or len(raw_embs) == 0:
        return [[e] for e in entries]

    embs = np.array(raw_embs, dtype=np.float32)
    # Re-order to match `entries`: chromadb.get(ids=...) returns in the
    # order it pleases; build a lookup by id.
    got_ids = got.get("ids", []) or []
    id_to_idx = {gid: gi for gi, gid in enumerate(got_ids)}
    rebuilt = np.zeros_like(embs)
    for new_idx, e in enumerate(entries):
        src = id_to_idx.get(e["id"])
        if src is None:
            rebuilt[new_idx] = embs[0] * 0  # zero vector → no similarity
        else:
            rebuilt[new_idx] = embs[src]
    embs = rebuilt

    # Normalize for cosine sim
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    embs = embs / norms

    n = len(entries)
    sim = embs @ embs.T  # n×n cosine sim matrix

    # Greedy clustering: walk pairs in descending similarity, union-find
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Iterate upper triangle, only merge above threshold
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i, j])
            if s >= threshold:
                pairs.append((s, i, j))
    pairs.sort(reverse=True)
    for _, i, j in pairs:
        union(i, j)

    # Bucket by root
    buckets: dict[int, list[dict]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(entries[i])
    return list(buckets.values())


# ---------- LLM merge ---------------------------------------------------

_MERGE_PROMPT_TEMPLATE = """You are helping a code-review system compact its
memory pool. The following {n} correction notes are semantically similar
and should be merged into ONE consolidated note. The merged note must
preserve every distinct piece of information from the originals.

Originals (each was an independent rejection rationale a human wrote
during code review):
{originals}

Write the merged correction note. Rules:
- Keep it short (≤ 2 sentences usually).
- Preserve every concrete clue (file paths, class names, language-specific
  conventions, specific anti-patterns) that appears in any original.
- Drop redundant phrasing.
- Output ONLY the merged note text — no preamble, no JSON, no quotes.
"""


async def _llm_merge(cluster: list[dict], repo_id: Optional[str]) -> str:
    """Single LLM call per cluster. Tagged in the observation stream as
    agent_name='MemoryCompactor' so the trace tree shows the work."""
    originals_block = "\n".join(
        f"  ({i+1}) {e['document']}" for i, e in enumerate(cluster)
    )
    prompt = _MERGE_PROMPT_TEMPLATE.format(
        n=len(cluster), originals=originals_block,
    )

    # Trace tag — `memory compact` doesn't run under a review/build trace,
    # so we give it its own per-invocation trace id. Reusing repo_id in the
    # trace_id makes it easy to find later.
    trace_id = f"compact-{(repo_id or 'all')}-{uuid.uuid4().hex[:6]}"
    set_trace_context(trace_id=trace_id, agent_name="MemoryCompactor")

    response = await llm_client.messages.create(
        model=COMPACT_MODEL_DEFAULT,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
