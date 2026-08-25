-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd115_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd115_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0106' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'verified_domains','workspace_memberships','workspace_sessions','global_accounts'))=4
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd115')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd115_up_guard;

-- Revision 26 adds SCIM read/manage without widening customized tenant matrices.
CREATE TEMP TABLE prd115_roles_to_expand AS
SELECT role_id,tenant_id,code FROM roles
WHERE code IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  AND matrix_sha256=CASE code
    WHEN 'admin' THEN 'e8bcc0a129f58b94fa56a46536a516eb984a2e97de914fdbb5670a79fa2840f2'
    WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
    WHEN 'viewer' THEN 'a981f2117c5083af9dd8e7cc16e9cf03b08f1b2e7bc972e8e0a0f5619e21bf19'
    WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
    WHEN 'security_admin' THEN 'c1c66bd7fea0e3fc2566a03cf41896a885bbf56db574ddbc1ef56244b456923e'
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
CREATE TABLE rbac_permission_templates_prd115_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=26));
INSERT INTO rbac_permission_templates_prd115_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,26
FROM rbac_permission_templates;
INSERT INTO rbac_permission_templates_prd115_new VALUES
 ('scim.read','identity','读取 SCIM 目录',
  '读取当前 Workspace 的 SCIM token 状态、同步计数、角色映射与冲突摘要。','read',26),
 ('scim.manage','identity','管理 SCIM 目录',
  '创建和轮换 SCIM token、维护角色映射并处理目录冲突。','critical',26);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd115_new RENAME TO rbac_permission_templates;
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted) VALUES
 ('admin','scim.read',1),('admin','scim.manage',1),('viewer','scim.read',1),
 ('security_admin','scim.read',1),('security_admin','scim.manage',1);
CREATE TABLE permissions_prd115_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=26),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),UNIQUE(tenant_id,permission_id));
INSERT INTO permissions_prd115_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  26,status,created_at,updated_at FROM permissions;
INSERT INTO permissions_prd115_new
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,26,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code LIKE 'scim.%';
DROP TABLE permissions;
ALTER TABLE permissions_prd115_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd115_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE (role.code='admin' AND permission.code LIKE 'scim.%')
   OR (role.code='viewer' AND permission.code='scim.read')
   OR (role.code='security_admin' AND permission.code LIKE 'scim.%');
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=CASE code
  WHEN 'admin' THEN '8cc3648fe99882833fd2cfef2b46fd76621f98a264501edae469591704e4d5a8'
  WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
  WHEN 'viewer' THEN 'e5953a49b667be67bd12d1ae8e94168f49a38644e2f81d2f8f64c655f3890e88'
  WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
  WHEN 'security_admin' THEN 'f31656fea127e6a06ddd65af47ceab41a9e6311acd5e3e461ce74412a9a95075'
  WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END
WHERE role_id IN(SELECT role_id FROM prd115_roles_to_expand);
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

-- Existing Membership identity remains authoritative; the new column records lifecycle ownership.
ALTER TABLE workspace_memberships ADD COLUMN lifecycle_source TEXT NOT NULL DEFAULT 'human'
  CHECK(lifecycle_source IN('human','scim'));
DROP TRIGGER trg_workspace_memberships_insert_prd094;
DROP TRIGGER trg_workspace_memberships_update_prd094;
CREATE TRIGGER trg_workspace_memberships_insert_prd115
BEFORE INSERT ON workspace_memberships FOR EACH ROW
WHEN NEW.revision!=1
  OR (NEW.role_code!='service_account' AND NOT EXISTS(
    SELECT 1 FROM account_identity_links link
    WHERE link.account_id=NEW.account_id AND link.legacy_tenant_id=NEW.tenant_id
      AND link.legacy_user_id=NEW.legacy_user_id AND link.status='active'))
  OR (NEW.lifecycle_source='scim'
    AND (NEW.role_code IN('owner','service_account') OR NEW.source!='membership_invitation'))
BEGIN SELECT RAISE(ABORT,'workspace_membership_invalid'); END;
CREATE TRIGGER trg_workspace_memberships_update_prd115
BEFORE UPDATE ON workspace_memberships FOR EACH ROW
WHEN NEW.workspace_membership_id!=OLD.workspace_membership_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.legacy_user_id IS NOT OLD.legacy_user_id
  OR NEW.source!=OLD.source OR NEW.lifecycle_source!=OLD.lifecycle_source
  OR NEW.created_by_account_id IS NOT OLD.created_by_account_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR (OLD.lifecycle_source='scim' AND OLD.status='disabled' AND NEW.status='active')
  OR ((OLD.role_code='owner' OR NEW.role_code='owner') AND NOT EXISTS(
    SELECT 1 FROM owner_transfers transfer
    WHERE transfer.workspace_id=OLD.workspace_id AND transfer.status='pending'
      AND (transfer.source_membership_id=OLD.workspace_membership_id
        OR transfer.target_membership_id=OLD.workspace_membership_id)))
  OR (OLD.role_code='owner' AND (NEW.role_code!='owner' OR NEW.status!='active')
    AND (SELECT COUNT(*) FROM workspace_memberships member
      WHERE member.workspace_id=OLD.workspace_id
        AND member.role_code='owner' AND member.status='active')<=1)
BEGIN SELECT RAISE(ABORT,'workspace_membership_invalid'); END;

CREATE TABLE scim_tokens(
  scim_token_id TEXT PRIMARY KEY CHECK(length(scim_token_id)=36
    AND substr(scim_token_id,1,4)='sct_' AND substr(scim_token_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64 AND token_hmac NOT GLOB '*[^0-9a-f]*'),
  token_prefix TEXT NOT NULL CHECK(length(token_prefix) BETWEEN 8 AND 20
    AND token_prefix NOT GLOB '*[^A-Za-z0-9_-]*'),
  scope_code TEXT NOT NULL CHECK(scope_code='users_groups'),
  status TEXT NOT NULL CHECK(status IN('active','rotating','revoked')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  grace_expires_at TEXT CHECK(grace_expires_at IS NULL OR unixepoch(grace_expires_at) IS NOT NULL),
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  created_by_membership_id TEXT NOT NULL,created_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,created_by_membership_id,created_by_account_id)
    REFERENCES workspace_memberships(workspace_id,tenant_id,workspace_membership_id,account_id),
  UNIQUE(scim_token_id,workspace_id,tenant_id),
  CHECK((status='active' AND grace_expires_at IS NULL)
    OR(status='rotating' AND grace_expires_at IS NOT NULL)
    OR(status='revoked')));
CREATE UNIQUE INDEX uq_scim_tokens_active_prd115 ON scim_tokens(workspace_id) WHERE status='active';
CREATE INDEX idx_scim_tokens_owner_prd115 ON scim_tokens(workspace_id,status,expires_at);

CREATE TABLE scim_user_links(
  scim_user_link_id TEXT PRIMARY KEY CHECK(length(scim_user_link_id)=36
    AND substr(scim_user_link_id,1,4)='sul_' AND substr(scim_user_link_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
  external_id TEXT NOT NULL CHECK(length(external_id) BETWEEN 1 AND 255),
  external_id_hmac TEXT NOT NULL CHECK(length(external_id_hmac)=64 AND external_id_hmac NOT GLOB '*[^0-9a-f]*'),
  user_name_normalized TEXT NOT NULL CHECK(length(user_name_normalized) BETWEEN 3 AND 320
    AND user_name_normalized=lower(user_name_normalized)),
  user_name_hmac TEXT NOT NULL CHECK(length(user_name_hmac)=64 AND user_name_hmac NOT GLOB '*[^0-9a-f]*'),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 120),
  account_id TEXT,membership_id TEXT,
  source_version INTEGER NOT NULL CHECK(source_version>=1),
  state TEXT NOT NULL CHECK(state IN('pending','active','disabled','conflict')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  disabled_at TEXT CHECK(disabled_at IS NULL OR unixepoch(disabled_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(workspace_id,tenant_id,membership_id,account_id)
    REFERENCES workspace_memberships(workspace_id,tenant_id,workspace_membership_id,account_id),
  UNIQUE(workspace_id,external_id_hmac),UNIQUE(workspace_id,user_name_hmac),
  UNIQUE(scim_user_link_id,workspace_id,tenant_id),
  CHECK((state='pending' AND account_id IS NULL AND membership_id IS NULL AND disabled_at IS NULL)
    OR(state='active' AND account_id IS NOT NULL AND membership_id IS NOT NULL AND disabled_at IS NULL)
    OR(state='disabled' AND disabled_at IS NOT NULL)
    OR(state='conflict')));
CREATE INDEX idx_scim_users_owner_prd115 ON scim_user_links(workspace_id,state,updated_at);

CREATE TABLE scim_pending_invitations(
  scim_pending_invitation_id TEXT PRIMARY KEY CHECK(length(scim_pending_invitation_id)=36
    AND substr(scim_pending_invitation_id,1,4)='spi_' AND substr(scim_pending_invitation_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,scim_user_link_id TEXT NOT NULL UNIQUE,
  target_email_hmac TEXT NOT NULL CHECK(length(target_email_hmac)=64 AND target_email_hmac NOT GLOB '*[^0-9a-f]*'),
  target_role_code TEXT NOT NULL CHECK(target_role_code='viewer'),
  status TEXT NOT NULL CHECK(status IN('pending','linked','cancelled')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(scim_user_link_id,workspace_id,tenant_id)
    REFERENCES scim_user_links(scim_user_link_id,workspace_id,tenant_id),
  UNIQUE(scim_pending_invitation_id,workspace_id,tenant_id));

CREATE TABLE scim_group_links(
  scim_group_link_id TEXT PRIMARY KEY CHECK(length(scim_group_link_id)=36
    AND substr(scim_group_link_id,1,4)='sgl_' AND substr(scim_group_link_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
  external_id TEXT NOT NULL CHECK(length(external_id) BETWEEN 1 AND 255),
  external_id_hmac TEXT NOT NULL CHECK(length(external_id_hmac)=64 AND external_id_hmac NOT GLOB '*[^0-9a-f]*'),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 120),
  source_version INTEGER NOT NULL CHECK(source_version>=1),
  state TEXT NOT NULL CHECK(state IN('active','disabled')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,external_id_hmac),UNIQUE(scim_group_link_id,workspace_id,tenant_id));
CREATE INDEX idx_scim_groups_owner_prd115 ON scim_group_links(workspace_id,state,updated_at);

CREATE TABLE scim_group_members(
  scim_group_member_id TEXT PRIMARY KEY CHECK(length(scim_group_member_id)=36
    AND substr(scim_group_member_id,1,4)='sgm_' AND substr(scim_group_member_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
  scim_group_link_id TEXT NOT NULL,scim_user_link_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','removed')),
  source_version INTEGER NOT NULL CHECK(source_version>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(scim_group_link_id,workspace_id,tenant_id)
    REFERENCES scim_group_links(scim_group_link_id,workspace_id,tenant_id),
  FOREIGN KEY(scim_user_link_id,workspace_id,tenant_id)
    REFERENCES scim_user_links(scim_user_link_id,workspace_id,tenant_id),
  UNIQUE(scim_group_link_id,scim_user_link_id),
  UNIQUE(scim_group_member_id,workspace_id,tenant_id));

CREATE TABLE scim_role_mappings(
  scim_role_mapping_id TEXT PRIMARY KEY CHECK(length(scim_role_mapping_id)=36
    AND substr(scim_role_mapping_id,1,4)='srm_' AND substr(scim_role_mapping_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,scim_group_link_id TEXT NOT NULL,
  role_code TEXT NOT NULL CHECK(role_code IN(
    'admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  updated_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(scim_group_link_id,workspace_id,tenant_id)
    REFERENCES scim_group_links(scim_group_link_id,workspace_id,tenant_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(scim_group_link_id),UNIQUE(scim_role_mapping_id,workspace_id,tenant_id));

CREATE TABLE scim_sync_runs(
  scim_sync_run_id TEXT PRIMARY KEY CHECK(length(scim_sync_run_id)=36
    AND substr(scim_sync_run_id,1,4)='ssr_' AND substr(scim_sync_run_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,scim_token_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('running','completed','failed','interrupted')),
  cursor_hmac TEXT CHECK(cursor_hmac IS NULL OR(length(cursor_hmac)=64 AND cursor_hmac NOT GLOB '*[^0-9a-f]*')),
  created_count INTEGER NOT NULL DEFAULT 0 CHECK(created_count>=0),
  updated_count INTEGER NOT NULL DEFAULT 0 CHECK(updated_count>=0),
  disabled_count INTEGER NOT NULL DEFAULT 0 CHECK(disabled_count>=0),
  conflict_count INTEGER NOT NULL DEFAULT 0 CHECK(conflict_count>=0),
  safe_outcome_code TEXT NOT NULL CHECK(length(safe_outcome_code) BETWEEN 2 AND 80
    AND safe_outcome_code=lower(safe_outcome_code) AND safe_outcome_code NOT GLOB '*[^a-z0-9._]*'),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(scim_token_id,workspace_id,tenant_id)
    REFERENCES scim_tokens(scim_token_id,workspace_id,tenant_id),
  UNIQUE(scim_sync_run_id,workspace_id,tenant_id),
  CHECK((status='running' AND completed_at IS NULL) OR(status!='running' AND completed_at IS NOT NULL)));
CREATE INDEX idx_scim_sync_owner_prd115 ON scim_sync_runs(workspace_id,started_at DESC);

CREATE TABLE scim_conflicts(
  scim_conflict_id TEXT PRIMARY KEY CHECK(length(scim_conflict_id)=36
    AND substr(scim_conflict_id,1,4)='scf_' AND substr(scim_conflict_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN('user','group','membership')),
  subject_id TEXT NOT NULL CHECK(length(subject_id)=36),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'owner_protected','human_membership','revision_stale','duplicate_external_id',
    'account_unverified','role_conflict')),
  directory_action_code TEXT NOT NULL CHECK(directory_action_code IN(
    'disable_membership','set_role','review_only')),
  directory_target_role_code TEXT CHECK(directory_target_role_code IS NULL OR
    directory_target_role_code IN(
      'admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  current_revision_hash TEXT NOT NULL CHECK(length(current_revision_hash)=64 AND current_revision_hash NOT GLOB '*[^0-9a-f]*'),
  directory_revision_hash TEXT NOT NULL CHECK(length(directory_revision_hash)=64 AND directory_revision_hash NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('open','resolved','ignored')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  resolution_code TEXT CHECK(resolution_code IS NULL OR resolution_code IN(
    'keep_local','accept_directory','ignore_directory')),
  resolved_by_account_id TEXT,resolved_at TEXT CHECK(resolved_at IS NULL OR unixepoch(resolved_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(resolved_by_account_id) REFERENCES global_accounts(account_id),
  CHECK((directory_action_code='set_role' AND directory_target_role_code IS NOT NULL)
    OR(directory_action_code!='set_role' AND directory_target_role_code IS NULL)),
  CHECK((status='open' AND resolution_code IS NULL AND resolved_by_account_id IS NULL AND resolved_at IS NULL)
    OR(status!='open' AND resolution_code IS NOT NULL AND resolved_by_account_id IS NOT NULL AND resolved_at IS NOT NULL)));
CREATE INDEX idx_scim_conflicts_owner_prd115 ON scim_conflicts(workspace_id,status,created_at);

CREATE TABLE scim_commands(
  scim_command_id TEXT PRIMARY KEY CHECK(length(scim_command_id)=36
    AND substr(scim_command_id,1,4)='scm_' AND substr(scim_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,actor_account_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN('token.create','token.rotate','mapping.upsert','conflict.resolve')),
  idempotency_hmac TEXT NOT NULL CHECK(length(idempotency_hmac)=64 AND idempotency_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_id TEXT NOT NULL CHECK(length(result_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,actor_account_id,action,idempotency_hmac));

CREATE TABLE scim_event_outbox(
  scim_event_id TEXT PRIMARY KEY CHECK(length(scim_event_id)=36
    AND substr(scim_event_id,1,4)='sev_' AND substr(scim_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,tenant_id TEXT NOT NULL,actor_account_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'scim.sync_completed','membership.disabled_by_scim','scim.conflict_detected')),
  subject_type TEXT NOT NULL CHECK(subject_type IN('sync','membership','conflict')),
  subject_id TEXT NOT NULL CHECK(length(subject_id)=36),
  subject_revision INTEGER NOT NULL CHECK(subject_revision>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=1024),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  delivery_status TEXT NOT NULL CHECK(delivery_status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 100),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  CHECK(payload_sha256=metahub_sha256(payload_json)),
  CHECK((delivery_status='delivered' AND delivered_at IS NOT NULL)
    OR(delivery_status!='delivered' AND delivered_at IS NULL)));
CREATE INDEX idx_scim_outbox_delivery_prd115
  ON scim_event_outbox(delivery_status,next_attempt_at,occurred_at,scim_event_id);

CREATE TRIGGER trg_scim_events_update_prd115 BEFORE UPDATE ON scim_event_outbox FOR EACH ROW
WHEN NEW.scim_event_id!=OLD.scim_event_id OR NEW.workspace_id!=OLD.workspace_id
 OR NEW.tenant_id!=OLD.tenant_id OR NEW.actor_account_id IS NOT OLD.actor_account_id
 OR NEW.event_type!=OLD.event_type OR NEW.subject_type!=OLD.subject_type
 OR NEW.subject_id!=OLD.subject_id OR NEW.subject_revision!=OLD.subject_revision
 OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
 OR NEW.payload_json!=OLD.payload_json OR NEW.payload_sha256!=OLD.payload_sha256
 OR NEW.occurred_at!=OLD.occurred_at OR NEW.attempt_count<OLD.attempt_count
 OR OLD.delivery_status='delivered'
BEGIN SELECT RAISE(ABORT,'scim_event_immutable'); END;
CREATE TRIGGER trg_scim_events_delete_prd115 BEFORE DELETE ON scim_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'scim_event_immutable'); END;
CREATE TRIGGER trg_scim_user_links_update_prd115 BEFORE UPDATE ON scim_user_links FOR EACH ROW
WHEN NEW.scim_user_link_id!=OLD.scim_user_link_id OR NEW.workspace_id!=OLD.workspace_id
 OR NEW.tenant_id!=OLD.tenant_id OR NEW.external_id!=OLD.external_id
 OR NEW.external_id_hmac!=OLD.external_id_hmac OR NEW.user_name_hmac!=OLD.user_name_hmac
 OR NEW.user_name_normalized!=OLD.user_name_normalized OR NEW.source_version<OLD.source_version
 OR NEW.revision!=OLD.revision+1 OR(OLD.state='disabled' AND NEW.state='active')
BEGIN SELECT RAISE(ABORT,'scim_user_link_invalid'); END;
CREATE TRIGGER trg_scim_group_links_update_prd115 BEFORE UPDATE ON scim_group_links FOR EACH ROW
WHEN NEW.scim_group_link_id!=OLD.scim_group_link_id OR NEW.workspace_id!=OLD.workspace_id
 OR NEW.tenant_id!=OLD.tenant_id OR NEW.external_id!=OLD.external_id
 OR NEW.external_id_hmac!=OLD.external_id_hmac OR NEW.source_version<OLD.source_version
 OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'scim_group_link_invalid'); END;

CREATE TEMP TABLE prd115_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd115_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'scim_tokens','scim_user_links','scim_pending_invitations','scim_group_links',
    'scim_group_members','scim_role_mappings','scim_sync_runs','scim_conflicts',
    'scim_commands','scim_event_outbox'))=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=106
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=269
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=26)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd115_up_final_guard;
DROP TABLE prd115_roles_to_expand;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
