-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_stream_session_provider_error_fact_conflict_insert_prd056;

CREATE TRIGGER trg_stream_session_provider_error_fact_conflict_insert_prd056
BEFORE INSERT ON stream_sessions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM stream_sessions current
  JOIN provider_error_facts fact
    ON fact.tenant_id=current.tenant_id
   AND fact.stream_session_id=current.stream_session_id
  WHERE current.stream_session_id=NEW.stream_session_id
     OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN
  SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable');
END;

COMMIT;
PRAGMA foreign_keys = ON;
