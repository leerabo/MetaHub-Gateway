-- metahub: stream-retry-provider-error-fact-owners-v1
-- Allow fallback stream owners to share request_id after an earlier provider fact.

BEGIN;

CREATE OR REPLACE FUNCTION "fn_trg_stream_session_provider_error_fact_confli_692891cdef35"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM stream_sessions current
    JOIN provider_error_facts fact
      ON fact.tenant_id=current.tenant_id
     AND fact.stream_session_id=current.stream_session_id
    WHERE current.stream_session_id=NEW.stream_session_id
  ) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stream_session_provider_error_fact_immutable';
  END IF;
  RETURN NEW;
END;
$$;

COMMIT;
