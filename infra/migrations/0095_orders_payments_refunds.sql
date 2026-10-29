-- 建立订单、支付回调、催收、退款和 exactly-once 商业效果的权威事实。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd103_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 前置守卫要求 0094 为当前版本，并确认订阅、权益、Workspace 与平台账务依赖完整。
INSERT INTO prd103_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0094' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_subscriptions','subscription_order_records','entitlement_snapshots',
      'workspace_tenant_bindings','workspace_memberships','platform_operators'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd103')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd103_up_guard;

-- 正式订单冻结原币小单位金额、计划版本和规范 intent，旧 0090 记录只作为不可变来源。
CREATE TABLE subscription_orders(
  order_id TEXT PRIMARY KEY CHECK(length(order_id)=36 AND substr(order_id,1,4)='ord_'),
  source_order_record_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  plan_version_id TEXT NOT NULL,
  order_kind TEXT NOT NULL CHECK(order_kind IN('create','upgrade','downgrade','renewal')),
  status TEXT NOT NULL CHECK(status IN(
    'pending_payment','paid','fulfilled','past_due','canceled','refunded','partially_refunded')),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor BETWEEN 0 AND 1000000000000),
  captured_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK(
    captured_amount_minor BETWEEN 0 AND amount_minor),
  refunded_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK(
    refunded_amount_minor BETWEEN 0 AND captured_amount_minor),
  canonical_intent_sha256 TEXT NOT NULL CHECK(length(canonical_intent_sha256)=64),
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  created_by_membership_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(source_order_record_id) REFERENCES subscription_order_records(order_record_id),
  FOREIGN KEY(workspace_id,tenant_id,subscription_id)
    REFERENCES workspace_subscriptions(workspace_id,tenant_id,subscription_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  FOREIGN KEY(created_by_membership_id) REFERENCES workspace_memberships(workspace_membership_id)
);
CREATE INDEX idx_subscription_orders_workspace_prd103
  ON subscription_orders(workspace_id,created_at DESC,order_id);
CREATE TRIGGER trg_subscription_orders_update_prd103
BEFORE UPDATE ON subscription_orders FOR EACH ROW
WHEN NEW.order_id!=OLD.order_id OR NEW.source_order_record_id!=OLD.source_order_record_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.subscription_id!=OLD.subscription_id OR NEW.plan_version_id!=OLD.plan_version_id
  OR NEW.order_kind!=OLD.order_kind OR NEW.currency!=OLD.currency
  OR NEW.amount_minor!=OLD.amount_minor OR NEW.canonical_intent_sha256!=OLD.canonical_intent_sha256
  OR NEW.created_by_membership_id!=OLD.created_by_membership_id OR NEW.created_at!=OLD.created_at
  OR NEW.state_revision!=OLD.state_revision+1
  OR NEW.captured_amount_minor<OLD.captured_amount_minor
  OR NEW.refunded_amount_minor<OLD.refunded_amount_minor
  OR NOT(
    (OLD.status='pending_payment' AND NEW.status IN(
      'pending_payment','paid','fulfilled','past_due','canceled'))
    OR(OLD.status='paid' AND NEW.status IN('paid','fulfilled','partially_refunded','refunded'))
    OR(OLD.status='fulfilled' AND NEW.status IN('fulfilled','partially_refunded','refunded'))
    OR(OLD.status='past_due' AND NEW.status IN(
      'past_due','pending_payment','paid','fulfilled','canceled'))
    OR(OLD.status='partially_refunded' AND NEW.status IN('partially_refunded','refunded'))
  )
BEGIN SELECT RAISE(ABORT,'subscription_order_state_invalid'); END;
CREATE TRIGGER trg_subscription_orders_delete_prd103
BEFORE DELETE ON subscription_orders
BEGIN SELECT RAISE(ABORT,'subscription_order_immutable'); END;

-- 支付意图只保存一次性 client token 的 HMAC，不保存支付方式或渠道 client secret。
CREATE TABLE payment_intents(
  payment_intent_id TEXT PRIMARY KEY CHECK(
    length(payment_intent_id)=36 AND substr(payment_intent_id,1,4)='pmi_'),
  order_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  provider TEXT NOT NULL CHECK(provider IN('mockpay','cloudpay')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor BETWEEN 1 AND 1000000000000),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  client_token_hmac TEXT NOT NULL UNIQUE CHECK(length(client_token_hmac)=64),
  status TEXT NOT NULL CHECK(status IN(
    'requires_action','processing','succeeded','failed','canceled')),
  provider_event_version INTEGER NOT NULL DEFAULT 0 CHECK(provider_event_version>=0),
  provider_occurred_at TEXT CHECK(
    provider_occurred_at IS NULL OR unixepoch(provider_occurred_at) IS NOT NULL),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_by_membership_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(workspace_id,idempotency_sha256),
  FOREIGN KEY(order_id) REFERENCES subscription_orders(order_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_membership_id) REFERENCES workspace_memberships(workspace_membership_id)
);
CREATE INDEX idx_payment_intents_order_prd103
  ON payment_intents(order_id,created_at DESC,payment_intent_id);
CREATE TRIGGER trg_payment_intents_update_prd103
BEFORE UPDATE ON payment_intents FOR EACH ROW
WHEN NEW.payment_intent_id!=OLD.payment_intent_id OR NEW.order_id!=OLD.order_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.provider!=OLD.provider OR NEW.amount_minor!=OLD.amount_minor
  OR NEW.currency!=OLD.currency OR NEW.client_token_hmac!=OLD.client_token_hmac
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256 OR NEW.request_sha256!=OLD.request_sha256
  OR NEW.expires_at!=OLD.expires_at OR NEW.created_by_membership_id!=OLD.created_by_membership_id
  OR NEW.created_at!=OLD.created_at OR NEW.state_revision!=OLD.state_revision+1
  OR NEW.provider_event_version<OLD.provider_event_version
  OR NOT(
    (OLD.status='requires_action' AND NEW.status IN(
      'requires_action','processing','succeeded','failed','canceled'))
    OR(OLD.status='processing' AND NEW.status IN('processing','succeeded','failed','canceled'))
    OR(OLD.status='failed' AND NEW.status IN('failed','processing','succeeded'))
    OR(OLD.status=NEW.status AND OLD.status IN('succeeded','canceled'))
  )
BEGIN SELECT RAISE(ABORT,'payment_intent_state_invalid'); END;
CREATE TRIGGER trg_payment_intents_delete_prd103
BEFORE DELETE ON payment_intents
BEGIN SELECT RAISE(ABORT,'payment_intent_immutable'); END;

-- Receipt 在验签后先持久化，再由 lease worker 解密处理；同 provider/event id 只能有一份正文。
CREATE TABLE payment_webhook_receipts(
  receipt_id TEXT PRIMARY KEY CHECK(length(receipt_id)=36 AND substr(receipt_id,1,4)='pwr_'),
  provider TEXT NOT NULL CHECK(provider IN('mockpay','cloudpay')),
  provider_event_id TEXT NOT NULL CHECK(length(provider_event_id) BETWEEN 8 AND 128),
  provider_event_version INTEGER NOT NULL CHECK(provider_event_version>=1),
  provider_occurred_at TEXT NOT NULL CHECK(unixepoch(provider_occurred_at) IS NOT NULL),
  raw_body_object_reference TEXT NOT NULL UNIQUE CHECK(
    length(raw_body_object_reference) BETWEEN 20 AND 160),
  raw_body_sha256 TEXT NOT NULL CHECK(length(raw_body_sha256)=64),
  signature_key_version INTEGER NOT NULL CHECK(signature_key_version>=1),
  encryption_key_version INTEGER NOT NULL CHECK(encryption_key_version>=1),
  status TEXT NOT NULL CHECK(status IN('pending','processing','completed','conflict','failed')),
  processing_owner TEXT,
  processing_lease_until TEXT CHECK(
    processing_lease_until IS NULL OR unixepoch(processing_lease_until) IS NOT NULL),
  safe_error_code TEXT CHECK(safe_error_code IS NULL OR safe_error_code IN(
    'payment.event_invalid','payment.owner_mismatch','payment.state_conflict','payment.storage_error')),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  processed_at TEXT CHECK(processed_at IS NULL OR unixepoch(processed_at) IS NOT NULL),
  UNIQUE(provider,provider_event_id),
  CHECK((status IN('pending','completed','conflict','failed')
      AND processing_owner IS NULL AND processing_lease_until IS NULL)
    OR(status='processing' AND processing_owner IS NOT NULL
      AND processing_lease_until IS NOT NULL))
);
CREATE INDEX idx_payment_receipts_worker_prd103
  ON payment_webhook_receipts(status,processing_lease_until,received_at,receipt_id);
CREATE TRIGGER trg_payment_receipts_owner_update_prd103
BEFORE UPDATE ON payment_webhook_receipts FOR EACH ROW
WHEN NEW.receipt_id!=OLD.receipt_id OR NEW.provider!=OLD.provider
  OR NEW.provider_event_id!=OLD.provider_event_id
  OR NEW.provider_event_version!=OLD.provider_event_version
  OR NEW.provider_occurred_at!=OLD.provider_occurred_at
  OR NEW.raw_body_object_reference!=OLD.raw_body_object_reference
  OR NEW.raw_body_sha256!=OLD.raw_body_sha256
  OR NEW.signature_key_version!=OLD.signature_key_version
  OR NEW.encryption_key_version!=OLD.encryption_key_version OR NEW.received_at!=OLD.received_at
BEGIN SELECT RAISE(ABORT,'payment_receipt_owner_immutable'); END;
CREATE TRIGGER trg_payment_receipts_delete_prd103
BEFORE DELETE ON payment_webhook_receipts
BEGIN SELECT RAISE(ABORT,'payment_receipt_immutable'); END;

-- 每个已验证 receipt 追加一条渠道 attempt，旧事件也保留审计事实但不得倒退意图状态。
CREATE TABLE payment_attempts(
  payment_attempt_id TEXT PRIMARY KEY CHECK(
    length(payment_attempt_id)=36 AND substr(payment_attempt_id,1,4)='pat_'),
  payment_intent_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL UNIQUE,
  provider_status TEXT NOT NULL CHECK(provider_status IN(
    'requires_action','processing','succeeded','failed','canceled','ignored_stale')),
  provider_event_version INTEGER NOT NULL CHECK(provider_event_version>=1),
  provider_occurred_at TEXT NOT NULL CHECK(unixepoch(provider_occurred_at) IS NOT NULL),
  safe_error_code TEXT CHECK(safe_error_code IS NULL OR length(safe_error_code) BETWEEN 3 AND 64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(payment_intent_id) REFERENCES payment_intents(payment_intent_id),
  FOREIGN KEY(receipt_id) REFERENCES payment_webhook_receipts(receipt_id)
);
CREATE INDEX idx_payment_attempts_intent_prd103
  ON payment_attempts(payment_intent_id,provider_event_version,payment_attempt_id);
CREATE TRIGGER trg_payment_attempts_update_prd103
BEFORE UPDATE ON payment_attempts
BEGIN SELECT RAISE(ABORT,'payment_attempt_immutable'); END;
CREATE TRIGGER trg_payment_attempts_delete_prd103
BEFORE DELETE ON payment_attempts
BEGIN SELECT RAISE(ABORT,'payment_attempt_immutable'); END;

-- 退款申请冻结订单 owner、原币金额和幂等 intent；客户不能写入渠道引用或审核状态。
CREATE TABLE refunds(
  refund_id TEXT PRIMARY KEY CHECK(length(refund_id)=36 AND substr(refund_id,1,4)='rfd_'),
  order_id TEXT NOT NULL,
  payment_intent_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK(amount_minor BETWEEN 1 AND 1000000000000),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'customer_request','service_failure','duplicate_charge')),
  status TEXT NOT NULL CHECK(status IN('requested','processing','succeeded','failed','rejected')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  approved_by_operator_id TEXT,
  decision_reason_code TEXT CHECK(decision_reason_code IS NULL OR decision_reason_code IN(
    'policy_approved','amount_invalid','evidence_missing')),
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  requested_by_membership_id TEXT NOT NULL,
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(workspace_id,idempotency_sha256),
  FOREIGN KEY(order_id) REFERENCES subscription_orders(order_id),
  FOREIGN KEY(payment_intent_id) REFERENCES payment_intents(payment_intent_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(requested_by_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  CHECK((status='requested' AND approved_by_operator_id IS NULL AND decision_reason_code IS NULL)
    OR(status IN('processing','succeeded','failed','rejected')
      AND approved_by_operator_id IS NOT NULL AND decision_reason_code IS NOT NULL))
);
CREATE INDEX idx_refunds_order_prd103 ON refunds(order_id,status,requested_at,refund_id);
CREATE TRIGGER trg_refunds_update_prd103
BEFORE UPDATE ON refunds FOR EACH ROW
WHEN NEW.refund_id!=OLD.refund_id OR NEW.order_id!=OLD.order_id
  OR NEW.payment_intent_id!=OLD.payment_intent_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.amount_minor!=OLD.amount_minor
  OR NEW.currency!=OLD.currency OR NEW.reason_code!=OLD.reason_code
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256 OR NEW.request_sha256!=OLD.request_sha256
  OR NEW.requested_by_membership_id!=OLD.requested_by_membership_id
  OR NEW.requested_at!=OLD.requested_at OR NEW.state_revision!=OLD.state_revision+1
  OR NOT((OLD.status='requested' AND NEW.status IN('processing','rejected'))
    OR(OLD.status='processing' AND NEW.status IN('succeeded','failed')))
BEGIN SELECT RAISE(ABORT,'refund_state_invalid'); END;
CREATE TRIGGER trg_refunds_delete_prd103
BEFORE DELETE ON refunds
BEGIN SELECT RAISE(ABORT,'refund_immutable'); END;

-- 催收案件与尝试只保存固定模板和安全结果码，通知失败不能回滚支付事实。
CREATE TABLE dunning_cases(
  dunning_case_id TEXT PRIMARY KEY CHECK(
    length(dunning_case_id)=36 AND substr(dunning_case_id,1,4)='dun_'),
  order_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('open','recovered','closed')),
  cycle_number INTEGER NOT NULL CHECK(cycle_number BETWEEN 1 AND 12),
  template_reference TEXT NOT NULL CHECK(length(template_reference) BETWEEN 8 AND 96),
  next_action_at TEXT CHECK(next_action_at IS NULL OR unixepoch(next_action_at) IS NOT NULL),
  grace_policy_reference TEXT NOT NULL CHECK(length(grace_policy_reference) BETWEEN 8 AND 96),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(order_id) REFERENCES subscription_orders(order_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_dunning_cases_due_prd103
  ON dunning_cases(status,next_action_at,dunning_case_id);
CREATE TRIGGER trg_dunning_cases_delete_prd103
BEFORE DELETE ON dunning_cases
BEGIN SELECT RAISE(ABORT,'dunning_case_immutable'); END;

-- 每次催收动作追加记录，模板发送失败仅改变安全结果，不影响订单或支付状态。
CREATE TABLE dunning_attempts(
  dunning_attempt_id TEXT PRIMARY KEY CHECK(
    length(dunning_attempt_id)=36 AND substr(dunning_attempt_id,1,4)='dua_'),
  dunning_case_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 12),
  action_code TEXT NOT NULL CHECK(action_code IN('notify','retry_payment','grace_expired')),
  template_reference TEXT NOT NULL CHECK(length(template_reference) BETWEEN 8 AND 96),
  result_code TEXT NOT NULL CHECK(result_code IN('pending','sent','failed','skipped')),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(dunning_case_id) REFERENCES dunning_cases(dunning_case_id),
  UNIQUE(dunning_case_id,attempt_number)
);
CREATE TRIGGER trg_dunning_attempts_update_prd103
BEFORE UPDATE ON dunning_attempts
BEGIN SELECT RAISE(ABORT,'dunning_attempt_immutable'); END;
CREATE TRIGGER trg_dunning_attempts_delete_prd103
BEFORE DELETE ON dunning_attempts
BEGIN SELECT RAISE(ABORT,'dunning_attempt_immutable'); END;

-- 商业效果唯一键把 capture/refund/dunning 与权益、账务和通知 outbox 原子绑定。
CREATE TABLE commercial_effects(
  commercial_effect_id TEXT PRIMARY KEY CHECK(
    length(commercial_effect_id)=36 AND substr(commercial_effect_id,1,4)='cef_'),
  effect_key TEXT NOT NULL UNIQUE CHECK(length(effect_key) BETWEEN 16 AND 160),
  source_type TEXT NOT NULL CHECK(source_type IN('receipt','refund','order')),
  source_id TEXT NOT NULL CHECK(length(source_id) BETWEEN 8 AND 128),
  order_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  effect_type TEXT NOT NULL CHECK(effect_type IN(
    'capture_fulfilled','refund_ledger','dunning_scheduled','entitlement_command')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor BETWEEN 0 AND 1000000000000),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  outbox_status TEXT NOT NULL CHECK(outbox_status IN('pending','completed','failed')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(order_id) REFERENCES subscription_orders(order_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((outbox_status='pending' AND completed_at IS NULL)
    OR(outbox_status IN('completed','failed') AND completed_at IS NOT NULL))
);
CREATE INDEX idx_commercial_effects_outbox_prd103
  ON commercial_effects(outbox_status,created_at,commercial_effect_id);
CREATE TRIGGER trg_commercial_effects_owner_update_prd103
BEFORE UPDATE ON commercial_effects FOR EACH ROW
WHEN NEW.commercial_effect_id!=OLD.commercial_effect_id OR NEW.effect_key!=OLD.effect_key
  OR NEW.source_type!=OLD.source_type OR NEW.source_id!=OLD.source_id
  OR NEW.order_id!=OLD.order_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.effect_type!=OLD.effect_type
  OR NEW.amount_minor!=OLD.amount_minor OR NEW.currency!=OLD.currency
  OR NEW.created_at!=OLD.created_at OR OLD.outbox_status!='pending'
  OR NEW.outbox_status NOT IN('completed','failed') OR NEW.completed_at IS NULL
BEGIN SELECT RAISE(ABORT,'commercial_effect_immutable'); END;
CREATE TRIGGER trg_commercial_effects_delete_prd103
BEFORE DELETE ON commercial_effects
BEGIN SELECT RAISE(ABORT,'commercial_effect_immutable'); END;

CREATE TEMP TABLE prd103_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 最终守卫确认 8 张商业事实表、唯一索引、外键和数据库完整性同时成立。
INSERT INTO prd103_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'subscription_orders','payment_intents','payment_webhook_receipts','payment_attempts',
    'refunds','dunning_cases','dunning_attempts','commercial_effects'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd103_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
