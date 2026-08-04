-- 回滚职责：仅在没有 SavedFilter、查询审计或对外事件事实时移除 PRD-055 结构。
-- 安全边界：回滚绝不删除数据库外日志、pytest 临时目标或验收证据。
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd055_down_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd055_down_guard BEFORE INSERT ON prd055_down_guard
FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd055_down_has_business_facts'); END;
-- 聚合检查全部 PRD-055 业务事实，任一存在时都在执行 DDL 前原子拒绝回滚。
INSERT INTO prd055_down_guard(issue_count)
SELECT (SELECT COUNT(*) FROM saved_filters)
  + (SELECT COUNT(*) FROM request_log_query_events)
  + (SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='log.query_executed');
DROP TRIGGER trg_prd055_down_guard;
DROP TABLE prd055_down_guard;

DROP TRIGGER trg_request_log_query_events_immutable_delete_prd055;
DROP TRIGGER trg_request_log_query_events_state_update_prd055;
DROP TRIGGER trg_request_log_query_events_rowid_insert_prd055;
DROP TRIGGER trg_request_log_query_events_conflict_insert_prd055;
DROP TRIGGER trg_request_log_query_events_keys_insert_prd055;
DROP TRIGGER trg_request_log_query_events_state_insert_prd055;
DROP TRIGGER trg_request_log_query_events_owner_insert_prd055;
DROP TRIGGER trg_saved_filters_immutable_delete_prd055;
DROP TRIGGER trg_saved_filters_state_update_prd055;
DROP TRIGGER trg_saved_filters_payload_update_prd055;
DROP TRIGGER trg_saved_filters_rowid_insert_prd055;
DROP TRIGGER trg_saved_filters_conflict_insert_prd055;
DROP TRIGGER trg_saved_filters_payload_insert_prd055;
DROP TRIGGER trg_saved_filters_state_insert_prd055;
DROP TRIGGER trg_saved_filters_owner_insert_prd055;

DROP INDEX idx_request_logs_terminal_stage_time_prd055;
DROP INDEX idx_request_logs_cost_time_prd055;
DROP INDEX idx_request_logs_tokens_time_prd055;
DROP INDEX idx_request_logs_latency_time_prd055;
DROP INDEX idx_request_logs_status_time_prd055;
DROP INDEX idx_request_logs_provider_time_prd055;
DROP INDEX idx_request_logs_virtual_key_time_prd055;
DROP INDEX idx_request_logs_trace_time_prd055;
DROP INDEX idx_request_log_query_events_pending_prd055;
DROP INDEX idx_request_log_query_events_actor_time_prd055;
DROP INDEX idx_saved_filters_owner_time_prd055;
DROP INDEX idx_saved_filters_owner_name_prd055;

DROP TABLE request_log_query_events;
DROP TABLE saved_filters;

CREATE TEMP TABLE prd055_down_integrity(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd055_down_integrity BEFORE INSERT ON prd055_down_integrity
FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd055_down_integrity_failed'); END;
-- 删除结构后再次核对残留对象、外键和数据库完整性，失败时撤销整个回滚事务。
INSERT INTO prd055_down_integrity(issue_count)
SELECT (SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%prd055')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + CASE WHEN (SELECT integrity_check FROM pragma_integrity_check)='ok' THEN 0 ELSE 1 END;
DROP TRIGGER trg_prd055_down_integrity;
DROP TABLE prd055_down_integrity;

COMMIT;
