-- PRD-049 条件回滚：任何反馈、持久事件、派生统计、订阅或投递事实存在时禁止降级。
-- 循环 deferred FK 只在空事实 guard 通过后拆除，因此事务前临时关闭外键并在脚本末恢复。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- TEMP guard 必须先于所有正式 DROP；固定错误便于运维区分业务事实与 SQL 结构故障。
CREATE TEMP TABLE prd049_down_business_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd049_down_business_guard
BEFORE INSERT ON prd049_down_business_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd049_down_business_facts_present');
END;
-- 汇总 Feedback/Event/Aggregate、feedback.created 订阅、投递及投递尝试，任一存在均原子拒绝。
INSERT INTO prd049_down_business_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM request_feedbacks)
  + (SELECT COUNT(*) FROM request_feedback_events)
  + (SELECT COUNT(*) FROM model_feedback_statistics)
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE EXISTS (
       SELECT 1 FROM json_each(endpoint.event_types_json) item
       WHERE item.value='feedback.created'
     ))
  + (SELECT COUNT(*) FROM webhook_deliveries delivery
     WHERE delivery.event_type='feedback.created')
  + (SELECT COUNT(*)
     FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='feedback.created');
DROP TRIGGER trg_prd049_down_business_guard;
DROP TABLE prd049_down_business_guard;

-- 先解除父表保护，避免随后删除 Feedback 表时留下引用已不存在对象的 trigger。
DROP TRIGGER IF EXISTS trg_routing_attempt_feedback_parent_delete_prd049;
DROP TRIGGER IF EXISTS trg_routing_attempt_feedback_parent_update_prd049;
DROP TRIGGER IF EXISTS trg_request_log_feedback_parent_delete_prd049;
DROP TRIGGER IF EXISTS trg_request_log_feedback_parent_update_prd049;
-- 再解除 Event/Feedback 不可变与 graph/payload 保护，允许空循环表按确定顺序拆除。
DROP TRIGGER IF EXISTS trg_request_feedback_event_immutable_delete_prd049;
DROP TRIGGER IF EXISTS trg_request_feedback_event_immutable_update_prd049;
DROP TRIGGER IF EXISTS trg_request_feedback_immutable_delete_prd049;
DROP TRIGGER IF EXISTS trg_request_feedback_immutable_update_prd049;
DROP TRIGGER IF EXISTS trg_request_feedback_event_payload_insert_prd049;
DROP TRIGGER IF EXISTS trg_request_feedback_event_graph_insert_prd049;
-- Aggregate DELETE 防护必须在删派生表前解除，Feedback AFTER INSERT 与验证 trigger 一并撤销。
DROP TRIGGER IF EXISTS trg_request_feedback_aggregate_insert_prd049;
DROP TRIGGER IF EXISTS trg_model_feedback_statistics_delete_prd049;
DROP TRIGGER IF EXISTS trg_model_feedback_statistics_validate_update_prd049;
DROP TRIGGER IF EXISTS trg_model_feedback_statistics_validate_insert_prd049;
DROP TRIGGER IF EXISTS trg_request_feedback_graph_insert_prd049;
DROP TRIGGER IF EXISTS trg_request_feedback_tags_insert_prd049;

-- 删除 0038 自有索引；前序 tenant-qualified user/model/Key 候选键必须保留。
DROP INDEX IF EXISTS idx_request_feedback_event_time_prd049;
-- 删除源 Key 时间索引，不影响前序 virtual_keys tenant 候选键。
DROP INDEX IF EXISTS idx_request_feedback_key_time_prd049;
-- 删除模型时间索引，models 的 tenant-qualified unique index 继续保留。
DROP INDEX IF EXISTS idx_request_feedback_model_time_prd049;
-- 删除租户窗口索引，恢复 0037 时不遗留引用已删除表的对象。
DROP INDEX IF EXISTS idx_request_feedback_tenant_time_prd049;
-- 删除虚拟密钥提交反馈使用的租户内幂等唯一索引，该对象完全由本次迁移创建。
DROP INDEX IF EXISTS uq_request_feedback_key_operation_prd049;
-- 删除控制台用户提交反馈使用的租户内幂等唯一索引，前序用户索引仍完整保留。
DROP INDEX IF EXISTS uq_request_feedback_session_operation_prd049;

-- 外键已在事务前关闭，按 Event -> Aggregate -> Feedback 顺序拆除空的循环图。
DROP TABLE IF EXISTS request_feedback_events;
-- Event 已移除后删除空 Aggregate，避免保留旧代码无法解释的派生表。
DROP TABLE IF EXISTS model_feedback_statistics;
-- 最后删除空 Feedback 父事实表，循环 FK 在本事务内不会产生悬挂事实。
DROP TABLE IF EXISTS request_feedbacks;

-- 删除后再次用 TEMP trigger 校验前序 schema 的 FK 与内部结构，异常会回滚全部 DROP。
CREATE TEMP TABLE prd049_down_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd049_down_integrity_guard
BEFORE INSERT ON prd049_down_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd049_down_integrity_failed');
END;
-- 空表回滚后必须恢复为无 FK 悬挂且 integrity_check=ok 的 0037 结构。
INSERT INTO prd049_down_integrity_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER trg_prd049_down_integrity_guard;
DROP TABLE prd049_down_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
