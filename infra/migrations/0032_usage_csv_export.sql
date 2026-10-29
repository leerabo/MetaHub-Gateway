-- 迁移职责：为 PRD-043 增加租户级 Usage CSV 导出任务、完成事件和严格导出审计动作。
-- 任务制品当前只允许保存为有界 SQLite BLOB；对象存储字段仅作后续兼容预留，不能写入本机路径。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- SQLite 隐式 rowid 在删除最大行后可能复用，不能作为异步导出的财务快照边界。
-- 独立 AUTOINCREMENT 序列只增不复用，并用账本 ID 唯一映射保证每条 Usage 只有一个稳定水位。
CREATE TABLE usage_export_ledger_sequences (
  usage_export_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  usage_ledger_id TEXT NOT NULL UNIQUE,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (usage_ledger_id) REFERENCES usage_ledger(usage_ledger_id)
);

-- 导出始终按 tenant_id 读取最大序列并联表筛选，复合索引避免扫描其他租户的映射。
CREATE INDEX idx_usage_export_ledger_sequences_tenant_sequence
  ON usage_export_ledger_sequences(tenant_id, usage_export_sequence);

-- 先按业务时间和稳定 ID 回填历史账本，使升级前数据也获得确定且可复现的序列顺序。
INSERT INTO usage_export_ledger_sequences(tenant_id, usage_ledger_id)
SELECT tenant_id, usage_ledger_id
FROM usage_ledger
ORDER BY created_at ASC, usage_ledger_id ASC;

-- 映射租户必须与真实账本一致，禁止内部 SQL 构造跨租户序列关系。
CREATE TRIGGER trg_usage_export_ledger_sequences_owner_insert
BEFORE INSERT ON usage_export_ledger_sequences
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM usage_ledger ul
  WHERE ul.usage_ledger_id = NEW.usage_ledger_id AND ul.tenant_id = NEW.tenant_id
)
BEGIN
  SELECT RAISE(ABORT, 'usage_export_ledger_sequence_owner_mismatch');
END;

-- 序列和所属账本一经分配不得改写或删除，否则旧导出任务的水位含义会漂移。
CREATE TRIGGER trg_usage_export_ledger_sequences_immutable_update
BEFORE UPDATE ON usage_export_ledger_sequences
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_export_ledger_sequence_immutable');
END;
CREATE TRIGGER trg_usage_export_ledger_sequences_immutable_delete
BEFORE DELETE ON usage_export_ledger_sequences
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_export_ledger_sequence_immutable');
END;

-- 财务账本采用追加事实语义；更正应写入新的调整事实，不能原地替换水位内的行。
CREATE TRIGGER trg_usage_ledger_export_append_only_update
BEFORE UPDATE ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;
CREATE TRIGGER trg_usage_ledger_export_append_only_delete
BEFORE DELETE ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;

-- 新账本与序列在同一事务中写入；任何映射失败都会回滚账本 INSERT，避免出现无水位记录。
CREATE TRIGGER trg_usage_ledger_export_sequence_insert
AFTER INSERT ON usage_ledger
FOR EACH ROW
BEGIN
  -- 新账本映射必须与业务事实同事务写入，失败时由 SQLite 原子回滚两者。
  INSERT INTO usage_export_ledger_sequences(tenant_id, usage_ledger_id)
  VALUES(NEW.tenant_id, NEW.usage_ledger_id);
END;

-- 导出任务同时承载同步和异步结果。状态相关 CHECK 防止半成品 CSV 被下载或过期后仍保留正文。
CREATE TABLE usage_export_jobs (
  usage_export_job_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'expired')),
  generation_mode TEXT NOT NULL CHECK (generation_mode IN ('sync', 'async')),
  format TEXT NOT NULL DEFAULT 'csv' CHECK (format = 'csv'),
  filters_json TEXT NOT NULL CHECK (json_valid(filters_json) AND json_type(filters_json) = 'object'),
  filter_fingerprint TEXT NOT NULL CHECK (length(filter_fingerprint) = 64),
  snapshot_at TEXT NOT NULL,
  snapshot_ledger_sequence INTEGER NOT NULL CHECK (snapshot_ledger_sequence >= 0),
  row_count INTEGER NOT NULL CHECK (row_count >= 0),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
  total_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (total_cost_cny >= 0),
  total_cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (total_cost_usd >= 0),
  file_name TEXT NOT NULL CHECK (
    length(file_name) BETWEEN 1 AND 200
    AND file_name GLOB 'metahub-usage-*.csv'
    AND instr(file_name, '/') = 0 AND instr(file_name, char(92)) = 0
    AND instr(file_name, char(10)) = 0 AND instr(file_name, char(13)) = 0
  ),
  content_type TEXT NOT NULL DEFAULT 'text/csv' CHECK (content_type = 'text/csv'),
  csv_content BLOB,
  content_size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (content_size_bytes >= 0),
  content_sha256 TEXT CHECK (content_sha256 IS NULL OR length(content_sha256) = 64),
  storage_backend TEXT NOT NULL DEFAULT 'sqlite_blob' CHECK (storage_backend = 'sqlite_blob'),
  object_key TEXT CHECK (object_key IS NULL),
  download_count INTEGER NOT NULL DEFAULT 0 CHECK (download_count >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
  lease_token TEXT,
  lease_until TEXT,
  error_code TEXT CHECK (error_code IS NULL OR error_code = 'usage.export_unavailable'),
  requested_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (requested_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, request_id),
  UNIQUE (tenant_id, usage_export_job_id),
  CHECK (
    (status = 'pending'
      AND csv_content IS NULL AND content_size_bytes = 0 AND content_sha256 IS NULL
      AND lease_token IS NULL AND lease_until IS NULL
      AND completed_at IS NULL AND expires_at IS NULL)
    OR
    (status = 'processing'
      AND generation_mode = 'async'
      AND csv_content IS NULL AND content_size_bytes = 0 AND content_sha256 IS NULL
      AND attempt_count BETWEEN 1 AND 3
      AND lease_token IS NOT NULL AND lease_until IS NOT NULL AND started_at IS NOT NULL
      AND completed_at IS NULL AND expires_at IS NULL AND error_code IS NULL)
    OR
    (status = 'completed'
      AND csv_content IS NOT NULL AND content_size_bytes > 0
      AND length(csv_content) = content_size_bytes AND content_sha256 IS NOT NULL
      AND lease_token IS NULL AND lease_until IS NULL AND error_code IS NULL
      AND completed_at IS NOT NULL AND expires_at IS NOT NULL)
    OR
    (status = 'failed'
      AND generation_mode = 'async'
      AND csv_content IS NULL AND content_size_bytes = 0 AND content_sha256 IS NULL
      AND attempt_count BETWEEN 1 AND 3
      AND lease_token IS NULL AND lease_until IS NULL
      AND error_code IS NOT NULL AND completed_at IS NOT NULL AND expires_at IS NOT NULL)
    OR
    (status = 'expired'
      AND csv_content IS NULL AND object_key IS NULL
      AND lease_token IS NULL AND lease_until IS NULL AND expires_at IS NOT NULL)
  )
);

-- 当前租户按时间查看任务时不扫描其他租户；状态在索引中便于页面筛选。
CREATE INDEX idx_usage_export_jobs_tenant_time
  ON usage_export_jobs(tenant_id, created_at DESC, usage_export_job_id DESC);
-- worker 按最早请求领取 pending 或 lease 已过期 processing，索引覆盖状态、租约和稳定顺序。
CREATE INDEX idx_usage_export_jobs_pending_lease
  ON usage_export_jobs(status, lease_until, requested_at, usage_export_job_id);
-- 保留期清理只按终态和过期时间扫描，不读取 BLOB 正文。
CREATE INDEX idx_usage_export_jobs_expiry
  ON usage_export_jobs(status, expires_at, usage_export_job_id);

-- requested_by 必须真实属于任务 tenant，避免内部调用只凭全局 user_id 拼错租户。
CREATE TRIGGER trg_usage_export_jobs_owner_insert
BEFORE INSERT ON usage_export_jobs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u
  WHERE u.user_id = NEW.requested_by AND u.tenant_id = NEW.tenant_id
)
BEGIN
  SELECT RAISE(ABORT, 'usage_export_job_owner_mismatch');
END;

-- 任务的身份、筛选快照和对账基数创建后不可改写；worker 只能推进状态和制品字段。
CREATE TRIGGER trg_usage_export_jobs_identity_immutable
BEFORE UPDATE OF
  usage_export_job_id, tenant_id, requested_by, request_id, trace_id,
  generation_mode, format, filters_json, filter_fingerprint, snapshot_at, snapshot_ledger_sequence,
  row_count, total_tokens, total_cost_cny, total_cost_usd,
  file_name, content_type, storage_backend
ON usage_export_jobs
FOR EACH ROW
WHEN
  NEW.usage_export_job_id IS NOT OLD.usage_export_job_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.requested_by IS NOT OLD.requested_by
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.trace_id IS NOT OLD.trace_id
  OR NEW.generation_mode IS NOT OLD.generation_mode
  OR NEW.format IS NOT OLD.format
  OR NEW.filters_json IS NOT OLD.filters_json
  OR NEW.filter_fingerprint IS NOT OLD.filter_fingerprint
  OR NEW.snapshot_at IS NOT OLD.snapshot_at
  OR NEW.snapshot_ledger_sequence IS NOT OLD.snapshot_ledger_sequence
  OR NEW.row_count IS NOT OLD.row_count
  OR NEW.total_tokens IS NOT OLD.total_tokens
  OR NEW.total_cost_cny IS NOT OLD.total_cost_cny
  OR NEW.total_cost_usd IS NOT OLD.total_cost_usd
  OR NEW.file_name IS NOT OLD.file_name
  OR NEW.content_type IS NOT OLD.content_type
  OR NEW.storage_backend IS NOT OLD.storage_backend
BEGIN
  SELECT RAISE(ABORT, 'usage_export_job_identity_immutable');
END;

-- 状态机只允许领取、完成、可重试回退、最终失败和终态过期；同状态更新供下载计数与 lease 续期使用。
CREATE TRIGGER trg_usage_export_jobs_status_transition
BEFORE UPDATE OF status ON usage_export_jobs
FOR EACH ROW
WHEN NOT (
  NEW.status = OLD.status
  OR (OLD.status = 'pending' AND NEW.status = 'processing')
  OR (OLD.status = 'processing' AND NEW.status IN ('pending', 'completed', 'failed'))
  OR (OLD.status IN ('completed', 'failed') AND NEW.status = 'expired')
)
BEGIN
  SELECT RAISE(ABORT, 'usage_export_job_status_transition_invalid');
END;

-- 完成事件只保存安全汇总；CSV、完整筛选、名称、路径和密钥不属于事件表合同。
CREATE TABLE usage_export_events (
  usage_export_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  usage_export_job_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type = 'usage.exported'),
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (usage_export_job_id) REFERENCES usage_export_jobs(usage_export_job_id),
  FOREIGN KEY (actor_user_id) REFERENCES users(user_id),
  UNIQUE (tenant_id, usage_export_job_id, event_type)
);

-- 事件按租户和时间检索，支撑完成事实、Webhook 和 request_id 的交叉核对。
CREATE INDEX idx_usage_export_events_tenant_time
  ON usage_export_events(tenant_id, created_at DESC, usage_export_event_id DESC);
CREATE INDEX idx_usage_export_events_request
  ON usage_export_events(tenant_id, request_id);

-- job 与 actor 必须同时属于事件 tenant，单列外键不能表达该复合所有权约束。
CREATE TRIGGER trg_usage_export_events_owner_insert
BEFORE INSERT ON usage_export_events
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM usage_export_jobs j
    WHERE j.usage_export_job_id = NEW.usage_export_job_id AND j.tenant_id = NEW.tenant_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM users u
    WHERE u.user_id = NEW.actor_user_id AND u.tenant_id = NEW.tenant_id
  )
BEGIN
  SELECT RAISE(ABORT, 'usage_export_event_owner_mismatch');
END;

-- usage.exported 是追加完成事实，业务代码不能改写或删除后伪造新的完成结果。
CREATE TRIGGER trg_usage_export_events_immutable_update
BEFORE UPDATE ON usage_export_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_export_event_immutable');
END;
CREATE TRIGGER trg_usage_export_events_immutable_delete
BEFORE DELETE ON usage_export_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_export_event_immutable');
END;

-- 审计表的动作和对象使用数据库 CHECK；新增导出审计时必须原子重建并保留全部 0031 历史。
DROP TABLE IF EXISTS audit_logs_prd043_new;
CREATE TABLE audit_logs_prd043_new (
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
      'usage_export.requested', 'usage_export.downloaded'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job'
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

-- 审计升级只扩展白名单，不改变任何既有身份快照、摘要、来源和链路字段。
INSERT INTO audit_logs_prd043_new(
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
-- 新表已经承载扩展动作白名单，恢复稳定表名后业务审计服务无需感知重建过程。
ALTER TABLE audit_logs_prd043_new RENAME TO audit_logs;
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
