-- INV-5: wasDerivedFrom must be acyclic (a DAG). The trigger rejects any edge creating a cycle.
-- Semantics: row(subject=S, object=O, 'wasDerivedFrom') means "S follows from O" (S depends on O).
-- A cycle appears when O already (transitively) depends on S — that is, S is reachable from O along
-- subject->object edges. Native CYCLE (PG14+) protects the recursion even against dirty pre-existing cycles.
-- IDEMPOTENT (CREATE OR REPLACE + DROP/CREATE TRIGGER).
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION provgraf_dag_guard() RETURNS trigger AS $$
BEGIN
  IF NEW.predicate <> 'wasDerivedFrom' THEN
    RETURN NEW;
  END IF;

  IF EXISTS (
    WITH RECURSIVE reach AS (
      SELECT r.object_id AS node
        FROM relation r
        WHERE r.predicate = 'wasDerivedFrom' AND r.subject_id = NEW.object_id
      UNION ALL
      SELECT r.object_id
        FROM relation r JOIN reach ON r.subject_id = reach.node
        WHERE r.predicate = 'wasDerivedFrom'
    ) CYCLE node SET is_cycle USING path
    SELECT 1 FROM reach WHERE node = NEW.subject_id
  ) THEN
    RAISE EXCEPTION 'INV-5: wasDerivedFrom %->% would create a cycle', NEW.subject_id, NEW.object_id
      USING ERRCODE = 'check_violation';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dag_guard ON relation;
CREATE TRIGGER trg_dag_guard
  BEFORE INSERT ON relation
  FOR EACH ROW EXECUTE FUNCTION provgraf_dag_guard();
