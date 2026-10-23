-- metahub: routing-attempt-count-32-v1
-- Extend request log attribution and graph guards to the 32-candidate route plan.

ALTER TABLE "request_logs"
  DROP CONSTRAINT IF EXISTS "ck_request_logs_8";
ALTER TABLE "request_logs"
  ADD CONSTRAINT "ck_request_logs_8"
  CHECK (routing_attempt_count BETWEEN 0 AND 32);

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
        CASE WHEN p.prokind='f' THEN pg_get_functiondef(p.oid) ELSE '' END LIKE '%routing_attempt_count IN (1,2)%'
        OR CASE WHEN p.prokind='f' THEN pg_get_functiondef(p.oid) ELSE '' END LIKE '%routing_attempt_count IN (1, 2)%'
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
