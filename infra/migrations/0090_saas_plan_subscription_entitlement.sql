PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd098_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd098_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0089' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspaces','workspace_tenant_bindings','workspace_memberships','platform_roles',
      'platform_operators','platform_operator_role_bindings','platform_audit_logs'))=7
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd098')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd098_up_guard;

-- Platform IAM v1 is immutable. Later product domains extend it through this equally
-- immutable overlay so existing Operator/Session ownership is not rebuilt in place.
CREATE TABLE platform_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.catalog.read','platform.catalog.write')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-098'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=2)
);
CREATE TABLE platform_role_permission_extensions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_permission_extensions(permission_code)
);
INSERT INTO platform_permission_extensions(permission_code,source_prd,catalog_revision) VALUES
  ('platform.catalog.read','PRD-098',2),
  ('platform.catalog.write','PRD-098',2);
INSERT INTO platform_role_permission_extensions(role_code,permission_code) VALUES
  ('platform_owner','platform.catalog.read'),
  ('platform_owner','platform.catalog.write'),
  ('platform_billing','platform.catalog.read'),
  ('platform_billing','platform.catalog.write');
CREATE TRIGGER trg_platform_permission_extensions_insert_prd098
BEFORE INSERT ON platform_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_permission_extensions_update_prd098
BEFORE UPDATE ON platform_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_permission_extensions_delete_prd098
BEFORE DELETE ON platform_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_role_permission_extensions_insert_prd098
BEFORE INSERT ON platform_role_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_role_permission_extensions_update_prd098
BEFORE UPDATE ON platform_role_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_role_permission_extensions_delete_prd098
BEFORE DELETE ON platform_role_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;

CREATE TABLE saas_plan_catalogs(
  plan_catalog_id TEXT PRIMARY KEY CHECK(length(plan_catalog_id)=36
    AND substr(plan_catalog_id,1,4)='spc_'
    AND substr(plan_catalog_id,5) NOT GLOB '*[^0-9a-f]*'),
  plan_code TEXT NOT NULL UNIQUE CHECK(length(plan_code) BETWEEN 2 AND 40
    AND plan_code=lower(plan_code) AND plan_code NOT GLOB '*[^a-z0-9_-]*'),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 80),
  status TEXT NOT NULL CHECK(status IN('draft','active','retired')),
  display_order INTEGER NOT NULL CHECK(display_order BETWEEN 0 AND 10000),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE INDEX idx_saas_plan_catalogs_public_prd098
  ON saas_plan_catalogs(status,display_order,plan_code);

CREATE TABLE saas_plan_versions(
  plan_version_id TEXT PRIMARY KEY CHECK(length(plan_version_id)=36
    AND substr(plan_version_id,1,4)='spv_'
    AND substr(plan_version_id,5) NOT GLOB '*[^0-9a-f]*'),
  plan_catalog_id TEXT NOT NULL,
  version_number INTEGER NOT NULL CHECK(version_number>=1),
  status TEXT NOT NULL CHECK(status IN('draft','published','retired')),
  billing_period TEXT NOT NULL CHECK(billing_period IN('none','monthly','yearly')),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  unit_price_minor INTEGER NOT NULL CHECK(unit_price_minor BETWEEN 0 AND 1000000000000),
  trial_days INTEGER NOT NULL CHECK(trial_days BETWEEN 0 AND 90),
  entitlements_json TEXT NOT NULL CHECK(json_valid(entitlements_json)
    AND json_type(entitlements_json)='object'
    AND length(CAST(entitlements_json AS BLOB))<=4096),
  entitlements_sha256 TEXT NOT NULL CHECK(length(entitlements_sha256)=64
    AND entitlements_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_by_operator_id TEXT NOT NULL,
  published_at TEXT CHECK(published_at IS NULL OR unixepoch(published_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(plan_catalog_id) REFERENCES saas_plan_catalogs(plan_catalog_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(plan_catalog_id,version_number),
  UNIQUE(plan_catalog_id,plan_version_id),
  CHECK((status='published' AND published_at IS NOT NULL)
    OR(status!='published' AND published_at IS NULL))
);
CREATE INDEX idx_saas_plan_versions_public_prd098
  ON saas_plan_versions(status,plan_catalog_id,version_number DESC);
CREATE TRIGGER trg_saas_plan_versions_published_update_prd098
BEFORE UPDATE ON saas_plan_versions FOR EACH ROW WHEN OLD.status='published'
BEGIN SELECT RAISE(ABORT,'saas_plan_version_immutable'); END;
CREATE TRIGGER trg_saas_plan_versions_delete_prd098
BEFORE DELETE ON saas_plan_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'saas_plan_version_immutable'); END;

CREATE TABLE workspace_subscriptions(
  subscription_id TEXT PRIMARY KEY CHECK(length(subscription_id)=36
    AND substr(subscription_id,1,4)='sub_'
    AND substr(subscription_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  plan_version_id TEXT NOT NULL,
  pending_plan_version_id TEXT,
  status TEXT NOT NULL CHECK(status IN(
    'pending','active','trialing','past_due','canceled','expired')),
  current_period_started_at TEXT NOT NULL CHECK(unixepoch(current_period_started_at) IS NOT NULL),
  current_period_ends_at TEXT CHECK(
    current_period_ends_at IS NULL OR unixepoch(current_period_ends_at) IS NOT NULL),
  pending_effective_at TEXT CHECK(
    pending_effective_at IS NULL OR unixepoch(pending_effective_at) IS NOT NULL),
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  created_by_membership_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  FOREIGN KEY(pending_plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  FOREIGN KEY(created_by_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(workspace_id,tenant_id),
  UNIQUE(workspace_id,tenant_id,subscription_id),
  CHECK((pending_plan_version_id IS NULL AND pending_effective_at IS NULL)
    OR(pending_plan_version_id IS NOT NULL AND pending_effective_at IS NOT NULL))
);
CREATE INDEX idx_workspace_subscriptions_status_prd098
  ON workspace_subscriptions(status,updated_at DESC,workspace_id);

CREATE TABLE subscription_trials(
  trial_id TEXT PRIMARY KEY CHECK(length(trial_id)=36 AND substr(trial_id,1,4)='trl_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  plan_version_id TEXT NOT NULL,
  cycle INTEGER NOT NULL CHECK(cycle=1),
  status TEXT NOT NULL CHECK(status IN('active','expired','ended')),
  starts_at TEXT NOT NULL CHECK(unixepoch(starts_at) IS NOT NULL),
  ends_at TEXT NOT NULL CHECK(unixepoch(ends_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,subscription_id)
    REFERENCES workspace_subscriptions(workspace_id,tenant_id,subscription_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  UNIQUE(workspace_id,cycle),
  CHECK(unixepoch(ends_at)>unixepoch(starts_at))
);
CREATE INDEX idx_subscription_trials_due_prd098
  ON subscription_trials(status,ends_at,trial_id);

CREATE TABLE subscription_order_records(
  order_record_id TEXT PRIMARY KEY CHECK(length(order_record_id)=36
    AND substr(order_record_id,1,4)='sor_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  plan_version_id TEXT NOT NULL,
  order_kind TEXT NOT NULL CHECK(order_kind IN('create','upgrade','downgrade')),
  status TEXT NOT NULL CHECK(status IN('not_required','pending_payment','scheduled')),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor BETWEEN 0 AND 1000000000000),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  created_by_membership_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,subscription_id)
    REFERENCES workspace_subscriptions(workspace_id,tenant_id,subscription_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  FOREIGN KEY(created_by_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(workspace_id,idempotency_sha256)
);

CREATE TABLE entitlement_snapshots(
  entitlement_snapshot_id TEXT PRIMARY KEY CHECK(length(entitlement_snapshot_id)=36
    AND substr(entitlement_snapshot_id,1,4)='ent_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  plan_version_id TEXT NOT NULL,
  source_order_record_id TEXT NOT NULL,
  snapshot_version INTEGER NOT NULL CHECK(snapshot_version>=1),
  entitlements_json TEXT NOT NULL CHECK(json_valid(entitlements_json)
    AND json_type(entitlements_json)='object'),
  entitlements_sha256 TEXT NOT NULL CHECK(length(entitlements_sha256)=64),
  effective_from TEXT NOT NULL CHECK(unixepoch(effective_from) IS NOT NULL),
  effective_until TEXT CHECK(effective_until IS NULL OR unixepoch(effective_until) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,subscription_id)
    REFERENCES workspace_subscriptions(workspace_id,tenant_id,subscription_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  FOREIGN KEY(source_order_record_id) REFERENCES subscription_order_records(order_record_id),
  UNIQUE(subscription_id,snapshot_version),
  CHECK(effective_until IS NULL OR unixepoch(effective_until)>unixepoch(effective_from))
);
CREATE INDEX idx_entitlement_snapshots_current_prd098
  ON entitlement_snapshots(workspace_id,effective_until,snapshot_version DESC);
CREATE TRIGGER trg_entitlement_snapshots_update_prd098
BEFORE UPDATE ON entitlement_snapshots FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'entitlement_snapshot_immutable'); END;
CREATE TRIGGER trg_entitlement_snapshots_delete_prd098
BEFORE DELETE ON entitlement_snapshots FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'entitlement_snapshot_immutable'); END;

CREATE TABLE saas_subscription_events(
  event_id TEXT PRIMARY KEY CHECK(length(event_id)=36 AND substr(event_id,1,4)='sev_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  actor_membership_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'subscription.created','subscription.plan_change_scheduled','subscription.trial_expired')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'customer_selected','customer_changed_plan','trial_expired_fallback',
    'trial_expired_no_free_plan')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,subscription_id)
    REFERENCES workspace_subscriptions(workspace_id,tenant_id,subscription_id),
  FOREIGN KEY(actor_membership_id) REFERENCES workspace_memberships(workspace_membership_id)
);
CREATE TRIGGER trg_saas_subscription_events_update_prd098
BEFORE UPDATE ON saas_subscription_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'saas_subscription_event_immutable'); END;
CREATE TRIGGER trg_saas_subscription_events_delete_prd098
BEFORE DELETE ON saas_subscription_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'saas_subscription_event_immutable'); END;

CREATE TEMP TABLE prd098_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd098_up_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd098_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
