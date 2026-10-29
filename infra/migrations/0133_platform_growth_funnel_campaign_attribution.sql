-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd141_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd141_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0132' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'product_events','product_event_outbox','workspace_tenant_bindings',
      'workspace_memberships','consent_records','privacy_preferences',
      'platform_operators','platform_roles','tenant_deletion_table_manifest',
      'tenant_deletion_trigger_manifest'))=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM product_event_outbox
      WHERE status='leased' AND unixepoch(lease_expires_at)>unixepoch('now'))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd141')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd141_up_guard;

-- Retire the Workspace-scoped funnel permission without changing unrelated grants.
CREATE TEMP TABLE prd141_roles_to_contract(role_id TEXT PRIMARY KEY);
INSERT INTO prd141_roles_to_contract
SELECT DISTINCT role_permission.role_id
FROM role_permissions role_permission
JOIN permissions permission ON permission.permission_id=role_permission.permission_id
WHERE permission.code='product_analytics.read';
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DELETE FROM role_permissions WHERE permission_id IN(
  SELECT permission_id FROM permissions WHERE code='product_analytics.read');
DELETE FROM rbac_role_permission_templates WHERE permission_code='product_analytics.read';
DELETE FROM permissions WHERE code='product_analytics.read';
DELETE FROM rbac_permission_templates WHERE permission_code='product_analytics.read';
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=metahub_sha256(coalesce((
      SELECT json_group_array(code) FROM(
        SELECT permission.code AS code
        FROM role_permissions role_permission
        JOIN permissions permission ON permission.permission_id=role_permission.permission_id
        WHERE role_permission.tenant_id=roles.tenant_id
          AND role_permission.role_id=roles.role_id
        ORDER BY permission.code)), '[]'))
WHERE role_id IN(SELECT role_id FROM prd141_roles_to_contract);
DROP TABLE prd141_roles_to_contract;

CREATE TRIGGER trg_rbac_permission_templates_insert_prd059 BEFORE INSERT ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059 BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059 BEFORE DELETE ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_insert_prd059 BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059 BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059 BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_permissions_owner_insert_prd059 BEFORE INSERT ON permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates t WHERE t.permission_code=NEW.code AND t.category=NEW.category AND t.name=NEW.name AND t.description=NEW.description AND t.risk_level=NEW.risk_level AND t.catalog_revision=NEW.catalog_revision) OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.status!='active' OR EXISTS(SELECT 1 FROM permissions p WHERE p.permission_id=NEW.permission_id OR(p.tenant_id=NEW.tenant_id AND p.code=NEW.code) OR(NEW.rowid!=-1 AND p.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059 AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0 BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN 'eb48d3c3fddfa76477ae5af3943449253af0581da445e3d5695f893258dba6b8' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN 'bdab5bcae4741701f7171d1f08624f71306963dea15e99ccbde55028df2e6426' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-141 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN 'eb48d3c3fddfa76477ae5af3943449253af0581da445e3d5695f893258dba6b8' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN 'bdab5bcae4741701f7171d1f08624f71306963dea15e99ccbde55028df2e6426' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,27,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

CREATE TABLE platform_growth_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.growth.read','platform.growth.adoption_read',
    'platform.growth.channel_attribution_read','platform.growth.campaign.manage',
    'platform.growth.experiment.manage','platform.growth.export.request',
    'platform.growth.export.approve','platform.growth.export.download')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE TABLE platform_growth_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_growth_permission_extensions(permission_code)
);
INSERT INTO platform_growth_permission_extensions VALUES
  ('platform.growth.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.growth.adoption_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.growth.channel_attribution_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.growth.campaign.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.growth.experiment.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.growth.export.request',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.growth.export.approve',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.growth.export.download',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
INSERT INTO platform_growth_role_permissions
SELECT role_code,permission_code,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (SELECT 'platform_owner' AS role_code UNION ALL SELECT 'platform_growth') roles
CROSS JOIN platform_growth_permission_extensions
WHERE NOT(role_code='platform_growth' AND permission_code='platform.growth.export.approve');
INSERT INTO platform_growth_role_permissions VALUES
  ('platform_channel','platform.growth.channel_attribution_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_customer_success','platform.growth.adoption_read',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_platform_growth_permissions_insert_prd141 BEFORE INSERT ON platform_growth_permission_extensions BEGIN SELECT RAISE(ABORT,'growth_permission_immutable'); END;
CREATE TRIGGER trg_platform_growth_permissions_update_prd141 BEFORE UPDATE ON platform_growth_permission_extensions BEGIN SELECT RAISE(ABORT,'growth_permission_immutable'); END;
CREATE TRIGGER trg_platform_growth_permissions_delete_prd141 BEFORE DELETE ON platform_growth_permission_extensions BEGIN SELECT RAISE(ABORT,'growth_permission_immutable'); END;
CREATE TRIGGER trg_platform_growth_roles_insert_prd141 BEFORE INSERT ON platform_growth_role_permissions BEGIN SELECT RAISE(ABORT,'growth_permission_immutable'); END;
CREATE TRIGGER trg_platform_growth_roles_update_prd141 BEFORE UPDATE ON platform_growth_role_permissions BEGIN SELECT RAISE(ABORT,'growth_permission_immutable'); END;
CREATE TRIGGER trg_platform_growth_roles_delete_prd141 BEFORE DELETE ON platform_growth_role_permissions BEGIN SELECT RAISE(ABORT,'growth_permission_immutable'); END;

CREATE TABLE platform_growth_event_eligibility(
  growth_event_eligibility_id TEXT PRIMARY KEY CHECK(length(growth_event_eligibility_id)=36
    AND substr(growth_event_eligibility_id,1,4)='gel_'
    AND substr(growth_event_eligibility_id,5) NOT GLOB '*[^0-9a-f]*'),
  product_event_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  formula_version TEXT NOT NULL CHECK(formula_version='growth-eligibility-v2'),
  subject_token_sha256 TEXT CHECK(subject_token_sha256 IS NULL OR(
    length(subject_token_sha256)=64 AND subject_token_sha256 NOT GLOB '*[^0-9a-f]*')),
  subject_key_version INTEGER CHECK(subject_key_version IS NULL OR subject_key_version>=1),
  consent_record_id TEXT,
  privacy_preference_revision INTEGER,
  purpose_code TEXT NOT NULL CHECK(purpose_code='analytics'),
  purpose_version TEXT NOT NULL CHECK(length(purpose_version) BETWEEN 1 AND 80),
  status TEXT NOT NULL CHECK(status IN('eligible','excluded','withdrawn','erased')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'eligible','historical_consent_unproven','consent_denied','consent_withdrawn',
    'preference_disabled','membership_inactive','owner_conflict','subject_erased')),
  evaluated_at TEXT NOT NULL CHECK(unixepoch(evaluated_at) IS NOT NULL),
  canonical_sha256 TEXT NOT NULL CHECK(length(canonical_sha256)=64
    AND canonical_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(owner_workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,product_event_id)
    REFERENCES product_events(tenant_id,product_event_id),
  FOREIGN KEY(consent_record_id) REFERENCES consent_records(consent_record_id),
  UNIQUE(product_event_id,formula_version),
  CHECK((status='eligible' AND reason_code='eligible' AND subject_token_sha256 IS NOT NULL
      AND subject_key_version IS NOT NULL AND consent_record_id IS NOT NULL
      AND privacy_preference_revision IS NOT NULL)
    OR(status!='eligible' AND reason_code!='eligible')),
  CHECK(updated_at>=created_at)
);
CREATE INDEX idx_growth_eligibility_source_prd141
  ON platform_growth_event_eligibility(tenant_id,product_event_id,status);
CREATE TRIGGER trg_growth_eligibility_owner_update_prd141 BEFORE UPDATE ON platform_growth_event_eligibility FOR EACH ROW
WHEN NEW.growth_event_eligibility_id!=OLD.growth_event_eligibility_id
  OR NEW.product_event_id!=OLD.product_event_id OR NEW.owner_workspace_id!=OLD.owner_workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.formula_version!=OLD.formula_version
  OR NEW.purpose_code!=OLD.purpose_code OR NEW.purpose_version!=OLD.purpose_version
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'growth_eligibility_owner_immutable'); END;
CREATE TRIGGER trg_growth_eligibility_delete_prd141 BEFORE DELETE ON platform_growth_event_eligibility
BEGIN SELECT RAISE(ABORT,'growth_eligibility_immutable'); END;

CREATE TABLE platform_growth_legacy_reconciliations(
  growth_legacy_reconciliation_id TEXT PRIMARY KEY CHECK(length(growth_legacy_reconciliation_id)=36
    AND substr(growth_legacy_reconciliation_id,1,4)='glr_'),
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL UNIQUE,
  formula_version TEXT NOT NULL CHECK(formula_version='legacy-activation-v1'),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  registered_count INTEGER NOT NULL CHECK(registered_count>=0),
  activated_count INTEGER NOT NULL CHECK(activated_count>=0),
  cumulative_stage_count INTEGER NOT NULL CHECK(cumulative_stage_count BETWEEN 0 AND 7),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  reconciled_at TEXT NOT NULL CHECK(unixepoch(reconciled_at) IS NOT NULL),
  FOREIGN KEY(owner_workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK(registered_count<=source_row_count AND activated_count<=source_row_count)
);
CREATE TRIGGER trg_growth_legacy_reconciliation_update_prd141 BEFORE UPDATE ON platform_growth_legacy_reconciliations
BEGIN SELECT RAISE(ABORT,'growth_reconciliation_immutable'); END;
CREATE TRIGGER trg_growth_legacy_reconciliation_delete_prd141 BEFORE DELETE ON platform_growth_legacy_reconciliations
BEGIN SELECT RAISE(ABORT,'growth_reconciliation_immutable'); END;

CREATE TABLE platform_growth_legacy_mappings(
  growth_legacy_mapping_id TEXT PRIMARY KEY CHECK(length(growth_legacy_mapping_id)=36
    AND substr(growth_legacy_mapping_id,1,4)='glm_'),
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN('product_event','product_event_outbox')),
  source_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('mapped','conflict')),
  conflict_code TEXT CHECK(conflict_code IS NULL OR conflict_code IN(
    'owner_missing','owner_conflict','source_drift')),
  mapped_at TEXT NOT NULL CHECK(unixepoch(mapped_at) IS NOT NULL),
  FOREIGN KEY(owner_workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(source_kind,source_id),
  CHECK((status='mapped' AND conflict_code IS NULL)
    OR(status='conflict' AND conflict_code IS NOT NULL))
);
CREATE TRIGGER trg_growth_legacy_mapping_update_prd141 BEFORE UPDATE ON platform_growth_legacy_mappings
BEGIN SELECT RAISE(ABORT,'growth_mapping_immutable'); END;
CREATE TRIGGER trg_growth_legacy_mapping_delete_prd141 BEFORE DELETE ON platform_growth_legacy_mappings
BEGIN SELECT RAISE(ABORT,'growth_mapping_immutable'); END;

CREATE TABLE platform_acquisition_sources(
  acquisition_source_id TEXT PRIMARY KEY CHECK(length(acquisition_source_id)=36
    AND substr(acquisition_source_id,1,4)='acs_'),
  source_code_sha256 TEXT NOT NULL UNIQUE CHECK(length(source_code_sha256)=64
    AND source_code_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_kind TEXT NOT NULL CHECK(source_kind IN(
    'direct','organic','paid','partner','referral','sales','unknown')),
  channel_code TEXT NOT NULL CHECK(length(channel_code) BETWEEN 1 AND 40
    AND channel_code=lower(channel_code) AND channel_code NOT GLOB '*[^a-z0-9._-]*'),
  channel_partner_id TEXT,
  channel_referral_code_id TEXT,
  campaign_id TEXT,
  verification_status TEXT NOT NULL CHECK(verification_status IN(
    'verified','unverified','retired')),
  valid_from TEXT NOT NULL CHECK(unixepoch(valid_from) IS NOT NULL),
  valid_until TEXT CHECK(valid_until IS NULL OR unixepoch(valid_until) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  canonical_sha256 TEXT NOT NULL CHECK(length(canonical_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id) REFERENCES channel_partners(channel_partner_id),
  FOREIGN KEY(channel_referral_code_id)
    REFERENCES channel_referral_codes(channel_referral_code_id),
  CHECK(valid_until IS NULL OR unixepoch(valid_until)>unixepoch(valid_from))
);
CREATE INDEX idx_growth_sources_prd141
  ON platform_acquisition_sources(source_kind,channel_code,verification_status);
CREATE TRIGGER trg_growth_source_owner_update_prd141 BEFORE UPDATE ON platform_acquisition_sources FOR EACH ROW
WHEN NEW.acquisition_source_id!=OLD.acquisition_source_id
  OR NEW.source_code_sha256!=OLD.source_code_sha256 OR NEW.source_kind!=OLD.source_kind
  OR NEW.channel_partner_id IS NOT OLD.channel_partner_id
  OR NEW.channel_referral_code_id IS NOT OLD.channel_referral_code_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'growth_source_owner_immutable'); END;
CREATE TRIGGER trg_growth_source_delete_prd141 BEFORE DELETE ON platform_acquisition_sources
BEGIN SELECT RAISE(ABORT,'growth_source_immutable'); END;

CREATE TABLE platform_growth_funnel_snapshots(
  growth_funnel_snapshot_id TEXT PRIMARY KEY CHECK(length(growth_funnel_snapshot_id)=36
    AND substr(growth_funnel_snapshot_id,1,4)='gfs_'),
  bucket_start TEXT NOT NULL CHECK(length(bucket_start)=10 AND date(bucket_start)=bucket_start),
  bucket_kind TEXT NOT NULL CHECK(bucket_kind IN('day','week','month')),
  view_code TEXT NOT NULL CHECK(view_code IN(
    'overview','adoption','retention','legacy_operational_v1')),
  dimension_code TEXT NOT NULL CHECK(dimension_code IN(
    'none','source','channel','region','plan_band')),
  dimension_value TEXT NOT NULL CHECK(length(dimension_value) BETWEEN 1 AND 80),
  formula_version TEXT NOT NULL CHECK(formula_version IN(
    'growth-funnel-v2','legacy-activation-v1')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  registered_count INTEGER NOT NULL CHECK(registered_count>=0),
  activated_count INTEGER NOT NULL CHECK(activated_count>=0),
  trial_started_count INTEGER NOT NULL CHECK(trial_started_count>=0),
  paid_count INTEGER NOT NULL CHECK(paid_count>=0),
  retained_30d_count INTEGER NOT NULL CHECK(retained_30d_count>=0),
  registered_to_activated_bps INTEGER CHECK(registered_to_activated_bps BETWEEN 0 AND 10000),
  activated_to_trial_bps INTEGER CHECK(activated_to_trial_bps BETWEEN 0 AND 10000),
  trial_to_paid_bps INTEGER CHECK(trial_to_paid_bps BETWEEN 0 AND 10000),
  paid_to_retained_bps INTEGER CHECK(paid_to_retained_bps BETWEEN 0 AND 10000),
  currency_code TEXT CHECK(currency_code IS NULL OR(
    length(currency_code)=3 AND currency_code=upper(currency_code))),
  revenue_minor INTEGER CHECK(revenue_minor IS NULL OR revenue_minor>=0),
  cost_minor INTEGER CHECK(cost_minor IS NULL OR cost_minor>=0),
  coverage_bps INTEGER NOT NULL CHECK(coverage_bps BETWEEN 0 AND 10000),
  distinct_subject_count INTEGER NOT NULL CHECK(distinct_subject_count>=0),
  suppression_threshold INTEGER NOT NULL CHECK(suppression_threshold IN(20,50)),
  suppressed INTEGER NOT NULL CHECK(suppressed IN(0,1)),
  suppression_code TEXT CHECK(suppression_code IS NULL OR suppression_code IN(
    'k_anonymity','complementary','privacy_budget')),
  source_watermark TEXT,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  supersedes_snapshot_id TEXT,
  generated_at TEXT NOT NULL CHECK(unixepoch(generated_at) IS NOT NULL),
  FOREIGN KEY(supersedes_snapshot_id)
    REFERENCES platform_growth_funnel_snapshots(growth_funnel_snapshot_id),
  UNIQUE(bucket_start,bucket_kind,view_code,dimension_code,dimension_value,
    formula_version,generation),
  CHECK((currency_code IS NULL AND revenue_minor IS NULL AND cost_minor IS NULL)
    OR(currency_code IS NOT NULL AND revenue_minor IS NOT NULL AND cost_minor IS NOT NULL)),
  CHECK((suppressed=0 AND suppression_code IS NULL)
    OR(suppressed=1 AND suppression_code IS NOT NULL))
);
CREATE INDEX idx_growth_funnels_query_prd141
  ON platform_growth_funnel_snapshots(
    view_code,bucket_kind,bucket_start,dimension_code,dimension_value,generation DESC);
CREATE TRIGGER trg_growth_funnel_update_prd141 BEFORE UPDATE ON platform_growth_funnel_snapshots
BEGIN SELECT RAISE(ABORT,'growth_snapshot_immutable'); END;
CREATE TRIGGER trg_growth_funnel_delete_prd141 BEFORE DELETE ON platform_growth_funnel_snapshots
BEGIN SELECT RAISE(ABORT,'growth_snapshot_immutable'); END;

CREATE TABLE platform_growth_cohort_snapshots(
  growth_cohort_snapshot_id TEXT PRIMARY KEY CHECK(length(growth_cohort_snapshot_id)=36
    AND substr(growth_cohort_snapshot_id,1,4)='gco_'),
  cohort_kind TEXT NOT NULL CHECK(cohort_kind IN(
    'signup_week','activation_week','first_paid_month','campaign')),
  cohort_bucket TEXT NOT NULL CHECK(length(cohort_bucket)=10 AND date(cohort_bucket)=cohort_bucket),
  age_bucket INTEGER NOT NULL CHECK(age_bucket BETWEEN 0 AND 23),
  view_code TEXT NOT NULL CHECK(view_code IN('adoption','retention')),
  dimension_code TEXT NOT NULL CHECK(dimension_code IN(
    'none','source','channel','region','plan_band')),
  dimension_value TEXT NOT NULL CHECK(length(dimension_value) BETWEEN 1 AND 80),
  eligible_count INTEGER NOT NULL CHECK(eligible_count>=0),
  retained_count INTEGER NOT NULL CHECK(retained_count>=0),
  churned_count INTEGER NOT NULL CHECK(churned_count>=0),
  retention_bps INTEGER CHECK(retention_bps BETWEEN 0 AND 10000),
  coverage_bps INTEGER NOT NULL CHECK(coverage_bps BETWEEN 0 AND 10000),
  distinct_subject_count INTEGER NOT NULL CHECK(distinct_subject_count>=0),
  suppression_threshold INTEGER NOT NULL CHECK(suppression_threshold IN(20,50)),
  suppressed INTEGER NOT NULL CHECK(suppressed IN(0,1)),
  suppression_code TEXT CHECK(suppression_code IS NULL OR suppression_code IN(
    'k_anonymity','complementary','privacy_budget')),
  formula_version TEXT NOT NULL CHECK(formula_version='growth-cohort-v2'),
  generation INTEGER NOT NULL CHECK(generation>=1),
  source_watermark TEXT,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  supersedes_snapshot_id TEXT,
  generated_at TEXT NOT NULL CHECK(unixepoch(generated_at) IS NOT NULL),
  FOREIGN KEY(supersedes_snapshot_id)
    REFERENCES platform_growth_cohort_snapshots(growth_cohort_snapshot_id),
  UNIQUE(cohort_kind,cohort_bucket,age_bucket,view_code,dimension_code,
    dimension_value,formula_version,generation),
  CHECK(retained_count+churned_count<=eligible_count),
  CHECK((suppressed=0 AND suppression_code IS NULL)
    OR(suppressed=1 AND suppression_code IS NOT NULL))
);
CREATE INDEX idx_growth_cohorts_query_prd141
  ON platform_growth_cohort_snapshots(
    view_code,cohort_kind,cohort_bucket,age_bucket,dimension_code,generation DESC);
CREATE TRIGGER trg_growth_cohort_update_prd141 BEFORE UPDATE ON platform_growth_cohort_snapshots
BEGIN SELECT RAISE(ABORT,'growth_snapshot_immutable'); END;
CREATE TRIGGER trg_growth_cohort_delete_prd141 BEFORE DELETE ON platform_growth_cohort_snapshots
BEGIN SELECT RAISE(ABORT,'growth_snapshot_immutable'); END;

CREATE TABLE platform_growth_campaigns(
  growth_campaign_id TEXT PRIMARY KEY CHECK(length(growth_campaign_id)=36
    AND substr(growth_campaign_id,1,4)='gca_'),
  display_name TEXT NOT NULL CHECK(length(CAST(display_name AS BLOB)) BETWEEN 1 AND 120),
  acquisition_source_id TEXT NOT NULL,
  channel_code TEXT NOT NULL CHECK(length(channel_code) BETWEEN 1 AND 40
    AND channel_code=lower(channel_code) AND channel_code NOT GLOB '*[^a-z0-9._-]*'),
  target_stage TEXT NOT NULL CHECK(target_stage IN(
    'registered','activated','trial_started','paid','retained_30d')),
  starts_at TEXT NOT NULL CHECK(unixepoch(starts_at) IS NOT NULL),
  ends_at TEXT NOT NULL CHECK(unixepoch(ends_at) IS NOT NULL),
  budget_minor INTEGER NOT NULL CHECK(budget_minor>=0),
  currency_code TEXT NOT NULL CHECK(length(currency_code)=3 AND currency_code=upper(currency_code)),
  status TEXT NOT NULL CHECK(status IN(
    'draft','scheduled','active','paused','completed','archived')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  created_by_operator_id TEXT NOT NULL,
  updated_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(acquisition_source_id)
    REFERENCES platform_acquisition_sources(acquisition_source_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(unixepoch(ends_at)>unixepoch(starts_at)),
  CHECK(updated_at>=created_at)
);
CREATE INDEX idx_growth_campaigns_query_prd141
  ON platform_growth_campaigns(status,starts_at,channel_code,growth_campaign_id);
CREATE TRIGGER trg_growth_campaign_owner_update_prd141 BEFORE UPDATE ON platform_growth_campaigns FOR EACH ROW
WHEN NEW.growth_campaign_id!=OLD.growth_campaign_id OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'growth_campaign_transition_invalid'); END;
CREATE TRIGGER trg_growth_campaign_delete_prd141 BEFORE DELETE ON platform_growth_campaigns
BEGIN SELECT RAISE(ABORT,'growth_campaign_immutable'); END;

CREATE TABLE platform_growth_experiments(
  growth_experiment_id TEXT PRIMARY KEY CHECK(length(growth_experiment_id)=36
    AND substr(growth_experiment_id,1,4)='gex_'),
  hypothesis_code TEXT NOT NULL CHECK(length(hypothesis_code) BETWEEN 3 AND 80
    AND hypothesis_code=lower(hypothesis_code) AND hypothesis_code NOT GLOB '*[^a-z0-9._-]*'),
  surface_code TEXT NOT NULL CHECK(length(surface_code) BETWEEN 3 AND 80
    AND surface_code=lower(surface_code) AND surface_code NOT GLOB '*[^a-z0-9._-]*'),
  primary_metric_code TEXT NOT NULL CHECK(length(primary_metric_code) BETWEEN 3 AND 80
    AND primary_metric_code=lower(primary_metric_code)
    AND primary_metric_code NOT GLOB '*[^a-z0-9._-]*'),
  control_code TEXT NOT NULL CHECK(length(control_code) BETWEEN 1 AND 40),
  variant_code TEXT NOT NULL CHECK(length(variant_code) BETWEEN 1 AND 40
    AND variant_code!=control_code),
  control_allocation_bps INTEGER NOT NULL CHECK(control_allocation_bps BETWEEN 1 AND 9999),
  variant_allocation_bps INTEGER NOT NULL CHECK(variant_allocation_bps BETWEEN 1 AND 9999),
  eligibility_policy_version TEXT NOT NULL CHECK(length(eligibility_policy_version) BETWEEN 1 AND 80),
  consent_purpose TEXT NOT NULL CHECK(consent_purpose='analytics'),
  consent_version TEXT NOT NULL CHECK(length(consent_version) BETWEEN 1 AND 80),
  sample_cap INTEGER NOT NULL CHECK(sample_cap BETWEEN 20 AND 10000000),
  starts_at TEXT NOT NULL CHECK(unixepoch(starts_at) IS NOT NULL),
  ends_at TEXT NOT NULL CHECK(unixepoch(ends_at) IS NOT NULL),
  guardrail_code TEXT NOT NULL CHECK(length(guardrail_code) BETWEEN 3 AND 80),
  decision_policy_version TEXT NOT NULL CHECK(length(decision_policy_version) BETWEEN 1 AND 80),
  status TEXT NOT NULL CHECK(status IN(
    'draft','scheduled','running','paused','decided','stopped','canceled','archived')),
  decision_code TEXT CHECK(decision_code IS NULL OR decision_code IN(
    'winner_control','winner_variant','inconclusive','stopped_guardrail','canceled')),
  source_watermark TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  canonical_sha256 TEXT NOT NULL CHECK(length(canonical_sha256)=64),
  created_by_operator_id TEXT NOT NULL,
  updated_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  decided_at TEXT CHECK(decided_at IS NULL OR unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(control_allocation_bps+variant_allocation_bps=10000),
  CHECK(unixepoch(ends_at)>unixepoch(starts_at)),
  CHECK((status IN('decided','stopped','canceled','archived') AND decision_code IS NOT NULL)
    OR(status NOT IN('decided','stopped','canceled','archived') AND decision_code IS NULL)),
  CHECK(updated_at>=created_at)
);
CREATE INDEX idx_growth_experiments_query_prd141
  ON platform_growth_experiments(status,surface_code,primary_metric_code,starts_at);
CREATE TRIGGER trg_growth_experiment_owner_update_prd141 BEFORE UPDATE ON platform_growth_experiments FOR EACH ROW
WHEN NEW.growth_experiment_id!=OLD.growth_experiment_id
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'growth_experiment_transition_invalid'); END;
CREATE TRIGGER trg_growth_experiment_delete_prd141 BEFORE DELETE ON platform_growth_experiments
BEGIN SELECT RAISE(ABORT,'growth_experiment_immutable'); END;

CREATE TABLE platform_growth_experiment_assignments(
  growth_experiment_assignment_id TEXT PRIMARY KEY CHECK(
    length(growth_experiment_assignment_id)=36
    AND substr(growth_experiment_assignment_id,1,4)='gea_'),
  growth_experiment_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_token_sha256 TEXT CHECK(subject_token_sha256 IS NULL OR length(subject_token_sha256)=64),
  subject_key_version INTEGER CHECK(subject_key_version IS NULL OR subject_key_version>=1),
  deterministic_bucket INTEGER NOT NULL CHECK(deterministic_bucket BETWEEN 0 AND 9999),
  variant_code TEXT NOT NULL CHECK(length(variant_code) BETWEEN 1 AND 40),
  consent_record_id TEXT NOT NULL,
  privacy_preference_revision INTEGER NOT NULL CHECK(privacy_preference_revision>=1),
  assigned_at TEXT NOT NULL CHECK(unixepoch(assigned_at) IS NOT NULL),
  withdrawn_at TEXT CHECK(withdrawn_at IS NULL OR unixepoch(withdrawn_at) IS NOT NULL),
  assignment_sha256 TEXT NOT NULL CHECK(length(assignment_sha256)=64),
  FOREIGN KEY(growth_experiment_id)
    REFERENCES platform_growth_experiments(growth_experiment_id),
  FOREIGN KEY(owner_workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(consent_record_id) REFERENCES consent_records(consent_record_id),
  UNIQUE(growth_experiment_id,owner_workspace_id,subject_token_sha256)
);
CREATE TRIGGER trg_growth_assignment_owner_update_prd141 BEFORE UPDATE ON platform_growth_experiment_assignments FOR EACH ROW
WHEN NEW.growth_experiment_assignment_id!=OLD.growth_experiment_assignment_id
  OR NEW.growth_experiment_id!=OLD.growth_experiment_id
  OR NEW.owner_workspace_id!=OLD.owner_workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.deterministic_bucket!=OLD.deterministic_bucket OR NEW.variant_code!=OLD.variant_code
  OR NEW.assigned_at!=OLD.assigned_at
BEGIN SELECT RAISE(ABORT,'growth_assignment_immutable'); END;
CREATE TRIGGER trg_growth_assignment_delete_prd141 BEFORE DELETE ON platform_growth_experiment_assignments
BEGIN SELECT RAISE(ABORT,'growth_assignment_immutable'); END;

CREATE TABLE platform_conversion_attributions(
  conversion_attribution_id TEXT PRIMARY KEY CHECK(length(conversion_attribution_id)=36
    AND substr(conversion_attribution_id,1,4)='cat_'),
  conversion_source_kind TEXT NOT NULL CHECK(conversion_source_kind IN(
    'product_event','subscription','order','payment','retention')),
  conversion_source_id TEXT NOT NULL CHECK(length(conversion_source_id) BETWEEN 1 AND 128),
  conversion_stage TEXT NOT NULL CHECK(conversion_stage IN(
    'registered','activated','trial_started','paid','retained_30d')),
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_token_sha256 TEXT CHECK(subject_token_sha256 IS NULL OR length(subject_token_sha256)=64),
  subject_key_version INTEGER CHECK(subject_key_version IS NULL OR subject_key_version>=1),
  acquisition_source_id TEXT NOT NULL,
  growth_campaign_id TEXT,
  channel_partner_id TEXT,
  formula_version TEXT NOT NULL CHECK(formula_version='last-eligible-touch-30d-v1'),
  lookback_days INTEGER NOT NULL CHECK(lookback_days=30),
  value_minor INTEGER CHECK(value_minor IS NULL OR value_minor>=0),
  currency_code TEXT CHECK(currency_code IS NULL OR(
    length(currency_code)=3 AND currency_code=upper(currency_code))),
  consent_record_id TEXT,
  privacy_preference_revision INTEGER,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(owner_workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(acquisition_source_id)
    REFERENCES platform_acquisition_sources(acquisition_source_id),
  FOREIGN KEY(growth_campaign_id) REFERENCES platform_growth_campaigns(growth_campaign_id),
  FOREIGN KEY(channel_partner_id) REFERENCES channel_partners(channel_partner_id),
  FOREIGN KEY(consent_record_id) REFERENCES consent_records(consent_record_id),
  UNIQUE(conversion_source_kind,conversion_source_id,formula_version),
  CHECK((value_minor IS NULL AND currency_code IS NULL)
    OR(value_minor IS NOT NULL AND currency_code IS NOT NULL))
);
CREATE INDEX idx_growth_attribution_query_prd141
  ON platform_conversion_attributions(
    occurred_at,conversion_stage,acquisition_source_id,growth_campaign_id,channel_partner_id);
CREATE TRIGGER trg_growth_attribution_update_prd141 BEFORE UPDATE ON platform_conversion_attributions
BEGIN SELECT RAISE(ABORT,'growth_attribution_immutable'); END;
CREATE TRIGGER trg_growth_attribution_delete_prd141 BEFORE DELETE ON platform_conversion_attributions
BEGIN SELECT RAISE(ABORT,'growth_attribution_immutable'); END;

CREATE TABLE platform_growth_exports(
  growth_export_id TEXT PRIMARY KEY CHECK(length(growth_export_id)=36
    AND substr(growth_export_id,1,4)='gxp_'),
  requester_operator_id TEXT NOT NULL,
  requester_revision INTEGER NOT NULL CHECK(requester_revision>=1),
  requester_permission_sha256 TEXT NOT NULL CHECK(length(requester_permission_sha256)=64),
  query_kind TEXT NOT NULL CHECK(query_kind IN('funnels','cohorts','campaigns','experiments','attribution')),
  query_manifest_json TEXT NOT NULL CHECK(json_valid(query_manifest_json)
    AND json_type(query_manifest_json)='object'
    AND length(CAST(query_manifest_json AS BLOB))<=8192),
  query_sha256 TEXT NOT NULL CHECK(length(query_sha256)=64),
  schema_version TEXT NOT NULL CHECK(schema_version='platform-growth-export-v1'),
  formula_version TEXT NOT NULL CHECK(length(formula_version) BETWEEN 1 AND 80),
  sensitivity TEXT NOT NULL CHECK(sensitivity IN('aggregate','high_cardinality')),
  status TEXT NOT NULL CHECK(status IN(
    'requested','policy_review','approved','rejected','building','ready',
    'downloaded','expired','revoked','failed')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  row_count INTEGER CHECK(row_count IS NULL OR row_count>=0),
  cell_count INTEGER CHECK(cell_count IS NULL OR cell_count>=0),
  artifact_sha256 TEXT CHECK(artifact_sha256 IS NULL OR length(artifact_sha256)=64),
  artifact_key_reference TEXT CHECK(artifact_key_reference IS NULL OR(
    length(artifact_key_reference) BETWEEN 8 AND 256
    AND artifact_key_reference NOT GLOB '*[[:space:]]*')),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  download_count INTEGER NOT NULL CHECK(download_count IN(0,1)),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(requester_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(updated_at>=created_at),
  CHECK((status IN('ready','downloaded') AND artifact_sha256 IS NOT NULL
      AND artifact_key_reference IS NOT NULL AND expires_at IS NOT NULL
      AND row_count IS NOT NULL AND cell_count IS NOT NULL)
    OR(status NOT IN('ready','downloaded'))),
  CHECK((status='downloaded' AND download_count=1)
    OR(status!='downloaded' AND download_count=0))
);
CREATE INDEX idx_growth_exports_query_prd141
  ON platform_growth_exports(requester_operator_id,status,created_at DESC,growth_export_id);
CREATE TRIGGER trg_growth_export_owner_update_prd141 BEFORE UPDATE ON platform_growth_exports FOR EACH ROW
WHEN NEW.growth_export_id!=OLD.growth_export_id OR NEW.requester_operator_id!=OLD.requester_operator_id
  OR NEW.requester_revision!=OLD.requester_revision
  OR NEW.requester_permission_sha256!=OLD.requester_permission_sha256
  OR NEW.query_kind!=OLD.query_kind OR NEW.query_manifest_json!=OLD.query_manifest_json
  OR NEW.query_sha256!=OLD.query_sha256 OR NEW.schema_version!=OLD.schema_version
  OR NEW.formula_version!=OLD.formula_version OR NEW.sensitivity!=OLD.sensitivity
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.generation<OLD.generation OR NEW.download_count<OLD.download_count
BEGIN SELECT RAISE(ABORT,'growth_export_transition_invalid'); END;
CREATE TRIGGER trg_growth_export_delete_prd141 BEFORE DELETE ON platform_growth_exports
BEGIN SELECT RAISE(ABORT,'growth_export_immutable'); END;

CREATE TABLE platform_growth_export_subjects(
  growth_export_subject_id TEXT PRIMARY KEY CHECK(length(growth_export_subject_id)=36
    AND substr(growth_export_subject_id,1,4)='gxs_'),
  growth_export_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_digest TEXT NOT NULL CHECK(length(subject_digest)=64),
  key_version INTEGER CHECK(key_version IS NULL OR key_version>=1),
  status TEXT NOT NULL CHECK(status IN('active','destroyed')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  destroyed_at TEXT CHECK(destroyed_at IS NULL OR unixepoch(destroyed_at) IS NOT NULL),
  FOREIGN KEY(growth_export_id) REFERENCES platform_growth_exports(growth_export_id),
  FOREIGN KEY(owner_workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(growth_export_id,owner_workspace_id,subject_digest),
  CHECK((status='active' AND key_version IS NOT NULL AND destroyed_at IS NULL)
    OR(status='destroyed' AND key_version IS NULL AND destroyed_at IS NOT NULL))
);
CREATE TRIGGER trg_growth_export_subject_owner_update_prd141 BEFORE UPDATE ON platform_growth_export_subjects FOR EACH ROW
WHEN NEW.growth_export_subject_id!=OLD.growth_export_subject_id
  OR NEW.growth_export_id!=OLD.growth_export_id OR NEW.owner_workspace_id!=OLD.owner_workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.subject_digest!=OLD.subject_digest
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'growth_export_subject_immutable'); END;
CREATE TRIGGER trg_growth_export_subject_delete_prd141 BEFORE DELETE ON platform_growth_export_subjects
BEGIN SELECT RAISE(ABORT,'growth_export_subject_immutable'); END;

CREATE TABLE platform_growth_export_approvals(
  growth_export_approval_id TEXT PRIMARY KEY CHECK(length(growth_export_approval_id)=36
    AND substr(growth_export_approval_id,1,4)='gxa_'),
  growth_export_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  decision TEXT NOT NULL CHECK(decision IN('approved','rejected')),
  approver_operator_id TEXT NOT NULL,
  approver_revision INTEGER NOT NULL CHECK(approver_revision>=1),
  permission_sha256 TEXT NOT NULL CHECK(length(permission_sha256)=64),
  step_up_generation INTEGER NOT NULL CHECK(step_up_generation>=1),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(growth_export_id) REFERENCES platform_growth_exports(growth_export_id),
  FOREIGN KEY(approver_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(growth_export_id,generation)
);
CREATE TRIGGER trg_growth_export_approval_owner_prd141
BEFORE INSERT ON platform_growth_export_approvals FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM platform_growth_exports export
  WHERE export.growth_export_id=NEW.growth_export_id
    AND export.requester_operator_id=NEW.approver_operator_id)
BEGIN SELECT RAISE(ABORT,'growth_export_independent_approval_required'); END;
CREATE TRIGGER trg_growth_export_approval_update_prd141 BEFORE UPDATE ON platform_growth_export_approvals
BEGIN SELECT RAISE(ABORT,'growth_export_approval_immutable'); END;
CREATE TRIGGER trg_growth_export_approval_delete_prd141 BEFORE DELETE ON platform_growth_export_approvals
BEGIN SELECT RAISE(ABORT,'growth_export_approval_immutable'); END;

CREATE TABLE platform_growth_export_receipts(
  growth_export_receipt_id TEXT PRIMARY KEY CHECK(length(growth_export_receipt_id)=36
    AND substr(growth_export_receipt_id,1,4)='gxr_'),
  growth_export_id TEXT NOT NULL,
  requester_operator_id TEXT NOT NULL,
  receipt_hmac TEXT NOT NULL UNIQUE CHECK(length(receipt_hmac)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','consumed','expired','revoked')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  FOREIGN KEY(growth_export_id) REFERENCES platform_growth_exports(growth_export_id),
  FOREIGN KEY(requester_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(growth_export_id,generation),
  CHECK((status='consumed' AND consumed_at IS NOT NULL)
    OR(status!='consumed' AND consumed_at IS NULL)),
  CHECK(unixepoch(expires_at)>unixepoch(created_at))
);
CREATE TRIGGER trg_growth_export_receipt_owner_update_prd141 BEFORE UPDATE ON platform_growth_export_receipts FOR EACH ROW
WHEN NEW.growth_export_receipt_id!=OLD.growth_export_receipt_id
  OR NEW.growth_export_id!=OLD.growth_export_id OR NEW.requester_operator_id!=OLD.requester_operator_id
  OR NEW.receipt_hmac!=OLD.receipt_hmac OR NEW.generation!=OLD.generation
  OR NEW.expires_at!=OLD.expires_at OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'growth_export_receipt_immutable'); END;
CREATE TRIGGER trg_growth_export_receipt_delete_prd141 BEFORE DELETE ON platform_growth_export_receipts
BEGIN SELECT RAISE(ABORT,'growth_export_receipt_immutable'); END;

CREATE TABLE platform_growth_commands(
  growth_command_id TEXT PRIMARY KEY CHECK(length(growth_command_id)=36
    AND substr(growth_command_id,1,4)='gcm_'),
  operator_id TEXT NOT NULL,
  operator_revision INTEGER NOT NULL CHECK(operator_revision>=1),
  action_code TEXT NOT NULL CHECK(length(action_code) BETWEEN 3 AND 80
    AND action_code=lower(action_code) AND action_code NOT GLOB '*[^a-z0-9._-]*'),
  object_kind TEXT NOT NULL CHECK(object_kind IN('campaign','experiment','export')),
  object_id TEXT,
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  expected_revision INTEGER CHECK(expected_revision IS NULL OR expected_revision>=0),
  result_revision INTEGER CHECK(result_revision IS NULL OR result_revision>=1),
  status TEXT NOT NULL CHECK(status IN('committed','rejected')),
  result_code TEXT NOT NULL CHECK(length(result_code) BETWEEN 3 AND 80),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(operator_id,action_code,idempotency_key_hmac)
);
CREATE TRIGGER trg_growth_command_update_prd141 BEFORE UPDATE ON platform_growth_commands
BEGIN SELECT RAISE(ABORT,'growth_command_immutable'); END;
CREATE TRIGGER trg_growth_command_delete_prd141 BEFORE DELETE ON platform_growth_commands
BEGIN SELECT RAISE(ABORT,'growth_command_immutable'); END;

CREATE TABLE platform_growth_events(
  growth_event_id TEXT PRIMARY KEY CHECK(length(growth_event_id)=36
    AND substr(growth_event_id,1,4)='gev_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'growth.conversion_recorded','growth.campaign_changed',
    'growth.experiment_decided','growth.exported')),
  object_kind TEXT NOT NULL CHECK(object_kind IN(
    'conversion','campaign','experiment','export')),
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  object_revision INTEGER NOT NULL CHECK(object_revision>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  UNIQUE(event_type,object_id,generation)
);
CREATE TRIGGER trg_growth_event_update_prd141 BEFORE UPDATE ON platform_growth_events
BEGIN SELECT RAISE(ABORT,'growth_event_immutable'); END;
CREATE TRIGGER trg_growth_event_delete_prd141 BEFORE DELETE ON platform_growth_events
BEGIN SELECT RAISE(ABORT,'growth_event_immutable'); END;

CREATE TABLE platform_growth_event_outbox(
  growth_event_outbox_id TEXT PRIMARY KEY CHECK(length(growth_event_outbox_id)=37
    AND substr(growth_event_outbox_id,1,5)='gevo_'),
  growth_event_id TEXT NOT NULL UNIQUE,
  stable_event_id TEXT NOT NULL UNIQUE CHECK(length(stable_event_id)=64),
  status TEXT NOT NULL CHECK(status IN('pending','leased','sent','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  generation INTEGER NOT NULL CHECK(generation>=0),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR length(lease_token_sha256)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  subscription_snapshot_json TEXT CHECK(subscription_snapshot_json IS NULL OR(
    json_valid(subscription_snapshot_json) AND json_type(subscription_snapshot_json)='array')),
  snapshot_frozen_at TEXT CHECK(
    snapshot_frozen_at IS NULL OR unixepoch(snapshot_frozen_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'growth.delivery_unavailable','growth.payload_invalid')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(growth_event_id) REFERENCES platform_growth_events(growth_event_id),
  CHECK((status='pending' AND lease_owner_id IS NULL AND lease_token_sha256 IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NOT NULL AND delivered_at IS NULL)
    OR(status='leased' AND lease_owner_id IS NOT NULL AND lease_token_sha256 IS NOT NULL
      AND lease_expires_at IS NOT NULL AND next_attempt_at IS NULL AND delivered_at IS NULL)
    OR(status='sent' AND lease_owner_id IS NULL AND lease_token_sha256 IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NULL AND delivered_at IS NOT NULL)
    OR(status='failed' AND attempt_count=10 AND lease_owner_id IS NULL
      AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NULL AND delivered_at IS NULL AND last_error_code IS NOT NULL)),
  CHECK((subscription_snapshot_json IS NULL AND snapshot_frozen_at IS NULL)
    OR(subscription_snapshot_json IS NOT NULL AND snapshot_frozen_at IS NOT NULL))
);
CREATE INDEX idx_growth_event_outbox_claim_prd141
  ON platform_growth_event_outbox(status,next_attempt_at,lease_expires_at,created_at);
CREATE TRIGGER trg_growth_outbox_delete_prd141 BEFORE DELETE ON platform_growth_event_outbox
BEGIN SELECT RAISE(ABORT,'growth_outbox_immutable'); END;

CREATE TABLE platform_growth_migration_cursors(
  source_code TEXT PRIMARY KEY CHECK(source_code IN(
    'product_events','product_event_outbox','workspace_bindings','consent','billing')),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  target_row_count INTEGER NOT NULL CHECK(target_row_count>=0),
  pending_count INTEGER NOT NULL CHECK(pending_count>=0),
  leased_count INTEGER NOT NULL CHECK(leased_count>=0),
  sent_count INTEGER NOT NULL CHECK(sent_count>=0),
  failed_count INTEGER NOT NULL CHECK(failed_count>=0),
  source_watermark TEXT,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=0),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO platform_growth_migration_cursors VALUES
  ('product_events',0,0,0,0,0,0,NULL,metahub_sha256(''),0,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('product_event_outbox',0,0,0,0,0,0,NULL,metahub_sha256(''),0,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('workspace_bindings',0,0,0,0,0,0,NULL,metahub_sha256(''),0,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('consent',0,0,0,0,0,0,NULL,metahub_sha256(''),0,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('billing',0,0,0,0,0,0,NULL,metahub_sha256(''),0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_growth_cursor_delete_prd141 BEFORE DELETE ON platform_growth_migration_cursors
BEGIN SELECT RAISE(ABORT,'growth_cursor_immutable'); END;

CREATE TABLE platform_growth_scheduler_state(
  job_code TEXT PRIMARY KEY CHECK(job_code IN(
    'source_eligibility','daily_funnel','cohort_projection','attribution',
    'experiment_guardrail','export_build','privacy_correction',
    'growth_event_outbox','product_event_outbox')),
  status TEXT NOT NULL CHECK(status IN('idle','leased','paused','failed')),
  generation INTEGER NOT NULL CHECK(generation>=0),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR length(lease_token_sha256)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  cursor_json TEXT CHECK(cursor_json IS NULL OR(
    json_valid(cursor_json) AND json_type(cursor_json)='object'
    AND length(CAST(cursor_json AS BLOB))<=4096)),
  source_watermark TEXT,
  next_run_at TEXT NOT NULL CHECK(unixepoch(next_run_at) IS NOT NULL),
  consecutive_failure_count INTEGER NOT NULL CHECK(consecutive_failure_count BETWEEN 0 AND 20),
  last_error_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((status='leased' AND lease_owner_id IS NOT NULL AND lease_token_sha256 IS NOT NULL
      AND lease_expires_at IS NOT NULL)
    OR(status!='leased' AND lease_owner_id IS NULL AND lease_token_sha256 IS NULL
      AND lease_expires_at IS NULL))
);
INSERT INTO platform_growth_scheduler_state
SELECT job_code,'idle',0,NULL,NULL,NULL,NULL,NULL,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),0,NULL,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (SELECT 'source_eligibility' AS job_code UNION ALL SELECT 'daily_funnel'
  UNION ALL SELECT 'cohort_projection' UNION ALL SELECT 'attribution'
  UNION ALL SELECT 'experiment_guardrail' UNION ALL SELECT 'export_build'
  UNION ALL SELECT 'privacy_correction' UNION ALL SELECT 'growth_event_outbox'
  UNION ALL SELECT 'product_event_outbox');
CREATE TRIGGER trg_growth_scheduler_delete_prd141 BEFORE DELETE ON platform_growth_scheduler_state
BEGIN SELECT RAISE(ABORT,'growth_scheduler_immutable'); END;

-- Legacy ProductEvent remains untouched. Only owner-qualified references and digests are added.
INSERT INTO platform_growth_event_eligibility(
  growth_event_eligibility_id,product_event_id,owner_workspace_id,tenant_id,
  formula_version,subject_token_sha256,subject_key_version,consent_record_id,
  privacy_preference_revision,purpose_code,purpose_version,status,reason_code,
  evaluated_at,canonical_sha256,created_at,updated_at)
SELECT 'gel_'||substr(metahub_sha256(event.product_event_id||'growth-eligibility-v2'),1,32),
  event.product_event_id,binding.workspace_id,event.tenant_id,'growth-eligibility-v2',
  NULL,NULL,NULL,NULL,'analytics','analytics-v2','excluded',
  'historical_consent_unproven',strftime('%Y-%m-%dT%H:%M:%fZ','now'),
  metahub_sha256(json_array(event.product_event_id,binding.workspace_id,event.tenant_id,
    'growth-eligibility-v2','excluded','historical_consent_unproven')),
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM product_events event
JOIN workspace_tenant_bindings binding ON binding.tenant_id=event.tenant_id
WHERE binding.status='active';

INSERT INTO platform_growth_legacy_mappings(
  growth_legacy_mapping_id,owner_workspace_id,tenant_id,source_kind,source_id,
  source_sha256,status,conflict_code,mapped_at)
SELECT 'glm_'||substr(metahub_sha256('product_event'||event.product_event_id),1,32),
  binding.workspace_id,event.tenant_id,'product_event',event.product_event_id,
  metahub_sha256(json_array(event.product_event_id,event.tenant_id,event.event_type,
    event.actor_user_id,event.source_kind,event.source_id,event.operation_request_id,
    event.operation_trace_id,event.capture_mode,event.status,event.occurred_at,
    event.created_at,event.updated_at)),'mapped',NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM product_events event JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=event.tenant_id AND binding.status='active';
INSERT INTO platform_growth_legacy_mappings(
  growth_legacy_mapping_id,owner_workspace_id,tenant_id,source_kind,source_id,
  source_sha256,status,conflict_code,mapped_at)
SELECT 'glm_'||substr(metahub_sha256('product_event_outbox'||outbox.product_event_outbox_id),1,32),
  binding.workspace_id,outbox.tenant_id,'product_event_outbox',outbox.product_event_outbox_id,
  metahub_sha256(json_array(outbox.product_event_outbox_id,outbox.tenant_id,
    outbox.product_event_id,outbox.status,outbox.attempt_count,outbox.generation,
    outbox.lease_owner_id,outbox.lease_token,outbox.lease_expires_at,
    outbox.next_attempt_at,outbox.subscription_snapshot_json,outbox.snapshot_frozen_at,
    outbox.last_error_code,outbox.delivered_at,outbox.created_at,outbox.updated_at)),
  'mapped',NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM product_event_outbox outbox JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=outbox.tenant_id AND binding.status='active';

INSERT INTO platform_growth_legacy_reconciliations(
  growth_legacy_reconciliation_id,owner_workspace_id,tenant_id,formula_version,
  source_row_count,registered_count,activated_count,cumulative_stage_count,
  source_sha256,reconciled_at)
SELECT 'glr_'||substr(metahub_sha256(binding.tenant_id||'legacy-activation-v1'),1,32),
  binding.workspace_id,binding.tenant_id,'legacy-activation-v1',count(event.product_event_id),
  count(DISTINCT CASE WHEN event.event_type='tenant.registered' THEN event.product_event_id END),
  count(DISTINCT CASE WHEN event.event_type='gateway.first_call_succeeded' THEN event.product_event_id END),
  (SELECT count(*) FROM(
    SELECT stage.event_type FROM(SELECT 'tenant.registered' event_type,1 stage_order
      UNION ALL SELECT 'provider.created',2 UNION ALL SELECT 'virtual_key.created',3
      UNION ALL SELECT 'gateway.first_call_succeeded',4 UNION ALL SELECT 'budget.configured',5
      UNION ALL SELECT 'teammate.invited',6 UNION ALL SELECT 'request_logs.viewed',7) stage
    WHERE NOT EXISTS(SELECT 1 FROM(
      SELECT prior.event_type FROM(SELECT 'tenant.registered' event_type,1 stage_order
        UNION ALL SELECT 'provider.created',2 UNION ALL SELECT 'virtual_key.created',3
        UNION ALL SELECT 'gateway.first_call_succeeded',4 UNION ALL SELECT 'budget.configured',5
        UNION ALL SELECT 'teammate.invited',6 UNION ALL SELECT 'request_logs.viewed',7) prior
      WHERE prior.stage_order<=stage.stage_order AND NOT EXISTS(
        SELECT 1 FROM product_events source_event
        WHERE source_event.tenant_id=binding.tenant_id
          AND source_event.event_type=prior.event_type))))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT product_event_id||char(31)||event_type||char(31)||occurred_at AS record
    FROM product_events source_event WHERE source_event.tenant_id=binding.tenant_id
    ORDER BY product_event_id)),'')),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_tenant_bindings binding
JOIN product_events event ON event.tenant_id=binding.tenant_id
WHERE binding.status='active'
GROUP BY binding.workspace_id,binding.tenant_id;

UPDATE platform_growth_migration_cursors SET
  source_row_count=(SELECT count(*) FROM product_events),
  target_row_count=(SELECT count(*) FROM platform_growth_event_eligibility),
  source_watermark=(SELECT max(occurred_at) FROM product_events),
  source_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT product_event_id||char(31)||event_type||char(31)||status||char(31)||occurred_at AS record
    FROM product_events ORDER BY product_event_id)),'')),
  generation=CASE WHEN EXISTS(SELECT 1 FROM product_events) THEN 1 ELSE 0 END,
  updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE source_code='product_events';
UPDATE platform_growth_migration_cursors SET
  source_row_count=(SELECT count(*) FROM product_event_outbox),
  target_row_count=(SELECT count(*) FROM platform_growth_legacy_mappings
    WHERE source_kind='product_event_outbox'),
  pending_count=(SELECT count(*) FROM product_event_outbox WHERE status='pending'),
  leased_count=(SELECT count(*) FROM product_event_outbox WHERE status='leased'),
  sent_count=(SELECT count(*) FROM product_event_outbox WHERE status='sent'),
  failed_count=(SELECT count(*) FROM product_event_outbox WHERE status='failed'),
  source_watermark=(SELECT max(updated_at) FROM product_event_outbox),
  source_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT product_event_outbox_id||char(31)||status||char(31)||attempt_count||char(31)
      ||generation||char(31)||coalesce(delivered_at,'') AS record
    FROM product_event_outbox ORDER BY product_event_outbox_id)),'')),
  generation=CASE WHEN EXISTS(SELECT 1 FROM product_event_outbox) THEN 1 ELSE 0 END,
  updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE source_code='product_event_outbox';
UPDATE platform_growth_migration_cursors SET
  source_row_count=(SELECT count(*) FROM workspace_tenant_bindings WHERE status='active'),
  target_row_count=(SELECT count(DISTINCT tenant_id) FROM platform_growth_legacy_mappings),
  source_watermark=(SELECT max(created_at) FROM workspace_tenant_bindings WHERE status='active'),
  source_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT workspace_id||char(31)||tenant_id AS record FROM workspace_tenant_bindings
    WHERE status='active' ORDER BY workspace_id)),'')),
  generation=CASE WHEN EXISTS(SELECT 1 FROM platform_growth_legacy_mappings) THEN 1 ELSE 0 END,
  updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE source_code='workspace_bindings';

CREATE TABLE tenant_deletion_manifest_extensions_prd141(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-141'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd141_manifest_sources(
  source_table TEXT PRIMARY KEY,disposition TEXT NOT NULL,retention_class TEXT NOT NULL);
INSERT INTO prd141_manifest_sources VALUES
  ('platform_growth_event_eligibility','archive_then_delete','security'),
  ('platform_growth_legacy_reconciliations','archive_then_delete','audit'),
  ('platform_growth_legacy_mappings','archive_then_delete','audit'),
  ('platform_growth_experiment_assignments','archive_then_delete','security'),
  ('platform_conversion_attributions','archive_then_delete','operational'),
  ('platform_growth_export_subjects','archive_then_delete','security');
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT table_record.name,source.disposition,source.retention_class,
  metahub_sha256(table_record.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
    FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
      AND trigger_record.tbl_name=table_record.name ORDER BY trigger_record.name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema table_record JOIN prd141_manifest_sources source
  ON source.source_table=table_record.name WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd141
SELECT source_table,'tenant_id','PRD-141',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd141_manifest_sources ORDER BY source_table;
DROP TABLE prd141_manifest_sources;
DELETE FROM tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd141_insert BEFORE INSERT ON tenant_deletion_manifest_extensions_prd141 BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd141_update BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd141 BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd141_delete BEFORE DELETE ON tenant_deletion_manifest_extensions_prd141 BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd141_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd141_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_growth_permission_extensions)=8
      AND (SELECT COUNT(*) FROM platform_growth_role_permissions)=17
      AND (SELECT COUNT(*) FROM platform_growth_migration_cursors)=5
      AND (SELECT COUNT(*) FROM platform_growth_scheduler_state)=9
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd141)=6
      AND (SELECT COUNT(*) FROM platform_growth_event_eligibility)=
        (SELECT COUNT(*) FROM product_events event JOIN workspace_tenant_bindings binding
          ON binding.tenant_id=event.tenant_id AND binding.status='active')
      AND (SELECT COUNT(*) FROM platform_growth_legacy_mappings)=
        ((SELECT COUNT(*) FROM product_events event JOIN workspace_tenant_bindings binding
            ON binding.tenant_id=event.tenant_id AND binding.status='active')
        +(SELECT COUNT(*) FROM product_event_outbox outbox
          JOIN workspace_tenant_bindings binding
            ON binding.tenant_id=outbox.tenant_id AND binding.status='active'))
    THEN 0 ELSE 1 END
  + CASE WHEN EXISTS(SELECT 1 FROM permissions WHERE code='product_analytics.read')
      OR EXISTS(SELECT 1 FROM rbac_permission_templates
        WHERE permission_code='product_analytics.read')
      OR EXISTS(SELECT 1 FROM rbac_role_permission_templates
        WHERE permission_code='product_analytics.read') THEN 1 ELSE 0 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd141_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
