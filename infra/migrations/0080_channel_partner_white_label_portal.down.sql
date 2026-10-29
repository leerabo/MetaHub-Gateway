-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- PRD-087 回滚迁移：原子恢复 revision-21 RBAC，并删除伙伴门户租户数据。

-- 仅收缩仍匹配 revision-22 矩阵的角色，避免覆盖迁移后的人工授权变化。
-- Only roles that still match the migration-produced revision-22 matrix may be contracted.
CREATE TEMP TABLE prd087_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','viewer')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
    WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
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

-- 回滚前置守卫先验证角色和外键，任何不一致都必须原子拒绝。
-- The guard precedes the first persistent DDL so every rejection is atomic.
DROP TABLE IF EXISTS temp.prd087_down_guard;
CREATE TEMP TABLE prd087_down_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd087_down_guard_abort
BEFORE INSERT ON prd087_down_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd087_down_guard_failed'); END;
INSERT INTO prd087_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0080' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=96
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
  + (SELECT COUNT(*) FROM partners)
  + (SELECT COUNT(*) FROM partner_customers)
  + (SELECT COUNT(*) FROM commission_records)
  + (SELECT COUNT(*) FROM partner_customer_outbox)
  + (SELECT COUNT(*) FROM partner_portal_operations)
  + (SELECT COUNT(*) FROM partner_commission_cursors WHERE status!='pending'
      OR scan_upper_sequence IS NOT NULL OR generation!=0
      OR last_scanned_ledger_sequence!=0 OR consecutive_failure_count!=0
      OR lease_owner_id IS NOT NULL OR lease_token IS NOT NULL
      OR lease_expires_at IS NOT NULL OR last_error_code IS NOT NULL
      OR next_run_at!=created_at OR updated_at!=created_at)
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE json_valid(endpoint.event_types_json) AND EXISTS(
        SELECT 1 FROM json_each(endpoint.event_types_json) event
        WHERE event.value='partner.customer_created'))
  + (SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='partner.customer_created')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type='partner.customer_created')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
      WHERE event_type='partner.customer_created')
  + (SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
      LEFT JOIN prd087_roles_to_contract selected
        ON selected.tenant_id=relation.tenant_id AND selected.role_id=relation.role_id
      WHERE permission.code LIKE 'partner_%' AND selected.role_id IS NULL)
  + (SELECT COUNT(*) FROM prd087_roles_to_contract selected
      WHERE(SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=selected.tenant_id AND relation.role_id=selected.role_id
          AND permission.code LIKE 'partner_%')!=CASE selected.code
            WHEN 'admin' THEN 6 WHEN 'viewer' THEN 5 END)
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'partners','partner_customers','commission_records','partner_commission_cursors',
      'partner_customer_outbox','partner_portal_operations'))=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND name LIKE '%_prd087')=18 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND name LIKE '%_prd087')=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
-- 删除临时守卫，随后按依赖顺序撤除伙伴事件、游标和业务表。
DROP TRIGGER prd087_down_guard_abort;
DROP TABLE prd087_down_guard;

DROP TRIGGER trg_partner_customer_outbox_delete_prd087;
DROP TRIGGER trg_partner_customer_outbox_transition_prd087;
DROP TRIGGER trg_partner_customer_outbox_owner_insert_prd087;
DROP TRIGGER trg_partner_commission_cursor_delete_prd087;
DROP TRIGGER trg_partner_commission_cursor_transition_prd087;
DROP TRIGGER trg_partner_commission_cursor_owner_insert_prd087;
DROP TRIGGER trg_partner_operations_delete_prd087;
DROP TRIGGER trg_partner_operations_update_prd087;
DROP TRIGGER trg_partner_operations_owner_insert_prd087;
DROP TRIGGER trg_commission_records_delete_prd087;
DROP TRIGGER trg_commission_records_update_prd087;
DROP TRIGGER trg_commission_records_owner_insert_prd087;
DROP TRIGGER trg_partner_customers_delete_prd087;
DROP TRIGGER trg_partner_customers_update_prd087;
DROP TRIGGER trg_partner_customers_owner_insert_prd087;
DROP TRIGGER trg_partners_delete_prd087;
DROP TRIGGER trg_partners_update_prd087;
DROP TRIGGER trg_partners_owner_insert_prd087;
DROP TABLE partner_customer_outbox;
DROP TABLE partner_portal_operations;
DROP TABLE commission_records;
DROP TABLE partner_commission_cursors;
DROP TABLE partner_customers;
DROP TABLE partners;

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

-- 仅移除 PRD-087 新增权限关系，保留其他租户和历史授权。
DELETE FROM role_permissions
WHERE permission_id IN(SELECT permission_id FROM permissions WHERE code LIKE 'partner_%')
  AND EXISTS(SELECT 1 FROM prd087_roles_to_contract selected
    WHERE selected.tenant_id=role_permissions.tenant_id
      AND selected.role_id=role_permissions.role_id);
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
  END
WHERE role_id IN(SELECT role_id FROM prd087_roles_to_contract);

DELETE FROM rbac_role_permission_templates WHERE permission_code LIKE 'partner_%';
CREATE TABLE rbac_permission_templates_prd087_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=21)
);
INSERT INTO rbac_permission_templates_prd087_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,21
FROM rbac_permission_templates WHERE permission_code NOT LIKE 'partner_%';
-- 恢复 revision-21 收紧的九个权限标签，保持冻结基线哈希可验证。
-- Restore the nine revision-21 labels tightened by the revision-22 catalog.
UPDATE rbac_permission_templates_prd087_old SET
  name=CAST(X'E8AFBBE58F96E69687E4BBB6' AS TEXT),
  description=CAST(X'E8AFBBE58F96E5BD93E5898DE7A79FE688B7E69687E4BBB6E5A484E79086E78AB6E68081E38081E68ABDE58F96E69687E69CACE5928CE5AE89E585A8E69198E8A681E38082' AS TEXT)
WHERE permission_code='file.read';
UPDATE rbac_permission_templates_prd087_old SET
  name=CAST(X'E4B88AE4BCA0E69687E4BBB6' AS TEXT),
  description=CAST(X'E4B88AE4BCA0E5B9B6E8A7A3E69E90E5BD93E5898DE7A79FE688B7E79A84E58F97E694AFE68C81E69687E4BBB6E38082' AS TEXT)
WHERE permission_code='file.write';
UPDATE rbac_permission_templates_prd087_old SET
  description=CAST(X'E8AFBBE58F96E5BD93E5898DE7A79FE688B7E884B1E6958FE79A84E69CACE59CB0E6A8A1E59E8B20456E64706F696E7420E8819AE59088E4B88EE79BAEE5BD95E69198E8A681E38082' AS TEXT)
WHERE permission_code='local_endpoint.read';
UPDATE rbac_permission_templates_prd087_old SET
  description=CAST(X'E58E9FE5AD90E6B3A8E5868CE5BD93E5898DE7A79FE688B7E69CACE59CB0E6A8A1E59E8B20456E64706F696E74E38081E5889DE5A78BE6A8A1E59E8BE4B88EE58FAFE98089E587ADE68DAEE38082' AS TEXT)
WHERE permission_code='local_endpoint.manage';
UPDATE rbac_permission_templates_prd087_old SET
  description=CAST(X'E689A7E8A18CE5BD93E5898DE7A79FE688B7E58F97E99990E581A5E5BAB7E68EA2E6B58BE4B88EE6A8A1E59E8BE79BAEE5BD95E58F91E78EB0E38082' AS TEXT)
WHERE permission_code='local_endpoint.test';
UPDATE rbac_permission_templates_prd087_old SET
  name=CAST(X'E7AEA1E79086204D435020E587BAE7AB99E7BD91E7BB9C' AS TEXT)
WHERE permission_code='mcp.network.manage';
UPDATE rbac_permission_templates_prd087_old SET
  description=CAST(X'E8AFBBE58F96E4BC81E4B89A2050726F6D707420E6A8A1E69DBFE58583E695B0E68DAEE38081E78988E69CACE58E86E58FB2E5928CE68E88E69D83E5908EE79A84E6A8A1E69DBFE5AE9AE4B989E38082' AS TEXT)
WHERE permission_code='prompt_template.read';
UPDATE rbac_permission_templates_prd087_old SET
  description=CAST(X'E5889BE5BBBAE38081E4BFAEE694B9E38081E590AFE5819CE4BC81E4B89A2050726F6D707420E6A8A1E69DBFE5B9B6E58F91E5B883E4B88DE58FAFE58F98E78988E69CACE38082' AS TEXT)
WHERE permission_code='prompt_template.write';
UPDATE rbac_permission_templates_prd087_old SET
  description=CAST(X'E59CA8E4BC81E4B89A204368617420E4B8ADE98089E68BA9E5B9B6E8B083E794A8E5BD93E5898DE5B7B2E58F91E5B883E79A842050726F6D707420E6A8A1E69DBFE78988E69CACE38082' AS TEXT)
WHERE permission_code='prompt_template.use';
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd087_old RENAME TO rbac_permission_templates;

CREATE TABLE permissions_prd087_old(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=21),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
INSERT INTO permissions_prd087_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT permission.rowid,permission.permission_id,permission.tenant_id,permission.code,
  template.category,template.name,template.description,template.risk_level,
  21,permission.status,permission.created_at,permission.updated_at
FROM permissions permission JOIN rbac_permission_templates template
  ON template.permission_code=permission.code;
DROP TABLE permissions;
ALTER TABLE permissions_prd087_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

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
    WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
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
    CASE catalog.role_code
      WHEN 'admin' THEN CAST(X'E7A79FE688B7E69C80E9AB98E7AEA1E79086E59198EFBC8CE68BA5E69C89E5AE8CE695B4E69D83E99990E4B894E79FA9E998B5E4B88DE58FAFE4BFAEE694B9E38082' AS TEXT)
      WHEN 'developer' THEN CAST(X'E8B49FE8B4A3E6A8A1E59E8BE68EA5E585A5E380814B6579E38081E8B083E8AF95E5928CE8AFB7E6B182E68E92E99A9CEFBC8CE4B88DE8AFBBE58F96E6ADA3E5BC8FE8B4A6E58D95E38082' AS TEXT)
      WHEN 'viewer' THEN CAST(X'E4BB85E69FA5E79C8BE59FBAE7A180E6A682E8A788E5928CE585ACE5BC80E68EA7E588B6E58FB0E4BFA1E681AFE38082' AS TEXT)
      WHEN 'finance_admin' THEN CAST(X'E8B49FE8B4A3E794A8E9878FE38081E8B4A6E69CACE38081E9A284E7AE97E38081E4BBB7E6A0BCE38081E68890E69CACE5BC82E5B8B8E38081E5A597E9A490E9A29DE5BAA6E5928CE8B4A6E58D95E4BA89E8AEAEE38082' AS TEXT)
      WHEN 'security_admin' THEN CAST(X'E8B49FE8B4A3E5AEA1E8AEA1E38081444C50E38081E7AD96E795A5E38081E99499E8AFAFE5BD92E4B880E5928CE5AE89E585A8E4BA8BE4BBB6E38082' AS TEXT)
      WHEN 'ops_admin' THEN CAST(X'E8B49FE8B4A32050726F7669646572E38081E78EAFE5A283E38081E9A1B9E79BAEE380814B6579E38081E6A8A1E59E8BE38081E8B7AFE794B1E38081E689B9E9878FE4BBBBE58AA1E5928CE59CA8E7BABFE5AE9EE9AA8CE8BF90E7BBB4E38082' AS TEXT)
    END,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
      WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,21,'active',
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

DROP TABLE IF EXISTS temp.prd087_down_final_guard;
CREATE TEMP TABLE prd087_down_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd087_down_final_guard_abort
BEFORE INSERT ON prd087_down_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd087_down_final_invalid'); END;
INSERT INTO prd087_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=90
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=21)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='03776958f56b3ac3d3d0355d29b4ae2ea3520be09607295f4fa15a549bbfe28b'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=236
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='c737744b0a9f8aa577c49f40bd7cdf2457f69ed93bb0dedf8d01365a5855aef0'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=21
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
      AND NOT EXISTS(SELECT 1 FROM permissions WHERE code LIKE 'partner_%')
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM prd087_roles_to_contract selected
      JOIN roles role ON role.tenant_id=selected.tenant_id AND role.role_id=selected.role_id
      WHERE role.matrix_sha256!=CASE role.code
        WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
        WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      END OR EXISTS(
        SELECT 1 FROM role_permissions relation JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
          AND permission.code LIKE 'partner_%'))
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM sqlite_schema WHERE name IN(
      'partners','partner_customers','commission_records','partner_commission_cursors',
      'partner_customer_outbox','partner_portal_operations')) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM sqlite_schema WHERE name LIKE '%_prd087')
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd087_down_final_guard_abort;
DROP TABLE prd087_down_final_guard;
DROP TABLE prd087_roles_to_contract;

PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
