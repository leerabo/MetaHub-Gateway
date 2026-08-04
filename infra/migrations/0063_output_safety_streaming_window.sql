-- PRD-071：输出安全全文/流式滑窗检测、账单争议事实与流终态兼容迁移。
-- 所有安全事实都以 tenant/request 复合 owner 约束；只保存枚举、计数、规则 ID 和版本，
-- 不保存 Prompt、Response、命中文本、规则 pattern、Provider Key 或异常原文。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前证明数据库精确位于 0062，避免把列替换应用到未知后续 schema。
DROP TABLE IF EXISTS temp.prd071_up_guard;
CREATE TEMP TABLE prd071_up_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd071_up_guard_ok CHECK (issue_count=0)
);
-- 把版本、目录、表形状和完整性问题聚合为一次原子前置校验。
INSERT INTO prd071_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0062' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0062')
  -- 本 PRD 复用 revision 7，迁移前后都不得隐式增删权限或扩展默认角色。
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=59
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=7)=59
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=155
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*59 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=7)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('dlp_rules'))=14
      AND (SELECT COUNT(*) FROM pragma_table_info('stream_sessions'))=25
      AND (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=66
      AND (SELECT COUNT(*) FROM pragma_table_info('usage_ledger'))=32
      AND (SELECT COUNT(*) FROM pragma_table_info('finops_ledger_entries'))=38
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND tbl_name='stream_sessions' AND name NOT LIKE 'sqlite_autoindex%')=5
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='stream_sessions')=6
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND tbl_name='request_logs' AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='request_logs')=14
    THEN 0 ELSE 1 END
  -- stream_status 只允许被 0062 的表和五个已知 Feedback/Stream trigger 引用。
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL AND instr(lower(object.sql),'stream_status')>0
       AND object.name NOT IN (
         'request_logs','trg_request_feedback_graph_insert_prd049',
         'trg_request_log_feedback_parent_conflict_insert_prd049',
         'trg_request_log_feedback_parent_update_prd049',
         'trg_request_log_stream_insert','trg_request_log_stream_update'
       ))
  + (SELECT COUNT(*) FROM sqlite_schema object WHERE object.name IN (
      'output_safety_scans','output_safety_events','finops_ledger_disputes',
      'stream_sessions_prd071_old','idx_dlp_rules_tenant_scope_status_priority_prd071',
      'idx_dlp_rules_tenant_owner_prd071','idx_stream_sessions_request_owner_prd071',
      'idx_usage_ledger_request_owner_prd071','idx_finops_ledger_request_owner_prd071'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd071_up_guard;

-- 历史 DLP 规则默认只保护输入，升级不得把现有 pattern 静默用于模型输出。
ALTER TABLE dlp_rules ADD COLUMN scan_scope TEXT NOT NULL DEFAULT 'input'
  CHECK (scan_scope IN ('input','output','both'));
CREATE INDEX idx_dlp_rules_tenant_scope_status_priority_prd071
  ON dlp_rules(tenant_id,status,scan_scope,priority);
-- 复合候选键让子事实通过 tenant-qualified FK 绑定规则，而不是只信任全局字符串 ID。
CREATE UNIQUE INDEX idx_dlp_rules_tenant_owner_prd071
  ON dlp_rules(tenant_id,dlp_rule_id);

-- StreamSession 采用保真换表扩展两个本地安全终态；显式复制 rowid，避免改变历史稳定顺序。
ALTER TABLE stream_sessions RENAME TO stream_sessions_prd071_old;
CREATE TABLE stream_sessions (
  stream_session_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  environment_id TEXT,
  environment_code TEXT,
  project_id TEXT,
  application_id TEXT,
  model_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_code TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN (
      'starting','active','completed','timed_out','client_disconnected','upstream_failed',
      'output_blocked','output_safety_unavailable'
    )
  ),
  inter_token_timeout_ms INTEGER NOT NULL CHECK (inter_token_timeout_ms > 0),
  chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
  bytes_sent INTEGER NOT NULL DEFAULT 0 CHECK (bytes_sent >= 0),
  first_chunk_at TEXT,
  last_chunk_at TEXT,
  error_code TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  upstream_http_status INTEGER CHECK (
    upstream_http_status IS NULL OR upstream_http_status BETWEEN 100 AND 599
  ),
  retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0,1)),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (project_id) REFERENCES projects(project_id),
  FOREIGN KEY (application_id) REFERENCES applications(application_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);
-- 显式复制全部 25 列和 rowid，确保历史会话身份与内部稳定顺序不漂移。
INSERT INTO stream_sessions(
  rowid,stream_session_id,tenant_id,request_id,trace_id,virtual_key_id,
  environment_id,environment_code,project_id,application_id,model_id,provider_id,
  provider_code,status,inter_token_timeout_ms,chunk_count,bytes_sent,first_chunk_at,
  last_chunk_at,error_code,started_at,completed_at,created_at,updated_at,
  upstream_http_status,retryable
)
SELECT
  rowid,stream_session_id,tenant_id,request_id,trace_id,virtual_key_id,
  environment_id,environment_code,project_id,application_id,model_id,provider_id,
  provider_code,status,inter_token_timeout_ms,chunk_count,bytes_sent,first_chunk_at,
  last_chunk_at,error_code,started_at,completed_at,created_at,updated_at,
  upstream_http_status,retryable
FROM stream_sessions_prd071_old;
DROP TABLE stream_sessions_prd071_old;

-- 恢复 0062 的全部 StreamSession 索引，并把 Provider 成功终态索引纳入两个本地安全终态。
CREATE UNIQUE INDEX idx_stream_sessions_tenant_request
  ON stream_sessions(tenant_id,request_id);
CREATE INDEX idx_stream_sessions_tenant_status_time
  ON stream_sessions(tenant_id,status,started_at DESC);
CREATE INDEX idx_stream_sessions_tenant_model_time
  ON stream_sessions(tenant_id,model_id,started_at DESC);
CREATE UNIQUE INDEX idx_stream_sessions_tenant_owner_prd056
  ON stream_sessions(tenant_id,stream_session_id);
CREATE INDEX idx_stream_sessions_tenant_provider_completed
  ON stream_sessions(tenant_id,provider_id,completed_at DESC,stream_session_id DESC)
  WHERE status IN (
    'completed','timed_out','client_disconnected','upstream_failed',
    'output_blocked','output_safety_unavailable'
  ) AND completed_at IS NOT NULL;
CREATE UNIQUE INDEX idx_stream_sessions_request_owner_prd071
  ON stream_sessions(tenant_id,request_id,stream_session_id);

-- 会话写入仍逐项复核租户图，换表不能削弱 0027 的跨租户防线。
CREATE TRIGGER trg_stream_session_tenant_insert
BEFORE INSERT ON stream_sessions FOR EACH ROW
BEGIN
  SELECT CASE WHEN NEW.virtual_key_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM virtual_keys owner
    WHERE owner.virtual_key_id=NEW.virtual_key_id AND owner.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT,'stream_session_virtual_key_tenant_mismatch') END;
  SELECT CASE WHEN NEW.environment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environments owner
    WHERE owner.environment_id=NEW.environment_id AND owner.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT,'stream_session_environment_tenant_mismatch') END;
  SELECT CASE WHEN NEW.environment_id IS NOT NULL AND NEW.environment_code IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM environments owner
      WHERE owner.environment_id=NEW.environment_id AND owner.tenant_id=NEW.tenant_id
        AND owner.code=NEW.environment_code
    ) THEN RAISE(ABORT,'stream_session_environment_snapshot_mismatch') END;
  SELECT CASE WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM projects owner
    WHERE owner.project_id=NEW.project_id AND owner.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT,'stream_session_project_tenant_mismatch') END;
  SELECT CASE WHEN NEW.application_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM applications owner
    WHERE owner.application_id=NEW.application_id AND owner.tenant_id=NEW.tenant_id
      AND (NEW.project_id IS NULL OR owner.project_id=NEW.project_id)
  ) THEN RAISE(ABORT,'stream_session_application_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM models owner
    WHERE owner.model_id=NEW.model_id AND owner.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT,'stream_session_model_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM providers owner
    WHERE owner.provider_id=NEW.provider_id AND owner.tenant_id=NEW.tenant_id
      AND owner.provider_code=NEW.provider_code
  ) THEN RAISE(ABORT,'stream_session_provider_tenant_mismatch') END;
END;

CREATE TRIGGER trg_stream_session_tenant_update
BEFORE UPDATE OF tenant_id,virtual_key_id,environment_id,environment_code,
  project_id,application_id,model_id,provider_id,provider_code
ON stream_sessions FOR EACH ROW
BEGIN
  SELECT CASE WHEN NEW.tenant_id<>OLD.tenant_id
    THEN RAISE(ABORT,'stream_session_tenant_immutable') END;
  SELECT CASE WHEN NEW.virtual_key_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM virtual_keys owner
    WHERE owner.virtual_key_id=NEW.virtual_key_id AND owner.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT,'stream_session_virtual_key_tenant_mismatch') END;
  SELECT CASE WHEN NEW.environment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environments owner
    WHERE owner.environment_id=NEW.environment_id AND owner.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT,'stream_session_environment_tenant_mismatch') END;
  SELECT CASE WHEN NEW.environment_id IS NOT NULL AND NEW.environment_code IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM environments owner
      WHERE owner.environment_id=NEW.environment_id AND owner.tenant_id=NEW.tenant_id
        AND owner.code=NEW.environment_code
    ) THEN RAISE(ABORT,'stream_session_environment_snapshot_mismatch') END;
  SELECT CASE WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM projects owner
    WHERE owner.project_id=NEW.project_id AND owner.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT,'stream_session_project_tenant_mismatch') END;
  SELECT CASE WHEN NEW.application_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM applications owner
    WHERE owner.application_id=NEW.application_id AND owner.tenant_id=NEW.tenant_id
      AND (NEW.project_id IS NULL OR owner.project_id=NEW.project_id)
  ) THEN RAISE(ABORT,'stream_session_application_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM models owner
    WHERE owner.model_id=NEW.model_id AND owner.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT,'stream_session_model_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM providers owner
    WHERE owner.provider_id=NEW.provider_id AND owner.tenant_id=NEW.tenant_id
      AND owner.provider_code=NEW.provider_code
  ) THEN RAISE(ABORT,'stream_session_provider_tenant_mismatch') END;
END;

-- 两个新增终态只能从 active 进入；starting 仍只能握手成功或按上游失败收口。
CREATE TRIGGER trg_stream_session_status_transition
BEFORE UPDATE OF status ON stream_sessions FOR EACH ROW
WHEN NOT (
  NEW.status=OLD.status
  OR (OLD.status='starting' AND NEW.status IN ('active','upstream_failed'))
  OR (OLD.status='active' AND NEW.status IN (
    'completed','timed_out','client_disconnected','upstream_failed',
    'output_blocked','output_safety_unavailable'
  ))
)
BEGIN SELECT RAISE(ABORT,'stream_session_status_transition_invalid'); END;

-- 新安全终态使用固定公开错误码且不可标记 retryable，避免伪造成 Provider 质量故障。
CREATE TRIGGER trg_stream_session_output_safety_insert_prd071
BEFORE INSERT ON stream_sessions FOR EACH ROW
WHEN NEW.status IN ('output_blocked','output_safety_unavailable')
BEGIN SELECT RAISE(ABORT,'stream_session_output_safety_transition_invalid'); END;
CREATE TRIGGER trg_stream_session_output_safety_update_prd071
BEFORE UPDATE OF status,error_code,retryable ON stream_sessions FOR EACH ROW
WHEN (NEW.status='output_blocked'
      AND (NEW.error_code IS NOT 'content_policy_violation' OR NEW.retryable!=0
        OR NEW.completed_at IS NULL))
  OR (NEW.status='output_safety_unavailable'
      AND (NEW.error_code IS NOT 'gateway.output_safety_unavailable' OR NEW.retryable!=0
        OR NEW.completed_at IS NULL))
BEGIN SELECT RAISE(ABORT,'stream_session_output_safety_state_invalid'); END;

-- Provider 错误事实存在后继续冻结完整 StreamSession；新增安全状态不改变既有反向不可变合同。
CREATE TRIGGER trg_stream_session_provider_error_fact_update_prd056
BEFORE UPDATE ON stream_sessions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts fact
  WHERE fact.tenant_id=OLD.tenant_id AND fact.stream_session_id=OLD.stream_session_id
)
AND (
  NEW.rowid IS NOT OLD.rowid OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.request_id IS NOT OLD.request_id
  OR NEW.trace_id IS NOT OLD.trace_id OR NEW.virtual_key_id IS NOT OLD.virtual_key_id
  OR NEW.environment_id IS NOT OLD.environment_id
  OR NEW.environment_code IS NOT OLD.environment_code
  OR NEW.project_id IS NOT OLD.project_id OR NEW.application_id IS NOT OLD.application_id
  OR NEW.model_id IS NOT OLD.model_id OR NEW.provider_id IS NOT OLD.provider_id
  OR NEW.provider_code IS NOT OLD.provider_code OR NEW.status IS NOT OLD.status
  OR NEW.inter_token_timeout_ms IS NOT OLD.inter_token_timeout_ms
  OR NEW.chunk_count IS NOT OLD.chunk_count OR NEW.bytes_sent IS NOT OLD.bytes_sent
  OR NEW.first_chunk_at IS NOT OLD.first_chunk_at OR NEW.last_chunk_at IS NOT OLD.last_chunk_at
  OR NEW.error_code IS NOT OLD.error_code OR NEW.started_at IS NOT OLD.started_at
  OR NEW.completed_at IS NOT OLD.completed_at OR NEW.created_at IS NOT OLD.created_at
  OR NEW.updated_at IS NOT OLD.updated_at
  OR NEW.upstream_http_status IS NOT OLD.upstream_http_status
  OR NEW.retryable IS NOT OLD.retryable
)
BEGIN SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable'); END;
CREATE TRIGGER trg_stream_session_provider_error_fact_delete_prd056
BEFORE DELETE ON stream_sessions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts fact
  WHERE fact.tenant_id=OLD.tenant_id AND fact.stream_session_id=OLD.stream_session_id
)
BEGIN SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable'); END;
CREATE TRIGGER trg_stream_session_provider_error_fact_conflict_insert_prd056
BEFORE INSERT ON stream_sessions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM stream_sessions current
  JOIN provider_error_facts fact
    ON fact.tenant_id=current.tenant_id AND fact.stream_session_id=current.stream_session_id
  WHERE current.stream_session_id=NEW.stream_session_id
     OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable'); END;

-- 列替换前只移除确实引用 stream_status 的五个 trigger，其他 009-070 治理对象保持原位。
DROP TRIGGER trg_request_feedback_graph_insert_prd049;
DROP TRIGGER trg_request_log_feedback_parent_conflict_insert_prd049;
DROP TRIGGER trg_request_log_feedback_parent_update_prd049;
DROP TRIGGER trg_request_log_stream_insert;
DROP TRIGGER trg_request_log_stream_update;
-- 先保留旧列，避免扩枚举过程中丢失任何历史终态摘要。
ALTER TABLE request_logs RENAME COLUMN stream_status TO stream_status_prd071_old;
-- 新列只扩展两个固定输出安全终态，不接受任意字符串状态。
ALTER TABLE request_logs ADD COLUMN stream_status TEXT CHECK (
  stream_status IS NULL OR stream_status IN (
    'completed','timed_out','client_disconnected','upstream_failed',
    'output_blocked','output_safety_unavailable'
  )
);
-- 在同一事务内逐行回填，NULL 非流式摘要保持 NULL。
UPDATE request_logs SET stream_status=stream_status_prd071_old;
-- 回填完成后才移除临时旧列，所有引用 trigger 随后按冻结合同重建。
ALTER TABLE request_logs DROP COLUMN stream_status_prd071_old;

-- RequestLog 仍必须绑定同 tenant/request 的真实 StreamSession；两个本地安全终态均是 failed 摘要。
CREATE TRIGGER trg_request_log_stream_insert
BEFORE INSERT ON request_logs FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.stream_session_id IS NULL)<>(NEW.stream_status IS NULL)
    THEN RAISE(ABORT,'request_log_stream_fields_incomplete') END;
  SELECT CASE WHEN NEW.stream_session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM stream_sessions owner
    WHERE owner.stream_session_id=NEW.stream_session_id AND owner.tenant_id=NEW.tenant_id
      AND owner.request_id=NEW.request_id AND owner.status=NEW.stream_status
  ) THEN RAISE(ABORT,'request_log_stream_session_mismatch') END;
  SELECT CASE WHEN NEW.stream_status IS NOT NULL AND NOT (
    (NEW.stream_status='completed' AND NEW.status='success')
    OR (NEW.stream_status!='completed' AND NEW.status='failed')
  ) THEN RAISE(ABORT,'request_log_stream_status_mismatch') END;
END;
CREATE TRIGGER trg_request_log_stream_update
BEFORE UPDATE OF tenant_id,request_id,status,stream_session_id,stream_status
ON request_logs FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.stream_session_id IS NULL)<>(NEW.stream_status IS NULL)
    THEN RAISE(ABORT,'request_log_stream_fields_incomplete') END;
  SELECT CASE WHEN NEW.stream_session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM stream_sessions owner
    WHERE owner.stream_session_id=NEW.stream_session_id AND owner.tenant_id=NEW.tenant_id
      AND owner.request_id=NEW.request_id AND owner.status=NEW.stream_status
  ) THEN RAISE(ABORT,'request_log_stream_session_mismatch') END;
  SELECT CASE WHEN NEW.stream_status IS NOT NULL AND NOT (
    (NEW.stream_status='completed' AND NEW.status='success')
    OR (NEW.stream_status!='completed' AND NEW.status='failed')
  ) THEN RAISE(ABORT,'request_log_stream_status_mismatch') END;
END;

-- 恢复 Feedback 对成功调用图的精确约束；安全阻断/不可用都不是可评价的成功响应。
CREATE TRIGGER trg_request_feedback_graph_insert_prd049
BEFORE INSERT ON request_feedbacks FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM tenants tenant
    WHERE tenant.tenant_id=NEW.tenant_id AND tenant.status='active'
  )
  OR (NEW.actor_type='session_user' AND NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
      AND actor.status='active' AND actor.role IN ('admin','developer')
  ))
  OR (NEW.actor_type='virtual_key' AND NOT EXISTS (
    SELECT 1 FROM virtual_keys actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.virtual_key_id=NEW.actor_virtual_key_id
      AND actor.status='active'
  ))
  OR NOT EXISTS (
    SELECT 1 FROM request_logs request
    WHERE request.tenant_id=NEW.tenant_id AND request.request_id=NEW.source_request_id
      AND request.status='success' AND request.virtual_key_id IS NEW.virtual_key_id
      AND request.project_id IS NEW.project_id
      AND request.application_id IS NEW.application_id
      AND request.environment_id IS NEW.environment_id
      AND ((request.stream_session_id IS NULL AND request.stream_status IS NULL)
        OR (request.stream_session_id IS NOT NULL AND request.stream_status='completed'))
      AND (NEW.actor_type='session_user' OR (
        request.virtual_key_id IS NOT NULL
        AND request.virtual_key_id=NEW.actor_virtual_key_id
      ))
      AND request.model_id=NEW.model_id AND request.provider_id=NEW.provider_id
      AND ((request.routing_attempt_count=0 AND request.final_routing_attempt_id IS NULL
          AND request.model_id IS NOT NULL AND request.provider_id IS NOT NULL)
        OR (request.routing_attempt_count IN (1,2)
          AND request.final_routing_attempt_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM routing_attempts attempt
            WHERE attempt.tenant_id=request.tenant_id
              AND attempt.request_id=request.request_id
              AND attempt.routing_attempt_id=request.final_routing_attempt_id
              AND attempt.status='succeeded' AND attempt.model_id=NEW.model_id
              AND attempt.provider_id=NEW.provider_id
          )))
  )
  OR NOT EXISTS (
    SELECT 1 FROM models model
    WHERE model.tenant_id=NEW.tenant_id AND model.model_id=NEW.model_id
      AND model.provider_id=NEW.provider_id
  )
BEGIN SELECT RAISE(ABORT,'request_feedback_graph_invalid'); END;

CREATE TRIGGER trg_request_log_feedback_parent_update_prd049
BEFORE UPDATE OF
  tenant_id,request_id,virtual_key_id,status,model_id,provider_id,
  project_id,application_id,environment_id,stream_session_id,stream_status,
  final_routing_attempt_id,routing_attempt_count
ON request_logs FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_feedbacks feedback
  WHERE feedback.tenant_id=OLD.tenant_id AND feedback.source_request_id=OLD.request_id
)
AND (
  NEW.tenant_id IS NOT OLD.tenant_id OR NEW.request_id IS NOT OLD.request_id
  OR NEW.virtual_key_id IS NOT OLD.virtual_key_id OR NEW.status IS NOT OLD.status
  OR NEW.model_id IS NOT OLD.model_id OR NEW.provider_id IS NOT OLD.provider_id
  OR NEW.project_id IS NOT OLD.project_id OR NEW.application_id IS NOT OLD.application_id
  OR NEW.environment_id IS NOT OLD.environment_id
  OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.stream_status IS NOT OLD.stream_status
  OR NEW.final_routing_attempt_id IS NOT OLD.final_routing_attempt_id
  OR NEW.routing_attempt_count IS NOT OLD.routing_attempt_count
)
BEGIN SELECT RAISE(ABORT,'request_log_feedback_parent_immutable'); END;

CREATE TRIGGER trg_request_log_feedback_parent_conflict_insert_prd049
BEFORE INSERT ON request_logs FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_logs current
  JOIN request_feedbacks feedback
    ON feedback.tenant_id=current.tenant_id
   AND feedback.source_request_id=current.request_id
  WHERE current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id
    AND (current.virtual_key_id IS NOT NEW.virtual_key_id
      OR current.status IS NOT NEW.status OR current.model_id IS NOT NEW.model_id
      OR current.provider_id IS NOT NEW.provider_id OR current.project_id IS NOT NEW.project_id
      OR current.application_id IS NOT NEW.application_id
      OR current.environment_id IS NOT NEW.environment_id
      OR current.stream_session_id IS NOT NEW.stream_session_id
      OR current.stream_status IS NOT NEW.stream_status
      OR current.final_routing_attempt_id IS NOT NEW.final_routing_attempt_id
      OR current.routing_attempt_count IS NOT NEW.routing_attempt_count)
)
BEGIN SELECT RAISE(ABORT,'request_log_feedback_parent_immutable'); END;

-- 两个新增本地安全终态代表 Provider 已成功产出，Cache Affinity 选择应成功收口且不处罚 Provider。
DROP TRIGGER trg_cache_affinity_selections_prd050_update;
CREATE TRIGGER trg_cache_affinity_selections_prd050_update
BEFORE UPDATE ON cache_affinity_selections FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.cache_affinity_selection_id!=OLD.cache_affinity_selection_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.request_id!=OLD.request_id
  OR NEW.trace_id!=OLD.trace_id OR NEW.attempt_sequence!=OLD.attempt_sequence
  OR NEW.routing_attempt_id IS NOT OLD.routing_attempt_id
  OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.cache_affinity_id!=OLD.cache_affinity_id
  OR NEW.cache_affinity_version!=OLD.cache_affinity_version
  OR NEW.cache_affinity_scope_id_snapshot!=OLD.cache_affinity_scope_id_snapshot
  OR NEW.cache_affinity_binding_id_snapshot!=OLD.cache_affinity_binding_id_snapshot
  OR NEW.model_alias_id!=OLD.model_alias_id OR NEW.model_endpoint_id!=OLD.model_endpoint_id
  OR NEW.endpoint_revision_digest_snapshot!=OLD.endpoint_revision_digest_snapshot
  OR NEW.provider_id!=OLD.provider_id OR NEW.provider_code_snapshot!=OLD.provider_code_snapshot
  OR NEW.provider_credential_id_snapshot IS NOT OLD.provider_credential_id_snapshot
  OR NEW.target_kind!=OLD.target_kind OR NEW.target_generation!=OLD.target_generation
  OR NEW.pending_lease_generation_snapshot!=OLD.pending_lease_generation_snapshot
  OR NEW.ttl_seconds_snapshot!=OLD.ttl_seconds_snapshot
  OR NEW.warm_evidence_at_snapshot IS NOT OLD.warm_evidence_at_snapshot
  OR NEW.endpoint_preference_applied!=OLD.endpoint_preference_applied
  OR NEW.endpoint_preference_state!=OLD.endpoint_preference_state
  OR NEW.binding_state_before!=OLD.binding_state_before
  OR NEW.selection_reason!=OLD.selection_reason OR NEW.estimated_hit!=OLD.estimated_hit
  OR NEW.created_at!=OLD.created_at
  OR NOT (
    (OLD.outcome_status='pending' AND NEW.outcome_status IN ('succeeded','failed'))
    OR (OLD.outcome_status IN ('succeeded','failed')
      AND NEW.outcome_status=OLD.outcome_status AND NEW.outcome_at=OLD.outcome_at)
  )
  OR (OLD.outcome_status='pending' AND NEW.outcome_status='succeeded' AND NOT (
    EXISTS (
      SELECT 1 FROM routing_attempts attempt
      WHERE attempt.tenant_id=NEW.tenant_id
        AND attempt.routing_attempt_id=NEW.routing_attempt_id
        AND attempt.status='succeeded' AND attempt.completed_at=NEW.outcome_at
    ) OR EXISTS (
      SELECT 1 FROM stream_sessions session
      WHERE session.tenant_id=NEW.tenant_id
        AND session.stream_session_id=NEW.stream_session_id
        AND session.status IN ('completed','output_blocked','output_safety_unavailable')
        AND session.completed_at=NEW.outcome_at
    )
  ))
  OR (OLD.outcome_status='pending' AND NEW.outcome_status='failed' AND NOT (
    EXISTS (
      SELECT 1 FROM routing_attempts attempt
      WHERE attempt.tenant_id=NEW.tenant_id
        AND attempt.routing_attempt_id=NEW.routing_attempt_id
        AND attempt.status='failed' AND attempt.completed_at=NEW.outcome_at
    ) OR EXISTS (
      SELECT 1 FROM stream_sessions session
      WHERE session.tenant_id=NEW.tenant_id
        AND session.stream_session_id=NEW.stream_session_id
        AND session.status IN ('timed_out','client_disconnected','upstream_failed')
        AND session.completed_at=NEW.outcome_at
    )
  ))
BEGIN SELECT RAISE(ABORT,'cache_affinity_selection_transition_invalid'); END;

-- Usage/FinOps 的现有全局 ID 仍保留，同时增加 tenant/request 复合候选键供争议事实 FK 使用。
CREATE UNIQUE INDEX idx_usage_ledger_request_owner_prd071
  ON usage_ledger(tenant_id,request_id,usage_ledger_id);
CREATE UNIQUE INDEX idx_finops_ledger_request_owner_prd071
  ON finops_ledger_entries(tenant_id,source_request_id,finops_ledger_entry_id);

-- 每个 tenant/request 只有一个扫描 owner；pending 在 Provider 前提交，随后只允许一次终结。
CREATE TABLE output_safety_scans (
  output_safety_scan_id TEXT PRIMARY KEY CHECK (
    length(output_safety_scan_id)=36
    AND substr(output_safety_scan_id,1,4)='oss_'
    AND substr(output_safety_scan_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128 AND instr(request_id,char(0))=0
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128 AND instr(trace_id,char(0))=0
  ),
  stream_session_id TEXT,
  scan_mode TEXT NOT NULL CHECK (scan_mode IN ('non_stream_full','stream_window')),
  scan_status TEXT NOT NULL CHECK (
    scan_status IN ('pending','not_configured','completed','unavailable','not_applicable')
  ),
  decision TEXT NOT NULL CHECK (decision IN ('none','clean','matched','blocked','unknown')),
  action TEXT CHECK (action IS NULL OR action IN ('allow','log_only','block')),
  dlp_rule_id TEXT,
  rule_type TEXT CHECK (rule_type IS NULL OR rule_type IN ('keyword','regex')),
  match_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(match_count)='integer' AND match_count>=0
  ),
  ruleset_fingerprint TEXT CHECK (
    ruleset_fingerprint IS NULL OR (
      length(ruleset_fingerprint)=64
      AND ruleset_fingerprint NOT GLOB '*[^0-9a-f]*'
    )
  ),
  detector_version TEXT NOT NULL DEFAULT 'output-safety-dlp-v1'
    CHECK (detector_version='output-safety-dlp-v1'),
  interval_tokens INTEGER CHECK (
    interval_tokens IS NULL OR (
      typeof(interval_tokens)='integer' AND interval_tokens BETWEEN 8 AND 256
    )
  ),
  window_tokens INTEGER CHECK (
    window_tokens IS NULL OR (
      typeof(window_tokens)='integer' AND window_tokens BETWEEN 32 AND 1024
    )
  ),
  checkpoint_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(checkpoint_count)='integer' AND checkpoint_count>=0
  ),
  observed_output_token_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(observed_output_token_count)='integer' AND observed_output_token_count>=0
  ),
  scanned_token_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(scanned_token_count)='integer' AND scanned_token_count>=0
  ),
  emitted_token_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(emitted_token_count)='integer' AND emitted_token_count>=0
  ),
  emitted_sse_event_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(emitted_sse_event_count)='integer' AND emitted_sse_event_count>=0
  ),
  emitted_bytes INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(emitted_bytes)='integer' AND emitted_bytes>=0
  ),
  exposure_state TEXT NOT NULL CHECK (
    exposure_state IN ('none','partial','complete','unknown')
  ),
  billing_disposition TEXT NOT NULL CHECK (
    billing_disposition IN ('not_required','dispute_pending')
  ),
  failure_reason_code TEXT CHECK (
    failure_reason_code IS NULL OR failure_reason_code IN (
      'output_safety.fragment_limit_exceeded',
      'output_safety.response_shape_invalid',
      'output_safety.response_too_large',
      'output_safety.evidence_unavailable',
      'output_safety.rule_invalid',
      'output_safety.rule_snapshot_unavailable',
      'output_safety.sse_event_incomplete',
      'output_safety.sse_event_too_large',
      'output_safety.sse_json_duplicate_key',
      'output_safety.sse_json_invalid',
      'output_safety.sse_utf8_invalid',
      'output_safety.stream_incomplete',
      'output_safety.window_capacity_exceeded'
    )
  ),
  started_at TEXT NOT NULL CHECK (
    length(started_at) BETWEEN 1 AND 64 AND unixepoch(started_at) IS NOT NULL
  ),
  completed_at TEXT CHECK (
    completed_at IS NULL OR (
      length(completed_at) BETWEEN 1 AND 64 AND unixepoch(completed_at) IS NOT NULL
      AND unixepoch(completed_at)>=unixepoch(started_at)
    )
  ),
  UNIQUE (tenant_id,request_id),
  UNIQUE (tenant_id,output_safety_scan_id),
  UNIQUE (tenant_id,request_id,output_safety_scan_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,request_id)
    REFERENCES gateway_request_claims(tenant_id,request_id),
  FOREIGN KEY (tenant_id,dlp_rule_id)
    REFERENCES dlp_rules(tenant_id,dlp_rule_id),
  FOREIGN KEY (tenant_id,request_id,stream_session_id)
    REFERENCES stream_sessions(tenant_id,request_id,stream_session_id),
  -- 非流式没有会话/窗口参数；流式三者必须同时存在且窗口不得小于检查间隔。
  CHECK (
    (scan_mode='non_stream_full' AND stream_session_id IS NULL
      AND interval_tokens IS NULL AND window_tokens IS NULL)
    OR (scan_mode='stream_window' AND stream_session_id IS NOT NULL
      AND interval_tokens IS NOT NULL AND window_tokens IS NOT NULL
      AND window_tokens>=interval_tokens)
  ),
  CHECK (emitted_token_count<=scanned_token_count),
  CHECK (scanned_token_count<=observed_output_token_count),
  CHECK (scan_status!='completed' OR checkpoint_count>=1),
  -- 非流式阻断发生在完整响应返回前，数据库不得接受 partial 或任何已交付计数。
  CHECK (
    scan_status!='completed' OR decision!='blocked' OR scan_mode!='non_stream_full'
    OR (exposure_state='none' AND emitted_token_count=0
      AND emitted_sse_event_count=0 AND emitted_bytes=0)
  ),
  -- 状态矩阵把“不配置”“完整 clean”和“不可证明”严格分开，任何未知历史都不能伪造成 clean。
  CHECK (
    (scan_status='pending' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NULL)
    OR
    (scan_status='not_configured' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NOT NULL)
    OR
    (scan_status='not_applicable' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='clean' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND exposure_state='complete'
      AND billing_disposition='not_required' AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='matched' AND action IN ('allow','log_only')
      AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL AND match_count>=1
      AND ruleset_fingerprint IS NOT NULL AND exposure_state='complete'
      AND billing_disposition='not_required' AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='blocked' AND action='block'
      AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL AND match_count>=1
      AND ruleset_fingerprint IS NOT NULL AND exposure_state IN ('none','partial')
      AND billing_disposition='dispute_pending' AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL)
    OR
    (scan_status='unavailable' AND decision IN ('unknown','matched')
      AND exposure_state IN ('none','partial','unknown')
      AND billing_disposition='not_required' AND failure_reason_code IS NOT NULL
      AND completed_at IS NOT NULL
      AND ((decision='unknown' AND action IS NULL AND dlp_rule_id IS NULL
          AND rule_type IS NULL AND match_count=0)
        OR (decision='matched' AND action IN ('allow','log_only')
          AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL
          AND match_count>=1 AND ruleset_fingerprint IS NOT NULL)))
  )
);

CREATE INDEX idx_output_safety_scans_tenant_time_prd071
  ON output_safety_scans(tenant_id,started_at DESC,output_safety_scan_id DESC);
CREATE INDEX idx_output_safety_scans_tenant_status_time_prd071
  ON output_safety_scans(tenant_id,scan_status,decision,started_at DESC);

-- INSERT 同时复核 claim trace、流 owner 和 DLP 类型；FK 只负责存在性，不能表达这些快照等式。
CREATE TRIGGER trg_output_safety_scans_owner_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id AND claim.request_id=NEW.request_id
      AND claim.trace_id=NEW.trace_id
  )
  OR (NEW.scan_mode='stream_window' AND NOT EXISTS (
    SELECT 1 FROM stream_sessions session
    WHERE session.tenant_id=NEW.tenant_id AND session.request_id=NEW.request_id
      AND session.stream_session_id=NEW.stream_session_id
      AND session.trace_id=NEW.trace_id
  ))
  OR (NEW.dlp_rule_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM dlp_rules rule
    WHERE rule.tenant_id=NEW.tenant_id AND rule.dlp_rule_id=NEW.dlp_rule_id
      AND rule.rule_type=NEW.rule_type
  ))
BEGIN SELECT RAISE(ABORT,'output_safety_scan_owner_mismatch'); END;

-- 终态只能由已提交 pending owner 单向产生，禁止绕过 Provider 前闸门直接插入结果。
CREATE TRIGGER trg_output_safety_scans_state_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN NEW.scan_status!='pending'
BEGIN SELECT RAISE(ABORT,'output_safety_scan_initial_state_invalid'); END;

-- 全局 ID、tenant/request 唯一 owner 和显式 rowid 冲突都不能被 INSERT OR REPLACE 覆盖。
CREATE TRIGGER trg_output_safety_scans_conflict_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM output_safety_scans current
  WHERE current.output_safety_scan_id=NEW.output_safety_scan_id
     OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_immutable'); END;

-- scan 身份、模式、检测器与起始窗口在 pending 创建时冻结，终结不能换 owner 或参数。
CREATE TRIGGER trg_output_safety_scans_owner_update_prd071
BEFORE UPDATE ON output_safety_scans FOR EACH ROW
WHEN NEW.rowid IS NOT OLD.rowid
  OR NEW.output_safety_scan_id IS NOT OLD.output_safety_scan_id
  OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.request_id IS NOT OLD.request_id
  OR NEW.trace_id IS NOT OLD.trace_id
  OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.scan_mode IS NOT OLD.scan_mode
  OR NEW.detector_version IS NOT OLD.detector_version
  OR NEW.interval_tokens IS NOT OLD.interval_tokens
  OR NEW.window_tokens IS NOT OLD.window_tokens
  OR NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT,'output_safety_scan_owner_immutable'); END;

-- 决定性规则只在终结时写入，因此 UPDATE 也必须重新复核 tenant 与规则类型。
CREATE TRIGGER trg_output_safety_scans_rule_update_prd071
BEFORE UPDATE OF dlp_rule_id,rule_type ON output_safety_scans FOR EACH ROW
WHEN NEW.dlp_rule_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM dlp_rules rule
  WHERE rule.tenant_id=NEW.tenant_id AND rule.dlp_rule_id=NEW.dlp_rule_id
    AND rule.rule_type=NEW.rule_type
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_rule_owner_mismatch'); END;

-- pending 只能单向进入一个终态；终态仅允许逐字段全同值幂等 UPDATE。
CREATE TRIGGER trg_output_safety_scans_transition_prd071
BEFORE UPDATE ON output_safety_scans FOR EACH ROW
WHEN NOT (
  (NEW.rowid IS OLD.rowid
    AND NEW.output_safety_scan_id IS OLD.output_safety_scan_id
    AND NEW.tenant_id IS OLD.tenant_id AND NEW.request_id IS OLD.request_id
    AND NEW.trace_id IS OLD.trace_id
    AND NEW.stream_session_id IS OLD.stream_session_id
    AND NEW.scan_mode IS OLD.scan_mode AND NEW.scan_status IS OLD.scan_status
    AND NEW.decision IS OLD.decision AND NEW.action IS OLD.action
    AND NEW.dlp_rule_id IS OLD.dlp_rule_id AND NEW.rule_type IS OLD.rule_type
    AND NEW.match_count IS OLD.match_count
    AND NEW.ruleset_fingerprint IS OLD.ruleset_fingerprint
    AND NEW.detector_version IS OLD.detector_version
    AND NEW.interval_tokens IS OLD.interval_tokens
    AND NEW.window_tokens IS OLD.window_tokens
    AND NEW.checkpoint_count IS OLD.checkpoint_count
    AND NEW.observed_output_token_count IS OLD.observed_output_token_count
    AND NEW.scanned_token_count IS OLD.scanned_token_count
    AND NEW.emitted_token_count IS OLD.emitted_token_count
    AND NEW.emitted_sse_event_count IS OLD.emitted_sse_event_count
    AND NEW.emitted_bytes IS OLD.emitted_bytes
    AND NEW.exposure_state IS OLD.exposure_state
    AND NEW.billing_disposition IS OLD.billing_disposition
    AND NEW.failure_reason_code IS OLD.failure_reason_code
    AND NEW.started_at IS OLD.started_at AND NEW.completed_at IS OLD.completed_at)
  OR (OLD.scan_status='pending' AND NEW.scan_status IN (
    'not_configured','completed','unavailable','not_applicable'
  ))
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_transition_invalid'); END;

CREATE TRIGGER trg_output_safety_scans_delete_prd071
BEFORE DELETE ON output_safety_scans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_scan_immutable'); END;

-- 阻断 source event 只投影固定 Webhook 字段，不保存自由 JSON 或命中正文。
CREATE TABLE output_safety_events (
  event_id TEXT PRIMARY KEY CHECK (
    length(event_id)=38 AND substr(event_id,1,6)='osevt_'
    AND substr(event_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  output_safety_scan_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type='security.output_blocked'),
  schema_version INTEGER NOT NULL CHECK (schema_version=1),
  scan_mode TEXT NOT NULL CHECK (scan_mode IN ('non_stream_full','stream_window')),
  scan_status TEXT NOT NULL CHECK (scan_status='completed'),
  decision TEXT NOT NULL CHECK (decision='blocked'),
  error_code TEXT NOT NULL CHECK (error_code='content_policy_violation'),
  exposure_state TEXT NOT NULL CHECK (exposure_state IN ('none','partial')),
  emitted_token_count INTEGER NOT NULL CHECK (
    typeof(emitted_token_count)='integer' AND emitted_token_count>=0
  ),
  checkpoint_count INTEGER NOT NULL CHECK (
    typeof(checkpoint_count)='integer' AND checkpoint_count>=0
  ),
  dlp_rule_id TEXT NOT NULL,
  detector_version TEXT NOT NULL CHECK (detector_version='output-safety-dlp-v1'),
  billing_disposition TEXT NOT NULL CHECK (billing_disposition='dispute_pending'),
  occurred_at TEXT NOT NULL CHECK (
    length(occurred_at) BETWEEN 1 AND 64 AND unixepoch(occurred_at) IS NOT NULL
  ),
  UNIQUE (tenant_id,output_safety_scan_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,request_id)
    REFERENCES gateway_request_claims(tenant_id,request_id),
  FOREIGN KEY (tenant_id,request_id,output_safety_scan_id)
    REFERENCES output_safety_scans(tenant_id,request_id,output_safety_scan_id)
);
CREATE INDEX idx_output_safety_events_tenant_time_prd071
  ON output_safety_events(tenant_id,occurred_at DESC,event_id DESC);

CREATE TRIGGER trg_output_safety_events_owner_insert_prd071
BEFORE INSERT ON output_safety_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM output_safety_scans scan
  WHERE scan.tenant_id=NEW.tenant_id AND scan.request_id=NEW.request_id
    AND scan.output_safety_scan_id=NEW.output_safety_scan_id
    AND scan.trace_id=NEW.trace_id AND scan.scan_status='completed'
    AND scan.decision='blocked' AND scan.scan_mode=NEW.scan_mode
    AND scan.exposure_state=NEW.exposure_state
    AND scan.emitted_token_count=NEW.emitted_token_count
    AND scan.checkpoint_count=NEW.checkpoint_count
    AND scan.dlp_rule_id=NEW.dlp_rule_id
    AND scan.detector_version=NEW.detector_version
    AND scan.billing_disposition=NEW.billing_disposition
    AND scan.completed_at=NEW.occurred_at
)
BEGIN SELECT RAISE(ABORT,'output_safety_event_owner_mismatch'); END;
CREATE TRIGGER trg_output_safety_events_conflict_insert_prd071
BEFORE INSERT ON output_safety_events FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM output_safety_events current
  WHERE current.event_id=NEW.event_id
     OR (current.tenant_id=NEW.tenant_id
       AND current.output_safety_scan_id=NEW.output_safety_scan_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'output_safety_event_immutable'); END;
CREATE TRIGGER trg_output_safety_events_update_prd071
BEFORE UPDATE ON output_safety_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_event_immutable'); END;
CREATE TRIGGER trg_output_safety_events_delete_prd071
BEFORE DELETE ON output_safety_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_event_immutable'); END;

-- 争议 marker 与正式 Usage/usage_charge 同 owner；真实费用保留，只冻结人工复核意图。
CREATE TABLE finops_ledger_disputes (
  finops_ledger_dispute_id TEXT PRIMARY KEY CHECK (
    length(finops_ledger_dispute_id)=36
    AND substr(finops_ledger_dispute_id,1,4)='fld_'
    AND substr(finops_ledger_dispute_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  output_safety_scan_id TEXT NOT NULL,
  usage_ledger_id TEXT NOT NULL,
  finops_ledger_entry_id TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status='dispute_pending'),
  reversal_eligible INTEGER NOT NULL CHECK (reversal_eligible IN (0,1)),
  reason_code TEXT NOT NULL CHECK (
    reason_code='output_safety.content_policy_violation'
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at) BETWEEN 1 AND 64 AND unixepoch(created_at) IS NOT NULL
  ),
  UNIQUE (tenant_id,request_id,output_safety_scan_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,request_id)
    REFERENCES gateway_request_claims(tenant_id,request_id),
  FOREIGN KEY (tenant_id,request_id,output_safety_scan_id)
    REFERENCES output_safety_scans(tenant_id,request_id,output_safety_scan_id),
  FOREIGN KEY (tenant_id,request_id,usage_ledger_id)
    REFERENCES usage_ledger(tenant_id,request_id,usage_ledger_id),
  FOREIGN KEY (tenant_id,request_id,finops_ledger_entry_id)
    REFERENCES finops_ledger_entries(
      tenant_id,source_request_id,finops_ledger_entry_id
    )
);
CREATE INDEX idx_finops_ledger_disputes_tenant_time_prd071
  ON finops_ledger_disputes(tenant_id,created_at DESC,finops_ledger_dispute_id DESC);
CREATE INDEX idx_finops_ledger_disputes_tenant_review_prd071
  ON finops_ledger_disputes(tenant_id,review_status,created_at DESC);

CREATE TRIGGER trg_finops_ledger_disputes_owner_insert_prd071
BEFORE INSERT ON finops_ledger_disputes FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM output_safety_scans scan
    WHERE scan.tenant_id=NEW.tenant_id AND scan.request_id=NEW.request_id
      AND scan.output_safety_scan_id=NEW.output_safety_scan_id
      AND scan.trace_id=NEW.trace_id AND scan.scan_status='completed'
      AND scan.decision='blocked' AND scan.billing_disposition='dispute_pending'
  )
  OR NOT EXISTS (
    SELECT 1 FROM usage_ledger usage
    WHERE usage.tenant_id=NEW.tenant_id AND usage.request_id=NEW.request_id
      AND usage.usage_ledger_id=NEW.usage_ledger_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM finops_ledger_entries charge
    WHERE charge.tenant_id=NEW.tenant_id
      AND charge.source_request_id=NEW.request_id
      AND charge.source_trace_id=NEW.trace_id
      AND charge.finops_ledger_entry_id=NEW.finops_ledger_entry_id
      AND charge.entry_type='usage_charge'
      AND charge.usage_ledger_id=NEW.usage_ledger_id
      AND NEW.reversal_eligible=CASE WHEN charge.amount_micros>0 THEN 1 ELSE 0 END
  )
BEGIN SELECT RAISE(ABORT,'finops_ledger_dispute_owner_mismatch'); END;
CREATE TRIGGER trg_finops_ledger_disputes_conflict_insert_prd071
BEFORE INSERT ON finops_ledger_disputes FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM finops_ledger_disputes current
  WHERE current.finops_ledger_dispute_id=NEW.finops_ledger_dispute_id
     OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id
       AND current.output_safety_scan_id=NEW.output_safety_scan_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'finops_ledger_dispute_immutable'); END;
CREATE TRIGGER trg_finops_ledger_disputes_update_prd071
BEFORE UPDATE ON finops_ledger_disputes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'finops_ledger_dispute_immutable'); END;
CREATE TRIGGER trg_finops_ledger_disputes_delete_prd071
BEFORE DELETE ON finops_ledger_disputes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'finops_ledger_dispute_immutable'); END;

-- PAGE-099 会把四个输出安全枚举写入 QueryEvent 的“字段名集合”；0045 的触发器
-- 仍只允许旧字段名，因此必须在同一迁移内扩展白名单。触发器继续拒绝字段值、重复键
-- 和未排序数组，避免新增筛选能力削弱不可变查询审计。
DROP TRIGGER trg_request_log_query_events_keys_insert_prd055;
CREATE TRIGGER trg_request_log_query_events_keys_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN NOT (
  json_valid(NEW.filter_keys_json)
  AND CASE WHEN json_valid(NEW.filter_keys_json)
    THEN json_type(NEW.filter_keys_json) END='array'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
  ))<=28
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) item
    WHERE item.type!='text' OR item.value NOT IN (
      'request_id','trace_id','status','model','request_model','resolved_model',
      'provider_code','virtual_key_id','environment_id','project_id','application_id',
      'snapshot_version','error_code','created_from','created_to','latency_ms_min',
      'latency_ms_max','total_tokens_min','total_tokens_max','cost_amount_min',
      'cost_amount_max','currency','terminal_stage','output_safety_status',
      'output_safety_mode','output_safety_exposure','output_safety_billing'
    )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) earlier
    JOIN json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) later ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE CAST(earlier.value AS TEXT)>=CAST(later.value AS TEXT)
  )
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_keys_invalid'); END;

-- 提交前统一验收对象形状、revision 7 原样保留以及全部 SQLite 完整性。
DROP TABLE IF EXISTS temp.prd071_up_final_guard;
CREATE TEMP TABLE prd071_up_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd071_up_final_ok CHECK (issue_count=0)
);
-- 最终 guard 同时验证新表形状、trigger 数量、RBAC 与 SQLite 完整性。
INSERT INTO prd071_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('dlp_rules'))=15
      AND (SELECT COUNT(*) FROM pragma_table_info('stream_sessions'))=25
      AND (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=66
      AND (SELECT COUNT(*) FROM pragma_table_info('output_safety_scans'))=27
      AND (SELECT COUNT(*) FROM pragma_table_info('output_safety_events'))=18
      AND (SELECT COUNT(*) FROM pragma_table_info('finops_ledger_disputes'))=11
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=59
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=7)=59
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=155
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*59 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=7)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='stream_sessions')=8
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='request_logs')=14
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='output_safety_scans')=7
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='output_safety_events')=4
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='finops_ledger_disputes')=4
    THEN 0 ELSE 1 END
  -- 最终 guard 证明查询审计触发器已同时识别四个新字段，而不是只改应用层解析器。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name='trg_request_log_query_events_keys_insert_prd055'
        AND instr(sql,'''output_safety_status''')>0
        AND instr(sql,'''output_safety_mode''')>0
        AND instr(sql,'''output_safety_exposure''')>0
        AND instr(sql,'''output_safety_billing''')>0)=1
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd071_up_final_guard;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
