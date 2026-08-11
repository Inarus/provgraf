"""Conflict-resolution suggestion (recency + issuer) — ONLY a hint for the report.

The bank never resolves anything by itself: the heuristic picks a candidate, a human runs
`provgraf resolve`. Rules:
  - we compare the dates of the source documents behind the disputed facts (value->>'date');
  - a suggestion is made only when >=2 facts have a dated document and the maximum is UNIQUE
    (tied dates or no dates = no suggestion);
  - the same issuer across all documents strengthens the rationale (a newer version from the
    same authority), different issuers are noted in the rationale.
"""
import datetime as dt


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def suggest(members: list[dict]) -> tuple[str, str] | None:
    """members: [{qname, val, unit, doc_qname, doc_date, issuer}, ...] — a single conflict group.
    Returns (candidate_qname, rationale) or None."""
    dated = {}
    for m in members:
        d = _parse_date(m.get("doc_date"))
        if d is None:
            continue
        cur = dated.get(m["qname"])
        if cur is None or d > cur["date"]:
            dated[m["qname"]] = {"date": d, "doc": m.get("doc_qname"), "issuer": m.get("issuer")}
    if len(dated) < 2:
        return None
    best_date = max(v["date"] for v in dated.values())
    winners = [q for q, v in dated.items() if v["date"] == best_date]
    if len(winners) != 1:
        return None
    win = winners[0]
    issuers = {v["issuer"] for v in dated.values()}
    reason = f"newest document: {dated[win]['doc']} ({dated[win]['date']})"
    if len(issuers) == 1 and None not in issuers:
        reason += f", same issuer ({issuers.pop()})"
    elif len(issuers) > 1:
        reason += " — NOTE: different issuers, assess source credibility"
    return win, reason


def group_disputed(disputed_rows, source_rows) -> list[dict]:
    """Joins disputed_rows (qname + alternates) with disputed_facts_sources into conflict groups.
    A group = a fact plus its alternatives; deduplicated by member set (a<->b is one group)."""
    by_q: dict[str, list[dict]] = {}
    for r in source_rows:
        by_q.setdefault(r["qname"], []).append(dict(r))
    groups, seen = [], set()
    for r in disputed_rows:
        members_q = tuple(sorted({r["qname"], *(r["alternates"] or [])}))
        if members_q in seen:
            continue
        seen.add(members_q)
        members = [m for q in members_q for m in by_q.get(q, [{"qname": q, "doc_date": None}])]
        groups.append({"canonical": r["qname"], "alternates": r["alternates"] or [],
                       "label": r["label"], "suggestion": suggest(members)})
    return groups
