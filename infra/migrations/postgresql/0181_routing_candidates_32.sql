-- metahub: routing-candidates-32-v1
BEGIN;

ALTER TABLE "routing_candidates"
  DROP CONSTRAINT IF EXISTS "ck_routing_candidates_3",
  DROP CONSTRAINT IF EXISTS "ck_routing_candidates_4",
  DROP CONSTRAINT IF EXISTS "ck_routing_candidates_28",
  DROP CONSTRAINT IF EXISTS "ck_routing_candidates_29";

ALTER TABLE "routing_candidates"
  ADD CONSTRAINT "ck_routing_candidates_3"
    CHECK (typeof(configured_priority)='integer' AND configured_priority BETWEEN 0 AND 31) NOT VALID,
  ADD CONSTRAINT "ck_routing_candidates_4"
    CHECK (rank IS NULL OR (typeof(rank)='integer' AND rank BETWEEN 1 AND 32)) NOT VALID,
  ADD CONSTRAINT "ck_routing_candidates_28"
    CHECK ((candidate_role='default' AND configured_priority=0)
       OR (candidate_role='fallback' AND configured_priority BETWEEN 1 AND 31)) NOT VALID,
  ADD CONSTRAINT "ck_routing_candidates_29"
    CHECK ((status='selected' AND rank=1)
       OR (status='standby' AND rank BETWEEN 2 AND 32)
       OR (status='rejected' AND rank IS NULL)) NOT VALID;

DROP INDEX IF EXISTS "uq_routing_candidates_tenant_id_routing_decision_12f7627a3745";
CREATE UNIQUE INDEX "uq_routing_candidates_tenant_decision_model_v1"
  ON "routing_candidates" ("tenant_id", "routing_decision_id", "model_id");

DO $$
DECLARE definition TEXT;
BEGIN
  SELECT pg_get_functiondef(p.oid) INTO definition
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname=current_schema() AND p.proname='fn_trg_routing_candidates_prd050_conflict_insert';
  IF definition IS NULL THEN RAISE EXCEPTION 'missing routing candidate conflict trigger function'; END IF;
  definition := replace(definition, 'current.candidate_role=NEW.candidate_role',
                        'current.model_id=NEW.model_id');
  EXECUTE definition;
END;
$$;

DO $$
DECLARE definition TEXT;
BEGIN
  SELECT pg_get_functiondef(p.oid) INTO definition
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname=current_schema() AND p.proname='fn_trg_routing_candidates_prd048_graph_insert';
  IF definition IS NULL THEN RAISE EXCEPTION 'missing routing candidate graph trigger function'; END IF;
  definition := replace(definition,
    '(NEW.candidate_role=''fallback'' AND a.fallback_model_id IS NOT NULL AND a.fallback_model_id=NEW.model_id)',
    '(NEW.candidate_role=''fallback'' AND EXISTS (SELECT 1 FROM model_alias_fallback_candidates candidate WHERE candidate.tenant_id=NEW.tenant_id AND candidate.model_alias_id=NEW.model_alias_id AND candidate.model_id=NEW.model_id AND candidate.configured_priority=NEW.configured_priority))');
  IF definition LIKE '%a.fallback_model_id IS NOT NULL AND a.fallback_model_id=NEW.model_id%' THEN
    RAISE EXCEPTION 'fallback graph trigger replacement did not apply';
  END IF;
  EXECUTE definition;
END;
$$;

COMMIT;
