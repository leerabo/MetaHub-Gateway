-- metahub: routing-candidates-32-v1 down

ALTER TABLE "routing_candidates"
  DROP CONSTRAINT IF EXISTS "ck_routing_candidates_3",
  DROP CONSTRAINT IF EXISTS "ck_routing_candidates_4",
  DROP CONSTRAINT IF EXISTS "ck_routing_candidates_28",
  DROP CONSTRAINT IF EXISTS "ck_routing_candidates_29";

ALTER TABLE "routing_candidates"
  ADD CONSTRAINT "ck_routing_candidates_3"
    CHECK (typeof(configured_priority)='integer' AND configured_priority IN (0, 1)),
  ADD CONSTRAINT "ck_routing_candidates_4"
    CHECK (rank IS NULL OR (typeof(rank)='integer' AND rank IN (1, 2))),
  ADD CONSTRAINT "ck_routing_candidates_28"
    CHECK ((candidate_role='default' AND configured_priority=0)
       OR (candidate_role='fallback' AND configured_priority=1)),
  ADD CONSTRAINT "ck_routing_candidates_29"
    CHECK ((status='selected' AND rank=1)
       OR (status='standby' AND rank=2)
       OR (status='rejected' AND rank IS NULL));

DROP INDEX IF EXISTS "uq_routing_candidates_tenant_decision_model_v1";
CREATE UNIQUE INDEX "uq_routing_candidates_tenant_id_routing_decision_12f7627a3745"
  ON "routing_candidates" ("tenant_id", "routing_decision_id", "candidate_role");

DO $$
DECLARE definition TEXT;
BEGIN
  SELECT CASE WHEN p.prokind='f' THEN pg_get_functiondef(p.oid) ELSE '' END INTO definition
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname=current_schema() AND p.proname='fn_trg_routing_candidates_prd050_conflict_insert';
  IF definition IS NULL THEN RAISE EXCEPTION 'missing routing candidate conflict trigger function'; END IF;
  definition := replace(definition, 'current.model_id=NEW.model_id',
                        'current.candidate_role=NEW.candidate_role');
  EXECUTE definition;
END;
$$;

DO $$
DECLARE definition TEXT;
BEGIN
  SELECT CASE WHEN p.prokind='f' THEN pg_get_functiondef(p.oid) ELSE '' END INTO definition
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname=current_schema() AND p.proname='fn_trg_routing_candidates_prd048_graph_insert';
  IF definition IS NULL THEN RAISE EXCEPTION 'missing routing candidate graph trigger function'; END IF;
  definition := replace(definition,
    '(NEW.candidate_role=''fallback'' AND EXISTS (SELECT 1 FROM model_alias_fallback_candidates candidate WHERE candidate.tenant_id=NEW.tenant_id AND candidate.model_alias_id=NEW.model_alias_id AND candidate.model_id=NEW.model_id AND candidate.configured_priority=NEW.configured_priority))',
    '(NEW.candidate_role=''fallback'' AND a.fallback_model_id IS NOT NULL AND a.fallback_model_id=NEW.model_id)');
  EXECUTE definition;
END;
$$;
