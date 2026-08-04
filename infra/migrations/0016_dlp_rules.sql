-- 迁移说明：0016_dlp_rules 为 PRD-024 增加租户级关键词/正则 DLP 规则、命中记录和调用日志摘要字段。
-- 创建 dlp_rules 表：保存租户级 DLP 规则，pattern 是管理员配置数据，普通请求日志不得输出该字段。
CREATE TABLE IF NOT EXISTS dlp_rules (
  dlp_rule_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL,
  rule_type TEXT NOT NULL CHECK (rule_type IN ('keyword', 'regex')),
  pattern TEXT NOT NULL,
  pattern_hash TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('allow', 'log_only', 'block')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 1 AND priority <= 10000),
  description TEXT,
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, pattern_hash, rule_type)
);

-- 创建 dlp_matches 表：保存请求级 DLP 命中摘要，只记录规则 ID、动作和哈希，不保存命中文本明文。
CREATE TABLE IF NOT EXISTS dlp_matches (
  dlp_match_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT,
  dlp_rule_id TEXT NOT NULL,
  rule_name TEXT NOT NULL,
  rule_type TEXT NOT NULL CHECK (rule_type IN ('keyword', 'regex')),
  action TEXT NOT NULL CHECK (action IN ('allow', 'log_only', 'block')),
  message_index INTEGER,
  match_text_hash TEXT NOT NULL,
  match_length INTEGER NOT NULL DEFAULT 0 CHECK (match_length >= 0),
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (dlp_rule_id) REFERENCES dlp_rules(dlp_rule_id)
);

-- 调整 request_logs 表：保存 DLP 检查状态，便于列表和详情快速解释安全策略结果。
ALTER TABLE request_logs
  ADD COLUMN dlp_status TEXT;

-- 调整 request_logs 表：保存第一条生效 DLP 规则 ID，不保存规则 pattern 或命中文本。
ALTER TABLE request_logs
  ADD COLUMN dlp_rule_id TEXT;

-- 调整 request_logs 表：保存第一条生效 DLP 动作，区分 allow、log_only 和 block。
ALTER TABLE request_logs
  ADD COLUMN dlp_action TEXT;

-- 调整 request_logs 表：保存本次请求命中的 DLP 规则数量，用于日志详情提示是否存在多条命中。
ALTER TABLE request_logs
  ADD COLUMN dlp_match_count INTEGER NOT NULL DEFAULT 0;

-- 调整 request_logs 表：保存 DLP 原因码，前端可据此展示统一诊断，不暴露原始输入。
ALTER TABLE request_logs
  ADD COLUMN dlp_reason_code TEXT;

-- 创建 idx_dlp_rules_tenant_status_priority 索引：支撑数据面按租户快速加载 active 规则并按优先级执行。
CREATE INDEX IF NOT EXISTS idx_dlp_rules_tenant_status_priority
  ON dlp_rules(tenant_id, status, priority);

-- 创建 idx_dlp_rules_tenant_type 索引：支撑控制台按关键词/正则类型筛选规则。
CREATE INDEX IF NOT EXISTS idx_dlp_rules_tenant_type
  ON dlp_rules(tenant_id, rule_type);

-- 创建 idx_dlp_matches_request 索引：支撑调用日志详情按 request_id 读取 DLP 命中摘要。
CREATE INDEX IF NOT EXISTS idx_dlp_matches_request
  ON dlp_matches(tenant_id, request_id);

-- 创建 idx_dlp_matches_rule_date 索引：支撑后续按规则和时间分析误拦截或安全命中趋势。
CREATE INDEX IF NOT EXISTS idx_dlp_matches_rule_date
  ON dlp_matches(tenant_id, dlp_rule_id, created_at);
