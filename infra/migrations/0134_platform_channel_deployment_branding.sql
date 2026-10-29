-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd142_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd142_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0133' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'branding_configs','branding_events','branding_event_outbox','platform_operators',
      'platform_roles','channel_partners','edge_deployments','workspace_tenant_bindings'))=8
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM branding_event_outbox
      WHERE status='leased' AND unixepoch(lease_expires_at)>unixepoch('now'))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd142')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd142_up_guard;

-- PRD-088 tenant branding is no longer a Workspace capability.
CREATE TEMP TABLE prd142_roles_to_contract(role_id TEXT PRIMARY KEY);
INSERT INTO prd142_roles_to_contract
SELECT DISTINCT assignment.role_id
FROM role_permissions assignment
JOIN permissions permission ON permission.permission_id=assignment.permission_id
WHERE permission.code IN('branding.read','branding.manage');
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
  SELECT permission_id FROM permissions WHERE code IN('branding.read','branding.manage'));
DELETE FROM rbac_role_permission_templates
WHERE permission_code IN('branding.read','branding.manage');
DELETE FROM permissions WHERE code IN('branding.read','branding.manage');
DELETE FROM rbac_permission_templates
WHERE permission_code IN('branding.read','branding.manage');
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=metahub_sha256(coalesce((
      SELECT json_group_array(code) FROM(
        SELECT permission.code AS code
        FROM role_permissions assignment
        JOIN permissions permission ON permission.permission_id=assignment.permission_id
        WHERE assignment.tenant_id=roles.tenant_id
          AND assignment.role_id=roles.role_id
        ORDER BY permission.code)), '[]'))
WHERE role_id IN(SELECT role_id FROM prd142_roles_to_contract);
DROP TABLE prd142_roles_to_contract;

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
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN 'a9ceca1ab103efaaa8b6f2ba23713e0fa2c46e18be8709bef25ef607ed329c1c' WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1' WHEN 'viewer' THEN '612465d72c402e7d35c56779b4644d0603571f56b4714053d2b30bc3fb2a769e' WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c' WHEN 'security_admin' THEN '8365be2449480db94f2075bbb4925ac40b9cdd8fad4cb9629e2b5c291daf8a75' WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-142 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN 'a9ceca1ab103efaaa8b6f2ba23713e0fa2c46e18be8709bef25ef607ed329c1c' WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1' WHEN 'viewer' THEN '612465d72c402e7d35c56779b4644d0603571f56b4714053d2b30bc3fb2a769e' WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c' WHEN 'security_admin' THEN '8365be2449480db94f2075bbb4925ac40b9cdd8fad4cb9629e2b5c291daf8a75' WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,27,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

CREATE TABLE platform_brand_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.brand.read','platform.brand.manage','platform.brand.release',
    'platform.channel_brand.read','platform.channel_brand.manage',
    'platform.channel_brand.release')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE TABLE platform_brand_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_brand_permission_extensions(permission_code)
);
INSERT INTO platform_brand_permission_extensions VALUES
  ('platform.brand.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.brand.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.brand.release',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.channel_brand.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.channel_brand.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.channel_brand.release',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
INSERT INTO platform_brand_role_permissions
SELECT 'platform_owner',permission_code,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM platform_brand_permission_extensions;
INSERT INTO platform_brand_role_permissions VALUES
  ('platform_channel','platform.channel_brand.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_channel','platform.channel_brand.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('release_manager','platform.brand.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('release_manager','platform.brand.release',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('release_manager','platform.channel_brand.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('release_manager','platform.channel_brand.release',strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE workspace_brand_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'deployment.branding.read','deployment.branding.manage')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE TABLE workspace_brand_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('owner','admin')),
  permission_code TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_brand_permission_extensions(permission_code)
);
INSERT INTO workspace_brand_permission_extensions VALUES
  ('deployment.branding.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('deployment.branding.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
INSERT INTO workspace_brand_role_permissions
SELECT role_code,permission_code,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (SELECT 'owner' role_code UNION ALL SELECT 'admin') roles
CROSS JOIN workspace_brand_permission_extensions;

CREATE TRIGGER trg_platform_brand_permissions_insert_prd142 BEFORE INSERT ON platform_brand_permission_extensions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_platform_brand_permissions_update_prd142 BEFORE UPDATE ON platform_brand_permission_extensions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_platform_brand_permissions_delete_prd142 BEFORE DELETE ON platform_brand_permission_extensions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_platform_brand_roles_insert_prd142 BEFORE INSERT ON platform_brand_role_permissions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_platform_brand_roles_update_prd142 BEFORE UPDATE ON platform_brand_role_permissions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_platform_brand_roles_delete_prd142 BEFORE DELETE ON platform_brand_role_permissions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_workspace_brand_permissions_insert_prd142 BEFORE INSERT ON workspace_brand_permission_extensions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_workspace_brand_permissions_update_prd142 BEFORE UPDATE ON workspace_brand_permission_extensions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_workspace_brand_permissions_delete_prd142 BEFORE DELETE ON workspace_brand_permission_extensions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_workspace_brand_roles_insert_prd142 BEFORE INSERT ON workspace_brand_role_permissions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_workspace_brand_roles_update_prd142 BEFORE UPDATE ON workspace_brand_role_permissions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;
CREATE TRIGGER trg_workspace_brand_roles_delete_prd142 BEFORE DELETE ON workspace_brand_role_permissions BEGIN SELECT RAISE(ABORT,'brand_permission_immutable'); END;

CREATE TABLE platform_brands(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  platform_brand_id TEXT NOT NULL UNIQUE CHECK(length(platform_brand_id)=36
    AND substr(platform_brand_id,1,4)='pbr_' AND substr(platform_brand_id,5) NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('draft','ready','released','suspended')),
  product_name TEXT NOT NULL CHECK(length(CAST(product_name AS BLOB)) BETWEEN 1 AND 80),
  primary_color TEXT NOT NULL CHECK(length(primary_color)=7 AND primary_color GLOB '#??????'
    AND substr(primary_color,2) NOT GLOB '*[^0-9A-F]*'),
  secondary_color TEXT NOT NULL CHECK(length(secondary_color)=7 AND secondary_color GLOB '#??????'
    AND substr(secondary_color,2) NOT GLOB '*[^0-9A-F]*'),
  login_title TEXT NOT NULL CHECK(length(CAST(login_title AS BLOB)) BETWEEN 1 AND 120),
  login_message TEXT NOT NULL CHECK(length(CAST(login_message AS BLOB)) BETWEEN 1 AND 240),
  draft_logo_asset_id TEXT,
  draft_favicon_asset_id TEXT,
  draft_domain_id TEXT,
  published_generation INTEGER NOT NULL CHECK(published_generation>=1),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation BETWEEN 1 AND published_generation),
  revision INTEGER NOT NULL CHECK(revision>=1),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  updated_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id)
);
INSERT INTO platform_brands VALUES(
  1,'pbr_'||substr(metahub_sha256('metahub-platform-brand-v1'),1,32),'released',
  'MetaHub','#176B87','#F59E0B','企业 AI Gateway','安全、可追踪的企业模型网关',
  NULL,NULL,NULL,1,1,1,metahub_sha256('MetaHub|#176B87|#F59E0B|企业 AI Gateway|安全、可追踪的企业模型网关'),
  NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE channel_brands(
  channel_brand_id TEXT PRIMARY KEY CHECK(length(channel_brand_id)=36
    AND substr(channel_brand_id,1,4)='cbr_' AND substr(channel_brand_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64),
  contract_revision INTEGER NOT NULL CHECK(contract_revision>=1),
  status TEXT NOT NULL CHECK(status IN('draft','pending_domain','ready','released','suspended','archived')),
  product_name TEXT NOT NULL CHECK(length(CAST(product_name AS BLOB)) BETWEEN 1 AND 80),
  primary_color TEXT NOT NULL CHECK(length(primary_color)=7 AND primary_color GLOB '#??????'
    AND substr(primary_color,2) NOT GLOB '*[^0-9A-F]*'),
  secondary_color TEXT NOT NULL CHECK(length(secondary_color)=7 AND secondary_color GLOB '#??????'
    AND substr(secondary_color,2) NOT GLOB '*[^0-9A-F]*'),
  login_title TEXT NOT NULL CHECK(length(CAST(login_title AS BLOB)) BETWEEN 1 AND 120),
  login_message TEXT NOT NULL CHECK(length(CAST(login_message AS BLOB)) BETWEEN 1 AND 240),
  draft_logo_asset_id TEXT,
  draft_favicon_asset_id TEXT,
  draft_domain_id TEXT,
  published_generation INTEGER NOT NULL CHECK(published_generation>=0),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation BETWEEN 0 AND published_generation),
  revision INTEGER NOT NULL CHECK(revision>=1),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  updated_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(channel_partner_id,owner_workspace_id,tenant_id,channel_brand_id),
  UNIQUE(channel_partner_id)
);

CREATE TABLE deployment_brands(
  deployment_brand_id TEXT PRIMARY KEY CHECK(length(deployment_brand_id)=36
    AND substr(deployment_brand_id,1,4)='dbr_' AND substr(deployment_brand_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('draft','ready','published','suspended')),
  product_name TEXT NOT NULL CHECK(length(CAST(product_name AS BLOB)) BETWEEN 1 AND 80),
  primary_color TEXT NOT NULL CHECK(length(primary_color)=7 AND primary_color GLOB '#??????'
    AND substr(primary_color,2) NOT GLOB '*[^0-9A-F]*'),
  secondary_color TEXT NOT NULL CHECK(length(secondary_color)=7 AND secondary_color GLOB '#??????'
    AND substr(secondary_color,2) NOT GLOB '*[^0-9A-F]*'),
  login_title TEXT NOT NULL CHECK(length(CAST(login_title AS BLOB)) BETWEEN 1 AND 120),
  login_message TEXT NOT NULL CHECK(length(CAST(login_message AS BLOB)) BETWEEN 1 AND 240),
  draft_logo_asset_id TEXT,
  draft_favicon_asset_id TEXT,
  desired_generation INTEGER NOT NULL CHECK(desired_generation>=0),
  applied_generation INTEGER NOT NULL CHECK(applied_generation BETWEEN 0 AND desired_generation),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation BETWEEN 0 AND applied_generation),
  revision INTEGER NOT NULL CHECK(revision>=1),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  updated_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,deployment_brand_id),
  UNIQUE(edge_deployment_id)
);

CREATE TABLE platform_channel_partner_scopes(
  operator_id TEXT NOT NULL,
  channel_partner_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  granted_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(operator_id,channel_partner_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(channel_partner_id) REFERENCES channel_partners(channel_partner_id),
  FOREIGN KEY(granted_by_operator_id) REFERENCES platform_operators(operator_id)
);

CREATE TABLE brand_assets(
  brand_asset_id TEXT PRIMARY KEY CHECK(length(brand_asset_id)=36
    AND substr(brand_asset_id,1,4)='bas_' AND substr(brand_asset_id,5) NOT GLOB '*[^0-9a-f]*'),
  owner_kind TEXT NOT NULL CHECK(owner_kind IN('platform','channel','deployment')),
  platform_brand_id TEXT,
  channel_brand_id TEXT,
  deployment_brand_id TEXT,
  asset_kind TEXT NOT NULL CHECK(asset_kind IN('logo','favicon')),
  mime_type TEXT NOT NULL CHECK(mime_type='image/png'),
  object_key TEXT NOT NULL UNIQUE CHECK(length(object_key) BETWEEN 16 AND 200),
  byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 1 AND 524288),
  width INTEGER NOT NULL CHECK(width BETWEEN 16 AND 1024),
  height INTEGER NOT NULL CHECK(height BETWEEN 16 AND 1024),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  scan_result_sha256 TEXT,
  status TEXT NOT NULL CHECK(status IN('staged','scanning','clean','rejected','published','quarantined')),
  content_revision INTEGER NOT NULL CHECK(content_revision=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  scanned_at TEXT CHECK(scanned_at IS NULL OR unixepoch(scanned_at) IS NOT NULL),
  published_at TEXT CHECK(published_at IS NULL OR unixepoch(published_at) IS NOT NULL),
  FOREIGN KEY(platform_brand_id) REFERENCES platform_brands(platform_brand_id),
  FOREIGN KEY(channel_brand_id) REFERENCES channel_brands(channel_brand_id),
  FOREIGN KEY(deployment_brand_id) REFERENCES deployment_brands(deployment_brand_id),
  CHECK((owner_kind='platform' AND platform_brand_id IS NOT NULL
      AND channel_brand_id IS NULL AND deployment_brand_id IS NULL)
    OR(owner_kind='channel' AND platform_brand_id IS NULL
      AND channel_brand_id IS NOT NULL AND deployment_brand_id IS NULL)
    OR(owner_kind='deployment' AND platform_brand_id IS NULL
      AND channel_brand_id IS NULL AND deployment_brand_id IS NOT NULL))
);
CREATE INDEX idx_brand_assets_scan_prd142 ON brand_assets(status,created_at,brand_asset_id);

CREATE TABLE brand_verified_domains(
  verified_domain_id TEXT PRIMARY KEY CHECK(length(verified_domain_id)=36
    AND substr(verified_domain_id,1,4)='vdo_' AND substr(verified_domain_id,5) NOT GLOB '*[^0-9a-f]*'),
  owner_kind TEXT NOT NULL CHECK(owner_kind IN('platform','channel','deployment')),
  platform_brand_id TEXT,
  channel_brand_id TEXT,
  deployment_brand_id TEXT,
  canonical_domain TEXT NOT NULL UNIQUE CHECK(length(canonical_domain) BETWEEN 4 AND 253
    AND canonical_domain=lower(canonical_domain) AND canonical_domain NOT GLOB '*[^a-z0-9.-]*'),
  challenge_hmac TEXT NOT NULL CHECK(length(challenge_hmac)=64),
  challenge_generation INTEGER NOT NULL CHECK(challenge_generation>=1),
  observation_count INTEGER NOT NULL CHECK(observation_count BETWEEN 0 AND 2),
  status TEXT NOT NULL CHECK(status IN('pending','verified','active','reverification_required','revoked','quarantined')),
  certificate_ready INTEGER NOT NULL CHECK(certificate_ready IN(0,1)),
  routing_ready INTEGER NOT NULL CHECK(routing_ready IN(0,1)),
  revision INTEGER NOT NULL CHECK(revision>=1),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  last_verified_at TEXT CHECK(last_verified_at IS NULL OR unixepoch(last_verified_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(platform_brand_id) REFERENCES platform_brands(platform_brand_id),
  FOREIGN KEY(channel_brand_id) REFERENCES channel_brands(channel_brand_id),
  FOREIGN KEY(deployment_brand_id) REFERENCES deployment_brands(deployment_brand_id),
  CHECK((owner_kind='platform' AND platform_brand_id IS NOT NULL
      AND channel_brand_id IS NULL AND deployment_brand_id IS NULL)
    OR(owner_kind='channel' AND platform_brand_id IS NULL
      AND channel_brand_id IS NOT NULL AND deployment_brand_id IS NULL)
    OR(owner_kind='deployment' AND platform_brand_id IS NULL
      AND channel_brand_id IS NULL AND deployment_brand_id IS NOT NULL))
);
CREATE TABLE brand_domain_observations(
  brand_domain_observation_id TEXT PRIMARY KEY CHECK(length(brand_domain_observation_id)=36
    AND substr(brand_domain_observation_id,1,4)='bdo_'),
  verified_domain_id TEXT NOT NULL,
  challenge_generation INTEGER NOT NULL,
  resolver_code TEXT NOT NULL CHECK(resolver_code IN('resolver_a','resolver_b')),
  observation_sha256 TEXT NOT NULL CHECK(length(observation_sha256)=64),
  dnssec_valid INTEGER NOT NULL CHECK(dnssec_valid IN(0,1)),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  FOREIGN KEY(verified_domain_id) REFERENCES brand_verified_domains(verified_domain_id),
  UNIQUE(verified_domain_id,challenge_generation,resolver_code)
);

CREATE TABLE brand_releases(
  brand_release_id TEXT PRIMARY KEY CHECK(length(brand_release_id)=36
    AND substr(brand_release_id,1,4)='brl_' AND substr(brand_release_id,5) NOT GLOB '*[^0-9a-f]*'),
  owner_kind TEXT NOT NULL CHECK(owner_kind IN('platform','channel','deployment')),
  platform_brand_id TEXT,
  channel_brand_id TEXT,
  deployment_brand_id TEXT,
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('released','published','superseded','suspended','lkg')),
  product_name TEXT NOT NULL,
  primary_color TEXT NOT NULL,
  secondary_color TEXT NOT NULL,
  login_title TEXT NOT NULL,
  login_message TEXT NOT NULL,
  logo_asset_id TEXT,
  favicon_asset_id TEXT,
  verified_domain_id TEXT,
  config_sha256 TEXT NOT NULL CHECK(length(config_sha256)=64),
  asset_sha256 TEXT CHECK(asset_sha256 IS NULL OR length(asset_sha256)=64),
  domain_sha256 TEXT CHECK(domain_sha256 IS NULL OR length(domain_sha256)=64),
  released_by_operator_id TEXT,
  published_by_account_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(platform_brand_id) REFERENCES platform_brands(platform_brand_id),
  FOREIGN KEY(channel_brand_id) REFERENCES channel_brands(channel_brand_id),
  FOREIGN KEY(deployment_brand_id) REFERENCES deployment_brands(deployment_brand_id),
  FOREIGN KEY(logo_asset_id) REFERENCES brand_assets(brand_asset_id),
  FOREIGN KEY(favicon_asset_id) REFERENCES brand_assets(brand_asset_id),
  FOREIGN KEY(verified_domain_id) REFERENCES brand_verified_domains(verified_domain_id),
  FOREIGN KEY(released_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(published_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(owner_kind,platform_brand_id,generation),
  UNIQUE(owner_kind,channel_brand_id,generation),
  UNIQUE(owner_kind,deployment_brand_id,generation),
  CHECK((owner_kind='platform' AND platform_brand_id IS NOT NULL
      AND channel_brand_id IS NULL AND deployment_brand_id IS NULL)
    OR(owner_kind='channel' AND platform_brand_id IS NULL
      AND channel_brand_id IS NOT NULL AND deployment_brand_id IS NULL)
    OR(owner_kind='deployment' AND platform_brand_id IS NULL
      AND channel_brand_id IS NULL AND deployment_brand_id IS NOT NULL))
);
INSERT INTO brand_releases(
  brand_release_id,owner_kind,platform_brand_id,channel_brand_id,deployment_brand_id,
  generation,status,product_name,primary_color,secondary_color,login_title,login_message,
  logo_asset_id,favicon_asset_id,verified_domain_id,config_sha256,asset_sha256,domain_sha256,
  released_by_operator_id,published_by_account_id,created_at)
SELECT 'brl_'||substr(metahub_sha256('platform:'||platform_brand_id||':1'),1,32),
  'platform',platform_brand_id,NULL,NULL,1,'released',product_name,primary_color,
  secondary_color,login_title,login_message,NULL,NULL,NULL,source_sha256,NULL,NULL,NULL,NULL,
  created_at
FROM platform_brands WHERE singleton_id=1;

CREATE TABLE brand_public_heads(
  brand_public_head_id TEXT PRIMARY KEY CHECK(length(brand_public_head_id)=36
    AND substr(brand_public_head_id,1,4)='bph_'),
  canonical_host TEXT NOT NULL UNIQUE,
  owner_kind TEXT NOT NULL CHECK(owner_kind IN('platform','channel')),
  platform_brand_id TEXT,
  channel_brand_id TEXT,
  brand_release_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','lkg','revoked')),
  cache_epoch INTEGER NOT NULL CHECK(cache_epoch>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(platform_brand_id) REFERENCES platform_brands(platform_brand_id),
  FOREIGN KEY(channel_brand_id) REFERENCES channel_brands(channel_brand_id),
  FOREIGN KEY(brand_release_id) REFERENCES brand_releases(brand_release_id),
  CHECK((owner_kind='platform' AND platform_brand_id IS NOT NULL AND channel_brand_id IS NULL)
    OR(owner_kind='channel' AND platform_brand_id IS NULL AND channel_brand_id IS NOT NULL))
);

CREATE TABLE deployment_brand_bundles(
  deployment_brand_bundle_id TEXT PRIMARY KEY CHECK(length(deployment_brand_bundle_id)=36
    AND substr(deployment_brand_bundle_id,1,4)='dbb_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  deployment_brand_id TEXT NOT NULL,
  brand_release_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json) AND json_type(manifest_json)='object'),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  signature_sha256 TEXT NOT NULL CHECK(length(signature_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending','available','applied','superseded','failed')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,deployment_brand_id)
    REFERENCES deployment_brands(workspace_id,tenant_id,edge_deployment_id,deployment_brand_id),
  FOREIGN KEY(brand_release_id) REFERENCES brand_releases(brand_release_id),
  UNIQUE(edge_deployment_id,generation)
);
CREATE TABLE deployment_brand_acks(
  deployment_brand_ack_id TEXT PRIMARY KEY CHECK(length(deployment_brand_ack_id)=36
    AND substr(deployment_brand_ack_id,1,4)='dba_'),
  edge_deployment_id TEXT NOT NULL,
  deployment_brand_bundle_id TEXT NOT NULL,
  applied_generation INTEGER NOT NULL CHECK(applied_generation>=1),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation BETWEEN 1 AND applied_generation),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('applied','rejected','stale')),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  FOREIGN KEY(deployment_brand_bundle_id) REFERENCES deployment_brand_bundles(deployment_brand_bundle_id),
  UNIQUE(edge_deployment_id,applied_generation)
);

CREATE TABLE brand_commands(
  brand_command_id TEXT PRIMARY KEY CHECK(length(brand_command_id)=36
    AND substr(brand_command_id,1,4)='bco_'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace')),
  actor_id TEXT NOT NULL,
  owner_kind TEXT NOT NULL CHECK(owner_kind IN('platform','channel','deployment')),
  owner_id TEXT NOT NULL,
  action_code TEXT NOT NULL,
  idempotency_hmac TEXT NOT NULL CHECK(length(idempotency_hmac)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  expected_revision INTEGER NOT NULL CHECK(expected_revision>=1),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  result_generation INTEGER NOT NULL CHECK(result_generation>=0),
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(actor_domain,actor_id,owner_kind,owner_id,idempotency_hmac)
);

CREATE TABLE brand_events(
  brand_event_id TEXT PRIMARY KEY CHECK(length(brand_event_id)=36
    AND substr(brand_event_id,1,4)='bre_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'platform_brand.changed','channel_brand.changed','deployment_brand.changed',
    'brand_domain.verified')),
  owner_kind TEXT NOT NULL CHECK(owner_kind IN('platform','channel','deployment')),
  owner_id TEXT NOT NULL,
  object_id TEXT NOT NULL,
  status TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  config_sha256 TEXT CHECK(config_sha256 IS NULL OR length(config_sha256)=64),
  asset_sha256 TEXT CHECK(asset_sha256 IS NULL OR length(asset_sha256)=64),
  domain_sha256 TEXT CHECK(domain_sha256 IS NULL OR length(domain_sha256)=64),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace','scheduler')),
  actor_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(event_type,owner_kind,owner_id,generation)
);
CREATE TABLE brand_event_outbox(
  brand_event_outbox_id TEXT PRIMARY KEY CHECK(length(brand_event_outbox_id)=36
    AND substr(brand_event_outbox_id,1,4)='beo_'),
  brand_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','leased','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  generation INTEGER NOT NULL CHECK(generation>=0),
  lease_owner_id TEXT,
  lease_token_hmac TEXT,
  lease_expires_at TEXT,
  next_attempt_at TEXT,
  last_error_code TEXT,
  delivered_at TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(brand_event_id) REFERENCES brand_events(brand_event_id)
);
CREATE INDEX idx_brand_event_outbox_claim_prd142
  ON brand_event_outbox(status,next_attempt_at,created_at,brand_event_outbox_id);

CREATE TABLE brand_legacy_object_mappings(
  brand_legacy_object_mapping_id TEXT PRIMARY KEY CHECK(length(brand_legacy_object_mapping_id)=36
    AND substr(brand_legacy_object_mapping_id,1,4)='blm_'),
  source_kind TEXT NOT NULL CHECK(source_kind IN('branding_config','branding_event','branding_event_outbox')),
  source_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  target_kind TEXT CHECK(target_kind IS NULL OR target_kind IN('channel','deployment','history')),
  target_id TEXT,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('default_collapsed','requires_assignment','historical_preserved')),
  mapped_at TEXT NOT NULL CHECK(unixepoch(mapped_at) IS NOT NULL),
  UNIQUE(source_kind,source_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  CHECK((status='default_collapsed' AND target_kind IS NULL AND target_id IS NULL)
    OR(status='requires_assignment' AND target_kind IS NULL AND target_id IS NULL)
    OR(status='historical_preserved' AND target_kind='history' AND target_id IS NULL))
);
INSERT INTO brand_legacy_object_mappings
SELECT 'blm_'||substr(metahub_sha256('config:'||branding_config_id),1,32),
  'branding_config',branding_config_id,tenant_id,NULL,NULL,
  metahub_sha256(branding_config_id||char(31)||tenant_id||char(31)||product_name||char(31)||
    primary_color||char(31)||secondary_color||char(31)||login_title||char(31)||login_message||
    char(31)||coalesce(custom_domain,'')||char(31)||coalesce(logo_sha256,'')||char(31)||revision),
  CASE WHEN product_name='MetaHub' AND primary_color='#176B87' AND secondary_color='#F59E0B'
    AND login_title='企业 AI Gateway' AND login_message='安全、可追踪的企业模型网关'
    AND custom_domain IS NULL AND logo_png IS NULL AND revision=1
    AND NOT EXISTS(SELECT 1 FROM branding_events event
      WHERE event.tenant_id=branding_configs.tenant_id)
    THEN 'default_collapsed' ELSE 'requires_assignment' END,
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM branding_configs;
INSERT INTO brand_legacy_object_mappings
SELECT 'blm_'||substr(metahub_sha256('event:'||branding_event_id),1,32),
  'branding_event',branding_event_id,tenant_id,'history',NULL,payload_sha256,
  'historical_preserved',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM branding_events;
INSERT INTO brand_legacy_object_mappings
SELECT 'blm_'||substr(metahub_sha256('outbox:'||branding_event_outbox_id),1,32),
  'branding_event_outbox',branding_event_outbox_id,tenant_id,'history',NULL,payload_sha256,
  'historical_preserved',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM branding_event_outbox;

CREATE TABLE brand_migration_cursors(
  brand_migration_cursor_id TEXT PRIMARY KEY CHECK(length(brand_migration_cursor_id)=36
    AND substr(brand_migration_cursor_id,1,4)='bmc_'),
  source_kind TEXT NOT NULL UNIQUE CHECK(source_kind IN(
    'branding_config','branding_event','branding_event_outbox')),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  mapped_row_count INTEGER NOT NULL CHECK(mapped_row_count=source_row_count),
  status TEXT NOT NULL CHECK(status='cutover'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
INSERT INTO brand_migration_cursors
SELECT 'bmc_'||substr(metahub_sha256(source_kind),1,32),source_kind,row_count,
  metahub_sha256(source_kind||':'||row_count),row_count,'cutover',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (
  SELECT 'branding_config' source_kind,count(*) row_count FROM branding_configs
  UNION ALL SELECT 'branding_event',count(*) FROM branding_events
  UNION ALL SELECT 'branding_event_outbox',count(*) FROM branding_event_outbox
) WHERE row_count>0;

CREATE TABLE brand_scheduler_state(
  job_code TEXT PRIMARY KEY CHECK(job_code IN(
    'legacy_outbox','asset_scan','domain_verify','public_publish',
    'deployment_bundle','brand_event_outbox','cleanup')),
  cursor_value TEXT,
  fence_generation INTEGER NOT NULL CHECK(fence_generation>=1),
  lease_owner_id TEXT,
  lease_token_hmac TEXT,
  lease_expires_at TEXT,
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO brand_scheduler_state
SELECT job_code,NULL,1,NULL,NULL,NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (SELECT 'legacy_outbox' job_code UNION ALL SELECT 'asset_scan'
  UNION ALL SELECT 'domain_verify' UNION ALL SELECT 'public_publish'
  UNION ALL SELECT 'deployment_bundle' UNION ALL SELECT 'brand_event_outbox'
  UNION ALL SELECT 'cleanup');

-- Legacy config rows are inventory-only after cutover. The legacy outbox remains mutable
-- so the replacement scheduler can finish the frozen delivery snapshot.
DROP TRIGGER trg_tenants_branding_initialize_prd088;
CREATE TRIGGER trg_branding_configs_cutover_prd142 BEFORE INSERT ON branding_configs
BEGIN SELECT RAISE(ABORT,'branding_cutover_read_only'); END;
CREATE TRIGGER trg_branding_configs_cutover_update_prd142 BEFORE UPDATE ON branding_configs
BEGIN SELECT RAISE(ABORT,'branding_cutover_read_only'); END;
CREATE TRIGGER trg_branding_configs_cutover_delete_prd142 BEFORE DELETE ON branding_configs
BEGIN SELECT RAISE(ABORT,'branding_cutover_read_only'); END;

CREATE TRIGGER trg_platform_brands_owner_prd142 BEFORE UPDATE ON platform_brands FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.platform_brand_id!=OLD.platform_brand_id
  OR NEW.revision!=OLD.revision+1 OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'platform_brand_owner_immutable'); END;
CREATE TRIGGER trg_platform_brands_delete_prd142 BEFORE DELETE ON platform_brands
BEGIN SELECT RAISE(ABORT,'platform_brand_immutable'); END;
CREATE TRIGGER trg_channel_brands_owner_prd142 BEFORE UPDATE ON channel_brands FOR EACH ROW
WHEN NEW.channel_brand_id!=OLD.channel_brand_id OR NEW.channel_partner_id!=OLD.channel_partner_id
  OR NEW.owner_workspace_id!=OLD.owner_workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.revision!=OLD.revision+1 OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'channel_brand_owner_immutable'); END;
CREATE TRIGGER trg_channel_brands_delete_prd142 BEFORE DELETE ON channel_brands
BEGIN SELECT RAISE(ABORT,'channel_brand_immutable'); END;
CREATE TRIGGER trg_deployment_brands_owner_prd142 BEFORE UPDATE ON deployment_brands FOR EACH ROW
WHEN NEW.deployment_brand_id!=OLD.deployment_brand_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.revision!=OLD.revision+1 OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'deployment_brand_owner_immutable'); END;
CREATE TRIGGER trg_deployment_brands_delete_prd142 BEFORE DELETE ON deployment_brands
BEGIN SELECT RAISE(ABORT,'deployment_brand_immutable'); END;
CREATE TRIGGER trg_brand_assets_owner_prd142 BEFORE UPDATE ON brand_assets FOR EACH ROW
WHEN NEW.brand_asset_id!=OLD.brand_asset_id OR NEW.owner_kind!=OLD.owner_kind
  OR NEW.platform_brand_id IS NOT OLD.platform_brand_id
  OR NEW.channel_brand_id IS NOT OLD.channel_brand_id
  OR NEW.deployment_brand_id IS NOT OLD.deployment_brand_id
  OR NEW.asset_kind!=OLD.asset_kind OR NEW.object_key!=OLD.object_key
  OR NEW.byte_count!=OLD.byte_count OR NEW.width!=OLD.width OR NEW.height!=OLD.height
  OR NEW.content_sha256!=OLD.content_sha256 OR NEW.content_revision!=OLD.content_revision
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'brand_asset_owner_immutable'); END;
CREATE TRIGGER trg_brand_assets_delete_prd142 BEFORE DELETE ON brand_assets
BEGIN SELECT RAISE(ABORT,'brand_asset_immutable'); END;
CREATE TRIGGER trg_brand_verified_domains_owner_prd142 BEFORE UPDATE ON brand_verified_domains FOR EACH ROW
WHEN NEW.verified_domain_id!=OLD.verified_domain_id OR NEW.owner_kind!=OLD.owner_kind
  OR NEW.platform_brand_id IS NOT OLD.platform_brand_id
  OR NEW.channel_brand_id IS NOT OLD.channel_brand_id
  OR NEW.deployment_brand_id IS NOT OLD.deployment_brand_id
  OR NEW.canonical_domain!=OLD.canonical_domain OR NEW.revision!=OLD.revision+1
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'brand_domain_owner_immutable'); END;
CREATE TRIGGER trg_brand_verified_domains_delete_prd142 BEFORE DELETE ON brand_verified_domains
BEGIN SELECT RAISE(ABORT,'brand_domain_immutable'); END;
CREATE TRIGGER trg_brand_releases_update_prd142 BEFORE UPDATE ON brand_releases
BEGIN SELECT RAISE(ABORT,'brand_release_immutable'); END;
CREATE TRIGGER trg_brand_releases_delete_prd142 BEFORE DELETE ON brand_releases
BEGIN SELECT RAISE(ABORT,'brand_release_immutable'); END;
CREATE TRIGGER trg_brand_events_update_prd142 BEFORE UPDATE ON brand_events
BEGIN SELECT RAISE(ABORT,'brand_event_immutable'); END;
CREATE TRIGGER trg_brand_events_delete_prd142 BEFORE DELETE ON brand_events
BEGIN SELECT RAISE(ABORT,'brand_event_immutable'); END;
CREATE TRIGGER trg_brand_commands_update_prd142 BEFORE UPDATE ON brand_commands
BEGIN SELECT RAISE(ABORT,'brand_command_immutable'); END;
CREATE TRIGGER trg_brand_commands_delete_prd142 BEFORE DELETE ON brand_commands
BEGIN SELECT RAISE(ABORT,'brand_command_immutable'); END;
CREATE TRIGGER trg_brand_legacy_mapping_update_prd142 BEFORE UPDATE ON brand_legacy_object_mappings
BEGIN SELECT RAISE(ABORT,'brand_mapping_immutable'); END;
CREATE TRIGGER trg_brand_legacy_mapping_delete_prd142 BEFORE DELETE ON brand_legacy_object_mappings
BEGIN SELECT RAISE(ABORT,'brand_mapping_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;

CREATE TEMP TABLE prd142_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd142_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + CASE WHEN (SELECT COUNT(*) FROM platform_brand_permission_extensions)=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_brand_role_permissions)=12 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_brand_role_permissions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions
      WHERE code IN('branding.read','branding.manage'))=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM brand_legacy_object_mappings)=
      (SELECT COUNT(*) FROM branding_configs)+(SELECT COUNT(*) FROM branding_events)+
      (SELECT COUNT(*) FROM branding_event_outbox) THEN 0 ELSE 1 END;
DROP TABLE prd142_post_guard;
