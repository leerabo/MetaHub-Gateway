-- PRD-035 Provider 健康探测：保存可用于路由的新版健康证据、逐凭据状态和安全探测日志。

-- Provider 聚合字段分别保留最近成功与失败时间，便于恢复判断和控制台解释。
ALTER TABLE providers ADD COLUMN last_success_at TEXT;
-- 最近失败时间只记录确定性失败，inconclusive 不得刷新该证据。
ALTER TABLE providers ADD COLUMN last_failure_at TEXT;
-- 延迟只保存非负毫秒值，避免异常计时污染监控口径。
ALTER TABLE providers ADD COLUMN last_probe_latency_ms INTEGER CHECK (last_probe_latency_ms IS NULL OR last_probe_latency_ms >= 0);
-- HTTP 状态限制为标准三位数范围，传输异常使用空值和错误码表达。
ALTER TABLE providers ADD COLUMN last_probe_http_status INTEGER CHECK (last_probe_http_status IS NULL OR last_probe_http_status BETWEEN 100 AND 599);
-- 错误字段只保存固定分类，不保存上游响应正文或异常 repr。
ALTER TABLE providers ADD COLUMN last_probe_error_code TEXT;
-- 连续失败计数支撑定时探测阈值状态机，不能出现负数。
ALTER TABLE providers ADD COLUMN consecutive_probe_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_probe_failures >= 0);
-- 配置代际用于拒绝 Endpoint 或 KeyPool 变化后的迟到探测结果。
ALTER TABLE providers ADD COLUMN health_generation INTEGER NOT NULL DEFAULT 0 CHECK (health_generation >= 0);
-- 健康证据时间与配置 updated_at 分离，避免周期任务伪造管理变更。
ALTER TABLE providers ADD COLUMN health_updated_at TEXT;
-- 下次探测与租约字段支持多进程安全领取，不进入数据面请求热路径。
ALTER TABLE providers ADD COLUMN next_probe_at TEXT;
-- 租约令牌标识本轮唯一领取者，完成事务必须带令牌执行条件更新。
ALTER TABLE providers ADD COLUMN probe_lease_token TEXT;
-- 租约到期时间允许进程崩溃后重新领取，避免任务永久卡住。
ALTER TABLE providers ADD COLUMN probe_lease_until TEXT;

-- Key 级健康字段避免一把坏 Key 被放大为整个 Provider 故障。
ALTER TABLE provider_credentials ADD COLUMN probe_health_status TEXT NOT NULL DEFAULT 'unknown'
  CHECK (probe_health_status IN ('unknown', 'healthy', 'unhealthy'));
-- Key 最近探测时间用于新鲜度过滤，过期结论按 unknown Fail Open。
ALTER TABLE provider_credentials ADD COLUMN probe_last_tested_at TEXT;
-- 最近成功时间便于认证恢复排障，但不替代业务调用 last_used_at。
ALTER TABLE provider_credentials ADD COLUMN probe_last_success_at TEXT;
-- Key 错误只保存固定分类，不记录明文、密文或上游正文。
ALTER TABLE provider_credentials ADD COLUMN probe_last_error_code TEXT;
-- Key 连续失败计数与管理状态分离，探测不得自动禁用管理员配置。
ALTER TABLE provider_credentials ADD COLUMN probe_consecutive_failures INTEGER NOT NULL DEFAULT 0
  CHECK (probe_consecutive_failures >= 0);

-- 旧 /test 没有携带托管凭据，其结果不能直接参与新版自动摘流。
UPDATE providers
SET health_status='unknown', last_tested_at=NULL, last_test_error=NULL,
    consecutive_probe_failures=0, health_updated_at=NULL,
    next_probe_at=CASE WHEN status='active' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
    probe_lease_token=NULL, probe_lease_until=NULL;

CREATE TABLE provider_health_probe_logs (
  probe_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_credential_id TEXT,
  health_generation INTEGER NOT NULL CHECK (health_generation >= 0),
  attempt_sequence INTEGER NOT NULL CHECK (attempt_sequence > 0),
  trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual', 'scheduled', 'legacy_test')),
  result_status TEXT NOT NULL CHECK (result_status IN ('success', 'failed', 'inconclusive')),
  auth_result TEXT NOT NULL CHECK (auth_result IN ('accepted', 'rejected', 'unverified', 'unavailable', 'rate_limited')),
  http_status INTEGER CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
  latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
  error_code TEXT,
  result_summary TEXT NOT NULL CHECK (length(CAST(result_summary AS BLOB)) <= 240),
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id),
  FOREIGN KEY (provider_credential_id) REFERENCES provider_credentials(provider_credential_id)
);

CREATE INDEX idx_provider_health_due
  ON providers(status, next_probe_at, probe_lease_until);
CREATE INDEX idx_provider_health_tenant_status
  ON providers(tenant_id, health_status, health_updated_at);
CREATE INDEX idx_provider_health_logs_tenant_provider
  ON provider_health_probe_logs(tenant_id, provider_id, checked_at DESC);

-- SQLite 复合外键无法直接引用现有主键结构，触发器负责拒绝跨租户 Provider/Key 归属。
CREATE TRIGGER trg_provider_health_log_tenant_insert
BEFORE INSERT ON provider_health_probe_logs
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM providers p
    WHERE p.provider_id=NEW.provider_id AND p.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'provider_health_tenant_mismatch') END;
  SELECT CASE WHEN NEW.provider_credential_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM provider_credentials c
    WHERE c.provider_credential_id=NEW.provider_credential_id
      AND c.provider_id=NEW.provider_id AND c.tenant_id=NEW.tenant_id
  ) THEN RAISE(ABORT, 'provider_health_credential_mismatch') END;
END;
