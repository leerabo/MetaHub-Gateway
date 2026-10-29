DROP TRIGGER IF EXISTS trg_rbac_role_templates_insert_prd059 ON rbac_role_permission_templates;
DROP TRIGGER IF EXISTS trg_rbac_role_templates_update_prd059 ON rbac_role_permission_templates;
DROP TRIGGER IF EXISTS trg_rbac_role_templates_delete_prd059 ON rbac_role_permission_templates;
DELETE FROM rbac_role_permission_templates WHERE role_code='viewer';
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted) VALUES
  ('viewer','console.basic.read',1),('viewer','dashboard.read',1),('viewer','chat.use',1),('viewer','prompt_template.use',1),
  ('viewer','file.read',1),('viewer','file.write',1),('viewer','file.security.scan',1),('viewer','partner_portal.read',1),
  ('viewer','partner_customer.manage',1),('viewer','partner_branding.manage',1),('viewer','partner_commission.export',1),
  ('viewer','partner_delivery.download',1),('viewer','enterprise_sso.read',1),('viewer','scim.read',1),('viewer','conditional_access.read',1);
DELETE FROM role_permissions WHERE role_id IN (SELECT role_id FROM roles WHERE code='viewer');
INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||md5(random()::text||clock_timestamp()::text),r.tenant_id,r.role_id,p.permission_id,metahub_utc_now(),metahub_utc_now() FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.code='viewer';
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256='612465d72c402e7d35c56779b4644d0603571f56b4714053d2b30bc3fb2a769e' WHERE code='viewer';
CREATE TRIGGER trg_rbac_role_templates_insert_prd059 BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW EXECUTE FUNCTION fn_trg_rbac_role_templates_insert_prd059();
CREATE TRIGGER trg_rbac_role_templates_update_prd059 BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW EXECUTE FUNCTION fn_trg_rbac_role_templates_update_prd059();
CREATE TRIGGER trg_rbac_role_templates_delete_prd059 BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW EXECUTE FUNCTION fn_trg_rbac_role_templates_delete_prd059();
