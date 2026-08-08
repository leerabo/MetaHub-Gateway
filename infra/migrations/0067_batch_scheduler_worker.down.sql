-- PRD-074 rollback: only an empty Batch system and exact revision-10 canonical matrices may contract.
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

DROP TABLE IF EXISTS temp.prd074_down_guard;
CREATE TEMP TABLE prd074_down_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd074_down_guard_ok CHECK(issue_count=0)
);
INSERT INTO prd074_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0067' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0067')
  + (SELECT COUNT(*) FROM batch_jobs)
  + (SELECT COUNT(*) FROM batch_tasks)
  + (SELECT COUNT(*) FROM batch_attempts)
  + (SELECT COUNT(*) FROM batch_execution_grants)
  + (SELECT COUNT(*) FROM batch_budget_holds)
  + (SELECT COUNT(*) FROM batch_budget_allocations)
  + (SELECT COUNT(*) FROM batch_job_events)
  + (SELECT COUNT(*) FROM batch_job_event_outbox)
  + (SELECT COUNT(*) FROM batch_reconciliation_events)
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE EXISTS(SELECT 1 FROM json_each(endpoint.event_types_json)
        WHERE value IN ('batch_job.created','batch_job.completed')))
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE event_type IN ('batch_job.created','batch_job.completed'))
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      WHERE EXISTS(SELECT 1 FROM webhook_deliveries delivery
        WHERE delivery.webhook_delivery_id=attempt.webhook_delivery_id
          AND delivery.event_type IN ('batch_job.created','batch_job.completed')))
  + (SELECT COUNT(*) FROM webhook_dead_letter_events dead
      WHERE EXISTS(SELECT 1 FROM webhook_deliveries delivery
        WHERE delivery.webhook_delivery_id=dead.webhook_delivery_id
          AND delivery.event_type IN ('batch_job.created','batch_job.completed')))
  + (SELECT COUNT(*) FROM role_permission_events event
      WHERE EXISTS(SELECT 1 FROM json_each(event.added_permissions_json)
        WHERE value IN ('batch.read','batch.write'))
         OR EXISTS(SELECT 1 FROM json_each(event.removed_permissions_json)
        WHERE value IN ('batch.read','batch.write')))
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=64
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=10)=64
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=173
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd074_down_guard;

CREATE TEMP TABLE prd074_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262'
    WHEN 'developer' THEN '89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30'
    WHEN 'ops_admin' THEN '72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6'
  END
  AND NOT EXISTS(
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS(
      SELECT 1 FROM role_permissions relation
      JOIN permissions permission ON permission.tenant_id=relation.tenant_id
        AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code
    )
  )
  AND NOT EXISTS(
    SELECT 1 FROM role_permissions relation
    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS(SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code)
  );

DROP TABLE IF EXISTS temp.prd074_down_role_guard;
CREATE TEMP TABLE prd074_down_role_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd074_down_role_guard_ok CHECK(issue_count=0)
);
INSERT INTO prd074_down_role_guard(issue_count)
SELECT COUNT(*)
FROM role_permissions relation
JOIN permissions permission ON permission.tenant_id=relation.tenant_id
  AND permission.permission_id=relation.permission_id
JOIN roles role ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
WHERE (permission.code IN ('batch.read','batch.write')
    OR (role.code='ops_admin' AND permission.code='file.read'))
  AND NOT EXISTS(SELECT 1 FROM prd074_roles_to_contract canonical
    WHERE canonical.tenant_id=role.tenant_id AND canonical.role_id=role.role_id);
DROP TABLE prd074_down_role_guard;

-- Drop Batch objects only after every business and external-reference guard has passed.
DROP TRIGGER trg_batch_outbox_rowid_prd074;
DROP TRIGGER trg_batch_outbox_delete_prd074;
DROP TRIGGER trg_batch_outbox_update_prd074;
DROP TRIGGER trg_batch_outbox_conflict_insert_prd074;
DROP TRIGGER trg_batch_outbox_validate_insert_prd074;
DROP TRIGGER trg_batch_events_create_outbox_prd074;
DROP TRIGGER trg_batch_reconciliation_delete_prd074;
DROP TRIGGER trg_batch_reconciliation_update_prd074;
DROP TRIGGER trg_batch_reconciliation_owner_insert_prd074;
DROP TRIGGER trg_batch_events_delete_prd074;
DROP TRIGGER trg_batch_events_update_prd074;
DROP TRIGGER trg_batch_events_owner_insert_prd074;
DROP TRIGGER trg_batch_scheduler_delete_prd074;
DROP TRIGGER trg_batch_scheduler_update_prd074;
DROP TRIGGER trg_batch_allocations_delete_prd074;
DROP TRIGGER trg_batch_allocations_update_prd074;
DROP TRIGGER trg_batch_allocations_owner_insert_prd074;
DROP TRIGGER trg_batch_holds_delete_prd074;
DROP TRIGGER trg_batch_holds_amount_nonincrease_prd074;
DROP TRIGGER trg_batch_holds_update_prd074;
DROP TRIGGER trg_batch_holds_owner_insert_prd074;
DROP TRIGGER trg_batch_grants_delete_prd074;
DROP TRIGGER trg_batch_grants_update_prd074;
DROP TRIGGER trg_batch_grants_owner_insert_prd074;
DROP TRIGGER trg_batch_attempts_delete_prd074;
DROP TRIGGER trg_batch_attempts_update_prd074;
DROP TRIGGER trg_batch_attempts_owner_insert_prd074;
DROP TRIGGER trg_batch_tasks_delete_prd074;
DROP TRIGGER trg_batch_tasks_update_prd074;
DROP TRIGGER trg_batch_tasks_owner_insert_prd074;
DROP TRIGGER trg_batch_jobs_delete_prd074;
DROP TRIGGER trg_batch_jobs_update_prd074;
DROP TRIGGER trg_batch_jobs_owner_insert_prd074;
DROP TABLE batch_reconciliation_events;
DROP TABLE batch_job_event_outbox;
DROP TABLE batch_job_events;
DROP TABLE batch_scheduler_tenants;
DROP TABLE batch_budget_allocations;
DROP TABLE batch_budget_holds;
DROP TABLE batch_execution_grants;
DROP TABLE batch_attempts;
DROP TABLE batch_tasks;
DROP TABLE batch_jobs;
DROP INDEX uq_budget_rules_tenant_rule_prd074;

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
WHERE EXISTS(SELECT 1 FROM prd074_roles_to_contract role
  WHERE role.tenant_id=role_permissions.tenant_id
    AND role.role_id=role_permissions.role_id)
  AND permission_id IN(SELECT permission_id FROM permissions
    WHERE tenant_id=role_permissions.tenant_id
      AND (code IN ('batch.read','batch.write')
        OR (code='file.read' AND EXISTS(SELECT 1 FROM roles role
          WHERE role.tenant_id=role_permissions.tenant_id
            AND role.role_id=role_permissions.role_id AND role.code='ops_admin'))));
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN 'e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319'
      WHEN 'developer' THEN 'a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
    END
WHERE role_id IN(SELECT role_id FROM prd074_roles_to_contract);

DELETE FROM rbac_role_permission_templates
WHERE (role_code IN ('admin','developer','ops_admin')
    AND permission_code IN ('batch.read','batch.write'))
   OR (role_code='ops_admin' AND permission_code='file.read');

CREATE TABLE rbac_permission_templates_prd074_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=9)
);
INSERT INTO rbac_permission_templates_prd074_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,9
FROM rbac_permission_templates
WHERE permission_code NOT IN ('batch.read','batch.write');
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd074_old RENAME TO rbac_permission_templates;

CREATE TABLE permissions_prd074_old(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=9),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
INSERT INTO permissions_prd074_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       9,status,created_at,updated_at
FROM permissions WHERE code NOT IN ('batch.read','batch.write');
DROP TABLE permissions;
ALTER TABLE permissions_prd074_old RENAME TO permissions;
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
    AND template.risk_level=NEW.risk_level AND template.catalog_revision=NEW.catalog_revision)
  OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS(SELECT 1 FROM permissions current WHERE current.permission_id=NEW.permission_id
    OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059 AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN 'e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319'
    WHEN 'developer' THEN 'a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
    WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current WHERE current.role_id=NEW.role_id
    OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name
  OR NEW.description!=OLD.description OR NEW.status!=OLD.status
  OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
    AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id AND role.status='active'
    AND permission.permission_id=NEW.permission_id AND permission.status='active')
  OR EXISTS(SELECT 1 FROM role_permissions current
    WHERE current.role_permission_id=NEW.role_permission_id
      OR (current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
        AND current.permission_id=NEW.permission_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM roles role WHERE role.tenant_id=OLD.tenant_id
  AND role.role_id=OLD.role_id AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  ) SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格和成本异常。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型和路由运维。' END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN 'e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319'
      WHEN 'developer' THEN 'a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  ) SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    9,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  ) SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code WHERE role.tenant_id=NEW.tenant_id;
END;

DROP TABLE prd074_roles_to_contract;
DROP TABLE IF EXISTS temp.prd074_down_final_guard;
CREATE TEMP TABLE prd074_down_final_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd074_down_final_guard_ok CHECK(issue_count=0)
);
INSERT INTO prd074_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=62
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=9)=62
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=166
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'batch_%')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd074_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
PRAGMA legacy_alter_table = OFF;
