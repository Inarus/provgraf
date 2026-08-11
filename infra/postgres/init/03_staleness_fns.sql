-- Staleness functions. current_inputs_hash MUST produce exactly the same result as Python
-- (hashing.py), because a derivation writes inputs_hash from Python when it is generated, while
-- /prov-check compares it in SQL.
-- Hash formula: md5 over the list of lines "qname|content_hash|superseded", SORTED by qname and
--   joined with '\n'. superseded = (input.valid_to IS NOT NULL) -> 'true'/'false' (lowercase, as in Python).
-- Thanks to the superseded flag, REVISING a source (FR-070: the old entity gets a valid_to) dirties
-- its derivations even though the old version's content_hash did not change.
-- IDEMPOTENT (CREATE OR REPLACE).
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION provgraf_current_inputs_hash(p_entity_id BIGINT) RETURNS TEXT AS $$
  SELECT md5(
    string_agg(
      e.qname || '|' || COALESCE(e.content_hash, '') || '|' || ((e.valid_to IS NOT NULL)::text),
      E'\n' ORDER BY e.qname COLLATE "C"
    )
  )
  FROM relation r
  JOIN entity e ON e.id = r.object_id
  WHERE r.predicate = 'wasDerivedFrom' AND r.subject_id = p_entity_id;
$$ LANGUAGE sql STABLE;

-- Whether a source is overdue (FR-030): current version + the verification interval has elapsed.
CREATE OR REPLACE FUNCTION provgraf_is_overdue(p_entity_id BIGINT) RETURNS BOOLEAN AS $$
  SELECT e.provenance_class = 'source'
         AND e.valid_to IS NULL
         AND e.last_verified IS NOT NULL
         AND e.verification_interval IS NOT NULL
         AND now() > e.last_verified + e.verification_interval
  FROM entity e WHERE e.id = p_entity_id;
$$ LANGUAGE sql STABLE;
