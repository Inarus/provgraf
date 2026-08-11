"""Helpers for building small graphs in tests."""
import json

from provgraf import db, hashing


class FakePool:
    """The read-* functions take a pool; in tests we wrap the transactional conn (rolled back afterwards)."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


async def mk_doc(conn, qname="t:doc", owner="t-client"):
    aid = await db.upsert_agent(conn, "t:agent", "organization", "Test")
    return await db.insert_entity(
        conn, qname=qname, provenance_class="source", kind="document", scope="client",
        owner=owner, attributed_to=aid, content_hash=hashing.content_hash({"d": 1}), label=qname,
    )


async def mk_source_fact(conn, qname, value, doc_id, owner="t-client", status="confirmed"):
    """A source fact + wasDerivedFrom->doc. Returns (id, content_hash)."""
    ch = hashing.content_hash(value)
    eid = await db.insert_entity(
        conn, qname=qname, provenance_class="source", status=status, scope="client",
        owner=owner, value=json.dumps(value), value_type="number", content_hash=ch, label=qname,
    )
    await db.insert_relation(conn, "wasDerivedFrom", subject_id=eid, object_id=doc_id)
    return eid, ch


async def mk_derivation(conn, qname, value, inputs, owner="t-client"):
    """A derivation with wasDerivedFrom to every input + inputs_hash. inputs=[(id,qname,ch)]."""
    ih = hashing.inputs_hash([(q, ch, False) for _, q, ch in inputs])
    act = await db.insert_activity(conn, f"t:act:{qname}", "derivation", formula="test")
    did = await db.insert_entity(
        conn, qname=qname, provenance_class="derivation", scope="client", owner=owner,
        value=json.dumps(value), value_type="number", inputs_hash=ih, generated_by=act,
        content_hash=hashing.content_hash(value), label=qname,
    )
    for eid, _, _ in inputs:
        await db.insert_relation(conn, "wasDerivedFrom", subject_id=did, object_id=eid,
                                 activity_id=act)
    return did, ih
