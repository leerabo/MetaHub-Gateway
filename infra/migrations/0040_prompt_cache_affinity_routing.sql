-- PRD-050 Prompt Cache 感知粘性路由：只保存不可逆摘要、路由绑定与安全审计快照。
-- Prompt、Response、Provider Key、Endpoint URL 和上游模型名不得进入本迁移创建的任何表。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 0040 会保真重建 Candidate 与 Audit；开始前必须证明 0039 基线及其关键防线完整存在。
CREATE TEMP TABLE prd050_up_prerequisite_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd050_up_prerequisite_guard
BEFORE INSERT ON prd050_up_prerequisite_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd050_prerequisite_missing');
END;
-- 在任何结构变更前确认 0039 基线完整，避免对未知版本执行不可逆重建。
INSERT INTO prd050_up_prerequisite_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0039' AND status='applied'
  )=1 THEN 0 ELSE 1 END
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
      'trg_audit_logs_owner_insert',
      'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete'
    )
  )=6 THEN 0 ELSE 1 END;
DROP TRIGGER trg_prd050_up_prerequisite_guard;
DROP TABLE prd050_up_prerequisite_guard;

-- Candidate 按 0039 的全列顺序重建；唯一合同变化是允许两个亲和升格原因。
DROP TABLE IF EXISTS routing_candidates_prd050_new;
CREATE TABLE routing_candidates_prd050_new (
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
    (price_source IN ('price_plan', 'model_catalog')
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
INSERT INTO routing_candidates_prd050_new(
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
-- 在替换旧表前逐项核对行数、NULL 和内容差异，失败时让事务整体回滚。
INSERT INTO prd050_candidate_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM routing_candidates),
  (SELECT COUNT(*) FROM routing_candidates_prd050_new),
  CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE routing_policy_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE routing_policy_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE provider_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE provider_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE rank IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE rank IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE price_schema_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE price_schema_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE currency IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE currency IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE estimated_cost_amount IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE estimated_cost_amount IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE model_quality_score_id IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE model_quality_score_id IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_statistics_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE quality_statistics_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_feedback_version IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE quality_feedback_version IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_sample_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE quality_sample_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_success_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE quality_success_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_error_count IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE quality_error_count IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE manual_quality_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE manual_quality_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE average_latency_ms IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE average_latency_ms IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE quality_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE quality_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE cost_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE cost_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE latency_component_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE latency_component_bps IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM routing_candidates WHERE weighted_score_bps IS NULL)
    =(SELECT COUNT(*) FROM routing_candidates_prd050_new WHERE weighted_score_bps IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT * FROM routing_candidates EXCEPT SELECT * FROM routing_candidates_prd050_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT * FROM routing_candidates_prd050_new EXCEPT SELECT * FROM routing_candidates
  ));
DROP TRIGGER trg_prd050_candidate_copy_guard;
DROP TABLE prd050_candidate_copy_guard;

-- 该父表 trigger 在 DROP/RENAME 空窗会使 SQLite schema 校验失败，换表前显式移除并逐字恢复。
DROP TRIGGER trg_routing_decisions_prd048_candidate_bound_update;

DROP TABLE routing_candidates;
-- 校验搬运成功后才切换正式表名，使后续索引和触发器绑定新结构。
ALTER TABLE routing_candidates_prd050_new RENAME TO routing_candidates;

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
-- Alias 级配置只保存可信分类 code 与数值门限，不保存 Prompt、Key 或 Endpoint 信息。
CREATE TABLE cache_affinities (
  cache_affinity_id TEXT PRIMARY KEY CHECK (
    length(cache_affinity_id)=36
    AND substr(cache_affinity_id, 1, 4)='caf_'
    AND substr(cache_affinity_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('enabled', 'disabled')),
  data_class TEXT CHECK (
    data_class IS NULL OR (
      length(data_class) BETWEEN 1 AND 64
      AND substr(data_class, 1, 1) GLOB '[a-z0-9]'
      AND data_class NOT GLOB '*[^a-z0-9._-]*'
    )
  ),
  minimum_prefix_chars INTEGER NOT NULL DEFAULT 1024 CHECK (
    typeof(minimum_prefix_chars)='integer'
    AND minimum_prefix_chars BETWEEN 256 AND 16384
  ),
  ttl_seconds INTEGER NOT NULL DEFAULT 3600 CHECK (
    typeof(ttl_seconds)='integer' AND ttl_seconds BETWEEN 60 AND 86400
  ),
  version INTEGER NOT NULL CHECK (typeof(version)='integer' AND version>=1),
  created_by TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, model_alias_id)
    REFERENCES model_aliases(tenant_id, model_alias_id),
  FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, updated_by) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, cache_affinity_id),
  UNIQUE (tenant_id, model_alias_id),
  CHECK (
    (status='enabled' AND data_class IS NOT NULL)
    OR status='disabled'
  )
);

-- Scope 是 tenant + Virtual Key + data_class + Prompt 摘要的四维分组；摘要不可逆且版本化。
CREATE TABLE cache_affinity_scopes (
  cache_affinity_scope_id TEXT PRIMARY KEY CHECK (
    length(cache_affinity_scope_id)=37
    AND substr(cache_affinity_scope_id, 1, 5)='cafs_'
    AND substr(cache_affinity_scope_id, 6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  virtual_key_id TEXT NOT NULL,
  digest_schema_version INTEGER NOT NULL DEFAULT 1 CHECK (
    typeof(digest_schema_version)='integer' AND digest_schema_version=1
  ),
  affinity_scope_digest TEXT NOT NULL CHECK (
    length(affinity_scope_digest)=64
    AND affinity_scope_digest NOT GLOB '*[^0-9a-f]*'
  ),
  preferred_binding_id TEXT,
  preferred_lease_generation INTEGER CHECK (
    preferred_lease_generation IS NULL
    OR (typeof(preferred_lease_generation)='integer' AND preferred_lease_generation>=1)
  ),
  last_selected_at TEXT NOT NULL CHECK (length(last_selected_at) BETWEEN 1 AND 64),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, virtual_key_id)
    REFERENCES virtual_keys(tenant_id, virtual_key_id),
  UNIQUE (tenant_id, cache_affinity_scope_id),
  UNIQUE (
    tenant_id, virtual_key_id, digest_schema_version, affinity_scope_digest
  ),
  CHECK (
    (preferred_binding_id IS NULL AND preferred_lease_generation IS NULL)
    OR (preferred_binding_id IS NOT NULL AND preferred_lease_generation IS NOT NULL)
  )
);

-- Binding 每行只代表一个 scope + endpoint revision；Credential 是可重绑快照而非长期外键。
CREATE TABLE cache_affinity_bindings (
  cache_affinity_binding_id TEXT PRIMARY KEY CHECK (
    length(cache_affinity_binding_id)=36
    AND substr(cache_affinity_binding_id, 1, 4)='cab_'
    AND substr(cache_affinity_binding_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  cache_affinity_scope_id TEXT NOT NULL,
  affinity_key_digest TEXT NOT NULL CHECK (
    length(affinity_key_digest)=64
    AND affinity_key_digest NOT GLOB '*[^0-9a-f]*'
  ),
  model_endpoint_id TEXT NOT NULL,
  endpoint_revision_digest TEXT NOT NULL CHECK (
    length(endpoint_revision_digest)=64
    AND endpoint_revision_digest NOT GLOB '*[^0-9a-f]*'
  ),
  provider_id TEXT NOT NULL,
  provider_credential_id TEXT,
  target_kind TEXT NOT NULL CHECK (
    target_kind IN ('unassigned', 'managed', 'environment', 'none')
  ),
  target_generation INTEGER NOT NULL CHECK (
    typeof(target_generation)='integer' AND target_generation>=1
  ),
  pending_lease_generation INTEGER NOT NULL CHECK (
    typeof(pending_lease_generation)='integer' AND pending_lease_generation>=1
  ),
  pending_lease_started_at TEXT NOT NULL CHECK (
    length(pending_lease_started_at) BETWEEN 1 AND 64
  ),
  pending_lease_expires_at TEXT CHECK (
    pending_lease_expires_at IS NULL
    OR length(pending_lease_expires_at) BETWEEN 1 AND 64
  ),
  active_selection_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(active_selection_count)='integer' AND active_selection_count>=0
  ),
  last_success_at TEXT CHECK (
    last_success_at IS NULL OR length(last_success_at) BETWEEN 1 AND 64
  ),
  selection_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(selection_count)='integer' AND selection_count>=0
  ),
  estimated_hit_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(estimated_hit_count)='integer' AND estimated_hit_count>=0
  ),
  last_selected_at TEXT NOT NULL CHECK (length(last_selected_at) BETWEEN 1 AND 64),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, cache_affinity_scope_id)
    REFERENCES cache_affinity_scopes(tenant_id, cache_affinity_scope_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, model_endpoint_id)
    REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, provider_id)
    REFERENCES providers(tenant_id, provider_id),
  UNIQUE (tenant_id, cache_affinity_binding_id),
  UNIQUE (
    tenant_id, cache_affinity_scope_id, model_endpoint_id,
    endpoint_revision_digest
  ),
  UNIQUE (tenant_id, affinity_key_digest),
  CHECK (
    (target_kind='managed' AND provider_credential_id IS NOT NULL)
    OR (target_kind!='managed' AND provider_credential_id IS NULL)
  ),
  CHECK (target_kind!='unassigned' OR last_success_at IS NULL),
  CHECK (estimated_hit_count<=selection_count)
);

-- Selection 在 Provider 前冻结完整目标；父运行状态均为无长期 FK 的安全快照，便于淘汰 Binding。
CREATE TABLE cache_affinity_selections (
  cache_affinity_selection_id TEXT PRIMARY KEY CHECK (
    length(cache_affinity_selection_id)=36
    AND substr(cache_affinity_selection_id, 1, 4)='cas_'
    AND substr(cache_affinity_selection_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  attempt_sequence INTEGER NOT NULL CHECK (
    typeof(attempt_sequence)='integer' AND attempt_sequence IN (1, 2)
  ),
  routing_attempt_id TEXT,
  stream_session_id TEXT,
  cache_affinity_id TEXT NOT NULL,
  cache_affinity_version INTEGER NOT NULL CHECK (
    typeof(cache_affinity_version)='integer' AND cache_affinity_version>=1
  ),
  cache_affinity_scope_id_snapshot TEXT NOT NULL,
  cache_affinity_binding_id_snapshot TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  model_endpoint_id TEXT NOT NULL,
  endpoint_revision_digest_snapshot TEXT NOT NULL CHECK (
    length(endpoint_revision_digest_snapshot)=64
    AND endpoint_revision_digest_snapshot NOT GLOB '*[^0-9a-f]*'
  ),
  provider_id TEXT NOT NULL,
  provider_code_snapshot TEXT NOT NULL CHECK (
    length(provider_code_snapshot) BETWEEN 1 AND 128
  ),
  provider_credential_id_snapshot TEXT,
  target_kind TEXT NOT NULL CHECK (
    target_kind IN ('managed', 'environment', 'none')
  ),
  target_generation INTEGER NOT NULL CHECK (
    typeof(target_generation)='integer' AND target_generation>=1
  ),
  pending_lease_generation_snapshot INTEGER NOT NULL CHECK (
    typeof(pending_lease_generation_snapshot)='integer'
    AND pending_lease_generation_snapshot>=1
  ),
  ttl_seconds_snapshot INTEGER NOT NULL CHECK (
    typeof(ttl_seconds_snapshot)='integer' AND ttl_seconds_snapshot BETWEEN 60 AND 86400
  ),
  warm_evidence_at_snapshot TEXT CHECK (
    warm_evidence_at_snapshot IS NULL
    OR length(warm_evidence_at_snapshot) BETWEEN 1 AND 64
  ),
  endpoint_preference_applied INTEGER NOT NULL CHECK (
    typeof(endpoint_preference_applied)='integer'
    AND endpoint_preference_applied IN (0, 1)
  ),
  endpoint_preference_state TEXT NOT NULL CHECK (
    endpoint_preference_state IN ('none', 'pending', 'warm')
  ),
  binding_state_before TEXT NOT NULL CHECK (
    binding_state_before IN ('missing', 'pending', 'warm', 'expired', 'ineligible')
  ),
  selection_reason TEXT NOT NULL CHECK (
    selection_reason IN (
      'binding_created', 'binding_pending_reused',
      'binding_warm_reused', 'binding_rebound'
    )
  ),
  estimated_hit INTEGER NOT NULL CHECK (
    typeof(estimated_hit)='integer' AND estimated_hit IN (0, 1)
  ),
  outcome_status TEXT NOT NULL DEFAULT 'pending' CHECK (
    outcome_status IN ('pending', 'succeeded', 'failed')
  ),
  outcome_at TEXT CHECK (outcome_at IS NULL OR length(outcome_at) BETWEEN 1 AND 64),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id, cache_affinity_selection_id),
  UNIQUE (tenant_id, request_id, attempt_sequence),
  CHECK (
    (routing_attempt_id IS NOT NULL AND stream_session_id IS NULL)
    OR (routing_attempt_id IS NULL AND stream_session_id IS NOT NULL)
  ),
  CHECK (
    (target_kind='managed' AND provider_credential_id_snapshot IS NOT NULL)
    OR (target_kind!='managed' AND provider_credential_id_snapshot IS NULL)
  ),
  CHECK (
    (outcome_status='pending' AND outcome_at IS NULL)
    OR (outcome_status IN ('succeeded', 'failed') AND outcome_at IS NOT NULL)
  ),
  CHECK (
    estimated_hit=0 OR (
      selection_reason='binding_warm_reused'
      AND binding_state_before='warm'
      AND endpoint_preference_state='warm'
      AND warm_evidence_at_snapshot IS NOT NULL
    )
  ),
  CHECK (endpoint_preference_state!='none' OR endpoint_preference_applied=0)
);

-- 配置事件消费 operation_request_id，支持结果未知后的同 actor 幂等恢复。
CREATE TABLE cache_affinity_config_events (
  cache_affinity_config_event_id TEXT PRIMARY KEY CHECK (
    length(cache_affinity_config_event_id)=36
    AND substr(cache_affinity_config_event_id, 1, 4)='cae_'
    AND substr(cache_affinity_config_event_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  cache_affinity_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  cache_affinity_version INTEGER NOT NULL CHECK (
    typeof(cache_affinity_version)='integer' AND cache_affinity_version>=1
  ),
  actor_user_id TEXT NOT NULL,
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128
  ),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (
    length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  event_type TEXT NOT NULL CHECK (event_type IN ('created', 'updated', 'noop')),
  before_json TEXT NOT NULL CHECK (
    json_valid(before_json) AND json_type(before_json)='object'
  ),
  after_json TEXT NOT NULL CHECK (
    json_valid(after_json) AND json_type(after_json)='object'
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, cache_affinity_config_event_id),
  UNIQUE (tenant_id, actor_user_id, operation_request_id),
  CHECK (event_type!='created' OR before_json='{}'),
  CHECK (event_type!='noop' OR before_json=after_json)
);

-- 运行热路径索引全部以 tenant 开头，避免容量清理或详情查询退化成跨租户扫描。
CREATE INDEX idx_cache_affinities_tenant_alias
  ON cache_affinities(tenant_id, model_alias_id);
CREATE INDEX idx_cache_affinity_scopes_tenant_key_lru
  ON cache_affinity_scopes(tenant_id, virtual_key_id, last_selected_at, cache_affinity_scope_id);
CREATE INDEX idx_cache_affinity_scopes_tenant_lru
  ON cache_affinity_scopes(tenant_id, last_selected_at, cache_affinity_scope_id);
CREATE INDEX idx_cache_affinity_scopes_preferred
  ON cache_affinity_scopes(tenant_id, preferred_binding_id)
  WHERE preferred_binding_id IS NOT NULL;
CREATE INDEX idx_cache_affinity_bindings_scope_lru
  ON cache_affinity_bindings(
    tenant_id, cache_affinity_scope_id, last_selected_at, cache_affinity_binding_id
  );
CREATE INDEX idx_cache_affinity_bindings_cleanup
  ON cache_affinity_bindings(
    tenant_id, pending_lease_expires_at, last_success_at, last_selected_at
  );
CREATE INDEX idx_cache_affinity_bindings_credential
  ON cache_affinity_bindings(tenant_id, provider_credential_id)
  WHERE provider_credential_id IS NOT NULL;
CREATE INDEX idx_cache_affinity_selections_request
  ON cache_affinity_selections(tenant_id, request_id, attempt_sequence);
CREATE INDEX idx_cache_affinity_selections_pending
  ON cache_affinity_selections(tenant_id, outcome_status, created_at)
  WHERE outcome_status='pending';
CREATE INDEX idx_cache_affinity_selections_binding_pending
  ON cache_affinity_selections(
    tenant_id, cache_affinity_binding_id_snapshot,
    pending_lease_generation_snapshot, outcome_status
  );
CREATE INDEX idx_cache_affinity_config_events_affinity_version
  ON cache_affinity_config_events(
    tenant_id, cache_affinity_id, cache_affinity_version DESC, created_at DESC
  );
-- 配置写入先校验完整租户图；复合外键之外保留固定错误，便于服务层统一转换。
CREATE TRIGGER trg_cache_affinities_prd050_owner_insert
BEFORE INSERT ON cache_affinities
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM model_aliases alias
    WHERE alias.tenant_id=NEW.tenant_id AND alias.model_alias_id=NEW.model_alias_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.created_by
  )
  OR NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.updated_by
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_owner_mismatch');
END;

-- INSERT OR REPLACE 的隐式 DELETE 默认不递归触发，必须在插入前覆盖两个唯一键和 rowid。
CREATE TRIGGER trg_cache_affinities_prd050_conflict_insert
BEFORE INSERT ON cache_affinities
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinities current
    WHERE current.cache_affinity_id=NEW.cache_affinity_id
       OR current.rowid=NEW.rowid
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinities current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.model_alias_id=NEW.model_alias_id
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_replace_forbidden');
END;

-- UPDATE OR REPLACE 也可能用 rowid 或业务唯一键删除另一行，冲突目标必须排除 OLD 自身。
CREATE TRIGGER trg_cache_affinities_prd050_conflict_update
BEFORE UPDATE ON cache_affinities
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM cache_affinities current
  WHERE current.rowid!=OLD.rowid
    AND (
      current.rowid=NEW.rowid
      OR current.cache_affinity_id=NEW.cache_affinity_id
      OR (
        current.tenant_id=NEW.tenant_id
        AND current.model_alias_id=NEW.model_alias_id
      )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_replace_forbidden');
END;

-- 配置身份和创建者不可变；真实变化必须严格升一版，no-op 只写事件而不更新配置行。
CREATE TRIGGER trg_cache_affinities_prd050_update
BEFORE UPDATE ON cache_affinities
FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.cache_affinity_id!=OLD.cache_affinity_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.model_alias_id!=OLD.model_alias_id
  OR NEW.created_by!=OLD.created_by
  OR NEW.created_at!=OLD.created_at
  OR NEW.version!=OLD.version+1
  OR NEW.updated_at<OLD.updated_at
  OR (
    NEW.status=OLD.status
    AND NEW.data_class IS OLD.data_class
    AND NEW.minimum_prefix_chars=OLD.minimum_prefix_chars
    AND NEW.ttl_seconds=OLD.ttl_seconds
  )
  OR NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.updated_by
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_update_invalid');
END;

CREATE TRIGGER trg_cache_affinities_prd050_delete
BEFORE DELETE ON cache_affinities
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_delete_forbidden');
END;

-- Scope 插入时同时执行租户和 tenant+Virtual Key 两级容量门禁，清理必须先于创建。
CREATE TRIGGER trg_cache_affinity_scopes_prd050_insert
BEFORE INSERT ON cache_affinity_scopes
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM virtual_keys key
    WHERE key.tenant_id=NEW.tenant_id AND key.virtual_key_id=NEW.virtual_key_id
  )
  OR (
    SELECT COUNT(*) FROM cache_affinity_scopes scope
    WHERE scope.tenant_id=NEW.tenant_id
  )>=100000
  OR (
    SELECT COUNT(*) FROM cache_affinity_scopes scope
    WHERE scope.tenant_id=NEW.tenant_id
      AND scope.virtual_key_id=NEW.virtual_key_id
  )>=10000
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_scope_capacity_exceeded');
END;

CREATE TRIGGER trg_cache_affinity_scopes_prd050_conflict_insert
BEFORE INSERT ON cache_affinity_scopes
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinity_scopes current
    WHERE current.cache_affinity_scope_id=NEW.cache_affinity_scope_id
       OR current.rowid=NEW.rowid
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_scopes current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.virtual_key_id=NEW.virtual_key_id
      AND current.digest_schema_version=NEW.digest_schema_version
      AND current.affinity_scope_digest=NEW.affinity_scope_digest
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_scope_replace_forbidden');
END;

CREATE TRIGGER trg_cache_affinity_scopes_prd050_conflict_update
BEFORE UPDATE ON cache_affinity_scopes
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM cache_affinity_scopes current
  WHERE current.rowid!=OLD.rowid
    AND (
      current.rowid=NEW.rowid
      OR current.cache_affinity_scope_id=NEW.cache_affinity_scope_id
      OR (
        current.tenant_id=NEW.tenant_id
        AND current.virtual_key_id=NEW.virtual_key_id
        AND current.digest_schema_version=NEW.digest_schema_version
        AND current.affinity_scope_digest=NEW.affinity_scope_digest
      )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_scope_replace_forbidden');
END;

CREATE TRIGGER trg_cache_affinity_scopes_prd050_identity_update
BEFORE UPDATE ON cache_affinity_scopes
FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.cache_affinity_scope_id!=OLD.cache_affinity_scope_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.virtual_key_id!=OLD.virtual_key_id
  OR NEW.digest_schema_version!=OLD.digest_schema_version
  OR NEW.affinity_scope_digest!=OLD.affinity_scope_digest
  OR NEW.created_at!=OLD.created_at
  OR NEW.last_selected_at<OLD.last_selected_at
  OR NEW.updated_at<OLD.updated_at
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_scope_identity_immutable');
END;

-- Scope 与 Binding 存在父子顺序，初次 INSERT 不可能合法携带指针；只能在 Binding 创建后 UPDATE。
CREATE TRIGGER trg_cache_affinity_scopes_prd050_pointer_insert
BEFORE INSERT ON cache_affinity_scopes
FOR EACH ROW
WHEN NEW.preferred_binding_id IS NOT NULL
  OR NEW.preferred_lease_generation IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_scope_pointer_invalid');
END;

-- 指针写入必须在同一事务内指向同租户、同 Scope、同 lease generation 的现存 Binding。
CREATE TRIGGER trg_cache_affinity_scopes_prd050_pointer_update
BEFORE UPDATE OF preferred_binding_id, preferred_lease_generation
ON cache_affinity_scopes
FOR EACH ROW
WHEN NEW.preferred_binding_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM cache_affinity_bindings binding
  WHERE binding.tenant_id=NEW.tenant_id
    AND binding.cache_affinity_scope_id=NEW.cache_affinity_scope_id
    AND binding.cache_affinity_binding_id=NEW.preferred_binding_id
    AND binding.pending_lease_generation=NEW.preferred_lease_generation
)
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_scope_pointer_invalid');
END;

CREATE TRIGGER trg_cache_affinity_bindings_prd050_conflict_insert
BEFORE INSERT ON cache_affinity_bindings
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinity_bindings current
    WHERE current.cache_affinity_binding_id=NEW.cache_affinity_binding_id
       OR current.rowid=NEW.rowid
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_bindings current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.cache_affinity_scope_id=NEW.cache_affinity_scope_id
      AND current.model_endpoint_id=NEW.model_endpoint_id
      AND current.endpoint_revision_digest=NEW.endpoint_revision_digest
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_bindings current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.affinity_key_digest=NEW.affinity_key_digest
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_binding_replace_forbidden');
END;

-- Binding 图必须把 Scope、Model、Provider 和可选 Credential 同时锁在当前租户内。
CREATE TRIGGER trg_cache_affinity_bindings_prd050_graph_insert
BEFORE INSERT ON cache_affinity_bindings
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM cache_affinity_scopes scope
    WHERE scope.tenant_id=NEW.tenant_id
      AND scope.cache_affinity_scope_id=NEW.cache_affinity_scope_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM models model
    WHERE model.tenant_id=NEW.tenant_id
      AND model.model_id=NEW.model_endpoint_id
      AND model.provider_id=NEW.provider_id
  )
  OR (
    NEW.target_kind='managed' AND NOT EXISTS (
      SELECT 1 FROM provider_credentials credential
      WHERE credential.tenant_id=NEW.tenant_id
        AND credential.provider_id=NEW.provider_id
        AND credential.provider_credential_id=NEW.provider_credential_id
    )
  )
  OR (
    SELECT COUNT(*) FROM cache_affinity_bindings binding
    WHERE binding.tenant_id=NEW.tenant_id
      AND binding.cache_affinity_scope_id=NEW.cache_affinity_scope_id
  )>=8
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_binding_graph_invalid');
END;

CREATE TRIGGER trg_cache_affinity_bindings_prd050_conflict_update
BEFORE UPDATE ON cache_affinity_bindings
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM cache_affinity_bindings current
  WHERE current.rowid!=OLD.rowid
    AND (
      current.rowid=NEW.rowid
      OR current.cache_affinity_binding_id=NEW.cache_affinity_binding_id
      OR (
        current.tenant_id=NEW.tenant_id
        AND current.cache_affinity_scope_id=NEW.cache_affinity_scope_id
        AND current.model_endpoint_id=NEW.model_endpoint_id
        AND current.endpoint_revision_digest=NEW.endpoint_revision_digest
      )
      OR (
        current.tenant_id=NEW.tenant_id
        AND current.affinity_key_digest=NEW.affinity_key_digest
      )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_binding_replace_forbidden');
END;

CREATE TRIGGER trg_cache_affinity_bindings_prd050_identity_update
BEFORE UPDATE ON cache_affinity_bindings
FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.cache_affinity_binding_id!=OLD.cache_affinity_binding_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.cache_affinity_scope_id!=OLD.cache_affinity_scope_id
  OR NEW.affinity_key_digest!=OLD.affinity_key_digest
  OR NEW.model_endpoint_id!=OLD.model_endpoint_id
  OR NEW.endpoint_revision_digest!=OLD.endpoint_revision_digest
  OR NEW.provider_id!=OLD.provider_id
  OR NEW.created_at!=OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_binding_identity_immutable');
END;

-- target ABA 与 lease ABA 使用两个独立 generation；跨代时旧活动计数必须原子归零。
CREATE TRIGGER trg_cache_affinity_bindings_prd050_transition_update
BEFORE UPDATE ON cache_affinity_bindings
FOR EACH ROW
WHEN (
    NEW.target_kind IS OLD.target_kind
    AND NEW.provider_credential_id IS OLD.provider_credential_id
    AND NEW.target_generation!=OLD.target_generation
  )
  OR (
    NOT (
      NEW.target_kind IS OLD.target_kind
      AND NEW.provider_credential_id IS OLD.provider_credential_id
    )
    AND (
      NEW.target_generation!=OLD.target_generation+1
      OR NEW.last_success_at IS NOT NULL
      OR NEW.pending_lease_generation!=OLD.pending_lease_generation+1
      OR NEW.active_selection_count!=0
    )
  )
  OR NEW.pending_lease_generation NOT IN (
    OLD.pending_lease_generation, OLD.pending_lease_generation+1
  )
  OR (
    NEW.pending_lease_generation=OLD.pending_lease_generation+1
    AND (
      NEW.active_selection_count!=0
      OR NEW.pending_lease_started_at<OLD.pending_lease_started_at
    )
  )
  OR NEW.selection_count<OLD.selection_count
  OR NEW.estimated_hit_count<OLD.estimated_hit_count
  OR (
    NEW.target_generation=OLD.target_generation
    AND OLD.last_success_at IS NOT NULL
    AND (NEW.last_success_at IS NULL OR NEW.last_success_at<OLD.last_success_at)
  )
  OR NEW.last_selected_at<OLD.last_selected_at
  OR NEW.updated_at<OLD.updated_at
  OR (
    NEW.target_kind='managed'
    AND (
      OLD.target_kind!='managed'
      OR NEW.provider_credential_id IS NOT OLD.provider_credential_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM provider_credentials credential
      WHERE credential.tenant_id=NEW.tenant_id
        AND credential.provider_id=NEW.provider_id
        AND credential.provider_credential_id=NEW.provider_credential_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_binding_transition_invalid');
END;

CREATE TRIGGER trg_cache_affinity_selections_prd050_conflict_insert
BEFORE INSERT ON cache_affinity_selections
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinity_selections current
    WHERE current.cache_affinity_selection_id=NEW.cache_affinity_selection_id
       OR current.rowid=NEW.rowid
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_selections current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.request_id=NEW.request_id
      AND current.attempt_sequence=NEW.attempt_sequence
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_selection_immutable');
END;

-- Selection 插入必须与认证 Claim、配置、Binding 和已经提交的 execution gate 构成同一租户图。
CREATE TRIGGER trg_cache_affinity_selections_prd050_graph_insert
BEFORE INSERT ON cache_affinity_selections
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM gateway_request_claims claim
    JOIN cache_affinity_scopes scope
      ON scope.tenant_id=claim.tenant_id
     AND scope.virtual_key_id=claim.virtual_key_id
     AND scope.cache_affinity_scope_id=NEW.cache_affinity_scope_id_snapshot
    WHERE claim.tenant_id=NEW.tenant_id
      AND claim.request_id=NEW.request_id
      AND claim.trace_id=NEW.trace_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM cache_affinities affinity
    WHERE affinity.tenant_id=NEW.tenant_id
      AND affinity.cache_affinity_id=NEW.cache_affinity_id
      AND affinity.model_alias_id=NEW.model_alias_id
      AND affinity.version=NEW.cache_affinity_version
      AND affinity.status='enabled'
  )
  OR NOT EXISTS (
    SELECT 1
    FROM cache_affinity_bindings binding
    JOIN providers provider
      ON provider.tenant_id=binding.tenant_id
     AND provider.provider_id=binding.provider_id
    WHERE binding.tenant_id=NEW.tenant_id
      AND binding.cache_affinity_scope_id=NEW.cache_affinity_scope_id_snapshot
      AND binding.cache_affinity_binding_id=NEW.cache_affinity_binding_id_snapshot
      AND binding.model_endpoint_id=NEW.model_endpoint_id
      AND binding.endpoint_revision_digest=NEW.endpoint_revision_digest_snapshot
      AND binding.provider_id=NEW.provider_id
      AND provider.provider_code=NEW.provider_code_snapshot
      AND binding.provider_credential_id IS NEW.provider_credential_id_snapshot
      AND binding.target_kind=NEW.target_kind
      AND binding.target_generation=NEW.target_generation
      AND binding.pending_lease_generation=NEW.pending_lease_generation_snapshot
      AND binding.last_success_at IS NEW.warm_evidence_at_snapshot
  )
  OR (
    NEW.routing_attempt_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM routing_attempts attempt
      WHERE attempt.tenant_id=NEW.tenant_id
        AND attempt.routing_attempt_id=NEW.routing_attempt_id
        AND attempt.request_id=NEW.request_id
        AND attempt.trace_id=NEW.trace_id
        AND attempt.attempt_sequence=NEW.attempt_sequence
        AND attempt.model_alias_id=NEW.model_alias_id
        AND attempt.model_id=NEW.model_endpoint_id
        AND attempt.provider_id=NEW.provider_id
        AND attempt.provider_code=NEW.provider_code_snapshot
        AND attempt.status='started'
    )
  )
  OR (
    NEW.stream_session_id IS NOT NULL AND (
      NEW.attempt_sequence!=1 OR NOT EXISTS (
        SELECT 1 FROM stream_sessions session
        WHERE session.tenant_id=NEW.tenant_id
          AND session.stream_session_id=NEW.stream_session_id
          AND session.request_id=NEW.request_id
          AND session.trace_id=NEW.trace_id
          AND session.model_id=NEW.model_endpoint_id
          AND session.provider_id=NEW.provider_id
          AND session.provider_code=NEW.provider_code_snapshot
          AND session.status='starting'
      )
    )
  )
  OR (
    NEW.estimated_hit=1 AND NOT EXISTS (
      SELECT 1 FROM cache_affinity_bindings binding
      WHERE binding.tenant_id=NEW.tenant_id
        AND binding.cache_affinity_binding_id=NEW.cache_affinity_binding_id_snapshot
        AND binding.target_generation=NEW.target_generation
        AND binding.last_success_at IS NOT NULL
        AND datetime(NEW.created_at)<datetime(
          binding.last_success_at, '+' || NEW.ttl_seconds_snapshot || ' seconds'
        )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_selection_graph_invalid');
END;

-- outcome 之外的全部快照不可变；pending 只能依据已提交 execution terminal 单向进入一个终态。
CREATE TRIGGER trg_cache_affinity_selections_prd050_update
BEFORE UPDATE ON cache_affinity_selections
FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.cache_affinity_selection_id!=OLD.cache_affinity_selection_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.request_id!=OLD.request_id
  OR NEW.trace_id!=OLD.trace_id
  OR NEW.attempt_sequence!=OLD.attempt_sequence
  OR NEW.routing_attempt_id IS NOT OLD.routing_attempt_id
  OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.cache_affinity_id!=OLD.cache_affinity_id
  OR NEW.cache_affinity_version!=OLD.cache_affinity_version
  OR NEW.cache_affinity_scope_id_snapshot!=OLD.cache_affinity_scope_id_snapshot
  OR NEW.cache_affinity_binding_id_snapshot!=OLD.cache_affinity_binding_id_snapshot
  OR NEW.model_alias_id!=OLD.model_alias_id
  OR NEW.model_endpoint_id!=OLD.model_endpoint_id
  OR NEW.endpoint_revision_digest_snapshot!=OLD.endpoint_revision_digest_snapshot
  OR NEW.provider_id!=OLD.provider_id
  OR NEW.provider_code_snapshot!=OLD.provider_code_snapshot
  OR NEW.provider_credential_id_snapshot IS NOT OLD.provider_credential_id_snapshot
  OR NEW.target_kind!=OLD.target_kind
  OR NEW.target_generation!=OLD.target_generation
  OR NEW.pending_lease_generation_snapshot!=OLD.pending_lease_generation_snapshot
  OR NEW.ttl_seconds_snapshot!=OLD.ttl_seconds_snapshot
  OR NEW.warm_evidence_at_snapshot IS NOT OLD.warm_evidence_at_snapshot
  OR NEW.endpoint_preference_applied!=OLD.endpoint_preference_applied
  OR NEW.endpoint_preference_state!=OLD.endpoint_preference_state
  OR NEW.binding_state_before!=OLD.binding_state_before
  OR NEW.selection_reason!=OLD.selection_reason
  OR NEW.estimated_hit!=OLD.estimated_hit
  OR NEW.created_at!=OLD.created_at
  OR NOT (
    (OLD.outcome_status='pending' AND NEW.outcome_status IN ('succeeded', 'failed'))
    OR (
      OLD.outcome_status IN ('succeeded', 'failed')
      AND NEW.outcome_status=OLD.outcome_status
      AND NEW.outcome_at=OLD.outcome_at
    )
  )
  OR (
    OLD.outcome_status='pending'
    AND NEW.outcome_status='succeeded'
    AND NOT (
      EXISTS (
        SELECT 1 FROM routing_attempts attempt
        WHERE attempt.tenant_id=NEW.tenant_id
          AND attempt.routing_attempt_id=NEW.routing_attempt_id
          AND attempt.status='succeeded'
          AND attempt.completed_at=NEW.outcome_at
      )
      OR EXISTS (
        SELECT 1 FROM stream_sessions session
        WHERE session.tenant_id=NEW.tenant_id
          AND session.stream_session_id=NEW.stream_session_id
          AND session.status='completed'
          AND session.completed_at=NEW.outcome_at
      )
    )
  )
  OR (
    OLD.outcome_status='pending'
    AND NEW.outcome_status='failed'
    AND NOT (
      EXISTS (
        SELECT 1 FROM routing_attempts attempt
        WHERE attempt.tenant_id=NEW.tenant_id
          AND attempt.routing_attempt_id=NEW.routing_attempt_id
          AND attempt.status='failed'
          AND attempt.completed_at=NEW.outcome_at
      )
      OR EXISTS (
        SELECT 1 FROM stream_sessions session
        WHERE session.tenant_id=NEW.tenant_id
          AND session.stream_session_id=NEW.stream_session_id
          AND session.status IN ('timed_out', 'client_disconnected', 'upstream_failed')
          AND session.completed_at=NEW.outcome_at
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_selection_transition_invalid');
END;

CREATE TRIGGER trg_cache_affinity_selections_prd050_delete
BEFORE DELETE ON cache_affinity_selections
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_selection_immutable');
END;

CREATE TRIGGER trg_cache_affinity_config_events_prd050_conflict_insert
BEFORE INSERT ON cache_affinity_config_events
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinity_config_events current
    WHERE current.cache_affinity_config_event_id=NEW.cache_affinity_config_event_id
       OR current.rowid=NEW.rowid
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_config_events current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id
      AND current.operation_request_id=NEW.operation_request_id
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_config_event_immutable');
END;

-- Event 的 after 必须是当前配置的精确安全快照，JSON 不允许夹带 Prompt、Key 或 Endpoint 字段。
CREATE TRIGGER trg_cache_affinity_config_events_prd050_graph_insert
BEFORE INSERT ON cache_affinity_config_events
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM cache_affinities affinity
    WHERE affinity.tenant_id=NEW.tenant_id
      AND affinity.cache_affinity_id=NEW.cache_affinity_id
      AND affinity.model_alias_id=NEW.model_alias_id
      AND affinity.version=NEW.cache_affinity_version
      AND json_remove(
        NEW.after_json, '$.status', '$.data_class', '$.minimum_prefix_chars',
        '$.ttl_seconds', '$.version'
      )='{}'
      AND COALESCE(json_type(NEW.after_json, '$.status'), 'missing')='text'
      AND COALESCE(json_type(NEW.after_json, '$.data_class'), 'missing') IN ('text', 'null')
      AND COALESCE(json_type(NEW.after_json, '$.minimum_prefix_chars'), 'missing')='integer'
      AND COALESCE(json_type(NEW.after_json, '$.ttl_seconds'), 'missing')='integer'
      AND COALESCE(json_type(NEW.after_json, '$.version'), 'missing')='integer'
      AND json_extract(NEW.after_json, '$.status')=affinity.status
      AND json_extract(NEW.after_json, '$.data_class') IS affinity.data_class
      AND json_extract(NEW.after_json, '$.minimum_prefix_chars')=affinity.minimum_prefix_chars
      AND json_extract(NEW.after_json, '$.ttl_seconds')=affinity.ttl_seconds
      AND json_extract(NEW.after_json, '$.version')=affinity.version
  )
  OR (
    NEW.event_type IN ('updated', 'noop') AND (
      json_remove(
        NEW.before_json, '$.status', '$.data_class', '$.minimum_prefix_chars',
        '$.ttl_seconds', '$.version'
      )!='{}'
      OR COALESCE(json_type(NEW.before_json, '$.status'), 'missing')!='text'
      OR COALESCE(json_type(NEW.before_json, '$.minimum_prefix_chars'), 'missing')!='integer'
      OR COALESCE(json_type(NEW.before_json, '$.ttl_seconds'), 'missing')!='integer'
      OR COALESCE(json_type(NEW.before_json, '$.version'), 'missing')!='integer'
      OR COALESCE(json_type(NEW.before_json, '$.data_class'), 'missing') NOT IN ('text', 'null')
      OR (
        NEW.event_type='updated'
        AND json_extract(NEW.before_json, '$.version')!=NEW.cache_affinity_version-1
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_config_event_graph_invalid');
END;

CREATE TRIGGER trg_cache_affinity_config_events_prd050_immutable_update
BEFORE UPDATE ON cache_affinity_config_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_config_event_immutable');
END;
CREATE TRIGGER trg_cache_affinity_config_events_prd050_immutable_delete
BEFORE DELETE ON cache_affinity_config_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_config_event_immutable');
END;

-- 淘汰顺序是先清 Scope 指针，再删 Binding；直接 SQL 不得制造悬空指针或删活动事实。
CREATE TRIGGER trg_cache_affinity_bindings_prd050_delete
BEFORE DELETE ON cache_affinity_bindings
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinity_scopes scope
    WHERE scope.tenant_id=OLD.tenant_id
      AND scope.cache_affinity_scope_id=OLD.cache_affinity_scope_id
      AND scope.preferred_binding_id=OLD.cache_affinity_binding_id
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_selections selection
    WHERE selection.tenant_id=OLD.tenant_id
      AND selection.cache_affinity_binding_id_snapshot=OLD.cache_affinity_binding_id
      AND selection.outcome_status='pending'
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_binding_delete_unsafe');
END;

CREATE TRIGGER trg_cache_affinity_scopes_prd050_delete
BEFORE DELETE ON cache_affinity_scopes
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinity_bindings binding
    WHERE binding.tenant_id=OLD.tenant_id
      AND binding.cache_affinity_scope_id=OLD.cache_affinity_scope_id
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_selections selection
    WHERE selection.tenant_id=OLD.tenant_id
      AND selection.cache_affinity_scope_id_snapshot=OLD.cache_affinity_scope_id
      AND selection.outcome_status='pending'
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_scope_delete_unsafe');
END;
-- Audit 只扩展 CacheAffinity 配置动作；旧行 17 列必须逐值、逐 NULL 保真。
DROP TABLE IF EXISTS audit_logs_prd050_new;
CREATE TABLE audit_logs_prd050_new (
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
      'model_quality_score.updated',
      'cache_affinity.created', 'cache_affinity.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score', 'cache_affinity'
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

-- 全列复制旧审计事实，仅扩展 CacheAffinity 枚举，不改写原始审计内容。
INSERT INTO audit_logs_prd050_new(
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
-- 在切表前比较审计源表与目标表，阻断任何字段或 NULL 语义丢失。
INSERT INTO prd050_audit_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd050_new),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd050_new WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd050_new WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd050_new WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs
    EXCEPT SELECT rowid, * FROM audit_logs_prd050_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs_prd050_new
    EXCEPT SELECT rowid, * FROM audit_logs
  ));
DROP TRIGGER trg_prd050_audit_copy_guard;
DROP TABLE prd050_audit_copy_guard;

DROP TABLE audit_logs;
-- 仅在审计复制守卫通过后切换表名，确保旧历史始终可追溯。
ALTER TABLE audit_logs_prd050_new RENAME TO audit_logs;

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

-- Audit 的 INSERT OR REPLACE 会绕过 DELETE trigger；除 PK/rowid 外，还必须覆盖两条
-- 部分唯一键，否则更换 audit_log_id 仍可按 tenant/actor/request 隐式删除旧审计。
-- SQLite 在 BEFORE INSERT 阶段用 -1 表示尚未分配的隐式 rowid，因此该哨兵不能参与冲突比较。
CREATE TRIGGER trg_audit_logs_prd050_conflict_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_logs current
  WHERE current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.action IN ('policy.dry_run_executed', 'model_quality_score.updated')
      AND current.action=NEW.action
      AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id
      AND current.request_id=NEW.request_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

-- AFTER 阶段隐式 rowid 已获正式正值，仍为负数只能来自显式写入；ABORT 会连同 REPLACE
-- 的隐式删除一起回滚。历史负 rowid 在切表复制时保真，不会经过本触发器而被静默改写。
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

-- 最终守卫核对正式表、索引、关键 trigger、扩展枚举与数据库完整性，失败时回滚整次 0040。
CREATE TEMP TABLE prd050_up_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd050_up_integrity_guard
BEFORE INSERT ON prd050_up_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd050_up_integrity_failed');
END;
-- 提交迁移前核对表、索引和触发器全集，避免半成品结构被登记为已应用。
INSERT INTO prd050_up_integrity_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'cache_affinities', 'cache_affinity_scopes', 'cache_affinity_bindings',
      'cache_affinity_selections', 'cache_affinity_config_events',
      'routing_candidates', 'audit_logs'
    )
  )=7 THEN 0 ELSE 1 END
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
      'trg_routing_candidates_prd050_conflict_insert',
      'trg_cache_affinities_prd050_owner_insert',
      'trg_cache_affinities_prd050_conflict_insert',
      'trg_cache_affinity_scopes_prd050_pointer_insert',
      'trg_cache_affinity_scopes_prd050_pointer_update',
      'trg_cache_affinity_bindings_prd050_graph_insert',
      'trg_cache_affinity_bindings_prd050_transition_update',
      'trg_cache_affinity_selections_prd050_graph_insert',
      'trg_cache_affinity_selections_prd050_update',
      'trg_cache_affinity_config_events_prd050_graph_insert',
      'trg_cache_affinity_bindings_prd050_delete',
      'trg_cache_affinity_scopes_prd050_delete',
      'trg_audit_logs_owner_insert',
      'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete',
      'trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert'
    )
  )=20 THEN 0 ELSE 1 END
  + CASE WHEN EXISTS (
    SELECT 1 FROM sqlite_master
    WHERE type='table' AND name='routing_candidates'
      AND sql LIKE '%routing.cache_affinity_pending_preferred%'
      AND sql LIKE '%routing.cache_affinity_warm_preferred%'
  ) THEN 0 ELSE 1 END
  + CASE WHEN EXISTS (
    SELECT 1 FROM sqlite_master
    WHERE type='table' AND name='audit_logs'
      AND sql LIKE '%cache_affinity.created%'
      AND sql LIKE '%cache_affinity.updated%'
      AND sql LIKE '%cache_affinity%'
  ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER trg_prd050_up_integrity_guard;
DROP TABLE prd050_up_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
