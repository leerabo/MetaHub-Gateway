-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd116_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd116_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0107' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'global_accounts','global_sessions','account_mfa_factors','account_mfa_attempt_guards',
      'workspace_memberships','workspace_sessions'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd116')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd116_up_guard;

-- Revision 27 adds conditional-access read/manage without widening customized matrices.
CREATE TEMP TABLE prd116_roles_to_expand AS
SELECT role_id,tenant_id,code FROM roles
WHERE code IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  AND matrix_sha256=CASE code
    WHEN 'admin' THEN '8cc3648fe99882833fd2cfef2b46fd76621f98a264501edae469591704e4d5a8'
    WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
    WHEN 'viewer' THEN 'e5953a49b667be67bd12d1ae8e94168f49a38644e2f81d2f8f64c655f3890e88'
    WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
    WHEN 'security_admin' THEN 'f31656fea127e6a06ddd65af47ceab41a9e6311acd5e3e461ce74412a9a95075'
    WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END;
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
CREATE TABLE rbac_permission_templates_prd116_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=27));
INSERT INTO rbac_permission_templates_prd116_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,27
FROM rbac_permission_templates;
INSERT INTO rbac_permission_templates_prd116_new VALUES
 ('conditional_access.read','identity','读取条件访问',
  '读取当前 Workspace 的条件访问规则、模拟结果和固定影响摘要。','read',27),
 ('conditional_access.manage','identity','管理条件访问',
  '模拟、修改并在安全前置条件满足后启用当前 Workspace 的条件访问策略。','critical',27);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd116_new RENAME TO rbac_permission_templates;
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted) VALUES
 ('admin','conditional_access.read',1),('admin','conditional_access.manage',1),
 ('viewer','conditional_access.read',1),
 ('security_admin','conditional_access.read',1),
 ('security_admin','conditional_access.manage',1);
CREATE TABLE permissions_prd116_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=27),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),UNIQUE(tenant_id,permission_id));
INSERT INTO permissions_prd116_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  27,status,created_at,updated_at FROM permissions;
INSERT INTO permissions_prd116_new
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,27,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code LIKE 'conditional_access.%';
DROP TABLE permissions;
ALTER TABLE permissions_prd116_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd116_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE (role.code='admin' AND permission.code LIKE 'conditional_access.%')
   OR (role.code='viewer' AND permission.code='conditional_access.read')
   OR (role.code='security_admin' AND permission.code LIKE 'conditional_access.%');
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=CASE code
  WHEN 'admin' THEN '89276198685d1c9db600730f0087ed6a24f7b79980493a8babd229922629155b'
  WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
  WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af'
  WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
  WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb'
  WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END
WHERE role_id IN(SELECT role_id FROM prd116_roles_to_expand);
DROP TABLE prd116_roles_to_expand;
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
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN '89276198685d1c9db600730f0087ed6a24f7b79980493a8babd229922629155b' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-116 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN '89276198685d1c9db600730f0087ed6a24f7b79980493a8babd229922629155b' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,27,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

-- Preserve operational attempt guards while replacing the single-factor PRD-095 table.
CREATE TEMP TABLE prd116_mfa_guard_copy AS SELECT * FROM account_mfa_attempt_guards;
DROP TABLE account_mfa_attempt_guards;
DROP INDEX uq_account_mfa_active_prd095;
DROP INDEX uq_account_mfa_pending_prd095;
CREATE TABLE account_mfa_factors_prd116_new(
  factor_id TEXT PRIMARY KEY CHECK(length(factor_id)=36 AND substr(factor_id,1,4)='mfa_'
    AND substr(factor_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  factor_type TEXT NOT NULL CHECK(factor_type IN('totp','webauthn')),
  secret_ciphertext TEXT CHECK(secret_ciphertext IS NULL
    OR length(secret_ciphertext) BETWEEN 80 AND 1024),
  secret_version INTEGER CHECK(secret_version IS NULL OR secret_version=1),
  status TEXT NOT NULL CHECK(status IN('pending','active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  last_accepted_counter INTEGER CHECK(last_accepted_counter IS NULL OR last_accepted_counter>=0),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  disabled_at TEXT CHECK(disabled_at IS NULL OR unixepoch(disabled_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,factor_id),
  CHECK((factor_type='totp' AND secret_ciphertext IS NOT NULL AND secret_version=1)
    OR(factor_type='webauthn' AND secret_ciphertext IS NULL AND secret_version IS NULL
      AND last_accepted_counter IS NULL)),
  CHECK((status='pending' AND verified_at IS NULL AND disabled_at IS NULL
      AND last_accepted_counter IS NULL)
    OR(status='active' AND verified_at IS NOT NULL AND disabled_at IS NULL
      AND((factor_type='totp' AND last_accepted_counter IS NOT NULL)
        OR factor_type='webauthn'))
    OR(status='disabled' AND disabled_at IS NOT NULL))
);
INSERT INTO account_mfa_factors_prd116_new
SELECT factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,revision,
  last_accepted_counter,verified_at,disabled_at,created_at,updated_at
FROM account_mfa_factors;
DROP TABLE account_mfa_factors;
ALTER TABLE account_mfa_factors_prd116_new RENAME TO account_mfa_factors;
CREATE UNIQUE INDEX uq_account_mfa_active_totp_prd116 ON account_mfa_factors(account_id)
  WHERE factor_type='totp' AND status='active';
CREATE UNIQUE INDEX uq_account_mfa_pending_type_prd116
  ON account_mfa_factors(account_id,factor_type) WHERE status='pending';
CREATE INDEX idx_account_mfa_active_prd116
  ON account_mfa_factors(account_id,status,factor_type,created_at);
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

ALTER TABLE global_sessions ADD COLUMN revoke_generation INTEGER NOT NULL DEFAULT 1
  CHECK(revoke_generation>=1);
ALTER TABLE global_sessions ADD COLUMN assurance_level TEXT NOT NULL DEFAULT 'password'
  CHECK(assurance_level IN('password','mfa','recovery'));
ALTER TABLE global_sessions ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'password'
  CHECK(auth_method IN('password','totp','webauthn','recovery_code'));
ALTER TABLE global_sessions ADD COLUMN trusted_device_id TEXT;
ALTER TABLE workspace_sessions ADD COLUMN revoke_generation INTEGER NOT NULL DEFAULT 1
  CHECK(revoke_generation>=1);
ALTER TABLE workspace_sessions ADD COLUMN assurance_level TEXT NOT NULL DEFAULT 'password'
  CHECK(assurance_level IN('password','mfa','recovery'));
ALTER TABLE workspace_sessions ADD COLUMN trusted_device_id TEXT;
ALTER TABLE workspace_sessions ADD COLUMN conditional_access_revision INTEGER NOT NULL DEFAULT 0
  CHECK(conditional_access_revision>=0);

CREATE TABLE webauthn_credentials(
  webauthn_credential_id TEXT PRIMARY KEY CHECK(length(webauthn_credential_id)=36
    AND substr(webauthn_credential_id,1,4)='wac_'
    AND substr(webauthn_credential_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  credential_id_hmac TEXT NOT NULL UNIQUE CHECK(length(credential_id_hmac)=64
    AND credential_id_hmac NOT GLOB '*[^0-9a-f]*'),
  public_key_cose_b64 TEXT NOT NULL CHECK(length(public_key_cose_b64) BETWEEN 80 AND 4096),
  aaguid TEXT CHECK(aaguid IS NULL OR length(aaguid) BETWEEN 32 AND 64),
  sign_count INTEGER NOT NULL DEFAULT 0 CHECK(sign_count>=0),
  transports_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(transports_json)
    AND json_type(transports_json)='array' AND length(transports_json)<=512),
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id,factor_id) REFERENCES account_mfa_factors(account_id,factor_id),
  UNIQUE(webauthn_credential_id,account_id,factor_id),
  CHECK((status='active' AND revoked_at IS NULL)
    OR(status='revoked' AND revoked_at IS NOT NULL))
);
CREATE INDEX idx_webauthn_account_prd116
  ON webauthn_credentials(account_id,status,created_at,webauthn_credential_id);

CREATE TABLE mfa_challenges(
  mfa_challenge_id TEXT PRIMARY KEY CHECK(length(mfa_challenge_id)=36
    AND substr(mfa_challenge_id,1,4)='mfc_'
    AND substr(mfa_challenge_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  global_session_id TEXT NOT NULL,
  workspace_session_id TEXT,
  purpose TEXT NOT NULL CHECK(purpose IN(
    'webauthn_registration','webauthn_authentication','recovery','step_up')),
  method TEXT NOT NULL CHECK(method IN('totp','webauthn','recovery_code')),
  challenge_hmac TEXT NOT NULL UNIQUE CHECK(length(challenge_hmac)=64
    AND challenge_hmac NOT GLOB '*[^0-9a-f]*'),
  browser_binding_hmac TEXT NOT NULL CHECK(length(browser_binding_hmac)=64
    AND browser_binding_hmac NOT GLOB '*[^0-9a-f]*'),
  rp_id TEXT CHECK(rp_id IS NULL OR length(rp_id) BETWEEN 1 AND 253),
  origin TEXT CHECK(origin IS NULL OR length(origin) BETWEEN 8 AND 512),
  status TEXT NOT NULL CHECK(status IN('pending','consumed','expired','exhausted')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 8),
  ready_at TEXT NOT NULL CHECK(unixepoch(ready_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id,global_session_id) REFERENCES global_sessions(account_id,session_id),
  FOREIGN KEY(workspace_session_id) REFERENCES workspace_sessions(workspace_session_id),
  CHECK((status='consumed' AND consumed_at IS NOT NULL)
    OR(status!='consumed' AND consumed_at IS NULL)),
  CHECK(unixepoch(ready_at)<=unixepoch(expires_at)),
  CHECK((method='webauthn' AND rp_id IS NOT NULL AND origin IS NOT NULL)
    OR(method!='webauthn' AND rp_id IS NULL AND origin IS NULL))
);
CREATE INDEX idx_mfa_challenges_owner_prd116
  ON mfa_challenges(account_id,global_session_id,purpose,status,expires_at);

CREATE TABLE recovery_code_sets(
  recovery_code_set_id TEXT PRIMARY KEY CHECK(length(recovery_code_set_id)=36
    AND substr(recovery_code_set_id,1,4)='rcs_'
    AND substr(recovery_code_set_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','superseded','exhausted','revoked')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  generated_count INTEGER NOT NULL CHECK(generated_count BETWEEN 6 AND 20),
  consumed_count INTEGER NOT NULL DEFAULT 0 CHECK(consumed_count>=0
    AND consumed_count<=generated_count),
  generated_at TEXT NOT NULL CHECK(unixepoch(generated_at) IS NOT NULL),
  invalidated_at TEXT CHECK(invalidated_at IS NULL OR unixepoch(invalidated_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(recovery_code_set_id,account_id),
  CHECK((status='active' AND invalidated_at IS NULL AND consumed_count<generated_count)
    OR(status!='active' AND invalidated_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_recovery_code_set_active_prd116 ON recovery_code_sets(account_id)
  WHERE status='active';
CREATE TABLE recovery_codes(
  recovery_code_id TEXT PRIMARY KEY CHECK(length(recovery_code_id)=36
    AND substr(recovery_code_id,1,4)='rcd_'
    AND substr(recovery_code_id,5) NOT GLOB '*[^0-9a-f]*'),
  recovery_code_set_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  code_hash TEXT NOT NULL CHECK(length(code_hash) BETWEEN 60 AND 255),
  status TEXT NOT NULL CHECK(status IN('active','consumed','revoked')),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(recovery_code_set_id,account_id)
    REFERENCES recovery_code_sets(recovery_code_set_id,account_id),
  UNIQUE(recovery_code_set_id,code_hash),
  CHECK((status='consumed' AND consumed_at IS NOT NULL)
    OR(status!='consumed' AND consumed_at IS NULL))
);
CREATE INDEX idx_recovery_codes_set_prd116
  ON recovery_codes(recovery_code_set_id,status,recovery_code_id);

CREATE TABLE trusted_devices(
  trusted_device_id TEXT PRIMARY KEY CHECK(length(trusted_device_id)=36
    AND substr(trusted_device_id,1,4)='trd_'
    AND substr(trusted_device_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  device_hmac TEXT NOT NULL CHECK(length(device_hmac)=64
    AND device_hmac NOT GLOB '*[^0-9a-f]*'),
  label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 80),
  device_family TEXT NOT NULL CHECK(device_family IN(
    'chrome','edge','firefox','safari','mobile','unknown')),
  trust_level TEXT NOT NULL CHECK(trust_level='mfa_verified'),
  status TEXT NOT NULL CHECK(status IN('active','revoked','expired')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,device_hmac),UNIQUE(trusted_device_id,account_id),
  CHECK((status='active' AND revoked_at IS NULL)
    OR(status!='active' AND revoked_at IS NOT NULL))
);
CREATE INDEX idx_trusted_devices_account_prd116
  ON trusted_devices(account_id,status,expires_at,trusted_device_id);

CREATE TABLE conditional_access_policies(
  conditional_access_policy_id TEXT PRIMARY KEY CHECK(length(conditional_access_policy_id)=36
    AND substr(conditional_access_policy_id,1,4)='cap_'
    AND substr(conditional_access_policy_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN('audit','enforced')),
  require_mfa INTEGER NOT NULL CHECK(require_mfa IN(0,1)),
  require_step_up_for_high_risk INTEGER NOT NULL CHECK(require_step_up_for_high_risk IN(0,1)),
  allow_trusted_devices INTEGER NOT NULL CHECK(allow_trusted_devices IN(0,1)),
  region_rule TEXT NOT NULL CHECK(region_rule IN('any','domestic_only')),
  network_rule TEXT NOT NULL CHECK(network_rule IN('any','trusted_only')),
  allowed_cidrs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(allowed_cidrs_json)
    AND json_type(allowed_cidrs_json)='array' AND length(allowed_cidrs_json)<=4096),
  exception_membership_ids_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(exception_membership_ids_json)
      AND json_type(exception_membership_ids_json)='array'
      AND length(exception_membership_ids_json)<=4096),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  simulation_watermark_at TEXT CHECK(simulation_watermark_at IS NULL
    OR unixepoch(simulation_watermark_at) IS NOT NULL),
  updated_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id),UNIQUE(conditional_access_policy_id,workspace_id,tenant_id)
);
CREATE TABLE conditional_access_simulations(
  conditional_access_simulation_id TEXT PRIMARY KEY CHECK(
    length(conditional_access_simulation_id)=36
    AND substr(conditional_access_simulation_id,1,4)='cas_'
    AND substr(conditional_access_simulation_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
  policy_revision INTEGER NOT NULL CHECK(policy_revision>=1),
  outcome_code TEXT NOT NULL CHECK(outcome_code IN('simulation.ready','simulation.owner_blocked')),
  affected_session_count INTEGER NOT NULL CHECK(affected_session_count>=0),
  owner_ready INTEGER NOT NULL CHECK(owner_ready IN(0,1)),
  simulated_by_account_id TEXT NOT NULL,
  simulated_at TEXT NOT NULL CHECK(unixepoch(simulated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(simulated_by_account_id) REFERENCES global_accounts(account_id)
);
CREATE INDEX idx_conditional_simulation_prd116
  ON conditional_access_simulations(workspace_id,simulated_at DESC);

CREATE TABLE step_up_grants(
  step_up_grant_id TEXT PRIMARY KEY CHECK(length(step_up_grant_id)=36
    AND substr(step_up_grant_id,1,4)='sug_'
    AND substr(step_up_grant_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,global_session_id TEXT NOT NULL,workspace_session_id TEXT,
  workspace_id TEXT,tenant_id TEXT,
  grant_hmac TEXT NOT NULL UNIQUE CHECK(length(grant_hmac)=64
    AND grant_hmac NOT GLOB '*[^0-9a-f]*'),
  action_code TEXT NOT NULL CHECK(length(action_code) BETWEEN 3 AND 80
    AND action_code=lower(action_code) AND action_code NOT GLOB '*[^a-z0-9._]*'),
  resource_type TEXT NOT NULL CHECK(length(resource_type) BETWEEN 2 AND 60
    AND resource_type=lower(resource_type) AND resource_type NOT GLOB '*[^a-z0-9._]*'),
  resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 120),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64
    AND permission_fingerprint NOT GLOB '*[^0-9a-f]*'),
  auth_method TEXT NOT NULL CHECK(auth_method IN('totp','webauthn','recovery_code')),
  assurance_level TEXT NOT NULL CHECK(assurance_level IN('mfa','recovery')),
  status TEXT NOT NULL CHECK(status IN('active','consumed','expired','revoked')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id,global_session_id) REFERENCES global_sessions(account_id,session_id),
  FOREIGN KEY(workspace_session_id) REFERENCES workspace_sessions(workspace_session_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((workspace_id IS NULL AND tenant_id IS NULL AND workspace_session_id IS NULL)
    OR(workspace_id IS NOT NULL AND tenant_id IS NOT NULL)),
  CHECK((status='consumed' AND consumed_at IS NOT NULL)
    OR(status!='consumed' AND consumed_at IS NULL))
);
CREATE INDEX idx_step_up_owner_prd116
  ON step_up_grants(account_id,global_session_id,status,expires_at);

CREATE TABLE identity_security_events(
  identity_security_event_id TEXT PRIMARY KEY CHECK(length(identity_security_event_id)=36
    AND substr(identity_security_event_id,1,4)='ise_'
    AND substr(identity_security_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT,workspace_id TEXT,tenant_id TEXT,actor_account_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'identity.mfa_changed','identity.session_revoked','conditional_access.denied',
    'step_up.completed')),
  subject_type TEXT NOT NULL CHECK(subject_type IN(
    'factor','session','device','policy','grant')),
  subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 3 AND 120),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((workspace_id IS NULL AND tenant_id IS NULL)
    OR(workspace_id IS NOT NULL AND tenant_id IS NOT NULL))
);
CREATE TABLE identity_security_event_outbox(
  identity_security_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(identity_security_event_outbox_id)=36
    AND substr(identity_security_event_outbox_id,1,4)='iso_'
    AND substr(identity_security_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  identity_security_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 100),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(identity_security_event_id)
    REFERENCES identity_security_events(identity_security_event_id)
);
CREATE TRIGGER trg_identity_security_events_update_prd116
BEFORE UPDATE ON identity_security_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_security_event_immutable'); END;
CREATE TRIGGER trg_identity_security_events_delete_prd116
BEFORE DELETE ON identity_security_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_security_event_immutable'); END;

CREATE TEMP TABLE prd116_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd116_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'webauthn_credentials','mfa_challenges','recovery_code_sets','recovery_codes',
    'trusted_devices','conditional_access_policies','conditional_access_simulations',
    'step_up_grants','identity_security_events','identity_security_event_outbox'))=10
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('global_sessions') WHERE name IN(
      'revoke_generation','assurance_level','auth_method','trusted_device_id'))=4
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('workspace_sessions') WHERE name IN(
      'revoke_generation','assurance_level','trusted_device_id','conditional_access_revision'))=4
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=108
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=274
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=27)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd116_up_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
