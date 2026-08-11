"""provgraf MCP server — WARM access to the verified-facts bank.

A long-lived, LOCAL process (stdio): it keeps the Postgres pool and the RAG models
(mmlw + reranker) resident in memory, so queries — semantic ones included — are
instant instead of paying a 20 s cold start on every CLI invocation.

Read-only v1: list_facts / get_fact (with as-of `at`) / search / precedents / check.
Writes to the bank still go through the `provgraf add/revise/ingest` CLI — that is
where curation lives, along with the rule "only verified data from documents, after
the user signs off".

Run with: `uv run provgraf-mcp`  (or `python -m provgraf.mcp_server`).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

# The RAG models are already in the local HF cache — offline mode skips the network
# update checks (shorter cold start, works without internet). Set BEFORE importing
# sentence-transformers (which happens lazily in provgraf.embed).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from mcp.server.fastmcp import FastMCP

from provgraf import db, report
from provgraf.config import Settings

mcp = FastMCP("provgraf")
_settings = Settings()
_pool = None
_pool_lock = asyncio.Lock()

# workspace root used to resolve document paths (DANGLING-DOC check): four levels up from this file
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


async def _pool_get():
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await db.create_pool(_settings.database_url)
    return _pool


@mcp.tool()
async def list_facts(client: str, status: str | None = None) -> str:
    """List the bank's facts and documents for a client.

    client = owner slug (e.g. 'acme-housing').
    status (optional) = confirmed|disputed|to_confirm|resolved.
    """
    p = await _pool_get()
    rows = await db.list_all(p, owner=client, status=status)
    docs = [r for r in rows if r["kind"] == "document"]
    facts = [r for r in rows if r["kind"] != "document"]
    out = [f"BANK: {client}  ({len(docs)} docs / {len(facts)} facts)"]
    if docs:
        out.append("\nSOURCE DOCUMENTS:")
        out += [f"  [DOC] {r['qname']}  {r['label'] or ''}" for r in docs]
    out.append("\nFACTS:")
    for r in facts:
        u = f" {r['unit']}" if r["unit"] else ""
        src = ", ".join(r["sources"]) if r["sources"] else (r["issuer"] or "")
        st = "" if r["status"] in (None, "confirmed") else f" [{r['status']}]"
        out.append(f"  {r['qname']} = {r['val']}{u}{st}  <- {src}")
    return "\n".join(out)


@mcp.tool()
async def get_fact(qname: str, at: str | None = None, world_at: str | None = None) -> dict:
    """A single fact with full provenance: value, unit, status, sources (wasDerivedFrom), description.

    The bank is BITEMPORAL — two independent time axes:
      at       = 'YYYY-MM-DD': the BANK's state at the END of that day (transaction time, over the
                 valid_from/valid_to windows) — "what the bank knew then", e.g. on filing day.
      world_at = 'YYYY-MM-DD': the version in force IN THE WORLD that day (world time, over the
                 world_valid_from/world_valid_to windows) — e.g. the rent that applied in May.
    Both together = the full bitemporal question ("per what the bank knew on 15 June, what held in May")."""
    import datetime as dt

    def _ts(v: str):
        t = dt.datetime.fromisoformat(v)
        if len(v) == 10:
            t = t.replace(hour=23, minute=59, second=59, microsecond=999999)
        return t.astimezone() if t.tzinfo is None else t

    p = await _pool_get()
    if at or world_at:
        try:
            ts = _ts(at) if at else None
            wts = _ts(world_at) if world_at else None
        except ValueError as e:
            return {"error": f"date: {e} — expected YYYY-MM-DD or an ISO datetime"}
        row = await db.get_asof(p, qname, ts, wts)
        if row is None:
            return {"error": f"no version of '{qname}' (at={at}, world_at={world_at})"}
        val = row["val"]
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                pass
        return {
            "qname": row["qname"], "value": val, "unit": row["unit"],
            "label": row["label"], "status": row["status"],
            "provenance_class": row["provenance_class"], "sources": row["sources"],
            "valid_from": str(row["valid_from"]),
            "valid_to": str(row["valid_to"]) if row["valid_to"] else None,
            "world_valid_from": str(row["world_valid_from"]) if row["world_valid_from"] else None,
            "world_valid_to": str(row["world_valid_to"]) if row["world_valid_to"] else None,
            "historical": row["valid_to"] is not None,
        }
    async with p.acquire() as conn:
        e = await db.entity_full(conn, qname)
        if not e:
            return {"error": f"no current version of fact: {qname}"}
        srcs = await conn.fetch(
            "SELECT src.qname FROM relation r JOIN entity src ON src.id=r.object_id "
            "AND src.valid_to IS NULL WHERE r.predicate='wasDerivedFrom' AND r.subject_id=$1",
            e["id"],
        )
        gloss = await conn.fetchval("SELECT gloss FROM entity WHERE id=$1", e["id"])
    val = e["value"]
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            pass
    return {
        "qname": e["qname"],
        "value": val,
        "unit": e["unit"],
        "label": e["label"],
        "status": e["status"],
        "owner": e["owner"],
        "provenance_class": e["provenance_class"],
        "sources": [r["qname"] for r in srcs],
        "gloss": gloss,
    }


@mcp.tool()
async def search(query: str, client: str | None = None, k: int = 8, rerank: bool = True) -> list:
    """Semantic search over facts/documents (mmlw -> reranker).

    client = owner slug, or empty for everyone plus global. The models are loaded ONCE and
    kept warm — the first call after the server starts is slower, every later one is instant.
    """
    from provgraf import embed as emb

    p = await _pool_get()
    do_rerank = _settings.rerank and rerank
    qv = await asyncio.to_thread(emb.embed_query, query)
    n = max(k, _settings.rerank_candidates) if do_rerank else k
    rows = await db.search_embedding(p, qv, client or None, n)
    if not rows:
        return []
    if do_rerank:
        passages = [r["gloss"] or r["label"] or r["qname"] for r in rows]
        scores = await asyncio.to_thread(emb.rerank, query, passages)
        order = sorted(range(len(rows)), key=lambda i: scores[i], reverse=True)[:k]
        rows = [rows[i] for i in order]
    else:
        rows = rows[:k]
    return [
        {
            "qname": r["qname"],
            "kind": r["kind"],
            "provenance_class": r["provenance_class"],
            "value": r["val"],
            "unit": r["unit"],
            "label": r["label"],
            "sim": round(float(r["sim"]), 3),
            "source": r["zrodlo"],
        }
        for r in rows
    ]


@mcp.tool()
async def precedents(query: str, client: str | None = None, k: int = 5) -> list:
    """Decision precedents: the semantically closest EARLIER rulings (decision) and open
    structural questions. Run this BEFORE settling a new dilemma — if a similar one was
    already settled, reuse that rationale instead of asking from scratch."""
    from provgraf import embed as emb

    p = await _pool_get()
    qv = await asyncio.to_thread(emb.embed_query, query)
    rows = await db.search_embedding(p, qv, client or None, k, only_precedents=True)
    return [
        {
            "qname": r["qname"],
            "kind": r["kind"],
            "status": r["status"],
            "gloss": r["gloss"] or r["label"],
            "sim": round(float(r["sim"]), 3),
        }
        for r in rows
    ]


@mcp.tool()
async def check(client: str | None = None) -> str:
    """Integrity/freshness report: hard and soft staleness, overdue sources, disputed facts
    (with a recency hint), unresolved derivations, DANGLING-DOC and ORPHANED (a fact whose only
    source went missing). Same code as the CLI `provgraf check` — the report cannot drift.

    client (optional) = owner slug: narrows the documents and ADDS the INCOMPLETE section
    (required fields that are missing or unconfirmed)."""
    p = await _pool_get()
    r = await report.gather(p, client, Path(_REPO_ROOT))
    out = ["provgraf check REPORT:" + (f"  (client: {client})" if client else "")]

    def sec(title, rows, fmt):
        out.append(f"  {title}: {len(rows)}")
        out.extend(f"    - {fmt(x)}" for x in rows[:20])
        if len(rows) > 20:
            out.append(f"    … and {len(rows) - 20} more")

    sec("HARD-STALE (an input changed → recompute)", r.hard, lambda x: f"{x['qname']}  {x['label'] or ''}")
    sec("SOFT-STALE (depends on an overdue source)", r.soft, lambda x: f"{x['qname']}  {x['label'] or ''}")
    sec("OVERDUE (due for re-verification)", r.overdue, lambda x: f"{x['qname']}  last: {x['last_verified']}")
    sec("DISPUTED (conflicting sources)", r.disputed,
        lambda x: f"{x['qname']}  alternatives: {', '.join(x['alternates'] or [])}"
        + (f"  → suggestion: {r.suggestions[x['qname']][0]} ({r.suggestions[x['qname']][1]})"
           if x["qname"] in r.suggestions else ""))
    sec("UNRESOLVED DERIVATIONS", r.unresolved, lambda x: f"{x['qname']}  {x['label'] or ''}")
    if client:
        sec("INCOMPLETE (required field missing or unconfirmed)", r.incomplete, lambda x: f"{x[0]}  {x[1]}")
    sec("DANGLING-DOC (no file / file gone)", r.dangling, lambda x: f"{x[0]}  ({x[1]})")
    sec("ORPHANED (a dangling doc is the fact's ONLY source)", r.orphaned,
        lambda x: f"{x['qname']}  ⟵ {', '.join(x['lost_sources'])}")
    out.append(f"  TOTAL NEEDING ATTENTION: {r.total}" if r.total else "  ✓ clean")
    return "\n".join(out)


def _idle_unloader(idle_s: float) -> None:
    """Frees the RAG models after `idle_s` of inactivity — the daemon does not sit in RAM
    forever. The next search pays a reload (~10 s), which is a deliberate trade-off."""
    import time

    from provgraf import embed

    while True:
        time.sleep(60)
        if embed.last_used and time.time() - embed.last_used > idle_s:
            # deliberately NOT clearing last_used — overwriting it would race a concurrent
            # search (the fresh timestamp would be lost); unload() on empty dicts is cheap
            embed.unload()


def main() -> None:
    transport = os.environ.get("PROVGRAF_MCP_TRANSPORT", "stdio")
    if transport == "sse":
        import threading

        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = int(os.environ.get("PROVGRAF_MCP_PORT", "8399"))
        idle_s = float(os.environ.get("PROVGRAF_MODEL_IDLE_S", "1800"))
        threading.Thread(target=_idle_unloader, args=(idle_s,), daemon=True).start()
        mcp.run(transport="sse")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
