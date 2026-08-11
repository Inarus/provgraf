"""Agent write gating (Guru's "a foreign edit unverifies" pattern): a revision made by an
agent of kind=software enters the bank as `to_confirm` — a human approves it with `verify`.
The point: an agent may propose, but cannot silently change a verified fact."""
import json

from helpers import mk_doc

from provgraf import db, hashing


async def _seed(conn, value=27):
    doc = await mk_doc(conn, qname="t:doc.old")
    await mk_doc(conn, qname="t:doc.new")
    await db.upsert_agent(conn, "t:human", "person", "Human")
    await db.upsert_agent(conn, "t:bot", "software", "Software agent")
    eid = await db.insert_entity(
        conn, qname="t:rent", provenance_class="source", scope="client", owner="t-client",
        kind="fact", value=json.dumps(value), value_type="number", label="t:rent",
        content_hash=hashing.content_hash(value),
    )
    await db.insert_relation(conn, "wasDerivedFrom", subject_id=eid, object_id=doc)


async def _status(conn, qname="t:rent"):
    return await conn.fetchval(
        "SELECT status::text FROM entity WHERE qname=$1 AND valid_to IS NULL", qname)


async def test_software_agent_revision_lands_as_to_confirm(conn, monkeypatch):
    await _seed(conn)
    await _revise_in_tx(conn, monkeypatch, by="t:bot")
    assert await _status(conn) == "to_confirm"


async def test_human_revision_stays_confirmed(conn, monkeypatch):
    await _seed(conn)
    await _revise_in_tx(conn, monkeypatch, by="t:human")
    assert await _status(conn) == "confirmed"


async def test_explicit_status_wins_over_gating(conn, monkeypatch):
    await _seed(conn)
    await _revise_in_tx(conn, monkeypatch, by="t:bot", status="confirmed")
    assert await _status(conn) == "confirmed"


async def _revise_in_tx(conn, monkeypatch, by, status=None):
    """_revise opens its own pool — in tests we substitute a pool over the transactional conn."""
    from helpers import FakePool

    import provgraf.main as m

    async def _fake_pool(_url):
        pool = FakePool(conn)

        async def _close():
            return None

        pool.close = _close
        return pool

    monkeypatch.setattr(m.db, "create_pool", _fake_pool)
    await m._revise("t:rent", 30, "t:doc.new", None, by=by, status=status)
