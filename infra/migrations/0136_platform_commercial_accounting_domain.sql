-- PRD-144 separates Platform commercial accounting from Workspace Internal FinOps.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd144_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd144_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0135' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'saas_plan_catalogs','saas_plan_versions','workspace_subscriptions',
      'subscription_orders','payment_intents','refunds','billing_statements',
      'invoice_requests','invoice_artifacts','reconciliation_runs',
      'model_price_plans','models','providers','finops_ledger_entries',
      'finops_adjustments','usage_ledger','price_snapshots','plans','quota_buckets',
      'billing_disputes','channel_statements','workspace_tenant_bindings',
      'routing_candidates'))=23
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM model_price_plans WHERE
      abs(CAST(input_price_per_1k AS REAL)*1000000.0
        - round(CAST(input_price_per_1k AS REAL)*1000000.0))>0.000001
      OR abs(CAST(output_price_per_1k AS REAL)*1000000.0
        - round(CAST(output_price_per_1k AS REAL)*1000000.0))>0.000001)
  + (SELECT COUNT(*) FROM models WHERE
      abs(CAST(input_price_per_1k AS REAL)*1000000.0
        - round(CAST(input_price_per_1k AS REAL)*1000000.0))>0.000001
      OR abs(CAST(output_price_per_1k AS REAL)*1000000.0
        - round(CAST(output_price_per_1k AS REAL)*1000000.0))>0.000001)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd144_up_guard;
CREATE TABLE routing_candidates_prd144_new (
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
  updated_at TEXT NOT NULL CHECK (updated_at=created_at),
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
  -- 未评估行不能伪造半组质量事实；ready/cold 行必须携带可复现计数和质量分。
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
);

-- 按全部 35 列搬运既有 Candidate 历史，显式保留 NULL 与快照字段语义。
INSERT INTO routing_candidates_prd144_new(
  routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
  model_alias_id, model_id, provider_id, candidate_role, configured_priority,
  rank, status, reason_code, health_state, health_fresh, keypool_state,
  price_available, price_source, price_schema_version, currency,
  estimated_cost_amount, model_quality_score_id, quality_statistics_version,
  quality_feedback_version, quality_evidence_state, quality_sample_count,
  quality_success_count, quality_error_count, manual_quality_bps,
  average_latency_ms, quality_component_bps, cost_component_bps,
  latency_component_bps, weighted_score_bps, created_at, updated_at
)
SELECT
  routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
  model_alias_id, model_id, provider_id, candidate_role, configured_priority,
  rank, status, reason_code, health_state, health_fresh, keypool_state,
  price_available, price_source, price_schema_version, currency,
  estimated_cost_amount, model_quality_score_id, quality_statistics_version,
  quality_feedback_version, quality_evidence_state, quality_sample_count,
  quality_success_count, quality_error_count, manual_quality_bps,
  average_latency_ms, quality_component_bps, cost_component_bps,
  latency_component_bps, weighted_score_bps, created_at, updated_at
FROM routing_candidates;

-- 总数、逐列 NULL 数和全列双向差集共同证明迁移没有依赖 SQLite 默认值改写历史。
CREATE TEMP TABLE prd144_candidate_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd144_candidate_copy_guard
BEFORE INSERT ON prd144_candidate_copy_guard
FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0
  OR NEW.row_difference_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd144_candidate_copy_failed');
END;
-- 在替换旧表前逐项核对行数、NULL 和内容差异，失败时让事务整体回滚。
INSERT INTO prd144_candidate_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM routing_candidates),
  (SELECT COUNT(*) FROM routing_candidates_prd144_new),
  CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE routing_policy_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE routing_policy_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE provider_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE provider_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE rank IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE rank IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE price_schema_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE price_schema_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE currency IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE currency IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE estimated_cost_amount IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE estimated_cost_amount IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE model_quality_score_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE model_quality_score_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_statistics_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE quality_statistics_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_feedback_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE quality_feedback_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_sample_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE quality_sample_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_success_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE quality_success_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_error_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE quality_error_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE manual_quality_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE manual_quality_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE average_latency_ms IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE average_latency_ms IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE quality_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE cost_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE cost_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE latency_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE latency_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE weighted_score_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd144_new WHERE weighted_score_bps IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT * FROM routing_candidates EXCEPT SELECT * FROM routing_candidates_prd144_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT * FROM routing_candidates_prd144_new EXCEPT SELECT * FROM routing_candidates
  ));
DROP TRIGGER trg_prd144_candidate_copy_guard;
DROP TABLE prd144_candidate_copy_guard;

-- 该父表 trigger 在 DROP/RENAME 空窗会使 SQLite schema 校验失败，换表前显式移除并逐字恢复。
DROP TRIGGER trg_routing_decisions_prd048_candidate_bound_update;

DROP TABLE routing_candidates;
-- 校验搬运成功后才切换正式表名，使后续索引和触发器绑定新结构。
ALTER TABLE routing_candidates_prd144_new RENAME TO routing_candidates;

CREATE UNIQUE INDEX idx_routing_candidates_decision_rank_unique
  ON routing_candidates(tenant_id, routing_decision_id, rank)
  WHERE rank IS NOT NULL;
CREATE INDEX idx_routing_candidates_decision_order
  ON routing_candidates(tenant_id, routing_decision_id, rank, configured_priority);
CREATE INDEX idx_routing_candidates_quality_score
  ON routing_candidates(tenant_id, model_quality_score_id, created_at DESC)
  WHERE model_quality_score_id IS NOT NULL;

-- INSERT 冲突守卫显式覆盖 PK、业务唯一键、部分唯一 rank 和 rowid，阻断 REPLACE 隐式删除。
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
END;

-- 完整恢复 PRD-048 的 Decision、Policy、Alias、Model、Provider 与质量快照归属图。
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
END;

CREATE TRIGGER trg_routing_candidates_prd048_immutable_update
BEFORE UPDATE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END;
CREATE TRIGGER trg_routing_candidates_prd048_immutable_delete
BEFORE DELETE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END;

-- 首条 Candidate 形成不可变解释后，Decision 的任何字段都不得再被改写。
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
END;


CREATE TABLE platform_commercial_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.commercial.plan.read','platform.commercial.plan.manage',
    'platform.commercial.subscription.read','platform.commercial.invoice.read',
    'platform.commercial.dispute.decide','platform.commercial.reconciliation.manage',
    'platform.commercial.governance.approve','platform.channel.statement.read')),
  introduced_in_migration TEXT NOT NULL CHECK(introduced_in_migration='0136')
);
INSERT INTO platform_commercial_permission_extensions(permission_code,introduced_in_migration)
VALUES
  ('platform.commercial.plan.read','0136'),
  ('platform.commercial.plan.manage','0136'),
  ('platform.commercial.subscription.read','0136'),
  ('platform.commercial.invoice.read','0136'),
  ('platform.commercial.dispute.decide','0136'),
  ('platform.commercial.reconciliation.manage','0136'),
  ('platform.commercial.governance.approve','0136'),
  ('platform.channel.statement.read','0136');
CREATE TABLE platform_commercial_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_billing','platform_channel')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES platform_commercial_permission_extensions(permission_code),
  CHECK((role_code='platform_owner' AND permission_code IN(
      'platform.commercial.plan.read','platform.commercial.subscription.read',
      'platform.commercial.invoice.read','platform.commercial.governance.approve',
      'platform.channel.statement.read'))
    OR(role_code='platform_billing' AND permission_code IN(
      'platform.commercial.plan.read','platform.commercial.plan.manage',
      'platform.commercial.subscription.read','platform.commercial.invoice.read',
      'platform.commercial.dispute.decide','platform.commercial.reconciliation.manage',
      'platform.channel.statement.read'))
    OR(role_code='platform_channel' AND permission_code='platform.channel.statement.read'))
);
INSERT INTO platform_commercial_role_permissions(role_code,permission_code)
VALUES
  ('platform_owner','platform.commercial.plan.read'),
  ('platform_owner','platform.commercial.subscription.read'),
  ('platform_owner','platform.commercial.invoice.read'),
  ('platform_owner','platform.commercial.governance.approve'),
  ('platform_owner','platform.channel.statement.read'),
  ('platform_billing','platform.commercial.plan.read'),
  ('platform_billing','platform.commercial.plan.manage'),
  ('platform_billing','platform.commercial.subscription.read'),
  ('platform_billing','platform.commercial.invoice.read'),
  ('platform_billing','platform.commercial.dispute.decide'),
  ('platform_billing','platform.commercial.reconciliation.manage'),
  ('platform_billing','platform.channel.statement.read'),
  ('platform_channel','platform.channel.statement.read');

CREATE TABLE workspace_commercial_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'workspace.commercial.subscription.read','workspace.commercial.invoice.read',
    'workspace.commercial.dispute.submit','workspace.provider_cost.read',
    'workspace.provider_cost.manage','workspace.internal_finops.read',
    'workspace.internal_finops.manage')),
  introduced_in_migration TEXT NOT NULL CHECK(introduced_in_migration='0136')
);
INSERT INTO workspace_commercial_permission_extensions(permission_code,introduced_in_migration)
VALUES
  ('workspace.commercial.subscription.read','0136'),
  ('workspace.commercial.invoice.read','0136'),
  ('workspace.commercial.dispute.submit','0136'),
  ('workspace.provider_cost.read','0136'),
  ('workspace.provider_cost.manage','0136'),
  ('workspace.internal_finops.read','0136'),
  ('workspace.internal_finops.manage','0136');
CREATE TABLE workspace_commercial_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('owner','finance_admin')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_commercial_permission_extensions(permission_code)
);
INSERT INTO workspace_commercial_role_permissions(role_code,permission_code)
SELECT role_code,permission_code
FROM (SELECT 'owner' role_code UNION ALL SELECT 'finance_admin')
CROSS JOIN workspace_commercial_permission_extensions;

CREATE TRIGGER trg_platform_commercial_permissions_insert_prd144
BEFORE INSERT ON platform_commercial_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_commercial_permissions_immutable'); END;
CREATE TRIGGER trg_platform_commercial_permissions_update_prd144
BEFORE UPDATE ON platform_commercial_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_commercial_permissions_immutable'); END;
CREATE TRIGGER trg_platform_commercial_permissions_delete_prd144
BEFORE DELETE ON platform_commercial_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_commercial_permissions_immutable'); END;
CREATE TRIGGER trg_platform_commercial_roles_insert_prd144
BEFORE INSERT ON platform_commercial_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_commercial_roles_immutable'); END;
CREATE TRIGGER trg_platform_commercial_roles_update_prd144
BEFORE UPDATE ON platform_commercial_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_commercial_roles_immutable'); END;
CREATE TRIGGER trg_platform_commercial_roles_delete_prd144
BEFORE DELETE ON platform_commercial_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_commercial_roles_immutable'); END;
CREATE TRIGGER trg_workspace_commercial_permissions_insert_prd144
BEFORE INSERT ON workspace_commercial_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_commercial_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_commercial_permissions_update_prd144
BEFORE UPDATE ON workspace_commercial_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_commercial_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_commercial_permissions_delete_prd144
BEFORE DELETE ON workspace_commercial_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_commercial_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_commercial_roles_insert_prd144
BEFORE INSERT ON workspace_commercial_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_commercial_roles_immutable'); END;
CREATE TRIGGER trg_workspace_commercial_roles_update_prd144
BEFORE UPDATE ON workspace_commercial_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_commercial_roles_immutable'); END;
CREATE TRIGGER trg_workspace_commercial_roles_delete_prd144
BEFORE DELETE ON workspace_commercial_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_commercial_roles_immutable'); END;

CREATE TABLE platform_commercial_governance_approvals(
  commercial_governance_approval_id TEXT PRIMARY KEY CHECK(
    length(commercial_governance_approval_id)=37
    AND substr(commercial_governance_approval_id,1,5)='pcga_'),
  object_kind TEXT NOT NULL CHECK(object_kind IN('plan_release','provider_reference_rate')),
  object_id TEXT NOT NULL,
  candidate_sha256 TEXT NOT NULL CHECK(length(candidate_sha256)=64),
  candidate_revision INTEGER NOT NULL CHECK(candidate_revision>=1),
  approved_by_operator_id TEXT NOT NULL,
  approved_at TEXT NOT NULL CHECK(unixepoch(approved_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at)>unixepoch(approved_at)),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(object_kind,object_id,candidate_revision)
);

CREATE TABLE platform_plan_catalog_releases(
  platform_plan_catalog_release_id TEXT PRIMARY KEY CHECK(
    length(platform_plan_catalog_release_id)=37
    AND substr(platform_plan_catalog_release_id,1,5)='pplr_'),
  plan_catalog_id TEXT NOT NULL,
  plan_version_id TEXT NOT NULL UNIQUE,
  plan_code TEXT NOT NULL,
  billing_period TEXT NOT NULL CHECK(billing_period IN('none','monthly','yearly')),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  unit_price_minor INTEGER NOT NULL CHECK(unit_price_minor BETWEEN 0 AND 1000000000000),
  entitlements_sha256 TEXT NOT NULL CHECK(length(entitlements_sha256)=64),
  contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64),
  status TEXT NOT NULL CHECK(status IN(
    'legacy_reconciled','candidate','review_required','released','retired')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  release_generation INTEGER NOT NULL CHECK(release_generation>=0),
  candidate_by_operator_id TEXT,
  governance_approval_id TEXT,
  released_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  FOREIGN KEY(plan_catalog_id) REFERENCES saas_plan_catalogs(plan_catalog_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  FOREIGN KEY(candidate_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(governance_approval_id)
    REFERENCES platform_commercial_governance_approvals(commercial_governance_approval_id),
  FOREIGN KEY(released_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='legacy_reconciled' AND release_generation=0
      AND candidate_by_operator_id IS NULL AND governance_approval_id IS NULL
      AND released_by_operator_id IS NULL)
    OR status!='legacy_reconciled')
);
CREATE INDEX idx_platform_plan_releases_page_prd144
  ON platform_plan_catalog_releases(status,plan_code,release_generation,
    platform_plan_catalog_release_id);
CREATE TABLE platform_plan_catalog_release_heads(
  plan_catalog_id TEXT PRIMARY KEY,
  platform_plan_catalog_release_id TEXT NOT NULL UNIQUE,
  release_generation INTEGER NOT NULL CHECK(release_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(plan_catalog_id) REFERENCES saas_plan_catalogs(plan_catalog_id),
  FOREIGN KEY(platform_plan_catalog_release_id)
    REFERENCES platform_plan_catalog_releases(platform_plan_catalog_release_id)
);
INSERT INTO platform_plan_catalog_releases(
  platform_plan_catalog_release_id,plan_catalog_id,plan_version_id,plan_code,
  billing_period,currency,unit_price_minor,entitlements_sha256,contract_sha256,
  status,revision,release_generation,candidate_by_operator_id,governance_approval_id,
  released_by_operator_id,created_at,updated_at,released_at)
SELECT 'pplr_'||substr(metahub_sha256('legacy-plan|'||version.plan_version_id),1,32),
  catalog.plan_catalog_id,version.plan_version_id,catalog.plan_code,
  version.billing_period,version.currency,version.unit_price_minor,
  version.entitlements_sha256,
  metahub_sha256(json_array(catalog.plan_code,version.version_number,
    version.billing_period,version.currency,version.unit_price_minor,
    version.entitlements_sha256)),
  'legacy_reconciled',1,0,NULL,NULL,NULL,version.created_at,
  coalesce(version.published_at,version.created_at),version.published_at
FROM saas_plan_versions version
JOIN saas_plan_catalogs catalog USING(plan_catalog_id)
WHERE version.status='published';
INSERT INTO platform_plan_catalog_release_heads(
  plan_catalog_id,platform_plan_catalog_release_id,release_generation,revision,updated_at)
SELECT release.plan_catalog_id,release.platform_plan_catalog_release_id,1,1,release.updated_at
FROM platform_plan_catalog_releases release
WHERE NOT EXISTS(SELECT 1 FROM platform_plan_catalog_releases later
  WHERE later.plan_catalog_id=release.plan_catalog_id
    AND(later.updated_at>release.updated_at OR(later.updated_at=release.updated_at
      AND later.platform_plan_catalog_release_id>release.platform_plan_catalog_release_id)));

CREATE TABLE platform_provider_reference_rate_releases(
  provider_reference_rate_release_id TEXT PRIMARY KEY CHECK(
    length(provider_reference_rate_release_id)=37
    AND substr(provider_reference_rate_release_id,1,5)='pprr_'),
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  input_rate_micros_per_1k INTEGER NOT NULL CHECK(input_rate_micros_per_1k>=0),
  output_rate_micros_per_1k INTEGER NOT NULL CHECK(output_rate_micros_per_1k>=0),
  effective_from TEXT NOT NULL CHECK(unixepoch(effective_from) IS NOT NULL),
  effective_until TEXT CHECK(effective_until IS NULL OR unixepoch(effective_until) IS NOT NULL),
  source_version TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('candidate','released','retired')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  release_generation INTEGER NOT NULL CHECK(release_generation>=0),
  candidate_by_operator_id TEXT NOT NULL,
  governance_approval_id TEXT,
  released_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  FOREIGN KEY(candidate_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(governance_approval_id)
    REFERENCES platform_commercial_governance_approvals(commercial_governance_approval_id),
  FOREIGN KEY(released_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(effective_until IS NULL OR unixepoch(effective_until)>unixepoch(effective_from))
);
CREATE TABLE platform_provider_reference_rate_heads(
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  currency TEXT NOT NULL,
  provider_reference_rate_release_id TEXT NOT NULL UNIQUE,
  release_generation INTEGER NOT NULL CHECK(release_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(provider_id,model_id,currency),
  FOREIGN KEY(provider_reference_rate_release_id)
    REFERENCES platform_provider_reference_rate_releases(provider_reference_rate_release_id)
);

CREATE TABLE workspace_subscription_projections(
  workspace_subscription_projection_id TEXT PRIMARY KEY CHECK(
    length(workspace_subscription_projection_id)=37
    AND substr(workspace_subscription_projection_id,1,5)='wspj_'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  subscription_id TEXT NOT NULL UNIQUE,
  plan_catalog_id TEXT NOT NULL,
  plan_version_id TEXT NOT NULL,
  platform_plan_catalog_release_id TEXT,
  subscription_status TEXT NOT NULL,
  current_period_started_at TEXT NOT NULL,
  current_period_ends_at TEXT,
  entitlement_sha256 TEXT NOT NULL CHECK(length(entitlement_sha256)=64),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  current_due_minor INTEGER,
  paid_minor INTEGER,
  refunded_minor INTEGER,
  invoiced_minor INTEGER,
  order_status TEXT NOT NULL,
  payment_status TEXT NOT NULL,
  invoice_status TEXT NOT NULL,
  dispute_status TEXT NOT NULL,
  projection_state TEXT NOT NULL CHECK(projection_state IN(
    'current','partial','stale','reconciliation_required','unknown')),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  projection_generation INTEGER NOT NULL CHECK(projection_generation>=1),
  projected_at TEXT NOT NULL CHECK(unixepoch(projected_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(subscription_id) REFERENCES workspace_subscriptions(subscription_id),
  FOREIGN KEY(plan_catalog_id) REFERENCES saas_plan_catalogs(plan_catalog_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  FOREIGN KEY(platform_plan_catalog_release_id)
    REFERENCES platform_plan_catalog_releases(platform_plan_catalog_release_id),
  UNIQUE(workspace_id,tenant_id)
);
INSERT INTO workspace_subscription_projections(
  workspace_subscription_projection_id,workspace_id,tenant_id,subscription_id,
  plan_catalog_id,plan_version_id,platform_plan_catalog_release_id,
  subscription_status,current_period_started_at,current_period_ends_at,
  entitlement_sha256,currency,current_due_minor,paid_minor,refunded_minor,
  invoiced_minor,order_status,payment_status,invoice_status,dispute_status,
  projection_state,source_watermark,source_sha256,revision,projection_generation,projected_at)
SELECT 'wspj_'||substr(metahub_sha256('subscription|'||subscription.subscription_id),1,32),
  subscription.workspace_id,subscription.tenant_id,subscription.subscription_id,
  version.plan_catalog_id,subscription.plan_version_id,release.platform_plan_catalog_release_id,
  subscription.status,subscription.current_period_started_at,subscription.current_period_ends_at,
  version.entitlements_sha256,version.currency,NULL,NULL,NULL,NULL,
  coalesce((SELECT source.status FROM subscription_orders source
    WHERE source.subscription_id=subscription.subscription_id
    ORDER BY source.updated_at DESC,source.order_id DESC LIMIT 1),'unknown'),
  'unknown',
  coalesce((SELECT source.status FROM invoice_requests source
    WHERE source.workspace_id=subscription.workspace_id
    ORDER BY source.updated_at DESC,source.invoice_request_id DESC LIMIT 1),'unknown'),
  'unknown','partial',subscription.state_revision,
  metahub_sha256(json_array(subscription.subscription_id,subscription.state_revision,
    subscription.status,subscription.plan_version_id,version.entitlements_sha256)),
  1,1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_subscriptions subscription
JOIN saas_plan_versions version ON version.plan_version_id=subscription.plan_version_id
LEFT JOIN platform_plan_catalog_releases release
  ON release.plan_version_id=subscription.plan_version_id;
CREATE INDEX idx_workspace_subscription_projection_platform_prd144
  ON workspace_subscription_projections(subscription_status,plan_catalog_id,
    projection_state,workspace_subscription_projection_id);

CREATE TABLE platform_invoices(
  platform_invoice_id TEXT PRIMARY KEY CHECK(length(platform_invoice_id)=37
    AND substr(platform_invoice_id,1,5)='pinv_'),
  source_invoice_request_id TEXT NOT NULL UNIQUE,
  source_billing_statement_id TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  subtotal_minor INTEGER NOT NULL,
  tax_rate_basis_points INTEGER NOT NULL CHECK(tax_rate_basis_points BETWEEN 0 AND 10000),
  tax_minor INTEGER NOT NULL,
  total_minor INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN(
    'requested','processing','issued','failed','reversal_pending','reversed','replaced')),
  artifact_manifest_sha256 TEXT,
  invoice_number_masked TEXT,
  provider_receipt_token TEXT,
  predecessor_platform_invoice_id TEXT,
  replacement_platform_invoice_id TEXT,
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(source_invoice_request_id) REFERENCES invoice_requests(invoice_request_id),
  FOREIGN KEY(source_billing_statement_id) REFERENCES billing_statements(billing_statement_id),
  FOREIGN KEY(predecessor_platform_invoice_id) REFERENCES platform_invoices(platform_invoice_id),
  FOREIGN KEY(replacement_platform_invoice_id) REFERENCES platform_invoices(platform_invoice_id),
  CHECK(total_minor=subtotal_minor+tax_minor)
);
CREATE TABLE platform_invoice_workspace_bindings(
  platform_invoice_workspace_binding_id TEXT PRIMARY KEY CHECK(
    length(platform_invoice_workspace_binding_id)=37
    AND substr(platform_invoice_workspace_binding_id,1,5)='piwb_'),
  platform_invoice_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_visible_status TEXT NOT NULL,
  projection_revision INTEGER NOT NULL CHECK(projection_revision>=1),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  projected_at TEXT NOT NULL CHECK(unixepoch(projected_at) IS NOT NULL),
  FOREIGN KEY(platform_invoice_id) REFERENCES platform_invoices(platform_invoice_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(platform_invoice_id,workspace_id,tenant_id)
);
INSERT INTO platform_invoices(
  platform_invoice_id,source_invoice_request_id,source_billing_statement_id,currency,
  subtotal_minor,tax_rate_basis_points,tax_minor,total_minor,status,
  artifact_manifest_sha256,invoice_number_masked,provider_receipt_token,
  predecessor_platform_invoice_id,replacement_platform_invoice_id,source_revision,
  generation,created_at,updated_at)
SELECT 'pinv_'||substr(metahub_sha256('invoice|'||request.invoice_request_id),1,32),
  request.invoice_request_id,statement.billing_statement_id,statement.currency,
  statement.subtotal_minor,statement.tax_rate_basis_points,statement.tax_minor,
  statement.total_minor,request.status,
  (SELECT artifact.content_sha256 FROM invoice_artifacts artifact
    WHERE artifact.invoice_request_id=request.invoice_request_id
    ORDER BY artifact.issued_at DESC,artifact.invoice_artifact_id DESC LIMIT 1),
  (SELECT artifact.invoice_number_masked FROM invoice_artifacts artifact
    WHERE artifact.invoice_request_id=request.invoice_request_id
    ORDER BY artifact.issued_at DESC,artifact.invoice_artifact_id DESC LIMIT 1),
  (SELECT artifact.provider_invoice_id_token FROM invoice_artifacts artifact
    WHERE artifact.invoice_request_id=request.invoice_request_id
    ORDER BY artifact.issued_at DESC,artifact.invoice_artifact_id DESC LIMIT 1),
  NULL,NULL,request.state_revision,1,request.requested_at,request.updated_at
FROM invoice_requests request
JOIN billing_statements statement USING(billing_statement_id);
INSERT INTO platform_invoice_workspace_bindings(
  platform_invoice_workspace_binding_id,platform_invoice_id,workspace_id,tenant_id,
  customer_visible_status,projection_revision,source_sha256,projected_at)
SELECT 'piwb_'||substr(metahub_sha256('invoice-binding|'||request.invoice_request_id),1,32),
  invoice.platform_invoice_id,request.workspace_id,request.tenant_id,request.status,
  request.state_revision,
  metahub_sha256(json_array(request.invoice_request_id,request.workspace_id,
    request.tenant_id,request.status,request.state_revision)),
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM invoice_requests request
JOIN platform_invoices invoice ON invoice.source_invoice_request_id=request.invoice_request_id;
CREATE INDEX idx_platform_invoices_page_prd144
  ON platform_invoices(status,updated_at,platform_invoice_id);
CREATE INDEX idx_invoice_bindings_workspace_prd144
  ON platform_invoice_workspace_bindings(workspace_id,projected_at,platform_invoice_id);

CREATE TABLE billing_dispute_cases(
  billing_dispute_case_id TEXT PRIMARY KEY CHECK(length(billing_dispute_case_id)=36
    AND substr(billing_dispute_case_id,1,4)='bdc_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN('statement','invoice','statement_line')),
  source_id TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  disputed_amount_minor INTEGER NOT NULL CHECK(disputed_amount_minor>0),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'amount_mismatch','duplicate_charge','service_not_delivered','tax_mismatch')),
  status TEXT NOT NULL CHECK(status IN(
    'submitted','under_review','approved','rejected','withdrawn')),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  submitted_by_membership_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  resolved_at TEXT CHECK(resolved_at IS NULL OR unixepoch(resolved_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(submitted_by_membership_id)
    REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(workspace_id,tenant_id,source_kind,source_id)
);
CREATE TABLE billing_dispute_case_items(
  billing_dispute_case_item_id TEXT PRIMARY KEY CHECK(
    length(billing_dispute_case_item_id)=37
    AND substr(billing_dispute_case_item_id,1,5)='bdci_'),
  billing_dispute_case_id TEXT NOT NULL,
  source_line_id TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  source_amount_minor INTEGER NOT NULL,
  disputable_amount_minor INTEGER NOT NULL CHECK(disputable_amount_minor>0),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(billing_dispute_case_id)
    REFERENCES billing_dispute_cases(billing_dispute_case_id),
  UNIQUE(billing_dispute_case_id,source_line_id)
);
CREATE TABLE billing_dispute_case_transitions(
  billing_dispute_case_transition_id TEXT PRIMARY KEY CHECK(
    length(billing_dispute_case_transition_id)=37
    AND substr(billing_dispute_case_transition_id,1,5)='bdct_'),
  billing_dispute_case_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN(
    'submit','start_review','approve_credit','reject','withdraw')),
  from_status TEXT,
  to_status TEXT NOT NULL,
  expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
  new_revision INTEGER NOT NULL CHECK(new_revision=expected_revision+1),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('workspace','platform')),
  actor_id TEXT NOT NULL,
  decision_code TEXT,
  idempotency_hmac TEXT NOT NULL,
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(billing_dispute_case_id)
    REFERENCES billing_dispute_cases(billing_dispute_case_id),
  UNIQUE(actor_domain,actor_id,idempotency_hmac),
  UNIQUE(billing_dispute_case_id,new_revision)
);
CREATE TABLE billing_dispute_case_decisions(
  billing_dispute_case_decision_id TEXT PRIMARY KEY CHECK(
    length(billing_dispute_case_decision_id)=37
    AND substr(billing_dispute_case_decision_id,1,5)='bdcd_'),
  billing_dispute_case_id TEXT NOT NULL UNIQUE,
  decision TEXT NOT NULL CHECK(decision IN('approved','rejected')),
  decision_code TEXT NOT NULL CHECK(decision_code IN(
    'facts_confirmed','duplicate_confirmed','service_credit','claim_not_supported')),
  credited_amount_minor INTEGER NOT NULL CHECK(credited_amount_minor>=0),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  decided_by_operator_id TEXT NOT NULL,
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(billing_dispute_case_id)
    REFERENCES billing_dispute_cases(billing_dispute_case_id),
  FOREIGN KEY(decided_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((decision='approved' AND credited_amount_minor>0)
    OR(decision='rejected' AND credited_amount_minor=0))
);
CREATE TABLE platform_commercial_adjustments(
  platform_commercial_adjustment_id TEXT PRIMARY KEY CHECK(
    length(platform_commercial_adjustment_id)=37
    AND substr(platform_commercial_adjustment_id,1,5)='bcad_'),
  billing_dispute_case_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
  adjustment_kind TEXT NOT NULL CHECK(adjustment_kind='dispute_credit'),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(billing_dispute_case_id)
    REFERENCES billing_dispute_cases(billing_dispute_case_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_billing_dispute_platform_page_prd144
  ON billing_dispute_cases(status,updated_at,billing_dispute_case_id);
CREATE INDEX idx_billing_dispute_workspace_page_prd144
  ON billing_dispute_cases(workspace_id,updated_at,billing_dispute_case_id);

CREATE TABLE workspace_internal_finops_disputes(
  workspace_internal_finops_dispute_id TEXT PRIMARY KEY CHECK(
    length(workspace_internal_finops_dispute_id)=37
    AND substr(workspace_internal_finops_dispute_id,1,5)='wifd_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  legacy_billing_dispute_id TEXT NOT NULL UNIQUE,
  legacy_status TEXT NOT NULL,
  reason_sha256 TEXT NOT NULL CHECK(length(reason_sha256)=64),
  decision_sha256 TEXT,
  reason_ciphertext TEXT,
  decision_ciphertext TEXT,
  migration_state TEXT NOT NULL CHECK(migration_state IN('pending_kms','sealed_scrubbed')),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  created_at TEXT NOT NULL,
  migrated_at TEXT,
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(legacy_billing_dispute_id) REFERENCES billing_disputes(billing_dispute_id),
  CHECK((migration_state='pending_kms' AND reason_ciphertext IS NULL
      AND decision_ciphertext IS NULL AND migrated_at IS NULL)
    OR(migration_state='sealed_scrubbed' AND reason_ciphertext IS NOT NULL
      AND migrated_at IS NOT NULL))
);
INSERT INTO workspace_internal_finops_disputes(
  workspace_internal_finops_dispute_id,workspace_id,tenant_id,
  legacy_billing_dispute_id,legacy_status,reason_sha256,decision_sha256,
  reason_ciphertext,decision_ciphertext,migration_state,source_sha256,created_at,migrated_at)
SELECT 'wifd_'||substr(metahub_sha256('legacy-dispute|'||dispute.billing_dispute_id),1,32),
  binding.workspace_id,dispute.tenant_id,dispute.billing_dispute_id,dispute.status,
  dispute.reason_sha256,dispute.decision_reason_sha256,NULL,NULL,'pending_kms',
  metahub_sha256(json_array(dispute.billing_dispute_id,dispute.status,
    dispute.reason_sha256,dispute.decision_reason_sha256,dispute.state_revision)),
  dispute.created_at,NULL
FROM billing_disputes dispute
JOIN workspace_tenant_bindings binding ON binding.tenant_id=dispute.tenant_id
WHERE binding.status='active';

CREATE TABLE workspace_provider_cost_rates(
  workspace_provider_cost_rate_id TEXT PRIMARY KEY CHECK(
    length(workspace_provider_cost_rate_id)=36
    AND substr(workspace_provider_cost_rate_id,1,4)='pcr_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(tenant_id,model_id) REFERENCES models(tenant_id,model_id),
  UNIQUE(workspace_id,tenant_id,provider_id,model_id,currency)
);
CREATE TABLE workspace_provider_cost_rate_versions(
  workspace_provider_cost_rate_version_id TEXT PRIMARY KEY CHECK(
    length(workspace_provider_cost_rate_version_id)=37
    AND substr(workspace_provider_cost_rate_version_id,1,5)='pcrv_'),
  workspace_provider_cost_rate_id TEXT NOT NULL,
  version_number INTEGER NOT NULL CHECK(version_number>=1),
  input_rate_micros_per_1k INTEGER NOT NULL CHECK(input_rate_micros_per_1k>=0),
  output_rate_micros_per_1k INTEGER NOT NULL CHECK(output_rate_micros_per_1k>=0),
  effective_from TEXT NOT NULL CHECK(unixepoch(effective_from) IS NOT NULL),
  effective_until TEXT CHECK(effective_until IS NULL OR unixepoch(effective_until) IS NOT NULL),
  source_type TEXT NOT NULL CHECK(source_type IN(
    'manual','platform_reference','legacy_model_price','legacy_model')),
  source_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('active','superseded','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  created_by_membership_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_provider_cost_rate_id)
    REFERENCES workspace_provider_cost_rates(workspace_provider_cost_rate_id),
  FOREIGN KEY(created_by_membership_id)
    REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(workspace_provider_cost_rate_id,version_number),
  UNIQUE(workspace_provider_cost_rate_id,source_type,source_id),
  CHECK(effective_until IS NULL OR unixepoch(effective_until)>unixepoch(effective_from))
);
CREATE TABLE workspace_provider_cost_rate_heads(
  workspace_provider_cost_rate_id TEXT PRIMARY KEY,
  workspace_provider_cost_rate_version_id TEXT NOT NULL UNIQUE,
  head_revision INTEGER NOT NULL CHECK(head_revision>=1),
  adoption_state TEXT NOT NULL CHECK(adoption_state IN('active','disabled')),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_provider_cost_rate_id)
    REFERENCES workspace_provider_cost_rates(workspace_provider_cost_rate_id),
  FOREIGN KEY(workspace_provider_cost_rate_version_id)
    REFERENCES workspace_provider_cost_rate_versions(workspace_provider_cost_rate_version_id)
);
CREATE INDEX idx_workspace_provider_cost_rates_page_prd144
  ON workspace_provider_cost_rates(workspace_id,provider_id,model_id,currency,
    workspace_provider_cost_rate_id);
INSERT INTO workspace_provider_cost_rates(
  workspace_provider_cost_rate_id,workspace_id,tenant_id,provider_id,model_id,currency,created_at)
SELECT 'pcr_'||substr(metahub_sha256(json_array(binding.workspace_id,plan.tenant_id,
    model.provider_id,plan.model_id,plan.currency)),1,32),
  binding.workspace_id,plan.tenant_id,model.provider_id,plan.model_id,plan.currency,
  min(plan.created_at)
FROM model_price_plans plan
JOIN models model ON model.tenant_id=plan.tenant_id AND model.model_id=plan.model_id
JOIN workspace_tenant_bindings binding ON binding.tenant_id=plan.tenant_id
WHERE binding.status='active'
GROUP BY binding.workspace_id,plan.tenant_id,model.provider_id,plan.model_id,plan.currency;
INSERT OR IGNORE INTO workspace_provider_cost_rates(
  workspace_provider_cost_rate_id,workspace_id,tenant_id,provider_id,model_id,currency,created_at)
SELECT 'pcr_'||substr(metahub_sha256(json_array(binding.workspace_id,model.tenant_id,
    model.provider_id,model.model_id,model.currency)),1,32),
  binding.workspace_id,model.tenant_id,model.provider_id,model.model_id,model.currency,
  model.created_at
FROM models model
JOIN workspace_tenant_bindings binding ON binding.tenant_id=model.tenant_id
WHERE binding.status='active' AND NOT EXISTS(
  SELECT 1 FROM model_price_plans plan
  WHERE plan.tenant_id=model.tenant_id AND plan.model_id=model.model_id);
INSERT INTO workspace_provider_cost_rate_versions(
  workspace_provider_cost_rate_version_id,workspace_provider_cost_rate_id,version_number,
  input_rate_micros_per_1k,output_rate_micros_per_1k,effective_from,effective_until,
  source_type,source_id,source_sha256,status,revision,content_sha256,
  created_by_membership_id,created_at)
SELECT 'pcrv_'||substr(metahub_sha256('legacy-price|'||plan.price_plan_id),1,32),
  rate.workspace_provider_cost_rate_id,
  row_number() OVER(PARTITION BY rate.workspace_provider_cost_rate_id
    ORDER BY plan.effective_from,plan.price_plan_id),
  CAST(round(CAST(plan.input_price_per_1k AS REAL)*1000000.0) AS INTEGER),
  CAST(round(CAST(plan.output_price_per_1k AS REAL)*1000000.0) AS INTEGER),
  plan.effective_from,
  (SELECT min(later.effective_from) FROM model_price_plans later
    WHERE later.tenant_id=plan.tenant_id AND later.model_id=plan.model_id
      AND later.currency=plan.currency AND later.effective_from>plan.effective_from),
  'legacy_model_price',plan.price_plan_id,
  metahub_sha256(json_array(plan.price_plan_id,plan.price_schema_version,
    plan.input_price_per_1k,plan.output_price_per_1k,plan.currency,plan.effective_from)),
  CASE WHEN EXISTS(SELECT 1 FROM model_price_plans later
    WHERE later.tenant_id=plan.tenant_id AND later.model_id=plan.model_id
      AND later.currency=plan.currency AND later.effective_from>plan.effective_from)
    THEN 'superseded' ELSE 'active' END,1,
  metahub_sha256(json_array(plan.currency,
    CAST(round(CAST(plan.input_price_per_1k AS REAL)*1000000.0) AS INTEGER),
    CAST(round(CAST(plan.output_price_per_1k AS REAL)*1000000.0) AS INTEGER),
    plan.effective_from)),NULL,plan.created_at
FROM model_price_plans plan
JOIN models model ON model.tenant_id=plan.tenant_id AND model.model_id=plan.model_id
JOIN workspace_tenant_bindings binding ON binding.tenant_id=plan.tenant_id
JOIN workspace_provider_cost_rates rate
  ON rate.workspace_id=binding.workspace_id AND rate.tenant_id=plan.tenant_id
  AND rate.provider_id=model.provider_id AND rate.model_id=plan.model_id
  AND rate.currency=plan.currency
WHERE binding.status='active';
INSERT INTO workspace_provider_cost_rate_versions(
  workspace_provider_cost_rate_version_id,workspace_provider_cost_rate_id,version_number,
  input_rate_micros_per_1k,output_rate_micros_per_1k,effective_from,effective_until,
  source_type,source_id,source_sha256,status,revision,content_sha256,
  created_by_membership_id,created_at)
SELECT 'pcrv_'||substr(metahub_sha256('legacy-model|'||model.model_id),1,32),
  rate.workspace_provider_cost_rate_id,1,
  CAST(round(CAST(model.input_price_per_1k AS REAL)*1000000.0) AS INTEGER),
  CAST(round(CAST(model.output_price_per_1k AS REAL)*1000000.0) AS INTEGER),
  model.created_at,NULL,'legacy_model',model.model_id,
  metahub_sha256(json_array(model.model_id,model.price_schema_version,
    model.input_price_per_1k,model.output_price_per_1k,model.currency)),
  'active',1,
  metahub_sha256(json_array(model.currency,
    CAST(round(CAST(model.input_price_per_1k AS REAL)*1000000.0) AS INTEGER),
    CAST(round(CAST(model.output_price_per_1k AS REAL)*1000000.0) AS INTEGER),
    model.created_at)),NULL,model.created_at
FROM models model
JOIN workspace_tenant_bindings binding ON binding.tenant_id=model.tenant_id
JOIN workspace_provider_cost_rates rate
  ON rate.workspace_id=binding.workspace_id AND rate.tenant_id=model.tenant_id
  AND rate.provider_id=model.provider_id AND rate.model_id=model.model_id
  AND rate.currency=model.currency
WHERE binding.status='active' AND NOT EXISTS(
  SELECT 1 FROM model_price_plans plan
  WHERE plan.tenant_id=model.tenant_id AND plan.model_id=model.model_id);
INSERT INTO workspace_provider_cost_rate_heads(
  workspace_provider_cost_rate_id,workspace_provider_cost_rate_version_id,
  head_revision,adoption_state,updated_at)
SELECT version.workspace_provider_cost_rate_id,
  version.workspace_provider_cost_rate_version_id,version.revision,
  CASE version.status WHEN 'disabled' THEN 'disabled' ELSE 'active' END,version.created_at
FROM workspace_provider_cost_rate_versions version
WHERE version.status='active';

CREATE TABLE price_snapshot_provider_cost_rate_bindings(
  price_snapshot_provider_cost_rate_binding_id TEXT PRIMARY KEY CHECK(
    length(price_snapshot_provider_cost_rate_binding_id)=37
    AND substr(price_snapshot_provider_cost_rate_binding_id,1,5)='psrb_'),
  price_snapshot_id TEXT NOT NULL UNIQUE,
  workspace_provider_cost_rate_version_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(price_snapshot_id) REFERENCES price_snapshots(price_snapshot_id),
  FOREIGN KEY(workspace_provider_cost_rate_version_id)
    REFERENCES workspace_provider_cost_rate_versions(
      workspace_provider_cost_rate_version_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
INSERT INTO price_snapshot_provider_cost_rate_bindings
SELECT 'psrb_'||substr(metahub_sha256('price-rate|'||snapshot.price_snapshot_id),1,32),
  snapshot.price_snapshot_id,version.workspace_provider_cost_rate_version_id,
  rate.workspace_id,rate.tenant_id,
  metahub_sha256(json_array(snapshot.price_snapshot_id,
    version.workspace_provider_cost_rate_version_id,rate.workspace_id,rate.tenant_id)),
  1,snapshot.created_at
FROM price_snapshots snapshot
JOIN workspace_provider_cost_rate_versions version
  ON (snapshot.price_plan_id IS NOT NULL
      AND version.source_type='legacy_model_price'
      AND version.source_id=snapshot.price_plan_id)
  OR (snapshot.price_plan_id IS NULL AND snapshot.model_id IS NOT NULL
      AND version.source_type='legacy_model' AND version.source_id=snapshot.model_id)
JOIN workspace_provider_cost_rates rate
  ON rate.workspace_provider_cost_rate_id=version.workspace_provider_cost_rate_id
 AND rate.tenant_id=snapshot.tenant_id AND rate.model_id=snapshot.model_id
 AND rate.currency=snapshot.currency
 AND (snapshot.provider_id IS NULL OR rate.provider_id=snapshot.provider_id)
WHERE snapshot.source NOT IN('finops_unpriced','finops_legacy_backfill');

CREATE TABLE internal_finops_ledger_owner_bindings(
  internal_finops_owner_binding_id TEXT PRIMARY KEY CHECK(
    length(internal_finops_owner_binding_id)=37
    AND substr(internal_finops_owner_binding_id,1,5)='ifob_'),
  source_kind TEXT NOT NULL CHECK(source_kind IN(
    'ledger_entry','adjustment','usage_ledger','price_snapshot')),
  source_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(source_kind,source_id)
);
INSERT INTO internal_finops_ledger_owner_bindings
SELECT 'ifob_'||substr(metahub_sha256('ledger|'||entry.finops_ledger_entry_id),1,32),
  'ledger_entry',entry.finops_ledger_entry_id,binding.workspace_id,entry.tenant_id,
  metahub_sha256(json_array(entry.finops_ledger_entry_id,entry.ledger_sequence,
    entry.currency,entry.amount_micros,entry.status)),1,entry.created_at
FROM finops_ledger_entries entry JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=entry.tenant_id WHERE binding.status='active';
INSERT INTO internal_finops_ledger_owner_bindings
SELECT 'ifob_'||substr(metahub_sha256('adjustment|'||item.finops_adjustment_id),1,32),
  'adjustment',item.finops_adjustment_id,binding.workspace_id,item.tenant_id,
  metahub_sha256(json_array(item.finops_adjustment_id,item.currency,item.amount_micros)),
  1,item.created_at
FROM finops_adjustments item JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=item.tenant_id WHERE binding.status='active';
INSERT INTO internal_finops_ledger_owner_bindings
SELECT 'ifob_'||substr(metahub_sha256('usage|'||item.usage_ledger_id),1,32),
  'usage_ledger',item.usage_ledger_id,binding.workspace_id,item.tenant_id,
  metahub_sha256(json_array(item.usage_ledger_id,item.tenant_id)),1,item.created_at
FROM usage_ledger item JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=item.tenant_id WHERE binding.status='active';
INSERT INTO internal_finops_ledger_owner_bindings
SELECT 'ifob_'||substr(metahub_sha256('snapshot|'||item.price_snapshot_id),1,32),
  'price_snapshot',item.price_snapshot_id,binding.workspace_id,item.tenant_id,
  metahub_sha256(json_array(item.price_snapshot_id,item.currency,
    item.price_schema_version,item.price_plan_id)),1,item.created_at
FROM price_snapshots item JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=item.tenant_id WHERE binding.status='active';

CREATE TABLE workspace_quota_owner_bindings(
  workspace_quota_owner_binding_id TEXT PRIMARY KEY CHECK(
    length(workspace_quota_owner_binding_id)=37
    AND substr(workspace_quota_owner_binding_id,1,5)='qwob_'),
  source_kind TEXT NOT NULL CHECK(source_kind IN(
    'plan','bucket','adjustment','reservation','reservation_item','settlement',
    'allocation','event','outbox')),
  source_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(source_kind,source_id)
);
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('plan|'||source.plan_id),1,32),'plan',source.plan_id,
  binding.workspace_id,source.tenant_id,metahub_sha256(json_array(source.plan_id,
    source.state_revision,source.quota_generation)),1,source.created_at
FROM plans source JOIN workspace_tenant_bindings binding ON binding.tenant_id=source.tenant_id
WHERE binding.status='active';
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('bucket|'||source.quota_bucket_id),1,32),'bucket',
  source.quota_bucket_id,binding.workspace_id,source.tenant_id,
  metahub_sha256(json_array(source.quota_bucket_id,source.state_revision,source.status)),
  1,source.created_at
FROM quota_buckets source JOIN workspace_tenant_bindings binding ON binding.tenant_id=source.tenant_id
WHERE binding.status='active';
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('adjustment|'||source.quota_adjustment_id),1,32),
  'adjustment',source.quota_adjustment_id,binding.workspace_id,source.tenant_id,
  metahub_sha256(source.quota_adjustment_id),1,source.created_at
FROM quota_adjustments source JOIN workspace_tenant_bindings binding ON binding.tenant_id=source.tenant_id
WHERE binding.status='active';
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('reservation|'||source.quota_reservation_id),1,32),
  'reservation',source.quota_reservation_id,binding.workspace_id,source.tenant_id,
  metahub_sha256(source.quota_reservation_id),1,source.created_at
FROM quota_reservations source JOIN workspace_tenant_bindings binding ON binding.tenant_id=source.tenant_id
WHERE binding.status='active';
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('reservation-item|'||source.quota_reservation_item_id),1,32),
  'reservation_item',source.quota_reservation_item_id,binding.workspace_id,source.tenant_id,
  metahub_sha256(source.quota_reservation_item_id),1,reservation.created_at
FROM quota_reservation_items source
JOIN quota_reservations reservation USING(quota_reservation_id)
JOIN workspace_tenant_bindings binding ON binding.tenant_id=source.tenant_id
WHERE binding.status='active';
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('settlement|'||source.quota_usage_settlement_id),1,32),
  'settlement',source.quota_usage_settlement_id,binding.workspace_id,source.tenant_id,
  metahub_sha256(source.quota_usage_settlement_id),1,source.created_at
FROM quota_usage_settlements source JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=source.tenant_id WHERE binding.status='active';
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('allocation|'||source.quota_usage_allocation_id),1,32),
  'allocation',source.quota_usage_allocation_id,binding.workspace_id,source.tenant_id,
  metahub_sha256(source.quota_usage_allocation_id),1,settlement.created_at
FROM quota_usage_allocations source
JOIN quota_usage_settlements settlement USING(quota_usage_settlement_id)
JOIN workspace_tenant_bindings binding ON binding.tenant_id=source.tenant_id
WHERE binding.status='active';
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('event|'||source.quota_event_id),1,32),'event',
  source.quota_event_id,binding.workspace_id,source.tenant_id,
  metahub_sha256(source.quota_event_id),1,source.occurred_at
FROM quota_events source JOIN workspace_tenant_bindings binding ON binding.tenant_id=source.tenant_id
WHERE binding.status='active';
INSERT INTO workspace_quota_owner_bindings
SELECT 'qwob_'||substr(metahub_sha256('outbox|'||source.quota_event_outbox_id),1,32),
  'outbox',source.quota_event_outbox_id,binding.workspace_id,source.tenant_id,
  metahub_sha256(json_array(source.quota_event_outbox_id,source.status,
    source.attempt_count)),1,source.created_at
FROM quota_event_outbox source JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=source.tenant_id WHERE binding.status='active';

CREATE TABLE billing_domain_reconciliations(
  billing_domain_reconciliation_id TEXT PRIMARY KEY CHECK(
    length(billing_domain_reconciliation_id)=36
    AND substr(billing_domain_reconciliation_id,1,4)='bdr_'),
  status TEXT NOT NULL CHECK(status IN('queued','processing','completed','failed')),
  source_reconciliation_run_id TEXT NOT NULL UNIQUE,
  source_set_sha256 TEXT NOT NULL CHECK(length(source_set_sha256)=64),
  result_sha256 TEXT,
  difference_count INTEGER NOT NULL CHECK(difference_count>=0),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_by_operator_id TEXT NOT NULL,
  permission_sha256 TEXT NOT NULL CHECK(length(permission_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(source_reconciliation_run_id) REFERENCES reconciliation_runs(reconciliation_run_id),
  CHECK((status IN('queued','processing') AND result_sha256 IS NULL AND completed_at IS NULL)
    OR(status='completed' AND length(result_sha256)=64 AND completed_at IS NOT NULL)
    OR(status='failed' AND result_sha256 IS NULL AND completed_at IS NOT NULL))
);
CREATE INDEX idx_billing_domain_reconciliations_status_prd144
  ON billing_domain_reconciliations(status,created_at,billing_domain_reconciliation_id);
CREATE TRIGGER trg_billing_domain_reconciliation_update_prd144
BEFORE UPDATE ON billing_domain_reconciliations FOR EACH ROW
WHEN NEW.billing_domain_reconciliation_id!=OLD.billing_domain_reconciliation_id
  OR NEW.source_reconciliation_run_id!=OLD.source_reconciliation_run_id
  OR NEW.source_set_sha256!=OLD.source_set_sha256
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.permission_sha256!=OLD.permission_sha256 OR NEW.created_at!=OLD.created_at
  OR NEW.generation<OLD.generation
  OR NOT((OLD.status='queued' AND NEW.status IN('processing','failed'))
    OR(OLD.status='processing' AND NEW.status IN('completed','failed')))
BEGIN SELECT RAISE(ABORT,'billing_domain_reconciliation_state_invalid'); END;
CREATE TRIGGER trg_billing_domain_reconciliation_delete_prd144
BEFORE DELETE ON billing_domain_reconciliations
BEGIN SELECT RAISE(ABORT,'billing_domain_reconciliation_immutable'); END;
CREATE TABLE billing_domain_reconciliation_source_snapshots(
  billing_domain_reconciliation_source_snapshot_id TEXT PRIMARY KEY CHECK(
    length(billing_domain_reconciliation_source_snapshot_id)=37
    AND substr(billing_domain_reconciliation_source_snapshot_id,1,5)='bdrs_'),
  billing_domain_reconciliation_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  workspace_id TEXT,
  tenant_id TEXT,
  currency TEXT,
  row_count INTEGER NOT NULL CHECK(row_count>=0),
  max_sequence INTEGER NOT NULL CHECK(max_sequence>=0),
  amount_minor_or_micros INTEGER NOT NULL,
  owner_coverage_count INTEGER NOT NULL CHECK(owner_coverage_count>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  FOREIGN KEY(billing_domain_reconciliation_id)
    REFERENCES billing_domain_reconciliations(billing_domain_reconciliation_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(billing_domain_reconciliation_id,source_kind,workspace_id,tenant_id,currency),
  CHECK((workspace_id IS NULL AND tenant_id IS NULL)
    OR(workspace_id IS NOT NULL AND tenant_id IS NOT NULL))
);
CREATE TRIGGER trg_billing_domain_reconciliation_source_update_prd144
BEFORE UPDATE ON billing_domain_reconciliation_source_snapshots
BEGIN SELECT RAISE(ABORT,'billing_domain_reconciliation_source_immutable'); END;
CREATE TRIGGER trg_billing_domain_reconciliation_source_delete_prd144
BEFORE DELETE ON billing_domain_reconciliation_source_snapshots
BEGIN SELECT RAISE(ABORT,'billing_domain_reconciliation_source_immutable'); END;
CREATE TABLE billing_domain_reconciliation_differences(
  billing_domain_reconciliation_difference_id TEXT PRIMARY KEY CHECK(
    length(billing_domain_reconciliation_difference_id)=37
    AND substr(billing_domain_reconciliation_difference_id,1,5)='bdrd_'),
  billing_domain_reconciliation_id TEXT NOT NULL,
  workspace_id TEXT,
  tenant_id TEXT,
  difference_type TEXT NOT NULL,
  left_source_ref TEXT NOT NULL,
  right_source_ref TEXT,
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  amount_difference_minor INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN('open','acknowledged','adjusted')),
  adjustment_reference TEXT,
  detected_at TEXT NOT NULL CHECK(unixepoch(detected_at) IS NOT NULL),
  FOREIGN KEY(billing_domain_reconciliation_id)
    REFERENCES billing_domain_reconciliations(billing_domain_reconciliation_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(billing_domain_reconciliation_id,difference_type,left_source_ref),
  CHECK((workspace_id IS NULL AND tenant_id IS NULL)
    OR(workspace_id IS NOT NULL AND tenant_id IS NOT NULL)),
  CHECK((status IN('open','acknowledged') AND adjustment_reference IS NULL)
    OR(status='adjusted' AND length(adjustment_reference) BETWEEN 8 AND 128))
);
CREATE INDEX idx_billing_domain_differences_cursor_prd144
  ON billing_domain_reconciliation_differences(
    billing_domain_reconciliation_id,billing_domain_reconciliation_difference_id);
CREATE INDEX idx_reconciliation_differences_billing_domain_prd144
  ON reconciliation_differences(
    reconciliation_run_id,workspace_id,tenant_id,currency,reconciliation_difference_id);
CREATE TRIGGER trg_billing_domain_reconciliation_difference_update_prd144
BEFORE UPDATE ON billing_domain_reconciliation_differences FOR EACH ROW
WHEN NEW.billing_domain_reconciliation_difference_id
      !=OLD.billing_domain_reconciliation_difference_id
  OR NEW.billing_domain_reconciliation_id!=OLD.billing_domain_reconciliation_id
  OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.difference_type!=OLD.difference_type
  OR NEW.left_source_ref!=OLD.left_source_ref
  OR NEW.right_source_ref IS NOT OLD.right_source_ref
  OR NEW.currency!=OLD.currency
  OR NEW.amount_difference_minor!=OLD.amount_difference_minor
  OR NEW.detected_at!=OLD.detected_at
  OR NOT((OLD.status='open' AND NEW.status IN('acknowledged','adjusted'))
    OR(OLD.status='acknowledged' AND NEW.status='adjusted'))
BEGIN SELECT RAISE(ABORT,'billing_domain_reconciliation_difference_state_invalid'); END;
CREATE TRIGGER trg_billing_domain_reconciliation_difference_delete_prd144
BEFORE DELETE ON billing_domain_reconciliation_differences
BEGIN SELECT RAISE(ABORT,'billing_domain_reconciliation_difference_immutable'); END;

CREATE TABLE commercial_accounting_commands(
  commercial_accounting_command_id TEXT PRIMARY KEY CHECK(
    length(commercial_accounting_command_id)=37
    AND substr(commercial_accounting_command_id,1,5)='cacm_'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace')),
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  object_kind TEXT NOT NULL,
  object_id TEXT NOT NULL,
  owner_sha256 TEXT NOT NULL CHECK(length(owner_sha256)=64),
  idempotency_hmac TEXT NOT NULL CHECK(length(idempotency_hmac)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
  permission_revision INTEGER NOT NULL CHECK(permission_revision>=1),
  result_id TEXT NOT NULL,
  result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(actor_domain,actor_id,idempotency_hmac)
);
CREATE TABLE commercial_accounting_events(
  commercial_accounting_event_id TEXT PRIMARY KEY CHECK(
    length(commercial_accounting_event_id)=37
    AND substr(commercial_accounting_event_id,1,5)='caev_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'plan.released','subscription.changed','billing_dispute.submitted',
    'billing_dispute.decided','provider_cost_rate.changed')),
  object_kind TEXT NOT NULL,
  object_id TEXT NOT NULL,
  owner_sha256 TEXT NOT NULL CHECK(length(owner_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(CAST(payload_json AS BLOB))<=4096),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  UNIQUE(event_type,object_id,revision)
);
CREATE TABLE commercial_accounting_event_outbox(
  commercial_accounting_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(commercial_accounting_event_outbox_id)=37
    AND substr(commercial_accounting_event_outbox_id,1,5)='caob_'),
  commercial_accounting_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','succeeded','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  lease_generation INTEGER NOT NULL CHECK(lease_generation>=0),
  lease_owner_id TEXT,
  lease_token_hmac TEXT,
  lease_expires_at TEXT,
  next_attempt_at TEXT NOT NULL,
  dispatched_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(commercial_accounting_event_id)
    REFERENCES commercial_accounting_events(commercial_accounting_event_id)
);
CREATE INDEX idx_commercial_outbox_due_prd144
  ON commercial_accounting_event_outbox(status,next_attempt_at,lease_expires_at,created_at);

CREATE TABLE commercial_accounting_scheduler_state(
  worker_kind TEXT PRIMARY KEY CHECK(worker_kind IN(
    'quota_trial_outbox','subscription_trial','meter','payment_dunning_refund',
    'invoice_reconciliation','legacy_dispute','projection','commercial_outbox',
    'reference_rate','cleanup')),
  cursor_value TEXT,
  fence_generation INTEGER NOT NULL CHECK(fence_generation>=1),
  lease_owner_id TEXT,
  lease_token_hmac TEXT,
  lease_expires_at TEXT,
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO commercial_accounting_scheduler_state
SELECT worker_kind,NULL,1,NULL,NULL,NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (SELECT 'quota_trial_outbox' worker_kind UNION ALL SELECT 'subscription_trial'
  UNION ALL SELECT 'meter' UNION ALL SELECT 'payment_dunning_refund'
  UNION ALL SELECT 'invoice_reconciliation' UNION ALL SELECT 'legacy_dispute'
  UNION ALL SELECT 'projection' UNION ALL SELECT 'commercial_outbox'
  UNION ALL SELECT 'reference_rate' UNION ALL SELECT 'cleanup');

CREATE TABLE commercial_accounting_migration_cursors(
  source_kind TEXT PRIMARY KEY,
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  owner_coverage_count INTEGER NOT NULL CHECK(owner_coverage_count>=0),
  state TEXT NOT NULL CHECK(state IN('backfilled','blocked')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO commercial_accounting_migration_cursors
VALUES
  ('plan_catalog',(SELECT COUNT(*) FROM saas_plan_versions),
    metahub_sha256('plan_catalog:'||(SELECT COUNT(*) FROM saas_plan_versions)),
    (SELECT COUNT(*) FROM platform_plan_catalog_releases),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('subscriptions',(SELECT COUNT(*) FROM workspace_subscriptions),
    metahub_sha256('subscriptions:'||(SELECT COUNT(*) FROM workspace_subscriptions)),
    (SELECT COUNT(*) FROM workspace_subscription_projections),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('payments',(SELECT COUNT(*) FROM payment_intents)+(SELECT COUNT(*) FROM refunds),
    metahub_sha256('payments:'||((SELECT COUNT(*) FROM payment_intents)+(SELECT COUNT(*) FROM refunds))),
    (SELECT COUNT(DISTINCT workspace_id) FROM payment_intents),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('invoices',(SELECT COUNT(*) FROM invoice_requests),
    metahub_sha256('invoices:'||(SELECT COUNT(*) FROM invoice_requests)),
    (SELECT COUNT(*) FROM platform_invoice_workspace_bindings),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('legacy_disputes',(SELECT COUNT(*) FROM billing_disputes),
    metahub_sha256('legacy_disputes:'||(SELECT COUNT(*) FROM billing_disputes)),
    (SELECT COUNT(*) FROM workspace_internal_finops_disputes),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('provider_rates',(SELECT COUNT(*) FROM model_price_plans),
    metahub_sha256('provider_rates:'||(SELECT COUNT(*) FROM model_price_plans)),
    (SELECT COUNT(*) FROM workspace_provider_cost_rate_versions),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('internal_finops',(SELECT COUNT(*) FROM finops_ledger_entries),
    metahub_sha256('internal_finops:'||(SELECT COUNT(*) FROM finops_ledger_entries)),
    (SELECT COUNT(*) FROM internal_finops_ledger_owner_bindings),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('quotas',(SELECT COUNT(*) FROM plans),
    metahub_sha256('quotas:'||(SELECT COUNT(*) FROM plans)),
    (SELECT COUNT(*) FROM workspace_quota_owner_bindings),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('channel_statements',(SELECT COUNT(*) FROM channel_statements),
    metahub_sha256('channel_statements:'||(SELECT COUNT(*) FROM channel_statements)),
    (SELECT COUNT(*) FROM channel_statements),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('reconciliation',(SELECT COUNT(*) FROM reconciliation_runs),
    metahub_sha256('reconciliation:'||(SELECT COUNT(*) FROM reconciliation_runs)),
    (SELECT COUNT(*) FROM reconciliation_runs),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TRIGGER trg_plan_release_delete_prd144 BEFORE DELETE ON platform_plan_catalog_releases
BEGIN SELECT RAISE(ABORT,'platform_plan_release_immutable'); END;
CREATE TRIGGER trg_reference_rate_delete_prd144 BEFORE DELETE ON platform_provider_reference_rate_releases
BEGIN SELECT RAISE(ABORT,'platform_reference_rate_immutable'); END;
CREATE TRIGGER trg_platform_invoice_update_prd144 BEFORE UPDATE ON platform_invoices
BEGIN SELECT RAISE(ABORT,'platform_invoice_immutable'); END;
CREATE TRIGGER trg_platform_invoice_delete_prd144 BEFORE DELETE ON platform_invoices
BEGIN SELECT RAISE(ABORT,'platform_invoice_immutable'); END;
CREATE TRIGGER trg_cost_rate_version_update_prd144 BEFORE UPDATE ON workspace_provider_cost_rate_versions
BEGIN SELECT RAISE(ABORT,'workspace_provider_cost_rate_version_immutable'); END;
CREATE TRIGGER trg_cost_rate_version_delete_prd144 BEFORE DELETE ON workspace_provider_cost_rate_versions
BEGIN SELECT RAISE(ABORT,'workspace_provider_cost_rate_version_immutable'); END;
CREATE TRIGGER trg_commercial_event_update_prd144 BEFORE UPDATE ON commercial_accounting_events
BEGIN SELECT RAISE(ABORT,'commercial_accounting_event_immutable'); END;
CREATE TRIGGER trg_commercial_event_delete_prd144 BEFORE DELETE ON commercial_accounting_events
BEGIN SELECT RAISE(ABORT,'commercial_accounting_event_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;

CREATE TEMP TABLE prd144_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd144_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + CASE WHEN (SELECT COUNT(*) FROM platform_commercial_permission_extensions)=8
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_commercial_role_permissions)=13
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_commercial_role_permissions)=14
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM commercial_accounting_scheduler_state)=10
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_subscription_projections)=
      (SELECT COUNT(*) FROM workspace_subscriptions) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_invoice_workspace_bindings)=
      (SELECT COUNT(*) FROM invoice_requests) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_internal_finops_disputes)=
      (SELECT COUNT(*) FROM billing_disputes) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM internal_finops_ledger_owner_bindings)=
      ((SELECT COUNT(*) FROM finops_ledger_entries)+(SELECT COUNT(*) FROM finops_adjustments)
       +(SELECT COUNT(*) FROM usage_ledger)+(SELECT COUNT(*) FROM price_snapshots))
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_quota_owner_bindings)=
      ((SELECT COUNT(*) FROM plans)+(SELECT COUNT(*) FROM quota_buckets)
       +(SELECT COUNT(*) FROM quota_adjustments)+(SELECT COUNT(*) FROM quota_reservations)
       +(SELECT COUNT(*) FROM quota_reservation_items)
       +(SELECT COUNT(*) FROM quota_usage_settlements)
       +(SELECT COUNT(*) FROM quota_usage_allocations)+(SELECT COUNT(*) FROM quota_events)
       +(SELECT COUNT(*) FROM quota_event_outbox)) THEN 0 ELSE 1 END
  + CASE WHEN coalesce(instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='routing_candidates'),
      'workspace_provider_cost_rate'),0)>0 THEN 0 ELSE 1 END;
DROP TABLE prd144_post_guard;
