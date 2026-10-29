-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd115_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd115_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0107' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM scim_event_outbox)
  + (SELECT COUNT(*) FROM scim_commands)
  + (SELECT COUNT(*) FROM scim_conflicts)
  + (SELECT COUNT(*) FROM scim_sync_runs)
  + (SELECT COUNT(*) FROM scim_role_mappings)
  + (SELECT COUNT(*) FROM scim_group_members)
  + (SELECT COUNT(*) FROM scim_group_links)
  + (SELECT COUNT(*) FROM scim_pending_invitations)
  + (SELECT COUNT(*) FROM scim_user_links)
  + (SELECT COUNT(*) FROM scim_tokens)
  + (SELECT COUNT(*) FROM workspace_memberships WHERE lifecycle_source='scim')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd115_down_guard;

DROP TRIGGER trg_scim_events_delete_prd115;
DROP TRIGGER trg_scim_events_update_prd115;
DROP TRIGGER trg_scim_user_links_update_prd115;
DROP TRIGGER trg_scim_group_links_update_prd115;
DROP INDEX idx_scim_outbox_delivery_prd115;
DROP TABLE scim_event_outbox;
DROP TABLE scim_commands;
DROP INDEX idx_scim_conflicts_owner_prd115;
DROP TABLE scim_conflicts;
DROP INDEX idx_scim_sync_owner_prd115;
DROP TABLE scim_sync_runs;
DROP TABLE scim_role_mappings;
DROP TABLE scim_group_members;
DROP INDEX idx_scim_groups_owner_prd115;
DROP TABLE scim_group_links;
DROP TABLE scim_pending_invitations;
DROP INDEX idx_scim_users_owner_prd115;
DROP TABLE scim_user_links;
DROP INDEX idx_scim_tokens_owner_prd115;
DROP INDEX uq_scim_tokens_active_prd115;
DROP TABLE scim_tokens;

DROP TRIGGER trg_workspace_memberships_insert_prd115;
DROP TRIGGER trg_workspace_memberships_update_prd115;
ALTER TABLE workspace_memberships DROP COLUMN lifecycle_source;
CREATE TRIGGER trg_workspace_memberships_insert_prd094
BEFORE INSERT ON workspace_memberships FOR EACH ROW
WHEN NEW.revision!=1
  OR (NEW.role_code!='service_account' AND NOT EXISTS(
    SELECT 1 FROM account_identity_links link
    WHERE link.account_id=NEW.account_id AND link.legacy_tenant_id=NEW.tenant_id
      AND link.legacy_user_id=NEW.legacy_user_id AND link.status='active'))
BEGIN SELECT RAISE(ABORT,'workspace_membership_invalid'); END;
CREATE TRIGGER trg_workspace_memberships_update_prd094
BEFORE UPDATE ON workspace_memberships FOR EACH ROW
WHEN NEW.workspace_membership_id!=OLD.workspace_membership_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.legacy_user_id IS NOT OLD.legacy_user_id
  OR NEW.source!=OLD.source OR NEW.created_by_account_id IS NOT OLD.created_by_account_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
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

CREATE TEMP TABLE prd115_rbac_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd115_rbac_down_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=106
    AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=269
    AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=26)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM role_permissions relation
      JOIN permissions permission ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      JOIN roles role ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
      WHERE permission.code LIKE 'scim.%'
        AND role.matrix_sha256 NOT IN(
          '8cc3648fe99882833fd2cfef2b46fd76621f98a264501edae469591704e4d5a8',
          'e5953a49b667be67bd12d1ae8e94168f49a38644e2f81d2f8f64c655f3890e88',
          'f31656fea127e6a06ddd65af47ceab41a9e6311acd5e3e461ce74412a9a95075'));
DROP TABLE prd115_rbac_down_guard;
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
  SELECT permission_id FROM permissions WHERE code LIKE 'scim.%');
DELETE FROM permissions WHERE code LIKE 'scim.%';
DELETE FROM rbac_role_permission_templates WHERE permission_code LIKE 'scim.%';
CREATE TABLE rbac_permission_templates_prd115_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=25));
INSERT INTO rbac_permission_templates_prd115_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,25
FROM rbac_permission_templates WHERE permission_code NOT LIKE 'scim.%';
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd115_old RENAME TO rbac_permission_templates;
CREATE TABLE permissions_prd115_old(
  permission_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,code TEXT NOT NULL,
  category TEXT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=25),
  status TEXT NOT NULL CHECK(status='active'),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),UNIQUE(tenant_id,permission_id));
INSERT INTO permissions_prd115_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  25,status,created_at,updated_at FROM permissions;
DROP TABLE permissions;
ALTER TABLE permissions_prd115_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=CASE code
  WHEN 'admin' THEN 'e8bcc0a129f58b94fa56a46536a516eb984a2e97de914fdbb5670a79fa2840f2'
  WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
  WHEN 'viewer' THEN 'a981f2117c5083af9dd8e7cc16e9cf03b08f1b2e7bc972e8e0a0f5619e21bf19'
  WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
  WHEN 'security_admin' THEN 'c1c66bd7fea0e3fc2566a03cf41896a885bbf56db574ddbc1ef56244b456923e'
  WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END
WHERE matrix_sha256 IN(
  '8cc3648fe99882833fd2cfef2b46fd76621f98a264501edae469591704e4d5a8',
  '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686',
  'e5953a49b667be67bd12d1ae8e94168f49a38644e2f81d2f8f64c655f3890e88',
  '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc',
  'f31656fea127e6a06ddd65af47ceab41a9e6311acd5e3e461ce74412a9a95075',
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

CREATE TEMP TABLE prd115_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd115_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name LIKE 'scim_%')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('workspace_memberships')
      WHERE name='lifecycle_source')=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=104
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=264
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=25)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd115_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
