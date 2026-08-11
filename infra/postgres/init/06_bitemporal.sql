-- 06_bitemporal.sql — world-time layer (bitemporality; four-timestamp pattern à la Graphiti).
-- valid_from/valid_to (01_schema.sql) = the version window IN THE BANK (transaction time: when
-- the bank considered this version current). world_valid_* = when the fact HOLDS IN THE WORLD
-- per its source document (e.g. a rent effective 1 July per a resolution). NULL = unknown / open.
-- Full bitemporal question: get --at (bank state) + --world-at (world state).
ALTER TABLE entity ADD COLUMN IF NOT EXISTS world_valid_from TIMESTAMPTZ NULL;
ALTER TABLE entity ADD COLUMN IF NOT EXISTS world_valid_to   TIMESTAMPTZ NULL;
COMMENT ON COLUMN entity.valid_from IS 'version window in the bank (transaction time) — when this version became current';
COMMENT ON COLUMN entity.valid_to   IS 'version window in the bank (transaction time) — NULL = current version (FR-070)';
COMMENT ON COLUMN entity.world_valid_from IS 'world time: when the fact starts holding in the world per its source; NULL = unknown';
COMMENT ON COLUMN entity.world_valid_to   IS 'world time: when it stops holding; NULL = open-ended/unknown';
