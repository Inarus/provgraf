"""Hashing: determinism + the CRITICAL Python <-> SQL parity (otherwise staleness yields false negatives)."""
from helpers import mk_derivation, mk_doc, mk_source_fact

from provgraf import hashing


def test_content_hash_deterministic():
    assert hashing.content_hash(152) == hashing.content_hash(152)
    assert hashing.content_hash("a") != hashing.content_hash("b")


def test_inputs_hash_order_independent():
    a = [("acme:x", "h1", False), ("acme:y", "h2", False)]
    b = [("acme:y", "h2", False), ("acme:x", "h1", False)]
    assert hashing.inputs_hash(a) == hashing.inputs_hash(b)


def test_inputs_hash_superseded_changes():
    base = [("acme:x", "h1", False)]
    sup = [("acme:x", "h1", True)]
    assert hashing.inputs_hash(base) != hashing.inputs_hash(sup)


async def test_inputs_hash_parity_python_sql(conn):
    """Python hashing.inputs_hash == SQL provgraf_current_inputs_hash (byte for byte)."""
    doc = await mk_doc(conn)
    f1, h1 = await mk_source_fact(conn, "t:f1", 10, doc)
    f2, h2 = await mk_source_fact(conn, "t:f2", 20, doc)
    did, ih = await mk_derivation(conn, "t:d", 30, [(f1, "t:f1", h1), (f2, "t:f2", h2)])
    sql_ih = await conn.fetchval("SELECT provgraf_current_inputs_hash($1)", did)
    assert sql_ih == ih
