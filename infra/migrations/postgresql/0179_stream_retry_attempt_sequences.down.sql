-- metahub:expected-version-rollback-v1

ALTER TABLE "routing_attempts"
  DROP CONSTRAINT IF EXISTS "ck_routing_attempts_2";
ALTER TABLE "routing_attempts"
  ADD CONSTRAINT "ck_routing_attempts_2"
  CHECK (attempt_sequence IN (1, 2));

ALTER TABLE "cache_affinity_selections"
  DROP CONSTRAINT IF EXISTS "ck_cache_affinity_selections_4";
ALTER TABLE "cache_affinity_selections"
  ADD CONSTRAINT "ck_cache_affinity_selections_4"
  CHECK (typeof(attempt_sequence)='integer' AND attempt_sequence IN (1, 2));

ALTER TABLE "local_model_call_events"
  DROP CONSTRAINT IF EXISTS "ck_local_model_call_events_7";
ALTER TABLE "local_model_call_events"
  ADD CONSTRAINT "ck_local_model_call_events_7"
  CHECK (attempt_sequence IN(1,2));

DO $$
DECLARE definition TEXT;
BEGIN
  SELECT pg_get_functiondef(
    'fn_trg_cache_affinity_selections_prd050_graph_insert()'::regprocedure
  ) INTO definition;
  definition := replace(definition, 'NEW.attempt_sequence<1', 'NEW.attempt_sequence<>1');
  EXECUTE definition;
END;
$$;
