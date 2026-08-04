-- 迁移说明：0027_stream_sessions 为 PRD-037 建立流式调用审计会话，并把终态摘要关联到调用日志。

-- 创建 stream_sessions 表：只保存流生命周期、计数和安全归属元数据，绝不保存 Prompt、Response、Endpoint 或密钥。
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
    status IN ('starting', 'active', 'completed', 'timed_out', 'client_disconnected', 'upstream_failed')
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
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (project_id) REFERENCES projects(project_id),
  FOREIGN KEY (application_id) REFERENCES applications(application_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);

-- 会话写入前校验全部非空引用的租户归属；单列外键只能证明对象存在，不能阻止跨租户拼接。
CREATE TRIGGER trg_stream_session_tenant_insert
BEFORE INSERT ON stream_sessions
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NEW.virtual_key_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM virtual_keys vk
    WHERE vk.virtual_key_id=NEW.virtual_key_id AND vk.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'stream_session_virtual_key_tenant_mismatch') END;
  SELECT CASE WHEN NEW.environment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environments e
    WHERE e.environment_id=NEW.environment_id AND e.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'stream_session_environment_tenant_mismatch') END;
  SELECT CASE WHEN NEW.environment_id IS NOT NULL AND NEW.environment_code IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environments e
    WHERE e.environment_id=NEW.environment_id
      AND e.tenant_id=NEW.tenant_id
      AND e.code=NEW.environment_code
  ) THEN RAISE(ABORT, 'stream_session_environment_snapshot_mismatch') END;
  SELECT CASE WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM projects p
    WHERE p.project_id=NEW.project_id AND p.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'stream_session_project_tenant_mismatch') END;
  SELECT CASE WHEN NEW.application_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM applications a
    WHERE a.application_id=NEW.application_id
      AND a.tenant_id=NEW.tenant_id
      AND (NEW.project_id IS NULL OR a.project_id=NEW.project_id)
  ) THEN RAISE(ABORT, 'stream_session_application_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.model_id=NEW.model_id AND m.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'stream_session_model_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM providers p
    WHERE p.provider_id=NEW.provider_id
      AND p.tenant_id=NEW.tenant_id
      AND p.provider_code=NEW.provider_code
  ) THEN RAISE(ABORT, 'stream_session_provider_tenant_mismatch') END;
END;

-- 归属字段更新必须重新执行完整校验，并且 tenant_id 一经创建不可修改，避免会话被整体迁移到另一租户。
CREATE TRIGGER trg_stream_session_tenant_update
BEFORE UPDATE OF tenant_id, virtual_key_id, environment_id, environment_code,
  project_id, application_id, model_id, provider_id, provider_code ON stream_sessions
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NEW.tenant_id<>OLD.tenant_id
    THEN RAISE(ABORT, 'stream_session_tenant_immutable') END;
  SELECT CASE WHEN NEW.virtual_key_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM virtual_keys vk
    WHERE vk.virtual_key_id=NEW.virtual_key_id AND vk.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'stream_session_virtual_key_tenant_mismatch') END;
  SELECT CASE WHEN NEW.environment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environments e
    WHERE e.environment_id=NEW.environment_id AND e.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'stream_session_environment_tenant_mismatch') END;
  SELECT CASE WHEN NEW.environment_id IS NOT NULL AND NEW.environment_code IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environments e
    WHERE e.environment_id=NEW.environment_id
      AND e.tenant_id=NEW.tenant_id
      AND e.code=NEW.environment_code
  ) THEN RAISE(ABORT, 'stream_session_environment_snapshot_mismatch') END;
  SELECT CASE WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM projects p
    WHERE p.project_id=NEW.project_id AND p.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'stream_session_project_tenant_mismatch') END;
  SELECT CASE WHEN NEW.application_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM applications a
    WHERE a.application_id=NEW.application_id
      AND a.tenant_id=NEW.tenant_id
      AND (NEW.project_id IS NULL OR a.project_id=NEW.project_id)
  ) THEN RAISE(ABORT, 'stream_session_application_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.model_id=NEW.model_id AND m.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'stream_session_model_tenant_mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM providers p
    WHERE p.provider_id=NEW.provider_id
      AND p.tenant_id=NEW.tenant_id
      AND p.provider_code=NEW.provider_code
  ) THEN RAISE(ABORT, 'stream_session_provider_tenant_mismatch') END;
END;

-- 状态机只允许 starting -> active -> 终态，握手失败是唯一允许从 starting 直接进入的终态。
CREATE TRIGGER trg_stream_session_status_transition
BEFORE UPDATE OF status ON stream_sessions
FOR EACH ROW
WHEN NOT (
  NEW.status=OLD.status
  OR (OLD.status='starting' AND NEW.status IN ('active', 'upstream_failed'))
  OR (
    OLD.status='active'
    AND NEW.status IN ('completed', 'timed_out', 'client_disconnected', 'upstream_failed')
  )
)
BEGIN
  SELECT RAISE(ABORT, 'stream_session_status_transition_invalid');
END;

-- 同一租户的同一个 request_id 只能建立一条流会话，唯一冲突是重复上游调用的 Fail Closed 闸门。
CREATE UNIQUE INDEX idx_stream_sessions_tenant_request
  ON stream_sessions(tenant_id, request_id);

-- 状态时间索引用于租户内排查超时、断开和异常终态。
CREATE INDEX idx_stream_sessions_tenant_status_time
  ON stream_sessions(tenant_id, status, started_at DESC);

-- 模型时间索引用于按实际调用模型回看近期流稳定性。
CREATE INDEX idx_stream_sessions_tenant_model_time
  ON stream_sessions(tenant_id, model_id, started_at DESC);

-- 调用日志只保存会话 ID 和终态摘要；逐块计数与时间仅在详情查询时读取 stream_sessions。
ALTER TABLE request_logs
  ADD COLUMN stream_session_id TEXT REFERENCES stream_sessions(stream_session_id);

-- stream_status 是终态快照，非流式调用保持 NULL，starting/active 不允许写入调用日志摘要。
ALTER TABLE request_logs
  ADD COLUMN stream_status TEXT CHECK (
    stream_status IS NULL
    OR stream_status IN ('completed', 'timed_out', 'client_disconnected', 'upstream_failed')
  );

-- 关联索引支撑详情按租户和会话定位，并保持跨租户查询必须携带 tenant_id 的访问路径。
CREATE INDEX idx_request_logs_stream_session
  ON request_logs(tenant_id, stream_session_id)
  WHERE stream_session_id IS NOT NULL;

-- INSERT 一致性触发器：两个流字段必须成对出现，并匹配同租户、同 request_id 的已提交终态会话。
CREATE TRIGGER trg_request_log_stream_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.stream_session_id IS NULL)<>(NEW.stream_status IS NULL)
    THEN RAISE(ABORT, 'request_log_stream_fields_incomplete') END;
  SELECT CASE WHEN NEW.stream_session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM stream_sessions ss
    WHERE ss.stream_session_id=NEW.stream_session_id
      AND ss.tenant_id=NEW.tenant_id
      AND ss.request_id=NEW.request_id
      AND ss.status=NEW.stream_status
  ) THEN RAISE(ABORT, 'request_log_stream_session_mismatch') END;
  SELECT CASE WHEN NEW.stream_status IS NOT NULL AND NOT (
    (NEW.stream_status='completed' AND NEW.status='success')
    OR (NEW.stream_status<>'completed' AND NEW.status='failed')
  ) THEN RAISE(ABORT, 'request_log_stream_status_mismatch') END;
END;

-- UPDATE 一致性触发器覆盖网关终态回写及维护脚本改写，防止关联字段或 success/failed 摘要发生漂移。
CREATE TRIGGER trg_request_log_stream_update
BEFORE UPDATE OF tenant_id, request_id, status, stream_session_id, stream_status ON request_logs
FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.stream_session_id IS NULL)<>(NEW.stream_status IS NULL)
    THEN RAISE(ABORT, 'request_log_stream_fields_incomplete') END;
  SELECT CASE WHEN NEW.stream_session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM stream_sessions ss
    WHERE ss.stream_session_id=NEW.stream_session_id
      AND ss.tenant_id=NEW.tenant_id
      AND ss.request_id=NEW.request_id
      AND ss.status=NEW.stream_status
  ) THEN RAISE(ABORT, 'request_log_stream_session_mismatch') END;
  SELECT CASE WHEN NEW.stream_status IS NOT NULL AND NOT (
    (NEW.stream_status='completed' AND NEW.status='success')
    OR (NEW.stream_status<>'completed' AND NEW.status='failed')
  ) THEN RAISE(ABORT, 'request_log_stream_status_mismatch') END;
END;
