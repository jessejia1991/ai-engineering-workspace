"""Focused chromadb / vector_store smoke test.

Reproduces runner.py's memory-retrieval step in isolation to determine
whether the hang is in chromadb init, vector_store, or upstream (asyncio).
"""
import asyncio
import time
import sys


def stamp(t0, msg):
    print(f"[{time.time() - t0:6.2f}s] {msg}", flush=True)


def test_sync_serial():
    """Mimics what runner.py now does: serial calls in main thread."""
    t0 = time.time()
    stamp(t0, "test_sync_serial: importing vector_store")
    from memory.vector_store import query_relevant_memory, get_stats

    stamp(t0, "stats before any query:")
    stamp(t0, f"  {get_stats()}")

    for name in ["SecurityAgent", "BugFindingAgent", "TestingAgent"]:
        stamp(t0, f"querying {name}...")
        r = query_relevant_memory(name, "test query about validation")
        stamp(t0, f"  -> findings={r['findings_count']}, corrections={r['corrections_count']}")

    stamp(t0, "stats after queries:")
    stamp(t0, f"  {get_stats()}")
    stamp(t0, "test_sync_serial DONE")


def test_async_main_thread():
    """Same calls but inside an async function (event loop), still serial."""
    t0 = time.time()

    async def runner():
        from memory.vector_store import query_relevant_memory
        for name in ["SecurityAgent", "BugFindingAgent", "TestingAgent"]:
            stamp(t0, f"async-main querying {name}...")
            r = query_relevant_memory(name, "test query about validation")
            stamp(t0, f"  -> findings={r['findings_count']}")

    stamp(t0, "test_async_main_thread: starting asyncio.run")
    asyncio.run(runner())
    stamp(t0, "test_async_main_thread DONE")


def test_add_then_query():
    """Add a finding + correction, then query — verify writes and reads."""
    t0 = time.time()
    stamp(t0, "test_add_then_query: adding 1 finding + 1 correction")
    from memory.vector_store import add_finding, add_correction, query_relevant_memory, get_stats

    add_finding("test-f1", {
        "agent":      "SecurityAgent",
        "severity":   "high",
        "category":   "input-validation",
        "title":      "Notes field accepts unbounded input",
        "detail":     "No length cap on notes field",
        "suggestion": "Add @Size(max=255)",
        "file":       "Visit.java",
        "task_id":    "TEST",
    }, accepted=True)

    add_correction("test-c1",
                   note="Don't flag missing validation in DTOs that are validated upstream",
                   example="VisitDto already validated by VisitController",
                   correction_type="false-positive")

    stamp(t0, f"  stats: {get_stats()}")

    r = query_relevant_memory("SecurityAgent", "missing input validation on entity field")
    stamp(t0, f"  query findings={r['findings_count']}, corrections={r['corrections_count']}")
    if r["relevant_findings"]:
        stamp(t0, f"  top finding: {r['relevant_findings'][0]['document'][:80]}")
    if r["relevant_corrections"]:
        stamp(t0, f"  top correction: {r['relevant_corrections'][0]['document'][:80]}")
    stamp(t0, "test_add_then_query DONE")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("sync", "all"):
        test_sync_serial()
        print()
    if which in ("async", "all"):
        test_async_main_thread()
        print()
    if which in ("write", "all"):
        test_add_then_query()
