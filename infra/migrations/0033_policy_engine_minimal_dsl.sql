-- 迁移职责：为 PRD-044 增加受限策略 DSL 的 Policy、Rule、Action 与追加事件事实。
-- 安全边界：所有关联都以 tenant_id 参与复合外键；表达式只作为受限 DSL 正文保存，不能被数据库执行。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- users.user_id 虽为全局主键，但策略表必须把“用户属于租户”编码为复合外键，而不是依赖应用层约定。
CREATE UNIQUE INDEX uq_users_tenant_user_prd044
  ON users(tenant_id, user_id);
-- route_to 的模型目标同样使用 tenant_id + model_id 作为父键，阻止内部 SQL 拼出跨租户关系。
CREATE UNIQUE INDEX uq_models_tenant_model_prd044
  ON models(tenant_id, model_id);

-- 策略主表只保存控制面可编辑元数据；active 表示下一快照候选，不代表已经发布到 Gateway。
CREATE TABLE policies (
  policy_id TEXT PRIMARY KEY CHECK (
    length(policy_id) BETWEEN 5 AND 128 AND policy_id GLOB 'pol_*'
  ),
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT CHECK (description IS NULL OR length(description) <= 500),
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  priority INTEGER NOT NULL CHECK (
    typeof(priority) = 'integer' AND priority BETWEEN 1 AND 10000
  ),
  version INTEGER NOT NULL DEFAULT 1 CHECK (
    typeof(version) = 'integer' AND version >= 1
  ),
  dsl_version TEXT NOT NULL DEFAULT 'policy-dsl-v1' CHECK (dsl_version = 'policy-dsl-v1'),
  created_by TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, updated_by) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, name),
  UNIQUE (tenant_id, policy_id)
);

-- 数据面快照候选按租户、启停状态、priority 和稳定 ID 读取，不能扫描其他租户策略。
CREATE INDEX idx_policies_tenant_status_priority
  ON policies(tenant_id, status, priority ASC, policy_id ASC);
-- 控制台列表按最近更新时间分页，额外带 policy_id 解决同一时间戳的确定性顺序。
CREATE INDEX idx_policies_tenant_updated
  ON policies(tenant_id, updated_at DESC, policy_id DESC);

-- 显式 owner 触发器提供稳定诊断；即使调用方绕过服务层，也不能把其他租户用户写成操作者。
CREATE TRIGGER trg_policies_owner_insert
BEFORE INSERT ON policies
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id = NEW.tenant_id AND u.user_id = NEW.created_by
  )
  OR NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id = NEW.tenant_id AND u.user_id = NEW.updated_by
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_owner_mismatch');
END;

-- PATCH 可以更换 updated_by，但新操作者仍必须属于策略租户；created_by 由身份冻结触发器保护。
CREATE TRIGGER trg_policies_owner_update
BEFORE UPDATE OF created_by, updated_by, tenant_id ON policies
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id = NEW.tenant_id AND u.user_id = NEW.created_by
  )
  OR NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id = NEW.tenant_id AND u.user_id = NEW.updated_by
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_owner_mismatch');
END;

-- 策略身份、租户、DSL 解释版本和创建事实一经生成不得改写，避免历史审计与未来快照失去锚点。
CREATE TRIGGER trg_policies_identity_immutable
BEFORE UPDATE OF policy_id, tenant_id, dsl_version, created_by, created_at ON policies
FOR EACH ROW
WHEN
  NEW.policy_id IS NOT OLD.policy_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.dsl_version IS NOT OLD.dsl_version
  OR NEW.created_by IS NOT OLD.created_by
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'policy_identity_immutable');
END;

-- 每次 UPDATE 都必须精确推进一个版本；禁止跳号、回退或不增版本造成并发覆盖与快照漂移。
CREATE TRIGGER trg_policies_version_increment
BEFORE UPDATE ON policies
FOR EACH ROW
WHEN typeof(NEW.version) != 'integer' OR NEW.version != OLD.version + 1
BEGIN
  SELECT RAISE(ABORT, 'policy_version_increment_invalid');
END;

-- v1 每条策略恰好由服务事务写入一条规则；数据库 UNIQUE 负责阻止第二条规则污染最小版语义。
CREATE TABLE policy_rules (
  policy_rule_id TEXT PRIMARY KEY CHECK (
    length(policy_rule_id) BETWEEN 7 AND 128 AND policy_rule_id GLOB 'prule_*'
  ),
  tenant_id TEXT NOT NULL,
  policy_id TEXT NOT NULL,
  condition_expr TEXT NOT NULL CHECK (
    length(condition_expr) BETWEEN 1 AND 1024
    AND length(CAST(condition_expr AS BLOB)) <= 2048
    AND instr(condition_expr, char(0)) = 0
  ),
  condition_ast_json TEXT NOT NULL CHECK (
    json_valid(condition_ast_json)
    AND json_type(condition_ast_json) = 'object'
    AND length(CAST(condition_ast_json AS BLOB)) <= 32768
  ),
  condition_hash TEXT NOT NULL CHECK (
    length(condition_hash) = 64 AND condition_hash NOT GLOB '*[^0-9a-f]*'
  ),
  variables_json TEXT NOT NULL CHECK (
    json_valid(variables_json)
    AND json_type(variables_json) = 'array'
    AND json_array_length(variables_json) <= 6
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, policy_id) REFERENCES policies(tenant_id, policy_id),
  UNIQUE (tenant_id, policy_id)
);

-- 父策略必须在同一租户；显式触发器让归属错误不退化为难以诊断的通用 FK 文本。
CREATE TRIGGER trg_policy_rules_owner_insert
BEFORE INSERT ON policy_rules
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM policies p
  WHERE p.tenant_id = NEW.tenant_id AND p.policy_id = NEW.policy_id
)
BEGIN
  SELECT RAISE(ABORT, 'policy_rule_owner_mismatch');
END;

CREATE TRIGGER trg_policy_rules_owner_update
BEFORE UPDATE OF tenant_id, policy_id ON policy_rules
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM policies p
  WHERE p.tenant_id = NEW.tenant_id AND p.policy_id = NEW.policy_id
)
BEGIN
  SELECT RAISE(ABORT, 'policy_rule_owner_mismatch');
END;

-- Rule ID、父策略和创建时间构成稳定身份；更新只能替换已重新编译的正文、AST、hash 与变量摘要。
CREATE TRIGGER trg_policy_rules_identity_immutable
BEFORE UPDATE OF policy_rule_id, tenant_id, policy_id, created_at ON policy_rules
FOR EACH ROW
WHEN
  NEW.policy_rule_id IS NOT OLD.policy_rule_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.policy_id IS NOT OLD.policy_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'policy_rule_identity_immutable');
END;

-- v1 每条策略只能有一个动作；target_model_id 仅在 route_to 时出现，避免悬空参数产生歧义。
CREATE TABLE policy_actions (
  policy_action_id TEXT PRIMARY KEY CHECK (
    length(policy_action_id) BETWEEN 6 AND 128 AND policy_action_id GLOB 'pact_*'
  ),
  tenant_id TEXT NOT NULL,
  policy_id TEXT NOT NULL,
  action_type TEXT NOT NULL CHECK (
    action_type IN ('allow', 'deny', 'mask', 'log_only', 'route_to')
  ),
  target_model_id TEXT,
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, policy_id) REFERENCES policies(tenant_id, policy_id),
  FOREIGN KEY (tenant_id, target_model_id) REFERENCES models(tenant_id, model_id),
  UNIQUE (tenant_id, policy_id),
  CHECK (
    (action_type = 'route_to' AND target_model_id IS NOT NULL AND length(target_model_id) BETWEEN 1 AND 128)
    OR (action_type != 'route_to' AND target_model_id IS NULL)
  )
);

-- Action 的父 Policy 必须属于同一租户，防止全局 policy_id 被误当成租户归属证明。
CREATE TRIGGER trg_policy_actions_owner_insert
BEFORE INSERT ON policy_actions
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM policies p
  WHERE p.tenant_id = NEW.tenant_id AND p.policy_id = NEW.policy_id
)
BEGIN
  SELECT RAISE(ABORT, 'policy_action_owner_mismatch');
END;

CREATE TRIGGER trg_policy_actions_owner_update
BEFORE UPDATE OF tenant_id, policy_id ON policy_actions
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM policies p
  WHERE p.tenant_id = NEW.tenant_id AND p.policy_id = NEW.policy_id
)
BEGIN
  SELECT RAISE(ABORT, 'policy_action_owner_mismatch');
END;

-- route_to 保存时再次检查“当前租户且 active”，服务层的事务校验不能替代数据库最终防线。
CREATE TRIGGER trg_policy_actions_target_active_insert
BEFORE INSERT ON policy_actions
FOR EACH ROW
WHEN NEW.action_type = 'route_to' AND NOT EXISTS (
  SELECT 1 FROM models m
  WHERE m.tenant_id = NEW.tenant_id
    AND m.model_id = NEW.target_model_id
    AND m.status = 'active'
)
BEGIN
  SELECT RAISE(ABORT, 'policy_action_target_invalid');
END;

CREATE TRIGGER trg_policy_actions_target_active_update
BEFORE UPDATE OF action_type, target_model_id, tenant_id ON policy_actions
FOR EACH ROW
WHEN NEW.action_type = 'route_to' AND NOT EXISTS (
  SELECT 1 FROM models m
  WHERE m.tenant_id = NEW.tenant_id
    AND m.model_id = NEW.target_model_id
    AND m.status = 'active'
)
BEGIN
  SELECT RAISE(ABORT, 'policy_action_target_invalid');
END;

-- Action 身份与父策略不可迁移；PATCH 只能在同一行替换动作语义并更新时间。
CREATE TRIGGER trg_policy_actions_identity_immutable
BEFORE UPDATE OF policy_action_id, tenant_id, policy_id, created_at ON policy_actions
FOR EACH ROW
WHEN
  NEW.policy_action_id IS NOT OLD.policy_action_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.policy_id IS NOT OLD.policy_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'policy_action_identity_immutable');
END;

-- 策略事件只保存可安全外发的摘要，不保存 DSL 正文、AST、示例上下文或底层异常。
CREATE TABLE policy_events (
  policy_event_id TEXT PRIMARY KEY CHECK (
    length(policy_event_id) BETWEEN 6 AND 128 AND policy_event_id GLOB 'pevt_*'
  ),
  tenant_id TEXT NOT NULL,
  policy_id TEXT,
  event_type TEXT NOT NULL CHECK (
    event_type IN ('policy.created', 'policy.validation_failed')
  ),
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  reason_code TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 128),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json)
    AND json_type(payload_json) = 'object'
    AND length(CAST(payload_json AS BLOB)) <= 8192
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, policy_id) REFERENCES policies(tenant_id, policy_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  CHECK (
    (event_type = 'policy.created'
      AND policy_id IS NOT NULL
      AND reason_code = 'policy.created')
    OR
    (event_type = 'policy.validation_failed'
      AND reason_code IN (
        'policy.expression_invalid',
        'policy.action_invalid',
        'policy.route_target_invalid',
        'policy.context_invalid',
        'policy.evaluation_timeout'
      ))
  ),
  CHECK (
    event_type != 'policy.validation_failed'
    OR json_type(payload_json, '$.diagnostic_codes') IS NULL
    OR (
      json_type(payload_json, '$.diagnostic_codes') = 'array'
      AND json_array_length(payload_json, '$.diagnostic_codes') <= 8
    )
  )
);

-- 创建事件按 tenant+policy 唯一，保证重试不会重复发布 policy.created Webhook。
CREATE UNIQUE INDEX idx_policy_events_created_unique
  ON policy_events(tenant_id, policy_id)
  WHERE event_type = 'policy.created';
-- 校验失败只按 tenant+actor+request 去重副作用，API 仍需重新计算确定性 diagnostics。
CREATE UNIQUE INDEX idx_policy_events_validation_request_unique
  ON policy_events(tenant_id, actor_user_id, request_id)
  WHERE event_type = 'policy.validation_failed';
-- 事件查询必须先按租户收敛，再按稳定时间与 ID 排序或关联请求链路。
CREATE INDEX idx_policy_events_tenant_time
  ON policy_events(tenant_id, created_at DESC, policy_event_id DESC);
CREATE INDEX idx_policy_events_tenant_request
  ON policy_events(tenant_id, request_id);

-- actor 与可选 Policy 都必须属于事件 tenant；新草稿校验失败只能把 policy_id 留空，不能伪造外部 ID。
CREATE TRIGGER trg_policy_events_owner_insert
BEFORE INSERT ON policy_events
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id = NEW.tenant_id AND u.user_id = NEW.actor_user_id
  )
  OR (
    NEW.policy_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM policies p
      WHERE p.tenant_id = NEW.tenant_id AND p.policy_id = NEW.policy_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_event_owner_mismatch');
END;

-- 事件是追加事实；任何 UPDATE/DELETE 都会破坏审计与 Webhook 原始事件去重，因此一律拒绝。
CREATE TRIGGER trg_policy_events_immutable_update
BEFORE UPDATE ON policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_event_immutable');
END;
CREATE TRIGGER trg_policy_events_immutable_delete
BEFORE DELETE ON policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_event_immutable');
END;

-- audit_logs 带动作和对象 CHECK；新增 policy 审计必须按 0032 全字段重建，不能丢失 Usage/Webhook 历史。
DROP TABLE IF EXISTS audit_logs_prd044_new;
CREATE TABLE audit_logs_prd044_new (
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
      'policy.created', 'policy.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy'
    )
  ),
  object_id TEXT NOT NULL,
  object_name TEXT,
  before_summary_json TEXT NOT NULL CHECK (
    json_valid(before_summary_json) AND json_type(before_summary_json) = 'object'
  ),
  after_summary_json TEXT NOT NULL CHECK (
    json_valid(after_summary_json) AND json_type(after_summary_json) = 'object'
  ),
  changed_fields_json TEXT NOT NULL CHECK (
    json_valid(changed_fields_json) AND json_type(changed_fields_json) = 'array'
  ),
  client_ip TEXT,
  user_agent TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- 逐字段复制 0032 全部审计事实，身份快照、摘要、来源和 request_id/trace_id 必须完全不变。
INSERT INTO audit_logs_prd044_new(
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
FROM audit_logs;

DROP TABLE audit_logs;
-- 恢复稳定表名和 0032 的五个查询索引、owner 与两条不可变触发器，业务服务无需感知表重建。
ALTER TABLE audit_logs_prd044_new RENAME TO audit_logs;
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
CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u
  WHERE u.user_id = NEW.actor_user_id AND u.tenant_id = NEW.tenant_id
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

COMMIT;
PRAGMA foreign_keys = ON;
