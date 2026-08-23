-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd104_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd104_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0096' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM billing_statements)
  + (SELECT COUNT(*) FROM invoice_requests)
  + (SELECT COUNT(*) FROM invoice_provider_outbox)
  + (SELECT COUNT(*) FROM invoice_provider_receipts)
  + (SELECT COUNT(*) FROM invoice_artifacts)
  + (SELECT COUNT(*) FROM invoice_download_grants)
  + (SELECT COUNT(*) FROM invoice_reversal_requests)
  + (SELECT COUNT(*) FROM invoice_reversal_approvals)
  + (SELECT COUNT(*) FROM reconciliation_runs)
  + (SELECT COUNT(*) FROM reconciliation_differences)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd104_down_guard;

DROP TRIGGER trg_reconciliation_differences_delete_prd104;
DROP TRIGGER trg_reconciliation_differences_update_prd104;
DROP TRIGGER trg_reconciliation_runs_delete_prd104;
DROP TRIGGER trg_reconciliation_runs_owner_prd104;
DROP TRIGGER trg_invoice_reversal_approvals_delete_prd104;
DROP TRIGGER trg_invoice_reversal_approvals_update_prd104;
DROP TRIGGER trg_invoice_reversal_approvals_self_prd104;
DROP TRIGGER trg_invoice_reversals_delete_prd104;
DROP TRIGGER trg_invoice_reversals_update_prd104;
DROP TRIGGER trg_invoice_download_grants_delete_prd104;
DROP TRIGGER trg_invoice_download_grants_update_prd104;
DROP TRIGGER trg_invoice_artifacts_delete_prd104;
DROP TRIGGER trg_invoice_artifacts_update_prd104;
DROP TRIGGER trg_invoice_provider_receipts_delete_prd104;
DROP TRIGGER trg_invoice_provider_receipts_update_prd104;
DROP TRIGGER trg_invoice_provider_outbox_delete_prd104;
DROP TRIGGER trg_invoice_provider_outbox_owner_prd104;
DROP TRIGGER trg_invoice_requests_delete_prd104;
DROP TRIGGER trg_invoice_requests_update_prd104;
DROP TRIGGER trg_billing_statements_delete_prd104;
DROP TRIGGER trg_billing_statements_update_prd104;

DROP TABLE reconciliation_differences;
DROP TABLE reconciliation_runs;
DROP TABLE invoice_reversal_approvals;
DROP TABLE invoice_download_grants;
DROP TABLE invoice_artifacts;
DROP TABLE invoice_provider_receipts;
DROP TABLE invoice_provider_outbox;
DROP TABLE invoice_reversal_requests;
DROP TABLE invoice_requests;
DROP TABLE billing_statements;

CREATE TEMP TABLE prd104_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd104_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd104_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
