-- 回滚说明：先解除 request_logs 对流会话字段的触发器与索引依赖，再删除字段和流会话表。

DROP TRIGGER IF EXISTS trg_request_log_stream_update;
DROP TRIGGER IF EXISTS trg_request_log_stream_insert;
-- 删除调用日志流会话查询索引后，SQLite 才能安全重写并移除关联列。
DROP INDEX IF EXISTS idx_request_logs_stream_session;

-- 先删除终态摘要，再删除会话外键列，保证 SQLite 重写 request_logs 时不存在残留引用。
ALTER TABLE request_logs DROP COLUMN stream_status;
-- 会话终态摘要已移除，随后删除 request_logs 中的会话外键标识。
ALTER TABLE request_logs DROP COLUMN stream_session_id;

-- 按模型回看流会话的索引不再需要，先于会话表删除。
DROP INDEX IF EXISTS idx_stream_sessions_tenant_model_time;
-- 删除租户状态时间索引，恢复 0026 的索引集合。
DROP INDEX IF EXISTS idx_stream_sessions_tenant_status_time;
-- 最后删除唯一请求索引，解除同租户 request_id 的会话约束。
DROP INDEX IF EXISTS idx_stream_sessions_tenant_request;
DROP TRIGGER IF EXISTS trg_stream_session_status_transition;
DROP TRIGGER IF EXISTS trg_stream_session_tenant_update;
DROP TRIGGER IF EXISTS trg_stream_session_tenant_insert;
-- 所有外部关联、索引和触发器均已解除，现在删除流会话主表。
DROP TABLE IF EXISTS stream_sessions;
