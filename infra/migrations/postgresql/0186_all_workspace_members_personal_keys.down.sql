-- Roll back personal KEY grants for finance and security members.
DROP TRIGGER IF EXISTS trg_rbac_role_templates_insert_prd059 ON rbac_role_permission_templates;
DROP TRIGGER IF EXISTS trg_rbac_role_templates_update_prd059 ON rbac_role_permission_templates;
DROP TRIGGER IF EXISTS trg_rbac_role_templates_delete_prd059 ON rbac_role_permission_templates;

DELETE FROM role_permissions WHERE role_id IN (SELECT role_id FROM roles WHERE code IN ('finance_admin','security_admin'))
 AND permission_id IN (SELECT permission_id FROM permissions WHERE code IN ('environment.read','virtual_key.read','virtual_key.write'));
DELETE FROM rbac_role_permission_templates WHERE role_code IN ('finance_admin','security_admin')
 AND permission_code IN ('environment.read','virtual_key.read','virtual_key.write');
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
  IF (NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision<>1 OR NEW.matrix_sha256<>CASE NEW.code WHEN 'admin' THEN 'a9ceca1ab103efaaa8b6f2ba23713e0fa2c46e18be8709bef25ef607ed329c1c' WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1' WHEN 'viewer' THEN '4dc0ae3ce438a1ea54159d946e0ec051eaab00b08939eeb1fd4616d9ea2dab24' WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c' WHEN 'security_admin' THEN '8365be2449480db94f2075bbb4925ac40b9cdd8fad4cb9629e2b5c291daf8a75' WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba' END OR NEW.is_permission_immutable<>CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END) THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='rbac_role_immutable'; END IF;
  RETURN NEW;
END;
$trigger$;
CREATE OR REPLACE FUNCTION fn_trg_tenants_rbac_initialize_prd059() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||md5(random()::text||clock_timestamp()::text),NEW.tenant_id,c.role_code,c.role_code,'PRD-148 fixed role','active',metahub_utc_now(),metahub_utc_now(),1,CASE c.role_code WHEN 'admin' THEN 'a9ceca1ab103efaaa8b6f2ba23713e0fa2c46e18be8709bef25ef607ed329c1c' WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1' WHEN 'viewer' THEN '4dc0ae3ce438a1ea54159d946e0ec051eaab00b08939eeb1fd4616d9ea2dab24' WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c' WHEN 'security_admin' THEN '8365be2449480db94f2075bbb4925ac40b9cdd8fad4cb9629e2b5c291daf8a75' WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||md5(random()::text||clock_timestamp()::text),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,t.catalog_revision,'active',metahub_utc_now(),metahub_utc_now() FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||md5(random()::text||clock_timestamp()::text),NEW.tenant_id,r.role_id,p.permission_id,metahub_utc_now(),metahub_utc_now() FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
  RETURN NEW;
END;
$trigger$;
