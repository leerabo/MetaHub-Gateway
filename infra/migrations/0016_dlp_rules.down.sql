-- 迁移说明：0016_dlp_rules.down 回滚 PRD-024 的 DLP 规则、命中记录和调用日志摘要字段。
-- 回滚删除 idx_dlp_matches_rule_date 索引：先移除 DLP 命中按规则时间查询索引。
DROP INDEX IF EXISTS idx_dlp_matches_rule_date;

-- 回滚删除 idx_dlp_matches_request 索引：移除调用日志详情读取 DLP 命中的查询索引。
DROP INDEX IF EXISTS idx_dlp_matches_request;

-- 回滚删除 idx_dlp_rules_tenant_type 索引：移除控制台按规则类型筛选索引。
DROP INDEX IF EXISTS idx_dlp_rules_tenant_type;

-- 回滚删除 idx_dlp_rules_tenant_status_priority 索引：移除数据面加载 active 规则的热路径索引。
DROP INDEX IF EXISTS idx_dlp_rules_tenant_status_priority;

-- 回滚删除 dlp_matches 表：PRD-024 之前不存在 DLP 请求级命中摘要记录。
DROP TABLE IF EXISTS dlp_matches;

-- 回滚删除 dlp_rules 表：PRD-024 之前不存在租户级关键词/正则 DLP 规则配置。
DROP TABLE IF EXISTS dlp_rules;

-- 回滚删除 request_logs.dlp_reason_code 字段：恢复到 DLP 摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN dlp_reason_code;

-- 回滚删除 request_logs.dlp_match_count 字段：恢复到 DLP 摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN dlp_match_count;

-- 回滚删除 request_logs.dlp_action 字段：恢复到 DLP 摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN dlp_action;

-- 回滚删除 request_logs.dlp_rule_id 字段：恢复到 DLP 摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN dlp_rule_id;

-- 回滚删除 request_logs.dlp_status 字段：恢复到 DLP 摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN dlp_status;
