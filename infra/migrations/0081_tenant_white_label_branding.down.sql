-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- PRD-088 rollback: only an unused default BrandingConfig may be contracted.

-- Only roles that still match the migration-produced revision-23 default matrix may
-- be contracted. Tenant-customized non-admin matrices remain untouched.
CREATE TEMP TABLE prd088_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e'
    WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
    WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e'
    WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
    WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377'
    WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1'
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

DROP TABLE IF EXISTS temp.prd088_down_guard;
CREATE TEMP TABLE prd088_down_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd088_down_guard_abort
BEFORE INSERT ON prd088_down_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd088_down_guard_failed'); END;
INSERT INTO prd088_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
      ORDER BY version DESC LIMIT 1)='0081' THEN 0 ELSE 1 END
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
  + (SELECT COUNT(*) FROM branding_events)
  + (SELECT COUNT(*) FROM branding_event_outbox)
  + (SELECT COUNT(*) FROM branding_configs WHERE status!='active' OR revision!=1
      OR product_name!='MetaHub' OR primary_color!='#176B87' OR secondary_color!='#F59E0B'
      OR login_title!='企业 AI Gateway' OR login_message!='安全、可追踪的企业模型网关'
      OR logo_png IS NOT NULL OR logo_sha256 IS NOT NULL OR logo_width IS NOT NULL
      OR logo_height IS NOT NULL OR custom_domain IS NOT NULL
      OR custom_domain_status!='not_configured' OR updated_by_user_id IS NOT NULL
      OR updated_request_id IS NOT NULL OR updated_trace_id IS NOT NULL
      OR created_at!=updated_at)
  + (SELECT COUNT(*) FROM audit_logs
      WHERE action='branding.updated' OR object_type='branding_config')
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE json_valid(endpoint.event_types_json) AND EXISTS(
        SELECT 1 FROM json_each(endpoint.event_types_json) event
        WHERE event.value='branding.updated'))
  + (SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='branding.updated')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type='branding.updated')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events WHERE event_type='branding.updated')
  + (SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
      LEFT JOIN prd088_roles_to_contract selected
        ON selected.tenant_id=relation.tenant_id AND selected.role_id=relation.role_id
      WHERE permission.code IN('branding.read','branding.manage')
        AND selected.role_id IS NULL)
  + (SELECT COUNT(*) FROM prd088_roles_to_contract selected
      WHERE(SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=selected.tenant_id AND relation.role_id=selected.role_id
          AND permission.code IN('branding.read','branding.manage'))
        !=CASE selected.code WHEN 'admin' THEN 2 ELSE 1 END)
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table'
      AND name IN('branding_configs','branding_events','branding_event_outbox'))=3
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND name LIKE '%_prd088')=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND name LIKE '%_prd088')=3 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd088_down_guard_abort;
DROP TABLE prd088_down_guard;

DROP TRIGGER trg_tenants_branding_initialize_prd088;
DROP TRIGGER trg_branding_event_outbox_delete_prd088;
DROP TRIGGER trg_branding_event_outbox_transition_prd088;
DROP TRIGGER trg_branding_event_outbox_owner_insert_prd088;
DROP TRIGGER trg_branding_events_delete_prd088;
DROP TRIGGER trg_branding_events_immutable_prd088;
DROP TRIGGER trg_branding_events_owner_insert_prd088;
DROP TRIGGER trg_branding_configs_delete_prd088;
DROP TRIGGER trg_branding_configs_update_prd088;
DROP TRIGGER trg_branding_configs_owner_insert_prd088;
DROP TABLE branding_event_outbox;
DROP TABLE branding_events;
DROP TABLE branding_configs;

-- Contract revision 23 RBAC back to the exact revision 22 catalog and matrix.
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
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DELETE FROM role_permissions
WHERE permission_id IN(SELECT permission_id FROM permissions
    WHERE code IN('branding.read','branding.manage'))
  AND EXISTS(SELECT 1 FROM prd088_roles_to_contract selected
    WHERE selected.tenant_id=role_permissions.tenant_id
      AND selected.role_id=role_permissions.role_id);
DELETE FROM permissions WHERE code IN('branding.read','branding.manage');
DELETE FROM rbac_role_permission_templates
WHERE permission_code IN('branding.read','branding.manage');

CREATE TABLE rbac_permission_templates_prd088_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=22)
);
INSERT INTO rbac_permission_templates_prd088_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,22
FROM rbac_permission_templates
WHERE permission_code NOT IN('branding.read','branding.manage');
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd088_old RENAME TO rbac_permission_templates;

CREATE TABLE permissions_prd088_old(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=22),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
INSERT INTO permissions_prd088_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT permission.rowid,permission.permission_id,permission.tenant_id,permission.code,
  template.category,template.name,template.description,template.risk_level,
  22,permission.status,permission.created_at,permission.updated_at
FROM permissions permission JOIN rbac_permission_templates template
  ON template.permission_code=permission.code;
DROP TABLE permissions;
ALTER TABLE permissions_prd088_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
  END
WHERE role_id IN(SELECT role_id FROM prd088_roles_to_contract);

PRAGMA writable_schema=ON;
UPDATE sqlite_schema SET sql=replace(sql,'''response_cache.invalidated'',''branding.updated''','''response_cache.invalidated''')
  WHERE type='table' AND name='audit_logs';
UPDATE sqlite_schema SET sql=replace(sql,'''response_cache_invalidation'',''branding_config''','''response_cache_invalidation''')
  WHERE type='table' AND name='audit_logs';
PRAGMA schema_version=1;
PRAGMA writable_schema=OFF;

CREATE TRIGGER trg_rbac_permission_templates_insert_prd059
BEFORE INSERT ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059
BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059
BEFORE DELETE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_insert_prd059
BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059
BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059
BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level
    AND template.catalog_revision=NEW.catalog_revision)
  OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS(SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
      OR(current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059
AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current WHERE current.role_id=NEW.role_id
    OR(current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name
  OR NEW.description!=OLD.description OR NEW.status!=OLD.status
  OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64
  OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role JOIN permissions permission
    ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
    AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.status='active' AND permission.permission_id=NEW.permission_id
    AND permission.status='active')
  OR EXISTS(SELECT 1 FROM role_permissions current
    WHERE current.role_permission_id=NEW.role_permission_id
      OR(current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
        AND current.permission_id=NEW.permission_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM roles role WHERE role.tenant_id=OLD.tenant_id
  AND role.role_id=OLD.role_id AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常、套餐额度和账单争议。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。'
    END,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
      WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
      WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,22,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

DROP TABLE IF EXISTS temp.prd088_down_final_guard;
CREATE TEMP TABLE prd088_down_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd088_down_final_guard_abort
BEFORE INSERT ON prd088_down_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd088_down_final_invalid'); END;
INSERT INTO prd088_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=96
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
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=22
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
      AND NOT EXISTS(SELECT 1 FROM tenants tenant WHERE(
        SELECT COUNT(*) FROM permissions permission
        WHERE permission.tenant_id=tenant.tenant_id)!=96)
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM prd088_roles_to_contract selected
      JOIN roles role ON role.role_id=selected.role_id
      WHERE role.matrix_sha256!=CASE role.code
        WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
        WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
        WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
        WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
        WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
        WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
      END) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'branding_configs','branding_events','branding_event_outbox'))=0
    THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
      '''branding.updated''')=0
      AND instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
        '''branding_config''')=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd088_down_final_guard_abort;
DROP TABLE prd088_down_final_guard;
DROP TABLE prd088_roles_to_contract;

COMMIT;
PRAGMA foreign_keys = ON;
