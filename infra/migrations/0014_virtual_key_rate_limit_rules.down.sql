-- 迁移说明：0014_virtual_key_rate_limit_rules.down 回滚 PRD-022 的 Virtual Key 级限流规则配置。

-- 回滚删除 idx_virtual_key_rate_limit_rules_virtual_key_id 索引：先移除依赖限流规则表的热路径查询索引。
DROP INDEX IF EXISTS idx_virtual_key_rate_limit_rules_virtual_key_id;

-- 回滚删除 idx_virtual_key_rate_limit_rules_tenant_id 索引：再移除控制面租户维度查询索引。
DROP INDEX IF EXISTS idx_virtual_key_rate_limit_rules_tenant_id;

-- 回滚删除 virtual_key_rate_limit_rules 表：PRD-022 之前不存在 Virtual Key 限流规则配置。
DROP TABLE IF EXISTS virtual_key_rate_limit_rules;
