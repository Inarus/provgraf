"""Completeness (FR-040/041): detects GAPS — not just malformed data, but missing data.
Configuration: config/completeness.json (required fields per investment).
Field state: MISSING (no entity), UNCONFIRMED (exists, but status != confirmed/resolved).
"""
import json
from pathlib import Path

from provgraf import db

CONFIG = Path(__file__).resolve().parents[2] / "config" / "completeness.json"
_OK_STATUS = {"confirmed", "resolved"}


def expected_qnames(owner: str) -> list[str]:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    spec = cfg.get(owner)
    if not spec:
        return []
    prefix = spec["prefix"]
    out = []
    for inv in spec["investments"]:
        for field in spec["required_fields"]:
            out.append(f"{prefix}:{inv}.{field}")
    return out


async def holes(pool, owner: str) -> list[tuple[str, str]]:
    """List of (qname, 'MISSING'|'UNCONFIRMED') for required fields that are absent or unconfirmed."""
    qs = expected_qnames(owner)
    if not qs:
        return []
    statuses = await db.statuses_for(pool, qs)
    out = []
    for q in qs:
        st = statuses[q]
        if st is None:
            out.append((q, "MISSING"))
        elif st not in _OK_STATUS:
            out.append((q, f"UNCONFIRMED ({st})"))
    return out
