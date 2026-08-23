-- 建立不可变结算单、发票 provider 产物、单次下载、双人冲红和冻结水位对账事实。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd104_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd104_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0095' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_billing_cycles','platform_billing_summaries','platform_meter_readings',
      'platform_billing_adjustments','workspace_billing_profiles',
      'business_verification_intents','subscription_orders','payment_intents',
      'payment_attempts','platform_operators','workspace_sessions'))=11 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd104')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd104_up_guard;

-- Statement 冻结 cycle、Meter summary set、税率和原币小单位金额；更正只能追加新版本。
CREATE TABLE billing_statements(
  billing_statement_id TEXT PRIMARY KEY CHECK(
    length(billing_statement_id)=36 AND substr(billing_statement_id,1,4)='bst_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  billing_cycle_id TEXT NOT NULL,
  statement_kind TEXT NOT NULL CHECK(statement_kind IN('standard','credit','replacement')),
  predecessor_statement_id TEXT,
  immutable_version INTEGER NOT NULL CHECK(immutable_version>=1),
  meter_summary_set_sha256 TEXT NOT NULL CHECK(length(meter_summary_set_sha256)=64),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  subtotal_minor INTEGER NOT NULL CHECK(subtotal_minor BETWEEN -1000000000000 AND 1000000000000),
  tax_rate_basis_points INTEGER NOT NULL CHECK(tax_rate_basis_points BETWEEN 0 AND 10000),
  tax_minor INTEGER NOT NULL CHECK(tax_minor BETWEEN -1000000000000 AND 1000000000000),
  total_minor INTEGER NOT NULL CHECK(total_minor BETWEEN -1000000000000 AND 1000000000000),
  status TEXT NOT NULL CHECK(status='published'),
  statement_sha256 TEXT NOT NULL UNIQUE CHECK(length(statement_sha256)=64),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,billing_cycle_id)
    REFERENCES platform_billing_cycles(workspace_id,tenant_id,billing_cycle_id),
  FOREIGN KEY(predecessor_statement_id) REFERENCES billing_statements(billing_statement_id),
  UNIQUE(billing_cycle_id,immutable_version),
  UNIQUE(billing_statement_id,workspace_id,tenant_id),
  CHECK(total_minor=subtotal_minor+tax_minor),
  CHECK((statement_kind='standard' AND immutable_version=1 AND predecessor_statement_id IS NULL)
    OR(statement_kind!='standard' AND immutable_version>1 AND predecessor_statement_id IS NOT NULL))
);
CREATE INDEX idx_billing_statements_workspace_prd104
  ON billing_statements(workspace_id,issued_at DESC,billing_statement_id);
CREATE TRIGGER trg_billing_statements_update_prd104 BEFORE UPDATE ON billing_statements
BEGIN SELECT RAISE(ABORT,'billing_statement_immutable'); END;
CREATE TRIGGER trg_billing_statements_delete_prd104 BEFORE DELETE ON billing_statements
BEGIN SELECT RAISE(ABORT,'billing_statement_immutable'); END;

-- 发票申请锁定 Statement 与已经验证的 BillingProfile revision，金额和 owner 不来自浏览器。
CREATE TABLE invoice_requests(
  invoice_request_id TEXT PRIMARY KEY CHECK(
    length(invoice_request_id)=36 AND substr(invoice_request_id,1,4)='ivr_'),
  billing_statement_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_billing_profile_id TEXT NOT NULL,
  billing_profile_revision INTEGER NOT NULL CHECK(billing_profile_revision>=1),
  invoice_type TEXT NOT NULL CHECK(invoice_type IN('electronic_general','electronic_special')),
  provider TEXT NOT NULL CHECK(provider IN('mocktax','cloudtax')),
  status TEXT NOT NULL CHECK(status IN(
    'requested','processing','issued','failed','reversal_pending','reversed')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  requested_by_membership_id TEXT NOT NULL,
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(workspace_id,idempotency_sha256),
  UNIQUE(invoice_request_id,workspace_id,tenant_id),
  FOREIGN KEY(billing_statement_id,workspace_id,tenant_id)
    REFERENCES billing_statements(billing_statement_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_billing_profile_id) REFERENCES workspace_billing_profiles(
    workspace_billing_profile_id),
  FOREIGN KEY(requested_by_membership_id) REFERENCES workspace_memberships(workspace_membership_id)
);
CREATE INDEX idx_invoice_requests_workspace_prd104
  ON invoice_requests(workspace_id,requested_at DESC,invoice_request_id);
CREATE INDEX idx_invoice_requests_platform_prd104
  ON invoice_requests(status,updated_at,invoice_request_id);
CREATE TRIGGER trg_invoice_requests_update_prd104
BEFORE UPDATE ON invoice_requests FOR EACH ROW
WHEN NEW.invoice_request_id!=OLD.invoice_request_id
  OR NEW.billing_statement_id!=OLD.billing_statement_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.workspace_billing_profile_id!=OLD.workspace_billing_profile_id
  OR NEW.billing_profile_revision!=OLD.billing_profile_revision
  OR NEW.invoice_type!=OLD.invoice_type OR NEW.provider!=OLD.provider
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256 OR NEW.request_sha256!=OLD.request_sha256
  OR NEW.requested_by_membership_id!=OLD.requested_by_membership_id
  OR NEW.requested_at!=OLD.requested_at OR NEW.state_revision!=OLD.state_revision+1
  OR NOT((OLD.status='requested' AND NEW.status IN('processing','failed'))
    OR(OLD.status='processing' AND NEW.status IN('issued','failed'))
    OR(OLD.status='issued' AND NEW.status='reversal_pending')
    OR(OLD.status='reversal_pending' AND NEW.status IN('issued','reversed')))
BEGIN SELECT RAISE(ABORT,'invoice_request_state_invalid'); END;
CREATE TRIGGER trg_invoice_requests_delete_prd104 BEFORE DELETE ON invoice_requests
BEGIN SELECT RAISE(ABORT,'invoice_request_immutable'); END;

-- Provider 命令采用 lease outbox；effect key 使开票与红字命令在进程崩溃重试后仍唯一。
CREATE TABLE invoice_provider_outbox(
  invoice_provider_outbox_id TEXT PRIMARY KEY CHECK(
    length(invoice_provider_outbox_id)=36 AND substr(invoice_provider_outbox_id,1,4)='ipo_'),
  invoice_request_id TEXT NOT NULL,
  reversal_request_id TEXT,
  command_type TEXT NOT NULL CHECK(command_type IN('issue_invoice','reverse_invoice')),
  effect_key TEXT NOT NULL UNIQUE CHECK(length(effect_key) BETWEEN 16 AND 160),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending','processing','completed','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  lease_owner TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  safe_error_code TEXT CHECK(safe_error_code IS NULL OR safe_error_code IN(
    'invoice.provider_unavailable','invoice.provider_rejected','invoice.storage_unavailable')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(invoice_request_id) REFERENCES invoice_requests(invoice_request_id),
  FOREIGN KEY(reversal_request_id)
    REFERENCES invoice_reversal_requests(invoice_reversal_request_id),
  CHECK((command_type='issue_invoice' AND reversal_request_id IS NULL)
    OR(command_type='reverse_invoice' AND reversal_request_id IS NOT NULL)),
  CHECK((status='processing' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND safe_error_code IS NULL)
    OR(status IN('pending','completed') AND lease_owner IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND safe_error_code IS NULL)
    OR(status='failed' AND lease_owner IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND safe_error_code IS NOT NULL))
);
CREATE INDEX idx_invoice_provider_outbox_worker_prd104
  ON invoice_provider_outbox(status,lease_expires_at,updated_at,invoice_provider_outbox_id);
CREATE TRIGGER trg_invoice_provider_outbox_owner_prd104
BEFORE UPDATE ON invoice_provider_outbox FOR EACH ROW
WHEN NEW.invoice_provider_outbox_id!=OLD.invoice_provider_outbox_id
  OR NEW.invoice_request_id!=OLD.invoice_request_id
  OR NEW.reversal_request_id IS NOT OLD.reversal_request_id
  OR NEW.command_type!=OLD.command_type OR NEW.effect_key!=OLD.effect_key
  OR NEW.payload_sha256!=OLD.payload_sha256 OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count
BEGIN SELECT RAISE(ABORT,'invoice_provider_outbox_owner_immutable'); END;
CREATE TRIGGER trg_invoice_provider_outbox_delete_prd104 BEFORE DELETE ON invoice_provider_outbox
BEGIN SELECT RAISE(ABORT,'invoice_provider_outbox_immutable'); END;

-- Provider receipt 先落库；重复 event id 只能对应同一正文 hash，不保存税务正文。
CREATE TABLE invoice_provider_receipts(
  invoice_provider_receipt_id TEXT PRIMARY KEY CHECK(
    length(invoice_provider_receipt_id)=36 AND substr(invoice_provider_receipt_id,1,4)='ipr_'),
  invoice_request_id TEXT NOT NULL,
  provider TEXT NOT NULL CHECK(provider IN('mocktax','cloudtax')),
  provider_event_id TEXT NOT NULL CHECK(length(provider_event_id) BETWEEN 8 AND 128),
  provider_event_version INTEGER NOT NULL CHECK(provider_event_version>=1),
  event_type TEXT NOT NULL CHECK(event_type IN('invoice.issued','invoice.reversed','invoice.failed')),
  raw_body_sha256 TEXT NOT NULL CHECK(length(raw_body_sha256)=64),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  UNIQUE(provider,provider_event_id),
  FOREIGN KEY(invoice_request_id) REFERENCES invoice_requests(invoice_request_id)
);
CREATE INDEX idx_invoice_provider_receipts_request_prd104
  ON invoice_provider_receipts(invoice_request_id,provider_event_version,invoice_provider_receipt_id);
CREATE TRIGGER trg_invoice_provider_receipts_update_prd104 BEFORE UPDATE ON invoice_provider_receipts
BEGIN SELECT RAISE(ABORT,'invoice_provider_receipt_immutable'); END;
CREATE TRIGGER trg_invoice_provider_receipts_delete_prd104 BEFORE DELETE ON invoice_provider_receipts
BEGIN SELECT RAISE(ABORT,'invoice_provider_receipt_immutable'); END;

-- 每个 provider 产物为不可变对象；对象引用加密，号码只保存掩码，PDF/CSV hash 保真。
CREATE TABLE invoice_artifacts(
  invoice_artifact_id TEXT PRIMARY KEY CHECK(
    length(invoice_artifact_id)=36 AND substr(invoice_artifact_id,1,4)='ina_'),
  invoice_request_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  provider_receipt_id TEXT NOT NULL,
  predecessor_artifact_id TEXT,
  artifact_kind TEXT NOT NULL CHECK(artifact_kind IN('invoice_pdf','invoice_csv','red_letter_pdf')),
  provider_invoice_id_token TEXT NOT NULL UNIQUE CHECK(length(provider_invoice_id_token)=64),
  invoice_number_masked TEXT NOT NULL CHECK(length(invoice_number_masked) BETWEEN 4 AND 32),
  object_reference_ciphertext TEXT NOT NULL CHECK(length(object_reference_ciphertext) BETWEEN 64 AND 8192),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  mime_type TEXT NOT NULL CHECK(mime_type IN('application/pdf','text/csv')),
  size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 20971520),
  kms_key_version INTEGER NOT NULL CHECK(kms_key_version>=1),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  UNIQUE(invoice_artifact_id,workspace_id,tenant_id),
  FOREIGN KEY(invoice_request_id,workspace_id,tenant_id)
    REFERENCES invoice_requests(invoice_request_id,workspace_id,tenant_id),
  FOREIGN KEY(provider_receipt_id) REFERENCES invoice_provider_receipts(
    invoice_provider_receipt_id),
  FOREIGN KEY(predecessor_artifact_id) REFERENCES invoice_artifacts(invoice_artifact_id),
  CHECK((artifact_kind='invoice_csv' AND mime_type='text/csv')
    OR(artifact_kind!='invoice_csv' AND mime_type='application/pdf'))
);
CREATE INDEX idx_invoice_artifacts_request_prd104
  ON invoice_artifacts(invoice_request_id,issued_at,invoice_artifact_id);
CREATE TRIGGER trg_invoice_artifacts_update_prd104 BEFORE UPDATE ON invoice_artifacts
BEGIN SELECT RAISE(ABORT,'invoice_artifact_immutable'); END;
CREATE TRIGGER trg_invoice_artifacts_delete_prd104 BEFORE DELETE ON invoice_artifacts
BEGIN SELECT RAISE(ABORT,'invoice_artifact_immutable'); END;

-- 下载 token 只保存 HMAC，绑定 WorkspaceSession、tenant 与 artifact，并且只能 active->used/expired。
CREATE TABLE invoice_download_grants(
  invoice_download_grant_id TEXT PRIMARY KEY CHECK(
    length(invoice_download_grant_id)=36 AND substr(invoice_download_grant_id,1,4)='idg_'),
  invoice_artifact_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_session_id TEXT NOT NULL,
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64),
  status TEXT NOT NULL CHECK(status IN('active','used','expired')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  used_at TEXT CHECK(used_at IS NULL OR unixepoch(used_at) IS NOT NULL),
  FOREIGN KEY(invoice_artifact_id,workspace_id,tenant_id)
    REFERENCES invoice_artifacts(invoice_artifact_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_session_id) REFERENCES workspace_sessions(workspace_session_id),
  CHECK((status='active' AND used_at IS NULL) OR(status='used' AND used_at IS NOT NULL)
    OR(status='expired' AND used_at IS NULL))
);
CREATE INDEX idx_invoice_download_grants_expiry_prd104
  ON invoice_download_grants(status,expires_at,invoice_download_grant_id);
CREATE TRIGGER trg_invoice_download_grants_update_prd104
BEFORE UPDATE ON invoice_download_grants FOR EACH ROW
WHEN NEW.invoice_download_grant_id!=OLD.invoice_download_grant_id
  OR NEW.invoice_artifact_id!=OLD.invoice_artifact_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.workspace_session_id!=OLD.workspace_session_id OR NEW.token_hmac!=OLD.token_hmac
  OR NEW.expires_at!=OLD.expires_at OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status NOT IN('used','expired')
BEGIN SELECT RAISE(ABORT,'invoice_download_grant_immutable'); END;
CREATE TRIGGER trg_invoice_download_grants_delete_prd104 BEFORE DELETE ON invoice_download_grants
BEGIN SELECT RAISE(ABORT,'invoice_download_grant_immutable'); END;

-- 红字申请人不能审批；只有 billing 与 compliance 两个不同 domain 都批准才可出队。
CREATE TABLE invoice_reversal_requests(
  invoice_reversal_request_id TEXT PRIMARY KEY CHECK(
    length(invoice_reversal_request_id)=36 AND substr(invoice_reversal_request_id,1,4)='irr_'),
  invoice_request_id TEXT NOT NULL UNIQUE,
  requested_by_operator_id TEXT NOT NULL,
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'invoice_error','customer_correction','contract_canceled')),
  status TEXT NOT NULL CHECK(status IN('pending_approvals','queued','completed','rejected')),
  idempotency_sha256 TEXT NOT NULL UNIQUE CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(invoice_request_id) REFERENCES invoice_requests(invoice_request_id),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE INDEX idx_invoice_reversals_status_prd104
  ON invoice_reversal_requests(status,requested_at,invoice_reversal_request_id);
CREATE TRIGGER trg_invoice_reversals_update_prd104
BEFORE UPDATE ON invoice_reversal_requests FOR EACH ROW
WHEN NEW.invoice_reversal_request_id!=OLD.invoice_reversal_request_id
  OR NEW.invoice_request_id!=OLD.invoice_request_id
  OR NEW.requested_by_operator_id!=OLD.requested_by_operator_id
  OR NEW.reason_code!=OLD.reason_code OR NEW.idempotency_sha256!=OLD.idempotency_sha256
  OR NEW.request_sha256!=OLD.request_sha256 OR NEW.requested_at!=OLD.requested_at
  OR NEW.state_revision!=OLD.state_revision+1
  OR NOT((OLD.status='pending_approvals' AND NEW.status IN('queued','rejected'))
    OR(OLD.status='queued' AND NEW.status='completed'))
BEGIN SELECT RAISE(ABORT,'invoice_reversal_state_invalid'); END;
CREATE TRIGGER trg_invoice_reversals_delete_prd104 BEFORE DELETE ON invoice_reversal_requests
BEGIN SELECT RAISE(ABORT,'invoice_reversal_immutable'); END;

CREATE TABLE invoice_reversal_approvals(
  invoice_reversal_approval_id TEXT PRIMARY KEY CHECK(
    length(invoice_reversal_approval_id)=36 AND substr(invoice_reversal_approval_id,1,4)='ira_'),
  invoice_reversal_request_id TEXT NOT NULL,
  approval_domain TEXT NOT NULL CHECK(approval_domain IN('billing','compliance')),
  operator_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN('approve','reject')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'facts_confirmed','policy_confirmed','evidence_missing','policy_blocked')),
  idempotency_sha256 TEXT NOT NULL UNIQUE CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(invoice_reversal_request_id)
    REFERENCES invoice_reversal_requests(invoice_reversal_request_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(invoice_reversal_request_id,approval_domain),
  UNIQUE(invoice_reversal_request_id,operator_id)
);
CREATE INDEX idx_invoice_reversal_approvals_request_prd104
  ON invoice_reversal_approvals(invoice_reversal_request_id,created_at,invoice_reversal_approval_id);
CREATE TRIGGER trg_invoice_reversal_approvals_self_prd104
BEFORE INSERT ON invoice_reversal_approvals FOR EACH ROW
WHEN NEW.operator_id=(SELECT requested_by_operator_id FROM invoice_reversal_requests
  WHERE invoice_reversal_request_id=NEW.invoice_reversal_request_id)
BEGIN SELECT RAISE(ABORT,'invoice_reversal_self_approval'); END;
CREATE TRIGGER trg_invoice_reversal_approvals_update_prd104
BEFORE UPDATE ON invoice_reversal_approvals
BEGIN SELECT RAISE(ABORT,'invoice_reversal_approval_immutable'); END;
CREATE TRIGGER trg_invoice_reversal_approvals_delete_prd104
BEFORE DELETE ON invoice_reversal_approvals
BEGIN SELECT RAISE(ABORT,'invoice_reversal_approval_immutable'); END;

-- Run 冻结五类 source watermark/hash；worker lease/generation 仅推进状态，不改冻结快照。
CREATE TABLE reconciliation_runs(
  reconciliation_run_id TEXT PRIMARY KEY CHECK(
    length(reconciliation_run_id)=36 AND substr(reconciliation_run_id,1,4)='rec_'),
  scope TEXT NOT NULL CHECK(scope='all'),
  order_watermark INTEGER NOT NULL CHECK(order_watermark>=0),
  payment_watermark INTEGER NOT NULL CHECK(payment_watermark>=0),
  meter_watermark INTEGER NOT NULL CHECK(meter_watermark>=0),
  statement_watermark INTEGER NOT NULL CHECK(statement_watermark>=0),
  invoice_watermark INTEGER NOT NULL CHECK(invoice_watermark>=0),
  order_source_sha256 TEXT NOT NULL CHECK(length(order_source_sha256)=64),
  payment_source_sha256 TEXT NOT NULL CHECK(length(payment_source_sha256)=64),
  meter_source_sha256 TEXT NOT NULL CHECK(length(meter_source_sha256)=64),
  statement_source_sha256 TEXT NOT NULL CHECK(length(statement_source_sha256)=64),
  invoice_source_sha256 TEXT NOT NULL CHECK(length(invoice_source_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('queued','processing','completed','failed')),
  lease_owner TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  generation INTEGER NOT NULL CHECK(generation>=1),
  safe_error_code TEXT CHECK(safe_error_code IS NULL OR safe_error_code IN(
    'reconciliation.source_invalid','reconciliation.worker_unavailable')),
  created_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='queued' AND lease_owner IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND safe_error_code IS NULL AND completed_at IS NULL)
    OR(status='processing' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND safe_error_code IS NULL AND completed_at IS NULL)
    OR(status='completed' AND lease_owner IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND safe_error_code IS NULL AND completed_at IS NOT NULL)
    OR(status='failed' AND lease_owner IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND safe_error_code IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE INDEX idx_reconciliation_runs_worker_prd104
  ON reconciliation_runs(status,lease_expires_at,created_at,reconciliation_run_id);
CREATE TRIGGER trg_reconciliation_runs_owner_prd104
BEFORE UPDATE ON reconciliation_runs FOR EACH ROW
WHEN NEW.reconciliation_run_id!=OLD.reconciliation_run_id OR NEW.scope!=OLD.scope
  OR NEW.order_watermark!=OLD.order_watermark OR NEW.payment_watermark!=OLD.payment_watermark
  OR NEW.meter_watermark!=OLD.meter_watermark OR NEW.statement_watermark!=OLD.statement_watermark
  OR NEW.invoice_watermark!=OLD.invoice_watermark
  OR NEW.order_source_sha256!=OLD.order_source_sha256
  OR NEW.payment_source_sha256!=OLD.payment_source_sha256
  OR NEW.meter_source_sha256!=OLD.meter_source_sha256
  OR NEW.statement_source_sha256!=OLD.statement_source_sha256
  OR NEW.invoice_source_sha256!=OLD.invoice_source_sha256
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id OR NEW.created_at!=OLD.created_at
  OR NEW.generation<OLD.generation
BEGIN SELECT RAISE(ABORT,'reconciliation_run_owner_immutable'); END;
CREATE TRIGGER trg_reconciliation_runs_delete_prd104 BEFORE DELETE ON reconciliation_runs
BEGIN SELECT RAISE(ABORT,'reconciliation_run_immutable'); END;

-- Difference 只保存安全 source ref 与原币差额；不复制回调、税务资料，也不自动修改 source facts。
CREATE TABLE reconciliation_differences(
  reconciliation_difference_id TEXT PRIMARY KEY CHECK(
    length(reconciliation_difference_id)=36 AND substr(reconciliation_difference_id,1,4)='rcd_'),
  reconciliation_run_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  difference_type TEXT NOT NULL CHECK(difference_type IN(
    'order_payment_amount_mismatch','paid_order_missing_capture','closed_cycle_missing_statement',
    'meter_summary_watermark_mismatch','issued_invoice_missing_artifact')),
  left_source_type TEXT NOT NULL CHECK(left_source_type IN(
    'order','billing_cycle','meter_summary','invoice_request')),
  left_source_id TEXT NOT NULL CHECK(length(left_source_id) BETWEEN 8 AND 128),
  right_source_type TEXT CHECK(right_source_type IS NULL OR right_source_type IN(
    'payment_intent','billing_statement','meter_reading','invoice_artifact')),
  right_source_id TEXT CHECK(right_source_id IS NULL OR length(right_source_id) BETWEEN 8 AND 128),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  amount_difference_minor INTEGER NOT NULL CHECK(
    amount_difference_minor BETWEEN -1000000000000 AND 1000000000000),
  status TEXT NOT NULL CHECK(status IN('open','acknowledged','adjusted')),
  adjustment_reference TEXT,
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  detected_at TEXT NOT NULL CHECK(unixepoch(detected_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(reconciliation_run_id) REFERENCES reconciliation_runs(reconciliation_run_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(reconciliation_run_id,difference_type,left_source_type,left_source_id),
  CHECK((status IN('open','acknowledged') AND adjustment_reference IS NULL)
    OR(status='adjusted' AND length(adjustment_reference) BETWEEN 8 AND 128)),
  CHECK((right_source_type IS NULL AND right_source_id IS NULL)
    OR(right_source_type IS NOT NULL AND right_source_id IS NOT NULL))
);
CREATE INDEX idx_reconciliation_differences_cursor_prd104
  ON reconciliation_differences(reconciliation_run_id,reconciliation_difference_id);
CREATE TRIGGER trg_reconciliation_differences_update_prd104
BEFORE UPDATE ON reconciliation_differences FOR EACH ROW
WHEN NEW.reconciliation_difference_id!=OLD.reconciliation_difference_id
  OR NEW.reconciliation_run_id!=OLD.reconciliation_run_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.difference_type!=OLD.difference_type
  OR NEW.left_source_type!=OLD.left_source_type OR NEW.left_source_id!=OLD.left_source_id
  OR NEW.right_source_type IS NOT OLD.right_source_type
  OR NEW.right_source_id IS NOT OLD.right_source_id
  OR NEW.currency!=OLD.currency OR NEW.amount_difference_minor!=OLD.amount_difference_minor
  OR NEW.detected_at!=OLD.detected_at OR NEW.state_revision!=OLD.state_revision+1
  OR NOT((OLD.status='open' AND NEW.status IN('acknowledged','adjusted'))
    OR(OLD.status='acknowledged' AND NEW.status='adjusted'))
BEGIN SELECT RAISE(ABORT,'reconciliation_difference_state_invalid'); END;
CREATE TRIGGER trg_reconciliation_differences_delete_prd104
BEFORE DELETE ON reconciliation_differences
BEGIN SELECT RAISE(ABORT,'reconciliation_difference_immutable'); END;

CREATE TEMP TABLE prd104_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd104_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'billing_statements','invoice_requests','invoice_provider_outbox',
    'invoice_provider_receipts','invoice_artifacts','invoice_download_grants',
    'invoice_reversal_requests','invoice_reversal_approvals','reconciliation_runs',
    'reconciliation_differences'))=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd104_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
