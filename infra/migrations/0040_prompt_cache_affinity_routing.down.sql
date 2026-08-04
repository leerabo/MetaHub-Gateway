-- PRD-050 条件回滚：存在任何亲和配置、运行事实、解释或投递事实时一律拒绝降级。
-- 仅空事实库可以保真恢复 0039 Candidate/Audit 合同，不能静默删除生产数据。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- TEMP 门禁必须先于任一持久 DDL/DML；固定错误码让迁移器安全回滚并保留 0040 applied。
CREATE TEMP TABLE prd050_down_business_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd050_down_business_guard
BEFORE INSERT ON prd050_down_business_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd050_down_business_facts_present');
END;
-- 在任何 DDL 前枚举全部 PRD-050 事实，存在业务数据时必须原子拒绝回滚。
INSERT INTO prd050_down_business_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM cache_affinities)
  + (SELECT COUNT(*) FROM cache_affinity_scopes)
  + (SELECT COUNT(*) FROM cache_affinity_bindings)
  + (SELECT COUNT(*) FROM cache_affinity_selections)
  + (SELECT COUNT(*) FROM cache_affinity_config_events)
  + (SELECT COUNT(*) FROM routing_candidates
     WHERE reason_code IN (
       'routing.cache_affinity_pending_preferred',
       'routing.cache_affinity_warm_preferred'
     ))
  + (SELECT COUNT(*) FROM routing_decisions
     WHERE json_type(candidate_summary, '$.cache_affinity') IS NOT NULL)
  + (SELECT COUNT(*) FROM audit_logs
     WHERE action IN ('cache_affinity.created', 'cache_affinity.updated')
        OR object_type='cache_affinity')
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE EXISTS (
       SELECT 1 FROM json_each(endpoint.event_types_json) item
       WHERE item.value='cache.affinity_selected'
     ))
  + (SELECT COUNT(*) FROM webhook_deliveries delivery
     WHERE delivery.event_type='cache.affinity_selected')
  + (SELECT COUNT(*)
     FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='cache.affinity_selected');
DROP TRIGGER trg_prd050_down_business_guard;
DROP TABLE prd050_down_business_guard;

-- Candidate 按 0039 的 35 列和原 reason 枚举重建；历史基础路由事实逐值保留。
DROP TABLE IF EXISTS routing_candidates_prd050_down;
CREATE TABLE routing_candidates_prd050_down (
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
      'routing.candidate.quality_cold_start_deferred'
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
    price_source IN ('price_plan', 'model_catalog', 'missing', 'not_evaluated')
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
    (price_source IN ('missing', 'not_evaluated')
      AND price_available=0 AND price_schema_version IS NULL
      AND currency IS NULL AND estimated_cost_amount IS NULL)
    OR
    (price_source IN ('price_plan', 'model_catalog')
      AND price_available=1 AND price_schema_version IS NOT NULL
      AND currency IS NOT NULL AND estimated_cost_amount IS NOT NULL)
  ),
  -- 0039 的未评估/ready/cold 质量证据矩阵必须原样恢复。
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
-- 按 0039 的 35 列合同搬回 Candidate，去除新枚举但不改变旧历史字段。
INSERT INTO routing_candidates_prd050_down(
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
CREATE TEMP TABLE prd050_candidate_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd050_candidate_copy_guard
BEFORE INSERT ON prd050_candidate_copy_guard
FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0
  OR NEW.row_difference_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd050_candidate_copy_failed');
END;
-- 切回旧表前比较行数、NULL 和逐行内容，任何差异都终止整个回滚事务。
INSERT INTO prd050_candidate_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM routing_candidates),
  (SELECT COUNT(*) FROM routing_candidates_prd050_down),
  CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE routing_policy_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE routing_policy_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE provider_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE provider_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE rank IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE rank IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE price_schema_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE price_schema_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE currency IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE currency IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE estimated_cost_amount IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE estimated_cost_amount IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE model_quality_score_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE model_quality_score_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_statistics_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE quality_statistics_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_feedback_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE quality_feedback_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_sample_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE quality_sample_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_success_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE quality_success_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_error_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE quality_error_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE manual_quality_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE manual_quality_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE average_latency_ms IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE average_latency_ms IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE quality_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE cost_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE cost_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE latency_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE latency_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE weighted_score_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_down WHERE weighted_score_bps IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT * FROM routing_candidates EXCEPT SELECT * FROM routing_candidates_prd050_down
  ))
  + (SELECT COUNT(*) FROM (
    SELECT * FROM routing_candidates_prd050_down EXCEPT SELECT * FROM routing_candidates
  ));
DROP TRIGGER trg_prd050_candidate_copy_guard;
DROP TABLE prd050_candidate_copy_guard;

-- 该父表 trigger 在 DROP/RENAME 空窗会使 SQLite schema 校验失败，换表前显式移除并逐字恢复。
DROP TRIGGER trg_routing_decisions_prd048_candidate_bound_update;

DROP TABLE routing_candidates;
-- 复制守卫通过后恢复 0039 正式表名，再重建该版本的索引与触发器。
ALTER TABLE routing_candidates_prd050_down RENAME TO routing_candidates;

CREATE UNIQUE INDEX idx_routing_candidates_decision_rank_unique
  ON routing_candidates(tenant_id, routing_decision_id, rank)
  WHERE rank IS NOT NULL;
CREATE INDEX idx_routing_candidates_decision_order
  ON routing_candidates(tenant_id, routing_decision_id, rank, configured_priority);
CREATE INDEX idx_routing_candidates_quality_score
  ON routing_candidates(tenant_id, model_quality_score_id, created_at DESC)
  WHERE model_quality_score_id IS NOT NULL;

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
-- 五张 PRD-050 表已经由门禁证明为空，按子到父顺序移除不会丢失事实。
DROP TABLE cache_affinity_config_events;
DROP TABLE cache_affinity_selections;
DROP TABLE cache_affinity_bindings;
DROP TABLE cache_affinity_scopes;
DROP TABLE cache_affinities;

-- Audit 恢复 0039 枚举并逐列保真复制。
-- Audit 只扩展 CacheAffinity 配置动作；旧行 17 列必须逐值、逐 NULL 保真。
DROP TABLE IF EXISTS audit_logs_prd050_down;
CREATE TABLE audit_logs_prd050_down (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role IN ('admin', 'developer', 'viewer')),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created', 'provider.updated',
      'provider_key.created', 'provider_key.updated',
      'virtual_key.created', 'virtual_key.updated',
      'budget_rule.created', 'budget_rule.updated',
      'dlp_rule.created', 'dlp_rule.updated',
      'webhook_delivery.replayed', 'webhook_endpoint.secret_rotated',
      'usage_export.requested', 'usage_export.downloaded',
      'policy.created', 'policy.updated',
      'policy_snapshot.published', 'policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created', 'routing_policy.updated',
      'model_quality_score.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score'
    )
  ),
  object_id TEXT NOT NULL,
  object_name TEXT,
  before_summary_json TEXT NOT NULL CHECK (
    json_valid(before_summary_json) AND json_type(before_summary_json)='object'
  ),
  after_summary_json TEXT NOT NULL CHECK (
    json_valid(after_summary_json) AND json_type(after_summary_json)='object'
  ),
  changed_fields_json TEXT NOT NULL CHECK (
    json_valid(changed_fields_json) AND json_type(changed_fields_json)='array'
  ),
  client_ip TEXT,
  user_agent TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- 全列搬回 0039 审计结构，保留既有审计事实并剔除仅属于 PRD-050 的枚举。
INSERT INTO audit_logs_prd050_down(
  rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
)
SELECT
  rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
FROM audit_logs;

CREATE TEMP TABLE prd050_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd050_audit_copy_guard
BEFORE INSERT ON prd050_audit_copy_guard
FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0
  OR NEW.row_difference_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd050_audit_copy_failed');
END;
-- 切表前核对审计源表和回滚目标表，防止可追踪历史在降级时静默损坏。
INSERT INTO prd050_audit_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd050_down),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd050_down WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd050_down WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd050_down WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs
    EXCEPT SELECT rowid, * FROM audit_logs_prd050_down
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs_prd050_down
    EXCEPT SELECT rowid, * FROM audit_logs
  ));
DROP TRIGGER trg_prd050_audit_copy_guard;
DROP TABLE prd050_audit_copy_guard;

-- 0040 专属 Audit 守卫必须在恢复 0039 表前显式移除；随后切表不会遗留同名 trigger。
DROP TRIGGER trg_audit_logs_prd050_conflict_insert;
-- 早期已登记的 0040 尚未包含负 rowid AFTER 守卫；维护性 rollback/reapply 必须同时
-- 兼容该已部署结构，而 down 末尾的闭集断言仍保证新旧两种来源都不残留 PRD-050 trigger。
DROP TRIGGER IF EXISTS trg_audit_logs_prd050_rowid_insert;
DROP TABLE audit_logs;
-- 审计复制确认无损后恢复正式表名，使 0039 查询和索引合同继续成立。
ALTER TABLE audit_logs_prd050_down RENAME TO audit_logs;

CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id, created_at DESC, audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id, actor_user_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id, object_type, object_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id, action, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id, request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action = 'policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='model_quality_score.updated';

CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.user_id=NEW.actor_user_id AND user.tenant_id=NEW.tenant_id
)
BEGIN
  SELECT RAISE(ABORT, 'audit_log_owner_mismatch');
END;
CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

-- 回滚完成前证明 0039 的表、枚举、索引和不可变触发器已完整恢复。
CREATE TEMP TABLE prd050_down_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd050_down_integrity_guard
BEFORE INSERT ON prd050_down_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd050_down_integrity_failed');
END;
-- 结束回滚前核对 0039 对象全集，避免缺表或残留 PRD-050 对象进入可运行状态。
INSERT INTO prd050_down_integrity_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0039' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='table' AND name IN (
       'cache_affinities', 'cache_affinity_scopes', 'cache_affinity_bindings',
       'cache_affinity_selections', 'cache_affinity_config_events'
     ))
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN ('routing_candidates', 'audit_logs')
  )=2 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND name IN (
      'idx_routing_candidates_decision_rank_unique',
      'idx_routing_candidates_decision_order',
      'idx_routing_candidates_quality_score',
      'idx_audit_logs_tenant_time',
      'idx_audit_logs_tenant_actor_time',
      'idx_audit_logs_tenant_object_time',
      'idx_audit_logs_tenant_action_time',
      'idx_audit_logs_tenant_request',
      'idx_audit_logs_dry_run_request_unique',
      'idx_audit_logs_model_quality_request_unique'
    )
  )=10 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_routing_candidates_prd048_graph_insert',
      'trg_routing_candidates_prd048_immutable_update',
      'trg_routing_candidates_prd048_immutable_delete',
      'trg_routing_decisions_prd048_candidate_bound_update',
      'trg_audit_logs_owner_insert',
      'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete',
      'trg_request_feedback_conflict_insert_prd049',
      'trg_request_feedback_event_conflict_insert_prd049',
      'trg_request_log_feedback_parent_conflict_insert_prd049',
      'trg_request_log_feedback_parent_conflict_update_prd049',
      'trg_routing_attempt_feedback_parent_conflict_insert_prd049',
      'trg_routing_attempt_feedback_parent_conflict_update_prd049'
    )
  )=13 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='trigger' AND name IN (
       'trg_audit_logs_prd050_conflict_insert',
       'trg_audit_logs_prd050_rowid_insert'
     ))
  + CASE WHEN EXISTS (
    SELECT 1 FROM sqlite_master
    WHERE type='table' AND name='routing_candidates'
      AND sql NOT LIKE '%routing.cache_affinity_pending_preferred%'
      AND sql NOT LIKE '%routing.cache_affinity_warm_preferred%'
  ) THEN 0 ELSE 1 END
  + CASE WHEN EXISTS (
    SELECT 1 FROM sqlite_master
    WHERE type='table' AND name='audit_logs'
      AND sql NOT LIKE '%cache_affinity.created%'
      AND sql NOT LIKE '%cache_affinity.updated%'
  ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER trg_prd050_down_integrity_guard;
DROP TABLE prd050_down_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
