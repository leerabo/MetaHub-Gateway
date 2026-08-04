-- PRD-036：为非流式主备切换增加策略快照、逐次上游尝试和调用日志汇总。

-- Fallback 策略只保存启停和固定条件；主备模型仍由 model_aliases 统一持有，避免模型 ID 双写漂移。
CREATE TABLE IF NOT EXISTS fallback_policies (
  fallback_policy_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('enabled', 'disabled')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  priority INTEGER NOT NULL DEFAULT 100 CHECK (priority BETWEEN 1 AND 1000),
  condition_expr TEXT NOT NULL,
  action TEXT NOT NULL,
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (model_alias_id) REFERENCES model_aliases(model_alias_id),
  UNIQUE (tenant_id, model_alias_id)
);

-- 触发器在外键之外校验策略和别名属于同一租户，防止直接 SQL 绕过服务层租户隔离。
CREATE TRIGGER IF NOT EXISTS trg_fallback_policy_tenant_insert
BEFORE INSERT ON fallback_policies
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM model_aliases a
    WHERE a.model_alias_id=NEW.model_alias_id AND a.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'fallback_policy_tenant_mismatch') END;
END;

-- 更新策略归属时执行相同校验，避免后续维护脚本把策略移动到其他租户。
CREATE TRIGGER IF NOT EXISTS trg_fallback_policy_tenant_update
BEFORE UPDATE OF tenant_id, model_alias_id ON fallback_policies
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM model_aliases a
    WHERE a.model_alias_id=NEW.model_alias_id AND a.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'fallback_policy_tenant_mismatch') END;
END;

-- 按租户和启停状态读取策略，支撑模型别名列表和 Gateway 热路径查询。
CREATE INDEX IF NOT EXISTS idx_fallback_policies_tenant_status
  ON fallback_policies(tenant_id, status);

-- 既有主备别名升级为标准 enabled 策略，使升级后行为与 PRD-036 的主流程一致。
INSERT INTO fallback_policies(
  fallback_policy_id, tenant_id, model_alias_id, status, version, priority,
  condition_expr, action, created_by, updated_by, created_at, updated_at
)
SELECT
  'fbp_migrated_' || a.model_alias_id,
  a.tenant_id,
  a.model_alias_id,
  'enabled',
  1,
  100,
  '["upstream_timeout","upstream_rate_limited","upstream_5xx"]',
  '{"type":"use_alias_fallback","max_attempts":1}',
  a.created_by,
  a.created_by,
  a.created_at,
  a.updated_at
FROM model_aliases a
WHERE a.fallback_model_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM fallback_policies p
    WHERE p.tenant_id=a.tenant_id AND p.model_alias_id=a.model_alias_id
  );

-- 每一行只描述一次实际发出的上游调用，不保存请求正文、响应正文、Header 或 Provider Key。
CREATE TABLE IF NOT EXISTS routing_attempts (
  routing_attempt_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  model_alias_id TEXT,
  fallback_policy_id TEXT,
  fallback_policy_version INTEGER CHECK (fallback_policy_version IS NULL OR fallback_policy_version >= 1),
  attempt_sequence INTEGER NOT NULL CHECK (attempt_sequence IN (1, 2)),
  attempt_role TEXT NOT NULL CHECK (attempt_role IN ('primary', 'fallback')),
  trigger_type TEXT NOT NULL CHECK (trigger_type IN ('initial', 'fallback')),
  trigger_error_code TEXT,
  model_id TEXT NOT NULL,
  internal_model_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_code TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
  error_code TEXT,
  error_category TEXT CHECK (error_category IS NULL OR error_category IN ('timeout', 'rate_limited', 'upstream_5xx', 'other')),
  upstream_http_status INTEGER CHECK (upstream_http_status IS NULL OR upstream_http_status BETWEEN 100 AND 599),
  retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
  latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
  usage_status TEXT NOT NULL DEFAULT 'pending' CHECK (usage_status IN ('pending', 'recorded', 'unavailable', 'not_applicable')),
  usage_ledger_id TEXT,
  estimated_cost_cny NUMERIC CHECK (estimated_cost_cny IS NULL OR estimated_cost_cny >= 0),
  created_at TEXT NOT NULL,
  completed_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (model_alias_id) REFERENCES model_aliases(model_alias_id),
  FOREIGN KEY (fallback_policy_id) REFERENCES fallback_policies(fallback_policy_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id),
  UNIQUE (tenant_id, request_id, attempt_sequence)
);

-- 尝试写入必须同时验证模型、Provider、别名和策略的租户归属，禁止跨租户拼接路由证据。
CREATE TRIGGER IF NOT EXISTS trg_routing_attempt_tenant_insert
BEFORE INSERT ON routing_attempts
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.model_id=NEW.model_id AND m.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'routing_attempt_model_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM providers p
    WHERE p.provider_id=NEW.provider_id AND p.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'routing_attempt_provider_tenant_mismatch') END;
  SELECT CASE WHEN NEW.model_alias_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_aliases a
    WHERE a.model_alias_id=NEW.model_alias_id AND a.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'routing_attempt_alias_tenant_mismatch') END;
  SELECT CASE WHEN NEW.fallback_policy_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM fallback_policies fp
    WHERE fp.fallback_policy_id=NEW.fallback_policy_id AND fp.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'routing_attempt_policy_tenant_mismatch') END;
END;

-- request_id 与 sequence 唯一键是重试风暴闸门，索引同时支撑日志详情按顺序读取。
CREATE INDEX IF NOT EXISTS idx_routing_attempts_tenant_request
  ON routing_attempts(tenant_id, request_id, attempt_sequence);

-- 按模型别名回看近期切换记录，便于管理员定位某条策略的稳定性问题。
CREATE INDEX IF NOT EXISTS idx_routing_attempts_tenant_alias_time
  ON routing_attempts(tenant_id, model_alias_id, created_at DESC);

-- started 状态可用于识别进程中止留下的结果未知尝试。
CREATE INDEX IF NOT EXISTS idx_routing_attempts_tenant_status_time
  ON routing_attempts(tenant_id, status, created_at DESC);

-- 调用日志主表保存 Fallback 汇总，列表无需扫描 attempts 即可展示最终结果。
ALTER TABLE request_logs
  ADD COLUMN fallback_triggered INTEGER NOT NULL DEFAULT 0 CHECK (fallback_triggered IN (0, 1));

-- 单个客户端请求最多创建主、备两次尝试。
ALTER TABLE request_logs
  ADD COLUMN routing_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (routing_attempt_count BETWEEN 0 AND 2);

-- 最终尝试使用逻辑引用；SQLite ALTER COLUMN 无法直接补复合租户外键，后续触发器负责校验。
ALTER TABLE request_logs
  ADD COLUMN final_routing_attempt_id TEXT;

-- INSERT 若直接携带最终尝试，必须同时匹配 tenant_id 和 request_id。
CREATE TRIGGER IF NOT EXISTS trg_request_log_final_attempt_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN NEW.final_routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.final_routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
  ) THEN RAISE(ABORT, 'request_log_final_attempt_mismatch') END;
END;

-- request_logs upsert 会在请求终态回写最终尝试，因此 UPDATE 也必须执行租户与请求校验。
CREATE TRIGGER IF NOT EXISTS trg_request_log_final_attempt_update
BEFORE UPDATE OF final_routing_attempt_id, tenant_id, request_id ON request_logs
FOR EACH ROW
WHEN NEW.final_routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.final_routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
  ) THEN RAISE(ABORT, 'request_log_final_attempt_mismatch') END;
END;

-- 成功 Usage 关联到唯一成功尝试，使日志详情可以解释最终模型的 Token 和成本。
ALTER TABLE usage_ledger
  ADD COLUMN routing_attempt_id TEXT;

-- Usage 只能关联同租户、同 request_id 且成功的尝试，防止账本挂到失败或其他租户调用上。
CREATE TRIGGER IF NOT EXISTS trg_usage_routing_attempt_insert
BEFORE INSERT ON usage_ledger
FOR EACH ROW
WHEN NEW.routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
      AND ra.status='succeeded'
  ) THEN RAISE(ABORT, 'usage_routing_attempt_mismatch') END;
END;

-- 从一次尝试回查其正式 Usage，支撑 attempt 时间线的成本状态展示。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_routing_attempt
  ON usage_ledger(tenant_id, routing_attempt_id);
