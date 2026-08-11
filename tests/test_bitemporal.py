"""Bitemporality: two independent time axes.

  valid_from/valid_to    — the BANK's state (transaction time): what the bank knew when.
  world_valid_from/_to   — when the fact holds IN THE WORLD per its source document.

Testing note (same as test_asof): the `conn` fixture holds everything in ONE transaction and
Postgres `now()` returns that transaction's start time — so version windows are set explicitly.
"""
import datetime as dt
import json

from helpers import FakePool as _FakePool
from helpers import mk_doc

from provgraf import db, hashing

# bank axis (when we learned it)
BANK_T0 = dt.datetime(2026, 7, 10, tzinfo=dt.UTC)   # first write
BANK_T1 = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)    # backdated correction
BANK_MID = dt.datetime(2026, 7, 15, tzinfo=dt.UTC)  # "what the bank knew on 15 July"
# world axis (when it holds)
WORLD_FROM = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
WORLD_MAY = dt.datetime(2026, 5, 15, tzinfo=dt.UTC)
WORLD_JUN = dt.datetime(2026, 6, 15, tzinfo=dt.UTC)


async def _set_window(conn, eid, valid_from, valid_to=None):
    await conn.execute("UPDATE entity SET valid_from=$2, valid_to=$3 WHERE id=$1",
                       eid, valid_from, valid_to)


async def _fact(conn, qname, value, doc_id, world_from=None, world_to=None):
    eid = await db.insert_entity(
        conn, qname=qname, provenance_class="source", scope="client", owner="t-client",
        kind="fact", value=json.dumps(value), value_type="number", label=qname,
        content_hash=hashing.content_hash(value),
        world_valid_from=world_from, world_valid_to=world_to,
    )
    await db.insert_relation(conn, "wasDerivedFrom", subject_id=eid, object_id=doc_id)
    return eid


async def test_world_at_matches_only_inside_the_world_window(conn):
    """A fact written on 10 July that holds in the world from 1 June: --world-at 15 June yes, 15 May no."""
    pool = _FakePool(conn)
    doc = await mk_doc(conn)
    eid = await _fact(conn, "t:rent", 27, doc, world_from=WORLD_FROM)
    await _set_window(conn, eid, BANK_T0)

    assert await db.get_asof(pool, "t:rent", None, WORLD_JUN) is not None
    assert await db.get_asof(pool, "t:rent", None, WORLD_MAY) is None


async def test_backdated_correction_needs_both_axes(conn):
    """On 1 August the bank learns that since 1 June the value is 30 (not 27).

    - per the bank's state on 15 July (--at) → still 27 (the bank did not know the correction),
    - per today's bank state for world 15 June → 30.
    This question CANNOT be expressed with a single time axis — that is the point of bitemporality.
    """
    pool = _FakePool(conn)
    doc = await mk_doc(conn)
    old = await _fact(conn, "t:rent", 27, doc, world_from=WORLD_FROM)
    await _set_window(conn, old, BANK_T0, BANK_T1)           # the bank held this version 10 Jul → 1 Aug
    new = await _fact(conn, "t:rent", 30, doc, world_from=WORLD_FROM)
    await _set_window(conn, new, BANK_T1)                     # backdated correction, recorded 1 Aug

    bank_state_15_jul = await db.get_asof(pool, "t:rent", BANK_MID, WORLD_JUN)
    assert bank_state_15_jul["val"] == "27"

    bank_state_today = await db.get_asof(pool, "t:rent", None, WORLD_JUN)
    assert bank_state_today["val"] == "30"


async def test_null_world_time_does_not_break_existing_queries(conn):
    """Regression: facts without world-time (every pre-existing fact) behave as before —
    NULL bounds are open, so --world-at does not filter them out."""
    pool = _FakePool(conn)
    doc = await mk_doc(conn)
    eid = await _fact(conn, "t:legacy", 100, doc)             # no world-time
    await _set_window(conn, eid, BANK_T0)

    assert (await db.get_asof(pool, "t:legacy", None))["val"] == "100"
    assert (await db.get_asof(pool, "t:legacy", None, WORLD_MAY))["val"] == "100"
    assert (await db.get_asof(pool, "t:legacy", BANK_MID))["val"] == "100"
