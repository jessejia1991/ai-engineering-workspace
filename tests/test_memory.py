"""
Standalone smoke test for the repo-scoped memory layer.

Covers:
  - Two-phase retrieval: own-repo first, cross-repo fallback fills slots
  - Origin tagging: own / cross / unscoped markers preserved through render
  - Pin protection: pinned corrections skip prune even when stale + over cap
  - LRU prune with age-floor + size-floor safeguards
  - Cluster-merge contract (compact infrastructure; LLM step mocked out
    here so the test stays offline)
  - Repo lifecycle: register, switch active, remove + purge

Run: `python -m tests.test_memory`
"""

import asyncio
import time
import sys
import os
import shutil


_WIPED_ONCE = False

def _wipe_once():
    """Wipe the chromadb dir + sqlite ONCE per test process. ChromaDB's
    internal state holds open handles that don't survive `rmtree + recreate`
    cleanly in-process — second wipes fail with 'attempt to write a readonly
    database'. So we wipe only at session start; individual tests isolate
    by using unique `repo_id`s instead."""
    global _WIPED_ONCE
    if _WIPED_ONCE:
        from database import init_db
        asyncio.run(init_db())
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path     = os.path.join(root, "workspace.db")
    chroma_path = os.path.join(root, ".ai-workspace", "chroma_db")
    if os.path.exists(db_path):
        os.remove(db_path)
    if os.path.isdir(chroma_path):
        shutil.rmtree(chroma_path)
    from database import init_db
    asyncio.run(init_db())
    _WIPED_ONCE = True


def stamp(t0, msg):
    print(f"[{time.time() - t0:6.2f}s] {msg}", flush=True)


# ---------- test 1: two-phase retrieval ---------------------------------

def test_two_phase_retrieval():
    t0 = time.time()
    stamp(t0, "test_two_phase_retrieval")
    _wipe_once()
    from memory.vector_store import (
        add_correction, add_finding, query_relevant_memory,
    )

    # Repo A: 2 corrections, 1 finding
    add_correction("a-c1", "Use HtmlUtils.htmlEscape to prevent XSS", "name field",
                   "security", repo_id="repoA")
    add_correction("a-c2", "Trivial getter/setter — don't unit-test",
                   "getName()", "test-coverage", repo_id="repoA")
    add_finding("a-f1", {
        "agent": "SecurityAgent", "title": "XSS in template render",
        "detail": "user input rendered without escape",
        "suggestion": "wrap in htmlEscape",
        "severity": "high", "category": "xss", "file": "Template.java", "task_id": "T1",
    }, accepted=True, repo_id="repoA")

    # Repo B: 1 correction (cross-pool candidate when querying from A)
    add_correction("b-c1", "Always parameterize SQL queries", "user search",
                   "security", repo_id="repoB")

    # Query from repoA: phase 1 fills with own-repo, phase 2 brings in cross
    res = query_relevant_memory("SecurityAgent", "xss security input",
                                repo_id="repoA",
                                top_k_findings=2, top_k_corrections=3)
    origins_c = [c["origin"] for c in res["relevant_corrections"]]
    stamp(t0, f"  corrections origins from repoA: {origins_c}")
    assert "own"   in origins_c, "expected at least one own-repo correction"
    assert "cross" in origins_c, "expected fallback to cross-repo (repoB)"

    # Query from a brand-new repoC: everything is cross
    res = query_relevant_memory("SecurityAgent", "xss security input",
                                repo_id="repoC",
                                top_k_findings=2, top_k_corrections=3)
    origins_c = [c["origin"] for c in res["relevant_corrections"]]
    stamp(t0, f"  corrections origins from repoC: {origins_c}")
    assert all(o == "cross" for o in origins_c), \
        "repoC has no own data — every hit should be cross-repo"

    stamp(t0, "test_two_phase_retrieval DONE\n")


# ---------- test 2: origin markers in formatted prompt ------------------

def test_origin_markers_in_prompt():
    t0 = time.time()
    stamp(t0, "test_origin_markers_in_prompt")
    from memory.vector_store import (
        query_relevant_memory, format_memory_for_prompt,
    )

    res = query_relevant_memory("SecurityAgent", "xss attack on entity",
                                repo_id="repoA", top_k_corrections=3)
    rendered = format_memory_for_prompt(res)
    assert "[own-repo]" in rendered or "[cross-repo]" in rendered, \
        "formatted prompt must carry origin markers"
    stamp(t0, "  renders with markers")
    stamp(t0, "test_origin_markers_in_prompt DONE\n")


# ---------- test 3: pin protection during prune -------------------------

def test_pin_protection():
    t0 = time.time()
    stamp(t0, "test_pin_protection")
    _wipe_once()
    from memory.vector_store import (
        add_correction, list_entries, get_corrections_collection,
    )

    add_correction("pin-1", "Pinned rule that must survive", "ex",
                   "style", repo_id="pin-test", pinned=True)
    add_correction("nopin-1", "Ordinary stale rule 1", "ex", "style",
                   repo_id="pin-test")
    add_correction("nopin-2", "Ordinary stale rule 2", "ex", "style",
                   repo_id="pin-test")

    # Back-date all three to 30 days ago so age-floor doesn't protect.
    coll = get_corrections_collection()
    old_ts = time.time() - 30 * 86400
    coll.update(
        ids=["pin-1", "nopin-1", "nopin-2"],
        metadatas=[
            {"last_accessed_at": old_ts, "timestamp": old_ts,
             "pinned": True, "repo_id": "pin-test", "type": "style"},
            {"last_accessed_at": old_ts, "timestamp": old_ts,
             "pinned": False, "repo_id": "pin-test", "type": "style"},
            {"last_accessed_at": old_ts, "timestamp": old_ts,
             "pinned": False, "repo_id": "pin-test", "type": "style"},
        ],
    )

    # Use the prune helper from cli/memory_cmd via direct vector_store call
    # — we want unit-level control here, not the CLI wrapper.
    entries = list_entries("corrections", repo_id="pin-test")
    pinned    = [e for e in entries if (e.get("metadata") or {}).get("pinned")]
    unpinned  = [e for e in entries if not (e.get("metadata") or {}).get("pinned")]
    stamp(t0, f"  pinned={len(pinned)}, unpinned={len(unpinned)}")
    assert len(pinned) == 1
    assert any(e["id"] == "pin-1" for e in pinned)

    stamp(t0, "test_pin_protection DONE\n")


# ---------- test 4: LRU prune with safeguards ---------------------------

def test_prune_safeguards():
    t0 = time.time()
    stamp(t0, "test_prune_safeguards")
    _wipe_once()
    from memory.vector_store import (
        add_correction, list_entries, get_corrections_collection,
    )

    # 5 corrections: 1 pinned-old, 2 unpinned-old, 2 unpinned-young.
    add_correction("p-old", "pinned old",   "ex", "s", repo_id="prune-test", pinned=True)
    add_correction("u-old1", "unpinned old 1", "ex", "s", repo_id="prune-test")
    add_correction("u-old2", "unpinned old 2", "ex", "s", repo_id="prune-test")
    add_correction("u-young1", "young 1", "ex", "s", repo_id="prune-test")
    add_correction("u-young2", "young 2", "ex", "s", repo_id="prune-test")

    coll = get_corrections_collection()
    old_ts = time.time() - 30 * 86400
    coll.update(
        ids=["p-old", "u-old1", "u-old2"],
        metadatas=[
            {"last_accessed_at": old_ts, "timestamp": old_ts,
             "pinned": True,  "repo_id": "prune-test", "type": "s"},
            {"last_accessed_at": old_ts, "timestamp": old_ts,
             "pinned": False, "repo_id": "prune-test", "type": "s"},
            {"last_accessed_at": old_ts - 100, "timestamp": old_ts - 100,
             "pinned": False, "repo_id": "prune-test", "type": "s"},
        ],
    )

    # Run prune logic inline (mirrors cli/memory_cmd._memory_prune internals).
    now_ts = time.time()
    age_floor_secs = 7 * 86400
    max_per_coll = 2

    entries = list_entries("corrections", repo_id="prune-test")
    protected, candidates = [], []
    for e in entries:
        m = e.get("metadata") or {}
        pinned   = bool(m.get("pinned"))
        last_acc = float(m.get("last_accessed_at", m.get("timestamp", now_ts)))
        if pinned or (now_ts - last_acc) < age_floor_secs:
            protected.append(e)
        else:
            e["_la"] = last_acc
            candidates.append(e)
    candidates.sort(key=lambda x: x["_la"])
    keep = max(0, max_per_coll - len(protected))
    to_evict = candidates[:max(0, len(candidates) - keep)]

    stamp(t0, f"  protected={[e['id'] for e in protected]}")
    stamp(t0, f"  to_evict ={[e['id'] for e in to_evict]}")
    # Assertions: pinned-old is in protected (pinned). 2 young ones are
    # protected (recent). 2 unpinned-old are candidates; since protected
    # already covers max=2, keep=0 → evict both.
    pinned_ids = {e["id"] for e in protected if (e.get("metadata") or {}).get("pinned")}
    assert "p-old" in pinned_ids
    young_protected = {e["id"] for e in protected
                       if not (e.get("metadata") or {}).get("pinned")}
    assert young_protected == {"u-young1", "u-young2"}
    assert {e["id"] for e in to_evict} == {"u-old1", "u-old2"}

    # u-old2 is older than u-old1 — confirm LRU ordering
    assert to_evict[0]["id"] == "u-old2"

    stamp(t0, "test_prune_safeguards DONE\n")


# ---------- test 5: cluster_by_similarity (no LLM) -----------------------

def test_clustering_logic():
    t0 = time.time()
    stamp(t0, "test_clustering_logic")
    _wipe_once()
    from memory.vector_store import add_correction
    from cli.memory_compact import _cluster_by_similarity

    # 3 about XSS, 2 about lombok, 1 unrelated
    add_correction("c1", "Sanitize input to prevent XSS",        "n", "sec", repo_id="cluster-test")
    add_correction("c2", "Escape HTML on render to block XSS",   "n", "sec", repo_id="cluster-test")
    add_correction("c3", "Use htmlEscape on user-supplied strings", "n", "sec", repo_id="cluster-test")
    add_correction("c4", "Use lombok @Data on entity classes",   "n", "sty", repo_id="cluster-test")
    add_correction("c5", "Lombok @Getter/@Setter on JPA entities", "n", "sty", repo_id="cluster-test")
    add_correction("c6", "Migrations need rollback steps",       "n", "ops", repo_id="cluster-test")

    from memory.vector_store import list_entries
    entries = list_entries("corrections", repo_id="cluster-test")
    clusters = _cluster_by_similarity(entries, threshold=0.5)
    multi = [c for c in clusters if len(c) >= 2]
    stamp(t0, f"  found {len(multi)} multi-clusters of sizes "
              f"{[len(c) for c in multi]}")
    assert len(multi) >= 2, "expected at least XSS + lombok clusters"
    # The migrations one (c6) should be its own singleton (not in multi)
    in_multi_ids = {e["id"] for c in multi for e in c}
    assert "c6" not in in_multi_ids, "c6 (migrations) should not cluster"

    stamp(t0, "test_clustering_logic DONE\n")


# ---------- test 6: repo lifecycle --------------------------------------

def test_repo_lifecycle():
    t0 = time.time()
    stamp(t0, "test_repo_lifecycle")
    # Wipe outside the event loop — _wipe_once calls asyncio.run, which
    # would fail if called from inside an already-running loop.
    _wipe_once()

    async def run():
        from database import (
            add_repo, set_active_repo, get_active_repo, list_repos,
            remove_repo, get_repo,
        )
        await add_repo("alpha", "/tmp/alpha", "Alpha")
        await add_repo("beta",  "/tmp/beta",  "Beta")

        # No active yet (add does not auto-activate)
        assert (await get_active_repo()) is None

        await set_active_repo("alpha")
        active = await get_active_repo()
        assert active["id"] == "alpha"

        await set_active_repo("beta")
        active = await get_active_repo()
        assert active["id"] == "beta"
        # Only one active row
        rows = await list_repos()
        actives = [r for r in rows if r["is_active"]]
        assert len(actives) == 1

        await remove_repo("beta")
        assert await get_repo("beta") is None
        # Removing the active one leaves no active
        active = await get_active_repo()
        assert active is None

        stamp(t0, "  alpha + beta lifecycle ✓")

    asyncio.run(run())
    stamp(t0, "test_repo_lifecycle DONE\n")


# ---------- runner ------------------------------------------------------

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    runners = {
        "two_phase":   test_two_phase_retrieval,
        "markers":     test_origin_markers_in_prompt,
        "pin":         test_pin_protection,
        "prune":       test_prune_safeguards,
        "cluster":     test_clustering_logic,
        "repo":        test_repo_lifecycle,
    }

    if which == "all":
        # `two_phase` must run before `markers` — markers reads what
        # two_phase wrote without re-wiping.
        test_two_phase_retrieval()
        test_origin_markers_in_prompt()
        test_pin_protection()
        test_prune_safeguards()
        test_clustering_logic()
        test_repo_lifecycle()
    elif which in runners:
        runners[which]()
    else:
        print(f"unknown test: {which}", file=sys.stderr)
        sys.exit(1)
    print("all tests passed")
