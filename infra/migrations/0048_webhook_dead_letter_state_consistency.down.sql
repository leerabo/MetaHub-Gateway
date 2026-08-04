-- 回滚职责：只撤销 0048 的状态关系加固，恢复 0047 owner trigger；不删除或改写任何业务事实。
BEGIN IMMEDIATE;

DROP TRIGGER trg_webhook_dead_letters_owner_insert_prd057;
DROP TRIGGER trg_webhook_dead_letters_owner_update_prd057;

-- 恢复 0047 插入 owner 合同；身份、租户、来源 delivery 和操作者约束保持不变。
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

-- 恢复 0047 更新 owner 合同；0048 down 只放宽状态关系，不放宽租户与资源归属。
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
