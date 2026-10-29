-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd140_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd140_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0131' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','workspace_operational_states','workspace_health_projections',
      'customer_health_scores','customer_health_score_outbox',
      'customer_health_scheduler_tenants','workspace_subscriptions','support_tickets',
      'support_sla_clocks','platform_operators','tenant_deletion_table_manifest',
      'tenant_deletion_trigger_manifest'))=12 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM customer_health_scores score
      LEFT JOIN workspace_tenant_bindings binding ON binding.tenant_id=score.tenant_id
      WHERE binding.workspace_id IS NULL OR binding.status!='active') THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM workspace_health_projections projection
      JOIN customer_health_scores score
        ON score.tenant_id=projection.tenant_id
       AND score.customer_health_score_id=projection.source_score_id
      WHERE score.customer_health_score_sequence!=projection.source_sequence
         OR score.risk_level!=projection.risk_level
         OR score.health_projection_sha256!=projection.source_projection_sha256
         OR score.evaluated_at!=projection.source_evaluated_at
         OR score.customer_health_score_sequence!=(
           SELECT max(latest.customer_health_score_sequence)
           FROM customer_health_scores latest WHERE latest.tenant_id=projection.tenant_id))
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM customer_health_scheduler_tenants
      WHERE status='leased' AND unixepoch(lease_expires_at)>unixepoch('now'))
  + (SELECT COUNT(*) FROM customer_health_score_outbox
      WHERE status='leased' AND unixepoch(lease_expires_at)>unixepoch('now'))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd140')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd140_up_guard;

-- Retire the tenant-scoped customer health capability without changing unrelated grants.
CREATE TEMP TABLE prd140_roles_to_contract(role_id TEXT PRIMARY KEY);
INSERT INTO prd140_roles_to_contract
SELECT DISTINCT role_permission.role_id
FROM role_permissions role_permission
JOIN permissions permission ON permission.permission_id=role_permission.permission_id
WHERE permission.code='customer_health.read';
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
  SELECT permission_id FROM permissions WHERE code='customer_health.read');
DELETE FROM rbac_role_permission_templates WHERE permission_code='customer_health.read';
DELETE FROM permissions WHERE code='customer_health.read';
DELETE FROM rbac_permission_templates WHERE permission_code='customer_health.read';
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
WHERE role_id IN(SELECT role_id FROM prd140_roles_to_contract);
DROP TABLE prd140_roles_to_contract;

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
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN '9af89117d6e3eb365c9c90f61778d0fe7408d03a50a6cd32a0d6eb4e61e772fb' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN '71c49cf7d47d0a5092eb50c3727cb93a1409f2f1fe3359d47e477c2f6eefdc6b' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-140 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN '9af89117d6e3eb365c9c90f61778d0fe7408d03a50a6cd32a0d6eb4e61e772fb' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN '71c49cf7d47d0a5092eb50c3727cb93a1409f2f1fe3359d47e477c2f6eefdc6b' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,27,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

CREATE TABLE platform_customer_success_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.customer_success.read','platform.customer_success.support_read',
    'platform.customer_success.finance_read','platform.customer_success.contact.read',
    'platform.customer_success.plan.manage','platform.customer_success.task.manage',
    'platform.customer_success.renewal.read','platform.customer_success.batch.approve')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE TABLE platform_customer_success_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_customer_success_permission_extensions(permission_code)
);
INSERT INTO platform_customer_success_permission_extensions VALUES
  ('platform.customer_success.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.customer_success.support_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.customer_success.finance_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.customer_success.contact.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.customer_success.plan.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.customer_success.task.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.customer_success.renewal.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.customer_success.batch.approve',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
INSERT INTO platform_customer_success_role_permissions VALUES
  ('platform_owner','platform.customer_success.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.customer_success.support_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.customer_success.finance_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.customer_success.contact.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.customer_success.plan.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.customer_success.task.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.customer_success.renewal.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.customer_success.batch.approve',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_customer_success','platform.customer_success.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_customer_success','platform.customer_success.contact.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_customer_success','platform.customer_success.plan.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_customer_success','platform.customer_success.task.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_customer_success','platform.customer_success.renewal.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_support','platform.customer_success.support_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_billing','platform.customer_success.finance_read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_billing','platform.customer_success.renewal.read',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_platform_customer_permissions_insert_prd140 BEFORE INSERT ON platform_customer_success_permission_extensions BEGIN SELECT RAISE(ABORT,'customer_success_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_permissions_update_prd140 BEFORE UPDATE ON platform_customer_success_permission_extensions BEGIN SELECT RAISE(ABORT,'customer_success_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_permissions_delete_prd140 BEFORE DELETE ON platform_customer_success_permission_extensions BEGIN SELECT RAISE(ABORT,'customer_success_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_roles_insert_prd140 BEFORE INSERT ON platform_customer_success_role_permissions BEGIN SELECT RAISE(ABORT,'customer_success_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_roles_update_prd140 BEFORE UPDATE ON platform_customer_success_role_permissions BEGIN SELECT RAISE(ABORT,'customer_success_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_roles_delete_prd140 BEFORE DELETE ON platform_customer_success_role_permissions BEGIN SELECT RAISE(ABORT,'customer_success_permission_immutable'); END;

CREATE TABLE platform_customer_account_projections(
  customer_account_id TEXT PRIMARY KEY CHECK(length(customer_account_id)=36
    AND substr(customer_account_id,1,4)='csa_'
    AND substr(customer_account_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  workspace_revision INTEGER NOT NULL CHECK(workspace_revision>=1),
  display_name TEXT NOT NULL CHECK(length(CAST(display_name AS BLOB)) BETWEEN 1 AND 160),
  slug TEXT NOT NULL CHECK(length(slug) BETWEEN 3 AND 48),
  workspace_kind TEXT NOT NULL CHECK(workspace_kind IN('personal','organization')),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 40),
  operational_status TEXT NOT NULL CHECK(operational_status IN(
    'active','suspended','deletion_pending','deleted','migration_conflict')),
  activation_phase TEXT NOT NULL CHECK(activation_phase IN(
    'not_started','in_progress','activated')),
  activation_completed_steps INTEGER NOT NULL CHECK(activation_completed_steps BETWEEN 0 AND 8),
  adoption_band TEXT NOT NULL CHECK(adoption_band IN('unknown','low','medium','high')),
  subscription_id TEXT,
  plan_version_id TEXT,
  subscription_status TEXT NOT NULL CHECK(subscription_status IN(
    'not_configured','pending','active','trialing','past_due','canceled','expired')),
  renewal_at TEXT CHECK(renewal_at IS NULL OR unixepoch(renewal_at) IS NOT NULL),
  latest_health_snapshot_id TEXT,
  latest_health_sequence INTEGER CHECK(latest_health_sequence IS NULL OR latest_health_sequence>=1),
  health_risk TEXT NOT NULL CHECK(health_risk IN(
    'unknown','healthy','watch','at_risk','critical')),
  open_support_ticket_count INTEGER NOT NULL CHECK(open_support_ticket_count>=0),
  breached_sla_count INTEGER NOT NULL CHECK(breached_sla_count>=0),
  source_revision_sha256 TEXT NOT NULL CHECK(length(source_revision_sha256)=64
    AND source_revision_sha256 NOT GLOB '*[^0-9a-f]*'),
  projection_version TEXT NOT NULL CHECK(projection_version='customer-account-v1'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  projected_at TEXT NOT NULL CHECK(unixepoch(projected_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(subscription_id) REFERENCES workspace_subscriptions(subscription_id),
  UNIQUE(customer_account_id,workspace_id,tenant_id),
  CHECK((subscription_status='not_configured' AND subscription_id IS NULL
      AND plan_version_id IS NULL AND renewal_at IS NULL)
    OR(subscription_status!='not_configured' AND subscription_id IS NOT NULL
      AND plan_version_id IS NOT NULL)),
  CHECK((latest_health_snapshot_id IS NULL AND latest_health_sequence IS NULL
      AND health_risk='unknown')
    OR(latest_health_snapshot_id IS NOT NULL AND latest_health_sequence IS NOT NULL
      AND health_risk!='unknown'))
);
CREATE INDEX idx_platform_customer_directory_prd140
  ON platform_customer_account_projections(updated_at DESC,customer_account_id);
CREATE INDEX idx_platform_customer_filter_prd140
  ON platform_customer_account_projections(
    operational_status,region_code,activation_phase,adoption_band,health_risk,
    subscription_status,updated_at DESC,customer_account_id);

CREATE TABLE platform_customer_contacts(
  customer_contact_id TEXT PRIMARY KEY CHECK(length(customer_contact_id)=36
    AND substr(customer_contact_id,1,4)='cct_'
    AND substr(customer_contact_id,5) NOT GLOB '*[^0-9a-f]*'),
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  workspace_membership_id TEXT NOT NULL,
  contact_role TEXT NOT NULL CHECK(contact_role IN(
    'primary','billing','technical','security')),
  status TEXT NOT NULL CHECK(status IN('active','unavailable')),
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  notification_preference_revision INTEGER NOT NULL CHECK(notification_preference_revision>=0),
  preference_summary_json TEXT NOT NULL CHECK(json_valid(preference_summary_json)
    AND json_type(preference_summary_json)='array'
    AND length(CAST(preference_summary_json AS BLOB))<=2048),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,account_id) REFERENCES workspace_memberships(workspace_id,account_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(customer_account_id,account_id,contact_role)
);
CREATE INDEX idx_platform_customer_contacts_prd140
  ON platform_customer_contacts(customer_account_id,status,contact_role,customer_contact_id);

INSERT INTO platform_customer_account_projections(
  customer_account_id,workspace_id,tenant_id,workspace_revision,display_name,slug,
  workspace_kind,region_code,operational_status,activation_phase,
  activation_completed_steps,adoption_band,subscription_id,plan_version_id,
  subscription_status,renewal_at,latest_health_snapshot_id,latest_health_sequence,
  health_risk,open_support_ticket_count,breached_sla_count,source_revision_sha256,
  projection_version,revision,projected_at,created_at,updated_at)
SELECT 'csa_'||substr(metahub_sha256('customer-account-v1'||char(31)||binding.workspace_id),1,32),
  binding.workspace_id,binding.tenant_id,workspace.revision,
  coalesce(organization.name,personal.display_label,binding.workspace_id),
  coalesce(organization.slug_normalized,'ws-'||substr(binding.workspace_id,5,12)),
  workspace.kind,coalesce(region.region_code,'unknown'),
  coalesce(operational.status,'migration_conflict'),
  CASE WHEN coalesce(onboarding.completed_steps,0)=8 THEN 'activated'
    WHEN coalesce(onboarding.completed_steps,0)>0 THEN 'in_progress' ELSE 'not_started' END,
  coalesce(onboarding.completed_steps,0),
  CASE WHEN coalesce(onboarding.completed_steps,0)=8 THEN 'high'
    WHEN coalesce(onboarding.completed_steps,0)>=4 THEN 'medium'
    WHEN coalesce(onboarding.completed_steps,0)>0 THEN 'low' ELSE 'unknown' END,
  subscription.subscription_id,subscription.plan_version_id,
  coalesce(subscription.status,'not_configured'),subscription.current_period_ends_at,
  NULL,NULL,'unknown',
  (SELECT count(*) FROM support_tickets ticket
    WHERE ticket.workspace_id=binding.workspace_id AND ticket.tenant_id=binding.tenant_id
      AND ticket.status NOT IN('resolved','closed')),
  (SELECT count(*) FROM support_sla_clocks clock
    WHERE clock.workspace_id=binding.workspace_id AND clock.tenant_id=binding.tenant_id
      AND clock.state!='stopped' AND unixepoch(clock.resolution_deadline_at)<unixepoch('now')),
  metahub_sha256(json_array('customer-account-v1',workspace.revision,
    coalesce(operational.revision,0),coalesce(onboarding.revision,0),
    coalesce(subscription.state_revision,0))),
  'customer-account-v1',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'),
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_tenant_bindings binding
JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
LEFT JOIN organization_profiles organization ON organization.workspace_id=binding.workspace_id
LEFT JOIN personal_profiles personal ON personal.workspace_id=binding.workspace_id
LEFT JOIN workspace_regions region ON region.workspace_id=binding.workspace_id
LEFT JOIN workspace_operational_states operational ON operational.workspace_id=binding.workspace_id
LEFT JOIN onboarding_progress onboarding ON onboarding.workspace_id=binding.workspace_id
LEFT JOIN workspace_subscriptions subscription ON subscription.workspace_id=binding.workspace_id
WHERE binding.status='active';

INSERT INTO platform_customer_contacts(
  customer_contact_id,customer_account_id,workspace_id,tenant_id,account_id,
  workspace_membership_id,contact_role,status,membership_revision,
  notification_preference_revision,preference_summary_json,revision,created_at,updated_at)
SELECT 'cct_'||substr(metahub_sha256('customer-contact-v1'||char(31)
    ||membership.workspace_membership_id),1,32),account.customer_account_id,
  membership.workspace_id,membership.tenant_id,membership.account_id,
  membership.workspace_membership_id,'primary','active',membership.revision,0,'[]',1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_memberships membership
JOIN platform_customer_account_projections account
  ON account.workspace_id=membership.workspace_id AND account.tenant_id=membership.tenant_id
WHERE membership.status='active' AND membership.role_code='owner';

CREATE TABLE platform_customer_health_snapshots(
  customer_health_snapshot_sequence INTEGER PRIMARY KEY CHECK(
    typeof(customer_health_snapshot_sequence)='integer'
    AND customer_health_snapshot_sequence>0),
  customer_health_snapshot_id TEXT NOT NULL UNIQUE CHECK(
    length(customer_health_snapshot_id)=36
    AND substr(customer_health_snapshot_id,1,4)='chs_'
    AND substr(customer_health_snapshot_id,5) NOT GLOB '*[^0-9a-f]*'),
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  formula_version TEXT NOT NULL CHECK(formula_version IN(
    'customer-health-v1','customer-health-platform-v2')),
  window_start TEXT NOT NULL CHECK(unixepoch(window_start) IS NOT NULL),
  window_end TEXT NOT NULL CHECK(unixepoch(window_end) IS NOT NULL),
  comparison_start TEXT NOT NULL CHECK(unixepoch(comparison_start) IS NOT NULL),
  comparison_end TEXT NOT NULL CHECK(unixepoch(comparison_end) IS NOT NULL),
  source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint)=64
    AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  health_projection_sha256 TEXT NOT NULL CHECK(length(health_projection_sha256)=64
    AND health_projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  current_request_count INTEGER NOT NULL CHECK(current_request_count BETWEEN 0 AND 922337203685477),
  previous_request_count INTEGER NOT NULL CHECK(previous_request_count BETWEEN 0 AND 922337203685477),
  successful_request_count INTEGER NOT NULL CHECK(successful_request_count BETWEEN 0 AND 922337203685477),
  failed_request_count INTEGER NOT NULL CHECK(failed_request_count BETWEEN 0 AND 922337203685477),
  unresolved_cost_anomaly_count INTEGER NOT NULL CHECK(unresolved_cost_anomaly_count BETWEEN 0 AND 922337203685477),
  critical_cost_anomaly_count INTEGER NOT NULL CHECK(critical_cost_anomaly_count BETWEEN 0 AND 922337203685477),
  active_budget_rule_count INTEGER NOT NULL CHECK(active_budget_rule_count BETWEEN 0 AND 922337203685477),
  eligible_user_count INTEGER NOT NULL CHECK(eligible_user_count BETWEEN 0 AND 922337203685477),
  recently_active_user_count INTEGER NOT NULL CHECK(recently_active_user_count BETWEEN 0 AND 922337203685477),
  activation_stage_count INTEGER NOT NULL CHECK(activation_stage_count BETWEEN 0 AND 8),
  open_support_ticket_count INTEGER NOT NULL CHECK(open_support_ticket_count BETWEEN 0 AND 922337203685477),
  open_p1_ticket_count INTEGER NOT NULL CHECK(open_p1_ticket_count BETWEEN 0 AND 922337203685477),
  open_p2_ticket_count INTEGER NOT NULL CHECK(open_p2_ticket_count BETWEEN 0 AND 922337203685477),
  waiting_platform_ticket_count INTEGER NOT NULL CHECK(waiting_platform_ticket_count BETWEEN 0 AND 922337203685477),
  sla_at_risk_count INTEGER NOT NULL CHECK(sla_at_risk_count BETWEEN 0 AND 922337203685477),
  sla_breached_count INTEGER NOT NULL CHECK(sla_breached_count BETWEEN 0 AND 922337203685477),
  ticket_source_status TEXT NOT NULL CHECK(ticket_source_status IN('unavailable','available')),
  usage_trend_score_bps INTEGER NOT NULL CHECK(usage_trend_score_bps BETWEEN 0 AND 10000),
  usage_trend_contribution_bps INTEGER NOT NULL CHECK(usage_trend_contribution_bps BETWEEN 0 AND 10000),
  reliability_score_bps INTEGER NOT NULL CHECK(reliability_score_bps BETWEEN 0 AND 10000),
  reliability_contribution_bps INTEGER NOT NULL CHECK(reliability_contribution_bps BETWEEN 0 AND 10000),
  cost_anomaly_health_score_bps INTEGER NOT NULL CHECK(cost_anomaly_health_score_bps BETWEEN 0 AND 10000),
  cost_anomaly_health_contribution_bps INTEGER NOT NULL CHECK(cost_anomaly_health_contribution_bps BETWEEN 0 AND 10000),
  budget_readiness_score_bps INTEGER NOT NULL CHECK(budget_readiness_score_bps BETWEEN 0 AND 10000),
  budget_readiness_contribution_bps INTEGER NOT NULL CHECK(budget_readiness_contribution_bps BETWEEN 0 AND 10000),
  team_activity_score_bps INTEGER NOT NULL CHECK(team_activity_score_bps BETWEEN 0 AND 10000),
  team_activity_contribution_bps INTEGER NOT NULL CHECK(team_activity_contribution_bps BETWEEN 0 AND 10000),
  ticket_health_score_bps INTEGER CHECK(ticket_health_score_bps IS NULL
    OR ticket_health_score_bps BETWEEN 0 AND 10000),
  ticket_health_contribution_bps INTEGER NOT NULL CHECK(ticket_health_contribution_bps BETWEEN 0 AND 10000),
  effective_weight_bps INTEGER NOT NULL CHECK(effective_weight_bps IN(9000,10000)),
  score_bps INTEGER NOT NULL CHECK(score_bps BETWEEN 0 AND 10000),
  risk_level TEXT NOT NULL CHECK(risk_level IN('healthy','watch','at_risk','critical')),
  usage_declining INTEGER NOT NULL CHECK(usage_declining IN(0,1)),
  high_error_rate INTEGER NOT NULL CHECK(high_error_rate IN(0,1)),
  cost_anomaly_open INTEGER NOT NULL CHECK(cost_anomaly_open IN(0,1)),
  budget_missing INTEGER NOT NULL CHECK(budget_missing IN(0,1)),
  team_inactive INTEGER NOT NULL CHECK(team_inactive IN(0,1)),
  operation_request_id TEXT NOT NULL CHECK(length(operation_request_id) BETWEEN 8 AND 96),
  operation_trace_id TEXT NOT NULL CHECK(length(operation_trace_id) BETWEEN 8 AND 96),
  evaluated_at TEXT NOT NULL CHECK(unixepoch(evaluated_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  UNIQUE(customer_account_id,customer_health_snapshot_id),
  UNIQUE(customer_account_id,window_end,source_fingerprint),
  CHECK(successful_request_count+failed_request_count=current_request_count),
  CHECK(critical_cost_anomaly_count<=unresolved_cost_anomaly_count),
  CHECK(recently_active_user_count<=eligible_user_count),
  CHECK(open_p1_ticket_count+open_p2_ticket_count<=open_support_ticket_count),
  CHECK(waiting_platform_ticket_count<=open_support_ticket_count),
  CHECK(sla_breached_count<=open_support_ticket_count),
  CHECK((formula_version='customer-health-v1'
      AND ticket_source_status='unavailable' AND ticket_health_score_bps IS NULL
      AND ticket_health_contribution_bps=0 AND effective_weight_bps=9000)
    OR(formula_version='customer-health-platform-v2'
      AND ticket_source_status='available' AND ticket_health_score_bps IS NOT NULL
      AND effective_weight_bps=10000)),
  CHECK(usage_trend_contribution_bps+reliability_contribution_bps
    +cost_anomaly_health_contribution_bps+budget_readiness_contribution_bps
    +team_activity_contribution_bps+ticket_health_contribution_bps=score_bps)
);
CREATE INDEX idx_platform_customer_health_latest_prd140
  ON platform_customer_health_snapshots(
    customer_account_id,customer_health_snapshot_sequence DESC);
CREATE TRIGGER trg_platform_customer_health_update_prd140
BEFORE UPDATE ON platform_customer_health_snapshots
BEGIN SELECT RAISE(ABORT,'customer_health_snapshot_immutable'); END;
CREATE TRIGGER trg_platform_customer_health_delete_prd140
BEFORE DELETE ON platform_customer_health_snapshots
BEGIN SELECT RAISE(ABORT,'customer_health_snapshot_immutable'); END;

CREATE TABLE platform_customer_health_heads(
  customer_account_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  customer_health_snapshot_id TEXT NOT NULL UNIQUE,
  source_sequence INTEGER NOT NULL CHECK(source_sequence>=1),
  source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint)=64
    AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64
    AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  risk_level TEXT NOT NULL CHECK(risk_level IN('healthy','watch','at_risk','critical')),
  freshness_watermark TEXT NOT NULL CHECK(unixepoch(freshness_watermark) IS NOT NULL),
  projected_at TEXT NOT NULL CHECK(unixepoch(projected_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  FOREIGN KEY(source_sequence)
    REFERENCES platform_customer_health_snapshots(customer_health_snapshot_sequence),
  FOREIGN KEY(customer_health_snapshot_id)
    REFERENCES platform_customer_health_snapshots(customer_health_snapshot_id)
);
CREATE TRIGGER trg_platform_customer_health_head_update_prd140
BEFORE UPDATE ON platform_customer_health_heads FOR EACH ROW
WHEN NEW.customer_account_id!=OLD.customer_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.source_sequence<=OLD.source_sequence OR NEW.revision!=OLD.revision+1
  OR unixepoch(NEW.projected_at)<=unixepoch(OLD.projected_at)
BEGIN SELECT RAISE(ABORT,'customer_health_head_transition_invalid'); END;
CREATE TRIGGER trg_platform_customer_health_head_delete_prd140
BEFORE DELETE ON platform_customer_health_heads
BEGIN SELECT RAISE(ABORT,'customer_health_head_immutable'); END;

INSERT INTO platform_customer_health_snapshots(
  customer_health_snapshot_sequence,customer_health_snapshot_id,customer_account_id,
  workspace_id,tenant_id,formula_version,window_start,window_end,comparison_start,
  comparison_end,source_fingerprint,health_projection_sha256,current_request_count,
  previous_request_count,successful_request_count,failed_request_count,
  unresolved_cost_anomaly_count,critical_cost_anomaly_count,active_budget_rule_count,
  eligible_user_count,recently_active_user_count,activation_stage_count,
  open_support_ticket_count,open_p1_ticket_count,open_p2_ticket_count,
  waiting_platform_ticket_count,sla_at_risk_count,sla_breached_count,ticket_source_status,
  usage_trend_score_bps,usage_trend_contribution_bps,reliability_score_bps,
  reliability_contribution_bps,cost_anomaly_health_score_bps,
  cost_anomaly_health_contribution_bps,budget_readiness_score_bps,
  budget_readiness_contribution_bps,team_activity_score_bps,
  team_activity_contribution_bps,ticket_health_score_bps,
  ticket_health_contribution_bps,effective_weight_bps,score_bps,risk_level,
  usage_declining,high_error_rate,cost_anomaly_open,budget_missing,team_inactive,
  operation_request_id,operation_trace_id,evaluated_at,created_at,updated_at)
SELECT score.customer_health_score_sequence,score.customer_health_score_id,
  account.customer_account_id,account.workspace_id,account.tenant_id,
  score.score_version,score.window_start,score.window_end,score.comparison_start,
  score.comparison_end,score.source_fingerprint,score.health_projection_sha256,
  score.current_request_count,score.previous_request_count,score.successful_request_count,
  score.failed_request_count,score.unresolved_cost_anomaly_count,
  score.critical_cost_anomaly_count,score.active_budget_rule_count,
  score.eligible_user_count,score.recently_active_user_count,score.activation_stage_count,
  0,0,0,0,0,0,score.ticket_source_status,
  score.usage_trend_score_bps,score.usage_trend_contribution_bps,
  score.reliability_score_bps,score.reliability_contribution_bps,
  score.cost_anomaly_health_score_bps,score.cost_anomaly_health_contribution_bps,
  score.budget_readiness_score_bps,score.budget_readiness_contribution_bps,
  score.team_activity_score_bps,score.team_activity_contribution_bps,
  score.ticket_health_score_bps,score.ticket_health_contribution_bps,
  score.effective_weight_bps,score.score_bps,score.risk_level,
  score.usage_declining,score.high_error_rate,score.cost_anomaly_open,
  score.budget_missing,score.team_inactive,score.operation_request_id,
  score.operation_trace_id,score.evaluated_at,score.created_at,score.updated_at
FROM customer_health_scores score
JOIN platform_customer_account_projections account ON account.tenant_id=score.tenant_id
ORDER BY score.customer_health_score_sequence;

INSERT INTO platform_customer_health_heads(
  customer_account_id,workspace_id,tenant_id,customer_health_snapshot_id,
  source_sequence,source_fingerprint,projection_sha256,risk_level,
  freshness_watermark,projected_at,revision)
SELECT account.customer_account_id,projection.workspace_id,projection.tenant_id,
  projection.source_score_id,projection.source_sequence,snapshot.source_fingerprint,
  projection.source_projection_sha256,projection.risk_level,
  projection.source_evaluated_at,projection.projected_at,1
FROM workspace_health_projections projection
JOIN platform_customer_account_projections account
  ON account.workspace_id=projection.workspace_id AND account.tenant_id=projection.tenant_id
JOIN platform_customer_health_snapshots snapshot
  ON snapshot.customer_health_snapshot_id=projection.source_score_id
 AND snapshot.customer_account_id=account.customer_account_id;

UPDATE platform_customer_account_projections
SET latest_health_snapshot_id=(SELECT head.customer_health_snapshot_id
      FROM platform_customer_health_heads head
      WHERE head.customer_account_id=platform_customer_account_projections.customer_account_id),
    latest_health_sequence=(SELECT head.source_sequence
      FROM platform_customer_health_heads head
      WHERE head.customer_account_id=platform_customer_account_projections.customer_account_id),
    health_risk=coalesce((SELECT head.risk_level
      FROM platform_customer_health_heads head
      WHERE head.customer_account_id=platform_customer_account_projections.customer_account_id),'unknown'),
    revision=revision+CASE WHEN EXISTS(SELECT 1 FROM platform_customer_health_heads head
      WHERE head.customer_account_id=platform_customer_account_projections.customer_account_id)
      THEN 1 ELSE 0 END,
    updated_at=CASE WHEN EXISTS(SELECT 1 FROM platform_customer_health_heads head
      WHERE head.customer_account_id=platform_customer_account_projections.customer_account_id)
      THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE updated_at END;

CREATE TRIGGER trg_platform_customer_account_owner_update_prd140
BEFORE UPDATE ON platform_customer_account_projections FOR EACH ROW
WHEN NEW.customer_account_id!=OLD.customer_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.projection_version!=OLD.projection_version
  OR NEW.revision!=OLD.revision+1 OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'customer_account_projection_transition_invalid'); END;
CREATE TRIGGER trg_platform_customer_account_delete_prd140
BEFORE DELETE ON platform_customer_account_projections
BEGIN SELECT RAISE(ABORT,'customer_account_projection_immutable'); END;
CREATE TRIGGER trg_platform_customer_contact_update_prd140
BEFORE UPDATE ON platform_customer_contacts FOR EACH ROW
WHEN NEW.customer_contact_id!=OLD.customer_contact_id
  OR NEW.customer_account_id!=OLD.customer_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id
  OR NEW.workspace_membership_id!=OLD.workspace_membership_id
  OR NEW.contact_role!=OLD.contact_role OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'customer_contact_transition_invalid'); END;
CREATE TRIGGER trg_platform_customer_contact_delete_prd140
BEFORE DELETE ON platform_customer_contacts
BEGIN SELECT RAISE(ABORT,'customer_contact_immutable'); END;

CREATE TABLE platform_customer_success_plans(
  customer_success_plan_id TEXT PRIMARY KEY CHECK(length(customer_success_plan_id)=36
    AND substr(customer_success_plan_id,1,4)='csp_'
    AND substr(customer_success_plan_id,5) NOT GLOB '*[^0-9a-f]*'),
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  objective_code TEXT NOT NULL CHECK(objective_code IN(
    'activation','adoption','reliability','renewal','security_readiness')),
  starts_on TEXT NOT NULL CHECK(date(starts_on) IS NOT NULL),
  ends_on TEXT NOT NULL CHECK(date(ends_on) IS NOT NULL AND date(ends_on)>=date(starts_on)),
  target_health_bps INTEGER NOT NULL CHECK(target_health_bps BETWEEN 0 AND 10000),
  target_activation_steps INTEGER NOT NULL CHECK(target_activation_steps BETWEEN 0 AND 8),
  target_adoption_bps INTEGER NOT NULL CHECK(target_adoption_bps BETWEEN 0 AND 10000),
  source_health_sequence INTEGER CHECK(source_health_sequence IS NULL OR source_health_sequence>=1),
  status TEXT NOT NULL CHECK(status IN(
    'draft','active','at_risk','completed','canceled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT NOT NULL,
  updated_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(customer_success_plan_id,customer_account_id,workspace_id,tenant_id),
  CHECK((status='completed' AND completed_at IS NOT NULL)
    OR(status!='completed' AND completed_at IS NULL))
);
CREATE INDEX idx_platform_customer_plans_prd140
  ON platform_customer_success_plans(
    customer_account_id,status,updated_at DESC,customer_success_plan_id DESC);
CREATE TRIGGER trg_platform_customer_plan_update_prd140
BEFORE UPDATE ON platform_customer_success_plans FOR EACH ROW
WHEN NEW.customer_success_plan_id!=OLD.customer_success_plan_id
  OR NEW.customer_account_id!=OLD.customer_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.objective_code!=OLD.objective_code OR NEW.starts_on!=OLD.starts_on
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
  OR NOT ((NEW.status=OLD.status)
    OR(OLD.status='draft' AND NEW.status IN('active','canceled'))
    OR(OLD.status='active' AND NEW.status IN('at_risk','completed','canceled'))
    OR(OLD.status='at_risk' AND NEW.status IN('active','completed','canceled')))
BEGIN SELECT RAISE(ABORT,'customer_success_plan_transition_invalid'); END;
CREATE TRIGGER trg_platform_customer_plan_delete_prd140
BEFORE DELETE ON platform_customer_success_plans
BEGIN SELECT RAISE(ABORT,'customer_success_plan_immutable'); END;

CREATE TABLE platform_renewal_opportunities(
  renewal_opportunity_id TEXT PRIMARY KEY CHECK(length(renewal_opportunity_id)=36
    AND substr(renewal_opportunity_id,1,4)='rno_'
    AND substr(renewal_opportunity_id,5) NOT GLOB '*[^0-9a-f]*'),
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  plan_version_id TEXT NOT NULL,
  renewal_at TEXT NOT NULL CHECK(unixepoch(renewal_at) IS NOT NULL),
  currency TEXT CHECK(currency IS NULL OR currency IN('CNY','USD')),
  amount_minor INTEGER CHECK(amount_minor IS NULL OR amount_minor>=0),
  amount_status TEXT NOT NULL CHECK(amount_status IN('available','not_available')),
  health_risk_code TEXT NOT NULL CHECK(health_risk_code IN(
    'unknown','healthy','watch','at_risk','critical')),
  adoption_risk_code TEXT NOT NULL CHECK(adoption_risk_code IN(
    'unknown','healthy','watch','at_risk')),
  payment_risk_code TEXT NOT NULL CHECK(payment_risk_code IN(
    'unknown','current','past_due','failed')),
  invoice_status TEXT NOT NULL CHECK(invoice_status IN(
    'not_available','open','paid','past_due','void')),
  status TEXT NOT NULL CHECK(status IN(
    'observing','qualified','committed','renewed','lost','canceled')),
  formula_version TEXT NOT NULL CHECK(formula_version='renewal-opportunity-v1'),
  source_digest TEXT NOT NULL CHECK(length(source_digest)=64
    AND source_digest NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,subscription_id)
    REFERENCES workspace_subscriptions(workspace_id,tenant_id,subscription_id),
  FOREIGN KEY(plan_version_id) REFERENCES saas_plan_versions(plan_version_id),
  UNIQUE(customer_account_id,subscription_id),
  CHECK((amount_status='available' AND currency IS NOT NULL AND amount_minor IS NOT NULL)
    OR(amount_status='not_available' AND currency IS NULL AND amount_minor IS NULL))
);
CREATE INDEX idx_platform_renewal_pipeline_prd140
  ON platform_renewal_opportunities(
    renewal_at,renewal_opportunity_id,status,health_risk_code);
CREATE TRIGGER trg_platform_renewal_update_prd140
BEFORE UPDATE ON platform_renewal_opportunities FOR EACH ROW
WHEN NEW.renewal_opportunity_id!=OLD.renewal_opportunity_id
  OR NEW.customer_account_id!=OLD.customer_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.subscription_id!=OLD.subscription_id OR NEW.plan_version_id!=OLD.plan_version_id
  OR NEW.formula_version!=OLD.formula_version OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
  OR NOT ((NEW.status=OLD.status)
    OR(OLD.status='observing' AND NEW.status IN('qualified','committed','renewed','lost','canceled'))
    OR(OLD.status='qualified' AND NEW.status IN('committed','renewed','lost','canceled'))
    OR(OLD.status='committed' AND NEW.status IN('renewed','lost','canceled')))
BEGIN SELECT RAISE(ABORT,'renewal_opportunity_transition_invalid'); END;
CREATE TRIGGER trg_platform_renewal_delete_prd140
BEFORE DELETE ON platform_renewal_opportunities
BEGIN SELECT RAISE(ABORT,'renewal_opportunity_immutable'); END;

INSERT INTO platform_renewal_opportunities(
  renewal_opportunity_id,customer_account_id,workspace_id,tenant_id,subscription_id,
  plan_version_id,renewal_at,currency,amount_minor,amount_status,health_risk_code,
  adoption_risk_code,payment_risk_code,invoice_status,status,formula_version,
  source_digest,revision,created_at,updated_at)
SELECT 'rno_'||substr(metahub_sha256('renewal-v1'||char(31)||subscription.subscription_id),1,32),
  account.customer_account_id,account.workspace_id,account.tenant_id,
  subscription.subscription_id,subscription.plan_version_id,
  subscription.current_period_ends_at,NULL,NULL,'not_available',account.health_risk,
  CASE account.adoption_band WHEN 'high' THEN 'healthy' WHEN 'medium' THEN 'watch'
    WHEN 'low' THEN 'at_risk' ELSE 'unknown' END,
  CASE subscription.status WHEN 'past_due' THEN 'past_due'
    WHEN 'active' THEN 'current' WHEN 'trialing' THEN 'current' ELSE 'unknown' END,
  'not_available','observing','renewal-opportunity-v1',
  metahub_sha256(json_array('renewal-v1',subscription.subscription_id,
    subscription.plan_version_id,subscription.status,subscription.state_revision,
    subscription.current_period_ends_at,account.health_risk,account.adoption_band)),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_subscriptions subscription
JOIN platform_customer_account_projections account
  ON account.workspace_id=subscription.workspace_id AND account.tenant_id=subscription.tenant_id
WHERE subscription.current_period_ends_at IS NOT NULL;

CREATE TABLE platform_customer_tasks(
  customer_task_id TEXT PRIMARY KEY CHECK(length(customer_task_id)=36
    AND substr(customer_task_id,1,4)='ctk_'
    AND substr(customer_task_id,5) NOT GLOB '*[^0-9a-f]*'),
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_success_plan_id TEXT,
  renewal_opportunity_id TEXT,
  task_type TEXT NOT NULL CHECK(task_type IN(
    'activation_review','adoption_follow_up','health_review','renewal_review',
    'security_review','qbr_prepare')),
  priority TEXT NOT NULL CHECK(priority IN('low','normal','high','critical')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80
    AND reason_code=lower(reason_code)
    AND reason_code NOT GLOB '*[^a-z0-9._-]*'),
  assignee_operator_id TEXT NOT NULL,
  due_at TEXT NOT NULL CHECK(unixepoch(due_at) IS NOT NULL),
  required INTEGER NOT NULL CHECK(required IN(0,1)),
  status TEXT NOT NULL CHECK(status IN('open','in_progress','completed','canceled')),
  due_generation INTEGER NOT NULL CHECK(due_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT NOT NULL,
  updated_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  FOREIGN KEY(customer_success_plan_id,customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_success_plans(
      customer_success_plan_id,customer_account_id,workspace_id,tenant_id),
  FOREIGN KEY(renewal_opportunity_id)
    REFERENCES platform_renewal_opportunities(renewal_opportunity_id),
  FOREIGN KEY(assignee_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(customer_task_id,customer_account_id,workspace_id,tenant_id),
  CHECK((status='completed' AND completed_at IS NOT NULL)
    OR(status!='completed' AND completed_at IS NULL))
);
CREATE INDEX idx_platform_customer_tasks_prd140
  ON platform_customer_tasks(
    customer_account_id,status,due_at,customer_task_id);
CREATE INDEX idx_platform_customer_tasks_due_prd140
  ON platform_customer_tasks(status,due_at,due_generation,customer_task_id);
CREATE TRIGGER trg_platform_customer_task_update_prd140
BEFORE UPDATE ON platform_customer_tasks FOR EACH ROW
WHEN NEW.customer_task_id!=OLD.customer_task_id
  OR NEW.customer_account_id!=OLD.customer_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.customer_success_plan_id IS NOT OLD.customer_success_plan_id
  OR NEW.renewal_opportunity_id IS NOT OLD.renewal_opportunity_id
  OR NEW.task_type!=OLD.task_type OR NEW.reason_code!=OLD.reason_code
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.due_generation<OLD.due_generation
  OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
  OR NOT ((NEW.status=OLD.status)
    OR(OLD.status='open' AND NEW.status IN('in_progress','completed','canceled'))
    OR(OLD.status='in_progress' AND NEW.status IN('open','completed','canceled')))
BEGIN SELECT RAISE(ABORT,'customer_task_transition_invalid'); END;
CREATE TRIGGER trg_platform_customer_task_delete_prd140
BEFORE DELETE ON platform_customer_tasks
BEGIN SELECT RAISE(ABORT,'customer_task_immutable'); END;

CREATE TABLE platform_qbr_summaries(
  qbr_summary_id TEXT PRIMARY KEY CHECK(length(qbr_summary_id)=36
    AND substr(qbr_summary_id,1,4)='qbr_'
    AND substr(qbr_summary_id,5) NOT GLOB '*[^0-9a-f]*'),
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  quarter TEXT NOT NULL CHECK(quarter GLOB '????-Q[1-4]'),
  health_sequence INTEGER,
  adoption_bps INTEGER CHECK(adoption_bps IS NULL OR adoption_bps BETWEEN 0 AND 10000),
  support_ticket_count INTEGER NOT NULL CHECK(support_ticket_count>=0),
  renewal_risk_count INTEGER NOT NULL CHECK(renewal_risk_count>=0),
  outcome_code TEXT NOT NULL CHECK(outcome_code IN(
    'healthy_growth','adoption_attention','renewal_attention','support_attention','incomplete')),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'maintain','activate_plan','prepare_renewal','review_support','complete_sources')),
  source_digest TEXT NOT NULL CHECK(length(source_digest)=64
    AND source_digest NOT GLOB '*[^0-9a-f]*'),
  supersedes_qbr_summary_id TEXT,
  generated_at TEXT NOT NULL CHECK(unixepoch(generated_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  FOREIGN KEY(supersedes_qbr_summary_id) REFERENCES platform_qbr_summaries(qbr_summary_id),
  UNIQUE(customer_account_id,quarter,source_digest)
);
CREATE INDEX idx_platform_qbr_customer_prd140
  ON platform_qbr_summaries(customer_account_id,quarter DESC,generated_at DESC);
CREATE TRIGGER trg_platform_qbr_update_prd140 BEFORE UPDATE ON platform_qbr_summaries
BEGIN SELECT RAISE(ABORT,'qbr_summary_immutable'); END;
CREATE TRIGGER trg_platform_qbr_delete_prd140 BEFORE DELETE ON platform_qbr_summaries
BEGIN SELECT RAISE(ABORT,'qbr_summary_immutable'); END;

CREATE TABLE platform_customer_task_batches(
  customer_task_batch_id TEXT PRIMARY KEY CHECK(length(customer_task_batch_id)=36
    AND substr(customer_task_batch_id,1,4)='ctb_'
    AND substr(customer_task_batch_id,5) NOT GLOB '*[^0-9a-f]*'),
  prepared_by_operator_id TEXT NOT NULL,
  approved_by_operator_id TEXT,
  account_manifest_json TEXT NOT NULL CHECK(json_valid(account_manifest_json)
    AND json_type(account_manifest_json)='array'
    AND json_array_length(account_manifest_json) BETWEEN 1 AND 100
    AND length(CAST(account_manifest_json AS BLOB))<=8192),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  task_type TEXT NOT NULL CHECK(task_type IN(
    'activation_review','adoption_follow_up','health_review','renewal_review',
    'security_review','qbr_prepare')),
  priority TEXT NOT NULL CHECK(priority IN('low','normal','high','critical')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80),
  assignee_operator_id TEXT NOT NULL,
  due_at TEXT NOT NULL CHECK(unixepoch(due_at) IS NOT NULL),
  status TEXT NOT NULL CHECK(status IN('awaiting_approval','applied','rejected')),
  requested_permission_revision INTEGER NOT NULL CHECK(requested_permission_revision>=1),
  approval_generation INTEGER NOT NULL CHECK(approval_generation>=1),
  result_count INTEGER NOT NULL CHECK(result_count BETWEEN 0 AND 100),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(prepared_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(assignee_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='awaiting_approval' AND approved_by_operator_id IS NULL AND result_count=0)
    OR(status!='awaiting_approval' AND approved_by_operator_id IS NOT NULL)),
  CHECK(approved_by_operator_id IS NULL
    OR approved_by_operator_id!=prepared_by_operator_id)
);
CREATE TRIGGER trg_platform_customer_batch_update_prd140
BEFORE UPDATE ON platform_customer_task_batches FOR EACH ROW
WHEN NEW.customer_task_batch_id!=OLD.customer_task_batch_id
  OR NEW.prepared_by_operator_id!=OLD.prepared_by_operator_id
  OR NEW.account_manifest_json!=OLD.account_manifest_json
  OR NEW.manifest_sha256!=OLD.manifest_sha256 OR NEW.task_type!=OLD.task_type
  OR NEW.priority!=OLD.priority OR NEW.reason_code!=OLD.reason_code
  OR NEW.assignee_operator_id!=OLD.assignee_operator_id OR NEW.due_at!=OLD.due_at
  OR NEW.requested_permission_revision!=OLD.requested_permission_revision
  OR NEW.created_at!=OLD.created_at OR OLD.status!='awaiting_approval'
  OR NEW.status NOT IN('applied','rejected') OR NEW.revision!=OLD.revision+1
  OR NEW.approval_generation!=OLD.approval_generation+1
  OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'customer_task_batch_transition_invalid'); END;
CREATE TRIGGER trg_platform_customer_batch_delete_prd140
BEFORE DELETE ON platform_customer_task_batches
BEGIN SELECT RAISE(ABORT,'customer_task_batch_immutable'); END;

CREATE TABLE platform_customer_task_batch_approvals(
  customer_task_batch_approval_id TEXT PRIMARY KEY CHECK(
    length(customer_task_batch_approval_id)=36
    AND substr(customer_task_batch_approval_id,1,4)='cta_'
    AND substr(customer_task_batch_approval_id,5) NOT GLOB '*[^0-9a-f]*'),
  customer_task_batch_id TEXT NOT NULL UNIQUE,
  approver_operator_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN('approved','rejected')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80),
  permission_revision INTEGER NOT NULL CHECK(permission_revision>=1),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(customer_task_batch_id)
    REFERENCES platform_customer_task_batches(customer_task_batch_id),
  FOREIGN KEY(approver_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TRIGGER trg_platform_customer_batch_approval_update_prd140
BEFORE UPDATE ON platform_customer_task_batch_approvals
BEGIN SELECT RAISE(ABORT,'customer_task_batch_approval_immutable'); END;
CREATE TRIGGER trg_platform_customer_batch_approval_delete_prd140
BEFORE DELETE ON platform_customer_task_batch_approvals
BEGIN SELECT RAISE(ABORT,'customer_task_batch_approval_immutable'); END;

CREATE TABLE platform_customer_success_commands(
  customer_success_command_id TEXT PRIMARY KEY CHECK(length(customer_success_command_id)=36
    AND substr(customer_success_command_id,1,4)='csc_'
    AND substr(customer_success_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  operator_id TEXT NOT NULL,
  customer_account_id TEXT,
  workspace_id TEXT,
  tenant_id TEXT,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'plan.create','plan.activate','plan.revise_targets','plan.complete','plan.cancel',
    'task.create_one','task.transition_one','task.prepare_batch','task.approve_batch')),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
  result_object_kind TEXT NOT NULL CHECK(result_object_kind IN(
    'success_plan','customer_task','task_batch')),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  result_json TEXT NOT NULL CHECK(json_valid(result_json)
    AND json_type(result_json)='object' AND length(CAST(result_json AS BLOB))<=32768),
  status TEXT NOT NULL CHECK(status='completed'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  UNIQUE(operator_id,action_code,idempotency_key_hmac),
  CHECK((customer_account_id IS NULL AND workspace_id IS NULL AND tenant_id IS NULL)
    OR(customer_account_id IS NOT NULL AND workspace_id IS NOT NULL AND tenant_id IS NOT NULL))
);
CREATE TRIGGER trg_platform_customer_command_update_prd140
BEFORE UPDATE ON platform_customer_success_commands
BEGIN SELECT RAISE(ABORT,'customer_success_command_immutable'); END;
CREATE TRIGGER trg_platform_customer_command_delete_prd140
BEFORE DELETE ON platform_customer_success_commands
BEGIN SELECT RAISE(ABORT,'customer_success_command_immutable'); END;

CREATE TABLE platform_customer_success_legacy_mappings(
  customer_success_legacy_mapping_id TEXT PRIMARY KEY CHECK(
    length(customer_success_legacy_mapping_id)=36
    AND substr(customer_success_legacy_mapping_id,1,4)='csm_'
    AND substr(customer_success_legacy_mapping_id,5) NOT GLOB '*[^0-9a-f]*'),
  source_table TEXT NOT NULL CHECK(source_table IN(
    'customer_health_scores','customer_health_score_outbox',
    'customer_health_scheduler_tenants','workspace_health_projections')),
  source_id TEXT NOT NULL,
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK(target_kind IN(
    'health_snapshot','health_event_outbox','health_scheduler','health_head')),
  target_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64
    AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64
    AND target_sha256 NOT GLOB '*[^0-9a-f]*'),
  migration_generation INTEGER NOT NULL CHECK(migration_generation>=1),
  status TEXT NOT NULL CHECK(status IN('migrated','sent_tombstone')),
  migrated_at TEXT NOT NULL CHECK(unixepoch(migrated_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  UNIQUE(source_table,source_id),
  UNIQUE(target_kind,target_id)
);
CREATE INDEX idx_platform_customer_legacy_owner_prd140
  ON platform_customer_success_legacy_mappings(
    customer_account_id,source_table,source_id);
CREATE TRIGGER trg_platform_customer_mapping_update_prd140
BEFORE UPDATE ON platform_customer_success_legacy_mappings
BEGIN SELECT RAISE(ABORT,'customer_success_mapping_immutable'); END;
CREATE TRIGGER trg_platform_customer_mapping_delete_prd140
BEFORE DELETE ON platform_customer_success_legacy_mappings
BEGIN SELECT RAISE(ABORT,'customer_success_mapping_immutable'); END;

CREATE TABLE platform_customer_success_migration_cursors(
  source_table TEXT PRIMARY KEY CHECK(source_table IN(
    'customer_health_scores','customer_health_score_outbox',
    'customer_health_scheduler_tenants','workspace_health_projections')),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  target_row_count INTEGER NOT NULL CHECK(target_row_count>=0),
  max_source_sequence INTEGER NOT NULL CHECK(max_source_sequence>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64
    AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64
    AND target_sha256 NOT GLOB '*[^0-9a-f]*'),
  pending_count INTEGER NOT NULL CHECK(pending_count>=0),
  leased_count INTEGER NOT NULL CHECK(leased_count>=0),
  sent_count INTEGER NOT NULL CHECK(sent_count>=0),
  failed_count INTEGER NOT NULL CHECK(failed_count>=0),
  scheduler_watermark TEXT,
  webhook_delivery_count INTEGER NOT NULL CHECK(webhook_delivery_count>=0),
  webhook_dead_letter_count INTEGER NOT NULL CHECK(webhook_dead_letter_count>=0),
  migration_generation INTEGER NOT NULL CHECK(migration_generation>=1),
  status TEXT NOT NULL CHECK(status='cutover'),
  reconciled_at TEXT NOT NULL CHECK(unixepoch(reconciled_at) IS NOT NULL),
  CHECK(source_row_count=target_row_count),
  CHECK(pending_count+leased_count+sent_count+failed_count<=source_row_count)
);
CREATE TRIGGER trg_platform_customer_cursor_update_prd140
BEFORE UPDATE ON platform_customer_success_migration_cursors
BEGIN SELECT RAISE(ABORT,'customer_success_cursor_immutable'); END;
CREATE TRIGGER trg_platform_customer_cursor_delete_prd140
BEFORE DELETE ON platform_customer_success_migration_cursors
BEGIN SELECT RAISE(ABORT,'customer_success_cursor_immutable'); END;

CREATE TABLE platform_customer_success_events(
  customer_success_event_id TEXT PRIMARY KEY CHECK(
    (length(customer_success_event_id)=36
      AND substr(customer_success_event_id,1,4)='cse_'
      AND substr(customer_success_event_id,5) NOT GLOB '*[^0-9a-f]*')
    OR(length(customer_success_event_id)=40
      AND substr(customer_success_event_id,1,8)='evt_chs_'
      AND substr(customer_success_event_id,9) NOT GLOB '*[^0-9a-f]*')),
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'customer.health_changed','success_plan.completed',
    'customer.task_due','renewal.risk_changed')),
  object_kind TEXT NOT NULL CHECK(object_kind IN(
    'health_snapshot','success_plan','customer_task','renewal_opportunity')),
  object_id TEXT NOT NULL,
  object_revision INTEGER NOT NULL CHECK(object_revision>=1),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80
    AND reason_code=lower(reason_code)
    AND reason_code NOT GLOB '*[^a-z0-9._-]*'),
  safe_count INTEGER NOT NULL CHECK(safe_count>=0),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=4096),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  schema_version TEXT NOT NULL CHECK(schema_version='customer-success-event-v1'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  UNIQUE(event_type,object_id,object_revision,reason_code)
);
CREATE INDEX idx_platform_customer_events_owner_prd140
  ON platform_customer_success_events(
    customer_account_id,occurred_at,customer_success_event_id);
CREATE TRIGGER trg_platform_customer_event_update_prd140
BEFORE UPDATE ON platform_customer_success_events
BEGIN SELECT RAISE(ABORT,'customer_success_event_immutable'); END;
CREATE TRIGGER trg_platform_customer_event_delete_prd140
BEFORE DELETE ON platform_customer_success_events
BEGIN SELECT RAISE(ABORT,'customer_success_event_immutable'); END;

CREATE TABLE platform_customer_success_event_outbox(
  customer_success_event_outbox_id TEXT PRIMARY KEY CHECK(
    (length(customer_success_event_outbox_id)=36
      AND substr(customer_success_event_outbox_id,1,4)='cso_'
      AND substr(customer_success_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*')
    OR(length(customer_success_event_outbox_id)=37
      AND substr(customer_success_event_outbox_id,1,5)='chso_'
      AND substr(customer_success_event_outbox_id,6) NOT GLOB '*[^0-9a-f]*')),
  customer_success_event_id TEXT NOT NULL UNIQUE,
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_outbox_id TEXT,
  status TEXT NOT NULL CHECK(status IN('pending','leased','sent','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR(
    length(lease_token_sha256)=64
    AND lease_token_sha256 NOT GLOB '*[^0-9a-f]*')),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL
    OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL
    OR unixepoch(next_attempt_at) IS NOT NULL),
  subscription_snapshot_json TEXT CHECK(subscription_snapshot_json IS NULL OR(
    json_valid(subscription_snapshot_json)
    AND json_type(subscription_snapshot_json)='array'
    AND length(CAST(subscription_snapshot_json AS BLOB))<=65536)),
  snapshot_frozen_at TEXT CHECK(snapshot_frozen_at IS NULL
    OR unixepoch(snapshot_frozen_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL
    OR length(last_error_code) BETWEEN 3 AND 80),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  recovery_watermark INTEGER NOT NULL CHECK(recovery_watermark>=0),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(customer_success_event_id)
    REFERENCES platform_customer_success_events(customer_success_event_id),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  CHECK((lease_owner_id IS NULL AND lease_token_sha256 IS NULL
      AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND lease_token_sha256 IS NOT NULL
      AND lease_expires_at IS NOT NULL)),
  CHECK((subscription_snapshot_json IS NULL AND snapshot_frozen_at IS NULL)
    OR(subscription_snapshot_json IS NOT NULL AND snapshot_frozen_at IS NOT NULL)),
  CHECK((status='pending' AND lease_owner_id IS NULL AND delivered_at IS NULL)
    OR(status='leased' AND lease_owner_id IS NOT NULL AND delivered_at IS NULL)
    OR(status='sent' AND lease_owner_id IS NULL AND delivered_at IS NOT NULL)
    OR(status='failed' AND lease_owner_id IS NULL AND delivered_at IS NULL))
);
CREATE INDEX idx_platform_customer_outbox_claim_prd140
  ON platform_customer_success_event_outbox(
    status,next_attempt_at,created_at,customer_success_event_outbox_id);
CREATE TRIGGER trg_platform_customer_outbox_delete_prd140
BEFORE DELETE ON platform_customer_success_event_outbox
BEGIN SELECT RAISE(ABORT,'customer_success_outbox_immutable'); END;

CREATE TABLE platform_customer_success_scheduler_state(
  customer_success_scheduler_id TEXT PRIMARY KEY CHECK(
    length(customer_success_scheduler_id)=36
    AND substr(customer_success_scheduler_id,1,4)='css_'
    AND substr(customer_success_scheduler_id,5) NOT GLOB '*[^0-9a-f]*'),
  customer_account_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  job_code TEXT NOT NULL CHECK(job_code IN(
    'account_projection','health_evaluation','renewal_projection',
    'task_due','qbr_snapshot','event_outbox')),
  status TEXT NOT NULL CHECK(status IN('pending','leased')),
  next_run_at TEXT NOT NULL CHECK(unixepoch(next_run_at) IS NOT NULL),
  scan_sequence_watermark INTEGER NOT NULL CHECK(scan_sequence_watermark>=0),
  scan_time_watermark TEXT NOT NULL CHECK(unixepoch(scan_time_watermark) IS NOT NULL),
  consecutive_failure_count INTEGER NOT NULL CHECK(consecutive_failure_count>=0),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR(
    length(lease_token_sha256)=64
    AND lease_token_sha256 NOT GLOB '*[^0-9a-f]*')),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL
    OR unixepoch(lease_expires_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL
    OR length(last_error_code) BETWEEN 3 AND 80),
  bounded_batch_size INTEGER NOT NULL CHECK(bounded_batch_size BETWEEN 1 AND 100),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(customer_account_id,workspace_id,tenant_id)
    REFERENCES platform_customer_account_projections(customer_account_id,workspace_id,tenant_id),
  UNIQUE(customer_account_id,job_code),
  CHECK((status='pending' AND lease_owner_id IS NULL
      AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)
    OR(status='leased' AND lease_owner_id IS NOT NULL
      AND lease_token_sha256 IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE INDEX idx_platform_customer_scheduler_claim_prd140
  ON platform_customer_success_scheduler_state(
    job_code,status,next_run_at,customer_success_scheduler_id);
CREATE TRIGGER trg_platform_customer_scheduler_update_prd140
BEFORE UPDATE ON platform_customer_success_scheduler_state FOR EACH ROW
WHEN NEW.customer_success_scheduler_id!=OLD.customer_success_scheduler_id
  OR NEW.customer_account_id!=OLD.customer_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.job_code!=OLD.job_code OR NEW.created_at!=OLD.created_at
  OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.scan_sequence_watermark<OLD.scan_sequence_watermark
  OR unixepoch(NEW.scan_time_watermark)<unixepoch(OLD.scan_time_watermark)
  OR NEW.revision!=OLD.revision+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'customer_success_scheduler_fenced'); END;
CREATE TRIGGER trg_platform_customer_scheduler_delete_prd140
BEFORE DELETE ON platform_customer_success_scheduler_state
BEGIN SELECT RAISE(ABORT,'customer_success_scheduler_required'); END;

-- Preserve every v1 event/outbox identifier and delivery terminal state. The up guard has
-- already rejected live legacy leases, so any copied lease is expired and recoverable.
INSERT INTO platform_customer_success_events(
  customer_success_event_id,customer_account_id,workspace_id,tenant_id,event_type,
  object_kind,object_id,object_revision,reason_code,safe_count,payload_json,
  payload_sha256,schema_version,occurred_at)
SELECT legacy.webhook_event_id,account.customer_account_id,account.workspace_id,
  account.tenant_id,'customer.health_changed','health_snapshot',
  legacy.customer_health_score_id,snapshot.customer_health_snapshot_sequence,
  'legacy_v1_migration',0,
  json_object('event_id',legacy.webhook_event_id,'schema_version','customer-success-event-v1',
    'occurred_at',legacy.created_at,'workspace_id',account.workspace_id,
    'tenant_id',account.tenant_id,'customer_account_id',account.customer_account_id,
    'object_id',legacy.customer_health_score_id,'revision',
    snapshot.customer_health_snapshot_sequence,'reason_code','legacy_v1_migration',
    'safe_count',0,'payload_sha256',snapshot.health_projection_sha256),
  metahub_sha256(json_array('customer.health_changed',legacy.webhook_event_id,
    account.workspace_id,account.tenant_id,account.customer_account_id,
    legacy.customer_health_score_id,snapshot.customer_health_snapshot_sequence,
    snapshot.health_projection_sha256)),
  'customer-success-event-v1',legacy.created_at
FROM customer_health_score_outbox legacy
JOIN platform_customer_account_projections account
  ON account.tenant_id=legacy.tenant_id
JOIN platform_customer_health_snapshots snapshot
  ON snapshot.customer_account_id=account.customer_account_id
 AND snapshot.customer_health_snapshot_id=legacy.customer_health_score_id;

INSERT INTO platform_customer_success_event_outbox(
  customer_success_event_outbox_id,customer_success_event_id,customer_account_id,
  workspace_id,tenant_id,source_outbox_id,status,attempt_count,fencing_generation,
  lease_owner_id,lease_token_sha256,lease_expires_at,next_attempt_at,
  subscription_snapshot_json,snapshot_frozen_at,last_error_code,delivered_at,
  recovery_watermark,created_at,updated_at)
SELECT legacy.customer_health_score_outbox_id,legacy.webhook_event_id,
  account.customer_account_id,account.workspace_id,account.tenant_id,
  legacy.customer_health_score_outbox_id,legacy.status,legacy.attempt_count,
  legacy.generation,legacy.lease_owner_id,legacy.lease_token,legacy.lease_expires_at,
  legacy.next_attempt_at,legacy.subscription_snapshot_json,legacy.snapshot_frozen_at,
  legacy.last_error_code,legacy.delivered_at,legacy.generation,
  legacy.created_at,legacy.updated_at
FROM customer_health_score_outbox legacy
JOIN platform_customer_account_projections account ON account.tenant_id=legacy.tenant_id;

INSERT INTO platform_customer_success_scheduler_state(
  customer_success_scheduler_id,customer_account_id,workspace_id,tenant_id,job_code,
  status,next_run_at,scan_sequence_watermark,scan_time_watermark,
  consecutive_failure_count,fencing_generation,lease_owner_id,lease_token_sha256,
  lease_expires_at,last_error_code,bounded_batch_size,revision,created_at,updated_at)
SELECT 'css_'||substr(metahub_sha256('customer-success-scheduler-v1'||char(31)
    ||account.customer_account_id||char(31)||job.job_code),1,32),
  account.customer_account_id,account.workspace_id,account.tenant_id,job.job_code,'pending',
  CASE WHEN job.job_code='health_evaluation'
      THEN coalesce(legacy.next_evaluation_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    ELSE strftime('%Y-%m-%dT%H:%M:%fZ','now') END,
  CASE WHEN job.job_code IN('health_evaluation','event_outbox')
      THEN coalesce((SELECT max(snapshot.customer_health_snapshot_sequence)
        FROM platform_customer_health_snapshots snapshot
        WHERE snapshot.customer_account_id=account.customer_account_id),0)
    ELSE 0 END,
  CASE WHEN job.job_code='health_evaluation'
      THEN coalesce(legacy.last_completed_window_end,'1970-01-01T00:00:00Z')
    ELSE '1970-01-01T00:00:00Z' END,
  CASE WHEN job.job_code='health_evaluation'
      THEN coalesce(legacy.consecutive_failure_count,0) ELSE 0 END,
  CASE WHEN job.job_code='health_evaluation'
      THEN coalesce(legacy.generation,0)+1 ELSE 0 END,
  NULL,NULL,NULL,
  CASE WHEN job.job_code='health_evaluation' THEN legacy.last_error_code ELSE NULL END,
  50,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM platform_customer_account_projections account
CROSS JOIN(
  SELECT 'account_projection' job_code UNION ALL SELECT 'health_evaluation'
  UNION ALL SELECT 'renewal_projection' UNION ALL SELECT 'task_due'
  UNION ALL SELECT 'qbr_snapshot' UNION ALL SELECT 'event_outbox') job
LEFT JOIN customer_health_scheduler_tenants legacy ON legacy.tenant_id=account.tenant_id;

INSERT INTO platform_customer_success_legacy_mappings
SELECT 'csm_'||substr(metahub_sha256('customer_health_scores'||char(31)
    ||score.customer_health_score_id),1,32),
  'customer_health_scores',score.customer_health_score_id,account.customer_account_id,
  account.workspace_id,account.tenant_id,'health_snapshot',score.customer_health_score_id,
  metahub_sha256(json_array(score.customer_health_score_sequence,
    score.customer_health_score_id,score.source_fingerprint,
    score.health_projection_sha256,score.score_bps,score.risk_level,score.updated_at)),
  metahub_sha256(json_array(snapshot.customer_health_snapshot_sequence,
    snapshot.customer_health_snapshot_id,snapshot.source_fingerprint,
    snapshot.health_projection_sha256,snapshot.score_bps,snapshot.risk_level,
    snapshot.updated_at)),1,'migrated',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM customer_health_scores score
JOIN platform_customer_account_projections account ON account.tenant_id=score.tenant_id
JOIN platform_customer_health_snapshots snapshot
  ON snapshot.customer_account_id=account.customer_account_id
 AND snapshot.customer_health_snapshot_id=score.customer_health_score_id;

INSERT INTO platform_customer_success_legacy_mappings
SELECT 'csm_'||substr(metahub_sha256('customer_health_score_outbox'||char(31)
    ||legacy.customer_health_score_outbox_id),1,32),
  'customer_health_score_outbox',legacy.customer_health_score_outbox_id,
  account.customer_account_id,account.workspace_id,account.tenant_id,
  'health_event_outbox',target.customer_success_event_outbox_id,
  metahub_sha256(json_array(legacy.customer_health_score_outbox_id,
    legacy.webhook_event_id,legacy.status,legacy.attempt_count,legacy.generation,
    legacy.updated_at)),
  metahub_sha256(json_array(target.customer_success_event_outbox_id,
    target.customer_success_event_id,target.status,target.attempt_count,
    target.fencing_generation,target.updated_at)),1,
  CASE WHEN legacy.status='sent' THEN 'sent_tombstone' ELSE 'migrated' END,
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM customer_health_score_outbox legacy
JOIN platform_customer_account_projections account ON account.tenant_id=legacy.tenant_id
JOIN platform_customer_success_event_outbox target
  ON target.source_outbox_id=legacy.customer_health_score_outbox_id;

INSERT INTO platform_customer_success_legacy_mappings
SELECT 'csm_'||substr(metahub_sha256('customer_health_scheduler_tenants'||char(31)
    ||legacy.tenant_id),1,32),
  'customer_health_scheduler_tenants',legacy.tenant_id,account.customer_account_id,
  account.workspace_id,account.tenant_id,'health_scheduler',target.customer_success_scheduler_id,
  metahub_sha256(json_array(legacy.tenant_id,legacy.status,legacy.next_evaluation_at,
    legacy.consecutive_failure_count,legacy.generation,
    legacy.last_completed_window_end,legacy.updated_at)),
  metahub_sha256(json_array(target.customer_success_scheduler_id,target.status,
    target.next_run_at,target.consecutive_failure_count,target.fencing_generation,
    target.scan_time_watermark,target.updated_at)),1,'migrated',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM customer_health_scheduler_tenants legacy
JOIN platform_customer_account_projections account ON account.tenant_id=legacy.tenant_id
JOIN platform_customer_success_scheduler_state target
  ON target.customer_account_id=account.customer_account_id
 AND target.job_code='health_evaluation';

INSERT INTO platform_customer_success_legacy_mappings
SELECT 'csm_'||substr(metahub_sha256('workspace_health_projections'||char(31)
    ||legacy.workspace_health_projection_id),1,32),
  'workspace_health_projections',legacy.workspace_health_projection_id,
  account.customer_account_id,account.workspace_id,account.tenant_id,
  'health_head',head.customer_health_snapshot_id,
  metahub_sha256(json_array(legacy.workspace_health_projection_id,
    legacy.source_score_id,legacy.source_sequence,legacy.risk_level,
    legacy.source_projection_sha256,legacy.source_evaluated_at,legacy.projected_at)),
  metahub_sha256(json_array(head.customer_health_snapshot_id,head.source_sequence,
    head.risk_level,head.projection_sha256,head.freshness_watermark,head.projected_at)),
  1,'migrated',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_health_projections legacy
JOIN platform_customer_account_projections account
  ON account.workspace_id=legacy.workspace_id AND account.tenant_id=legacy.tenant_id
JOIN platform_customer_health_heads head
  ON head.customer_account_id=account.customer_account_id;

INSERT INTO platform_customer_success_migration_cursors
SELECT 'customer_health_scores',count(*),count(*),
  coalesce(max(source.customer_health_score_sequence),0),
  metahub_sha256(coalesce(group_concat(source.customer_health_score_sequence||':'
    ||source.customer_health_score_id||':'||source.health_projection_sha256,'|'),'')),
  metahub_sha256(coalesce(group_concat(target.customer_health_snapshot_sequence||':'
    ||target.customer_health_snapshot_id||':'||target.health_projection_sha256,'|'),'')),
  0,0,0,0,NULL,0,0,1,'cutover',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM customer_health_scores source
LEFT JOIN platform_customer_health_snapshots target
  ON target.customer_health_snapshot_sequence=source.customer_health_score_sequence
 AND target.customer_health_snapshot_id=source.customer_health_score_id;
INSERT INTO platform_customer_success_migration_cursors
SELECT 'customer_health_score_outbox',count(*),count(*),
  coalesce(max(source.generation),0),
  metahub_sha256(coalesce(group_concat(source.customer_health_score_outbox_id||':'
    ||source.status||':'||source.attempt_count||':'||source.generation,'|'),'')),
  metahub_sha256(coalesce(group_concat(target.customer_success_event_outbox_id||':'
    ||target.status||':'||target.attempt_count||':'||target.fencing_generation,'|'),'')),
  count(*) FILTER(WHERE source.status='pending'),
  count(*) FILTER(WHERE source.status='leased'),
  count(*) FILTER(WHERE source.status='sent'),
  count(*) FILTER(WHERE source.status='failed'),NULL,
  (SELECT count(*) FROM webhook_deliveries delivery
    WHERE delivery.event_type='customer.health_changed'),
  (SELECT count(*) FROM webhook_dead_letter_events dead
    WHERE dead.event_type='customer.health_changed'),
  1,'cutover',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM customer_health_score_outbox source
LEFT JOIN platform_customer_success_event_outbox target
  ON target.source_outbox_id=source.customer_health_score_outbox_id;
INSERT INTO platform_customer_success_migration_cursors
SELECT 'customer_health_scheduler_tenants',count(*),count(*),
  coalesce(max(source.generation),0),
  metahub_sha256(coalesce(group_concat(source.tenant_id||':'||source.status||':'
    ||source.generation||':'||coalesce(source.last_completed_window_end,''),'|'),'')),
  metahub_sha256(coalesce(group_concat(target.tenant_id||':'||target.status||':'
    ||target.fencing_generation||':'||target.scan_time_watermark,'|'),'')),
  count(*) FILTER(WHERE source.status='pending'),
  count(*) FILTER(WHERE source.status='leased'),0,0,
  max(source.last_completed_window_end),0,0,1,'cutover',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM customer_health_scheduler_tenants source
LEFT JOIN platform_customer_success_scheduler_state target
  ON target.tenant_id=source.tenant_id AND target.job_code='health_evaluation';
INSERT INTO platform_customer_success_migration_cursors
SELECT 'workspace_health_projections',count(*),count(*),
  coalesce(max(source.source_sequence),0),
  metahub_sha256(coalesce(group_concat(source.workspace_health_projection_id||':'
    ||source.source_sequence||':'||source.source_projection_sha256,'|'),'')),
  metahub_sha256(coalesce(group_concat(target.customer_account_id||':'
    ||target.source_sequence||':'||target.projection_sha256,'|'),'')),
  0,0,0,0,max(source.source_evaluated_at),0,0,1,'cutover',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM workspace_health_projections source
LEFT JOIN platform_customer_health_heads target
  ON target.workspace_id=source.workspace_id AND target.tenant_id=source.tenant_id;

-- Cut over the two legacy automatic writers before installing hard write guards. These
-- triggers are intentionally not restored by down: runtime rollback never reactivates v1.
DROP TRIGGER IF EXISTS trg_tenants_customer_health_scheduler_insert_prd086;
DROP TRIGGER IF EXISTS trg_customer_health_workspace_projection_prd096;

CREATE TRIGGER trg_legacy_health_scores_write_prd140 BEFORE INSERT ON customer_health_scores
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_scores_update_prd140 BEFORE UPDATE ON customer_health_scores
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_scores_delete_prd140 BEFORE DELETE ON customer_health_scores
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_outbox_write_prd140 BEFORE INSERT ON customer_health_score_outbox
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_outbox_update_prd140 BEFORE UPDATE ON customer_health_score_outbox
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_outbox_delete_prd140 BEFORE DELETE ON customer_health_score_outbox
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_scheduler_write_prd140 BEFORE INSERT ON customer_health_scheduler_tenants
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_scheduler_update_prd140 BEFORE UPDATE ON customer_health_scheduler_tenants
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_scheduler_delete_prd140 BEFORE DELETE ON customer_health_scheduler_tenants
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_workspace_health_write_prd140 BEFORE INSERT ON workspace_health_projections
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_workspace_health_update_prd140 BEFORE UPDATE ON workspace_health_projections
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_workspace_health_delete_prd140 BEFORE DELETE ON workspace_health_projections
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd140(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-140'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd140_manifest_sources(
  source_table TEXT PRIMARY KEY,disposition TEXT NOT NULL,retention_class TEXT NOT NULL);
INSERT INTO prd140_manifest_sources VALUES
  ('platform_customer_account_projections','archive_then_delete','operational'),
  ('platform_customer_contacts','archive_then_delete','security'),
  ('platform_customer_health_snapshots','archive_then_delete','operational'),
  ('platform_customer_health_heads','archive_then_delete','operational'),
  ('platform_customer_success_plans','archive_then_delete','operational'),
  ('platform_renewal_opportunities','archive_then_delete','billing'),
  ('platform_customer_tasks','archive_then_delete','operational'),
  ('platform_qbr_summaries','archive_then_delete','operational'),
  ('platform_customer_success_commands','archive_then_delete','audit'),
  ('platform_customer_success_legacy_mappings','archive_then_delete','audit'),
  ('platform_customer_success_events','archive_then_delete','audit'),
  ('platform_customer_success_event_outbox','archive_then_delete','audit'),
  ('platform_customer_success_scheduler_state','archive_then_delete','operational');
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
FROM sqlite_schema table_record JOIN prd140_manifest_sources source
  ON source.source_table=table_record.name WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd140
SELECT source_table,'tenant_id','PRD-140',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd140_manifest_sources ORDER BY source_table;
DROP TABLE prd140_manifest_sources;
DELETE FROM tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd140_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd140
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd140_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd140
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd140_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd140
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd140_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd140_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_customer_success_permission_extensions)=8
      AND (SELECT COUNT(*) FROM platform_customer_success_role_permissions)=16
      AND (SELECT COUNT(*) FROM platform_customer_success_migration_cursors)=4
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd140)=13
      AND (SELECT COUNT(*) FROM platform_customer_account_projections)=
        (SELECT COUNT(*) FROM workspace_tenant_bindings WHERE status='active')
      AND (SELECT COUNT(*) FROM platform_customer_health_snapshots)=
        (SELECT COUNT(*) FROM customer_health_scores)
      AND (SELECT COUNT(*) FROM platform_customer_success_event_outbox)=
        (SELECT COUNT(*) FROM customer_health_score_outbox)
      AND (SELECT COUNT(*) FROM platform_customer_success_scheduler_state)=
        6*(SELECT COUNT(*) FROM platform_customer_account_projections)
      AND (SELECT COUNT(*) FROM platform_customer_success_legacy_mappings)=
        ((SELECT COUNT(*) FROM customer_health_scores)
        +(SELECT COUNT(*) FROM customer_health_score_outbox)
        +(SELECT COUNT(*) FROM customer_health_scheduler_tenants)
        +(SELECT COUNT(*) FROM workspace_health_projections))
    THEN 0 ELSE 1 END
  + CASE WHEN EXISTS(SELECT 1 FROM permissions WHERE code='customer_health.read')
      OR EXISTS(SELECT 1 FROM rbac_permission_templates
        WHERE permission_code='customer_health.read')
      OR EXISTS(SELECT 1 FROM rbac_role_permission_templates
        WHERE permission_code='customer_health.read') THEN 1 ELSE 0 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd140_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
