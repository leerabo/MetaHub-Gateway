-- PRD-049 不可变性加固：阻断 SQLite REPLACE 在默认非递归触发器语义下删除既有事实。
-- 本迁移只追加冲突守卫，不修改 0038 表、业务数据或已应用迁移的 checksum。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 0039 只能叠加在完整 0038 之上；缺表、缺迁移状态或缺原不可变触发器都必须先于正式 DDL 失败。
CREATE TEMP TABLE prd049_hardening_prerequisite_guard (
  issue_count INTEGER NOT NULL
);
-- TEMP trigger 把结构漂移转换为固定错误，避免把具体租户表名或底层 SQL 细节扩散给调用方。
CREATE TEMP TRIGGER trg_prd049_hardening_prerequisite_guard
BEFORE INSERT ON prd049_hardening_prerequisite_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd049_hardening_prerequisite_missing');
END;
-- 精确核对 0038 状态、三张业务表和八个原始 immutable/parent trigger，不能用版本号替代真实结构证据。
INSERT INTO prd049_hardening_prerequisite_guard(issue_count)
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
  )=8 THEN 0 ELSE 1 END;
-- 前置检查通过后立即移除 TEMP guard，避免它影响后续同连接迁移。
DROP TRIGGER trg_prd049_hardening_prerequisite_guard;
DROP TABLE prd049_hardening_prerequisite_guard;

-- Feedback 的五个非冗余唯一冲突必须在 REPLACE 隐式删除旧行前拒绝，固定复用 0038 不可变错误。
CREATE TRIGGER trg_request_feedback_conflict_insert_prd049
BEFORE INSERT ON request_feedbacks
FOR EACH ROW
WHEN
  EXISTS (
    SELECT 1 FROM request_feedbacks current
    WHERE current.request_feedback_id=NEW.request_feedback_id
  )
  OR EXISTS (
    SELECT 1 FROM request_feedbacks current
    WHERE current.feedback_event_id=NEW.feedback_event_id
  )
  OR EXISTS (
    SELECT 1 FROM request_feedbacks current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.source_request_id=NEW.source_request_id
  )
  OR (
    NEW.actor_type='session_user'
    AND EXISTS (
      SELECT 1 FROM request_feedbacks current
      WHERE current.actor_type='session_user'
        AND current.tenant_id=NEW.tenant_id
        AND current.actor_user_id=NEW.actor_user_id
        AND current.operation_request_id=NEW.operation_request_id
    )
  )
  OR (
    NEW.actor_type='virtual_key'
    AND EXISTS (
      SELECT 1 FROM request_feedbacks current
      WHERE current.actor_type='virtual_key'
        AND current.tenant_id=NEW.tenant_id
        AND current.actor_virtual_key_id=NEW.actor_virtual_key_id
        AND current.operation_request_id=NEW.operation_request_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_immutable');
END;

-- Event 的全局事件 ID 与全局 Feedback ID 都是一对一事实键，跨租户冲突同样不能覆盖旧事件。
CREATE TRIGGER trg_request_feedback_event_conflict_insert_prd049
BEFORE INSERT ON request_feedback_events
FOR EACH ROW
WHEN
  EXISTS (
    SELECT 1 FROM request_feedback_events current
    WHERE current.feedback_event_id=NEW.feedback_event_id
  )
  OR EXISTS (
    SELECT 1 FROM request_feedback_events current
    WHERE current.request_feedback_id=NEW.request_feedback_id
  )
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_event_immutable');
END;

-- RequestLog 的生产 upsert 可以更新非归因字段；仅当冲突目标已有 Feedback 且十一项保护快照漂移时拒绝。
CREATE TRIGGER trg_request_log_feedback_parent_conflict_insert_prd049
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM request_logs current
  JOIN request_feedbacks feedback
    ON feedback.tenant_id=current.tenant_id
   AND feedback.source_request_id=current.request_id
  WHERE current.tenant_id=NEW.tenant_id
    AND current.request_id=NEW.request_id
    AND (
      current.virtual_key_id IS NOT NEW.virtual_key_id
      OR current.status IS NOT NEW.status
      OR current.model_id IS NOT NEW.model_id
      OR current.provider_id IS NOT NEW.provider_id
      OR current.project_id IS NOT NEW.project_id
      OR current.application_id IS NOT NEW.application_id
      OR current.environment_id IS NOT NEW.environment_id
      OR current.stream_session_id IS NOT NEW.stream_session_id
      OR current.stream_status IS NOT NEW.stream_status
      OR current.final_routing_attempt_id IS NOT NEW.final_routing_attempt_id
      OR current.routing_attempt_count IS NOT NEW.routing_attempt_count
    )
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;

-- UPDATE OR REPLACE 不经过 INSERT trigger；从数据库定位即将被删除的复合主键目标，并用 rowid 排除当前 OLD 行。
CREATE TRIGGER trg_request_log_feedback_parent_conflict_update_prd049
BEFORE UPDATE OF tenant_id, request_id ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM request_logs target
  JOIN request_feedbacks feedback
    ON feedback.tenant_id=target.tenant_id
   AND feedback.source_request_id=target.request_id
  WHERE target.tenant_id=NEW.tenant_id
    AND target.request_id=NEW.request_id
    AND target.rowid != OLD.rowid
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;

-- final Attempt 的全局 ID 或 tenant/request/sequence 唯一目标都从既有 RequestLog 反查，不能信任 NEW 租户图。
CREATE TRIGGER trg_routing_attempt_feedback_parent_conflict_insert_prd049
BEFORE INSERT ON routing_attempts
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM routing_attempts target
  JOIN request_logs request
    ON request.tenant_id=target.tenant_id
   AND request.request_id=target.request_id
   AND request.final_routing_attempt_id=target.routing_attempt_id
  JOIN request_feedbacks feedback
    ON feedback.tenant_id=request.tenant_id
   AND feedback.source_request_id=request.request_id
  WHERE target.routing_attempt_id=NEW.routing_attempt_id
     OR (
       target.tenant_id=NEW.tenant_id
       AND target.request_id=NEW.request_id
       AND target.attempt_sequence=NEW.attempt_sequence
     )
)
BEGIN
  SELECT RAISE(ABORT, 'routing_attempt_feedback_parent_immutable');
END;

-- Attempt 的 UPDATE OR REPLACE 也必须查询冲突目标而非 OLD 源行；rowid 排除自身普通更新和生产补账。
CREATE TRIGGER trg_routing_attempt_feedback_parent_conflict_update_prd049
BEFORE UPDATE OF routing_attempt_id, tenant_id, request_id, attempt_sequence ON routing_attempts
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM routing_attempts target
  JOIN request_logs request
    ON request.tenant_id=target.tenant_id
   AND request.request_id=target.request_id
   AND request.final_routing_attempt_id=target.routing_attempt_id
  JOIN request_feedbacks feedback
    ON feedback.tenant_id=request.tenant_id
   AND feedback.source_request_id=request.request_id
  WHERE target.rowid != OLD.rowid
    AND (
      target.routing_attempt_id=NEW.routing_attempt_id
      OR (
        target.tenant_id=NEW.tenant_id
        AND target.request_id=NEW.request_id
        AND target.attempt_sequence=NEW.attempt_sequence
      )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'routing_attempt_feedback_parent_immutable');
END;

-- 六个持久 trigger 创建后同时核对名称、外键图和 SQLite 内部完整性，失败会回滚全部 0039 DDL。
CREATE TEMP TABLE prd049_hardening_up_integrity_guard (
  issue_count INTEGER NOT NULL
);
-- 固定错误区分 0039 完整性故障与前置版本缺失，便于运维采取不同恢复动作。
CREATE TEMP TRIGGER trg_prd049_hardening_up_integrity_guard
BEFORE INSERT ON prd049_hardening_up_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd049_hardening_up_integrity_failed');
END;
-- 版本存在不等于防线存在；验收必须看到六个精确名称且数据库无 FK 或内部结构问题。
INSERT INTO prd049_hardening_up_integrity_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger'
      AND name IN (
        'trg_request_feedback_conflict_insert_prd049',
        'trg_request_feedback_event_conflict_insert_prd049',
        'trg_request_log_feedback_parent_conflict_insert_prd049',
        'trg_request_log_feedback_parent_conflict_update_prd049',
        'trg_routing_attempt_feedback_parent_conflict_insert_prd049',
        'trg_routing_attempt_feedback_parent_conflict_update_prd049'
      )
  )=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
-- 完整性检查成功后清理 TEMP 对象，连接生命周期不残留本迁移的会话级状态。
DROP TRIGGER trg_prd049_hardening_up_integrity_guard;
DROP TABLE prd049_hardening_up_integrity_guard;

COMMIT;
