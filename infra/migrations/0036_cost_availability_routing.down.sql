-- PRD-047 回滚：恢复完整 0035 结构，并明确删除旧应用无法解释的动态路由候选和事件事实。
-- 数据边界：Audit 与 routing_decisions 采用过滤/保真重建，不能用普通 DELETE 绕过不可变触发器。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 旧版本不认识 route.policy_selected；先删 attempts，再同时清理 original/replay delivery。
DELETE FROM webhook_delivery_attempts
WHERE webhook_delivery_id IN (
  SELECT webhook_delivery_id
  FROM webhook_deliveries
  WHERE event_type='route.policy_selected'
);
-- attempts 清理完成后删除该事件的 original/replay 主记录，避免留下旧应用未知事件。
DELETE FROM webhook_deliveries
WHERE event_type='route.policy_selected';

-- 使用 SQLite JSON API 按原数组顺序移除订阅值；只订阅该事件的 Endpoint 必须得到合法 []。
UPDATE webhook_endpoints
SET event_types_json=COALESCE(
  (
    SELECT json_group_array(value)
    FROM (
      SELECT item.value AS value
      FROM json_each(webhook_endpoints.event_types_json) AS item
      WHERE item.value!='route.policy_selected'
      ORDER BY item.key
    )
  ),
  '[]'
)
WHERE EXISTS (
  SELECT 1
  FROM json_each(webhook_endpoints.event_types_json) AS item
  WHERE item.value='route.policy_selected'
);

-- 恢复 0035 预留结构；原始 reserved_cost_cny 和所有历史行均保留，不删除或重新估算业务事实。
ALTER TABLE budget_reservations DROP COLUMN outstanding_cost_cny;

-- 恢复 0035 audit CHECK，只过滤 PRD-047 的两类 RoutingPolicy 管理事实；其余字段基于已清洗事实逐值保留。
-- Provider 历史摘要在 up 阶段移除的可逆 base_url 属于安全删除，down 不得重新构造或恢复。
DROP TABLE IF EXISTS audit_logs_prd047_old;
CREATE TABLE audit_logs_prd047_old (
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
      'policy.dry_run_executed'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation'
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

-- 将所有非 RoutingPolicy 审计按 17 列原值复制，完整保留可空客户端元数据。
INSERT INTO audit_logs_prd047_old(
  audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
)
SELECT
  audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
FROM audit_logs
WHERE action NOT IN ('routing_policy.created', 'routing_policy.updated');

-- 过滤复制后创建计数守卫，先证明预期保留行与目标表完全一致。
DROP TABLE IF EXISTS prd047_audit_down_guard;
CREATE TABLE prd047_audit_down_guard (
  expected_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (expected_count=copied_count)
);
-- 守卫同时计算过滤后的源行数和目标行数，任何差异都会回滚 down。
INSERT INTO prd047_audit_down_guard(expected_count, copied_count)
SELECT
  (SELECT COUNT(*) FROM audit_logs
   WHERE action NOT IN ('routing_policy.created', 'routing_policy.updated')),
  (SELECT COUNT(*) FROM audit_logs_prd047_old);
DROP TABLE prd047_audit_down_guard;

DROP TABLE audit_logs;
-- 计数通过后恢复 0035 正式审计表名，后续再重建原索引与不可变 trigger。
ALTER TABLE audit_logs_prd047_old RENAME TO audit_logs;

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

CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u
  WHERE u.user_id=NEW.actor_user_id AND u.tenant_id=NEW.tenant_id
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

-- 先解除 Candidate/Event 的不可变保护，再按子到父顺序移除 PRD-047 专属事实。
DROP TRIGGER IF EXISTS trg_routing_decisions_prd047_candidate_bound_update;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd047_immutable_delete;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd047_immutable_update;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd047_graph_insert;
-- Candidate 查询顺序索引只服务 0036 强类型详情，删除表前先显式撤销。
DROP INDEX IF EXISTS idx_routing_candidates_decision_order;
-- rank 部分唯一索引依赖 Candidate 表，必须在表删除之前移除。
DROP INDEX IF EXISTS idx_routing_candidates_decision_rank_unique;
-- 候选属于 0036 请求级新事实，0035 无法解释，因此回滚时完整删除。
DROP TABLE IF EXISTS routing_candidates;

DROP TRIGGER IF EXISTS trg_routing_policy_events_prd047_immutable_delete;
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd047_immutable_update;
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd047_graph_insert;
-- 数组白名单与规范顺序触发器依赖 0036 事件表，删除表前先显式撤销。
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd047_payload_insert;
-- 配置事件的版本查询索引随新事件表一起撤销，不能残留悬空 schema 对象。
DROP INDEX IF EXISTS idx_routing_policy_events_policy_version;
-- 租户时间索引同样只属于 0036 配置事件读取路径。
DROP INDEX IF EXISTS idx_routing_policy_events_tenant_time;
-- 0035 没有稳定 RoutingPolicy 事件身份，回滚时删除该专属事实表。
DROP TABLE IF EXISTS routing_policy_events;

-- routing_decisions 恢复 0035 的 18 列；动态请求保留基础选路摘要，但删除 0036 冻结扩展字段。
DROP TRIGGER IF EXISTS trg_routing_decisions_prd047_owner_update;
DROP TRIGGER IF EXISTS trg_routing_decisions_prd047_owner_insert;
-- Policy-bound request 的部分唯一索引仅属于 0036，重建旧表前显式撤销，避免 down 遗留。
DROP INDEX IF EXISTS idx_routing_decisions_policy_request_unique;
-- 重建前清理上次失败可能留下的临时旧结构，保证 down 可重复安全执行。
DROP TABLE IF EXISTS routing_decisions_prd047_old;
CREATE TABLE routing_decisions_prd047_old (
  routing_decision_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  model_alias_id TEXT,
  requested_model TEXT NOT NULL,
  environment TEXT NOT NULL,
  selected_model_id TEXT,
  selected_internal_model_id TEXT,
  provider_id TEXT,
  provider_code TEXT,
  decision_status TEXT NOT NULL CHECK (decision_status IN ('selected', 'failed')),
  decision_reason TEXT NOT NULL,
  candidate_summary TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  created_at TEXT NOT NULL,
  environment_id TEXT REFERENCES environments(environment_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (model_alias_id) REFERENCES model_aliases(model_alias_id),
  FOREIGN KEY (selected_model_id) REFERENCES models(model_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);

-- 把 0036 Decision 的 18 个 0035 公共字段逐列复制，丢弃仅新版本认识的冻结扩展。
INSERT INTO routing_decisions_prd047_old(
  routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
  model_alias_id, requested_model, environment, selected_model_id,
  selected_internal_model_id, provider_id, provider_code, decision_status,
  decision_reason, candidate_summary, error_code, created_at, environment_id
)
SELECT
  routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
  model_alias_id, requested_model, environment, selected_model_id,
  selected_internal_model_id, provider_id, provider_code, decision_status,
  decision_reason, candidate_summary, error_code, created_at, environment_id
FROM routing_decisions;

-- Decision 降版复制建立独立计数守卫，避免字段重建过程中静默丢失历史请求。
DROP TABLE IF EXISTS prd047_decision_down_guard;
CREATE TABLE prd047_decision_down_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 将新旧 Decision 行数写入守卫，只有完全一致才允许替换正式表。
INSERT INTO prd047_decision_down_guard(source_count, copied_count)
SELECT
  (SELECT COUNT(*) FROM routing_decisions),
  (SELECT COUNT(*) FROM routing_decisions_prd047_old);
DROP TABLE prd047_decision_down_guard;

DROP TABLE routing_decisions;
-- 复制守卫通过后恢复 0035 表名，基础选路历史继续可查。
ALTER TABLE routing_decisions_prd047_old RENAME TO routing_decisions;

CREATE INDEX idx_routing_decisions_request_id
  ON routing_decisions(tenant_id, request_id);
CREATE INDEX idx_routing_decisions_alias_time
  ON routing_decisions(tenant_id, model_alias_id, created_at);
CREATE INDEX idx_routing_decisions_error_code
  ON routing_decisions(tenant_id, error_code);
CREATE INDEX idx_routing_decisions_environment_time
  ON routing_decisions(tenant_id, environment_id, created_at DESC);

-- Alias/Model owner trigger 和自动版本 trigger 属于 0036，必须在删除 Policy 前显式撤销。
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_policy_update;
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_policy_insert;
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_identity_update;
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_owner_update;
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_owner_insert;
DROP TRIGGER IF EXISTS trg_models_prd047_owner_update;
DROP TRIGGER IF EXISTS trg_models_prd047_owner_insert;

DROP TRIGGER IF EXISTS trg_routing_policies_prd047_delete;
DROP TRIGGER IF EXISTS trg_routing_policies_prd047_version_update;
DROP TRIGGER IF EXISTS trg_routing_policies_prd047_identity_update;
-- 模式查询索引依赖 RoutingPolicy 新表，删除父表之前必须先撤销。
DROP INDEX IF EXISTS idx_routing_policies_tenant_mode;
-- 0035 不认识 RoutingPolicy 对象，所有 Candidate/Event 已删除后再移除父表。
DROP TABLE IF EXISTS model_alias_routing_policies;

-- 只有所有 0036 复合子外键都已移除，才能撤销本迁移创建的父候选键。
DROP INDEX IF EXISTS uq_providers_tenant_id_prd047;
-- Alias 复合唯一键只为 0036 子外键提供父候选，降版后不再保留。
DROP INDEX IF EXISTS uq_model_aliases_tenant_id_prd047;
-- Environment 复合唯一键同样随 0036 owner 图一起撤销。
DROP INDEX IF EXISTS uq_environments_tenant_environment_prd047;

-- down 结束前验证恢复后的 0035 结构仍无 FK 或完整性问题。
DROP TABLE IF EXISTS prd047_down_integrity_guard;
CREATE TABLE prd047_down_integrity_guard (
  foreign_key_issue_count INTEGER NOT NULL CHECK (foreign_key_issue_count=0),
  integrity_issue_count INTEGER NOT NULL CHECK (integrity_issue_count=0)
);
-- 恢复提交前同时验证外键和 SQLite 完整性，任何非零问题都会原子回滚 down。
INSERT INTO prd047_down_integrity_guard(foreign_key_issue_count, integrity_issue_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check),
  (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd047_down_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
