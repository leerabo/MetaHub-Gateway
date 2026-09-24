-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DELETE FROM rbac_role_permission_templates WHERE role_code='viewer';
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted) VALUES
  ('viewer','console.basic.read',1),('viewer','dashboard.read',1),('viewer','chat.use',1),
  ('viewer','prompt_template.use',1),('viewer','file.read',1),('viewer','file.write',1),
  ('viewer','file.security.scan',1),('viewer','partner_portal.read',1),
  ('viewer','partner_customer.manage',1),('viewer','partner_branding.manage',1),
  ('viewer','partner_commission.export',1),('viewer','partner_delivery.download',1),
  ('viewer','enterprise_sso.read',1),('viewer','scim.read',1),('viewer','conditional_access.read',1);
DELETE FROM role_permissions WHERE role_id IN (SELECT role_id FROM roles WHERE code='viewer');
INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code JOIN permissions permission ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code WHERE role.code='viewer';
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256='612465d72c402e7d35c56779b4644d0603571f56b4714053d2b30bc3fb2a769e' WHERE code='viewer';
CREATE TRIGGER trg_rbac_role_templates_insert_prd059 BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059 BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059 BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN 'a9ceca1ab103efaaa8b6f2ba23713e0fa2c46e18be8709bef25ef607ed329c1c' WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1' WHEN 'viewer' THEN '612465d72c402e7d35c56779b4644d0603571f56b4714053d2b30bc3fb2a769e' WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c' WHEN 'security_admin' THEN '8365be2449480db94f2075bbb4925ac40b9cdd8fad4cb9629e2b5c291daf8a75' WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-142 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN 'a9ceca1ab103efaaa8b6f2ba23713e0fa2c46e18be8709bef25ef607ed329c1c' WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1' WHEN 'viewer' THEN '612465d72c402e7d35c56779b4644d0603571f56b4714053d2b30bc3fb2a769e' WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c' WHEN 'security_admin' THEN '8365be2449480db94f2075bbb4925ac40b9cdd8fad4cb9629e2b5c291daf8a75' WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,27,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;
COMMIT;
PRAGMA foreign_keys = ON;
