-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd116_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd116_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0108' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM webauthn_credentials)
  + (SELECT COUNT(*) FROM mfa_challenges)
  + (SELECT COUNT(*) FROM recovery_code_sets)
  + (SELECT COUNT(*) FROM recovery_codes)
  + (SELECT COUNT(*) FROM trusted_devices)
  + (SELECT COUNT(*) FROM conditional_access_policies)
  + (SELECT COUNT(*) FROM conditional_access_simulations)
  + (SELECT COUNT(*) FROM step_up_grants)
  + (SELECT COUNT(*) FROM identity_security_events)
  + (SELECT COUNT(*) FROM identity_security_event_outbox)
  + (SELECT COUNT(*) FROM account_mfa_factors WHERE factor_type!='totp')
  + (SELECT COUNT(*) FROM global_sessions WHERE revoke_generation!=1
      OR assurance_level!='password' OR auth_method!='password' OR trusted_device_id IS NOT NULL)
  + (SELECT COUNT(*) FROM workspace_sessions WHERE revoke_generation!=1
      OR assurance_level!='password' OR trusted_device_id IS NOT NULL
      OR conditional_access_revision!=0)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd116_down_guard;

CREATE TEMP TABLE prd116_rbac_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd116_rbac_down_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=108
    AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=274
    AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=27)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM role_permissions relation
      JOIN permissions permission ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      JOIN roles role ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
      WHERE permission.code LIKE 'conditional_access.%'
        AND role.matrix_sha256 NOT IN(
          '89276198685d1c9db600730f0087ed6a24f7b79980493a8babd229922629155b',
          'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af',
          '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb'));
DROP TABLE prd116_rbac_down_guard;
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
  SELECT permission_id FROM permissions WHERE code LIKE 'conditional_access.%');
DELETE FROM permissions WHERE code LIKE 'conditional_access.%';
DELETE FROM rbac_role_permission_templates WHERE permission_code LIKE 'conditional_access.%';
CREATE TABLE rbac_permission_templates_prd116_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=26));
INSERT INTO rbac_permission_templates_prd116_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,26
FROM rbac_permission_templates WHERE permission_code NOT LIKE 'conditional_access.%';
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd116_old RENAME TO rbac_permission_templates;
CREATE TABLE permissions_prd116_old(
  permission_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,code TEXT NOT NULL,
  category TEXT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=26),
  status TEXT NOT NULL CHECK(status='active'),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),UNIQUE(tenant_id,permission_id));
INSERT INTO permissions_prd116_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  26,status,created_at,updated_at FROM permissions;
DROP TABLE permissions;
ALTER TABLE permissions_prd116_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=CASE code
  WHEN 'admin' THEN '8cc3648fe99882833fd2cfef2b46fd76621f98a264501edae469591704e4d5a8'
  WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
  WHEN 'viewer' THEN 'e5953a49b667be67bd12d1ae8e94168f49a38644e2f81d2f8f64c655f3890e88'
  WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
  WHEN 'security_admin' THEN 'f31656fea127e6a06ddd65af47ceab41a9e6311acd5e3e461ce74412a9a95075'
  WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END
WHERE matrix_sha256 IN(
  '89276198685d1c9db600730f0087ed6a24f7b79980493a8babd229922629155b',
  '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686',
  'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af',
  '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc',
  '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb',
  'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1');
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
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN '8cc3648fe99882833fd2cfef2b46fd76621f98a264501edae469591704e4d5a8' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'e5953a49b667be67bd12d1ae8e94168f49a38644e2f81d2f8f64c655f3890e88' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'f31656fea127e6a06ddd65af47ceab41a9e6311acd5e3e461ce74412a9a95075' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-115 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN '8cc3648fe99882833fd2cfef2b46fd76621f98a264501edae469591704e4d5a8' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'e5953a49b667be67bd12d1ae8e94168f49a38644e2f81d2f8f64c655f3890e88' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'f31656fea127e6a06ddd65af47ceab41a9e6311acd5e3e461ce74412a9a95075' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,26,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

DROP TRIGGER trg_identity_security_events_delete_prd116;
DROP TRIGGER trg_identity_security_events_update_prd116;
DROP TABLE identity_security_event_outbox;
DROP TABLE identity_security_events;
DROP TABLE step_up_grants;
DROP TABLE conditional_access_simulations;
DROP TABLE conditional_access_policies;
DROP TABLE trusted_devices;
DROP TABLE recovery_codes;
DROP TABLE recovery_code_sets;
DROP TABLE mfa_challenges;
DROP TABLE webauthn_credentials;

CREATE TEMP TABLE prd116_mfa_guard_copy AS SELECT * FROM account_mfa_attempt_guards;
DROP TABLE account_mfa_attempt_guards;
DROP INDEX uq_account_mfa_active_totp_prd116;
DROP INDEX uq_account_mfa_pending_type_prd116;
DROP INDEX idx_account_mfa_active_prd116;
CREATE TABLE account_mfa_factors_prd116_old(
  factor_id TEXT PRIMARY KEY CHECK(length(factor_id)=36 AND substr(factor_id,1,4)='mfa_'
    AND substr(factor_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  factor_type TEXT NOT NULL CHECK(factor_type='totp'),
  secret_ciphertext TEXT NOT NULL CHECK(length(secret_ciphertext) BETWEEN 80 AND 1024),
  secret_version INTEGER NOT NULL CHECK(secret_version=1),
  status TEXT NOT NULL CHECK(status IN('pending','active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  last_accepted_counter INTEGER CHECK(last_accepted_counter IS NULL OR last_accepted_counter>=0),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  disabled_at TEXT CHECK(disabled_at IS NULL OR unixepoch(disabled_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,factor_id),
  CHECK((status='pending' AND verified_at IS NULL AND disabled_at IS NULL
      AND last_accepted_counter IS NULL)
    OR(status='active' AND verified_at IS NOT NULL AND disabled_at IS NULL
      AND last_accepted_counter IS NOT NULL)
    OR(status='disabled' AND disabled_at IS NOT NULL))
);
INSERT INTO account_mfa_factors_prd116_old
SELECT factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,revision,
  last_accepted_counter,verified_at,disabled_at,created_at,updated_at
FROM account_mfa_factors;
DROP TABLE account_mfa_factors;
ALTER TABLE account_mfa_factors_prd116_old RENAME TO account_mfa_factors;
CREATE UNIQUE INDEX uq_account_mfa_active_prd095 ON account_mfa_factors(account_id)
  WHERE status='active';
CREATE UNIQUE INDEX uq_account_mfa_pending_prd095 ON account_mfa_factors(account_id)
  WHERE status='pending';
CREATE TABLE account_mfa_attempt_guards(
  account_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  device_hmac TEXT NOT NULL CHECK(length(device_hmac)=64
    AND device_hmac NOT GLOB '*[^0-9a-f]*'),
  failed_count INTEGER NOT NULL CHECK(failed_count BETWEEN 0 AND 20),
  locked_until TEXT CHECK(locked_until IS NULL OR unixepoch(locked_until) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(account_id,factor_id,device_hmac),
  FOREIGN KEY(account_id,factor_id) REFERENCES account_mfa_factors(account_id,factor_id)
);
INSERT INTO account_mfa_attempt_guards SELECT * FROM prd116_mfa_guard_copy;
DROP TABLE prd116_mfa_guard_copy;

ALTER TABLE workspace_sessions DROP COLUMN conditional_access_revision;
ALTER TABLE workspace_sessions DROP COLUMN trusted_device_id;
ALTER TABLE workspace_sessions DROP COLUMN assurance_level;
ALTER TABLE workspace_sessions DROP COLUMN revoke_generation;
ALTER TABLE global_sessions DROP COLUMN trusted_device_id;
ALTER TABLE global_sessions DROP COLUMN auth_method;
ALTER TABLE global_sessions DROP COLUMN assurance_level;
ALTER TABLE global_sessions DROP COLUMN revoke_generation;

CREATE TEMP TABLE prd116_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd116_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'webauthn_credentials','mfa_challenges','recovery_code_sets','recovery_codes',
    'trusted_devices','conditional_access_policies','conditional_access_simulations',
    'step_up_grants','identity_security_events','identity_security_event_outbox'))=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('global_sessions') WHERE name IN(
      'revoke_generation','assurance_level','auth_method','trusted_device_id'))=0
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('workspace_sessions') WHERE name IN(
      'revoke_generation','assurance_level','trusted_device_id','conditional_access_revision'))=0
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=106
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=269
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=26)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd116_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
