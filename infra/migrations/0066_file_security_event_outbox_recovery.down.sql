-- PRD-073 终审修复回滚：只允许空 outbox 回到 0065，禁止删除任何恢复或投递历史。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 任一 source 对应的 pending/processing/成功/失败状态都是业务事实，降级必须原子拒绝。
DROP TABLE IF EXISTS temp.prd073_recovery_down_business_guard;
DROP TRIGGER IF EXISTS temp.trg_prd073_recovery_down_business_guard;
CREATE TEMP TABLE prd073_recovery_down_business_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd073_recovery_down_business_guard
BEFORE INSERT ON prd073_recovery_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd073_recovery_outbox_facts_present'); END;
-- 任一业务行都代表恢复或投递事实，先写 guard 触发器并在删除 DDL 前失败关闭。
INSERT INTO prd073_recovery_down_business_guard(invalid_count)
SELECT COUNT(*) FROM file_security_event_outbox;
DROP TRIGGER trg_prd073_recovery_down_business_guard;
DROP TABLE prd073_recovery_down_business_guard;

-- 首项持久 DDL 前冻结 0066 栈顶、表形状、触发器与数据库完整性。
DROP TABLE IF EXISTS temp.prd073_recovery_down_guard;
CREATE TEMP TABLE prd073_recovery_down_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd073_recovery_down_guard_ok CHECK (issue_count=0)
);
-- 聚合栈顶、对象形状与完整性，非零时不得移除任何持久对象。
INSERT INTO prd073_recovery_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0066' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0066')
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('file_security_events'))=14
      AND (SELECT COUNT(*) FROM pragma_table_info('file_security_event_outbox'))=13
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN (
      'trg_file_security_event_outbox_validate_insert_prd073_recovery',
      'trg_file_security_event_outbox_conflict_insert_prd073_recovery',
      'trg_file_security_event_outbox_state_update_prd073_recovery',
      'trg_file_security_event_outbox_delete_prd073_recovery',
      'trg_file_security_event_outbox_rowid_prd073_recovery',
      'trg_file_security_events_create_outbox_prd073_recovery'
    ))=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN (
      'idx_file_security_event_outbox_due_prd073_recovery',
      'idx_file_security_event_outbox_tenant_status_prd073_recovery'
    ))=2 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd073_recovery_down_guard;

DROP TRIGGER trg_file_security_events_create_outbox_prd073_recovery;
DROP TRIGGER trg_file_security_event_outbox_rowid_prd073_recovery;
DROP TRIGGER trg_file_security_event_outbox_delete_prd073_recovery;
DROP TRIGGER trg_file_security_event_outbox_state_update_prd073_recovery;
DROP TRIGGER trg_file_security_event_outbox_conflict_insert_prd073_recovery;
DROP TRIGGER trg_file_security_event_outbox_validate_insert_prd073_recovery;
DROP INDEX idx_file_security_event_outbox_tenant_status_prd073_recovery;
DROP INDEX idx_file_security_event_outbox_due_prd073_recovery;
DROP TABLE file_security_event_outbox;

-- 最终证明只移除 0066 对象，0065 的三张安全事实表保持原样。
DROP TABLE IF EXISTS temp.prd073_recovery_down_final_guard;
CREATE TEMP TABLE prd073_recovery_down_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd073_recovery_down_final_guard_ok CHECK (issue_count=0)
);
-- 最终 guard 证明只删除 0066 对象且 0065 安全事实保持可用。
INSERT INTO prd073_recovery_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('file_security_events'))=14
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'file_security_event_outbox',
      'idx_file_security_event_outbox_due_prd073_recovery',
      'idx_file_security_event_outbox_tenant_status_prd073_recovery',
      'trg_file_security_event_outbox_validate_insert_prd073_recovery',
      'trg_file_security_event_outbox_conflict_insert_prd073_recovery',
      'trg_file_security_event_outbox_state_update_prd073_recovery',
      'trg_file_security_event_outbox_delete_prd073_recovery',
      'trg_file_security_event_outbox_rowid_prd073_recovery',
      'trg_file_security_events_create_outbox_prd073_recovery'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd073_recovery_down_final_guard;
COMMIT;
