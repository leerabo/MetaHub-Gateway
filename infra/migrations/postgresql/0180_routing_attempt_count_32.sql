-- metahub: routing-attempt-count-32-v1
-- Extend request log attribution and graph guards to the 32-candidate route plan.

BEGIN;

ALTER TABLE "request_logs"
  DROP CONSTRAINT IF EXISTS "ck_request_logs_8";
ALTER TABLE "request_logs"
  ADD CONSTRAINT "ck_request_logs_8"
  CHECK (routing_attempt_count BETWEEN 0 AND 32) NOT VALID;

DO $$
DECLARE definition TEXT;
DECLARE function_row RECORD;
BEGIN
  FOR function_row IN
    SELECT p.oid::regprocedure AS signature
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = current_schema()
      AND (
        pg_get_functiondef(p.oid) LIKE '%routing_attempt_count IN (1,2)%'
        OR pg_get_functiondef(p.oid) LIKE '%routing_attempt_count IN (1, 2)%'
      )
  LOOP
    SELECT pg_get_functiondef(function_row.signature) INTO definition;
    definition := replace(definition, 'routing_attempt_count IN (1,2)',
                          'routing_attempt_count BETWEEN 1 AND 32');
    definition := replace(definition, 'routing_attempt_count IN (1, 2)',
                          'routing_attempt_count BETWEEN 1 AND 32');
    EXECUTE definition;
  END LOOP;
END;
$$;

COMMIT;
