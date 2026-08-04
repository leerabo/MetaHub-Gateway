-- 迁移说明：0001_core_mvp_tables.down 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 回滚删除 usage_ledger 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS usage_ledger;
-- 回滚删除 request_logs 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS request_logs;
-- 回滚删除 models 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS models;
-- 回滚删除 provider_credentials 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS provider_credentials;
-- 回滚删除 providers 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS providers;
-- 回滚删除 virtual_keys 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS virtual_keys;
-- 回滚删除 users 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS users;
-- 回滚删除 seed_runs 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS seed_runs;
-- 回滚删除 tenants 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS tenants;
