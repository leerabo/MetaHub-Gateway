-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- PRD-084 rollback is allowed only before any local Endpoint or runtime event fact exists.

CREATE TEMP TABLE prd084_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','developer','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
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

DROP TABLE IF EXISTS temp.prd084_down_guard;
CREATE TEMP TABLE prd084_down_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd084_down_guard_abort
BEFORE INSERT ON prd084_down_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd084_down_guard_failed'); END;
INSERT INTO prd084_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0077' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=88
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=19)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='f18c61ce9d657a22a0592abb0922407b7bda7cc1130d93fe5eea959ee1dbd3b0'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=232
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='0a448a84577e046654ed83d384f6cb59930477fcec78156f478a2aab66a5e239'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM providers WHERE provider_type='local_endpoint')
  + (SELECT COUNT(*) FROM local_model_endpoints)
  + (SELECT COUNT(*) FROM local_model_catalog_entries)
  + (SELECT COUNT(*) FROM local_endpoint_create_receipts)
  + (SELECT COUNT(*) FROM local_model_call_events)
  + (SELECT COUNT(*) FROM local_model_call_event_outbox)
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE json_valid(endpoint.event_types_json) AND EXISTS(
        SELECT 1 FROM json_each(endpoint.event_types_json) event
        WHERE event.value='local_model.called'))
  + (SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='local_model.called')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type='local_model.called')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
      WHERE event_type='local_model.called')
  + CASE WHEN (SELECT COUNT(*) FROM tenants)=
      (SELECT COUNT(*) FROM provider_adapter_catalog_activations activation
       WHERE activation.status='active'
         AND activation.catalog_version='adapter-spec-v2'
         AND activation.catalog_hash=
           'c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf'
         AND activation.spec_count=10)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM provider_adapter_catalog_activations activation
      WHERE activation.catalog_version='adapter-spec-v2' AND NOT(
        (activation.status='active' AND activation.changed=1
          AND activation.activated_by='adapter-spec-publisher'
          AND activation.request_id LIKE 'req_prd084_migrate_%'
          AND length(activation.request_id)=51
          AND activation.trace_id='trc_prd084_migrate_'||substr(activation.request_id,20)
          AND EXISTS(SELECT 1 FROM provider_adapter_catalog_activations legacy
            WHERE legacy.tenant_id=activation.tenant_id
              AND legacy.activation_id='paca_'||substr(activation.request_id,20)
              AND legacy.catalog_version='adapter-spec-v1'
              AND legacy.catalog_hash=
                '9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870'
              AND legacy.spec_count=9 AND legacy.status='superseded'
              AND legacy.superseded_at=activation.activated_at))
        OR(activation.status='active' AND activation.changed=0
          AND activation.activated_by='adapter-spec-bootstrap'
          AND activation.request_id LIKE 'req_prd084_bootstrap_%'
          AND activation.trace_id LIKE 'trc_prd084_bootstrap_%')))
  + (SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
      LEFT JOIN prd084_roles_to_contract selected
        ON selected.tenant_id=relation.tenant_id AND selected.role_id=relation.role_id
      WHERE permission.code IN(
        'local_endpoint.read','local_endpoint.manage','local_endpoint.test')
        AND selected.role_id IS NULL)
  + (SELECT COUNT(*) FROM roles role
      WHERE role.matrix_sha256 IN(
        '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074',
        'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1',
        'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba')
        AND NOT EXISTS(SELECT 1 FROM prd084_roles_to_contract selected
          WHERE selected.role_id=role.role_id AND selected.tenant_id=role.tenant_id))
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'local_model_endpoints','local_model_catalog_entries',
      'local_endpoint_create_receipts','local_model_call_events',
      'local_model_call_event_outbox'))=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE '%_prd084')=23 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('routing_attempts')
      WHERE name IN('provider_call_status','provider_call_started_at'))=2
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd084_down_guard_abort;
DROP TABLE prd084_down_guard;

-- Remove reverse parent guards before dropping their PRD-084 child owners.
DROP TRIGGER trg_local_endpoint_provider_update_prd084;
DROP TRIGGER trg_local_endpoint_provider_delete_prd084;
DROP TRIGGER trg_local_endpoint_model_update_prd084;
DROP TRIGGER trg_local_endpoint_model_delete_prd084;
DROP TRIGGER trg_routing_attempt_local_owner_insert_prd084;
DROP TRIGGER trg_routing_attempt_local_fence_update_prd084;
DROP TRIGGER trg_routing_attempt_local_event_prd084;

DROP TABLE local_model_call_event_outbox;
DROP TABLE local_model_call_events;
DROP TABLE local_endpoint_create_receipts;
DROP TABLE local_model_catalog_entries;
DROP TABLE local_model_endpoints;

-- Restore v1 for old tenants and synthesize the original 0055 bootstrap for tenants born on v2.
CREATE TEMP TABLE prd084_adapter_restore AS
SELECT tenant_id,request_id,activated_at,activated_by,changed
FROM provider_adapter_catalog_activations
WHERE status='active' AND catalog_version='adapter-spec-v2';
DROP TRIGGER trg_tenants_adapter_spec_initialize_prd063;
DROP TRIGGER trg_provider_adapter_activations_immutable_delete_prd063;
DROP TRIGGER trg_provider_adapter_activations_immutable_update_prd063;
DROP TRIGGER trg_provider_adapter_activations_owner_insert_prd063;

DELETE FROM provider_adapter_catalog_activations
WHERE catalog_version='adapter-spec-v2';
UPDATE provider_adapter_catalog_activations
SET status='active',superseded_at=NULL,updated_at=created_at
WHERE activation_id IN(
  SELECT 'paca_'||substr(request_id,20) FROM prd084_adapter_restore
  WHERE changed=1 AND activated_by='adapter-spec-publisher');
INSERT INTO provider_adapter_catalog_activations(
  activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
  request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at)
SELECT 'paca_'||lower(hex(randomblob(16))),tenant.tenant_id,'adapter-spec-v1',
  '9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870',
  9,'active',0,'req_prd063_bootstrap_'||lower(hex(randomblob(12))),
  'trc_prd063_bootstrap_'||lower(hex(randomblob(12))),
  'adapter-spec-bootstrap',strftime('%Y-%m-%dT%H:%M:%SZ','now'),NULL,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
WHERE NOT EXISTS(SELECT 1 FROM provider_adapter_catalog_activations activation
  WHERE activation.tenant_id=tenant.tenant_id AND activation.status='active');

CREATE TRIGGER trg_provider_adapter_activations_owner_insert_prd063
BEFORE INSERT ON provider_adapter_catalog_activations FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR(NEW.activated_by='adapter-spec-bootstrap' AND(
    NEW.catalog_version!='adapter-spec-v1'
    OR NEW.catalog_hash!='9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870'
    OR NEW.spec_count!=9 OR NEW.status!='active' OR NEW.changed!=0
    OR NEW.request_id NOT LIKE 'req_prd063_bootstrap_%'
    OR NEW.trace_id NOT LIKE 'trc_prd063_bootstrap_%'))
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_owner_invalid'); END;
CREATE TRIGGER trg_provider_adapter_activations_immutable_update_prd063
BEFORE UPDATE ON provider_adapter_catalog_activations FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.activation_id!=OLD.activation_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.catalog_version!=OLD.catalog_version
  OR NEW.catalog_hash!=OLD.catalog_hash OR NEW.spec_count!=OLD.spec_count
  OR NEW.changed!=OLD.changed OR NEW.request_id!=OLD.request_id
  OR NEW.trace_id!=OLD.trace_id OR NEW.activated_by!=OLD.activated_by
  OR NEW.activated_at!=OLD.activated_at OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status!='superseded'
  OR OLD.superseded_at IS NOT NULL OR NEW.superseded_at IS NULL
  OR NEW.updated_at!=NEW.superseded_at OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_immutable'); END;
CREATE TRIGGER trg_provider_adapter_activations_immutable_delete_prd063
BEFORE DELETE ON provider_adapter_catalog_activations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_immutable'); END;
CREATE TRIGGER trg_tenants_adapter_spec_initialize_prd063
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  INSERT INTO provider_adapter_catalog_activations(
    activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
    request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at)
  VALUES('paca_'||lower(hex(randomblob(16))),NEW.tenant_id,'adapter-spec-v1',
    '9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870',
    9,'active',0,'req_prd063_bootstrap_'||lower(hex(randomblob(12))),
    'trc_prd063_bootstrap_'||lower(hex(randomblob(12))),
    'adapter-spec-bootstrap',strftime('%Y-%m-%dT%H:%M:%SZ','now'),NULL,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;
DROP TABLE prd084_adapter_restore;

-- Contract only default matrices that still exactly match PRD-084.
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

DELETE FROM role_permissions
WHERE role_id IN(SELECT role_id FROM prd084_roles_to_contract)
  AND permission_id IN(SELECT permission_id FROM permissions WHERE code IN(
    'local_endpoint.read','local_endpoint.manage','local_endpoint.test'));
DELETE FROM rbac_role_permission_templates
WHERE permission_code IN(
  'local_endpoint.read','local_endpoint.manage','local_endpoint.test');

CREATE TABLE rbac_permission_templates_prd084_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=18)
);
INSERT INTO rbac_permission_templates_prd084_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,18
FROM rbac_permission_templates
WHERE permission_code NOT IN(
  'local_endpoint.read','local_endpoint.manage','local_endpoint.test');
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd084_old RENAME TO rbac_permission_templates;

CREATE TABLE permissions_prd084_old(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=18),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
INSERT INTO permissions_prd084_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  18,status,created_at,updated_at FROM permissions
WHERE code NOT IN('local_endpoint.read','local_endpoint.manage','local_endpoint.test');
DROP TABLE permissions;
ALTER TABLE permissions_prd084_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '5af655a2ffa8cb4f14d84bec71e1b0feb17e4a1e2d982c9fc9cbe711e74cead7'
    WHEN 'developer' THEN '69328ccc0d6741562250f7b83fe7cf3fce8509db4b092b6917b475e3dd446b8d'
    WHEN 'ops_admin' THEN 'dc3641880859dff9a82069c6c4b1f32049320aa9406e0f885770ad643da04c1e'
  END
WHERE role_id IN(SELECT role_id FROM prd084_roles_to_contract);

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
CREATE TRIGGER trg_permissions_update_prd059
BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059
BEFORE DELETE ON permissions FOR EACH ROW
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
    WHEN 'admin' THEN '5af655a2ffa8cb4f14d84bec71e1b0feb17e4a1e2d982c9fc9cbe711e74cead7'
    WHEN 'developer' THEN '69328ccc0d6741562250f7b83fe7cf3fce8509db4b092b6917b475e3dd446b8d'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN 'dc3641880859dff9a82069c6c4b1f32049320aa9406e0f885770ad643da04c1e'
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
  OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
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
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常、套餐额度和账单争议。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。'
    END,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '5af655a2ffa8cb4f14d84bec71e1b0feb17e4a1e2d982c9fc9cbe711e74cead7'
      WHEN 'developer' THEN '69328ccc0d6741562250f7b83fe7cf3fce8509db4b092b6917b475e3dd446b8d'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN 'dc3641880859dff9a82069c6c4b1f32049320aa9406e0f885770ad643da04c1e'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,18,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template
    ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;
DROP TABLE prd084_roles_to_contract;

-- No PRD-084 trigger remains, so SQLite can atomically remove both routing columns.
ALTER TABLE routing_attempts DROP COLUMN provider_call_started_at;
ALTER TABLE routing_attempts DROP COLUMN provider_call_status;

-- Rebuild the original nine-type Provider CHECK and restore all eight named indexes/four triggers.
DROP TRIGGER trg_policy_template_provider_delete_prd068;
DROP TRIGGER trg_policy_template_provider_update_prd068;
DROP TRIGGER trg_providers_experiment_arm_delete_prd075;
DROP TRIGGER trg_providers_experiment_arm_update_prd075;

CREATE TABLE providers_prd084_old(
  provider_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  provider_code TEXT NOT NULL,
  name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  provider_type TEXT NOT NULL DEFAULT 'openai_compatible' CHECK(provider_type IN(
    'openai_compatible','zhipu','dashscope','wenxin','volcengine','hunyuan',
    'deepseek','custom','migration_compatible')),
  auth_type TEXT NOT NULL DEFAULT 'bearer' CHECK(auth_type IN('none','bearer','custom_header')),
  description TEXT,
  health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(health_status IN('unknown','healthy','unhealthy')),
  last_tested_at TEXT,
  last_test_error TEXT,
  created_by TEXT,
  disabled_at TEXT,
  disabled_by TEXT,
  last_success_at TEXT,
  last_failure_at TEXT,
  last_probe_latency_ms INTEGER CHECK(last_probe_latency_ms IS NULL OR last_probe_latency_ms>=0),
  last_probe_http_status INTEGER CHECK(last_probe_http_status IS NULL OR last_probe_http_status BETWEEN 100 AND 599),
  last_probe_error_code TEXT,
  consecutive_probe_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_probe_failures>=0),
  health_generation INTEGER NOT NULL DEFAULT 0 CHECK(health_generation>=0),
  health_updated_at TEXT,
  next_probe_at TEXT,
  probe_lease_token TEXT,
  probe_lease_until TEXT,
  maintenance_note TEXT CHECK(maintenance_note IS NULL OR(
    typeof(maintenance_note)='text' AND length(maintenance_note) BETWEEN 1 AND 240
    AND maintenance_note=trim(maintenance_note) AND instr(maintenance_note,char(0))=0
    AND maintenance_note NOT GLOB(
      '*['||char(1)||'-'||char(31)||char(127)||'-'||char(159)||']*'))),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE(tenant_id,provider_code)
);
INSERT INTO providers_prd084_old(
  rowid,provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
  provider_type,auth_type,description,health_status,last_tested_at,last_test_error,
  created_by,disabled_at,disabled_by,last_success_at,last_failure_at,
  last_probe_latency_ms,last_probe_http_status,last_probe_error_code,
  consecutive_probe_failures,health_generation,health_updated_at,next_probe_at,
  probe_lease_token,probe_lease_until,maintenance_note)
SELECT rowid,provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
  provider_type,auth_type,description,health_status,last_tested_at,last_test_error,
  created_by,disabled_at,disabled_by,last_success_at,last_failure_at,
  last_probe_latency_ms,last_probe_http_status,last_probe_error_code,
  consecutive_probe_failures,health_generation,health_updated_at,next_probe_at,
  probe_lease_token,probe_lease_until,maintenance_note
FROM providers;
DROP TABLE providers;
ALTER TABLE providers_prd084_old RENAME TO providers;
CREATE INDEX idx_providers_tenant_id ON providers(tenant_id);
CREATE INDEX idx_providers_provider_type ON providers(tenant_id,provider_type);
CREATE INDEX idx_providers_health_status ON providers(tenant_id,health_status);
CREATE INDEX idx_provider_health_due ON providers(status,next_probe_at,probe_lease_until);
CREATE INDEX idx_provider_health_tenant_status
  ON providers(tenant_id,health_status,health_updated_at);
CREATE UNIQUE INDEX uq_providers_tenant_id_prd047 ON providers(tenant_id,provider_id);
CREATE UNIQUE INDEX uq_providers_tenant_provider_prd051 ON providers(tenant_id,provider_id);
CREATE UNIQUE INDEX idx_providers_tenant_provider_owner_prd056
  ON providers(tenant_id,provider_id);

CREATE TRIGGER trg_policy_template_provider_update_prd068
BEFORE UPDATE ON providers FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id AND dependency.resource_type='provider'
    AND dependency.resource_id=OLD.provider_id AND application.status='applied')
AND(NEW.rowid!=OLD.rowid OR NEW.provider_id!=OLD.provider_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.provider_type!=OLD.provider_type
  OR NEW.base_url!=OLD.base_url OR NEW.auth_type!=OLD.auth_type
  OR(OLD.status!='active' AND NEW.status='active'))
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_provider_delete_prd068
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id AND dependency.resource_type='provider'
    AND dependency.resource_id=OLD.provider_id AND application.status='applied')
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_providers_experiment_arm_update_prd075
BEFORE UPDATE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
      AND OLD.provider_id IN(experiment.model_a_provider_id,experiment.model_b_provider_id))
  AND(NEW.rowid!=OLD.rowid OR NEW.provider_id!=OLD.provider_id
    OR NEW.tenant_id!=OLD.tenant_id OR NEW.provider_code!=OLD.provider_code
    OR NEW.status!=OLD.status OR NEW.provider_type!=OLD.provider_type
    OR NEW.base_url!=OLD.base_url OR NEW.auth_type!=OLD.auth_type)
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;
CREATE TRIGGER trg_providers_experiment_arm_delete_prd075
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
  WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
    AND OLD.provider_id IN(experiment.model_a_provider_id,experiment.model_b_provider_id))
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;

DROP TABLE IF EXISTS temp.prd084_down_final_guard;
CREATE TEMP TABLE prd084_down_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd084_down_final_guard_abort
BEFORE INSERT ON prd084_down_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd084_down_final_invalid'); END;
INSERT INTO prd084_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=85
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=18)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='08eee7f1eb3c329ae7d27f205afdd47aa69db4d702647a09c75271aac0433492'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=224
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='cae76323d18bad139ecfd04950e2b3c50d37916dd41a2427a93bc3927acc7a84'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=18
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM tenants)=
      (SELECT COUNT(*) FROM provider_adapter_catalog_activations
       WHERE status='active' AND catalog_version='adapter-spec-v1'
         AND catalog_hash='9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870'
         AND spec_count=9)
      AND NOT EXISTS(SELECT 1 FROM provider_adapter_catalog_activations
        WHERE catalog_version='adapter-spec-v2')
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM sqlite_schema WHERE name IN(
      'local_model_endpoints','local_model_catalog_entries',
      'local_endpoint_create_receipts','local_model_call_events',
      'local_model_call_event_outbox')) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM pragma_table_info('routing_attempts')
      WHERE name IN('provider_call_status','provider_call_started_at'))
    THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='providers'),'''local_endpoint''')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE tbl_name='providers'
      AND type='index')=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE tbl_name='providers'
      AND type='trigger')=4 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM sqlite_schema
      WHERE type='trigger' AND name LIKE '%_prd084') THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd084_down_final_guard_abort;
DROP TABLE prd084_down_final_guard;

PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
