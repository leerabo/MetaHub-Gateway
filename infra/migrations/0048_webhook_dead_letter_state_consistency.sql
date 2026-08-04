-- 迁移职责：在不改写已应用 0047 checksum、也不删除既有死信证据的前提下，
-- 加固根死信状态与 current replay delivery 终态的一致性。
-- 部署约束：服务应在迁移期间停止写入；replay_pending + terminal delivery 是崩溃恢复的
-- 合法瞬时状态，worker 会在下一次到期扫描时原子收敛，因此迁移守卫不会误删或猜测该事实。
BEGIN IMMEDIATE;

-- 既有稳定终态如果与 replay delivery 冲突，必须阻断迁移并由人工依据原始 delivery 排查；
-- 不能为了通过约束静默改写治理状态、失败码或审计证据。
CREATE TEMP TABLE prd057_replay_state_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd057_replay_state_guard
BEFORE INSERT ON prd057_replay_state_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_replay_state_mismatch'); END;
-- 守卫只统计稳定终态；replay_pending 的崩溃瞬时状态由持久 worker 到期收敛。
INSERT INTO prd057_replay_state_guard(invalid_count)
SELECT COUNT(*)
FROM webhook_dead_letter_events dead
LEFT JOIN webhook_deliveries replay
  ON replay.tenant_id=dead.tenant_id
 AND replay.webhook_delivery_id=dead.current_replay_delivery_id
WHERE dead.current_replay_delivery_id IS NOT NULL
  AND (
    replay.webhook_delivery_id IS NULL
    OR (dead.status='replayed' AND (
      replay.status!='success' OR dead.replayed_at IS NULL
    ))
    OR (dead.status='dead_letter' AND (
      replay.status!='failed' OR dead.replayed_at IS NOT NULL
    ))
    OR (dead.status IN ('archived','expired') AND NOT (
      (replay.status='success' AND dead.replayed_at IS NOT NULL)
      OR (replay.status='failed' AND dead.replayed_at IS NULL)
    ))
  );
DROP TRIGGER trg_prd057_replay_state_guard;
DROP TABLE prd057_replay_state_guard;

-- 复用 0047 的正式 trigger 名称，避免增加无法由 0047 down migration 识别的持久依赖。
DROP TRIGGER trg_webhook_dead_letters_owner_insert_prd057;
DROP TRIGGER trg_webhook_dead_letters_owner_update_prd057;

-- 插入必须同时满足同租户 owner、来源身份和根状态/replay delivery 状态关系。
CREATE TRIGGER trg_webhook_dead_letters_owner_insert_prd057
BEFORE INSERT ON webhook_dead_letter_events FOR EACH ROW
WHEN NEW.dead_letter_event_id!='whdl_' || substr(NEW.webhook_delivery_id,5)
  OR NOT EXISTS (
    SELECT 1 FROM webhook_deliveries source
    WHERE source.tenant_id=NEW.tenant_id
      AND source.webhook_delivery_id=NEW.webhook_delivery_id
      AND source.webhook_endpoint_id=NEW.webhook_endpoint_id
      AND source.event_id=NEW.event_id AND source.event_type=NEW.event_type
      AND source.status='failed' AND source.delivery_kind='original'
  )
  OR (
    NEW.current_replay_delivery_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM webhook_deliveries replay
      WHERE replay.tenant_id=NEW.tenant_id
        AND replay.webhook_endpoint_id=NEW.webhook_endpoint_id
        AND replay.webhook_delivery_id=NEW.current_replay_delivery_id
        AND replay.delivery_kind='replay'
        AND replay.replayed_from_delivery_id=NEW.webhook_delivery_id
        AND replay.event_id=NEW.event_id AND replay.event_type=NEW.event_type
        AND replay.replayed_by=NEW.replay_requested_by
        AND replay.replayed_at=NEW.replay_requested_at
        AND (
          (NEW.status='replay_pending' AND replay.status IN ('pending','success','failed'))
          OR (NEW.status='replayed' AND replay.status='success')
          OR (NEW.status='dead_letter' AND replay.status='failed')
          OR (NEW.status IN ('archived','expired') AND (
            (replay.status='success' AND NEW.replayed_at IS NOT NULL)
            OR (replay.status='failed' AND NEW.replayed_at IS NULL)
          ))
        )
    )
  )
  OR (
    NEW.replay_requested_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.replay_requested_by
    )
  )
  OR (
    NEW.archived_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.archived_by
    )
  )
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_owner_mismatch'); END;

-- 任意 UPDATE 都重新校验 owner 和状态关系；worker 必须先持久化 delivery 终态，再推进根状态。
CREATE TRIGGER trg_webhook_dead_letters_owner_update_prd057
BEFORE UPDATE ON webhook_dead_letter_events FOR EACH ROW
WHEN (
    NEW.current_replay_delivery_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM webhook_deliveries replay
      WHERE replay.tenant_id=NEW.tenant_id
        AND replay.webhook_endpoint_id=NEW.webhook_endpoint_id
        AND replay.webhook_delivery_id=NEW.current_replay_delivery_id
        AND replay.delivery_kind='replay'
        AND replay.replayed_from_delivery_id=NEW.webhook_delivery_id
        AND replay.event_id=NEW.event_id AND replay.event_type=NEW.event_type
        AND replay.replayed_by=NEW.replay_requested_by
        AND replay.replayed_at=NEW.replay_requested_at
        AND (
          (NEW.status='replay_pending' AND replay.status IN ('pending','success','failed'))
          OR (NEW.status='replayed' AND replay.status='success')
          OR (NEW.status='dead_letter' AND replay.status='failed')
          OR (NEW.status IN ('archived','expired') AND (
            (replay.status='success' AND NEW.replayed_at IS NOT NULL)
            OR (replay.status='failed' AND NEW.replayed_at IS NULL)
          ))
        )
    )
  )
  OR (
    NEW.replay_requested_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.replay_requested_by
    )
  )
  OR (
    NEW.archived_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.archived_by
    )
  )
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_owner_mismatch'); END;

COMMIT;
