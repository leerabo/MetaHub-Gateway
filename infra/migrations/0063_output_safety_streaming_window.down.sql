-- PRD-071 回滚：只在没有输出安全业务事实、方向配置、治理引用和后续 schema 依赖时恢复 0062。
-- 所有失败关闭门禁都位于首项持久 DDL 前；回滚绝不通过删除安全证据来制造“可降级”。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 业务门禁聚合所有不可逆事实并用稳定错误码原子拒绝。
DROP TABLE IF EXISTS temp.prd071_down_business_guard;
DROP TRIGGER IF EXISTS temp.trg_prd071_down_business_guard;
CREATE TEMP TABLE prd071_down_business_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd071_down_business_guard
BEFORE INSERT ON prd071_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd071_down_business_facts_present'); END;
-- 所有不可逆业务事实聚合后一次写入失败触发器，确保首项 DDL 尚未发生。
INSERT INTO prd071_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM output_safety_scans)
  + (SELECT COUNT(*) FROM output_safety_events)
  + (SELECT COUNT(*) FROM finops_ledger_disputes)
  -- 非 input 方向是管理员显式配置，不能靠删列把 output/both 静默改回 input。
  + (SELECT COUNT(*) FROM dlp_rules WHERE scan_scope!='input')
  + (SELECT COUNT(*) FROM stream_sessions
     WHERE status IN ('output_blocked','output_safety_unavailable'))
  + (SELECT COUNT(*) FROM request_logs
     WHERE stream_status IN ('output_blocked','output_safety_unavailable'))
  -- Endpoint、投递、尝试和死信均是不可变治理事实，任一层存在都必须保留 0063。
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE (json_valid(endpoint.event_types_json) AND EXISTS (
       SELECT 1 FROM json_each(endpoint.event_types_json) item
       WHERE item.value='security.output_blocked'
     )) OR lower(endpoint.event_types_json) LIKE '%security.output_blocked%')
  + (SELECT COUNT(*) FROM webhook_deliveries
     WHERE event_type='security.output_blocked')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='security.output_blocked')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
     WHERE event_type='security.output_blocked')
  -- DLP 新 Audit 会在摘要/changed_fields 中携带 scan_scope；输出安全对象也保守文本兜底。
  + (SELECT COUNT(*) FROM audit_logs audit
     WHERE lower(audit.action) LIKE 'output_safety.%'
        OR lower(audit.object_type) LIKE 'output_safety%'
        OR instr(lower(audit.before_summary_json),'scan_scope')>0
        OR instr(lower(audit.after_summary_json),'scan_scope')>0
        OR instr(lower(audit.before_summary_json),'output_safety')>0
        OR instr(lower(audit.after_summary_json),'output_safety')>0
        OR instr(lower(audit.changed_fields_json),'scan_scope')>0
        OR instr(lower(audit.changed_fields_json),'output_safety')>0);
DROP TRIGGER trg_prd071_down_business_guard;
DROP TABLE prd071_down_business_guard;

-- 结构门禁冻结 0063 栈顶、表形状、revision 7 与所有未知后续引用。
DROP TABLE IF EXISTS temp.prd071_down_guard;
CREATE TEMP TABLE prd071_down_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd071_down_guard_ok CHECK (issue_count=0)
);
-- 结构、版本和未知依赖必须同时匹配 0063 冻结合同才允许继续。
INSERT INTO prd071_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0063' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0063')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=59
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=7)=59
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=155
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*59 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=7)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('dlp_rules'))=15
      AND (SELECT COUNT(*) FROM pragma_table_info('stream_sessions'))=25
      AND (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=66
      AND (SELECT COUNT(*) FROM pragma_table_info('output_safety_scans'))=27
      AND (SELECT COUNT(*) FROM pragma_table_info('output_safety_events'))=18
      AND (SELECT COUNT(*) FROM pragma_table_info('finops_ledger_disputes'))=11
    THEN 0 ELSE 1 END
  -- 0063 必须仍持有扩展后的查询审计白名单；若触发器被后续代码改写则拒绝降级。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name='trg_request_log_query_events_keys_insert_prd055'
        AND instr(sql,'''output_safety_status''')>0
        AND instr(sql,'''output_safety_mode''')>0
        AND instr(sql,'''output_safety_exposure''')>0
        AND instr(sql,'''output_safety_billing''')>0)=1
    THEN 0 ELSE 1 END
  -- 任意非 0063 自有对象引用新表、新列或新状态，都视为未知后续依赖并失败关闭。
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (instr(lower(object.sql),'output_safety_scans')>0
         OR instr(lower(object.sql),'output_safety_events')>0
         OR instr(lower(object.sql),'finops_ledger_disputes')>0
         OR instr(lower(object.sql),'scan_scope')>0
         OR instr(lower(object.sql),'''output_blocked''')>0
         OR instr(lower(object.sql),'''output_safety_unavailable''')>0)
       AND object.name NOT IN (
         'dlp_rules','idx_dlp_rules_tenant_scope_status_priority_prd071',
         'stream_sessions','idx_stream_sessions_tenant_provider_completed',
         'trg_stream_session_status_transition',
         'trg_stream_session_output_safety_insert_prd071',
         'trg_stream_session_output_safety_update_prd071',
         'request_logs','trg_request_log_stream_insert','trg_request_log_stream_update',
         'trg_request_log_query_events_keys_insert_prd055',
         'trg_cache_affinity_selections_prd050_update',
         'output_safety_scans','idx_output_safety_scans_tenant_time_prd071',
         'idx_output_safety_scans_tenant_status_time_prd071',
         'trg_output_safety_scans_owner_insert_prd071',
         'trg_output_safety_scans_state_insert_prd071',
         'trg_output_safety_scans_conflict_insert_prd071',
         'trg_output_safety_scans_owner_update_prd071',
         'trg_output_safety_scans_rule_update_prd071',
         'trg_output_safety_scans_transition_prd071',
         'trg_output_safety_scans_delete_prd071',
         'output_safety_events','idx_output_safety_events_tenant_time_prd071',
         'trg_output_safety_events_owner_insert_prd071',
         'trg_output_safety_events_conflict_insert_prd071',
         'trg_output_safety_events_update_prd071',
         'trg_output_safety_events_delete_prd071',
         'finops_ledger_disputes','idx_finops_ledger_disputes_tenant_time_prd071',
         'idx_finops_ledger_disputes_tenant_review_prd071',
         'trg_finops_ledger_disputes_owner_insert_prd071',
         'trg_finops_ledger_disputes_conflict_insert_prd071',
         'trg_finops_ledger_disputes_update_prd071',
         'trg_finops_ledger_disputes_delete_prd071'
       ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd071_down_guard;

-- 首项持久 DDL：业务门禁已证明三张事实表为空，按 child -> parent 顺序安全移除。
DROP TABLE finops_ledger_disputes;
DROP TABLE output_safety_events;
DROP TABLE output_safety_scans;
DROP INDEX idx_usage_ledger_request_owner_prd071;
DROP INDEX idx_finops_ledger_request_owner_prd071;

-- Cache Affinity 恢复 0062 语义：只有 completed StreamSession 可以把选择收口为 succeeded。
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
        AND session.status='completed' AND session.completed_at=NEW.outcome_at
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

-- RequestLog 先移除五个 stream_status 引用，再以列替换恢复 0062 精确枚举。
DROP TRIGGER trg_request_feedback_graph_insert_prd049;
DROP TRIGGER trg_request_log_feedback_parent_conflict_insert_prd049;
DROP TRIGGER trg_request_log_feedback_parent_update_prd049;
DROP TRIGGER trg_request_log_stream_insert;
DROP TRIGGER trg_request_log_stream_update;
-- 先保留 0063 列，业务门禁已证明其中没有两个新终态事实。
ALTER TABLE request_logs RENAME COLUMN stream_status TO stream_status_prd071;
-- 重新建立 0062 四终态精确枚举，禁止降级后遗留未知值。
ALTER TABLE request_logs ADD COLUMN stream_status TEXT CHECK (
  stream_status IS NULL OR stream_status IN (
    'completed','timed_out','client_disconnected','upstream_failed'
  )
);
-- 同事务回填旧枚举值，非流式 NULL 摘要保持不变。
UPDATE request_logs SET stream_status=stream_status_prd071;
-- 回填和 CHECK 验证完成后才删除 0063 临时列。
ALTER TABLE request_logs DROP COLUMN stream_status_prd071;

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

-- 恢复 Feedback 对成功响应图与 RequestLog 归因字段的原有冻结合同。
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

-- StreamSession 保真换表恢复 0062 的六状态枚举，业务门禁已证明没有两个新终态事实。
ALTER TABLE stream_sessions RENAME TO stream_sessions_prd071;
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
    status IN ('starting','active','completed','timed_out','client_disconnected','upstream_failed')
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
-- 显式复制全部列和 rowid，恢复过程中不得改变历史会话身份。
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
FROM stream_sessions_prd071;
DROP TABLE stream_sessions_prd071;

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
  WHERE status IN ('completed','timed_out','client_disconnected','upstream_failed')
    AND completed_at IS NOT NULL;

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

CREATE TRIGGER trg_stream_session_status_transition
BEFORE UPDATE OF status ON stream_sessions FOR EACH ROW
WHEN NOT (
  NEW.status=OLD.status
  OR (OLD.status='starting' AND NEW.status IN ('active','upstream_failed'))
  OR (OLD.status='active' AND NEW.status IN (
    'completed','timed_out','client_disconnected','upstream_failed'
  ))
)
BEGIN SELECT RAISE(ABORT,'stream_session_status_transition_invalid'); END;

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

-- 移除 0063 方向索引和候选键后再删列；business guard 已证明所有值都是默认 input。
DROP INDEX idx_dlp_rules_tenant_scope_status_priority_prd071;
DROP INDEX idx_dlp_rules_tenant_owner_prd071;
-- 非 input 配置已由 business guard 排除，删除方向列不会篡改管理员事实。
ALTER TABLE dlp_rules DROP COLUMN scan_scope;

-- 降级后应用层也会回到 0062，因此同步恢复 0045 的旧查询字段白名单；保留同名触发器
-- 可维持审计对象身份，且继续拒绝四个 0063 专属字段名。
DROP TRIGGER trg_request_log_query_events_keys_insert_prd055;
CREATE TRIGGER trg_request_log_query_events_keys_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN NOT (
  json_valid(NEW.filter_keys_json)
  AND CASE WHEN json_valid(NEW.filter_keys_json)
    THEN json_type(NEW.filter_keys_json) END='array'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
  ))<=24
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) item
    WHERE item.type!='text' OR item.value NOT IN (
      'request_id','trace_id','status','model','request_model','resolved_model',
      'provider_code','virtual_key_id','environment_id','project_id','application_id',
      'snapshot_version','error_code','created_from','created_to','latency_ms_min',
      'latency_ms_max','total_tokens_min','total_tokens_max','cost_amount_min',
      'cost_amount_max','currency','terminal_stage'
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

-- 最终检查 0062 形状、对象数量、revision 7 与完整性；任一失败回滚本次 down 的全部 DDL。
DROP TABLE IF EXISTS temp.prd071_down_final_guard;
CREATE TEMP TABLE prd071_down_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd071_down_final_ok CHECK (issue_count=0)
);
-- 提交前证明 0062 对象计数、revision 7 和 SQLite 完整性全部恢复。
INSERT INTO prd071_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('dlp_rules'))=14
      AND (SELECT COUNT(*) FROM pragma_table_info('stream_sessions'))=25
      AND (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=66
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='table'
        AND name IN ('output_safety_scans','output_safety_events','finops_ledger_disputes'))=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND tbl_name='dlp_rules' AND name NOT LIKE 'sqlite_autoindex%')=2
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND tbl_name='stream_sessions' AND name NOT LIKE 'sqlite_autoindex%')=5
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='stream_sessions')=6
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND tbl_name='request_logs' AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND tbl_name='request_logs')=14
    THEN 0 ELSE 1 END
  -- 恢复后的 0045 触发器不得残留任何 0063 专属字段名。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name='trg_request_log_query_events_keys_insert_prd055'
        AND instr(sql,'output_safety_status')=0
        AND instr(sql,'output_safety_mode')=0
        AND instr(sql,'output_safety_exposure')=0
        AND instr(sql,'output_safety_billing')=0)=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=59
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=7)=59
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=155
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*59 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=7)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd071%')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd071_down_final_guard;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
