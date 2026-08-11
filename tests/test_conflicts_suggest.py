"""The resolution-suggestion heuristic (recency + issuer) — a pure function, no database
(+ one integration test for the disputed_facts_sources SQL at the bottom)."""
import json

from helpers import FakePool, mk_doc, mk_source_fact

from provgraf import conflicts as cf
from provgraf import db


def _m(qname, doc_date, issuer="acme:office", doc_qname="d"):
    return {"qname": qname, "doc_date": doc_date, "issuer": issuer, "doc_qname": doc_qname}


def test_newest_document_wins():
    got = cf.suggest([_m("a", "2026-01-10"), _m("b", "2026-05-12")])
    assert got is not None and got[0] == "b"
    assert "2026-05-12" in got[1] and "same issuer" in got[1]


def test_tie_gives_no_suggestion():
    assert cf.suggest([_m("a", "2026-05-12"), _m("b", "2026-05-12")]) is None


def test_single_dated_member_gives_no_suggestion():
    assert cf.suggest([_m("a", "2026-01-10"), _m("b", None)]) is None


def test_no_dates_gives_no_suggestion():
    assert cf.suggest([_m("a", None), _m("b", None)]) is None


def test_different_issuers_flagged():
    got = cf.suggest([_m("a", "2026-01-10", issuer="municipality"), _m("b", "2026-05-12", issuer="agency")])
    assert got[0] == "b" and "different issuers" in got[1]


def test_multiple_docs_per_fact_takes_newest():
    got = cf.suggest([_m("a", "2026-01-10"), _m("a", "2026-06-01"), _m("b", "2026-03-01")])
    assert got[0] == "a" and "2026-06-01" in got[1]


def test_bad_date_treated_as_missing():
    assert cf.suggest([_m("a", "not-a-date"), _m("b", "2026-05-12")]) is None


async def test_disputed_facts_sources_sql_and_end_to_end(conn):
    """The SQL against the real schema + the full path: disputed pair -> group -> recency suggestion."""
    old = await mk_doc(conn, qname="t:doc.old")
    new = await mk_doc(conn, qname="t:doc.new")
    await conn.execute("UPDATE entity SET value=$2::jsonb WHERE id=$1",
                       old, json.dumps({"date": "2026-01-10"}))
    await conn.execute("UPDATE entity SET value=$2::jsonb WHERE id=$1",
                       new, json.dumps({"date": "2026-05-12"}))
    a, _ = await mk_source_fact(conn, "t:deposit.v1", 6, old, status="disputed")
    b, _ = await mk_source_fact(conn, "t:deposit.v2", 2, new, status="disputed")
    await db.insert_relation(conn, "alternateOf", subject_id=a, object_id=b)

    pool = FakePool(conn)
    sources = await db.disputed_facts_sources(pool)
    dates = {r["qname"]: r["doc_date"] for r in sources}
    assert dates == {"t:deposit.v1": "2026-01-10", "t:deposit.v2": "2026-05-12"}

    groups = cf.group_disputed(await db.disputed_rows(pool), sources)
    assert len(groups) == 1
    pick, reason = groups[0]["suggestion"]
    assert pick == "t:deposit.v2" and "2026-05-12" in reason


def test_group_disputed_dedups_pairs():
    disputed = [
        {"qname": "a", "label": "", "alternates": ["b"]},
        {"qname": "b", "label": "", "alternates": ["a"]},
    ]
    sources = [_m("a", "2026-01-10"), _m("b", "2026-05-12")]
    groups = cf.group_disputed(disputed, sources)
    assert len(groups) == 1
    assert groups[0]["suggestion"][0] == "b"
