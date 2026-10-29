-- metahub: stream-retry-provider-error-fact-owners-v1
-- A failed provider attempt is an immutable fact owned by its own stream session.
-- A later fallback attempt shares the request id but must receive a distinct owner.

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
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN
  SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable');
END;

COMMIT;
