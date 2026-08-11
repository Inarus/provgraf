"""provgraf CLI (typer). Every WRITE goes through here — this is where integrity lives (INV-1..5):
ENUM/CHECK/FK/UNIQUE and the DAG trigger in the database + INV-1 (provenance) enforced transactionally.
"""
import asyncio
import datetime as dt
import json
from pathlib import Path

import asyncpg
import typer
from rich.console import Console

from provgraf import completeness, db, hashing, qname, report, viz
from provgraf import conflicts as cf
from provgraf.config import Settings
from provgraf.model import (
    AGENT_KINDS,
    PROV_CLASSES,
    REL_PREDICATES,
    REL_SUBTYPES,
    STATUSES,
)

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

INIT_DIR = Path(__file__).resolve().parents[2] / "infra" / "postgres" / "init"
REPO_ROOT = Path(__file__).resolve().parents[2]  # repository root — base for document paths


def _settings() -> Settings:
    return Settings()


def _vjson(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _parse_value(s: str | None):
    if s is None:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s


def _check_in(name: str, val: str, allowed: set[str]) -> str:
    if val not in allowed:
        raise typer.BadParameter(f"{name}='{val}' outside {sorted(allowed)}")
    return val


def _err(msg: str) -> None:
    console.print(f"[bold red]✗[/] {msg}")
    raise typer.Exit(1)


# ════════════════════════════════════════════════════════════════════════════
# init
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def init():
    """Applies every infra/postgres/init/*.sql (idempotently) via asyncpg."""
    asyncio.run(_init())


async def _init():
    s = _settings()
    files = sorted(INIT_DIR.glob("*.sql"))
    if not files:
        _err(f"No SQL files in {INIT_DIR}")
    conn = await asyncpg.connect(s.database_url)
    try:
        for f in files:
            sql = f.read_text(encoding="utf-8")
            await conn.execute(sql)
            console.print(f"  [green]✓[/] {f.name}")
        console.print("[bold green]init OK[/] — schema ready.")
    finally:
        await conn.close()


# ════════════════════════════════════════════════════════════════════════════
# agent
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def agent(
    aqname: str = typer.Argument(..., help="agent qname, e.g. analyst / claude-code / acme:office"),
    kind: str = typer.Option(..., "--kind", help=f"{sorted(AGENT_KINDS)}"),
    name: str = typer.Option(..., "--name", help="Human-readable name"),
):
    """Adds/updates an Agent (PROV Agent: who is accountable)."""
    _check_in("kind", kind, AGENT_KINDS)
    asyncio.run(_agent(aqname, kind, name))


async def _agent(aqname, kind, name):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn:
            aid = await db.upsert_agent(conn, aqname, kind, name)
        console.print(f"[green]✓[/] agent {aqname} (id={aid})")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# add-doc — registers a source document (Entity source/document, wasAttributedTo agent)
# ════════════════════════════════════════════════════════════════════════════
@app.command(name="add-doc")
def add_doc(
    dqname: str = typer.Argument(..., help="document qname, e.g. acme:src.datasheet-2026-06"),
    by: str = typer.Option(..., "--by", help="qname of the issuing agent (wasAttributedTo)"),
    label: str = typer.Option("", "--label"),
    file: str = typer.Option("", "--file", help="path to the source file"),
    date: str = typer.Option("", "--date", help="document date YYYY-MM-DD"),
    scope: str = typer.Option("client", "--scope"),
    owner: str = typer.Option("", "--owner", help="client slug (required when scope<>global)"),
    audience: str = typer.Option("client", "--audience"),
):
    """Registers a source document as Entity(source, kind=document) + wasAttributedTo→agent."""
    qname.validate(dqname)
    asyncio.run(_add_doc(dqname, by, label, file, date, scope, owner, audience))


async def _add_doc(dqname, by, label, file, date, scope, owner, audience):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            agent_id = await db.get_agent_id(conn, by)
            if agent_id is None:
                _err(f"Agent '{by}' does not exist — add it: provgraf agent {by} ...")
            value = {"file": file or None, "date": date or None}
            eid = await db.insert_entity(
                conn,
                qname=dqname, provenance_class="source", kind="document",
                scope=scope, owner=(owner or None), audience=audience,
                value=_vjson(value), value_type="document",
                label=label or dqname, content_hash=hashing.content_hash(value),
                attributed_to=agent_id,
            )
            if await db.entity_provenance_count(conn, eid) < 1:
                _err("INV-1: document without provenance (no wasAttributedTo)")
        console.print(f"[green]✓[/] document {dqname} (id={eid}) wasAttributedTo {by}")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# add — source fact: wasDerivedFrom→document (+ optional freshness)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def add(
    fqname: str = typer.Argument(..., help="fact qname, e.g. acme:riverside.rent"),
    value: str = typer.Option(..., "--value", help="value (JSON or text), e.g. 152 / '\"27\"'"),
    from_doc: str = typer.Option(..., "--from", help="qname of the source document (wasDerivedFrom)"),
    pclass: str = typer.Option("source", "--class", help=f"{sorted(PROV_CLASSES)}"),
    status: str = typer.Option("confirmed", "--status", help=f"{sorted(STATUSES)}"),
    vtype: str = typer.Option("number", "--type"),
    unit: str = typer.Option("", "--unit"),
    label: str = typer.Option("", "--label"),
    scope: str = typer.Option("client", "--scope"),
    owner: str = typer.Option("", "--owner"),
    audience: str = typer.Option("client", "--audience"),
    load: str = typer.Option("lazy", "--load", help="eager|lazy"),
    note: str = typer.Option("", "--note", help="how/why it follows (stored on the edge)"),
    verify_days: int = typer.Option(0, "--verify-days", help="freshness interval in days (FR-030)"),
    last_verified: str = typer.Option("", "--last-verified", help="YYYY-MM-DD"),
    world_from: str = typer.Option("", "--world-from", help="when the fact starts holding IN THE WORLD (YYYY-MM-DD)"),
    world_to: str = typer.Option("", "--world-to", help="when it stops holding in the world (YYYY-MM-DD)"),
):
    """Adds a fact (source by default) with wasDerivedFrom→document provenance."""
    qname.validate(fqname)
    _check_in("class", pclass, PROV_CLASSES)
    _check_in("status", status, STATUSES)
    asyncio.run(_add(
        fqname, _parse_value(value), from_doc, pclass, status, vtype, unit, label,
        scope, owner, audience, load, note, verify_days, last_verified,
        _parse_at(world_from) if world_from else None, _parse_at(world_to) if world_to else None,
    ))


async def _add(fqname, value, from_doc, pclass, status, vtype, unit, label,
               scope, owner, audience, load, note, verify_days, last_verified,
               world_from=None, world_to=None):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            doc = await db.get_entity(conn, from_doc)
            if doc is None:
                _err(f"Source document '{from_doc}' does not exist (run add-doc first)")
            lv = dt.datetime.fromisoformat(last_verified) if last_verified else None
            vi = dt.timedelta(days=verify_days) if verify_days > 0 else None
            eid = await db.insert_entity(
                conn,
                qname=fqname, provenance_class=pclass, status=status,
                scope=scope, owner=(owner or None), load=load, audience=audience,
                kind="fact", value=_vjson(value), value_type=vtype,
                unit=(unit or None), label=(label or fqname),
                content_hash=hashing.content_hash(value),
                last_verified=lv, verification_interval=vi,
                world_valid_from=world_from, world_valid_to=world_to,
            )
            await db.insert_relation(
                conn, "wasDerivedFrom", subject_id=eid, object_id=doc["id"],
                note=(note or None),
            )
            if await db.entity_provenance_count(conn, eid) < 1:
                _err(f"INV-1: '{fqname}' without provenance")
        console.print(f"[green]✓[/] {fqname} = {value!r} ⟵ {from_doc}"
                      + (f"  [yellow][{status}][/]" if status != "confirmed" else ""))
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# derive — derivation: Activity + wasDerivedFrom→inputs + used + inputs_hash
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def derive(
    dqname: str = typer.Argument(..., help="derivation qname, e.g. acme:units_total.phase1"),
    value: str = typer.Option(..., "--value", help="computed value (JSON)"),
    inputs: list[str] = typer.Option(..., "--from", help="input qname (repeat for several)"),
    formula: str = typer.Option("", "--formula", help="formula, e.g. '152+84+58+38'"),
    by: str = typer.Option("claude-code", "--by", help="performing agent (wasAssociatedWith)"),
    label: str = typer.Option("", "--label"),
    unit: str = typer.Option("", "--unit"),
    scope: str = typer.Option("client", "--scope"),
    owner: str = typer.Option("", "--owner"),
    audience: str = typer.Option("client", "--audience"),
    load: str = typer.Option("lazy", "--load"),
    world_from: str = typer.Option("", "--world-from", help="when it starts holding in the world (YYYY-MM-DD)"),
    world_to: str = typer.Option("", "--world-to", help="when it stops holding in the world (YYYY-MM-DD)"),
):
    """Creates a derivation: Activity(derivation) + wasDerivedFrom to every input + inputs_hash."""
    qname.validate(dqname)
    asyncio.run(_derive(
        dqname, _parse_value(value), inputs, formula, by, label, unit, scope, owner, audience, load,
        _parse_at(world_from) if world_from else None, _parse_at(world_to) if world_to else None,
    ))


async def _derive(dqname, value, inputs, formula, by, label, unit, scope, owner, audience, load,
                  world_from=None, world_to=None):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            in_rows = []
            for iq in inputs:
                r = await db.get_entity(conn, iq)
                if r is None:
                    _err(f"Input '{iq}' does not exist")
                in_rows.append(r)
            # inputs_hash: at generation time every input is current (superseded=False)
            ih = hashing.inputs_hash([(r["qname"], r["content_hash"], False) for r in in_rows])
            agent_id = await db.get_agent_id(conn, by)
            act_id = await db.insert_activity(
                conn, qname=f"derive:{dqname}", kind="derivation",
                formula=(formula or None), agent_id=agent_id,
            )
            eid = await db.insert_entity(
                conn,
                qname=dqname, provenance_class="derivation", scope=scope,
                owner=(owner or None), load=load, audience=audience, kind="number",
                value=_vjson(value), value_type="number", unit=(unit or None),
                label=(label or dqname),
                content_hash=hashing.content_hash(value), inputs_hash=ih,
                generated_by=act_id,
                world_valid_from=world_from, world_valid_to=world_to,
            )
            for r in in_rows:
                await db.insert_relation(
                    conn, "wasDerivedFrom", subject_id=eid, object_id=r["id"],
                    activity_id=act_id, role="input",
                )
                await db.insert_used(conn, act_id, r["id"], role="input")
        console.print(f"[green]✓[/] derivation {dqname} = {value!r}  ⟵ {', '.join(inputs)}")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# link — explicit entity→entity edge (alternateOf, specializationOf, hadMember)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def link(
    subject: str = typer.Argument(..., help="subject qname"),
    predicate: str = typer.Argument(..., help=f"{sorted(REL_PREDICATES)}"),
    obj: str = typer.Argument(..., help="object qname"),
    subtype: str = typer.Option("", "--subtype", help=f"{sorted(REL_SUBTYPES)}"),
    note: str = typer.Option("", "--note"),
):
    """Creates an entity→entity edge (the database DAG-guard covers wasDerivedFrom)."""
    _check_in("predicate", predicate, REL_PREDICATES)
    if subtype:
        _check_in("subtype", subtype, REL_SUBTYPES)
    asyncio.run(_link(subject, predicate, obj, subtype or None, note or None))


async def _link(subject, predicate, obj, subtype, note):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            a = await db.get_entity(conn, subject)
            b = await db.get_entity(conn, obj)
            if a is None:
                _err(f"Subject '{subject}' does not exist")
            if b is None:
                _err(f"Object '{obj}' does not exist")
            # INV-3: wasDerivedFrom must not cross sideways between clients
            if predicate == "wasDerivedFrom" and a["owner"] != b["owner"]:
                _err(f"INV-3: wasDerivedFrom between different owners ({a['owner']} ⟶ {b['owner']})")
            try:
                await db.insert_relation(
                    conn, predicate, subject_id=a["id"], object_id=b["id"],
                    subtype=subtype, note=note,
                )
            except asyncpg.PostgresError as e:
                _err(f"rejected by the database: {e}")
        console.print(f"[green]✓[/] {subject} —{predicate}"
                      + (f"·{subtype}" if subtype else "") + f"→ {obj}")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# revise — FR-070: new source version (supersede the old one, Revision, wasDerivedFrom→new doc)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def revise(
    fqname: str = typer.Argument(..., help="qname of the fact to revise"),
    value: str = typer.Option(..., "--value", help="new value"),
    from_doc: str = typer.Option(..., "--from", help="qname of the NEW source document"),
    note: str = typer.Option("", "--note"),
    by: str = typer.Option("analyst", "--by", help="who revises (agent); a software agent → auto to_confirm"),
    status: str = typer.Option("", "--status", help="explicit status for the new version (wins over auto-gating)"),
    world_from: str = typer.Option("", "--world-from",
                                   help="when the NEW value starts holding in the world (YYYY-MM-DD)"),
    world_to: str = typer.Option("", "--world-to", help="when it stops holding in the world (YYYY-MM-DD)"),
):
    """Revises a source fact: closes the old version (valid_to), creates a new one with subtype=Revision.
    A revision by an agent of kind=software without an explicit --status lands as to_confirm
    (a human approves it with `provgraf verify`)."""
    if status:
        _check_in("status", status, STATUSES)
    asyncio.run(_revise(fqname, _parse_value(value), from_doc, note or None, by, status or None,
                        _parse_at(world_from) if world_from else None,
                        _parse_at(world_to) if world_to else None))


async def _revise(fqname, value, from_doc, note, by="analyst", status=None,
                  world_from=None, world_to=None):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            old = await conn.fetchrow(
                """
                    SELECT id, provenance_class, scope, owner, load, audience,
                           value_type, unit, label
                    FROM entity WHERE qname=$1 AND valid_to IS NULL
                    """,
                fqname,
            )
            if old is None:
                _err(f"Fact '{fqname}' does not exist (no current version)")
            doc = await db.get_entity(conn, from_doc)
            if doc is None:
                _err(f"New document '{from_doc}' does not exist")
            agent = await db.get_agent(conn, by)
            if agent is None:
                _err(f"Agent '{by}' does not exist")
            # Gating (Guru pattern): a software agent's revision without an explicit
            # --status lands as to_confirm — an agent may propose, never silently change.
            new_status = status
            if new_status is None:
                new_status = "to_confirm" if agent["kind"] == "software" else "confirmed"
            await db.supersede(conn, fqname)  # close the old version BEFORE inserting the new one
            new_id = await db.insert_entity(
                conn,
                qname=fqname, provenance_class=old["provenance_class"],
                status=new_status,
                scope=old["scope"], owner=old["owner"], load=old["load"],
                audience=old["audience"], kind="fact", value=_vjson(value),
                value_type=old["value_type"], unit=old["unit"], label=old["label"],
                content_hash=hashing.content_hash(value), attributed_to=agent["id"],
                world_valid_from=world_from, world_valid_to=world_to,
            )
            # Revision: new ⟵ old (subtype=Revision); plus wasDerivedFrom→new document
            await db.insert_relation(
                conn, "wasDerivedFrom", subject_id=new_id, object_id=old["id"],
                subtype="Revision", note=(note or "revision"),
            )
            await db.insert_relation(
                conn, "wasDerivedFrom", subject_id=new_id, object_id=doc["id"],
            )
        console.print(f"[green]✓[/] revision {fqname} → {value!r} (old version closed)")
        if status is None and agent["kind"] == "software":
            console.print(f"  [yellow]⚠ revision by a software agent — status to_confirm; "
                          f"approve with: provgraf verify {fqname}[/]")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# verify — record a verification (Activity) and clear overdue
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def verify(
    fqname: str = typer.Argument(...),
    by: str = typer.Option("analyst", "--by"),
):
    """Records a verification of the fact (last_verified=now)."""
    asyncio.run(_verify(fqname, by))


async def _verify(fqname, by):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        ok = await db.record_verification(pool, fqname, by)
        if not ok:
            _err(f"Fact '{fqname}' does not exist")
        console.print(f"[green]✓[/] verified {fqname} (by {by})")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# eager — the client's identity core (deterministic)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def eager(client: str = typer.Argument(..., help="client slug, e.g. acme-housing")):
    """Prints the client's identity core (load=eager, scope=client)."""
    asyncio.run(_eager(client))


async def _eager(client):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        rows = await db.eager_rows(pool, client)
        if not rows:
            console.print(f"(no eager nodes for {client})")
            return
        console.print(f"[bold]Identity core: {client}[/]")
        for r in rows:
            val = r["value"]
            console.print(f"  • {r['qname']} = {val}  "
                          + (f"[{r['unit']}]" if r["unit"] else "")
                          + (f"  [yellow]({r['status']})[/]" if r["status"] != "confirmed" else ""))
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# get — a single fact, also its state on a given day (as-of over the FR-070 windows)
# ════════════════════════════════════════════════════════════════════════════
def _parse_at(at: str) -> dt.datetime:
    """'YYYY-MM-DD' = state at the END of that day; a full ISO datetime is taken literally."""
    try:
        parsed = dt.datetime.fromisoformat(at)
    except ValueError:
        raise typer.BadParameter(f"--at '{at}' — expected YYYY-MM-DD or an ISO datetime") from None
    if len(at) == 10:  # date only → end of day
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # local zone (valid_from/valid_to are timestamptz)
    return parsed


@app.command()
def get(
    fqname: str = typer.Argument(..., help="fact qname"),
    at: str = typer.Option("", "--at", help="BANK state on day YYYY-MM-DD (transaction time)"),
    world_at: str = typer.Option("", "--world-at", help="version in force IN THE WORLD that day (world time)"),
    history: bool = typer.Option(False, "--history", help="show every version (FR-070 windows)"),
):
    """A single fact with its provenance. --at = what the bank knew that day, --world-at = what
    held in the world; both together = the full bitemporal question. --history = the full timeline."""
    asyncio.run(_get(fqname, _parse_at(at) if at else None, history,
                     _parse_at(world_at) if world_at else None))


async def _get(fqname, at, history, world_at=None):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        if history:
            rows = await db.get_versions(pool, fqname)
            if not rows:
                _err(f"Entity '{fqname}' does not exist")
            console.print(f"[bold]Version history: {fqname}[/] ({len(rows)})")
            for r in rows:
                do = r["valid_to"].date() if r["valid_to"] else "…"
                cur = "" if r["valid_to"] else "  [green]← current[/]"
                unit = f" {r['unit']}" if r["unit"] else ""
                world = ""
                if r["world_valid_from"] or r["world_valid_to"]:
                    wf = r["world_valid_from"].date() if r["world_valid_from"] else "…"
                    wt = r["world_valid_to"].date() if r["world_valid_to"] else "…"
                    world = f"  [dim]world: {wf}→{wt}[/]"
                console.print(f"  {r['valid_from'].date()} → {do}:  {r['val']}{unit}"
                              f"  [dim]({r['provenance_class']}/{r['status']})[/]{world}{cur}")
            return
        row = await db.get_asof(pool, fqname, at, world_at)
        if row is None:
            when = f" as of {at.date()}" if at else ""
            if world_at:
                when += f" (world-at {world_at.date()})"
            _err(f"No version of '{fqname}'{when}")
        unit = f" {row['unit']}" if row["unit"] else ""
        okno = f"{row['valid_from'].date()} → " + (str(row["valid_to"].date()) if row["valid_to"] else "…")
        console.print(f"[bold]{row['qname']}[/] = {row['val']}{unit}"
                      + (f"  [yellow]({row['status']})[/]" if row["status"] != "confirmed" else ""))
        console.print(f"  [dim]window: {okno}   class: {row['provenance_class']}[/]")
        if row.get("world_valid_from") or row.get("world_valid_to"):
            wf = row["world_valid_from"].date() if row["world_valid_from"] else "…"
            wt = row["world_valid_to"].date() if row["world_valid_to"] else "…"
            console.print(f"  [dim]in the world: {wf} → {wt}[/]")
        if row["sources"]:
            console.print(f"  [dim]⟵ {', '.join(row['sources'])}[/]")
        if (at or world_at) and row["valid_to"] is not None:
            console.print(f"  [yellow]⚠ historical version[/] — `provgraf get {fqname}` shows the current one")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# list — overview of the bank's contents (documents + facts with provenance)
# ════════════════════════════════════════════════════════════════════════════
@app.command(name="list")
def list_cmd(
    client: str = typer.Option("", "--client", help="client slug"),
    status: str = typer.Option("", "--status", help="filter: confirmed|disputed|to_confirm|resolved"),
):
    """Overview: every source document and fact (value, status, what they follow from)."""
    asyncio.run(_list(client or None, status or None))


async def _list(client, status):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        rows = await db.list_all(pool, client, status)
        docs = [r for r in rows if r["kind"] == "document"]
        facts = [r for r in rows if r["kind"] != "document"]
        if docs:
            console.print(f"[bold]SOURCE DOCUMENTS[/] ({len(docs)})")
            for r in docs:
                iss = f"  ⟵ {r['issuer']}" if r["issuer"] else ""
                console.print(f"  📄 {r['qname']}  [dim]{r['label'] or ''}[/]{iss}")
        console.print(f"\n[bold]FACTS[/] ({len(facts)})")
        for r in facts:
            tag = {"derivation": "[green]∑[/] ", "decision": "[yellow]⚖[/] "}.get(
                r["provenance_class"], "")
            st = f"  [yellow]({r['status']})[/]" if r["status"] != "confirmed" else ""
            unit = f" {r['unit']}" if r["unit"] else ""
            src = f"  [dim]⟵ {', '.join(r['sources'])}[/]" if r["sources"] else ""
            console.print(f"  {tag}{r['qname']} = {r['val']}{unit}{st}{src}")
        console.print(f"\n[dim]total: {len(docs)} docs + {len(facts)} facts[/]")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# subgraph — provenance in depth (transitive inputs)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def subgraph(fqname: str = typer.Argument(..., help="entity qname")):
    """Shows the provenance subgraph (what it follows from, transitively)."""
    asyncio.run(_subgraph(fqname))


async def _subgraph(fqname):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        rows = await viz.subgraph(pool, fqname)
        if not rows:
            _err(f"Entity '{fqname}' does not exist")
        console.print(f"[bold]Provenance: {fqname}[/]")
        for r in rows:
            indent = "  " * r["depth"]
            tag = {"source": "[blue]src[/]", "derivation": "[green]der[/]",
                   "decision": "[yellow]dec[/]"}.get(r["provenance_class"], "?")
            arrow = "" if r["depth"] == 0 else "⟵ "
            console.print(f"  {indent}{arrow}{r['qname']} {tag}  [dim]{r['label'] or ''}[/]")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# diagram — Mermaid (colored by class; stale=red, disputed=amber)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def diagram(
    client: str = typer.Argument(..., help="client slug"),
    out: str = typer.Option("", "--out", help="write to a .mmd file (stdout by default)"),
):
    """Generates a Mermaid diagram of the client's graph."""
    asyncio.run(_diagram(client, out))


async def _diagram(client, out):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        mmd = await viz.mermaid(pool, client)
        if out:
            Path(out).write_text(mmd, encoding="utf-8")
            console.print(f"[green]✓[/] wrote {out}")
        else:
            console.print(mmd)
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# export — PROV-JSON (W3C), with an audience filter (FR-062)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def export(
    client: str = typer.Argument(..., help="client slug"),
    fmt: str = typer.Option("prov-json", "--format", help="prov-json (prov-o = Future)"),
    audience: str = typer.Option("", "--audience", help="internal|client|public — filters out sensitive data"),
    out: str = typer.Option("", "--out", help="write to a file (stdout by default)"),
):
    """Exports the client's graph to PROV-JSON (W3C); --audience client excludes internal."""
    if fmt != "prov-json":
        _err(f"format '{fmt}' not supported in the pilot (prov-json only; prov-o = Future)")
    asyncio.run(_export(client, audience or None, out))


async def _export(client, audience, out):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        doc = await viz.prov_json(pool, client, audience)
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        if out:
            Path(out).write_text(text, encoding="utf-8")
            n = len(doc.get("entity", {}))
            console.print(f"[green]✓[/] {out}  ({n} entities"
                          + (f", audience={audience}" if audience else "") + ")")
        else:
            print(text)
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# view — visual HTML preview (fact table + interactive graph) for the browser
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def view(
    client: str = typer.Argument(..., help="client slug"),
    out: str = typer.Option("provgraf-view.html", "--out", help="HTML file"),
):
    """Generates a tangible HTML preview (facts + graph) — open it in a browser."""
    asyncio.run(_view(client, out))


async def _view(client, out):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        page = await viz.html_view(pool, client)
        Path(out).write_text(page, encoding="utf-8")
        console.print(f"[green]✓[/] {out} — open it in a browser: [bold]open {out}[/]")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# conflicts — open discrepancies between sources (validation layer)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def conflicts():
    """Lists open conflicts (status=disputed) + their alternatives."""
    asyncio.run(_conflicts())


async def _conflicts():
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        rows = await db.disputed_rows(pool)
        if not rows:
            console.print("[green]✓[/] no open conflicts.")
            return
        groups = cf.group_disputed(rows, await db.disputed_facts_sources(pool))
        console.print(f"[bold magenta]Open conflicts[/] ({len(groups)})")
        for g in groups:
            console.print(f"  • {g['canonical']}  [dim]{g['label'] or ''}[/]"
                          f"  ⟷ {', '.join(g['alternates'])}")
            if g["suggestion"]:
                pick, reason = g["suggestion"]
                console.print(f"    [cyan]→ suggestion (recency):[/] {pick}  [dim]{reason}[/]")
                console.print(f"    [dim]provgraf resolve {g['canonical']} --pick {pick} "
                              f"--by analyst --basis \"...\"[/]")
        console.print("\nThe suggestion is only a hint — a human decides: provgraf resolve "
                      "<canonical-qname> --pick <alternative> --by <agent> --basis \"...\"")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# resolve — settling a conflict by decision (the trace of alternatives is kept)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def resolve(
    canonical: str = typer.Argument(..., help="canonical fact qname, e.g. acme:riverside.deposit"),
    pick: str = typer.Option(..., "--pick", help="qname of the chosen alternative"),
    by: str = typer.Option("analyst", "--by", help="who made the decision"),
    basis: str = typer.Option(..., "--basis", help="on what grounds, e.g. 'phone call with the office 2026-06-24'"),
    note: str = typer.Option("", "--note"),
):
    """Settles a conflict: a decision Activity, canonical fact ⟵ chosen alternative; the alternatives remain."""
    qname.validate(canonical)
    asyncio.run(_resolve(canonical, pick, by, basis, note or None))


async def _resolve(canonical, pick, by, basis, note):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            pr = await db.entity_full(conn, pick)
            if pr is None:
                _err(f"The chosen alternative '{pick}' does not exist")
            # re-deciding: close the old canonical version
            if await db.get_entity(conn, canonical) is not None:
                await db.supersede(conn, canonical)
            agent_id = await db.get_agent_id(conn, by)
            if agent_id is None:
                _err(f"Agent '{by}' does not exist (a decision needs an agent for provenance)")
            act_qname = f"decide:{canonical}@{hashing.short_hash(basis)}"
            if await db.activity_exists(conn, act_qname):
                _err(f"A decision with the same basis is already recorded ({act_qname}) — "
                     f"change --basis or check `provgraf get {canonical} --history`")
            act_id = await db.insert_activity(
                conn, qname=act_qname, kind="decision",
                formula=basis, agent_id=agent_id,
                ended_at=dt.datetime.now(dt.UTC),
            )
            pv = json.loads(pr["value"]) if pr["value"] is not None else None
            rid = await db.insert_entity(
                conn,
                qname=canonical, provenance_class="decision", status="resolved",
                scope=pr["scope"], owner=pr["owner"], audience=pr["audience"],
                kind="fact", value=pr["value"], value_type=pr["value_type"],
                unit=pr["unit"], label=f"{canonical} (resolved: {basis})",
                content_hash=hashing.content_hash(pv), generated_by=act_id,
            )
            await db.insert_relation(
                conn, "wasDerivedFrom", subject_id=rid, object_id=pr["id"],
                activity_id=act_id, note=(note or f"resolved: {basis}"),
            )
            # trace: alternatives → status resolved (they stay in the graph)
            alts = set(await db.alternate_group(conn, pick)) | {pick}
            alts |= set(await db.alternate_group(conn, canonical))
            alts.discard(canonical)  # the canonical fact just got a fresh 'resolved' entry
            for a in alts:
                await db.set_status(conn, a, "resolved")
        console.print(f"[green]✓[/] {canonical} = {pv!r}  ⟵ {pick}  "
                      f"[dim](decision: {basis})[/]  — alternatives kept as a trace")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# check — raport: hard-stale / soft-stale / overdue / disputed / unresolved / incomplete
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def check(
    client: str = typer.Option("", "--client", help="client slug — enables the completeness section (gaps)"),
):
    """Integrity and staleness report (SM-1) + optionally completeness."""
    asyncio.run(_check(client or None))


async def _check(client):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        r = await report.gather(pool, client, REPO_ROOT)

        def section(title, rows, color, fmt):
            if not rows:
                return
            console.print(f"\n[bold {color}]{title}[/] ({len(rows)})")
            for row in rows:
                console.print("  " + fmt(row))

        section("HARD-STALE (an input changed → recompute)", r.hard, "red",
                lambda x: f"{x['qname']}  [dim]{x['label'] or ''}[/]")
        section("SOFT-STALE (depends on an overdue source)", r.soft, "yellow",
                lambda x: f"{x['qname']}  [dim]{x['label'] or ''}[/]")
        section("OVERDUE (source due for re-verification)", r.overdue, "yellow",
                lambda x: f"{x['qname']}  last: {x['last_verified']}  interval: {x['verification_interval']}")
        section("DISPUTED (conflicting sources)", r.disputed, "magenta",
                lambda x: f"{x['qname']}  alternatives: {', '.join(x['alternates'] or [])}"
                + (f"\n    [cyan]→ suggestion: {r.suggestions[x['qname']][0]}[/] "
                   f"[dim]({r.suggestions[x['qname']][1]})[/]" if x["qname"] in r.suggestions else ""))
        section("UNRESOLVED (derivation over a disputed/uncertain input)", r.unresolved, "yellow",
                lambda x: f"{x['qname']}  [dim]{x['label'] or ''}[/]")
        section("INCOMPLETE (required field missing or unconfirmed)", r.incomplete, "cyan",
                lambda x: f"{x[0]}  [dim]{x[1]}[/]")
        section("DANGLING-DOC (document without a file / file does not exist)", r.dangling, "red",
                lambda x: f"{x[0]}  [dim]{x[1]}[/]")
        section("ORPHANED (a dangling doc is the fact's ONLY source)", r.orphaned, "red",
                lambda x: f"{x['qname']}  [dim]{x['label'] or ''} ⟵ {', '.join(x['lost_sources'])}[/]")

        if r.total == 0:
            console.print("[bold green]✓ clean[/] — no stale/overdue/disputed/unresolved.")
        else:
            console.print(f"\n[bold]Total needing attention:[/] {r.total}")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# gaps — open items (missing/to confirm/to clarify) — for a meeting
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def gaps(
    client: str = typer.Argument(..., help="client slug"),
    out: str = typer.Option("", "--out", help="write markdown to a file (e.g. for a meeting)"),
):
    """List of open items from the bank — a ready-made 'to collect' agenda for a meeting."""
    asyncio.run(_gaps(client, out))


async def _gaps(client, out):
    from collections import defaultdict
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        holes = await completeness.holes(pool, client)
        to_conf = await db.status_facts(pool, client, "to_confirm")
        disputed = await db.status_facts(pool, client, "disputed")
        flagged = await db.flagged_facts(pool, client)

        byfield = defaultdict(list)
        for qname, _state in holes:
            local = qname.split(":", 1)[1] if ":" in qname else qname
            inv, _, field = local.partition(".")
            byfield[field].append(inv)

        L = [f"# {client} — open items (to collect)",
             f"_From the provgraf bank · {len(holes)} missing · {len(to_conf)} to confirm · "
             f"{len(disputed)} disputed · {len(flagged)} to clarify._", ""]

        L.append("## Missing data (to ask about)")
        L += ([f"- **{f}** — missing for: {', '.join(sorted(invs))}" for f, invs in sorted(byfield.items())]
              or ["- (complete)"])

        L += ["", "## To confirm"]
        L += ([f"- {r['qname']} — {r['label'] or ''}" for r in to_conf] or ["- (none)"])

        L += ["", "## Disputed / to settle"]
        L += ([f"- {r['qname']} — {r['label'] or ''}" for r in disputed] or ["- (none)"])

        L += ["", "## To clarify (notes / possible conflicts)"]
        L += ([f"- **{r['qname']}** = {r['val']} — {r['note']}" for r in flagged] or ["- (none)"])

        md = "\n".join(L)
        if out:
            Path(out).write_text(md, encoding="utf-8")
            console.print(f"[green]✓[/] wrote {out}")
        else:
            print(md)
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# ingest — structured ingest from a JSON file (document + facts after review)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def ingest(
    file: str = typer.Argument(..., help="JSON: {document:{...}, facts:[...]}"),
    owner: str = typer.Option(..., "--owner", help="client slug"),
):
    """Structured ingest: registers a source document + the facts drawn from it (each wasDerivedFrom→document).

    The file format holds APPROVED proposals (CC reads the document → proposes → you edit/accept the JSON
    → ingest applies it). Every fact already carries a confirmed value (FR-011).
    """
    asyncio.run(_ingest(file, owner))


async def _ingest(file, owner):
    data = json.loads(Path(file).read_text(encoding="utf-8"))
    doc = data["document"]
    facts = data.get("facts", [])
    qname.validate(doc["qname"])
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            if await db.get_entity(conn, doc["qname"]) is not None:
                console.print(f"[yellow]·[/] ingest {doc['qname']} — already in the bank, skipping")
                return
            agent_id = await db.get_agent_id(conn, doc["by"])
            if agent_id is None:
                _err(f"Agent '{doc['by']}' does not exist")
            dscope = doc.get("scope", "client")
            downer = None if dscope == "global" else owner
            dval = {"file": doc.get("file"), "date": doc.get("date")}
            did = await db.insert_entity(
                conn, qname=doc["qname"], provenance_class="source", kind="document",
                scope=dscope, owner=downer, audience=doc.get("audience", "client"),
                value=_vjson(dval), value_type="document",
                label=doc.get("label", doc["qname"]),
                content_hash=hashing.content_hash(dval), attributed_to=agent_id,
            )
            n = 0
            for fct in facts:
                qname.validate(fct["qname"])
                val = fct["value"]
                fscope = fct.get("scope", dscope)
                fowner = None if fscope == "global" else owner
                eid = await db.insert_entity(
                    conn, qname=fct["qname"], provenance_class=fct.get("class", "source"),
                    status=fct.get("status", "confirmed"), scope=fscope, owner=fowner,
                    load=fct.get("load", "lazy"), audience=fct.get("audience", "client"),
                    kind="fact", value=_vjson(val), value_type=fct.get("type", "number"),
                    unit=fct.get("unit"), label=fct.get("label", fct["qname"]),
                    content_hash=hashing.content_hash(val), gloss=fct.get("gloss"),
                )
                await db.insert_relation(
                    conn, "wasDerivedFrom", subject_id=eid, object_id=did,
                    note=fct.get("note"),
                )
                if await db.entity_provenance_count(conn, eid) < 1:
                    _err(f"INV-1: '{fct['qname']}' without provenance")
                n += 1
        console.print(f"[green]✓[/] ingest {doc['qname']} + {n} facts")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# structure — binding layer: collection nodes (PROV-DM prov:Collection) + hadMember
# ════════════════════════════════════════════════════════════════════════════
STRUCTURE_CFG = Path(__file__).resolve().parents[2] / "config" / "structure.json"


@app.command()
def structure(owner: str = typer.Argument(..., help="client slug")):
    """Builds the binding layer from config/structure.json: collection nodes (prov:Collection)
    + hadMember edges (grouping investments/municipalities, dual-role) + nodes for open
    structural questions. Idempotent, recreated after a rebuild. PROV-DM only (hadMember)."""
    asyncio.run(_structure(owner))


async def _structure(owner):
    cfg_all = json.loads(STRUCTURE_CFG.read_text(encoding="utf-8"))
    spec = cfg_all.get(owner)
    if spec is None:
        _err(f"No structure defined for '{owner}' in {STRUCTURE_CFG.name}")
    groups = spec.get("groups", [])
    questions = spec.get("questions", [])
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            agent_id = await db.get_agent_id(conn, "provgraf")
            if agent_id is None:
                _err("Agent 'provgraf' does not exist (add it in rebuild.sh / `provgraf agent`)")
            # PASS 1: collection nodes + question nodes (so nesting resolves in PASS 2)
            ng = nq = 0
            for g in groups:
                qname.validate(g["qname"])
                if await db.entity_exists(conn, g["qname"]):
                    continue
                gval = {"typ": g["kind"], "label": g.get("label", g["qname"]),
                        "prov:type": "prov:Collection"}
                eid = await db.insert_entity(
                    conn, qname=g["qname"], provenance_class="source", scope="client",
                    owner=owner, kind=g["kind"], value=_vjson(gval), value_type="collection",
                    label=g.get("label", g["qname"]),
                    content_hash=hashing.content_hash(gval), attributed_to=agent_id,
                )
                if await db.entity_provenance_count(conn, eid) < 1:
                    _err(f"INV-1: '{g['qname']}' without provenance")
                ng += 1
            for ques in questions:
                qname.validate(ques["qname"])
                if await db.entity_exists(conn, ques["qname"]):
                    continue
                qval = {"pytanie": ques["text"], "sciezka_rozwiazania": ques.get("resolution_path"),
                        "opcje": ques.get("options", []), "prov:type": "provgraf:OpenQuestion"}
                eid = await db.insert_entity(
                    conn, qname=ques["qname"], provenance_class="source", status="to_confirm",
                    scope="client", owner=owner, kind="question", value=_vjson(qval),
                    value_type="question", label=ques.get("label", ques["text"][:70]),
                    content_hash=hashing.content_hash(qval), attributed_to=agent_id,
                )
                if await db.entity_provenance_count(conn, eid) < 1:
                    _err(f"INV-1: '{ques['qname']}' without provenance")
                nq += 1
            # PASS 2: hadMember edges (collection → members; question → disputed facts)
            nm = 0
            for g in groups:
                grow = await db.get_entity(conn, g["qname"])
                for pat in g.get("members", []):
                    # escape % and _ (live LIKE wildcards — qnames contain underscores), then * → %
                    like = pat.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%")
                    members = await db.entities_matching(conn, owner, like)
                    if not members:
                        console.print(f"[yellow]·[/] {g['qname']}: pattern '{pat}' → 0 members")
                    for m in members:
                        if m["id"] == grow["id"]:
                            continue
                        if await db.ensure_relation(conn, "hadMember", grow["id"], m["id"]) is not None:
                            nm += 1
            for ques in questions:
                qrow = await db.get_entity(conn, ques["qname"])
                for ab in ques.get("about", []):
                    arow = await db.get_entity(conn, ab)
                    if arow is None:
                        console.print(f"[yellow]·[/] question {ques['qname']}: fact '{ab}' does not exist")
                        continue
                    if await db.ensure_relation(conn, "hadMember", qrow["id"], arow["id"],
                                                note="open question (structural) — to clarify") is not None:
                        nm += 1
        console.print(f"[green]✓[/] structure {owner}: {ng} collections + {nq} questions, {nm} hadMember edges")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# resolve-question — settling an open structural question (the trace is kept)
# ════════════════════════════════════════════════════════════════════════════
@app.command(name="resolve-question")
def resolve_question(
    question: str = typer.Argument(..., help="question qname, e.g. acme:q.hillside-phase2-permit"),
    answer: str = typer.Option(..., "--answer", help="the resolution, e.g. 'two investments'"),
    by: str = typer.Option("analyst", "--by", help="who made the decision"),
    basis: str = typer.Option(..., "--basis", help="the knowledge source giving 100% certainty"),
):
    """Settles an open structural question: decision Activity + question → resolved (the trace is kept).
    Actually changing the structure (splitting/merging nodes) = edit config/structure.json + re-run `structure`."""
    qname.validate(question)
    asyncio.run(_resolve_question(question, answer, by, basis))


async def _resolve_question(question, answer, by, basis):
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            q = await db.entity_full(conn, question)
            if q is None:
                _err(f"Question '{question}' does not exist")
            agent_id = await db.get_agent_id(conn, by)
            if agent_id is None:
                _err(f"Agent '{by}' does not exist (a decision needs an agent for provenance)")
            act_qname = f"decide:{question}@{hashing.short_hash(basis)}"
            if await db.activity_exists(conn, act_qname):
                _err(f"A decision with the same basis is already recorded ({act_qname})")
            act_id = await db.insert_activity(
                conn, qname=act_qname, kind="decision",
                formula=f"{answer} — {basis}", agent_id=agent_id,
                ended_at=dt.datetime.now(dt.UTC),
            )
            await db.supersede(conn, question)  # the old version of the question stays as a trace
            qv = json.loads(q["value"]) if q["value"] is not None else {}
            qv["rozstrzygniecie"] = answer
            qv["podstawa"] = basis
            await db.insert_entity(
                conn, qname=question, provenance_class="decision", status="resolved",
                scope=q["scope"], owner=q["owner"], audience=q["audience"], kind="question",
                value=_vjson(qv), value_type="question", label=f"{q['label']} → {answer}",
                content_hash=hashing.content_hash(qv), generated_by=act_id,
            )
        console.print(f"[green]✓[/] {question} → {answer}  [dim](basis: {basis})[/]\n"
                      "   → if this changes the nodes: update config/structure.json and run `provgraf structure`")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# RAG: embed / search / similar — semantic fact search (mmlw, run locally)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def embed(
    owner: str = typer.Argument("", help="client slug (empty = everyone + global)"),
    all_: bool = typer.Option(False, "--all", help="recompute already-embedded rows too"),
):
    """Generates embeddings (mmlw) for facts: compose_text(gloss or fallback) → vector → embedding column."""
    asyncio.run(_embed(owner or None, all_))


async def _embed(owner, all_):
    from provgraf import embed as emb
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        rows = await db.facts_to_embed(pool, owner, only_missing=not all_)
        if not rows:
            console.print("[yellow]·[/] no facts to embed (does everything already have an embedding?)")
            return
        console.print(f"[dim]loading model {s.embedding_model} ({s.embedding_device})…[/]")
        n = 0
        async with pool.acquire() as conn:
            for r in rows:
                text = emb.compose_text(r)
                if r["gloss"] is None:
                    await db.set_gloss(conn, r["id"], text)  # materialize the auto-gloss (so it becomes visible)
                vec = emb.embed_passage(text)
                await db.set_embedding(conn, r["id"], vec)
                n += 1
        console.print(f"[green]✓[/] embed: {n} facts embedded")
    finally:
        await pool.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="natural-language query, e.g. 'deposit in Skopanie'"),
    owner: str = typer.Option("", "--owner", help="client slug (empty = everyone + global)"),
    k: int = typer.Option(8, "--k", help="how many results"),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="skip the reranker (mmlw/bi-encoder only)"),
):
    """Semantic search: mmlw (top-N) → reranker (a cross-encoder orders the ranking) → top-k with provenance."""
    asyncio.run(_search(query, owner or None, k, no_rerank))


async def _search(query, owner, k, no_rerank):
    from rich.table import Table

    from provgraf import embed as emb
    s = _settings()
    do_rerank = s.rerank and not no_rerank
    pool = await db.create_pool(s.database_url)
    try:
        qv = emb.embed_query(query)
        n = max(k, s.rerank_candidates) if do_rerank else k
        rows = await db.search_embedding(pool, qv, owner, n)
        if not rows:
            console.print("[yellow]·[/] no results — has `provgraf embed` been run?")
            return
        rr = None
        if do_rerank:
            scores = emb.rerank(query, [r["gloss"] or r["label"] or r["qname"] for r in rows])
            order = sorted(range(len(rows)), key=lambda i: scores[i], reverse=True)[:k]
            rr = [scores[i] for i in order]
            rows = [rows[i] for i in order]
        else:
            rows = rows[:k]
        t = Table(title=f"🔎 {query!r}" + ("  ·  +reranker" if do_rerank else ""))
        t.add_column("rank" if do_rerank else "sim")
        t.add_column("cos", style="dim")
        t.add_column("qname", style="cyan"); t.add_column("value"); t.add_column("source", style="dim")
        for idx, r in enumerate(rows):
            is_doc = r["kind"] == "document"
            name = _kind_marker(r) + r["qname"]
            val = "" if is_doc else f"{r['val'] or ''} {r['unit'] or ''}".strip()
            primary = f"{rr[idx]:+.2f}" if rr is not None else f"{r['sim'] * 100:.0f}%"
            t.add_row(primary, f"{r['sim'] * 100:.0f}%", name, val, r["zrodlo"] or "")
        console.print(t)
    finally:
        await pool.close()


def _kind_marker(r) -> str:
    if r["kind"] == "document":
        return "📄 "
    if r["provenance_class"] == "decision" or r["kind"] == "question":
        return "⚖ "
    return ""


@app.command()
def similar(
    text: str = typer.Argument(..., help="text of a candidate fact (dedup: do we already have it?)"),
    owner: str = typer.Option("", "--owner"),
    k: int = typer.Option(5, "--k"),
):
    """'Do we already have this?' — the semantically closest existing facts (reconcile before writing)."""
    asyncio.run(_similar(text, owner or None, k))


async def _similar(text, owner, k):
    from rich.table import Table

    from provgraf import embed as emb
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        qv = emb.embed_passage(text)  # passage-to-passage (dedup, without the query prefix)
        rows = await db.search_embedding(pool, qv, owner, k)
        if not rows:
            console.print("[yellow]·[/] nothing — the bank is empty/not embedded")
            return
        t = Table(title=f"≈ {text!r}")
        t.add_column("sim"); t.add_column("qname", style="cyan"); t.add_column("value")
        for r in rows:
            name = _kind_marker(r) + r["qname"]
            val = "" if r["kind"] == "document" else f"{r['val'] or ''} {r['unit'] or ''}".strip()
            t.add_row(f"{r['sim'] * 100:.0f}%", name, val)
        console.print(t)
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# precedents — "have we already settled a similar question?" (decisions + questions)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def precedents(
    text: str = typer.Argument(..., help="description of the dilemma, e.g. 'is Hillside one investment or two'"),
    owner: str = typer.Option("", "--owner"),
    k: int = typer.Option(5, "--k"),
):
    """Precedents: the semantically closest EARLIER resolutions (decision) and open
    structural questions. Run this BEFORE settling a new dilemma."""
    asyncio.run(_precedents(text, owner or None, k))


async def _precedents(text, owner, k):
    from rich.table import Table

    from provgraf import embed as emb
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        qv = emb.embed_query(text)
        rows = await db.search_embedding(pool, qv, owner, k, only_precedents=True)
        if not rows:
            console.print("[yellow]·[/] no precedents — the bank has no decisions/questions yet "
                          "(or the embeddings are missing: `provgraf embed`)")
            return
        t = Table(title=f"⚖ precedents: {text!r}")
        t.add_column("sim"); t.add_column("qname", style="cyan")
        t.add_column("resolution / question"); t.add_column("status", style="dim")
        for r in rows:
            t.add_row(f"{r['sim'] * 100:.0f}%", r["qname"],
                      (r["gloss"] or r["label"] or "")[:120], r["status"])
        console.print(t)
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# chunks / search-docs — raw document chunks (a searchable-stash fallback)
# ════════════════════════════════════════════════════════════════════════════
def _extract_text(path) -> str:
    import re as _re
    import subprocess
    ext = path.suffix.lower()
    if ext == ".pdf":
        return subprocess.run(["pdftotext", "-layout", str(path), "-"],
                              capture_output=True, text=True).stdout
    if ext == ".docx":
        return subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                              capture_output=True, text=True).stdout
    if ext == ".json":
        import json as _json
        return _json.dumps(_json.loads(path.read_text(encoding="utf-8", errors="ignore")),
                           indent=2, ensure_ascii=False)
    txt = path.read_text(encoding="utf-8", errors="ignore")
    if ext in (".html", ".htm"):
        txt = _re.sub(r"<[^>]+>", " ", txt)
    return txt


def _chunk_text(text: str, target: int) -> list[str]:
    import re as _re
    blocks = [p.strip() for p in _re.split(r"\n\s*\n", text) if p.strip()]
    # split oversized blocks (JSON/markdown without blank lines) line by line
    units: list[str] = []
    for b in blocks:
        if len(b) <= target:
            units.append(b)
            continue
        cur = ""
        for ln in b.splitlines():
            if cur and len(cur) + len(ln) > target:
                units.append(cur); cur = ""
            cur += ("\n" if cur else "") + ln
        if cur.strip():
            units.append(cur)
    # merge small neighbouring units up to ~target
    out, cur = [], ""
    for u in units:
        if cur and len(cur) + len(u) > target:
            out.append(cur.strip()); cur = ""
        cur += ("\n" if cur else "") + u
    if cur.strip():
        out.append(cur.strip())
    return out


@app.command()
def chunks(
    doc: str = typer.Argument(..., help="qname of the source document"),
    chars: int = typer.Option(1200, "--chars", help="target chunk size (characters)"),
):
    """Cuts a document into chunks and embeds them (raw full-text fallback search — a searchable stash)."""
    asyncio.run(_chunks(doc, chars))


async def _chunks(doc, chars):
    from provgraf import embed as emb
    s = _settings()
    pool = await db.create_pool(s.database_url)
    try:
        meta = await db.doc_meta(pool, doc)
        if meta is None or not meta["file"]:
            _err(f"Document '{doc}' does not exist or has no file")
        path = REPO_ROOT / meta["file"]
        if not path.exists():
            _err(f"File does not exist: {path}")
        parts = _chunk_text(_extract_text(path), chars)
        if not parts:
            _err("Empty text after extraction (pdftotext/textutil?)")
        async with pool.acquire() as conn:
            await db.clear_chunks(conn, doc)
            for i, p in enumerate(parts):
                await db.insert_chunk(conn, doc, meta["owner"], meta["scope"], i, p, emb.embed_passage(p))
        console.print(f"[green]✓[/] {doc}: {len(parts)} chunks embedded")
    finally:
        await pool.close()


@app.command(name="search-docs")
def search_docs(
    query: str = typer.Argument(..., help="natural-language query"),
    owner: str = typer.Option("", "--owner"),
    k: int = typer.Option(6, "--k"),
    no_rerank: bool = typer.Option(False, "--no-rerank"),
):
    """Search over the RAW document chunks (fallback for when no atomic fact exists yet)."""
    asyncio.run(_search_docs(query, owner or None, k, no_rerank))


async def _search_docs(query, owner, k, no_rerank):
    from provgraf import embed as emb
    s = _settings()
    do_rerank = s.rerank and not no_rerank
    pool = await db.create_pool(s.database_url)
    try:
        qv = emb.embed_query(query)
        n = max(k, s.rerank_candidates) if do_rerank else k
        rows = await db.search_chunks(pool, qv, owner, n)
        if not rows:
            console.print("[yellow]·[/] no chunks — run `provgraf chunks <doc>`")
            return
        if do_rerank:
            sc = emb.rerank(query, [r["text"] for r in rows])
            rows = [rows[i] for i in sorted(range(len(rows)), key=lambda i: sc[i], reverse=True)[:k]]
        else:
            rows = rows[:k]
        for r in rows:
            snippet = " ".join(r["text"].split())[:300]
            console.print(f"[cyan]{r['doc_qname']}[/] [dim]#{r['ord']} · cos {r['sim'] * 100:.0f}%[/]\n  {snippet}…\n")
    finally:
        await pool.close()


# ════════════════════════════════════════════════════════════════════════════
# snapshot — pg_dump → snapshots/<tag>.sql.gz (audit/backup; no markdown mirror)
# ════════════════════════════════════════════════════════════════════════════
@app.command()
def snapshot(tag: str = typer.Argument(..., help="snapshot name, e.g. filing-2026-07")):
    """pg_dump of the database → snapshots/<tag>.sql.gz (recommended: follow with git add + tag)."""
    import shutil
    import subprocess

    docker = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
    out = Path("snapshots") / f"{tag}.sql.gz"
    out.parent.mkdir(exist_ok=True)
    with open(out, "wb") as fh:
        p1 = subprocess.Popen(
            [docker, "exec", "provgraf-pg", "pg_dump", "-U", "provgraf", "-d", "provgraf",
             "--no-owner", "--no-acl"],
            stdout=subprocess.PIPE,
        )
        p2 = subprocess.Popen(["gzip"], stdin=p1.stdout, stdout=fh)
        p1.stdout.close()
        p2.communicate()
    rc = p2.returncode
    if rc != 0:
        _err(f"pg_dump failed (rc={rc}) — is the provgraf-pg container running?")
    size = out.stat().st_size
    console.print(f"[green]✓[/] snapshot {out} ({size} B). Audit trail: git add {out} && git tag prov-{tag}")


if __name__ == "__main__":
    app()
