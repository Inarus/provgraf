"""As-of (temporal reads over the FR-070 windows): the version in force at a given instant.

Testing note: the `conn` fixture keeps everything inside ONE transaction, and `now()` in Postgres
returns the time that transaction started — supersede+insert would end up with identical windows.
That is why version windows are set explicitly (_set_window); in production every CLI invocation is
its own transaction.
"""
import datetime as dt
import json

from helpers import FakePool as _FakePool
from helpers import mk_doc, mk_source_fact

from provgraf import db, hashing

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
T1 = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
MID = dt.datetime(2026, 3, 15, tzinfo=dt.UTC)


async def _set_window(conn, eid, valid_from, valid_to=None):
    await conn.execute("UPDATE entity SET valid_from=$2, valid_to=$3 WHERE id=$1",
                       eid, valid_from, valid_to)


async def _revise(conn, qname, new_value, doc_id):
    """A minimal revision, as done by the CLI: supersede the old version + new version + Revision.
    Windows: old [T0, T1), new [T1, inf)."""
    old = await conn.fetchrow(
        "SELECT id FROM entity WHERE qname=$1 AND valid_to IS NULL", qname)
    await db.supersede(conn, qname)
    await _set_window(conn, old["id"], T0, T1)
    new_id = await db.insert_entity(
        conn, qname=qname, provenance_class="source", scope="client", owner="t-client",
        kind="fact", value=json.dumps(new_value), value_type="number",
        content_hash=hashing.content_hash(new_value), label=qname,
    )
    await _set_window(conn, new_id, T1)
    await db.insert_relation(conn, "wasDerivedFrom", subject_id=new_id, object_id=old["id"],
                             subtype="Revision")
    await db.insert_relation(conn, "wasDerivedFrom", subject_id=new_id, object_id=doc_id)
    return new_id


async def _mk_revised_fact(conn, doc):
    eid, _ = await mk_source_fact(conn, "t:rent", 100, doc)
    await _set_window(conn, eid, T0)
    await _revise(conn, "t:rent", 152, doc)


async def test_asof_returns_old_version_before_revision(conn):
    pool = _FakePool(conn)
    doc = await mk_doc(conn)
    await _mk_revised_fact(conn, doc)

    old = await db.get_asof(pool, "t:rent", MID)
    assert old is not None and old["val"] == "100"
    assert old["valid_to"] is not None  # a historical version

    current = await db.get_asof(pool, "t:rent", None)
    assert current["val"] == "152" and current["valid_to"] is None


async def test_asof_at_boundary_gets_new_version(conn):
    """Exactly at the instant of the revision the NEW version already applies (valid_from <= at, valid_to > at)."""
    pool = _FakePool(conn)
    doc = await mk_doc(conn)
    await _mk_revised_fact(conn, doc)
    assert (await db.get_asof(pool, "t:rent", T1))["val"] == "152"


async def test_asof_before_creation_is_none(conn):
    pool = _FakePool(conn)
    doc = await mk_doc(conn)
    await _mk_revised_fact(conn, doc)
    before = T0 - dt.timedelta(days=1)
    assert await db.get_asof(pool, "t:rent", before) is None


async def test_versions_lists_all_windows(conn):
    pool = _FakePool(conn)
    doc = await mk_doc(conn)
    await _mk_revised_fact(conn, doc)
    rows = await db.get_versions(pool, "t:rent")
    assert [r["val"] for r in rows] == ["100", "152"]
    assert rows[0]["valid_to"] is not None and rows[1]["valid_to"] is None


async def test_asof_provenance_of_historical_version(conn):
    """An old version shows ITS OWN sources (edges bind concrete ids, not qnames)."""
    pool = _FakePool(conn)
    doc = await mk_doc(conn, qname="t:doc")
    await _mk_revised_fact(conn, doc)
    old = await db.get_asof(pool, "t:rent", MID)
    assert "t:doc" in old["sources"]
