-- provgraf — W3C PROV knowledge-graph schema (PROV-DM as a relational, FK-safe schema).
-- IDEMPOTENT + immutable-after-deploy: a refactor means a new NN_*.sql with ALTER,
-- never edit an existing CREATE TABLE once it has been deployed.
--
-- PROV model -> relational:
--   Entity                — an atomic fact/number/document/claim (table entity).
--   Activity              — what produced or changed an entity (table activity).
--   Agent                 — who is responsible (table agent).
--   wasDerivedFrom        — the STALENESS EDGE (entity->entity) -> table relation.
--   alternateOf/...       — the remaining entity->entity edges -> table relation.
--   wasGeneratedBy        — entity->activity -> column entity.generated_by.
--   wasAttributedTo       — entity->agent    -> column entity.attributed_to.
--   wasAssociatedWith     — activity->agent  -> column activity.agent_id.
--   used                  — activity->entity -> table activity_used.
-- That way every link has a real FK (hard integrity, not polymorphism).
-- ════════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;   -- ready for the future (embeddings); NO HNSW index in the pilot
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- search over qname/label

-- -- ENUMs (DO-guard, because CREATE TYPE has no IF NOT EXISTS) ---------------
DO $$ BEGIN CREATE TYPE prov_class     AS ENUM ('source','decision','derivation'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE entity_status  AS ENUM ('confirmed','disputed','to_confirm','resolved'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE prov_scope     AS ENUM ('global','client','project'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE prov_load      AS ENUM ('eager','lazy'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE prov_audience  AS ENUM ('internal','client','public'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE agent_kind     AS ENUM ('person','software','organization'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE activity_kind  AS ENUM ('ingestion','extraction','derivation','verification','decision','approval'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- relation holds ONLY entity->entity edges:
DO $$ BEGIN CREATE TYPE rel_predicate  AS ENUM (
  'wasDerivedFrom','alternateOf','specializationOf','hadMember'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE rel_subtype    AS ENUM ('Revision','Quotation','PrimarySource'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- -- Agent (PROV Agent: who is responsible) ----------------------------------
CREATE TABLE IF NOT EXISTS agent (
  id         BIGSERIAL PRIMARY KEY,
  qname      TEXT NOT NULL UNIQUE,
  kind       agent_kind NOT NULL,
  name       TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -- Activity (PROV Activity) -------------------------------------------------
--   formula: derivation = the formula; decision = the rationale ("phone call with the issuing office")
--   agent_id: wasAssociatedWith (who performed the activity)
CREATE TABLE IF NOT EXISTS activity (
  id         BIGSERIAL PRIMARY KEY,
  qname      TEXT NOT NULL UNIQUE,
  kind       activity_kind NOT NULL,
  formula    TEXT,
  agent_id   BIGINT REFERENCES agent(id) ON DELETE RESTRICT,
  started_at TIMESTAMPTZ,
  ended_at   TIMESTAMPTZ,                                     -- verification: endedAtTime=now()
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -- Entity (PROV Entity: an atomic fact / number / document / claim) ---------
CREATE TABLE IF NOT EXISTS entity (
  id                    BIGSERIAL PRIMARY KEY,
  qname                 TEXT NOT NULL,                   -- e.g. acme:site1.rent (UNIQUE only among current rows — partial index below)
  provenance_class      prov_class    NOT NULL,
  status                entity_status NOT NULL DEFAULT 'confirmed',
  scope                 prov_scope    NOT NULL DEFAULT 'client',
  owner                 TEXT,                            -- client/project slug; NULL <=> scope=global
  load                  prov_load     NOT NULL DEFAULT 'lazy',
  audience              prov_audience NOT NULL DEFAULT 'client',
  kind                  TEXT,                            -- 'document'|'fact'|'number'|'claim'
  value                 JSONB,
  value_type            TEXT,                            -- number|money|date|text|bool
  unit                  TEXT,                            -- 'PLN/m2/month'|'PLN'|'pcs'
  label                 TEXT,
  content_hash          TEXT,                            -- hash of the normalized value (FR-071 threshold)
  inputs_hash           TEXT,                            -- derivation ONLY (FR-020)
  generated_by          BIGINT REFERENCES activity(id) ON DELETE RESTRICT,  -- wasGeneratedBy
  attributed_to         BIGINT REFERENCES agent(id)    ON DELETE RESTRICT,  -- wasAttributedTo
  valid_from            TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_to              TIMESTAMPTZ,                     -- NULL = current version (FR-070)
  last_verified         TIMESTAMPTZ,                     -- source ONLY
  verification_interval INTERVAL,                        -- source ONLY (NC-2)
  embedding             vector(1024),                    -- FUTURE (column ready, no index)
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT inv4_derivation_hash    CHECK (provenance_class <> 'derivation' OR inputs_hash IS NOT NULL),
  CONSTRAINT inputs_hash_only_deriv  CHECK (provenance_class = 'derivation' OR inputs_hash IS NULL),
  CONSTRAINT freshness_only_source   CHECK (provenance_class = 'source'
                                            OR (last_verified IS NULL AND verification_interval IS NULL)),
  CONSTRAINT scope_owner_consistency CHECK ((scope = 'global' AND owner IS NULL)
                                            OR (scope <> 'global' AND owner IS NOT NULL))
);

-- -- Relation (PROV influence entity->entity; wasDerivedFrom = the staleness edge) --
CREATE TABLE IF NOT EXISTS relation (
  id          BIGSERIAL PRIMARY KEY,
  predicate   rel_predicate NOT NULL,
  subtype     rel_subtype,                                       -- Revision/Quotation/PrimarySource
  subject_id  BIGINT NOT NULL REFERENCES entity(id) ON DELETE RESTRICT,  -- INV-2
  object_id   BIGINT NOT NULL REFERENCES entity(id) ON DELETE RESTRICT,  -- INV-2
  activity_id BIGINT REFERENCES activity(id) ON DELETE RESTRICT, -- qualified derivation (which Activity)
  role        TEXT,
  note        TEXT,                                              -- how/why (anti "provenance theater")
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT no_self_edge CHECK (subject_id <> object_id),
  UNIQUE (predicate, subject_id, object_id)                      -- FR-003 dup-guard
);

-- -- used (PROV: activity->entity, the activity's inputs) ---------------------
CREATE TABLE IF NOT EXISTS activity_used (
  activity_id BIGINT NOT NULL REFERENCES activity(id) ON DELETE RESTRICT,
  entity_id   BIGINT NOT NULL REFERENCES entity(id)   ON DELETE RESTRICT,
  role        TEXT,
  PRIMARY KEY (activity_id, entity_id)
);

-- -- Indexes (NFR-001: staleness walks wasDerivedFrom in both directions) -----
CREATE INDEX IF NOT EXISTS idx_relation_obj_pred   ON relation(object_id, predicate);
CREATE INDEX IF NOT EXISTS idx_relation_subj_pred  ON relation(subject_id, predicate);
CREATE INDEX IF NOT EXISTS idx_relation_activity   ON relation(activity_id);
CREATE INDEX IF NOT EXISTS idx_entity_owner_scope  ON entity(owner, scope);
CREATE INDEX IF NOT EXISTS idx_entity_class_status ON entity(provenance_class, status);
CREATE INDEX IF NOT EXISTS idx_entity_eager        ON entity(load, scope) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_entity_qname_trgm   ON entity USING gin (qname gin_trgm_ops);
-- FR-070: only ONE current version per qname; older versions (valid_to set) coexist.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_qname_current ON entity(qname) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_entity_generated_by ON entity(generated_by);
CREATE INDEX IF NOT EXISTS idx_entity_attributed   ON entity(attributed_to);
