-- PRD-049 0039 条件回滚：有任一反馈或投递事实时禁止移除 REPLACE 加固防线。
-- 0039 不含循环表 DDL，始终保持外键开启，并在一个显式事务内完成门禁、DROP 与复核。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 六类事实门禁完整复用 0038 口径，必须先于任一持久 trigger 的 DROP 执行。
CREATE TEMP TABLE prd049_hardening_down_business_guard (
  issue_count INTEGER NOT NULL
);
-- 固定沿用 0038 的业务事实错误，调用方无需依据版本猜测不可降级原因。
CREATE TEMP TRIGGER trg_prd049_hardening_down_business_guard
BEFORE INSERT ON prd049_hardening_down_business_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd049_down_business_facts_present');
END;
-- Feedback、Event、Aggregate、订阅、delivery 和关联 attempt 任一存在都原子拒绝回滚。
INSERT INTO prd049_hardening_down_business_guard(issue_count)
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
-- 事实为空后清理 TEMP 门禁；后续任何失败仍由外层事务恢复六个持久 trigger。
DROP TRIGGER trg_prd049_hardening_down_business_guard;
DROP TABLE prd049_hardening_down_business_guard;

-- 先移除 UPDATE 冲突目标守卫，再移除对应 INSERT 守卫，恢复为完整且未改动的 0038 行为。
DROP TRIGGER trg_routing_attempt_feedback_parent_conflict_update_prd049;
DROP TRIGGER trg_routing_attempt_feedback_parent_conflict_insert_prd049;
DROP TRIGGER trg_request_log_feedback_parent_conflict_update_prd049;
DROP TRIGGER trg_request_log_feedback_parent_conflict_insert_prd049;
-- Event 和 Feedback 守卫最后删除；0038 原 graph、immutable 和 parent trigger 全部保留。
DROP TRIGGER trg_request_feedback_event_conflict_insert_prd049;
DROP TRIGGER trg_request_feedback_conflict_insert_prd049;

-- down 完成前核对六个 0039 trigger 已清除、0038 基线仍完整且数据库无 FK/内部结构问题。
CREATE TEMP TABLE prd049_hardening_down_integrity_guard (
  issue_count INTEGER NOT NULL
);
-- 固定错误表明 down 后结构不再等价于 0038，迁移器会 rollback 并保持 0039 applied。
CREATE TEMP TRIGGER trg_prd049_hardening_down_integrity_guard
BEFORE INSERT ON prd049_hardening_down_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd049_hardening_down_integrity_failed');
END;
-- 精确核对版本、三表、八个原 trigger 和六个已删除 trigger，防止静默接受人工结构漂移。
INSERT INTO prd049_hardening_down_integrity_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0038' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table'
      AND name IN (
        'request_feedbacks',
        'request_feedback_events',
        'model_feedback_statistics'
      )
  )=3 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger'
      AND name IN (
        'trg_request_feedback_immutable_update_prd049',
        'trg_request_feedback_immutable_delete_prd049',
        'trg_request_feedback_event_immutable_update_prd049',
        'trg_request_feedback_event_immutable_delete_prd049',
        'trg_request_log_feedback_parent_update_prd049',
        'trg_request_log_feedback_parent_delete_prd049',
        'trg_routing_attempt_feedback_parent_update_prd049',
        'trg_routing_attempt_feedback_parent_delete_prd049'
      )
  )=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='trigger'
       AND name IN (
         'trg_request_feedback_conflict_insert_prd049',
         'trg_request_feedback_event_conflict_insert_prd049',
         'trg_request_log_feedback_parent_conflict_insert_prd049',
         'trg_request_log_feedback_parent_conflict_update_prd049',
         'trg_routing_attempt_feedback_parent_conflict_insert_prd049',
         'trg_routing_attempt_feedback_parent_conflict_update_prd049'
       ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
-- 成功时移除 TEMP 完整性门禁；失败时本事务连同此前 DROP 一并回滚。
DROP TRIGGER trg_prd049_hardening_down_integrity_guard;
DROP TABLE prd049_hardening_down_integrity_guard;

COMMIT;
