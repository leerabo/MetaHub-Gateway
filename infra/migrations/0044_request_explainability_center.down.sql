-- 回滚职责：在尚未产生 RequestExplanation/查看事件事实时完整移除 PRD-054 派生结构。
-- 安全边界：一旦存在访问审计或对外投递，回滚必须原子拒绝，不能静默丢失可追踪证据。
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd054_down_fact_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd054_down_fact_guard
BEFORE INSERT ON prd054_down_fact_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd054_down_has_business_facts');
END;
-- 回滚前先确认没有租户业务事实，防止降级过程静默删除已产生的请求解释和访问事件。
INSERT INTO prd054_down_fact_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM request_explanations)
  + (SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='request.explanation_viewed');
DROP TRIGGER trg_prd054_down_fact_guard;
DROP TABLE prd054_down_fact_guard;

-- 先停止新 delivery 自动建 link，再移除 append-only 保护和派生表，避免 DDL 窗口写入半套结构。
DROP TRIGGER trg_webhook_deliveries_request_link_insert_prd054;
DROP TRIGGER trg_request_webhook_links_immutable_delete_prd054;
DROP TRIGGER trg_request_webhook_links_immutable_update_prd054;
DROP TRIGGER trg_request_webhook_links_rowid_insert_prd054;
DROP TRIGGER trg_request_webhook_links_conflict_insert_prd054;
DROP TRIGGER trg_request_webhook_links_owner_insert_prd054;
DROP TRIGGER trg_request_explanations_immutable_delete_prd054;
DROP TRIGGER trg_request_explanations_state_update_prd054;
DROP TRIGGER trg_request_explanations_rowid_insert_prd054;
DROP TRIGGER trg_request_explanations_conflict_insert_prd054;
DROP TRIGGER trg_request_explanations_owner_insert_prd054;

DROP INDEX idx_usage_ledger_request_prd054;
DROP INDEX idx_budget_scope_events_request_prd054;
DROP INDEX idx_request_webhook_links_request_time_prd054;
DROP INDEX idx_request_explanations_pending_event_prd054;
DROP INDEX idx_request_explanations_actor_time_prd054;
DROP INDEX idx_request_explanations_request_time_prd054;

-- Link 是由既有 delivery 推导的索引，可在无本 PRD 业务事实时丢弃；目标 delivery 和请求事实不动。
DROP TABLE request_webhook_delivery_links;
DROP TABLE request_explanations;

CREATE TEMP TABLE prd054_down_integrity_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd054_down_integrity_guard
BEFORE INSERT ON prd054_down_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd054_down_integrity_failed');
END;
-- 删除对象后复核表、索引和触发器均已清除，任何残留都必须让回滚事务整体失败。
INSERT INTO prd054_down_integrity_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM sqlite_master WHERE name IN (
    'request_explanations', 'request_webhook_delivery_links',
    'idx_request_explanations_request_time_prd054',
    'idx_request_explanations_actor_time_prd054',
    'idx_request_explanations_pending_event_prd054',
    'idx_request_webhook_links_request_time_prd054',
    'idx_budget_scope_events_request_prd054',
    'idx_usage_ledger_request_prd054',
    'trg_request_explanations_owner_insert_prd054',
    'trg_request_explanations_conflict_insert_prd054',
    'trg_request_explanations_rowid_insert_prd054',
    'trg_request_explanations_state_update_prd054',
    'trg_request_explanations_immutable_delete_prd054',
    'trg_request_webhook_links_owner_insert_prd054',
    'trg_request_webhook_links_conflict_insert_prd054',
    'trg_request_webhook_links_rowid_insert_prd054',
    'trg_request_webhook_links_immutable_update_prd054',
    'trg_request_webhook_links_immutable_delete_prd054',
    'trg_webhook_deliveries_request_link_insert_prd054'
  ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + CASE WHEN (SELECT integrity_check FROM pragma_integrity_check)='ok' THEN 0 ELSE 1 END;
DROP TRIGGER trg_prd054_down_integrity_guard;
DROP TABLE prd054_down_integrity_guard;

COMMIT;
