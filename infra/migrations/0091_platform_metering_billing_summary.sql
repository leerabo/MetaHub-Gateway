PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd099_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd099_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0090' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'usage_ledger','usage_export_ledger_sequences','workspace_subscriptions',
      'entitlement_snapshots','saas_plan_versions','platform_roles','platform_operators',
      'platform_audit_logs'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd099')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd099_up_guard;

CREATE TABLE platform_billing_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code='platform.billing.adjust'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-099'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=3)
);
CREATE TABLE platform_billing_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_billing_permission_extensions(permission_code)
);
INSERT INTO platform_billing_permission_extensions VALUES('platform.billing.adjust','PRD-099',3);
INSERT INTO platform_billing_role_permissions VALUES
  ('platform_owner','platform.billing.adjust'),
  ('platform_billing','platform.billing.adjust');
CREATE TRIGGER trg_platform_billing_permissions_insert_prd099
BEFORE INSERT ON platform_billing_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_billing_permission_immutable'); END;
CREATE TRIGGER trg_platform_billing_permissions_update_prd099
BEFORE UPDATE ON platform_billing_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_billing_permission_immutable'); END;
CREATE TRIGGER trg_platform_billing_permissions_delete_prd099
BEFORE DELETE ON platform_billing_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_billing_permission_immutable'); END;
CREATE TRIGGER trg_platform_billing_role_permissions_insert_prd099
BEFORE INSERT ON platform_billing_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_billing_permission_immutable'); END;
CREATE TRIGGER trg_platform_billing_role_permissions_update_prd099
BEFORE UPDATE ON platform_billing_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_billing_permission_immutable'); END;
CREATE TRIGGER trg_platform_billing_role_permissions_delete_prd099
BEFORE DELETE ON platform_billing_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_billing_permission_immutable'); END;

CREATE TABLE platform_meter_definitions(
  meter_definition_id TEXT PRIMARY KEY CHECK(length(meter_definition_id)=36
    AND substr(meter_definition_id,1,4)='pmd_'),
  meter_code TEXT NOT NULL UNIQUE CHECK(meter_code IN(
    'api_requests','input_tokens','output_tokens','total_tokens')),
  unit TEXT NOT NULL CHECK(unit IN('request','token')),
  aggregation TEXT NOT NULL CHECK(aggregation='sum'),
  source_field TEXT NOT NULL CHECK(source_field IN(
    'request_count','input_tokens','output_tokens','total_tokens')),
  billing_mode TEXT NOT NULL CHECK(billing_mode IN('subscription_base','informational')),
  entitlement_key TEXT CHECK(entitlement_key IS NULL
    OR entitlement_key='gateway_requests_per_month'),
  source_schema_version INTEGER NOT NULL CHECK(source_schema_version=1),
  status TEXT NOT NULL CHECK(status='active'),
  effective_from TEXT NOT NULL CHECK(unixepoch(effective_from) IS NOT NULL),
  effective_until TEXT CHECK(effective_until IS NULL OR unixepoch(effective_until) IS NOT NULL),
  CHECK((billing_mode='subscription_base' AND entitlement_key IS NOT NULL)
    OR(billing_mode='informational' AND entitlement_key IS NULL)),
  CHECK(effective_until IS NULL OR unixepoch(effective_until)>unixepoch(effective_from))
);
INSERT INTO platform_meter_definitions VALUES
  ('pmd_00000000000000000000000000000001','api_requests','request','sum','request_count',
   'subscription_base','gateway_requests_per_month',1,'active','2026-08-22T00:00:00Z',NULL),
  ('pmd_00000000000000000000000000000002','input_tokens','token','sum','input_tokens',
   'informational',NULL,1,'active','2026-08-22T00:00:00Z',NULL),
  ('pmd_00000000000000000000000000000003','output_tokens','token','sum','output_tokens',
   'informational',NULL,1,'active','2026-08-22T00:00:00Z',NULL),
  ('pmd_00000000000000000000000000000004','total_tokens','token','sum','total_tokens',
   'informational',NULL,1,'active','2026-08-22T00:00:00Z',NULL);
CREATE TRIGGER trg_platform_meter_definitions_update_prd099
BEFORE UPDATE ON platform_meter_definitions
BEGIN SELECT RAISE(ABORT,'platform_meter_definition_immutable'); END;
CREATE TRIGGER trg_platform_meter_definitions_delete_prd099
BEFORE DELETE ON platform_meter_definitions
BEGIN SELECT RAISE(ABORT,'platform_meter_definition_immutable'); END;

CREATE TABLE platform_billing_cycles(
  billing_cycle_id TEXT PRIMARY KEY CHECK(length(billing_cycle_id)=36
    AND substr(billing_cycle_id,1,4)='pbc_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  plan_version_id TEXT NOT NULL,
  entitlement_snapshot_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('open','closed','reconciliation_required')),
  period_started_at TEXT NOT NULL CHECK(unixepoch(period_started_at) IS NOT NULL),
  period_ends_at TEXT NOT NULL CHECK(unixepoch(period_ends_at) IS NOT NULL),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  base_amount_minor INTEGER NOT NULL CHECK(base_amount_minor BETWEEN 0 AND 1000000000000),
  source_watermark INTEGER NOT NULL DEFAULT 0 CHECK(source_watermark>=0),
  summary_set_sha256 TEXT CHECK(summary_set_sha256 IS NULL OR length(summary_set_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,subscription_id)
    REFERENCES workspace_subscriptions(workspace_id,tenant_id,subscription_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  FOREIGN KEY(entitlement_snapshot_id) REFERENCES entitlement_snapshots(entitlement_snapshot_id),
  UNIQUE(subscription_id,period_started_at,period_ends_at),
  UNIQUE(workspace_id,tenant_id,billing_cycle_id),
  CHECK(unixepoch(period_ends_at)>unixepoch(period_started_at)),
  CHECK((status='open' AND summary_set_sha256 IS NULL)
    OR(status!='open' AND summary_set_sha256 IS NOT NULL))
);
CREATE INDEX idx_platform_billing_cycles_workspace_prd099
  ON platform_billing_cycles(workspace_id,status,period_started_at DESC,billing_cycle_id);
CREATE INDEX idx_platform_billing_cycles_platform_prd099
  ON platform_billing_cycles(status,updated_at DESC,billing_cycle_id);

CREATE TABLE platform_quota_allocations(
  quota_allocation_id TEXT PRIMARY KEY CHECK(length(quota_allocation_id)=36
    AND substr(quota_allocation_id,1,4)='pqa_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  billing_cycle_id TEXT NOT NULL,
  meter_definition_id TEXT NOT NULL,
  entitlement_snapshot_id TEXT NOT NULL,
  allocated_quantity INTEGER NOT NULL CHECK(allocated_quantity>=0),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,billing_cycle_id)
    REFERENCES platform_billing_cycles(workspace_id,tenant_id,billing_cycle_id),
  FOREIGN KEY(meter_definition_id) REFERENCES platform_meter_definitions(meter_definition_id),
  FOREIGN KEY(entitlement_snapshot_id) REFERENCES entitlement_snapshots(entitlement_snapshot_id),
  UNIQUE(billing_cycle_id,meter_definition_id)
);
CREATE TRIGGER trg_platform_quota_allocations_update_prd099
BEFORE UPDATE ON platform_quota_allocations
BEGIN SELECT RAISE(ABORT,'platform_quota_allocation_immutable'); END;
CREATE TRIGGER trg_platform_quota_allocations_delete_prd099
BEFORE DELETE ON platform_quota_allocations
BEGIN SELECT RAISE(ABORT,'platform_quota_allocation_immutable'); END;

CREATE TABLE platform_meter_readings(
  meter_reading_id TEXT PRIMARY KEY CHECK(length(meter_reading_id)=36
    AND substr(meter_reading_id,1,4)='pmr_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  billing_cycle_id TEXT NOT NULL,
  meter_definition_id TEXT NOT NULL,
  source_fact_type TEXT NOT NULL CHECK(source_fact_type='usage_ledger'),
  source_fact_id TEXT NOT NULL,
  source_sequence INTEGER NOT NULL CHECK(source_sequence>0),
  source_owner_sha256 TEXT NOT NULL CHECK(length(source_owner_sha256)=64),
  quantity INTEGER NOT NULL CHECK(quantity>=0),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  projected_at TEXT NOT NULL CHECK(unixepoch(projected_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,billing_cycle_id)
    REFERENCES platform_billing_cycles(workspace_id,tenant_id,billing_cycle_id),
  FOREIGN KEY(meter_definition_id) REFERENCES platform_meter_definitions(meter_definition_id),
  FOREIGN KEY(source_fact_id) REFERENCES usage_ledger(usage_ledger_id),
  UNIQUE(source_fact_type,source_fact_id,meter_definition_id),
  UNIQUE(meter_reading_id,workspace_id,tenant_id)
);
CREATE INDEX idx_platform_meter_readings_cycle_prd099
  ON platform_meter_readings(billing_cycle_id,meter_definition_id,source_sequence);
CREATE TRIGGER trg_platform_meter_readings_update_prd099
BEFORE UPDATE ON platform_meter_readings
BEGIN SELECT RAISE(ABORT,'platform_meter_reading_immutable'); END;
CREATE TRIGGER trg_platform_meter_readings_delete_prd099
BEFORE DELETE ON platform_meter_readings
BEGIN SELECT RAISE(ABORT,'platform_meter_reading_immutable'); END;

CREATE TABLE platform_billing_summaries(
  billing_summary_id TEXT PRIMARY KEY CHECK(length(billing_summary_id)=36
    AND substr(billing_summary_id,1,4)='pbs_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  billing_cycle_id TEXT NOT NULL,
  meter_definition_id TEXT NOT NULL,
  summary_revision INTEGER NOT NULL CHECK(summary_revision=1),
  usage_quantity INTEGER NOT NULL CHECK(usage_quantity>=0),
  allocated_quantity INTEGER NOT NULL CHECK(allocated_quantity>=0),
  overage_quantity INTEGER NOT NULL CHECK(overage_quantity>=0),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor BETWEEN 0 AND 1000000000000),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  summary_sha256 TEXT NOT NULL CHECK(length(summary_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,billing_cycle_id)
    REFERENCES platform_billing_cycles(workspace_id,tenant_id,billing_cycle_id),
  FOREIGN KEY(meter_definition_id) REFERENCES platform_meter_definitions(meter_definition_id),
  UNIQUE(billing_cycle_id,meter_definition_id),
  UNIQUE(billing_summary_id,workspace_id,tenant_id)
);
CREATE TRIGGER trg_platform_billing_summaries_update_prd099
BEFORE UPDATE ON platform_billing_summaries
BEGIN SELECT RAISE(ABORT,'platform_billing_summary_immutable'); END;
CREATE TRIGGER trg_platform_billing_summaries_delete_prd099
BEFORE DELETE ON platform_billing_summaries
BEGIN SELECT RAISE(ABORT,'platform_billing_summary_immutable'); END;

CREATE TABLE platform_billing_adjustments(
  billing_adjustment_id TEXT PRIMARY KEY CHECK(length(billing_adjustment_id)=36
    AND substr(billing_adjustment_id,1,4)='pba_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  billing_cycle_id TEXT NOT NULL,
  billing_summary_id TEXT NOT NULL,
  meter_reading_id TEXT,
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','system')),
  actor_operator_id TEXT,
  reason_code TEXT NOT NULL CHECK(reason_code IN('manual_credit','manual_debit','late_usage')),
  quantity_delta INTEGER NOT NULL,
  amount_delta_minor INTEGER NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  expected_summary_sha256 TEXT NOT NULL CHECK(length(expected_summary_sha256)=64),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,billing_cycle_id)
    REFERENCES platform_billing_cycles(workspace_id,tenant_id,billing_cycle_id),
  FOREIGN KEY(billing_summary_id,workspace_id,tenant_id)
    REFERENCES platform_billing_summaries(billing_summary_id,workspace_id,tenant_id),
  FOREIGN KEY(meter_reading_id,workspace_id,tenant_id)
    REFERENCES platform_meter_readings(meter_reading_id,workspace_id,tenant_id),
  FOREIGN KEY(actor_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(workspace_id,idempotency_sha256),
  CHECK((actor_domain='platform' AND actor_operator_id IS NOT NULL
      AND reason_code IN('manual_credit','manual_debit') AND meter_reading_id IS NULL)
    OR(actor_domain='system' AND actor_operator_id IS NULL
      AND reason_code='late_usage' AND meter_reading_id IS NOT NULL)),
  CHECK(quantity_delta!=0 OR amount_delta_minor!=0)
);
CREATE TRIGGER trg_platform_billing_adjustments_update_prd099
BEFORE UPDATE ON platform_billing_adjustments
BEGIN SELECT RAISE(ABORT,'platform_billing_adjustment_immutable'); END;
CREATE TRIGGER trg_platform_billing_adjustments_delete_prd099
BEFORE DELETE ON platform_billing_adjustments
BEGIN SELECT RAISE(ABORT,'platform_billing_adjustment_immutable'); END;

CREATE TABLE platform_meter_outbox(
  meter_outbox_id TEXT PRIMARY KEY CHECK(length(meter_outbox_id)=36
    AND substr(meter_outbox_id,1,4)='pmo_'),
  source_usage_ledger_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','completed','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  error_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(source_usage_ledger_id) REFERENCES usage_ledger(usage_ledger_id),
  CHECK((status='processing' AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL)
    OR(status!='processing' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL)),
  CHECK((status='failed' AND error_code IS NOT NULL)
    OR(status!='failed' AND error_code IS NULL))
);
CREATE INDEX idx_platform_meter_outbox_status_prd099
  ON platform_meter_outbox(status,updated_at,meter_outbox_id);

CREATE TABLE platform_meter_checkpoints(
  projection_code TEXT PRIMARY KEY CHECK(projection_code='usage_ledger_v1'),
  source_sequence INTEGER NOT NULL CHECK(source_sequence>=0),
  source_fact_id TEXT,
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((source_sequence=0 AND source_fact_id IS NULL)
    OR(source_sequence>0 AND source_fact_id IS NOT NULL))
);
INSERT INTO platform_meter_checkpoints(projection_code,source_sequence,source_fact_id,updated_at)
SELECT 'usage_ledger_v1',COALESCE(MAX(usage_export_sequence),0),
  CASE WHEN MAX(usage_export_sequence) IS NULL THEN NULL ELSE (
    SELECT usage_ledger_id FROM usage_export_ledger_sequences
    ORDER BY usage_export_sequence DESC LIMIT 1) END,
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM usage_export_ledger_sequences;

CREATE TRIGGER trg_usage_ledger_platform_meter_outbox_prd099
AFTER INSERT ON usage_ledger FOR EACH ROW
BEGIN
  INSERT INTO platform_meter_outbox(
    meter_outbox_id,source_usage_ledger_id,status,attempt_count,created_at,updated_at)
  VALUES('pmo_'||lower(hex(randomblob(16))),NEW.usage_ledger_id,'pending',0,
    strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;

CREATE TEMP TABLE prd099_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd099_up_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd099_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
