-- Finance and security members can create and use one personal KEY.
DROP TRIGGER IF EXISTS trg_rbac_role_templates_insert_prd059 ON rbac_role_permission_templates;
DROP TRIGGER IF EXISTS trg_rbac_role_templates_update_prd059 ON rbac_role_permission_templates;
DROP TRIGGER IF EXISTS trg_rbac_role_templates_delete_prd059 ON rbac_role_permission_templates;

INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted) VALUES
 ('finance_admin','environment.read',1),('finance_admin','virtual_key.read',1),('finance_admin','virtual_key.write',1),
 ('security_admin','environment.read',1),('security_admin','virtual_key.read',1),('security_admin','virtual_key.write',1);
INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||md5(random()::text||clock_timestamp()::text),r.tenant_id,r.role_id,p.permission_id,
 metahub_utc_now(),metahub_utc_now()
FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id
WHERE r.code IN ('finance_admin','security_admin')
 AND p.code IN ('environment.read','virtual_key.read','virtual_key.write')
 AND NOT EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_id=r.role_id AND rp.permission_id=p.permission_id);
UPDATE roles SET permission_revision=permission_revision+1,
 matrix_sha256=metahub_sha256('['||(SELECT string_agg(to_json(code)::text,',' ORDER BY code) FROM
  (SELECT p.code AS code FROM role_permissions rp JOIN permissions p ON p.permission_id=rp.permission_id
   WHERE rp.role_id=roles.role_id) x)||']'), updated_at=metahub_utc_now()
WHERE code IN ('finance_admin','security_admin');
CREATE TRIGGER trg_rbac_role_templates_insert_prd059 BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW EXECUTE FUNCTION fn_trg_rbac_role_templates_insert_prd059();
CREATE TRIGGER trg_rbac_role_templates_update_prd059 BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW EXECUTE FUNCTION fn_trg_rbac_role_templates_update_prd059();
CREATE TRIGGER trg_rbac_role_templates_delete_prd059 BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW EXECUTE FUNCTION fn_trg_rbac_role_templates_delete_prd059();
CREATE OR REPLACE FUNCTION fn_trg_roles_owner_insert_prd059() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  IF (NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision<>1 OR NEW.matrix_sha256<>CASE NEW.code WHEN 'admin' THEN 'a9ceca1ab103efaaa8b6f2ba23713e0fa2c46e18be8709bef25ef607ed329c1c' WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1' WHEN 'viewer' THEN '4dc0ae3ce438a1ea54159d946e0ec051eaab00b08939eeb1fd4616d9ea2dab24' WHEN 'finance_admin' THEN '3f6475d2e60e24602beafadd7ed08208a4f3d4366d0c645df53f5ce4a3dee4e3' WHEN 'security_admin' THEN '0eca049b26be7f979d8bf3118bb954a4266b928508b834482ba4bb8b9e0396e5' WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba' END OR NEW.is_permission_immutable<>CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END) THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='rbac_role_immutable'; END IF;
  RETURN NEW;
END;
$trigger$;
CREATE OR REPLACE FUNCTION fn_trg_tenants_rbac_initialize_prd059() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||md5(random()::text||clock_timestamp()::text),NEW.tenant_id,c.role_code,c.role_code,'PRD-148 fixed role','active',metahub_utc_now(),metahub_utc_now(),1,CASE c.role_code WHEN 'admin' THEN 'a9ceca1ab103efaaa8b6f2ba23713e0fa2c46e18be8709bef25ef607ed329c1c' WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1' WHEN 'viewer' THEN '4dc0ae3ce438a1ea54159d946e0ec051eaab00b08939eeb1fd4616d9ea2dab24' WHEN 'finance_admin' THEN '3f6475d2e60e24602beafadd7ed08208a4f3d4366d0c645df53f5ce4a3dee4e3' WHEN 'security_admin' THEN '0eca049b26be7f979d8bf3118bb954a4266b928508b834482ba4bb8b9e0396e5' WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||md5(random()::text||clock_timestamp()::text),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,t.catalog_revision,'active',metahub_utc_now(),metahub_utc_now() FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||md5(random()::text||clock_timestamp()::text),NEW.tenant_id,r.role_id,p.permission_id,metahub_utc_now(),metahub_utc_now() FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
  RETURN NEW;
END;
$trigger$;
