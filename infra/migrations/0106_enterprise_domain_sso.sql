-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 升级只允许从 PRD-113 权威头执行，并拒绝损坏或残留的半迁移结构。
CREATE TEMP TABLE prd114_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd114_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0105' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'global_accounts','global_sessions','workspaces','workspace_tenant_bindings',
      'workspace_memberships','workspace_sessions','account_mfa_factors'))=7 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd114')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd114_up_guard;

-- Revision 25 adds enterprise SSO permissions while preserving customized role matrices.
CREATE TEMP TABLE prd114_roles_to_expand AS
SELECT role_id,tenant_id,code FROM roles
WHERE code IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  AND matrix_sha256=CASE code
    WHEN 'admin' THEN 'd64ef0db03b06138b5a5b5d4652ccdfcad2c79b420b3ce04fd5772eeebbf0ab1'
    WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
    WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e'
    WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
    WHEN 'security_admin' THEN 'f0f642cea1be202d82016ac045332c729acd04060be8e5de68b32a597d57716b'
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
CREATE TABLE rbac_permission_templates_prd114_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=25));
INSERT INTO rbac_permission_templates_prd114_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,25
FROM rbac_permission_templates;
INSERT INTO rbac_permission_templates_prd114_new VALUES
 ('enterprise_sso.read','identity','读取企业 SSO',
  '读取当前 Workspace 的域名、连接、证书窗口和强制登录安全摘要。','read',25),
 ('enterprise_sso.manage','identity','管理企业 SSO',
  '创建、测试和维护当前 Workspace 的域名、身份连接与强制登录策略。','critical',25);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd114_new RENAME TO rbac_permission_templates;
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted) VALUES
 ('admin','enterprise_sso.read',1),
 ('admin','enterprise_sso.manage',1),
 ('viewer','enterprise_sso.read',1),
 ('security_admin','enterprise_sso.read',1),
 ('security_admin','enterprise_sso.manage',1);
CREATE TABLE permissions_prd114_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=25),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id));
INSERT INTO permissions_prd114_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  25,status,created_at,updated_at FROM permissions;
INSERT INTO permissions_prd114_new
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,25,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code LIKE 'enterprise_sso.%';
DROP TABLE permissions;
ALTER TABLE permissions_prd114_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd114_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE (role.code='admin' AND permission.code LIKE 'enterprise_sso.%')
   OR (role.code='viewer' AND permission.code='enterprise_sso.read')
   OR (role.code='security_admin' AND permission.code LIKE 'enterprise_sso.%');
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=CASE code
  WHEN 'admin' THEN 'e8bcc0a129f58b94fa56a46536a516eb984a2e97de914fdbb5670a79fa2840f2'
  WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
  WHEN 'viewer' THEN 'a981f2117c5083af9dd8e7cc16e9cf03b08f1b2e7bc972e8e0a0f5619e21bf19'
  WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
  WHEN 'security_admin' THEN 'c1c66bd7fea0e3fc2566a03cf41896a885bbf56db574ddbc1ef56244b456923e'
  WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END
WHERE role_id IN(SELECT role_id FROM prd114_roles_to_expand);
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
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN 'e8bcc0a129f58b94fa56a46536a516eb984a2e97de914fdbb5670a79fa2840f2' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'a981f2117c5083af9dd8e7cc16e9cf03b08f1b2e7bc972e8e0a0f5619e21bf19' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'c1c66bd7fea0e3fc2566a03cf41896a885bbf56db574ddbc1ef56244b456923e' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-114 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN 'e8bcc0a129f58b94fa56a46536a516eb984a2e97de914fdbb5670a79fa2840f2' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'a981f2117c5083af9dd8e7cc16e9cf03b08f1b2e7bc972e8e0a0f5619e21bf19' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'c1c66bd7fea0e3fc2566a03cf41896a885bbf56db574ddbc1ef56244b456923e' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,25,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

-- 域名是全局唯一信任入口；challenge 只保存摘要，释放或转移不能靠删除完成。
CREATE TABLE verified_domains(
  verified_domain_id TEXT PRIMARY KEY CHECK(length(verified_domain_id)=36
    AND substr(verified_domain_id,1,4)='vdm_'
    AND substr(verified_domain_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  domain_normalized TEXT NOT NULL UNIQUE CHECK(length(domain_normalized) BETWEEN 3 AND 253
    AND domain_normalized=lower(domain_normalized)
    AND domain_normalized NOT GLOB '*[^a-z0-9.-]*'
    AND substr(domain_normalized,1,1)!='.' AND substr(domain_normalized,-1,1)!='.'),
  domain_hmac TEXT NOT NULL UNIQUE CHECK(length(domain_hmac)=64
    AND domain_hmac NOT GLOB '*[^0-9a-f]*'),
  challenge_hash TEXT NOT NULL CHECK(length(challenge_hash)=64
    AND challenge_hash NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','verified','suspended')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  challenge_expires_at TEXT NOT NULL CHECK(unixepoch(challenge_expires_at) IS NOT NULL),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  suspended_at TEXT CHECK(suspended_at IS NULL OR unixepoch(suspended_at) IS NOT NULL),
  created_by_account_id TEXT NOT NULL,
  updated_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(verified_domain_id,workspace_id,tenant_id),
  CHECK((status='pending' AND revision=1 AND verified_at IS NULL AND suspended_at IS NULL)
    OR(status='verified' AND revision>=2 AND verified_at IS NOT NULL AND suspended_at IS NULL)
    OR(status='suspended' AND revision>=2 AND suspended_at IS NOT NULL))
);
CREATE INDEX idx_verified_domains_owner_prd114
  ON verified_domains(workspace_id,tenant_id,status,updated_at,verified_domain_id);
CREATE TRIGGER trg_verified_domains_update_prd114
BEFORE UPDATE ON verified_domains FOR EACH ROW
WHEN NEW.verified_domain_id!=OLD.verified_domain_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.domain_normalized!=OLD.domain_normalized OR NEW.domain_hmac!=OLD.domain_hmac
  OR NEW.challenge_hash!=OLD.challenge_hash
  OR NEW.challenge_expires_at!=OLD.challenge_expires_at
  OR NEW.created_by_account_id!=OLD.created_by_account_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='pending' AND NEW.status IN('verified','suspended'))
    OR(OLD.status='verified' AND NEW.status='suspended'))
BEGIN SELECT RAISE(ABORT,'verified_domain_transition_invalid'); END;
CREATE TRIGGER trg_verified_domains_delete_prd114 BEFORE DELETE ON verified_domains
BEGIN SELECT RAISE(ABORT,'verified_domain_immutable'); END;

-- 连接只保存固定协议配置和独立密文；assertion、authorization code 与 Token 永不落表。
CREATE TABLE sso_connections(
  sso_connection_id TEXT PRIMARY KEY CHECK(length(sso_connection_id)=36
    AND substr(sso_connection_id,1,4)='ssc_'
    AND substr(sso_connection_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  verified_domain_id TEXT NOT NULL,
  connection_code TEXT NOT NULL CHECK(length(connection_code) BETWEEN 3 AND 64
    AND connection_code=lower(connection_code)
    AND connection_code NOT GLOB '*[^a-z0-9-]*'),
  display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 120),
  protocol TEXT NOT NULL CHECK(protocol IN('oidc','saml')),
  issuer TEXT NOT NULL CHECK(length(issuer) BETWEEN 3 AND 512),
  public_config_json TEXT NOT NULL CHECK(json_valid(public_config_json)
    AND length(public_config_json) BETWEEN 2 AND 32768),
  client_secret_ciphertext TEXT CHECK(client_secret_ciphertext IS NULL
    OR length(client_secret_ciphertext) BETWEEN 80 AND 2048),
  secret_version INTEGER CHECK(secret_version IS NULL OR secret_version=1),
  metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64
    AND metadata_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('draft','testing','active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  tested_at TEXT CHECK(tested_at IS NULL OR unixepoch(tested_at) IS NOT NULL),
  disabled_at TEXT CHECK(disabled_at IS NULL OR unixepoch(disabled_at) IS NOT NULL),
  created_by_account_id TEXT NOT NULL,
  updated_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(verified_domain_id,workspace_id,tenant_id)
    REFERENCES verified_domains(verified_domain_id,workspace_id,tenant_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,connection_code),
  UNIQUE(sso_connection_id,workspace_id,tenant_id),
  CHECK((protocol='oidc' AND client_secret_ciphertext IS NOT NULL AND secret_version=1)
    OR(protocol='saml' AND client_secret_ciphertext IS NULL AND secret_version IS NULL)),
  CHECK((status IN('draft','testing') AND disabled_at IS NULL)
    OR(status='active' AND tested_at IS NOT NULL AND disabled_at IS NULL)
    OR(status='disabled' AND disabled_at IS NOT NULL))
);
CREATE INDEX idx_sso_connections_owner_prd114
  ON sso_connections(workspace_id,tenant_id,status,updated_at,sso_connection_id);
CREATE TRIGGER trg_sso_connections_update_prd114
BEFORE UPDATE ON sso_connections FOR EACH ROW
WHEN NEW.sso_connection_id!=OLD.sso_connection_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.verified_domain_id!=OLD.verified_domain_id
  OR NEW.connection_code!=OLD.connection_code OR NEW.protocol!=OLD.protocol
  OR NEW.created_by_account_id!=OLD.created_by_account_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='draft' AND NEW.status IN('draft','testing','disabled'))
    OR(OLD.status='testing' AND NEW.status IN('testing','active','disabled'))
    OR(OLD.status='active' AND NEW.status IN('active','testing','disabled')))
BEGIN SELECT RAISE(ABORT,'sso_connection_transition_invalid'); END;
CREATE TRIGGER trg_sso_connections_delete_prd114 BEFORE DELETE ON sso_connections
BEGIN SELECT RAISE(ABORT,'sso_connection_immutable'); END;

-- SAML 验签和 OIDC 轮换公钥使用显式证书窗口，旧证书只退休不覆盖。
CREATE TABLE sso_certificates(
  sso_certificate_id TEXT PRIMARY KEY CHECK(length(sso_certificate_id)=36
    AND substr(sso_certificate_id,1,4)='sct_'
    AND substr(sso_certificate_id,5) NOT GLOB '*[^0-9a-f]*'),
  sso_connection_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN('saml_signing','oidc_jwk')),
  fingerprint_sha256 TEXT NOT NULL CHECK(length(fingerprint_sha256)=64
    AND fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'),
  public_material TEXT NOT NULL CHECK(length(public_material) BETWEEN 32 AND 32768),
  status TEXT NOT NULL CHECK(status IN('staged','active','retired','expired')),
  not_before TEXT NOT NULL CHECK(unixepoch(not_before) IS NOT NULL),
  not_after TEXT NOT NULL CHECK(unixepoch(not_after) IS NOT NULL),
  activated_at TEXT CHECK(activated_at IS NULL OR unixepoch(activated_at) IS NOT NULL),
  retired_at TEXT CHECK(retired_at IS NULL OR unixepoch(retired_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(sso_connection_id,workspace_id,tenant_id)
    REFERENCES sso_connections(sso_connection_id,workspace_id,tenant_id),
  UNIQUE(sso_connection_id,fingerprint_sha256),
  CHECK(unixepoch(not_after)>unixepoch(not_before)),
  CHECK((status='staged' AND activated_at IS NULL AND retired_at IS NULL)
    OR(status='active' AND activated_at IS NOT NULL AND retired_at IS NULL)
    OR(status IN('retired','expired') AND retired_at IS NOT NULL))
);
CREATE INDEX idx_sso_certificates_window_prd114
  ON sso_certificates(sso_connection_id,status,not_after,sso_certificate_id);
CREATE TRIGGER trg_sso_certificates_update_prd114
BEFORE UPDATE ON sso_certificates FOR EACH ROW
WHEN NEW.sso_certificate_id!=OLD.sso_certificate_id
  OR NEW.sso_connection_id!=OLD.sso_connection_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.purpose!=OLD.purpose OR NEW.fingerprint_sha256!=OLD.fingerprint_sha256
  OR NEW.public_material!=OLD.public_material OR NEW.not_before!=OLD.not_before
  OR NEW.not_after!=OLD.not_after OR NEW.created_at!=OLD.created_at
  OR NOT((OLD.status='staged' AND NEW.status IN('active','retired','expired'))
    OR(OLD.status='active' AND NEW.status IN('retired','expired')))
BEGIN SELECT RAISE(ABORT,'sso_certificate_transition_invalid'); END;
CREATE TRIGGER trg_sso_certificates_delete_prd114 BEFORE DELETE ON sso_certificates
BEGIN SELECT RAISE(ABORT,'sso_certificate_immutable'); END;

-- Workspace 单例策略把强制 SSO 与明确连接、Owner break-glass 绑定在同一 owner 图中。
CREATE TABLE sso_policies(
  sso_policy_id TEXT PRIMARY KEY CHECK(length(sso_policy_id)=36
    AND substr(sso_policy_id,1,4)='ssp_'
    AND substr(sso_policy_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL CHECK(mode IN('optional','enforced')),
  active_connection_id TEXT,
  break_glass_account_id TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(active_connection_id,workspace_id,tenant_id)
    REFERENCES sso_connections(sso_connection_id,workspace_id,tenant_id),
  FOREIGN KEY(break_glass_account_id) REFERENCES global_accounts(account_id),
  CHECK((mode='optional') OR(active_connection_id IS NOT NULL
    AND break_glass_account_id IS NOT NULL))
);
CREATE TRIGGER trg_sso_policies_update_prd114
BEFORE UPDATE ON sso_policies FOR EACH ROW
WHEN NEW.sso_policy_id!=OLD.sso_policy_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'sso_policy_transition_invalid'); END;
CREATE TRIGGER trg_sso_policies_delete_prd114 BEFORE DELETE ON sso_policies
BEGIN SELECT RAISE(ABORT,'sso_policy_immutable'); END;

-- A login grant proves how one exact WorkspaceSession entered an enforced Workspace.
-- Recovery grants are short-lived and single-use; federated grants are recorded consumed.
CREATE TABLE enterprise_identity_session_grants(
  enterprise_identity_session_grant_id TEXT PRIMARY KEY CHECK(
    length(enterprise_identity_session_grant_id)=36
    AND substr(enterprise_identity_session_grant_id,1,4)='esg_'
    AND substr(enterprise_identity_session_grant_id,5) NOT GLOB '*[^0-9a-f]*'),
  global_session_id TEXT NOT NULL,
  workspace_session_id TEXT UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  grant_type TEXT NOT NULL CHECK(grant_type IN('federated','recovery')),
  sso_connection_id TEXT,
  reason_code TEXT CHECK(reason_code IS NULL OR reason_code IN(
    'idp_unavailable','identity_recovery')),
  status TEXT NOT NULL CHECK(status IN('active','consumed','expired')),
  revision INTEGER NOT NULL CHECK(revision IN(1,2)),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(global_session_id) REFERENCES global_sessions(session_id),
  FOREIGN KEY(workspace_session_id) REFERENCES workspace_sessions(workspace_session_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(sso_connection_id,workspace_id,tenant_id)
    REFERENCES sso_connections(sso_connection_id,workspace_id,tenant_id),
  UNIQUE(global_session_id,workspace_id),
  CHECK((grant_type='federated' AND sso_connection_id IS NOT NULL AND reason_code IS NULL)
    OR(grant_type='recovery' AND sso_connection_id IS NULL AND reason_code IS NOT NULL)),
  CHECK((status='active' AND grant_type='recovery' AND revision=1
      AND workspace_session_id IS NULL AND consumed_at IS NULL)
    OR(status='consumed' AND revision IN(1,2)
      AND workspace_session_id IS NOT NULL AND consumed_at IS NOT NULL)
    OR(status='expired' AND revision=2
      AND workspace_session_id IS NULL AND consumed_at IS NOT NULL))
);
CREATE INDEX idx_enterprise_identity_session_grants_prd114
  ON enterprise_identity_session_grants(
    workspace_id,account_id,status,expires_at,enterprise_identity_session_grant_id);
CREATE TRIGGER trg_enterprise_identity_session_grants_update_prd114
BEFORE UPDATE ON enterprise_identity_session_grants FOR EACH ROW
WHEN NEW.enterprise_identity_session_grant_id!=OLD.enterprise_identity_session_grant_id
  OR NEW.global_session_id!=OLD.global_session_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.grant_type!=OLD.grant_type
  OR NEW.sso_connection_id IS NOT OLD.sso_connection_id
  OR NEW.reason_code IS NOT OLD.reason_code OR NEW.expires_at!=OLD.expires_at
  OR NEW.created_at!=OLD.created_at OR OLD.status!='active'
  OR NEW.status NOT IN('consumed','expired') OR NEW.revision!=2
  OR NEW.consumed_at IS NULL OR NEW.updated_at<=OLD.updated_at
  OR (NEW.status='consumed' AND NEW.workspace_session_id IS NULL)
  OR (NEW.status='expired' AND NEW.workspace_session_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'enterprise_identity_session_grant_transition_invalid'); END;
CREATE TRIGGER trg_enterprise_identity_session_grants_delete_prd114
BEFORE DELETE ON enterprise_identity_session_grants
BEGIN SELECT RAISE(ABORT,'enterprise_identity_session_grant_immutable'); END;

-- 外部 subject 只保存 HMAC，并同时绑定本地账号与明确 Membership，assertion 不持久化。
CREATE TABLE account_federation_links(
  account_federation_link_id TEXT PRIMARY KEY CHECK(length(account_federation_link_id)=36
    AND substr(account_federation_link_id,1,4)='afl_'
    AND substr(account_federation_link_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  sso_connection_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  membership_id TEXT NOT NULL,
  external_subject_hmac TEXT NOT NULL CHECK(length(external_subject_hmac)=64
    AND external_subject_hmac NOT GLOB '*[^0-9a-f]*'),
  verified_email_hmac TEXT NOT NULL CHECK(length(verified_email_hmac)=64
    AND verified_email_hmac NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  linked_at TEXT NOT NULL CHECK(unixepoch(linked_at) IS NOT NULL),
  disabled_at TEXT CHECK(disabled_at IS NULL OR unixepoch(disabled_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(sso_connection_id,workspace_id,tenant_id)
    REFERENCES sso_connections(sso_connection_id,workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(sso_connection_id,external_subject_hmac),
  UNIQUE(workspace_id,account_id),
  CHECK((status='active' AND disabled_at IS NULL)
    OR(status='disabled' AND disabled_at IS NOT NULL))
);
CREATE TRIGGER trg_federation_links_owner_prd114
BEFORE INSERT ON account_federation_links FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM workspace_memberships member
  WHERE member.workspace_membership_id=NEW.membership_id
    AND member.workspace_id=NEW.workspace_id AND member.tenant_id=NEW.tenant_id
    AND member.account_id=NEW.account_id AND member.status='active'
    AND member.role_code!='service_account')
BEGIN SELECT RAISE(ABORT,'federation_link_owner_invalid'); END;
CREATE TRIGGER trg_federation_links_update_prd114
BEFORE UPDATE ON account_federation_links FOR EACH ROW
WHEN NEW.account_federation_link_id!=OLD.account_federation_link_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.sso_connection_id!=OLD.sso_connection_id OR NEW.account_id!=OLD.account_id
  OR NEW.membership_id!=OLD.membership_id
  OR NEW.external_subject_hmac!=OLD.external_subject_hmac
  OR NEW.verified_email_hmac!=OLD.verified_email_hmac OR NEW.linked_at!=OLD.linked_at
  OR OLD.status!='active' OR NEW.status!='disabled'
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'federation_link_transition_invalid'); END;
CREATE TRIGGER trg_federation_links_delete_prd114 BEFORE DELETE ON account_federation_links
BEGIN SELECT RAISE(ABORT,'federation_link_immutable'); END;

-- 登录 state 在服务器端绑定连接、浏览器、nonce/PKCE 或 SAML request，且只允许单次终结。
CREATE TABLE sso_login_states(
  sso_login_state_id TEXT PRIMARY KEY CHECK(length(sso_login_state_id)=36
    AND substr(sso_login_state_id,1,4)='sls_'
    AND substr(sso_login_state_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  verified_domain_id TEXT NOT NULL,
  sso_connection_id TEXT NOT NULL,
  protocol TEXT NOT NULL CHECK(protocol IN('oidc','saml')),
  state_hash TEXT NOT NULL UNIQUE CHECK(length(state_hash)=64
    AND state_hash NOT GLOB '*[^0-9a-f]*'),
  browser_hmac TEXT NOT NULL CHECK(length(browser_hmac)=64
    AND browser_hmac NOT GLOB '*[^0-9a-f]*'),
  nonce_hash TEXT CHECK(nonce_hash IS NULL OR(length(nonce_hash)=64
    AND nonce_hash NOT GLOB '*[^0-9a-f]*')),
  verifier_ciphertext TEXT CHECK(verifier_ciphertext IS NULL
    OR length(verifier_ciphertext) BETWEEN 80 AND 2048),
  request_id_hash TEXT CHECK(request_id_hash IS NULL OR(length(request_id_hash)=64
    AND request_id_hash NOT GLOB '*[^0-9a-f]*')),
  status TEXT NOT NULL CHECK(status IN('pending','consumed','expired')),
  revision INTEGER NOT NULL CHECK(revision IN(1,2)),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(verified_domain_id,workspace_id,tenant_id)
    REFERENCES verified_domains(verified_domain_id,workspace_id,tenant_id),
  FOREIGN KEY(sso_connection_id,workspace_id,tenant_id)
    REFERENCES sso_connections(sso_connection_id,workspace_id,tenant_id),
  CHECK((protocol='oidc' AND nonce_hash IS NOT NULL AND verifier_ciphertext IS NOT NULL
      AND request_id_hash IS NULL)
    OR(protocol='saml' AND nonce_hash IS NULL AND verifier_ciphertext IS NULL
      AND request_id_hash IS NOT NULL)),
  CHECK((status='pending' AND revision=1 AND consumed_at IS NULL)
    OR(status IN('consumed','expired') AND revision=2 AND consumed_at IS NOT NULL))
);
CREATE INDEX idx_sso_login_states_expiry_prd114 ON sso_login_states(status,expires_at);
CREATE TRIGGER trg_sso_login_states_update_prd114
BEFORE UPDATE ON sso_login_states FOR EACH ROW
WHEN NEW.sso_login_state_id!=OLD.sso_login_state_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.verified_domain_id!=OLD.verified_domain_id
  OR NEW.sso_connection_id!=OLD.sso_connection_id OR NEW.protocol!=OLD.protocol
  OR NEW.state_hash!=OLD.state_hash OR NEW.browser_hmac!=OLD.browser_hmac
  OR NEW.nonce_hash IS NOT OLD.nonce_hash
  OR NEW.verifier_ciphertext IS NOT OLD.verifier_ciphertext
  OR NEW.request_id_hash IS NOT OLD.request_id_hash
  OR NEW.expires_at!=OLD.expires_at OR NEW.created_at!=OLD.created_at
  OR OLD.status!='pending' OR NEW.status NOT IN('consumed','expired')
  OR NEW.revision!=2 OR NEW.consumed_at IS NULL OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'sso_login_state_transition_invalid'); END;
CREATE TRIGGER trg_sso_login_states_delete_prd114 BEFORE DELETE ON sso_login_states
BEGIN SELECT RAISE(ABORT,'sso_login_state_immutable'); END;

-- 已消费 assertion/code 的摘要形成独立 replay fence，任何路径都不能更新或删除。
CREATE TABLE sso_replay_guards(
  sso_replay_guard_id TEXT PRIMARY KEY CHECK(length(sso_replay_guard_id)=36
    AND substr(sso_replay_guard_id,1,4)='srg_'
    AND substr(sso_replay_guard_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  sso_connection_id TEXT NOT NULL,
  protocol TEXT NOT NULL CHECK(protocol IN('oidc','saml')),
  assertion_hmac TEXT NOT NULL UNIQUE CHECK(length(assertion_hmac)=64
    AND assertion_hmac NOT GLOB '*[^0-9a-f]*'),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(sso_connection_id,workspace_id,tenant_id)
    REFERENCES sso_connections(sso_connection_id,workspace_id,tenant_id)
);
CREATE INDEX idx_sso_replay_guards_expiry_prd114 ON sso_replay_guards(expires_at);
CREATE TRIGGER trg_sso_replay_guards_update_prd114 BEFORE UPDATE ON sso_replay_guards
BEGIN SELECT RAISE(ABORT,'sso_replay_guard_immutable'); END;
CREATE TRIGGER trg_sso_replay_guards_delete_prd114 BEFORE DELETE ON sso_replay_guards
BEGIN SELECT RAISE(ABORT,'sso_replay_guard_immutable'); END;

-- 管理命令用 owner、actor、幂等键和请求摘要绑定结果，模糊重试不能产生第二份事实。
CREATE TABLE enterprise_identity_commands(
  enterprise_identity_command_id TEXT PRIMARY KEY CHECK(
    length(enterprise_identity_command_id)=36
    AND substr(enterprise_identity_command_id,1,4)='eic_'
    AND substr(enterprise_identity_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN(
    'domain.create','domain.verify','connection.create','connection.test','policy.update')),
  idempotency_hmac TEXT NOT NULL CHECK(length(idempotency_hmac)=64
    AND idempotency_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_type TEXT NOT NULL CHECK(result_type IN('domain','connection','policy')),
  result_id TEXT NOT NULL CHECK(length(result_id)=36),
  status TEXT NOT NULL CHECK(status IN('succeeded','failed')),
  safe_outcome_code TEXT NOT NULL CHECK(length(safe_outcome_code) BETWEEN 3 AND 80
    AND safe_outcome_code=lower(safe_outcome_code)
    AND safe_outcome_code NOT GLOB '*[^a-z0-9._]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,actor_account_id,action,idempotency_hmac)
);
CREATE TRIGGER trg_enterprise_identity_commands_update_prd114
BEFORE UPDATE ON enterprise_identity_commands
BEGIN SELECT RAISE(ABORT,'enterprise_identity_command_immutable'); END;
CREATE TRIGGER trg_enterprise_identity_commands_delete_prd114
BEFORE DELETE ON enterprise_identity_commands
BEGIN SELECT RAISE(ABORT,'enterprise_identity_command_immutable'); END;

-- 业务事件与待投递水位共表持久化，payload 仅允许固定安全摘要且业务字段不可改写。
CREATE TABLE enterprise_identity_event_outbox(
  enterprise_identity_event_id TEXT PRIMARY KEY CHECK(
    length(enterprise_identity_event_id)=36
    AND substr(enterprise_identity_event_id,1,4)='eie_'
    AND substr(enterprise_identity_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_account_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'domain.verified','sso.connection_changed','sso.login_failed',
    'sso.certificate_expiring','sso.recovery_used')),
  subject_type TEXT NOT NULL CHECK(subject_type IN(
    'domain','connection','account_link','session_grant')),
  subject_id TEXT NOT NULL CHECK(length(subject_id)=36),
  subject_revision INTEGER NOT NULL CHECK(subject_revision>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 3 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 3 AND 128),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND length(payload_json) BETWEEN 2 AND 2048),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  delivery_status TEXT NOT NULL CHECK(delivery_status IN('pending','delivered','dead_lettered')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  CHECK((delivery_status='pending' AND delivered_at IS NULL)
    OR(delivery_status='delivered' AND delivered_at IS NOT NULL)
    OR(delivery_status='dead_lettered' AND delivered_at IS NULL))
);
CREATE INDEX idx_enterprise_identity_outbox_delivery_prd114
  ON enterprise_identity_event_outbox(delivery_status,next_attempt_at,occurred_at);
CREATE TRIGGER trg_enterprise_identity_events_update_prd114
BEFORE UPDATE ON enterprise_identity_event_outbox FOR EACH ROW
WHEN NEW.enterprise_identity_event_id!=OLD.enterprise_identity_event_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.actor_account_id IS NOT OLD.actor_account_id
  OR NEW.event_type!=OLD.event_type OR NEW.subject_type!=OLD.subject_type
  OR NEW.subject_id!=OLD.subject_id OR NEW.subject_revision!=OLD.subject_revision
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.payload_json!=OLD.payload_json OR NEW.payload_sha256!=OLD.payload_sha256
  OR NEW.occurred_at!=OLD.occurred_at OR NEW.attempt_count<OLD.attempt_count
  OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'enterprise_identity_event_immutable'); END;
CREATE TRIGGER trg_enterprise_identity_events_delete_prd114
BEFORE DELETE ON enterprise_identity_event_outbox
BEGIN SELECT RAISE(ABORT,'enterprise_identity_event_immutable'); END;

-- 迁移终态必须保持 owner 图、状态约束与 SQLite 完整性全部成立。
CREATE TEMP TABLE prd114_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd114_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'verified_domains','sso_connections','sso_certificates','sso_policies',
    'account_federation_links','sso_login_states','sso_replay_guards',
    'enterprise_identity_commands','enterprise_identity_event_outbox'))=9 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=104
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=25)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description
          ||char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code))
        ='37a7ba0d1db99198e3a96d3ffa4e5f256ecae2323bddf5843d51e2f988227407'
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=264
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code))
        ='27ca2652c59ac0b223ad16f0112874fe785d805006aa15ce8ea05da542c37717'
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd114_up_final_guard;
DROP TABLE prd114_roles_to_expand;
COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
