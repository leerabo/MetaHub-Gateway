-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
-- 0095 只允许 pristine 回滚；任何订单、receipt、attempt、退款或催收事实都会阻断降级。

CREATE TEMP TABLE prd103_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 回滚前验证版本、业务表全空、依赖仍完整且数据库没有外键或完整性损坏。
INSERT INTO prd103_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0095' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM subscription_orders)
  + (SELECT COUNT(*) FROM payment_intents)
  + (SELECT COUNT(*) FROM payment_webhook_receipts)
  + (SELECT COUNT(*) FROM payment_attempts)
  + (SELECT COUNT(*) FROM refunds)
  + (SELECT COUNT(*) FROM dunning_cases)
  + (SELECT COUNT(*) FROM dunning_attempts)
  + (SELECT COUNT(*) FROM commercial_effects)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd103_down_guard;

-- 按依赖逆序删除商业效果、催收、退款、attempt、receipt、intent 和正式订单。
DROP TRIGGER IF EXISTS trg_commercial_effects_delete_prd103;
DROP TRIGGER IF EXISTS trg_commercial_effects_owner_update_prd103;
DROP INDEX IF EXISTS idx_commercial_effects_outbox_prd103;
DROP TABLE IF EXISTS commercial_effects;
DROP TRIGGER IF EXISTS trg_dunning_attempts_delete_prd103;
DROP TRIGGER IF EXISTS trg_dunning_attempts_update_prd103;
DROP TABLE IF EXISTS dunning_attempts;
DROP TRIGGER IF EXISTS trg_dunning_cases_delete_prd103;
DROP INDEX IF EXISTS idx_dunning_cases_due_prd103;
DROP TABLE IF EXISTS dunning_cases;
DROP TRIGGER IF EXISTS trg_refunds_delete_prd103;
DROP TRIGGER IF EXISTS trg_refunds_update_prd103;
DROP INDEX IF EXISTS idx_refunds_order_prd103;
DROP TABLE IF EXISTS refunds;
DROP TRIGGER IF EXISTS trg_payment_attempts_delete_prd103;
DROP TRIGGER IF EXISTS trg_payment_attempts_update_prd103;
DROP INDEX IF EXISTS idx_payment_attempts_intent_prd103;
DROP TABLE IF EXISTS payment_attempts;
DROP TRIGGER IF EXISTS trg_payment_receipts_delete_prd103;
DROP TRIGGER IF EXISTS trg_payment_receipts_owner_update_prd103;
DROP INDEX IF EXISTS idx_payment_receipts_worker_prd103;
DROP TABLE IF EXISTS payment_webhook_receipts;
DROP TRIGGER IF EXISTS trg_payment_intents_delete_prd103;
DROP TRIGGER IF EXISTS trg_payment_intents_update_prd103;
DROP INDEX IF EXISTS idx_payment_intents_order_prd103;
DROP TABLE IF EXISTS payment_intents;
DROP TRIGGER IF EXISTS trg_subscription_orders_delete_prd103;
DROP TRIGGER IF EXISTS trg_subscription_orders_update_prd103;
DROP INDEX IF EXISTS idx_subscription_orders_workspace_prd103;
DROP TABLE IF EXISTS subscription_orders;

CREATE TEMP TABLE prd103_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 最终守卫确认所有 0095 对象消失，并仍可安全恢复到 0094 代码。
INSERT INTO prd103_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd103')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_subscriptions','subscription_order_records','entitlement_snapshots'))=3
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd103_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
