"""Staleness (SM-1): revising a source cascades to its dependants (direct + transitive)."""
from helpers import mk_derivation, mk_doc, mk_source_fact

from provgraf import db, hashing

_PROP_SQL = """
WITH RECURSIVE seed AS (
    SELECT e.id AS node FROM entity e
    WHERE e.provenance_class='derivation' AND e.valid_to IS NULL
      AND e.inputs_hash IS DISTINCT FROM provgraf_current_inputs_hash(e.id)
),
prop AS (
    SELECT node, ARRAY[node] AS path FROM seed
    UNION ALL
    SELECT r.subject_id, prop.path||r.subject_id
    FROM prop JOIN relation r ON r.predicate='wasDerivedFrom' AND r.object_id=prop.node
    WHERE NOT r.subject_id = ANY(prop.path)
)
SELECT DISTINCT e.qname FROM prop JOIN entity e ON e.id=prop.node WHERE e.valid_to IS NULL
"""


async def _hard(conn):
    rows = await conn.fetch(_PROP_SQL)
    return {r["qname"] for r in rows}


async def test_no_stale_initially(conn):
    doc = await mk_doc(conn)
    f, hf = await mk_source_fact(conn, "t:f", 100, doc)
    await mk_derivation(conn, "t:d", 100, [(f, "t:f", hf)])
    assert "t:d" not in await _hard(conn)


async def test_cascade_two_levels(conn):
    doc = await mk_doc(conn)
    f, hf = await mk_source_fact(conn, "t:f", 100, doc)
    d, _ = await mk_derivation(conn, "t:d", 100, [(f, "t:f", hf)])
    # level 2: d2 <- d
    hd = hashing.content_hash(100)
    await mk_derivation(conn, "t:d2", 100, [(d, "t:d", hd)])

    assert await _hard(conn) == set()  # clean

    # revise source f: close the old version (as revise/FR-070 does)
    await db.supersede(conn, "t:f")

    hard = await _hard(conn)
    assert "t:d" in hard, "the direct derivation should be stale"
    assert "t:d2" in hard, "the transitive one (level 2) should be stale — object->subject propagation"
