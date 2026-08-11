"""Integrity: INV-2..5 + the INV-1 helper. Every barrier MUST actually block."""
import json

import asyncpg
import pytest
from helpers import mk_doc, mk_source_fact

from provgraf import db


async def test_dag_cycle_rejected(conn):
    doc = await mk_doc(conn)
    a, _ = await mk_source_fact(conn, "t:a", 1, doc)
    b, _ = await mk_source_fact(conn, "t:b", 2, doc)
    await db.insert_relation(conn, "wasDerivedFrom", subject_id=a, object_id=b)  # a <- b
    with pytest.raises(asyncpg.PostgresError):  # b <- a closes the cycle -> INV-5 trigger
        await db.insert_relation(conn, "wasDerivedFrom", subject_id=b, object_id=a)


async def test_duplicate_edge_rejected(conn):
    doc = await mk_doc(conn)
    a, _ = await mk_source_fact(conn, "t:a", 1, doc)
    b, _ = await mk_source_fact(conn, "t:b", 2, doc)
    await db.insert_relation(conn, "alternateOf", subject_id=a, object_id=b)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.insert_relation(conn, "alternateOf", subject_id=a, object_id=b)


async def test_self_edge_rejected(conn):
    doc = await mk_doc(conn)
    a, _ = await mk_source_fact(conn, "t:a", 1, doc)
    with pytest.raises(asyncpg.PostgresError):
        await db.insert_relation(conn, "alternateOf", subject_id=a, object_id=a)


async def test_derivation_without_inputs_hash_rejected(conn):
    with pytest.raises(asyncpg.PostgresError):  # CHECK inv4_derivation_hash
        await db.insert_entity(
            conn, qname="t:d", provenance_class="derivation", scope="client",
            owner="t-client", content_hash="x",
        )


async def test_source_with_inputs_hash_rejected(conn):
    with pytest.raises(asyncpg.PostgresError):  # CHECK inputs_hash_only_deriv
        await db.insert_entity(
            conn, qname="t:s", provenance_class="source", scope="client",
            owner="t-client", content_hash="x", inputs_hash="zzz",
        )


async def test_scope_global_requires_null_owner(conn):
    with pytest.raises(asyncpg.PostgresError):  # CHECK scope_owner_consistency
        await db.insert_entity(
            conn, qname="t:g", provenance_class="source", scope="global",
            owner="t-client", content_hash="x", attributed_to=None,
        )


async def test_inv1_bare_fact_detected(conn):
    """A fact with no provenance edge -> entity_provenance_count==0 (the CLI would reject it)."""
    eid = await db.insert_entity(
        conn, qname="t:bare", provenance_class="source", scope="client",
        owner="t-client", value=json.dumps(1), content_hash="x",
    )
    assert await db.entity_provenance_count(conn, eid) == 0
