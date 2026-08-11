"""provgraf — live dashboard (Streamlit). Reads the database on the fly (not static HTML).
Run with:  uv run --group dashboard streamlit run dashboard/app.py
"""
import re

import pandas as pd
import psycopg
import streamlit as st
import streamlit.components.v1 as components

from provgraf.config import Settings

st.set_page_config(page_title="provgraf — verified-facts bank", page_icon="🏦", layout="wide")

_BADGE = {"confirmed": "🟢", "to_confirm": "🟠", "disputed": "🔴", "resolved": "🔵"}


@st.cache_resource
def _conn():
    return psycopg.connect(Settings().database_url, autocommit=True)


def q(sql, params=None) -> pd.DataFrame:
    with _conn().cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def nid(x):
    return re.sub(r"[^a-zA-Z0-9]", "_", x)


def incomplete_for(client, present):
    import json as _json
    from pathlib import Path as _Path
    try:
        cfg = _json.loads(
            (_Path(__file__).resolve().parents[1] / "config" / "completeness.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    spec = cfg.get(client)
    if not spec:
        return []
    return [(field, inv) for inv in spec["investments"] for field in spec["required_fields"]
            if f"{spec['prefix']}:{inv}.{field}" not in present]


def build_mermaid(client):
    nodes = q(
        "SELECT qname, provenance_class, status, kind FROM entity "
        "WHERE valid_to IS NULL AND (owner=%s OR scope='global') ORDER BY qname",
        (client,),
    )
    edges = q(
        "SELECT s.qname sub, o.qname obj, r.predicate, r.subtype FROM relation r "
        "JOIN entity s ON s.id=r.subject_id AND s.valid_to IS NULL "
        "JOIN entity o ON o.id=r.object_id AND o.valid_to IS NULL "
        "WHERE (s.owner=%s OR s.scope='global') "
        "AND r.predicate IN ('wasDerivedFrom','alternateOf','hadMember')",
        (client,),
    )
    lines = ["graph LR"]
    for _, n in nodes.iterrows():
        if n["kind"] in ("investment", "gmina", "finanse", "demografia"):
            cls = "collection"
        elif n["kind"] == "question":
            cls = "question"
        elif n["status"] == "disputed":
            cls = "disputed"
        elif n["status"] == "to_confirm":
            cls = "toconf"
        else:
            cls = n["provenance_class"]
        lines.append(f'  {nid(n["qname"])}["{n["qname"]}"]:::{cls}')
    for _, e in edges.iterrows():
        if e["predicate"] == "alternateOf":
            lines.append(f'  {nid(e["sub"])} -.->|alt| {nid(e["obj"])}')
        elif e["predicate"] == "hadMember":
            lines.append(f'  {nid(e["sub"])} -.->|has| {nid(e["obj"])}')
        else:
            lines.append(f'  {nid(e["sub"])} -->|{e["subtype"] or ""}| {nid(e["obj"])}')
    lines += [
        "classDef source fill:#dbeafe,stroke:#3b82f6;",
        "classDef derivation fill:#dcfce7,stroke:#22c55e;",
        "classDef decision fill:#fef9c3,stroke:#a16207;",
        "classDef disputed fill:#fed7aa,stroke:#ea580c,stroke-width:2px;",
        "classDef toconf fill:#fef3c7,stroke:#d97706,stroke-dasharray:5;",
        "classDef collection fill:#ede9fe,stroke:#7c3aed,stroke-width:2px;",
        "classDef question fill:#fde68a,stroke:#d97706,stroke-width:2px;",
    ]
    return "\n".join(lines)


def members_of(qn):
    return q(
        "SELECT o.qname, o.value#>>'{}' AS wartosc, o.unit, o.status, o.kind, o.label "
        "FROM relation r JOIN entity s ON s.id=r.subject_id AND s.valid_to IS NULL "
        "JOIN entity o ON o.id=r.object_id AND o.valid_to IS NULL "
        "WHERE r.predicate='hadMember' AND s.qname=%s ORDER BY o.qname",
        (qn,),
    )


def render_mermaid(code, height=560):
    components.html(
        f"""<div class="mermaid">{code}</div>
        <script type="module">
          import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
          mermaid.initialize({{startOnLoad:true, theme:'neutral'}});
        </script>""",
        height=height, scrolling=True,
    )


# -- UI ----------------------------------------------------------------------
st.title("🏦 provgraf — verified-facts bank")
st.caption("Every fact carries its provenance. Verified data only; anything uncertain is flagged explicitly. Reads the database live.")

try:
    clients = q("SELECT DISTINCT owner FROM entity WHERE owner IS NOT NULL ORDER BY 1")["owner"].tolist()
except Exception as e:  # noqa: BLE001
    st.error(f"No database connection (is Docker/provgraf-pg running?): {e}")
    st.stop()

if not clients:
    st.warning("The bank is empty. Load a document with `bash scripts/rebuild.sh` or `provgraf ingest`.")
    st.stop()

client = st.sidebar.selectbox("Client", clients)
if st.sidebar.button("🔄 Refresh data"):
    st.cache_resource.clear()
    st.rerun()
st.sidebar.caption("The data lives in PostgreSQL (Docker, locally). Changes made through `provgraf`/CC show up after a refresh.")

facts = q(
    """
    SELECT e.qname, e.provenance_class, e.kind, e.value#>>'{}' AS wartosc,
      e.value#>>'{file}' AS plik, e.unit, e.status, e.label,
      (SELECT string_agg(DISTINCT src.qname, ', ') FROM relation r JOIN entity src ON src.id=r.object_id
         WHERE r.predicate='wasDerivedFrom' AND r.subject_id=e.id AND src.valid_to IS NULL) AS zrodlo,
      (SELECT string_agg(r.note, ' | ') FROM relation r
         WHERE r.subject_id=e.id AND r.note IS NOT NULL) AS uwaga,
      (SELECT a.qname FROM agent a WHERE a.id=e.attributed_to) AS issuer
    FROM entity e WHERE e.valid_to IS NULL AND e.owner=%s
    ORDER BY (e.kind='document') DESC, e.qname
    """,
    (client,),
)
docs = facts[facts["kind"] == "document"]
fct = facts[facts["kind"] == "fact"].copy()  # real facts only (no collections/questions)
fct["st"] = fct["status"].map(lambda s: f"{_BADGE.get(s, '⚪')} {s}")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Source documents", len(docs))
m2.metric("Facts", len(fct))
m3.metric("To confirm", int((fct["status"] == "to_confirm").sum()))
m4.metric("Disputed", int((fct["status"] == "disputed").sum()))

t1, t2, t3, t4, t5, t6 = st.tabs(
    ["📋 Facts", "🕸️ Provenance graph", "🏛️ Shareholders", "📄 Documents",
     "🚩 Gaps / to clarify", "🧭 Navigation"])

with t1:
    cols = st.columns([2, 3])
    sel = cols[0].multiselect("Status", sorted(fct["status"].unique()),
                              default=sorted(fct["status"].unique()))
    search = cols[1].text_input("Search (qname / label)")
    v = fct[fct["status"].isin(sel)].copy()
    if search:
        m = v["qname"].str.contains(search, case=False) | v["label"].fillna("").str.contains(search, case=False)
        v = v[m]
    v["⚑"] = v["uwaga"].apply(lambda u: "🚩" if u else "")
    st.dataframe(
        v[["⚑", "qname", "wartosc", "unit", "st", "zrodlo", "uwaga"]].rename(
            columns={"st": "status", "wartosc": "value", "zrodlo": "source", "uwaga": "note"}),
        use_container_width=True, hide_index=True,
    )
    st.caption("🚩 = the fact has a note (e.g. a possible conflict to clarify). Details in the Gaps / to clarify tab.")

with t2:
    st.caption("Arrows: fact ⟶ the document it follows from. Colors: source/derivation/decision; dashed orange = to confirm, red = disputed.")
    render_mermaid(build_mermaid(client))

with t3:
    ud = fct[fct["qname"].str.contains(r":udzial\.")].copy()
    if ud.empty:
        st.info("No shareholding data for this client.")
    else:
        ud["shares"] = pd.to_numeric(ud["wartosc"], errors="coerce")
        ud["shareholder"] = ud["qname"].str.replace(r".*:udzial\.", "", regex=True)
        total = ud["shares"].sum()
        ud["%"] = (ud["shares"] / total * 100).round(1)
        ud = ud.sort_values("shares", ascending=False)
        st.metric("Capital (total shares)", f"{int(total):,}".replace(",", " ") + " shares")
        cc = st.columns([3, 2])
        cc[0].dataframe(
            ud[["shareholder", "shares", "%", "st", "zrodlo"]].rename(columns={"st": "status", "zrodlo": "source"}),
            use_container_width=True, hide_index=True,
        )
        cc[1].bar_chart(ud.set_index("shareholder")["%"], horizontal=True)

with t4:
    st.caption("Source of truth for files: `marketing/klienci/{slug}/dokumenty/`. Every document has a fixed path; `provgraf check` makes sure the file still exists (DANGLING-DOC once it disappears).")
    for _, d in docs.iterrows():
        w = f" · ⟵ {d['issuer']}" if d["issuer"] else ""
        st.markdown(f"📄 **{d['qname']}**  \n{d['label'] or ''}{w}")
        if d["plik"]:
            st.caption(f"📁 {d['plik']}")

with t5:
    from collections import defaultdict
    present = set(fct["qname"])
    byf = defaultdict(list)
    for field, inv in incomplete_for(client, present):
        byf[field].append(inv)

    st.subheader("Missing data — to ask about")
    if byf:
        for f, invs in sorted(byf.items()):
            st.markdown(f"- **{f}** — missing for: {', '.join(sorted(invs))}")
    else:
        st.success("All required fields present.")

    st.subheader("To clarify (notes / possible conflicts)")
    fl = fct[fct["uwaga"].fillna("").str.contains(
        "verif|conflict|confirm|check|doubt|open|question|separate investment",
        case=False, regex=True)]
    if len(fl):
        for _, r in fl.iterrows():
            st.markdown(f"- 🚩 **{r['qname']}** = {r['wartosc']} {r['unit'] or ''} — {r['uwaga']}")
    else:
        st.info("No notes.")

    st.subheader("To confirm / disputed")
    tc = fct[fct["status"].isin(["to_confirm", "disputed"])]
    if len(tc):
        st.dataframe(tc[["qname", "wartosc", "status", "uwaga"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("None.")

with t6:
    st.caption("Navigating the knowledge graph: pick an investment or a municipality (a PROV-DM `prov:Collection` node) to see everything that belongs to it (via `hadMember`). A municipality shows its dual role: shareholder and host of the investment.")
    groups = facts[facts["kind"].isin(["investment", "gmina", "finanse", "demografia"])].copy()
    questions = facts[facts["kind"] == "question"].copy()
    if groups.empty:
        st.info("No structure layer. Build it with `provgraf structure <slug>` (or `bash scripts/rebuild.sh`).")
    else:
        klabel = {"investment": "🏗️ Investment", "gmina": "🏛️ Municipality"}
        groups = groups.sort_values(["kind", "qname"])
        opts = {f'{klabel.get(r["kind"], r["kind"])} — {r["label"]}': r["qname"]
                for _, r in groups.iterrows()}
        pick = st.selectbox("Node (collection)", list(opts.keys()))
        qn = opts[pick]
        mem = members_of(qn)
        mfacts = mem[mem["kind"] == "fact"]
        subs = mem[mem["kind"].isin(["investment", "gmina"])]
        st.markdown(f"#### {pick}  ·  `{qn}`")
        if not mfacts.empty:
            st.dataframe(
                mfacts[["qname", "wartosc", "unit", "status", "label"]].rename(columns={"wartosc": "value"}),
                use_container_width=True, hide_index=True)
        for _, sc in subs.iterrows():
            with st.expander(f"↳ {sc['label']}   ({sc['qname']})", expanded=True):
                smf = members_of(sc["qname"])
                smf = smf[smf["kind"] == "fact"]
                if smf.empty:
                    st.caption("(no facts)")
                else:
                    st.dataframe(
                        smf[["qname", "wartosc", "unit", "status"]].rename(columns={"wartosc": "value"}),
                        use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("❓ Open structural questions")
    if questions.empty:
        st.success("No structural questions.")
    else:
        import json as _json
        for _, ques in questions.iterrows():
            try:
                qv = _json.loads(ques["wartosc"]) if ques["wartosc"] else {}
            except Exception:  # noqa: BLE001
                qv = {}
            badge = "🔵 resolved" if ques["status"] == "resolved" else "🟠 open"
            st.markdown(f"**{badge}** — {ques['label']}")
            if qv.get("sciezka_rozwiazania"):
                st.caption(f"🧭 Path to certainty: {qv['sciezka_rozwiazania']}")
            if qv.get("rozstrzygniecie"):
                st.caption(f"✓ Ruling: {qv['rozstrzygniecie']} — basis: {qv.get('podstawa', '')}")
            ab = members_of(ques["qname"])
            if not ab.empty:
                st.caption("Applies to: " + ", ".join(f"`{x}`={v}" for x, v in zip(ab["qname"], ab["wartosc"], strict=False)))
