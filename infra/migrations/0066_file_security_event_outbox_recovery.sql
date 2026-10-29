-- PRD-073 终审修复：为文件安全 Webhook source event 增加可恢复 lease/CAS outbox。
-- 不可变安全事件与可变投递状态分表，避免修改已发布的 0065 事实表及其 checksum。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前证明数据库精确位于 0065，且目标对象尚不存在。
DROP TABLE IF EXISTS temp.prd073_recovery_up_guard;
CREATE TEMP TABLE prd073_recovery_up_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd073_recovery_up_guard_ok CHECK (issue_count=0)
);
-- 聚合前置条件后一次写入零值 guard，任何非零项都在持久 DDL 前原子终止。
INSERT INTO prd073_recovery_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0065' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0065')
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('file_security_events'))=14
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
DROP TABLE prd073_recovery_up_guard;

-- Outbox 不复制正文或 finding 证据；worker 从同 tenant 不可变 source 重建固定七字段事件。
CREATE TABLE file_security_event_outbox (
  file_security_event_id TEXT PRIMARY KEY CHECK (
    length(file_security_event_id)=37 AND substr(file_security_event_id,1,5)='fsev_'
    AND substr(file_security_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  dispatch_status TEXT NOT NULL CHECK (
    dispatch_status IN ('pending','processing','dispatched','failed')
  ),
  dispatch_attempts INTEGER NOT NULL CHECK (
    typeof(dispatch_attempts)='integer' AND dispatch_attempts BETWEEN 0 AND 10
  ),
  next_attempt_at TEXT,
  dispatched_at TEXT,
  claim_token TEXT,
  lease_owner_id TEXT,
  lease_expires_at TEXT,
  delivery_snapshot_frozen_at TEXT CHECK (
    delivery_snapshot_frozen_at IS NULL
    OR unixepoch(delivery_snapshot_frozen_at) IS NOT NULL
  ),
  last_error_code TEXT CHECK (
    last_error_code IS NULL OR last_error_code IN (
      'webhook.payload_invalid','webhook.delivery_unavailable'
    )
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  UNIQUE (tenant_id,file_security_event_id),
  FOREIGN KEY (tenant_id,file_security_event_id)
    REFERENCES file_security_events(tenant_id,file_security_event_id),
  CHECK (
    (dispatch_status='pending' AND dispatch_attempts BETWEEN 0 AND 9
      AND next_attempt_at IS NOT NULL AND unixepoch(next_attempt_at) IS NOT NULL
      AND dispatched_at IS NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND (last_error_code IS NULL OR last_error_code='webhook.delivery_unavailable'))
    OR (dispatch_status='processing' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL AND last_error_code IS NULL
      AND claim_token IS NOT NULL AND length(claim_token)=32
      AND claim_token NOT GLOB '*[^0-9a-f]*'
      AND lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 1 AND 128
      AND lease_owner_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
      AND lease_expires_at IS NOT NULL AND unixepoch(lease_expires_at) IS NOT NULL)
    OR (dispatch_status='dispatched' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NOT NULL
      AND unixepoch(dispatched_at) IS NOT NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL AND last_error_code IS NULL
      AND delivery_snapshot_frozen_at IS NOT NULL)
    OR (dispatch_status='failed' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND (
        last_error_code='webhook.payload_invalid'
        OR (dispatch_attempts=10 AND last_error_code='webhook.delivery_unavailable')
      ))
  )
);

-- 到期索引不含 payload，只按状态、时间和稳定 source identity 支撑跨租户公平领取。
CREATE INDEX idx_file_security_event_outbox_due_prd073_recovery
  ON file_security_event_outbox(
    dispatch_status,next_attempt_at,lease_expires_at,created_at,file_security_event_id
  ) WHERE dispatch_status IN ('pending','processing');
CREATE INDEX idx_file_security_event_outbox_tenant_status_prd073_recovery
  ON file_security_event_outbox(
    tenant_id,dispatch_status,created_at,file_security_event_id
  );

-- 新行只能是与同 tenant source 同时形成的零尝试 pending；其他状态必须经 CAS 状态机进入。
CREATE TRIGGER trg_file_security_event_outbox_validate_insert_prd073_recovery
BEFORE INSERT ON file_security_event_outbox FOR EACH ROW
WHEN NOT (
  NEW.dispatch_status='pending' AND NEW.dispatch_attempts=0
  AND NEW.next_attempt_at=NEW.created_at AND NEW.updated_at=NEW.created_at
  AND NEW.dispatched_at IS NULL AND NEW.claim_token IS NULL
  AND NEW.lease_owner_id IS NULL AND NEW.lease_expires_at IS NULL
  AND NEW.delivery_snapshot_frozen_at IS NULL AND NEW.last_error_code IS NULL
  AND EXISTS (
    SELECT 1 FROM file_security_events source
    WHERE source.tenant_id=NEW.tenant_id
      AND source.file_security_event_id=NEW.file_security_event_id
      AND source.occurred_at=NEW.created_at
  )
)
BEGIN SELECT RAISE(ABORT,'file_security_event_outbox_invalid'); END;

-- recursive_triggers=OFF 时 REPLACE 的隐式 DELETE 不可靠，INSERT 前显式封死所有身份冲突。
CREATE TRIGGER trg_file_security_event_outbox_conflict_insert_prd073_recovery
BEFORE INSERT ON file_security_event_outbox FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM file_security_event_outbox current
  WHERE current.file_security_event_id=NEW.file_security_event_id
     OR (current.tenant_id=NEW.tenant_id
         AND current.file_security_event_id=NEW.file_security_event_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'file_security_event_outbox_immutable'); END;

-- 只允许到期 claim/接管、坏 source 精确隔离、有效 lease 完成/退避及第十次孤儿失败终结。
CREATE TRIGGER trg_file_security_event_outbox_state_update_prd073_recovery
BEFORE UPDATE ON file_security_event_outbox FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid
  AND NEW.file_security_event_id=OLD.file_security_event_id
  AND NEW.tenant_id=OLD.tenant_id AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    NEW.dispatch_status NOT IN ('dispatched','failed')
    OR NOT EXISTS (
      SELECT 1 FROM webhook_deliveries delivery
      WHERE delivery.tenant_id=NEW.tenant_id
        AND delivery.event_id=NEW.file_security_event_id
        AND delivery.event_type='file.prompt_injection_detected'
        AND delivery.delivery_kind='original' AND delivery.status='pending'
    )
  )
  AND (
    (
      (
        (OLD.dispatch_status='pending'
          AND unixepoch(OLD.next_attempt_at)<=unixepoch(NEW.updated_at))
        OR (OLD.dispatch_status='processing' AND OLD.dispatch_attempts BETWEEN 1 AND 9
          AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at))
      )
      AND NEW.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts+1
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token IS NOT NULL AND length(NEW.claim_token)=32
      AND NEW.claim_token NOT GLOB '*[^0-9a-f]*'
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 128
      AND NEW.lease_owner_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90
      AND NEW.delivery_snapshot_frozen_at IS OLD.delivery_snapshot_frozen_at
      AND NEW.last_error_code IS NULL
    )
    OR (
      -- 不可变 source、scan、finding 无法交叉验证时，只允许当前有效 owner 按真实 attempt
      -- 原子终结；pending 直跳和伪造第十次都会被拒绝。
      OLD.dispatch_status='processing' AND OLD.dispatch_attempts BETWEEN 1 AND 10
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.dispatch_status='failed'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token IS NULL AND NEW.lease_owner_id IS NULL
      AND NEW.lease_expires_at IS NULL
      AND NEW.delivery_snapshot_frozen_at IS OLD.delivery_snapshot_frozen_at
      AND NEW.last_error_code='webhook.payload_invalid'
    )
    OR (
      -- Webhook 最长单次外呼 30 秒且 Endpoint 串行处理；每次出网前允许同 owner 原子续租，
      -- 使 90 秒窗口覆盖一个 attempt，而不要求首次 claim 覆盖全部 Endpoint 的总时长。
      OLD.dispatch_status='processing' AND NEW.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token=OLD.claim_token AND NEW.lease_owner_id=OLD.lease_owner_id
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.lease_expires_at)>=unixepoch(OLD.lease_expires_at)
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90
      AND NEW.delivery_snapshot_frozen_at IS OLD.delivery_snapshot_frozen_at
      AND NEW.last_error_code IS NULL
    )
    OR (
      -- 首次恢复在同一数据库事务补齐全部 delivery intent、冻结即使为空的订阅快照并续租；
      -- marker 只能由当前有效 owner 从 NULL 写入一次，后续 Endpoint 变更不得追溯旧 source。
      OLD.dispatch_status='processing' AND NEW.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token=OLD.claim_token AND NEW.lease_owner_id=OLD.lease_owner_id
      AND OLD.delivery_snapshot_frozen_at IS NULL
      AND NEW.delivery_snapshot_frozen_at=NEW.updated_at
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.lease_expires_at)>=unixepoch(OLD.lease_expires_at)
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90
      AND NEW.last_error_code IS NULL
    )
    OR (
      OLD.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.claim_token IS NULL AND NEW.lease_owner_id IS NULL
      AND NEW.lease_expires_at IS NULL
      AND NEW.delivery_snapshot_frozen_at IS OLD.delivery_snapshot_frozen_at
      AND (
        (
          unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
          AND (
            (NEW.dispatch_status='dispatched' AND NEW.dispatched_at=NEW.updated_at
              AND NEW.next_attempt_at IS NULL AND NEW.last_error_code IS NULL)
            OR (OLD.dispatch_attempts<10 AND NEW.dispatch_status='pending'
              AND NEW.dispatched_at IS NULL
              AND NEW.last_error_code='webhook.delivery_unavailable'
              AND unixepoch(NEW.next_attempt_at)=unixepoch(NEW.updated_at)+CASE
                WHEN OLD.dispatch_attempts=1 THEN 5
                WHEN OLD.dispatch_attempts=2 THEN 30
                ELSE 300 END)
            OR (OLD.dispatch_attempts=10 AND NEW.dispatch_status='failed'
              AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
              AND NEW.last_error_code='webhook.delivery_unavailable')
          )
        )
        OR (
          OLD.dispatch_attempts=10
          AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)
          AND NEW.dispatch_status='failed' AND NEW.next_attempt_at IS NULL
          AND NEW.dispatched_at IS NULL
          AND NEW.last_error_code='webhook.delivery_unavailable'
        )
      )
    )
  )
)
BEGIN SELECT RAISE(ABORT,'file_security_event_outbox_state_invalid'); END;

-- Outbox 历史不可删除；迁移 down 只在空表 guard 通过后 DROP，不经过行级删除。
CREATE TRIGGER trg_file_security_event_outbox_delete_prd073_recovery
BEFORE DELETE ON file_security_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_event_outbox_immutable'); END;
CREATE TRIGGER trg_file_security_event_outbox_rowid_prd073_recovery
AFTER INSERT ON file_security_event_outbox FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'file_security_event_outbox_immutable'); END;

-- 升级前已经提交但尚无可恢复 intent 的 source 全部回填为立即到期 pending。
INSERT INTO file_security_event_outbox(
  file_security_event_id,tenant_id,dispatch_status,dispatch_attempts,
  next_attempt_at,dispatched_at,claim_token,lease_owner_id,lease_expires_at,
  delivery_snapshot_frozen_at,last_error_code,created_at,updated_at
)
SELECT file_security_event_id,tenant_id,'pending',0,occurred_at,NULL,NULL,NULL,NULL,NULL,NULL,
       occurred_at,occurred_at
FROM file_security_events
ORDER BY occurred_at,file_security_event_id;

-- 新安全事件与 pending intent 在同一扫描事务提交或回滚，关闭进程内 task 之前的丢失窗口。
CREATE TRIGGER trg_file_security_events_create_outbox_prd073_recovery
AFTER INSERT ON file_security_events FOR EACH ROW
BEGIN
  -- trigger 内只复制固定身份和时间，source 写入失败时让同一扫描事务整体回滚。
  INSERT INTO file_security_event_outbox(
    file_security_event_id,tenant_id,dispatch_status,dispatch_attempts,
    next_attempt_at,dispatched_at,claim_token,lease_owner_id,lease_expires_at,
    delivery_snapshot_frozen_at,last_error_code,created_at,updated_at
  ) VALUES(
    NEW.file_security_event_id,NEW.tenant_id,'pending',0,NEW.occurred_at,
    NULL,NULL,NULL,NULL,NULL,NULL,NEW.occurred_at,NEW.occurred_at
  );
END;

-- 提交前证明表形状、触发器、source/outbox 全覆盖、外键和数据库完整性。
DROP TABLE IF EXISTS temp.prd073_recovery_up_final_guard;
CREATE TEMP TABLE prd073_recovery_up_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd073_recovery_up_final_guard_ok CHECK (issue_count=0)
);
-- 提交前再次聚合结构、覆盖率和完整性，防止留下部分安装的恢复状态机。
INSERT INTO prd073_recovery_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('file_security_event_outbox'))=13
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
  + CASE WHEN (SELECT COUNT(*) FROM file_security_event_outbox)
      =(SELECT COUNT(*) FROM file_security_events) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM file_security_events source WHERE NOT EXISTS (
      SELECT 1 FROM file_security_event_outbox outbox
      WHERE outbox.tenant_id=source.tenant_id
        AND outbox.file_security_event_id=source.file_security_event_id
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd073_recovery_up_final_guard;
COMMIT;
