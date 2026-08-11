"""The `check` report has ONE source of truth (report.gather) — the CLI and MCP only render it.

This test pins the two things that actually drifted: a document with no file path (the
agent-facing copy skipped it) and an orphaned fact. Plus the basics: a changed input lands in
hard-stale, and a fact with a second live source is NOT an orphan.
"""
import json

from helpers import FakePool, mk_derivation, mk_doc, mk_source_fact

from provgraf import db, hashing, report


async def test_document_without_a_file_is_dangling_and_its_only_fact_is_orphaned(conn):
    pool = FakePool(conn)
    doc = await mk_doc(conn, qname="t:src.phonecall")        # verbal source — no file
    await mk_source_fact(conn, "t:rent", 25, doc)

    r = await report.gather(pool, "t-client")

    assert [q for q, _ in r.dangling] == ["t:src.phonecall"]
    assert [x["qname"] for x in r.orphaned] == ["t:rent"]
    assert r.total == 2


async def test_fact_with_a_second_live_source_is_not_orphaned(conn):
    pool = FakePool(conn)
    verbal = await mk_doc(conn, qname="t:src.phonecall")
    resolution = await mk_doc(conn, qname="t:src.resolution")
    await conn.execute("UPDATE entity SET value=$2::jsonb WHERE id=$1",
                       resolution, json.dumps({"file": "README.md", "date": None}))
    eid, _ = await mk_source_fact(conn, "t:rent", 25, verbal)
    await db.insert_relation(conn, "wasDerivedFrom", subject_id=eid, object_id=resolution)

    r = await report.gather(pool, "t-client")

    assert "t:src.phonecall" in [q for q, _ in r.dangling]
    assert r.orphaned == []       # the resolution still backs it — provenance is not at risk


async def test_a_changed_source_lands_in_hard_stale(conn):
    pool = FakePool(conn)
    doc = await mk_doc(conn, qname="t:src.datasheet")
    await conn.execute("UPDATE entity SET value=$2::jsonb WHERE id=$1",
                       doc, json.dumps({"file": "README.md", "date": None}))
    a, ah = await mk_source_fact(conn, "t:a", 10, doc)
    await mk_derivation(conn, "t:total", 10, [(a, "t:a", ah)])
    await conn.execute("UPDATE entity SET value=$2::jsonb, content_hash=$3 WHERE id=$1",
                       a, json.dumps(99), hashing.content_hash(99))

    r = await report.gather(pool, "t-client")

    assert [x["qname"] for x in r.hard] == ["t:total"]
    assert r.dangling == []       # the document's file exists
