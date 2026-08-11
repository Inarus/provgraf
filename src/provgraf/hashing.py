"""Hashing — MUST be bit-for-bit identical to the SQL function provgraf_current_inputs_hash
(03_staleness_fns.sql), because derivation writes inputs_hash here (Python) while /prov-check
compares it in SQL.

content_hash  = md5(canonical JSON value)
inputs_hash   = md5( '\\n'.join( sorted-by-qname  "qname|content_hash|superseded" ) )
  superseded  = 'true'/'false' (lowercase — like bool::text in Postgres)
  sort        = by qname in codepoint order (== ORDER BY ... COLLATE "C")
"""
import hashlib
import json


def content_hash(value) -> str:
    """md5 of canonical JSON (sort_keys, no superfluous whitespace)."""
    norm = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


def inputs_hash(inputs: list[tuple[str, str | None, bool]]) -> str:
    """inputs: list of (qname, content_hash, superseded). Sorted by qname (codepoint)."""
    items = sorted(inputs, key=lambda x: x[0])
    lines = [
        f"{qn}|{ch or ''}|{'true' if sup else 'false'}" for qn, ch, sup in items
    ]
    return hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()


def short_hash(text: str, n: int = 10) -> str:
    """A short, stable digest of a string — used in decision-activity qnames (truncating the
    basis to 24 chars made near-identical bases collide on activity.qname UNIQUE)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]
