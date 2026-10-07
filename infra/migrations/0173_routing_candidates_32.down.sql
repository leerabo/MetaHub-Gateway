-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_routing_candidates_prd050_conflict_insert;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd048_graph_insert;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd048_immutable_update;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd048_immutable_delete;
DROP TRIGGER IF EXISTS trg_routing_decisions_prd048_candidate_bound_update;

CREATE TABLE routing_candidates_prd173_down_new (
  routing_candidate_id TEXT PRIMARY KEY CHECK (
    length(routing_candidate_id)=38
    AND substr(routing_candidate_id, 1, 6)='rcand_'
    AND substr(routing_candidate_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  routing_decision_id TEXT NOT NULL,
  routing_policy_id TEXT,
  model_alias_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  provider_id TEXT,
  candidate_role TEXT NOT NULL CHECK (candidate_role IN ('default', 'fallback')),
  configured_priority INTEGER NOT NULL CHECK (
    typeof(configured_priority)='integer' AND configured_priority IN (0, 1)
  ),
  rank INTEGER CHECK (rank IS NULL OR (typeof(rank)='integer' AND rank IN (1, 2))),
  status TEXT NOT NULL CHECK (status IN ('selected', 'standby', 'rejected')),
  reason_code TEXT NOT NULL CHECK (
    reason_code IN (
      'routing.no_available_candidate',
      'routing.fixed_priority',
      'routing.cost_lowest',
      'routing.cost_tie_stable_priority',
      'routing.cost_fixed_fallback.price_missing',
      'routing.cost_fixed_fallback.currency_incomparable',
      'routing.availability_healthy',
      'routing.availability_unknown',
      'routing.single_eligible_candidate',
      'routing.candidate.standby',
      'routing.candidate.model_missing',
      'routing.candidate.model_disabled',
      'routing.candidate.provider_missing',
      'routing.candidate.provider_disabled',
      'routing.candidate.provider_unhealthy',
      'routing.candidate.keypool_unavailable',
      'routing.quality_cost_highest_score',
      'routing.quality_cost_score_tie_stable_priority',
      'routing.quality_cost_cold_start_priority',
      'routing.quality_cost_evidence_priority',
      'routing.quality_cost_fixed_fallback.price_missing',
      'routing.quality_cost_fixed_fallback.currency_incomparable',
      'routing.quality_cost_fixed_fallback.latency_missing',
      'routing.quality_cost_single_eligible_candidate',
      'routing.candidate.quality_below_floor',
      'routing.candidate.quality_cold_start_deferred',
      'routing.cache_affinity_pending_preferred',
      'routing.cache_affinity_warm_preferred'
    )
  ),
  health_state TEXT NOT NULL CHECK (health_state IN ('healthy', 'unhealthy', 'unknown')),
  health_fresh INTEGER NOT NULL CHECK (
    typeof(health_fresh)='integer' AND health_fresh IN (0, 1)
  ),
  keypool_state TEXT NOT NULL CHECK (
    keypool_state IN ('available', 'unavailable', 'unknown', 'not_evaluated')
  ),
  price_available INTEGER NOT NULL CHECK (
    typeof(price_available)='integer' AND price_available IN (0, 1)
  ),
  price_source TEXT NOT NULL CHECK (
    price_source IN ('workspace_provider_cost_rate', 'price_plan', 'model_catalog',
      'missing', 'not_evaluated')
  ),
  price_schema_version TEXT,
  currency TEXT CHECK (currency IS NULL OR currency IN ('CNY', 'USD')),
  estimated_cost_amount TEXT CHECK (
    estimated_cost_amount IS NULL
    OR (
      length(estimated_cost_amount) BETWEEN 17 AND 80
      AND estimated_cost_amount NOT GLOB '*[^0-9.]*'
      AND instr(estimated_cost_amount, '.')>1
      AND instr(substr(estimated_cost_amount, instr(estimated_cost_amount, '.')+1), '.')=0
      AND length(substr(estimated_cost_amount, instr(estimated_cost_amount, '.')+1))=15
    )
  ),
  model_quality_score_id TEXT,
  quality_statistics_version INTEGER CHECK (
    quality_statistics_version IS NULL
    OR (typeof(quality_statistics_version)='integer' AND quality_statistics_version>=0)
  ),
  quality_feedback_version INTEGER CHECK (
    quality_feedback_version IS NULL
    OR (typeof(quality_feedback_version)='integer' AND quality_feedback_version>=1)
  ),
  quality_evidence_state TEXT NOT NULL DEFAULT 'not_evaluated' CHECK (
    quality_evidence_state IN ('ready', 'cold', 'not_evaluated')
  ),
  quality_sample_count INTEGER CHECK (
    quality_sample_count IS NULL
    OR (typeof(quality_sample_count)='integer' AND quality_sample_count>=0)
  ),
  quality_success_count INTEGER CHECK (
    quality_success_count IS NULL
    OR (typeof(quality_success_count)='integer' AND quality_success_count>=0)
  ),
  quality_error_count INTEGER CHECK (
    quality_error_count IS NULL
    OR (typeof(quality_error_count)='integer' AND quality_error_count>=0)
  ),
  manual_quality_bps INTEGER CHECK (
    manual_quality_bps IS NULL
    OR (typeof(manual_quality_bps)='integer' AND manual_quality_bps BETWEEN 0 AND 10000)
  ),
  average_latency_ms TEXT CHECK (
    average_latency_ms IS NULL
    OR (
      length(average_latency_ms) BETWEEN 5 AND 80
      AND average_latency_ms NOT GLOB '*[^0-9.]*'
      AND instr(average_latency_ms, '.')>0
      AND instr(substr(average_latency_ms, instr(average_latency_ms, '.')+1), '.')=0
      AND length(substr(average_latency_ms, instr(average_latency_ms, '.')+1))=3
    )
  ),
  quality_component_bps INTEGER CHECK (
    quality_component_bps IS NULL
    OR (typeof(quality_component_bps)='integer' AND quality_component_bps BETWEEN 0 AND 10000)
  ),
  cost_component_bps INTEGER CHECK (
    cost_component_bps IS NULL
    OR (typeof(cost_component_bps)='integer' AND cost_component_bps BETWEEN 0 AND 10000)
  ),
  latency_component_bps INTEGER CHECK (
    latency_component_bps IS NULL
    OR (typeof(latency_component_bps)='integer' AND latency_component_bps BETWEEN 0 AND 10000)
  ),
  weighted_score_bps INTEGER CHECK (
    weighted_score_bps IS NULL
    OR (typeof(weighted_score_bps)='integer' AND weighted_score_bps BETWEEN 0 AND 10000)
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (updated_at=created_at), comparison_currency TEXT
  CHECK (comparison_currency IS NULL OR comparison_currency IN ('CNY')), comparison_fx_rate TEXT
  CHECK (
    comparison_fx_rate IS NULL
    OR (
      length(comparison_fx_rate) BETWEEN 1 AND 80
      AND comparison_fx_rate NOT GLOB '*[^0-9.]*'
      AND CAST(comparison_fx_rate AS REAL) > 0
    )
  ), comparison_fx_rate_version TEXT, comparison_cost_amount TEXT
  CHECK (
    comparison_cost_amount IS NULL
    OR (
      length(comparison_cost_amount) BETWEEN 17 AND 80
      AND comparison_cost_amount NOT GLOB '*[^0-9.]*'
      AND instr(comparison_cost_amount, '.')>1
      AND instr(substr(comparison_cost_amount, instr(comparison_cost_amount, '.')+1), '.')=0
      AND length(substr(comparison_cost_amount, instr(comparison_cost_amount, '.')+1))=15
    )
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, routing_decision_id)
    REFERENCES routing_decisions(tenant_id, routing_decision_id),
  FOREIGN KEY (tenant_id, routing_policy_id)
    REFERENCES model_alias_routing_policies(tenant_id, routing_policy_id),
  FOREIGN KEY (tenant_id, model_alias_id)
    REFERENCES model_aliases(tenant_id, model_alias_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, provider_id) REFERENCES providers(tenant_id, provider_id),
  FOREIGN KEY (tenant_id, model_quality_score_id)
    REFERENCES model_quality_scores(tenant_id, model_quality_score_id),
  UNIQUE (tenant_id, routing_decision_id, candidate_role),
  CHECK (
    (candidate_role='default' AND configured_priority=0)
    OR (candidate_role='fallback' AND configured_priority=1)
  ),
  CHECK (
    (status='selected' AND rank=1)
    OR (status='standby' AND rank=2)
    OR (status='rejected' AND rank IS NULL)
  ),
  CHECK (
    reason_code NOT IN (
      'routing.cache_affinity_pending_preferred',
      'routing.cache_affinity_warm_preferred'
    ) OR (status='selected' AND rank=1)
  ),
  CHECK (
    (price_source IN ('missing', 'not_evaluated')
      AND price_available=0 AND price_schema_version IS NULL
      AND currency IS NULL AND estimated_cost_amount IS NULL)
    OR
    (price_source IN ('workspace_provider_cost_rate', 'price_plan', 'model_catalog')
      AND price_available=1 AND price_schema_version IS NOT NULL
      AND currency IS NOT NULL AND estimated_cost_amount IS NOT NULL)
  ),
  -- δ�����в���α�����������ʵ��ready/cold �б���Я���ɸ��ּ����������֡�
  CHECK (
    (
      quality_evidence_state='not_evaluated'
      AND model_quality_score_id IS NULL
      AND quality_statistics_version IS NULL AND quality_feedback_version IS NULL
      AND quality_sample_count IS NULL AND quality_success_count IS NULL
      AND quality_error_count IS NULL AND manual_quality_bps IS NULL
      AND average_latency_ms IS NULL AND quality_component_bps IS NULL
      AND cost_component_bps IS NULL AND latency_component_bps IS NULL
      AND weighted_score_bps IS NULL
    )
    OR
    (
      quality_evidence_state IN ('ready', 'cold')
      AND model_quality_score_id IS NOT NULL
      AND quality_statistics_version IS NOT NULL AND quality_feedback_version IS NOT NULL
      AND quality_sample_count IS NOT NULL AND quality_success_count IS NOT NULL
      AND quality_error_count IS NOT NULL AND quality_component_bps IS NOT NULL
      AND quality_sample_count=quality_success_count+quality_error_count
      AND quality_statistics_version=quality_sample_count
    )
  )
)
;

INSERT INTO routing_candidates_prd173_down_new
SELECT * FROM routing_candidates;

DROP TABLE routing_candidates;
ALTER TABLE routing_candidates_prd173_down_new RENAME TO routing_candidates;

CREATE UNIQUE INDEX idx_routing_candidates_decision_rank_unique
  ON routing_candidates(tenant_id, routing_decision_id, rank)
  WHERE rank IS NOT NULL;
CREATE INDEX idx_routing_candidates_decision_order
  ON routing_candidates(tenant_id, routing_decision_id, rank, configured_priority);
CREATE INDEX idx_routing_candidates_quality_score
  ON routing_candidates(tenant_id, model_quality_score_id, created_at DESC)
  WHERE model_quality_score_id IS NOT NULL;

CREATE TRIGGER trg_routing_candidates_prd050_conflict_insert
BEFORE INSERT ON routing_candidates
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM routing_candidates current
    WHERE current.routing_candidate_id=NEW.routing_candidate_id
       OR current.rowid=NEW.rowid
  )
  OR EXISTS (
    SELECT 1 FROM routing_candidates current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.routing_decision_id=NEW.routing_decision_id
      AND current.candidate_role=NEW.candidate_role
  )
  OR (
    NEW.rank IS NOT NULL AND EXISTS (
      SELECT 1 FROM routing_candidates current
      WHERE current.tenant_id=NEW.tenant_id
        AND current.routing_decision_id=NEW.routing_decision_id
        AND current.rank=NEW.rank
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END
;
CREATE TRIGGER trg_routing_candidates_prd048_graph_insert
BEFORE INSERT ON routing_candidates
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM routing_decisions d
    WHERE d.tenant_id=NEW.tenant_id
      AND d.routing_decision_id=NEW.routing_decision_id
      AND d.model_alias_id=NEW.model_alias_id
      AND d.routing_policy_id IS NEW.routing_policy_id
  )
  OR (
    NEW.routing_policy_id IS NOT NULL AND NOT EXISTS (
      SELECT 1
      FROM routing_decisions d
      JOIN model_alias_routing_policies rp
        ON rp.tenant_id=d.tenant_id AND rp.routing_policy_id=d.routing_policy_id
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND rp.model_alias_id=d.model_alias_id
        AND rp.version=d.routing_policy_version
        AND rp.routing_mode=d.routing_mode
    )
  )
  OR NOT EXISTS (
    SELECT 1 FROM model_aliases a
    WHERE a.tenant_id=NEW.tenant_id AND a.model_alias_id=NEW.model_alias_id
      AND (
        (NEW.candidate_role='default' AND a.default_model_id=NEW.model_id)
        OR
        (NEW.candidate_role='fallback' AND a.fallback_model_id IS NOT NULL
          AND a.fallback_model_id=NEW.model_id)
      )
  )
  OR NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.model_id
      AND m.provider_id=NEW.provider_id
  )
  OR (
    NEW.status='selected' AND NOT EXISTS (
      SELECT 1 FROM routing_decisions d
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND d.decision_status='selected'
        AND d.selected_model_id=NEW.model_id
        AND d.provider_id=NEW.provider_id
    )
  )
  OR (
    EXISTS (
      SELECT 1 FROM routing_decisions d
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND d.decision_status='failed'
    )
    AND NEW.status!='rejected'
  )
  OR (
    NEW.quality_evidence_state!='not_evaluated' AND NOT EXISTS (
      SELECT 1
      FROM model_quality_scores qs
      JOIN routing_decisions d
        ON d.tenant_id=NEW.tenant_id AND d.routing_decision_id=NEW.routing_decision_id
      WHERE qs.tenant_id=NEW.tenant_id
        AND qs.model_quality_score_id=NEW.model_quality_score_id
        AND qs.model_id=NEW.model_id
        AND qs.statistics_version=NEW.quality_statistics_version
        AND qs.feedback_version=NEW.quality_feedback_version
        AND qs.sample_count=NEW.quality_sample_count
        AND qs.success_count=NEW.quality_success_count
        AND qs.error_count=NEW.quality_error_count
        AND qs.manual_quality_bps IS NEW.manual_quality_bps
        AND d.routing_mode='quality_cost'
        AND (
          (NEW.quality_evidence_state='ready'
            AND (qs.manual_quality_bps IS NOT NULL
                 OR qs.sample_count>=d.minimum_sample_count))
          OR
          (NEW.quality_evidence_state='cold'
            AND qs.manual_quality_bps IS NULL
            AND qs.sample_count<d.minimum_sample_count)
        )
    )
  )
  OR (
    NEW.quality_evidence_state='not_evaluated'
    AND EXISTS (
      SELECT 1 FROM routing_decisions d
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND d.quality_scoring_performed=1
    )
  )
  OR (
    NEW.quality_evidence_state!='not_evaluated'
    AND EXISTS (
      SELECT 1 FROM routing_decisions d
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND (
          (d.quality_scoring_performed=1 AND (
            NEW.weighted_score_bps IS NULL
            OR (d.cost_weight_bps>0 AND NEW.cost_component_bps IS NULL)
            OR (d.cost_weight_bps=0 AND NEW.cost_component_bps IS NOT NULL)
            OR (d.latency_weight_bps>0 AND NEW.latency_component_bps IS NULL)
            OR (d.latency_weight_bps=0 AND NEW.latency_component_bps IS NOT NULL)
          ))
          OR
          (d.quality_scoring_performed=0 AND (
            NEW.weighted_score_bps IS NOT NULL
            OR NEW.cost_component_bps IS NOT NULL
            OR NEW.latency_component_bps IS NOT NULL
          ))
        )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_graph_invalid');
END
;
CREATE TRIGGER trg_routing_candidates_prd048_immutable_update
BEFORE UPDATE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END
;
CREATE TRIGGER trg_routing_candidates_prd048_immutable_delete
BEFORE DELETE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END
;
CREATE TRIGGER trg_routing_decisions_prd048_candidate_bound_update
BEFORE UPDATE ON routing_decisions
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM routing_candidates candidate
  WHERE candidate.tenant_id=OLD.tenant_id
    AND candidate.routing_decision_id=OLD.routing_decision_id
)
BEGIN
  SELECT RAISE(ABORT, 'routing_decision_candidate_bound');
END
;

PRAGMA foreign_key_check;
COMMIT;
PRAGMA foreign_keys = ON;
