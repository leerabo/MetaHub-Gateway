PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- PRD-088 only accepts the published PRD-087 schema as its base.
CREATE TEMP TABLE prd088_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd088_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
  ORDER BY version DESC LIMIT 1)='0080' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=96
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=22)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='b44391603c39067c55f6d32742e2cb3daa9833721ab2ed4657c2c50db4dca125'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=247
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='0ebf053173c2a5b1acc10143f092753ef2dafe1d63f2a4ba8398ff49599b4379'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'branding_configs','branding_events','branding_event_outbox',
      'rbac_permission_templates_prd088_new','permissions_prd088_new'))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd088')
  + CASE WHEN instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
      '''branding.updated''')=0
      AND instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
        '''branding_config''')=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd088_guard;

-- The branding owner is a tenant-scoped immutable configuration.  The Logo is
-- deliberately kept out of all JSON projections and event summaries.
CREATE TABLE branding_configs(
  branding_config_id TEXT PRIMARY KEY CHECK(length(branding_config_id)=36
    AND substr(branding_config_id,1,4)='brc_'
    AND substr(branding_config_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status='active'),
  product_name TEXT NOT NULL CHECK(length(CAST(product_name AS BLOB)) BETWEEN 1 AND 80
    AND instr(product_name,char(0))=0 AND instr(product_name,char(10))=0
    AND instr(product_name,char(13))=0),
  primary_color TEXT NOT NULL CHECK(primary_color GLOB '#??????'
    AND length(primary_color)=7 AND substr(primary_color,2) NOT GLOB '*[^0-9A-F]*'),
  secondary_color TEXT NOT NULL CHECK(secondary_color GLOB '#??????'
    AND length(secondary_color)=7 AND substr(secondary_color,2) NOT GLOB '*[^0-9A-F]*'),
  login_title TEXT NOT NULL CHECK(length(CAST(login_title AS BLOB)) BETWEEN 1 AND 120
    AND instr(login_title,char(0))=0 AND instr(login_title,char(10))=0
    AND instr(login_title,char(13))=0),
  login_message TEXT NOT NULL CHECK(length(CAST(login_message AS BLOB)) BETWEEN 1 AND 240
    AND instr(login_message,char(0))=0 AND instr(login_message,char(10))=0
    AND instr(login_message,char(13))=0),
  custom_domain TEXT UNIQUE CHECK(custom_domain IS NULL OR(
    length(CAST(custom_domain AS BLOB)) BETWEEN 4 AND 253
    AND custom_domain=lower(custom_domain) AND instr(custom_domain,'.')>0
    AND custom_domain NOT GLOB '*[^a-z0-9.-]*')),
  custom_domain_status TEXT NOT NULL CHECK(custom_domain_status IN('not_configured','reserved')),
  logo_png BLOB CHECK(logo_png IS NULL OR length(logo_png)<=131072),
  logo_sha256 TEXT CHECK(logo_sha256 IS NULL OR(length(logo_sha256)=64
    AND logo_sha256 NOT GLOB '*[^0-9a-f]*')),
  logo_width INTEGER CHECK(logo_width IS NULL OR(typeof(logo_width)='integer' AND logo_width BETWEEN 16 AND 512)),
  logo_height INTEGER CHECK(logo_height IS NULL OR(typeof(logo_height)='integer' AND logo_height BETWEEN 16 AND 512)),
  revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
  updated_by_user_id TEXT,
  updated_request_id TEXT,
  updated_trace_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,updated_by_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,branding_config_id),
  CHECK((logo_png IS NULL AND logo_sha256 IS NULL AND logo_width IS NULL AND logo_height IS NULL)
    OR(logo_png IS NOT NULL AND logo_sha256 IS NOT NULL AND logo_width IS NOT NULL AND logo_height IS NOT NULL)),
  CHECK((custom_domain IS NULL AND custom_domain_status='not_configured')
    OR(custom_domain IS NOT NULL AND custom_domain_status='reserved'))
);
CREATE INDEX idx_branding_configs_tenant_prd088 ON branding_configs(tenant_id,status);

CREATE TABLE branding_events(
  branding_event_id TEXT PRIMARY KEY CHECK(length(branding_event_id)=36
    AND substr(branding_event_id,1,4)='bev_' AND substr(branding_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  branding_config_id TEXT NOT NULL,
  audit_log_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type='branding.updated'),
  revision INTEGER NOT NULL CHECK(revision>=2),
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  changed_fields_json TEXT NOT NULL CHECK(json_valid(changed_fields_json)
    AND json_type(changed_fields_json)='array'),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(CAST(payload_json AS BLOB))<=4096),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,branding_config_id) REFERENCES branding_configs(tenant_id,branding_config_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(tenant_id,audit_log_id) REFERENCES audit_logs(tenant_id,audit_log_id),
  UNIQUE(tenant_id,branding_event_id),
  UNIQUE(tenant_id,branding_config_id,revision),
  UNIQUE(tenant_id,audit_log_id)
);
CREATE INDEX idx_branding_events_tenant_time_prd088
  ON branding_events(tenant_id,created_at DESC,branding_event_id);

CREATE TABLE branding_event_outbox(
  branding_event_outbox_id TEXT PRIMARY KEY CHECK(length(branding_event_outbox_id)=36
    AND substr(branding_event_outbox_id,1,4)='bro_' AND substr(branding_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  branding_event_id TEXT NOT NULL,
  webhook_event_id TEXT NOT NULL CHECK(length(webhook_event_id)=40
    AND substr(webhook_event_id,1,8)='evt_brd_' AND substr(webhook_event_id,9) NOT GLOB '*[^0-9a-f]*'),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(CAST(payload_json AS BLOB))<=4096),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','leased','delivered','no_subscription','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 10),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  next_attempt_at TEXT,
  last_error_code TEXT,
  subscription_snapshot_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(subscription_snapshot_json)
    AND json_type(subscription_snapshot_json)='array'),
  snapshot_frozen_at TEXT,
  delivered_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,branding_event_id) REFERENCES branding_events(tenant_id,branding_event_id),
  UNIQUE(tenant_id,branding_event_id),
  UNIQUE(tenant_id,webhook_event_id),
  CHECK((status IN('pending','leased') AND delivered_at IS NULL)
    OR(status IN('delivered','no_subscription') AND delivered_at IS NOT NULL)
    OR(status='failed' AND attempt_count=10 AND delivered_at IS NULL))
);
CREATE INDEX idx_branding_outbox_claim_prd088
  ON branding_event_outbox(status,next_attempt_at,tenant_id,created_at,branding_event_outbox_id);

CREATE TRIGGER trg_branding_configs_owner_insert_prd088
BEFORE INSERT ON branding_configs FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.branding_config_id!='brc_'||lower(substr(NEW.branding_config_id,5))
  OR NEW.status!='active' OR NEW.revision!=1
  OR NEW.updated_by_user_id IS NOT NULL OR NEW.updated_request_id IS NOT NULL
  OR NEW.updated_trace_id IS NOT NULL
  OR EXISTS(SELECT 1 FROM branding_configs current WHERE current.tenant_id=NEW.tenant_id
    OR current.branding_config_id=NEW.branding_config_id OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'branding_config_owner_mismatch'); END;
CREATE TRIGGER trg_branding_configs_update_prd088
BEFORE UPDATE ON branding_configs FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.branding_config_id!=OLD.branding_config_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.status!=OLD.status
  OR NEW.revision!=OLD.revision+1 OR NEW.created_at!=OLD.created_at
  OR NEW.updated_by_user_id IS NULL OR NEW.updated_request_id IS NULL
  OR NEW.updated_trace_id IS NULL
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR(NEW.product_name=OLD.product_name AND NEW.primary_color=OLD.primary_color
    AND NEW.secondary_color=OLD.secondary_color AND NEW.login_title=OLD.login_title
    AND NEW.login_message=OLD.login_message AND NEW.custom_domain IS OLD.custom_domain
    AND NEW.logo_sha256 IS OLD.logo_sha256)
BEGIN SELECT RAISE(ABORT,'branding_config_transition_invalid'); END;
CREATE TRIGGER trg_branding_configs_delete_prd088 BEFORE DELETE ON branding_configs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'branding_config_immutable'); END;
CREATE TRIGGER trg_branding_events_immutable_prd088 BEFORE UPDATE ON branding_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'branding_event_immutable'); END;
CREATE TRIGGER trg_branding_events_delete_prd088 BEFORE DELETE ON branding_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'branding_event_immutable'); END;

CREATE TRIGGER trg_branding_events_owner_insert_prd088
BEFORE INSERT ON branding_events FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM branding_configs config
    WHERE config.tenant_id=NEW.tenant_id
      AND config.branding_config_id=NEW.branding_config_id
      AND config.revision=NEW.revision)
  OR NOT EXISTS(SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id)
  OR NOT EXISTS(SELECT 1 FROM audit_logs audit
    WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id)
  OR NEW.branding_event_id!='bev_'||substr(metahub_sha256(
      NEW.branding_config_id||char(31)||NEW.revision),1,32)
  OR NOT EXISTS(SELECT 1 FROM audit_logs audit
    WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
      AND audit.action='branding.updated' AND audit.object_type='branding_config'
      AND audit.object_id=NEW.branding_config_id
      AND audit.actor_user_id=NEW.actor_user_id
      AND audit.request_id=NEW.request_id AND audit.trace_id=NEW.trace_id
      AND audit.created_at=NEW.created_at)
  OR NEW.payload_sha256!=metahub_sha256(NEW.payload_json)
  OR json_valid(NEW.changed_fields_json)=0 OR json_type(NEW.changed_fields_json)!='array'
  OR json_valid(NEW.payload_json)=0 OR json_type(NEW.payload_json)!='object'
  OR EXISTS(SELECT 1 FROM branding_events current
    WHERE current.branding_event_id=NEW.branding_event_id
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'branding_event_owner_mismatch'); END;

CREATE TRIGGER trg_branding_event_outbox_owner_insert_prd088
BEFORE INSERT ON branding_event_outbox FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM branding_events event
    WHERE event.tenant_id=NEW.tenant_id AND event.branding_event_id=NEW.branding_event_id
      AND NEW.branding_event_outbox_id='bro_'||substr(event.branding_event_id,5)
      AND NEW.webhook_event_id='evt_brd_'||substr(event.branding_event_id,5)
      AND event.payload_json=NEW.payload_json
      AND event.payload_sha256=NEW.payload_sha256)
  OR NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.generation!=0
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_token IS NOT NULL
  OR NEW.lease_expires_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
  OR NEW.delivered_at IS NOT NULL OR NEW.next_attempt_at!=NEW.created_at
  OR NEW.updated_at!=NEW.created_at OR NEW.snapshot_frozen_at IS NULL
  OR NEW.snapshot_frozen_at!=NEW.created_at
  OR json_valid(NEW.subscription_snapshot_json)=0
  OR json_type(NEW.subscription_snapshot_json)!='array'
  OR json(NEW.subscription_snapshot_json)!=NEW.subscription_snapshot_json
  OR EXISTS(SELECT 1 FROM json_each(NEW.subscription_snapshot_json)
    WHERE type!='text' OR value NOT GLOB 'whd_[0-9a-f]*')
  OR EXISTS(SELECT 1 FROM json_each(NEW.subscription_snapshot_json) item
    WHERE NOT EXISTS(SELECT 1 FROM webhook_deliveries delivery
      WHERE delivery.tenant_id=NEW.tenant_id
        AND delivery.webhook_delivery_id=item.value
        AND delivery.event_id=NEW.webhook_event_id
        AND delivery.delivery_kind='original'))
  OR (SELECT COUNT(*) FROM json_each(NEW.subscription_snapshot_json))
     !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.subscription_snapshot_json))
  OR EXISTS(SELECT 1 FROM branding_event_outbox current
    WHERE current.branding_event_outbox_id=NEW.branding_event_outbox_id
      OR(current.tenant_id=NEW.tenant_id AND current.branding_event_id=NEW.branding_event_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'branding_outbox_owner_mismatch'); END;

CREATE TRIGGER trg_branding_event_outbox_transition_prd088
BEFORE UPDATE ON branding_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.branding_event_outbox_id!=OLD.branding_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.branding_event_id!=OLD.branding_event_id
  OR NEW.webhook_event_id!=OLD.webhook_event_id OR NEW.payload_json!=OLD.payload_json
  OR NEW.payload_sha256!=OLD.payload_sha256 OR NEW.created_at!=OLD.created_at
  OR NEW.subscription_snapshot_json IS NOT OLD.subscription_snapshot_json
  OR NEW.snapshot_frozen_at IS NOT OLD.snapshot_frozen_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT((OLD.status='pending' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count+1 AND NEW.generation=OLD.generation+1
      AND NEW.lease_owner_id IS NOT NULL AND NEW.lease_token IS NOT NULL
      AND NEW.lease_expires_at IS NOT NULL AND NEW.next_attempt_at IS NULL
      AND NEW.delivered_at IS NULL)
    OR(OLD.status='leased' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND NEW.lease_owner_id=OLD.lease_owner_id AND NEW.lease_token!=OLD.lease_token
      AND NEW.lease_expires_at IS NOT NULL AND NEW.delivered_at IS NULL)
    OR(OLD.status='leased' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND NEW.lease_owner_id IS NULL AND NEW.lease_token IS NULL
      AND NEW.lease_expires_at IS NULL AND NEW.next_attempt_at IS NOT NULL
      AND NEW.delivered_at IS NULL)
    OR(OLD.status='leased' AND NEW.status IN('delivered','no_subscription','failed')
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND NEW.lease_owner_id IS NULL AND NEW.lease_token IS NULL
      AND NEW.lease_expires_at IS NULL
      AND ((NEW.status IN('delivered','no_subscription') AND NEW.delivered_at IS NOT NULL)
        OR(NEW.status='failed' AND NEW.attempt_count=10 AND NEW.delivered_at IS NULL))))
BEGIN SELECT RAISE(ABORT,'branding_outbox_transition_invalid'); END;

CREATE TRIGGER trg_branding_event_outbox_delete_prd088
BEFORE DELETE ON branding_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'branding_outbox_immutable'); END;

CREATE TRIGGER trg_tenants_branding_initialize_prd088
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  INSERT INTO branding_configs(
    branding_config_id,tenant_id,status,product_name,primary_color,secondary_color,
    login_title,login_message,custom_domain,custom_domain_status,logo_png,logo_sha256,
    logo_width,logo_height,revision,created_at,updated_at)
  VALUES('brc_'||lower(hex(randomblob(16))),NEW.tenant_id,'active','MetaHub','#176B87','#F59E0B',
    '企业 AI Gateway','安全、可追踪的企业模型网关',NULL,'not_configured',NULL,NULL,NULL,NULL,1,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;

-- Seed existing tenants before installing the new-tenant trigger.
INSERT INTO branding_configs(
  branding_config_id,tenant_id,status,product_name,primary_color,secondary_color,
  login_title,login_message,custom_domain,custom_domain_status,logo_png,logo_sha256,
  logo_width,logo_height,revision,updated_by_user_id,updated_request_id,updated_trace_id,
  created_at,updated_at)
SELECT 'brc_'||lower(hex(randomblob(16))),tenant_id,'active','MetaHub','#176B87','#F59E0B',
  '企业 AI Gateway','安全、可追踪的企业模型网关',NULL,'not_configured',NULL,NULL,NULL,NULL,1,
  NULL,NULL,NULL,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants;

-- Extend the audit schema without dropping its long chain of historical owner triggers.
-- SQLite has no ALTER CHECK; writable_schema is scoped to these two exact enum tokens and
-- immediately disabled.  All existing indexes/triggers/rows remain byte-for-byte intact.
CREATE TEMP TABLE prd088_audit_schema_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd088_audit_schema_guard(issue_count)
SELECT CASE WHEN instr(sql,'''response_cache.invalidated''')>0
    AND instr(sql,'''response_cache_invalidation''')>0
    AND instr(sql,'''branding.updated''')=0 AND instr(sql,'''branding_config''')=0
  THEN 0 ELSE 1 END
FROM sqlite_schema WHERE type='table' AND name='audit_logs';
DROP TABLE prd088_audit_schema_guard;
PRAGMA writable_schema=ON;
UPDATE sqlite_master SET sql=replace(sql,'''response_cache.invalidated''','''response_cache.invalidated'',''branding.updated''')
WHERE type='table' AND name='audit_logs';
UPDATE sqlite_master SET sql=replace(sql,'''response_cache_invalidation''','''response_cache_invalidation'',''branding_config''')
WHERE type='table' AND name='audit_logs';
PRAGMA schema_version=1;
PRAGMA writable_schema=OFF;

-- Only roles still matching the revision-22 default matrix receive new default grants.
-- A tenant-customized non-admin matrix remains byte-for-byte unchanged and fails closed.
CREATE TEMP TABLE prd088_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
  END
  AND NOT EXISTS(
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS(
      SELECT 1 FROM role_permissions relation JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code))
  AND NOT EXISTS(
    SELECT 1 FROM role_permissions relation JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS(SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code));

-- RBAC revision 23.  Existing immutable triggers are temporarily removed for the atomic
-- catalog/table projection, then recreated with the new matrix hashes and tenant defaults.
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

CREATE TABLE rbac_permission_templates_prd088_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code) AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=23));
INSERT INTO rbac_permission_templates_prd088_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,23 FROM rbac_permission_templates;
INSERT INTO rbac_permission_templates_prd088_new VALUES
 ('branding.read','branding','读取白标配置','读取当前租户控制台与登录页的安全白标配置和受限 Logo。','read',23),
 ('branding.manage','branding','管理白标配置','修改当前租户控制台与登录页的 Logo、名称、配色和文案。','critical',23);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd088_new RENAME TO rbac_permission_templates;
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
SELECT role_code,'branding.read',1 FROM (SELECT 'admin' role_code UNION ALL SELECT 'developer'
  UNION ALL SELECT 'viewer' UNION ALL SELECT 'finance_admin' UNION ALL SELECT 'security_admin'
  UNION ALL SELECT 'ops_admin');
INSERT INTO rbac_role_permission_templates VALUES('admin','branding.manage',1);

CREATE TABLE permissions_prd088_new(
  permission_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, code TEXT NOT NULL,
  category TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=23), status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code), UNIQUE(tenant_id,permission_id));
INSERT INTO permissions_prd088_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,
  status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,23,status,created_at,updated_at
FROM permissions;
INSERT INTO permissions_prd088_new
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,23,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code IN('branding.read','branding.manage');
DROP TABLE permissions;
ALTER TABLE permissions_prd088_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,permission.permission_id,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd088_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id
WHERE permission.code='branding.read' OR(permission.code='branding.manage' AND role.code='admin');
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
   WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e'
   WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
   WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e'
   WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
   WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377'
   WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END
WHERE role_id IN(SELECT role_id FROM prd088_roles_to_expand);

-- Recreate the immutable RBAC guards with revision 23; the existing role-permission
-- transition guards remain valid because the template relation is now revision 23.
CREATE TRIGGER trg_rbac_permission_templates_insert_prd059 BEFORE INSERT ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059 BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059 BEFORE DELETE ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_insert_prd059 BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059 BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059 BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_permissions_owner_insert_prd059 BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates t WHERE t.permission_code=NEW.code AND t.category=NEW.category AND t.name=NEW.name AND t.description=NEW.description AND t.risk_level=NEW.risk_level AND t.catalog_revision=NEW.catalog_revision) OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.status!='active' OR EXISTS(SELECT 1 FROM permissions p WHERE p.permission_id=NEW.permission_id OR(p.tenant_id=NEW.tenant_id AND p.code=NEW.code) OR(NEW.rowid!=-1 AND p.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059 AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0 BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END OR EXISTS(SELECT 1 FROM roles p WHERE p.role_id=NEW.role_id OR(p.tenant_id=NEW.tenant_id AND p.code=NEW.code) OR(NEW.rowid!=-1 AND p.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,
    CASE c.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer' WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin' WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE c.role_code WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。' WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。' WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。' WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常、套餐额度和账单争议。' WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。' WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。' END,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE c.role_code WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,23,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

CREATE TEMP TABLE prd088_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd088_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM branding_configs)=(SELECT COUNT(*) FROM tenants)
    THEN 0 ELSE 1 END
 + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=98
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=23)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='fcda2ee936d9a04c1f8f469ef204ac9fdf04aa7c646a895e93d8b5d7dd2f0350'
    THEN 0 ELSE 1 END
 + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=254
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='07ff7275f31fa5cdd03a9f7f80e36692a71fca5e2af8022d0e765940dcc758f8'
    THEN 0 ELSE 1 END
 + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=23
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
      AND NOT EXISTS(SELECT 1 FROM tenants tenant WHERE(
        SELECT COUNT(*) FROM permissions permission
        WHERE permission.tenant_id=tenant.tenant_id)!=98)
    THEN 0 ELSE 1 END
 + CASE WHEN NOT EXISTS(SELECT 1 FROM prd088_roles_to_expand selected
      JOIN roles role ON role.role_id=selected.role_id
      WHERE role.matrix_sha256!=CASE role.code
        WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e'
        WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
        WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e'
        WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
        WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377'
        WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1'
      END) THEN 0 ELSE 1 END
 + CASE WHEN NOT EXISTS(SELECT 1 FROM prd088_roles_to_expand selected
      WHERE(SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=selected.tenant_id AND relation.role_id=selected.role_id
          AND permission.code IN('branding.read','branding.manage'))
        !=CASE selected.code WHEN 'admin' THEN 2 ELSE 1 END)
    THEN 0 ELSE 1 END
 + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table'
      AND name IN('branding_configs','branding_events','branding_event_outbox'))=3
    THEN 0 ELSE 1 END
 + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND name LIKE '%_prd088')=10 THEN 0 ELSE 1 END
 + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND name LIKE '%_prd088')=3 THEN 0 ELSE 1 END
 + CASE WHEN instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
      '''branding.updated''')>0
      AND instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
        '''branding_config''')>0 THEN 0 ELSE 1 END
 + (SELECT COUNT(*) FROM pragma_foreign_key_check)
 + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd088_final_guard;
DROP TABLE prd088_roles_to_expand;
COMMIT;
PRAGMA foreign_keys = ON;
