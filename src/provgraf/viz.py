"""Visualization and export: subgraph (provenance), Mermaid, PROV-JSON (W3C), HTML view."""
import html as _html
import json
import re

from provgraf import db

_RANK = {"internal": 2, "client": 1, "public": 0}
_AGENT_TYPE = {"person": "prov:Person", "organization": "prov:Organization",
               "software": "prov:SoftwareAgent"}


def _nid(qname: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", qname)


async def subgraph(pool, qname: str) -> list:
    """Transitive inputs of an entity along wasDerivedFrom (provenance in depth)."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            WITH RECURSIVE sub AS (
                SELECT e.id, e.qname, e.label, e.provenance_class, 0 AS depth, ARRAY[e.id] AS path
                FROM entity e WHERE e.qname=$1 AND e.valid_to IS NULL
                UNION ALL
                SELECT o.id, o.qname, o.label, o.provenance_class, sub.depth+1, sub.path||o.id
                FROM sub
                JOIN relation r ON r.predicate='wasDerivedFrom' AND r.subject_id=sub.id
                JOIN entity o ON o.id=r.object_id AND o.valid_to IS NULL
                WHERE NOT o.id = ANY(sub.path)
            )
            SELECT id, qname, label, provenance_class, min(depth) AS depth
            FROM sub GROUP BY id, qname, label, provenance_class ORDER BY depth, qname
            """,
            qname,
        )


async def mermaid(pool, owner: str) -> str:
    """Mermaid diagram: colored by provenance_class; stale=red, disputed=amber."""
    async with pool.acquire() as conn:
        nodes = await conn.fetch(
            """
            SELECT qname, provenance_class, status, kind FROM entity
            WHERE valid_to IS NULL AND (owner=$1 OR scope='global')
            ORDER BY qname
            """,
            owner,
        )
        edges = await conn.fetch(
            """
            SELECT s.qname AS sub, o.qname AS obj, r.predicate, r.subtype
            FROM relation r
            JOIN entity s ON s.id=r.subject_id AND s.valid_to IS NULL
            JOIN entity o ON o.id=r.object_id AND o.valid_to IS NULL
            WHERE (s.owner=$1 OR s.scope='global')
              AND r.predicate IN ('wasDerivedFrom','alternateOf','hadMember')
            """,
            owner,
        )
    stale = {r["qname"] for r in await db.staleness_rows(pool)
             if r["hard_stale"] or (r["soft_stale"] and r["provenance_class"] != "source")}

    lines = ["graph TD"]
    for n in nodes:
        if n["kind"] in ("investment", "gmina"):
            cls = "collection"
        elif n["kind"] == "question":
            cls = "question"
        elif n["qname"] in stale:
            cls = "stale"
        elif n["status"] == "disputed":
            cls = "disputed"
        else:
            cls = n["provenance_class"]
        label = n["qname"].replace('"', "'")
        lines.append(f'  {_nid(n["qname"])}["{label}"]:::{cls}')
    for e in edges:
        if e["predicate"] == "alternateOf":
            lines.append(f'  {_nid(e["sub"])} -.->|alt| {_nid(e["obj"])}')
        elif e["predicate"] == "hadMember":
            lines.append(f'  {_nid(e["sub"])} -.->|has| {_nid(e["obj"])}')
        else:
            lbl = e["subtype"] or "derives"
            lines.append(f'  {_nid(e["sub"])} -->|{lbl}| {_nid(e["obj"])}')
    lines += [
        "classDef source fill:#dbeafe,stroke:#3b82f6;",
        "classDef derivation fill:#dcfce7,stroke:#22c55e;",
        "classDef decision fill:#fef9c3,stroke:#a16207;",
        "classDef stale fill:#fecaca,stroke:#ef4444,stroke-width:2px;",
        "classDef disputed fill:#fed7aa,stroke:#ea580c,stroke-width:2px;",
        "classDef collection fill:#ede9fe,stroke:#7c3aed,stroke-width:2px;",
        "classDef question fill:#fef3c7,stroke:#d97706,stroke-width:2px;",
    ]
    return "\n".join(lines)


# -- PROV-JSON export (W3C) --------------------------------------------------
def _eid(qname: str) -> str:
    """qname -> PROV-JSON identifier (qualified name). '@'->'_'; no prefix -> pg:."""
    q = qname.replace("@", "_")
    return q if ":" in q else "pg:" + q


def _prefixes(ids: list[str]) -> dict:
    pref = {"prov": "http://www.w3.org/ns/prov#", "provgraf": "http://provgraf.local/ns#"}
    for i in ids:
        if ":" in i:
            p = i.split(":", 1)[0]
            pref.setdefault(p, f"http://provgraf.local/{p}#")
    return pref


async def prov_json(pool, owner: str, audience: str | None = None) -> dict:
    """Builds the PROV-JSON document for a client (+global), filtered by audience (FR-062)."""
    async with pool.acquire() as conn:
        ents = await conn.fetch(
            """
            SELECT id, qname, provenance_class, status, value, unit, label,
                   audience, kind, generated_by, attributed_to
            FROM entity WHERE valid_to IS NULL AND (owner=$1 OR scope='global')
            """,
            owner,
        )
        if audience:
            r = _RANK[audience]
            ents = [e for e in ents if _RANK[e["audience"]] <= r]
        eids = [e["id"] for e in ents]
        id2q = {e["id"]: e["qname"] for e in ents}

        rels = await conn.fetch(
            """
            SELECT subject_id, object_id, subtype, activity_id
            FROM relation WHERE predicate='wasDerivedFrom'
              AND subject_id=ANY($1::bigint[]) AND object_id=ANY($1::bigint[])
            """,
            eids,
        )
        hadmem = await conn.fetch(
            """
            SELECT subject_id, object_id
            FROM relation WHERE predicate='hadMember'
              AND subject_id=ANY($1::bigint[]) AND object_id=ANY($1::bigint[])
            """,
            eids,
        )
        act_ids = {e["generated_by"] for e in ents if e["generated_by"]}
        act_ids |= {r["activity_id"] for r in rels if r["activity_id"]}
        acts = await conn.fetch(
            "SELECT id, qname, kind, formula, agent_id, started_at, ended_at "
            "FROM activity WHERE id=ANY($1::bigint[])",
            list(act_ids),
        )
        actq = {a["id"]: a["qname"] for a in acts}
        agent_ids = {e["attributed_to"] for e in ents if e["attributed_to"]}
        agent_ids |= {a["agent_id"] for a in acts if a["agent_id"]}
        ags = await conn.fetch(
            "SELECT id, qname, kind, name FROM agent WHERE id=ANY($1::bigint[])",
            list(agent_ids),
        )
        agentq = {g["id"]: g["qname"] for g in ags}
        useds = await conn.fetch(
            "SELECT activity_id, entity_id FROM activity_used "
            "WHERE activity_id=ANY($1::bigint[]) AND entity_id=ANY($2::bigint[])",
            list(act_ids), eids,
        )

    doc = {"prefix": {}, "entity": {}, "activity": {}, "agent": {},
           "wasDerivedFrom": {}, "wasGeneratedBy": {}, "used": {},
           "wasAttributedTo": {}, "wasAssociatedWith": {}, "hadMember": {}}
    all_ids = []

    for e in ents:
        eid = _eid(e["qname"]); all_ids.append(eid)
        a = {"prov:label": e["label"], "provgraf:class": e["provenance_class"],
             "provgraf:status": e["status"]}
        if e["kind"] in ("investment", "gmina"):
            a["prov:type"] = "prov:Collection"
        elif e["kind"] == "question":
            a["prov:type"] = "provgraf:OpenQuestion"
        if e["value"] is not None:
            v = json.loads(e["value"])
            # PROV-JSON: only scalars as plain literals; dict/list -> string
            a["provgraf:value"] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        if e["unit"]:
            a["provgraf:unit"] = e["unit"]
        doc["entity"][eid] = a
    for a in acts:
        aid = _eid(a["qname"]); all_ids.append(aid)
        ent = {"prov:label": a["qname"], "provgraf:kind": a["kind"]}
        if a["formula"]:
            ent["provgraf:formula"] = a["formula"]
        if a["ended_at"]:
            ent["prov:endTime"] = a["ended_at"].isoformat()
        doc["activity"][aid] = ent
    for g in ags:
        gid = _eid(g["qname"]); all_ids.append(gid)
        doc["agent"][gid] = {"prov:label": g["name"], "prov:type": _AGENT_TYPE.get(g["kind"]),
                             "provgraf:kind": g["kind"]}
    for i, r in enumerate(rels):
        entry = {"prov:generatedEntity": _eid(id2q[r["subject_id"]]),
                 "prov:usedEntity": _eid(id2q[r["object_id"]])}
        if r["subtype"] == "Revision":
            entry["prov:type"] = "prov:Revision"
        if r["activity_id"] and r["activity_id"] in actq:
            entry["prov:activity"] = _eid(actq[r["activity_id"]])
        doc["wasDerivedFrom"][f"_:wdf{i}"] = entry
    for i, m in enumerate(hadmem):
        if m["subject_id"] in id2q and m["object_id"] in id2q:
            doc["hadMember"][f"_:hm{i}"] = {
                "prov:collection": _eid(id2q[m["subject_id"]]),
                "prov:entity": _eid(id2q[m["object_id"]])}
    for e in ents:
        if e["generated_by"] and e["generated_by"] in actq:
            doc["wasGeneratedBy"][f"_:wgb{e['id']}"] = {
                "prov:entity": _eid(e["qname"]), "prov:activity": _eid(actq[e["generated_by"]])}
        if e["attributed_to"] and e["attributed_to"] in agentq:
            doc["wasAttributedTo"][f"_:wat{e['id']}"] = {
                "prov:entity": _eid(e["qname"]), "prov:agent": _eid(agentq[e["attributed_to"]])}
    for a in acts:
        if a["agent_id"] and a["agent_id"] in agentq:
            doc["wasAssociatedWith"][f"_:waw{a['id']}"] = {
                "prov:activity": _eid(a["qname"]), "prov:agent": _eid(agentq[a["agent_id"]])}
    for i, u in enumerate(useds):
        doc["used"][f"_:u{i}"] = {"prov:activity": _eid(actq[u["activity_id"]]),
                                  "prov:entity": _eid(id2q[u["entity_id"]])}

    doc["prefix"] = _prefixes(all_ids)
    # drop empty sections (PROV-JSON allows them, but this is cleaner)
    return {k: v for k, v in doc.items() if v}


# -- HTML view (a tangible preview in the browser) ---------------------------
_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>provgraf — %%OWNER%%</title>
<style>
 :root{--bg:#0f172a;--card:#1e293b;--mut:#94a3b8;--line:#334155;--txt:#e2e8f0}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);
  font:15px/1.5 -apple-system,Inter,Segoe UI,sans-serif}
 .wrap{max-width:1100px;margin:0 auto;padding:24px}
 h1{font-size:20px;margin:0 0 2px} .sub{color:var(--mut);margin:0 0 20px;font-size:13px}
 .grid{display:grid;grid-template-columns:1fr;gap:20px}
 @media(min-width:880px){.grid{grid-template-columns:1.1fr .9fr}}
 .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
 .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 12px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
 .q{font-family:ui-monospace,monospace;color:#7dd3fc;white-space:nowrap}
 .v{font-weight:600} .unit{color:var(--mut);font-weight:400;font-size:12px}
 .src{color:var(--mut);font-size:12px} .badge{color:#fff;border-radius:6px;padding:1px 7px;font-size:11px}
 ul{list-style:none;padding:0;margin:0} li{padding:6px 0;border-bottom:1px solid var(--line);font-size:13px}
 .mermaid{background:#fff;border-radius:10px;padding:12px;overflow:auto}
 .cnt{color:var(--mut);font-weight:400}
</style></head><body><div class="wrap">
<h1>🏦 provgraf — verified-facts bank</h1>
<p class="sub">%%OWNER%% · %%NDOC%% source documents · %%NFACT%% facts · every one with provenance</p>
<div class="grid">
 <div class="card"><h2>Facts <span class="cnt">(%%NFACT%%)</span></h2>
  <table>%%FACTS%%</table></div>
 <div class="card"><h2>Source documents <span class="cnt">(%%NDOC%%)</span></h2>
  <ul>%%DOCS%%</ul>
  <h2 style="margin-top:20px">Provenance graph</h2>
  <pre class="mermaid">%%MERMAID%%</pre></div>
</div>
<p class="sub" style="margin-top:20px">Generated by <code>provgraf view</code>. To refresh, run the command again.</p>
</div>
<script type="module">
 import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
 mermaid.initialize({startOnLoad:true, theme:'neutral'});
</script></body></html>"""

_BADGE = {"confirmed": "#16a34a", "to_confirm": "#d97706", "disputed": "#ea580c", "resolved": "#2563eb"}


async def html_view(pool, owner: str) -> str:
    rows = await db.list_all(pool, owner)
    mmd = await mermaid(pool, owner)
    docs = [r for r in rows if r["kind"] == "document"]
    facts = [r for r in rows if r["kind"] != "document"]

    def esc(x):
        return _html.escape(str(x)) if x is not None else ""

    frows = []
    for r in facts:
        c = _BADGE.get(r["status"], "#64748b")
        src = ", ".join(r["sources"]) if r["sources"] else (r["issuer"] or "")
        val = esc(r["val"]) + (f" <span class='unit'>{esc(r['unit'])}</span>" if r["unit"] else "")
        frows.append(
            f"<tr><td class='q'>{esc(r['qname'])}</td><td class='v'>{val}</td>"
            f"<td><span class='badge' style='background:{c}'>{esc(r['status'])}</span></td>"
            f"<td class='src'>⟵ {esc(src)}</td></tr>"
        )
    drows = "".join(
        f"<li>📄 <b>{esc(d['qname'])}</b><br><span class='src'>{esc(d['label'])}"
        + (f" · ⟵ {esc(d['issuer'])}" if d["issuer"] else "") + "</span></li>"
        for d in docs
    )
    return (_HTML
            .replace("%%OWNER%%", esc(owner))
            .replace("%%NDOC%%", str(len(docs)))
            .replace("%%NFACT%%", str(len(facts)))
            .replace("%%FACTS%%", "".join(frows))
            .replace("%%DOCS%%", drows)
            .replace("%%MERMAID%%", mmd))
