-- 迁移说明：0017_webhooks 为 PRD-025 增加基础 Webhook 订阅、投递和重试记录。
-- 创建 webhook_endpoints 表：保存租户级 Endpoint 配置，secret 只保存密文、前缀和指纹。
CREATE TABLE IF NOT EXISTS webhook_endpoints (
  webhook_endpoint_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL,
  endpoint_url TEXT NOT NULL,
  event_types_json TEXT NOT NULL,
  encrypted_secret TEXT NOT NULL,
  secret_prefix TEXT NOT NULL,
  secret_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  timeout_ms INTEGER NOT NULL DEFAULT 5000 CHECK (timeout_ms >= 1000 AND timeout_ms <= 30000),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1 AND max_attempts <= 3),
  last_delivery_status TEXT,
  last_delivery_at TEXT,
  last_success_at TEXT,
  last_failure_at TEXT,
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id)
);

-- 创建 webhook_deliveries 表：保存每个事件对每个 Endpoint 的最终投递状态和安全 payload。
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  webhook_delivery_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  webhook_endpoint_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_timestamp TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1 AND max_attempts <= 3),
  http_status INTEGER,
  duration_ms INTEGER,
  error_code TEXT,
  error_summary TEXT,
  next_retry_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (webhook_endpoint_id) REFERENCES webhook_endpoints(webhook_endpoint_id),
  UNIQUE (tenant_id, webhook_endpoint_id, event_id)
);

-- 创建 webhook_delivery_attempts 表：逐次记录发送结果，满足失败重试可追踪要求。
CREATE TABLE IF NOT EXISTS webhook_delivery_attempts (
  webhook_delivery_attempt_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  webhook_delivery_id TEXT NOT NULL,
  webhook_endpoint_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
  status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
  http_status INTEGER,
  duration_ms INTEGER,
  error_code TEXT,
  error_summary TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (webhook_delivery_id) REFERENCES webhook_deliveries(webhook_delivery_id),
  FOREIGN KEY (webhook_endpoint_id) REFERENCES webhook_endpoints(webhook_endpoint_id),
  UNIQUE (tenant_id, webhook_delivery_id, attempt_no)
);

-- 创建 idx_webhook_endpoints_tenant_status 索引：支撑控制台按租户和状态筛选 Endpoint。
CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_tenant_status
  ON webhook_endpoints(tenant_id, status);

-- 创建 idx_webhook_endpoints_tenant_secret 索引：支撑按指纹排障，避免查询 secret 明文或密文。
CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_tenant_secret
  ON webhook_endpoints(tenant_id, secret_fingerprint);

-- 创建 idx_webhook_deliveries_endpoint_time 索引：支撑发送记录页读取某个 Endpoint 的最近投递。
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint_time
  ON webhook_deliveries(tenant_id, webhook_endpoint_id, created_at DESC);

-- 创建 idx_webhook_deliveries_event 索引：支撑按 event_id 回溯一次治理事件的所有投递。
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event
  ON webhook_deliveries(tenant_id, event_id);

-- 创建 idx_webhook_deliveries_status_time 索引：支撑后续失败率统计和运维排障。
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status_time
  ON webhook_deliveries(tenant_id, status, created_at DESC);

-- 创建 idx_webhook_attempts_delivery 索引：支撑发送记录详情按顺序展示每次尝试。
CREATE INDEX IF NOT EXISTS idx_webhook_attempts_delivery
  ON webhook_delivery_attempts(tenant_id, webhook_delivery_id, attempt_no);
