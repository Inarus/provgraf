"""PROV-JSON export checked against the REFERENCE W3C implementation (`prov`).

This is deliberately a test and not a README claim: the export must deserialize cleanly with
the library the standard's own tooling is built on. Note the scope — clean deserialization
proves the serialization is well-formed PROV-JSON; it is NOT a PROV-CONSTRAINTS conformance
check (typing, causality loops, provenance going backwards in time), which needs a validator.
"""
import json

import pytest
from helpers import FakePool, mk_derivation, mk_doc, mk_source_fact

from provgraf import viz

prov = pytest.importorskip("prov.model", reason="dev group not installed (uv sync --group dev)")


async def test_prov_json_export_deserializes_with_reference_library(conn):
    pool = FakePool(conn)
    doc = await mk_doc(conn, qname="t:src.doc")
    a, ah = await mk_source_fact(conn, "t:a", 10, doc)
    b, bh = await mk_source_fact(conn, "t:b", 32, doc)
    await mk_derivation(conn, "t:total", 42, [(a, "t:a", ah), (b, "t:b", bh)])

    exported = await viz.prov_json(pool, "t-client")

    # round-trip through the reference implementation: raises on malformed PROV-JSON
    document = prov.ProvDocument.deserialize(content=json.dumps(exported))
    qnames = {str(r.identifier) for r in document.get_records()}
    assert any(q.endswith("total") for q in qnames)
    assert any(q.endswith("src.doc") for q in qnames)
    assert len(document.get_records()) >= 4
