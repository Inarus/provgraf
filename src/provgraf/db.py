"""Postgres access layer (asyncpg). The insert/* functions take a `conn`
(so they compose inside one transaction in main.py); the read-* functions take a pool.
Integrity: ENUM/CHECK/FK + the DAG trigger in the database; INV-1 is enforced in the CLI
(entity_provenance_count).
"""
import asyncpg


async def create_pool(database_url: str) -> asyncpg.Pool:
    from pgvector.asyncpg import register_vector

    async def _reg(conn):
        try:
            await register_vector(conn)
        except Exception:  # noqa: BLE001 — the vector type appears after `init`; registration is only needed for embed/search
            pass

    return await asyncpg.create_pool(database_url, min_size=1, max_size=4, init=_reg)


# ── Agent / Activity ────────────────────────────────────────────────────────
async def upsert_agent(conn, qname: str, kind: str, name: str) -> int:
    return await conn.fetchval(
        """
        INSERT INTO agent (qname, kind, name) VALUES ($1, $2, $3)
        ON CONFLICT (qname) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind
        RETURNING id
        """,
        qname, kind, name,
    )


async def get_agent_id(conn, qname: str) -> int | None:
    return await conn.fetchval("SELECT id FROM agent WHERE qname = $1", qname)


async def get_agent(conn, qname: str) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT id, qname, kind, name FROM agent WHERE qname = $1", qname)


async def insert_activity(
    conn, qname: str, kind: str, formula: str | None = None,
    agent_id: int | None = None, ended_at=None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO activity (qname, kind, formula, agent_id, started_at, ended_at)
        VALUES ($1, $2, $3, $4, now(), $5)
        RETURNING id
        """,
        qname, kind, formula, agent_id, ended_at,
    )


# ── Entity ──────────────────────────────────────────────────────────────────
async def get_entity(conn, qname: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT id, qname, provenance_class, status, scope, owner, audience,
               content_hash, valid_to
        FROM entity WHERE qname = $1 AND valid_to IS NULL
        """,
        qname,
    )


async def insert_entity(conn, **f) -> int:
    return await conn.fetchval(
        """
        INSERT INTO entity (
          qname, provenance_class, status, scope, owner, load, audience, kind,
          value, value_type, unit, label, content_hash, inputs_hash,
          generated_by, attributed_to, last_verified, verification_interval, gloss,
          world_valid_from, world_valid_to
        ) VALUES (
          $1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21
        )
        RETURNING id
        """,
        f["qname"], f["provenance_class"], f.get("status", "confirmed"),
        f.get("scope", "client"), f.get("owner"), f.get("load", "lazy"),
        f.get("audience", "client"), f.get("kind"), f.get("value"),
        f.get("value_type"), f.get("unit"), f.get("label"),
        f.get("content_hash"), f.get("inputs_hash"),
        f.get("generated_by"), f.get("attributed_to"),
        f.get("last_verified"), f.get("verification_interval"), f.get("gloss"),
        f.get("world_valid_from"), f.get("world_valid_to"),
    )


async def insert_relation(
    conn, predicate: str, subject_id: int, object_id: int,
    subtype: str | None = None, activity_id: int | None = None,
    role: str | None = None, note: str | None = None,
) -> int:
    # The DAG guard (INV-5) and UNIQUE (FR-003) are enforced by the database — asyncpg will raise.
    return await conn.fetchval(
        """
        INSERT INTO relation (predicate, subtype, subject_id, object_id, activity_id, role, note)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        RETURNING id
        """,
        predicate, subtype, subject_id, object_id, activity_id, role, note,
    )


async def ensure_relation(
    conn, predicate: str, subject_id: int, object_id: int,
    subtype: str | None = None, note: str | None = None,
) -> int | None:
    """Idempotent relation INSERT: ON CONFLICT (predicate,subject,object) DO NOTHING.
    Returns the id of the new edge, or None if it already existed. The DAG guard (trigger) still applies."""
    return await conn.fetchval(
        """
        INSERT INTO relation (predicate, subtype, subject_id, object_id, note)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (predicate, subject_id, object_id) DO NOTHING
        RETURNING id
        """,
        predicate, subtype, subject_id, object_id, note,
    )


async def entity_exists(conn, qname: str) -> bool:
    """True if a current version of the entity with this qname exists (valid_to IS NULL)."""
    return await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM entity WHERE qname=$1 AND valid_to IS NULL)", qname
    )


async def entities_matching(conn, owner: str, like: str) -> list[asyncpg.Record]:
    """Current client entities matching a LIKE qname pattern (for grouping); documents excluded."""
    return await conn.fetch(
        "SELECT id, qname FROM entity WHERE owner=$1 AND qname LIKE $2 "
        "AND valid_to IS NULL AND kind IS DISTINCT FROM 'document' ORDER BY qname",
        owner, like,
    )


async def insert_used(conn, activity_id: int, entity_id: int, role: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO activity_used (activity_id, entity_id, role) VALUES ($1,$2,$3) "
        "ON CONFLICT DO NOTHING",
        activity_id, entity_id, role,
    )


async def supersede(conn, qname: str) -> bool:
    """FR-070: close the version window of the current entity (valid_to=now()). True if something was closed."""
    res = await conn.execute(
        "UPDATE entity SET valid_to = now() WHERE qname = $1 AND valid_to IS NULL", qname
    )
    return res.endswith("1")


async def entity_provenance_count(conn, entity_id: int) -> int:
    """INV-1: number of provenance edges of an entity (wasDerivedFrom OR wasGeneratedBy OR wasAttributedTo)."""
    return await conn.fetchval(
        """
        SELECT
          (SELECT count(*) FROM relation r
             WHERE r.predicate='wasDerivedFrom' AND r.subject_id=$1)
          + (SELECT count(*) FROM entity e
               WHERE e.id=$1 AND (e.generated_by IS NOT NULL OR e.attributed_to IS NOT NULL))
        """,
        entity_id,
    )


# -- Read: staleness / overdue / disputed / eager ----------------------------
async def staleness_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Per-node hard/soft staleness after object->subject propagation along wasDerivedFrom."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            WITH RECURSIVE seed AS (
                SELECT e.id AS node, true AS hard, false AS soft
                FROM entity e
                WHERE e.provenance_class='derivation' AND e.valid_to IS NULL
                  AND e.inputs_hash IS DISTINCT FROM provgraf_current_inputs_hash(e.id)
                UNION ALL
                SELECT e.id, false, true
                FROM entity e
                WHERE provgraf_is_overdue(e.id)
            ),
            prop AS (
                SELECT node, hard, soft, ARRAY[node] AS path FROM seed
                UNION ALL
                SELECT r.subject_id, p.hard, p.soft, p.path || r.subject_id
                FROM prop p
                JOIN relation r ON r.predicate='wasDerivedFrom' AND r.object_id = p.node
                WHERE NOT r.subject_id = ANY(p.path)
            )
            SELECT e.id, e.qname, e.label, e.provenance_class,
                   bool_or(prop.hard) AS hard_stale,
                   bool_or(prop.soft) AS soft_stale
            FROM prop JOIN entity e ON e.id = prop.node
            WHERE e.valid_to IS NULL
            GROUP BY e.id, e.qname, e.label, e.provenance_class
            ORDER BY e.qname
            """
        )


async def overdue_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT qname, label, last_verified, verification_interval
            FROM entity e
            WHERE provgraf_is_overdue(e.id)
            ORDER BY qname
            """
        )


async def disputed_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT e.qname, e.label,
                   array_agg(DISTINCT alt.qname) FILTER (WHERE alt.qname IS NOT NULL) AS alternates
            FROM entity e
            LEFT JOIN relation r ON r.predicate='alternateOf'
                 AND (r.subject_id=e.id OR r.object_id=e.id)
            LEFT JOIN entity alt
                 ON alt.id = CASE WHEN r.subject_id=e.id THEN r.object_id ELSE r.subject_id END
            WHERE e.status='disputed' AND e.valid_to IS NULL
            GROUP BY e.qname, e.label
            ORDER BY e.qname
            """
        )


async def disputed_facts_sources(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Disputed facts plus the date/issuer of their source document (input for the resolution suggestion).
    One row per fact x document pair; facts without a document are returned too (doc_qname NULL)."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT e.qname, e.value#>>'{}' AS val, e.unit,
                   src.qname AS doc_qname, src.value#>>'{date}' AS doc_date, ag.qname AS issuer
            FROM entity e
            LEFT JOIN relation rd ON rd.predicate='wasDerivedFrom' AND rd.subject_id=e.id
            LEFT JOIN entity src ON src.id=rd.object_id AND src.kind='document'
            LEFT JOIN agent ag ON ag.id=src.attributed_to
            WHERE e.status='disputed' AND e.valid_to IS NULL
            ORDER BY e.qname
            """
        )


async def unresolved_derivations(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Derivations with a disputed/to_confirm input (B3: inheritance of uncertainty)."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT DISTINCT e.qname, e.label
            FROM entity e
            JOIN relation r ON r.predicate='wasDerivedFrom' AND r.subject_id=e.id
            JOIN entity i ON i.id = r.object_id
            WHERE e.provenance_class='derivation' AND e.valid_to IS NULL
              AND i.status IN ('disputed','to_confirm')
            ORDER BY e.qname
            """
        )


async def eager_rows(pool: asyncpg.Pool, owner: str) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT qname, label, value, value_type, unit, provenance_class, status
            FROM entity
            WHERE load='eager' AND scope='client' AND owner=$1 AND valid_to IS NULL
            ORDER BY qname
            """,
            owner,
        )


async def record_verification(pool: asyncpg.Pool, qname: str, agent_qname: str) -> bool:
    async with pool.acquire() as conn, conn.transaction():
        ent = await get_entity(conn, qname)
        if ent is None:
            return False
        agent_id = await get_agent_id(conn, agent_qname)
        act_q = f"verify:{qname}@{agent_qname}"
        await conn.execute(
            """
                INSERT INTO activity (qname, kind, agent_id, started_at, ended_at)
                VALUES ($1,'verification',$2, now(), now())
                ON CONFLICT (qname) DO UPDATE SET ended_at = now()
                """,
            act_q, agent_id,
        )
        await conn.execute(
            "UPDATE entity SET last_verified = now() WHERE qname=$1 AND valid_to IS NULL", qname
        )
        return True


# -- As-of (FR-070: temporal reads over the valid_from/valid_to windows) ------
async def get_asof(pool: asyncpg.Pool, qname: str, at, world_at=None) -> asyncpg.Record | None:
    """The entity version per the BANK's state at `at` (None = current) and/or in force IN THE
    WORLD at `world_at` (NULL world-time bounds are treated as open). Provenance comes from the
    edges of THAT version (relation binds concrete ids, so old versions keep their own sources)."""
    conds = ["qname=$1"]
    args: list = [qname]
    if at is None and world_at is None:
        conds.append("valid_to IS NULL")
    if at is not None:
        args.append(at)
        n = len(args)
        conds.append(f"valid_from <= ${n} AND (valid_to IS NULL OR valid_to > ${n})")
    if world_at is not None:
        args.append(world_at)
        n = len(args)
        conds.append(f"(world_valid_from IS NULL OR world_valid_from <= ${n})"
                     f" AND (world_valid_to IS NULL OR world_valid_to > ${n})")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT id, qname, provenance_class, status, value#>>'{{}}' AS val, value_type,
                   unit, label, valid_from, valid_to, world_valid_from, world_valid_to
            FROM entity WHERE {' AND '.join(conds)}
            ORDER BY valid_from DESC, id DESC LIMIT 1
            """,
            *args,
        )
        if row is None:
            return None
        srcs = await conn.fetch(
            "SELECT src.qname FROM relation r JOIN entity src ON src.id=r.object_id "
            "WHERE r.predicate='wasDerivedFrom' AND r.subject_id=$1 ORDER BY src.qname",
            row["id"],
        )
    return dict(row) | {"sources": [s["qname"] for s in srcs]}


async def get_versions(pool: asyncpg.Pool, qname: str) -> list[asyncpg.Record]:
    """Full version history of an entity (oldest first)."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT value#>>'{}' AS val, unit, status, provenance_class, valid_from, valid_to,
                   world_valid_from, world_valid_to
            FROM entity WHERE qname=$1 ORDER BY valid_from, id
            """,
            qname,
        )


# -- Phase B: conflicts + completeness ---------------------------------------
async def entity_full(conn, qname: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT id, qname, provenance_class, status, scope, owner, audience,
               value, value_type, unit, label, content_hash
        FROM entity WHERE qname=$1 AND valid_to IS NULL
        """,
        qname,
    )


async def orphaned_by_docs(pool: asyncpg.Pool, doc_qnames: list[str]) -> list[asyncpg.Record]:
    """Facts for which the given documents (e.g. DANGLING-DOC) are the ONLY source — their
    provenance is at risk (INV-1). Facts that still have another live source are NOT listed.
    The same condition must gate any future `rm-doc` (shared-source guard)."""
    if not doc_qnames:
        return []
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT e.qname, e.label,
                   array_agg(DISTINCT src.qname) AS lost_sources
            FROM entity e
            JOIN relation r ON r.predicate='wasDerivedFrom' AND r.subject_id=e.id
            JOIN entity src ON src.id=r.object_id AND src.kind='document'
            WHERE e.valid_to IS NULL AND e.kind IS DISTINCT FROM 'document'
            GROUP BY e.id, e.qname, e.label
            HAVING bool_and(src.qname = ANY($1::text[]))
            ORDER BY e.qname
            """,
            doc_qnames,
        )


async def get_alternates(conn, qname: str) -> list[str]:
    """qnames linked by alternateOf (in either direction) to the given entity, plus the entity itself."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT alt.qname
        FROM entity e
        JOIN relation r ON r.predicate='alternateOf' AND (r.subject_id=e.id OR r.object_id=e.id)
        JOIN entity alt ON alt.id = CASE WHEN r.subject_id=e.id THEN r.object_id ELSE r.subject_id END
        WHERE e.qname=$1 AND e.valid_to IS NULL
        """,
        qname,
    )
    return [r["qname"] for r in rows]


async def alternate_group(conn, qname: str) -> list[str]:
    """The WHOLE conflict group: connected component over alternateOf edges (transitively, both
    directions), including the given entity. A star-shaped conflict (canonical<->alt1,
    canonical<->alt2) closes in a single `resolve` — without this, alt2 stayed disputed."""
    rows = await conn.fetch(
        """
        WITH RECURSIVE ids AS (
            SELECT id FROM entity WHERE qname=$1 AND valid_to IS NULL
          UNION
            SELECT CASE WHEN r.subject_id=i.id THEN r.object_id ELSE r.subject_id END
            FROM ids i
            JOIN relation r ON r.predicate='alternateOf'
                           AND (r.subject_id=i.id OR r.object_id=i.id)
        )
        SELECT DISTINCT e.qname FROM ids JOIN entity e ON e.id=ids.id
        WHERE e.valid_to IS NULL
        """,
        qname,
    )
    return [r["qname"] for r in rows]


async def set_status(conn, qname: str, status: str) -> None:
    await conn.execute(
        "UPDATE entity SET status=$2 WHERE qname=$1 AND valid_to IS NULL", qname, status
    )


async def status_facts(pool: asyncpg.Pool, owner: str, status: str) -> list[asyncpg.Record]:
    """Facts with the given status (e.g. to_confirm/disputed) for a client."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT qname, label FROM entity WHERE status=$2 AND valid_to IS NULL "
            "AND owner=$1 ORDER BY qname",
            owner, status,
        )


async def flagged_facts(pool: asyncpg.Pool, owner: str) -> list[asyncpg.Record]:
    """Facts whose note flags a doubt/conflict to clear up (agenda for a meeting).
    The regex is a keyword heuristic over free-text notes; it matches both English and
    Polish keywords (the original deployment stored Polish notes)."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT DISTINCT e.qname, e.value#>>'{}' AS val, r.note
            FROM entity e JOIN relation r ON r.subject_id=e.id AND r.note IS NOT NULL
            WHERE e.valid_to IS NULL AND e.owner=$1
              AND r.note ~* '(verif|confirm|conflict|check|doubt|open question|to be resolved'
                            '|zweryfik|konflikt|do potwierdz|potwierdzi|sprawdz|watpliw|otwarte|pytanie)'
            ORDER BY e.qname
            """,
            owner,
        )


async def documents(pool: asyncpg.Pool, owner: str | None = None) -> list[asyncpg.Record]:
    """Document nodes plus their file path (source of truth: where the document physically lives)."""
    clauses = ["e.kind='document'", "e.valid_to IS NULL"]
    args: list = []
    if owner:
        args.append(owner); clauses.append(f"e.owner = ${len(args)}")
    where = " AND ".join(clauses)
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"SELECT e.qname, e.value#>>'{{file}}' AS file, e.label, "
            f"       e.value#>>'{{date}}' AS date, "
            f"       coalesce((e.value#>>'{{testimony}}')::bool, false) AS testimony, "
            f"       (SELECT a.qname FROM agent a WHERE a.id = e.attributed_to) AS issuer "
            f"FROM entity e WHERE {where} ORDER BY e.qname",
            *args,
        )


async def list_all(pool: asyncpg.Pool, owner: str | None = None,
                   status: str | None = None) -> list[asyncpg.Record]:
    """Overview of the bank's contents: entities plus their sources (wasDerivedFrom / wasAttributedTo)."""
    clauses = ["e.valid_to IS NULL"]
    args: list = []
    if owner:
        args.append(owner); clauses.append(f"e.owner = ${len(args)}")
    if status:
        args.append(status); clauses.append(f"e.status = ${len(args)}")
    where = " AND ".join(clauses)
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"""
            SELECT e.qname, e.provenance_class, e.kind, e.value#>>'{{}}' AS val,
                   e.unit, e.status, e.label,
                   array_remove(array_agg(DISTINCT src.qname), NULL) AS sources,
                   max(ag.qname) AS issuer
            FROM entity e
            LEFT JOIN relation r ON r.predicate='wasDerivedFrom' AND r.subject_id=e.id
            LEFT JOIN entity src ON src.id=r.object_id AND src.valid_to IS NULL
            LEFT JOIN agent ag ON ag.id=e.attributed_to
            WHERE {where}
            GROUP BY e.id, e.qname, e.provenance_class, e.kind, e.value, e.unit, e.status, e.label
            ORDER BY (e.kind='document') DESC, e.qname
            """,
            *args,
        )


async def statuses_for(pool: asyncpg.Pool, qnames: list[str]) -> dict[str, str | None]:
    """Map of qname -> status (None when there is no current version)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT qname, status FROM entity WHERE qname = ANY($1::text[]) AND valid_to IS NULL",
            qnames,
        )
    found = {r["qname"]: r["status"] for r in rows}
    return {q: found.get(q) for q in qnames}


# -- RAG: embeddings (mmlw, column embedding vector(1024)) -------------------
async def set_embedding(conn, entity_id: int, vec) -> None:
    """Store a fact's embedding vector (the pgvector codec encodes numpy/list)."""
    await conn.execute("UPDATE entity SET embedding=$2 WHERE id=$1", entity_id, vec)


async def set_gloss(conn, entity_id: int, text: str) -> None:
    """Store a fact's generated description (auto-gloss) — materialized so it is visible and reusable."""
    await conn.execute("UPDATE entity SET gloss=$2 WHERE id=$1", entity_id, text)


async def facts_to_embed(pool: asyncpg.Pool, owner: str | None, only_missing: bool = True):
    """Facts to embed plus the context for the gloss (the source note, collection labels, source qname).
    Also covers questions/resolutions (kind=question) — the index of decision precedents."""
    clauses = ["e.kind IN ('fact','document','question')", "e.valid_to IS NULL"]
    args: list = []
    if only_missing:
        clauses.append("e.embedding IS NULL")
    if owner:
        args.append(owner)
        clauses.append(f"(e.owner=${len(args)} OR e.scope='global')")
    where = " AND ".join(clauses)
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"""
            SELECT e.id, e.qname, e.kind, e.label, e.value#>>'{{}}' AS val, e.unit, e.gloss,
              (SELECT a.qname FROM agent a WHERE a.id=e.attributed_to) AS issuer,
              (SELECT string_agg(r.note, ' | ') FROM relation r
                 WHERE r.subject_id=e.id AND r.note IS NOT NULL) AS note,
              (SELECT string_agg(DISTINCT col.label, ', ') FROM relation r
                 JOIN entity col ON col.id=r.subject_id AND col.valid_to IS NULL
                 WHERE r.predicate='hadMember' AND r.object_id=e.id) AS collections,
              (SELECT string_agg(DISTINCT src.qname, ', ') FROM relation r
                 JOIN entity src ON src.id=r.object_id AND src.valid_to IS NULL
                 WHERE r.predicate='wasDerivedFrom' AND r.subject_id=e.id) AS zrodlo
            FROM entity e WHERE {where} ORDER BY e.qname
            """,
            *args,
        )


async def search_embedding(pool: asyncpg.Pool, query_vec, owner: str | None, k: int = 8,
                           only_precedents: bool = False):
    """Top-k facts by cosine distance to query_vec (+ provenance). owner=None -> everyone + global.
    only_precedents=True narrows it to decisions and questions (find_precedents: "have we already
    resolved this?")."""
    prec = "AND (e.provenance_class='decision' OR e.kind='question')" if only_precedents else ""
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"""
            SELECT e.qname, e.kind, e.provenance_class, e.label, e.gloss,
                   e.value#>>'{{}}' AS val, e.unit, e.status,
                   1 - (e.embedding <=> $1) AS sim,
                   (SELECT string_agg(DISTINCT src.qname, ', ') FROM relation r
                      JOIN entity src ON src.id=r.object_id AND src.valid_to IS NULL
                      WHERE r.predicate='wasDerivedFrom' AND r.subject_id=e.id) AS zrodlo
            FROM entity e
            WHERE e.embedding IS NOT NULL AND e.valid_to IS NULL
              AND e.kind IN ('fact','document','question') {prec}
              AND ($2::text IS NULL OR e.owner=$2 OR e.scope='global')
            ORDER BY e.embedding <=> $1
            LIMIT $3
            """,
            query_vec, owner, k,
        )


# -- Document chunks (raw fallback retrieval) --------------------------------
async def doc_meta(pool: asyncpg.Pool, qname: str):
    """file/owner/scope of the source document (for chunking)."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT value#>>'{file}' AS file, owner, scope FROM entity "
            "WHERE qname=$1 AND kind='document' AND valid_to IS NULL",
            qname,
        )


async def clear_chunks(conn, doc_qname: str) -> None:
    await conn.execute("DELETE FROM doc_chunk WHERE doc_qname=$1", doc_qname)


async def insert_chunk(conn, doc_qname, owner, scope, ord_, text, vec) -> None:
    await conn.execute(
        "INSERT INTO doc_chunk (doc_qname, owner, scope, ord, text, embedding) "
        "VALUES ($1,$2,$3,$4,$5,$6)",
        doc_qname, owner, scope, ord_, text, vec,
    )


async def search_chunks(pool: asyncpg.Pool, query_vec, owner: str | None, k: int = 8):
    """Top-k raw chunks by cosine distance. owner=None -> everyone + global."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT doc_qname, ord, text, 1 - (embedding <=> $1) AS sim
            FROM doc_chunk
            WHERE embedding IS NOT NULL
              AND ($2::text IS NULL OR owner=$2 OR scope='global')
            ORDER BY embedding <=> $1
            LIMIT $3
            """,
            query_vec, owner, k,
        )


async def activity_exists(conn, qname: str) -> bool:
    """Whether an activity with this qname already exists (activity.qname is UNIQUE)."""
    return bool(await conn.fetchval("SELECT EXISTS(SELECT 1 FROM activity WHERE qname=$1)", qname))
