-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- PRD-078 反向迁移只允许在没有审批业务事实时恢复 revision-13 目录和原预算约束。
-- 回退前后均校验 schema 哈希、租户 RBAC、外键与完整性；任何偏离都原子中止。

-- Refuse rollback before the first persistent DDL unless the complete PRD-078
-- schema, revision-14 catalog, and database health are still canonical.
-- 首个持久 DDL 前创建 schema guard，要求 0071、revision-14 目录和数据库健康状态完全规范。
DROP TABLE IF EXISTS temp.prd078_down_schema_guard;
CREATE TEMP TABLE prd078_down_schema_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd078_down_schema_guard_abort
BEFORE INSERT ON prd078_down_schema_guard FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd078_down_schema_invalid'); END;
-- 汇总版本、schema 哈希、RBAC 目录、临时对象、外键与完整性偏差，非零即拒绝回退。
INSERT INTO prd078_down_schema_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0071' AND status='applied')=1
      AND (SELECT MAX(version) FROM schema_migrations WHERE status='applied')='0071'
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT COUNT(*) FROM (
        SELECT type,name,tbl_name,sql FROM sqlite_schema
        WHERE name IN('rbac_permission_templates','permissions','budget_blocks')
           OR tbl_name IN('approval_tasks','approval_decisions',
              'approval_step_up_challenges','approval_step_up_guards',
              'approval_action_replays','approval_writebacks','approval_events',
              'approval_event_outbox','approval_scheduler_tenants')
           OR name IN('idx_permissions_tenant_category_prd059',
              'trg_tenants_rbac_initialize_prd059','trg_roles_owner_insert_prd059',
              'trg_roles_matrix_update_prd059','trg_role_permissions_owner_insert_prd059',
              'trg_role_permissions_delete_prd059',
              'trg_rbac_permission_templates_insert_prd059',
              'trg_rbac_permission_templates_update_prd059',
              'trg_rbac_permission_templates_delete_prd059',
              'trg_rbac_role_templates_insert_prd059',
              'trg_rbac_role_templates_update_prd059',
              'trg_rbac_role_templates_delete_prd059',
              'trg_permissions_owner_insert_prd059','trg_permissions_update_prd059',
              'trg_permissions_delete_prd059','trg_permissions_rowid_insert_prd059',
              'trg_budget_blocks_owner_insert','trg_budget_blocks_owner_update',
              'trg_budget_blocks_prd052_scope_insert',
              'trg_budget_blocks_prd052_scope_update')
      ))=92 AND (
      SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT type||char(31)||name||char(31)||tbl_name||char(31)||
          coalesce(sql,'<null>') AS record
        FROM sqlite_schema
        WHERE name IN('rbac_permission_templates','permissions','budget_blocks')
           OR tbl_name IN('approval_tasks','approval_decisions',
              'approval_step_up_challenges','approval_step_up_guards',
              'approval_action_replays','approval_writebacks','approval_events',
              'approval_event_outbox','approval_scheduler_tenants')
           OR name IN('idx_permissions_tenant_category_prd059',
              'trg_tenants_rbac_initialize_prd059','trg_roles_owner_insert_prd059',
              'trg_roles_matrix_update_prd059','trg_role_permissions_owner_insert_prd059',
              'trg_role_permissions_delete_prd059',
              'trg_rbac_permission_templates_insert_prd059',
              'trg_rbac_permission_templates_update_prd059',
              'trg_rbac_permission_templates_delete_prd059',
              'trg_rbac_role_templates_insert_prd059',
              'trg_rbac_role_templates_update_prd059',
              'trg_rbac_role_templates_delete_prd059',
              'trg_permissions_owner_insert_prd059','trg_permissions_update_prd059',
              'trg_permissions_delete_prd059','trg_permissions_rowid_insert_prd059',
              'trg_budget_blocks_owner_insert','trg_budget_blocks_owner_update',
              'trg_budget_blocks_prd052_scope_insert',
              'trg_budget_blocks_prd052_scope_update')
        ORDER BY type,name
      ))='6fa8f22701957d69283cee8679d1b7737dc836878f2868c80ecdd4236a286341'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=72
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=14)
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=194
      AND NOT EXISTS(SELECT 1 FROM permissions WHERE catalog_revision!=14 OR status!='active')
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||
          description||char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='8a3781ececf4fe0e3d2607215a4f390d09712b0421051d6e776919c9a9c47c8a'
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='26d89a9cde93032441d74150a8aa3eadda06745103172b90a099c4942794b421'
      AND NOT EXISTS(SELECT tenant.tenant_id,template.permission_code,
          template.category,template.name,template.description,template.risk_level,14,'active'
        FROM tenants tenant CROSS JOIN rbac_permission_templates template
        EXCEPT SELECT tenant_id,code,category,name,description,risk_level,
          catalog_revision,status FROM permissions)
      AND NOT EXISTS(SELECT tenant_id,code,category,name,description,risk_level,
          catalog_revision,status FROM permissions
        EXCEPT SELECT tenant.tenant_id,template.permission_code,
          template.category,template.name,template.description,template.risk_level,14,'active'
        FROM tenants tenant CROSS JOIN rbac_permission_templates template)
      AND NOT EXISTS(SELECT 1 FROM tenants tenant CROSS JOIN rbac_permission_templates template
        WHERE NOT EXISTS(SELECT 1 FROM permissions permission
          WHERE permission.tenant_id=tenant.tenant_id
            AND permission.code=template.permission_code))
      AND NOT EXISTS(SELECT 1 FROM permissions permission
        WHERE NOT EXISTS(SELECT 1 FROM tenants tenant
          JOIN rbac_permission_templates template ON template.permission_code=permission.code
          WHERE tenant.tenant_id=permission.tenant_id
            AND template.category=permission.category AND template.name=permission.name
            AND template.description=permission.description
            AND template.risk_level=permission.risk_level))
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'budget_blocks_prd078_current','rbac_permission_templates_prd078_old',
      'permissions_prd078_old'))=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd078_down_schema_guard_abort;
DROP TABLE prd078_down_schema_guard;

-- Approval tasks, notifications, deliveries, role-change history, or a grant
-- outside the exact revision-14 defaults are durable business facts and must
-- never be silently deleted by downgrade.
CREATE TEMP TABLE prd078_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','finance_admin','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '73bdfac7bfe9fc41b783580f2459d71c87aa8aae02e3359a45918c5024434495'
    WHEN 'finance_admin' THEN '5b2aef1ae06711890946de8894623ea7c740f724cca886ce223b0b175fbc472a'
    WHEN 'security_admin' THEN '7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b'
  END
  AND NOT EXISTS(
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS(
      SELECT 1 FROM role_permissions relation
      JOIN permissions permission ON permission.tenant_id=relation.tenant_id
        AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code))
  AND NOT EXISTS(
    SELECT 1 FROM role_permissions relation
    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS(SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code));

-- 业务事实 guard 在删表前阻止丢失审批、通知、回写、角色变更或已批准预算释放记录。
DROP TABLE IF EXISTS temp.prd078_down_business_guard;
CREATE TEMP TABLE prd078_down_business_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd078_down_business_guard_abort
BEFORE INSERT ON prd078_down_business_guard FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd078_down_business_facts_present'); END;
-- 统计所有 PRD-078 持久业务事实；存在任意一条时整次回退必须失败关闭。
INSERT INTO prd078_down_business_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM approval_event_outbox)
  + (SELECT COUNT(*) FROM approval_events)
  + (SELECT COUNT(*) FROM approval_writebacks)
  + (SELECT COUNT(*) FROM approval_action_replays)
  + (SELECT COUNT(*) FROM approval_step_up_guards)
  + (SELECT COUNT(*) FROM approval_step_up_challenges)
  + (SELECT COUNT(*) FROM approval_decisions)
  + (SELECT COUNT(*) FROM approval_scheduler_tenants)
  + (SELECT COUNT(*) FROM approval_tasks)
  + (SELECT COUNT(*) FROM budget_blocks WHERE release_reason='approval_approved')
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE json_valid(endpoint.event_types_json) AND EXISTS(
        SELECT 1 FROM json_each(endpoint.event_types_json) event
        WHERE event.value IN('approval.created','approval.timeout')))
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE event_type IN('approval.created','approval.timeout'))
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type IN('approval.created','approval.timeout'))
  + (SELECT COUNT(*) FROM webhook_dead_letter_events dead_letter
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=dead_letter.tenant_id
       AND delivery.webhook_delivery_id=dead_letter.webhook_delivery_id
      WHERE delivery.event_type IN('approval.created','approval.timeout'))
  + (SELECT COUNT(*) FROM role_permission_events event
      WHERE (json_valid(event.added_permissions_json) AND EXISTS(
        SELECT 1 FROM json_each(event.added_permissions_json)
        WHERE value IN('approval.read','approval.decide')))
        OR (json_valid(event.removed_permissions_json) AND EXISTS(
        SELECT 1 FROM json_each(event.removed_permissions_json)
        WHERE value IN('approval.read','approval.decide'))))
  + (SELECT COUNT(*) FROM role_permissions relation
      JOIN permissions permission ON permission.tenant_id=relation.tenant_id
        AND permission.permission_id=relation.permission_id
      WHERE permission.code IN('approval.read','approval.decide')
        AND NOT EXISTS(SELECT 1 FROM prd078_roles_to_contract canonical
          WHERE canonical.tenant_id=relation.tenant_id
            AND canonical.role_id=relation.role_id))
  + (SELECT COUNT(*) FROM roles role
      WHERE role.matrix_sha256 IN(
        '73bdfac7bfe9fc41b783580f2459d71c87aa8aae02e3359a45918c5024434495',
        '5b2aef1ae06711890946de8894623ea7c740f724cca886ce223b0b175fbc472a',
        '7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b')
        AND NOT EXISTS(SELECT 1 FROM prd078_roles_to_contract canonical
          WHERE canonical.tenant_id=role.tenant_id AND canonical.role_id=role.role_id))
  + ABS(
      (SELECT COUNT(*) FROM role_permissions relation
       JOIN permissions permission ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
       WHERE permission.code IN('approval.read','approval.decide'))
      - 2*(SELECT COUNT(*) FROM prd078_roles_to_contract));
DROP TRIGGER prd078_down_business_guard_abort;
DROP TABLE prd078_down_business_guard;

-- All dedicated tables are proven empty. Drop them in reverse FK order.
DROP TABLE approval_event_outbox;
DROP TABLE approval_events;
DROP TABLE approval_writebacks;
DROP TABLE approval_action_replays;
DROP TABLE approval_step_up_guards;
DROP TABLE approval_step_up_challenges;
DROP TABLE approval_decisions;
DROP TABLE approval_scheduler_tenants;
DROP TABLE approval_tasks;

-- Contract only exact default revision-14 roles. Custom matrices without the
-- removed permissions remain byte-for-byte untouched.
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

-- 仅从仍精确匹配 revision-14 默认矩阵的角色删除审批授权，不触碰租户自定义矩阵。
DELETE FROM role_permissions
WHERE EXISTS(SELECT 1 FROM prd078_roles_to_contract role
  WHERE role.tenant_id=role_permissions.tenant_id
    AND role.role_id=role_permissions.role_id)
  AND permission_id IN(SELECT permission_id FROM permissions
    WHERE tenant_id=role_permissions.tenant_id
      AND code IN('approval.read','approval.decide'));
-- 提升权限修订并恢复 revision-13 矩阵摘要，使含审批权限的旧认证快照立即失效。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d'
      WHEN 'finance_admin' THEN '63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8'
      WHEN 'security_admin' THEN '38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656'
    END
WHERE role_id IN(SELECT role_id FROM prd078_roles_to_contract);
-- 从固定角色模板移除两项审批权限，其他角色与权限映射完整保留。
DELETE FROM rbac_role_permission_templates
WHERE role_code IN('admin','finance_admin','security_admin')
  AND permission_code IN('approval.read','approval.decide');

CREATE TABLE "rbac_permission_templates_prd078_old"(
  permission_code TEXT PRIMARY KEY CHECK(
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=13)
);
-- 逐行保留非审批模板及 rowid，并恢复 catalog revision 13。
INSERT INTO rbac_permission_templates_prd078_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,13
FROM rbac_permission_templates
WHERE permission_code NOT IN('approval.read','approval.decide');
DROP TABLE rbac_permission_templates;
-- 数据保真复制完成后恢复 revision-13 权限模板权威表。
ALTER TABLE rbac_permission_templates_prd078_old RENAME TO rbac_permission_templates;

CREATE TABLE "permissions_prd078_old"(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=13),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 为所有租户保留非审批权限主键、rowid 和审计时间，并恢复 revision 13。
INSERT INTO permissions_prd078_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       13,status,created_at,updated_at
FROM permissions WHERE code NOT IN('approval.read','approval.decide');
DROP TABLE permissions;
-- 完整复制非审批权限后恢复旧 permissions 表和租户分类索引。
ALTER TABLE permissions_prd078_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059
  ON permissions(tenant_id,category,code);

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
WHEN NOT EXISTS(
  SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level
    AND template.catalog_revision=NEW.catalog_revision
) OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS(SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
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
  OR NEW.code NOT IN(
    'admin','developer','viewer','finance_admin','security_admin','ops_admin'
  )
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d'
    WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8'
    WHEN 'security_admin' THEN '38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656'
    WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current
    WHERE current.role_id=NEW.role_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin'
  OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code
  OR NEW.name!=OLD.name OR NEW.description!=OLD.description
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template
    ON template.role_code=role.code
   AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.status='active' AND permission.permission_id=NEW.permission_id
    AND permission.status='active'
) OR EXISTS(SELECT 1 FROM role_permissions current
  WHERE current.role_permission_id=NEW.role_permission_id
     OR (current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
         AND current.permission_id=NEW.permission_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM roles role
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id
    AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin'
      WHEN 'developer' THEN 'Developer' WHEN 'viewer' THEN 'Viewer'
      WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin'
      WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常和响应缓存。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。'
    END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d'
      WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8'
      WHEN 'security_admin' THEN '38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656'
      WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,
    template.permission_code,template.category,template.name,
    template.description,template.risk_level,13,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  )
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

-- Rebuild BudgetBlock under its original name so sqlite_schema matches 0070.
DROP TRIGGER trg_budget_blocks_owner_insert;
DROP TRIGGER trg_budget_blocks_owner_update;
DROP TRIGGER trg_budget_blocks_prd052_scope_insert;
DROP TRIGGER trg_budget_blocks_prd052_scope_update;
CREATE TABLE budget_blocks_prd078_current AS
SELECT rowid AS source_rowid,* FROM budget_blocks;
DROP TABLE budget_blocks;
CREATE TABLE budget_blocks (
  budget_block_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'project', 'application', 'virtual_key')),
  scope_id TEXT NOT NULL,
  budget_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'released')),
  blocked_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (blocked_cost_cny >= 0),
  budget_cny NUMERIC NOT NULL CHECK (budget_cny > 0),
  blocked_request_id TEXT NOT NULL,
  blocked_trace_id TEXT NOT NULL,
  blocked_at TEXT NOT NULL,
  release_reason TEXT CHECK (
    release_reason IS NULL OR release_reason IN (
      'budget_raised', 'mode_alert_only', 'rule_disabled', 'below_threshold', 'business_day_ended'
    )
  ),
  released_by TEXT,
  release_request_id TEXT,
  release_trace_id TEXT,
  released_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id),
  FOREIGN KEY (released_by) REFERENCES users(user_id)
);
-- 将冻结的 BudgetBlock 行按原 rowid 与租户链路字段完整写回旧约束表。
INSERT INTO budget_blocks(
  rowid,budget_block_id,tenant_id,environment_id,budget_rule_id,scope_type,scope_id,
  budget_date,status,blocked_cost_cny,budget_cny,blocked_request_id,blocked_trace_id,
  blocked_at,release_reason,released_by,release_request_id,release_trace_id,released_at,
  created_at,updated_at
)
SELECT source_rowid,budget_block_id,tenant_id,environment_id,budget_rule_id,scope_type,
  scope_id,budget_date,status,blocked_cost_cny,budget_cny,blocked_request_id,
  blocked_trace_id,blocked_at,release_reason,released_by,release_request_id,
  release_trace_id,released_at,created_at,updated_at
FROM budget_blocks_prd078_current;
DROP TABLE budget_blocks_prd078_current;
CREATE INDEX idx_budget_blocks_environment_date
  ON budget_blocks(tenant_id, environment_id, budget_date, status);
CREATE UNIQUE INDEX idx_budget_blocks_one_active_rule_day
  ON budget_blocks(tenant_id, budget_rule_id, budget_date)
  WHERE status='active';
CREATE INDEX idx_budget_blocks_tenant_rule_status
  ON budget_blocks(tenant_id, budget_rule_id, status, blocked_at DESC);
CREATE TRIGGER trg_budget_blocks_owner_insert
BEFORE INSERT ON budget_blocks
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules r
  JOIN environments e ON e.environment_id=r.environment_id AND e.tenant_id=r.tenant_id
  WHERE r.budget_rule_id=NEW.budget_rule_id
    AND r.tenant_id=NEW.tenant_id
    AND r.environment_id=NEW.environment_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_block_owner_mismatch');
END;
CREATE TRIGGER trg_budget_blocks_owner_update
BEFORE UPDATE OF tenant_id, environment_id, budget_rule_id ON budget_blocks
WHEN NOT EXISTS (
       SELECT 1 FROM budget_rules r
       JOIN environments e ON e.environment_id=r.environment_id AND e.tenant_id=r.tenant_id
       WHERE r.budget_rule_id=NEW.budget_rule_id
         AND r.tenant_id=NEW.tenant_id
         AND r.environment_id=NEW.environment_id
     )
  OR EXISTS (
       SELECT 1 FROM budget_block_events event
       WHERE event.budget_block_id=OLD.budget_block_id
         AND (
           event.tenant_id<>NEW.tenant_id
           OR event.environment_id<>NEW.environment_id
           OR event.budget_rule_id<>NEW.budget_rule_id
         )
     )
BEGIN
  SELECT RAISE(ABORT, 'budget_block_owner_mismatch');
END;
CREATE TRIGGER trg_budget_blocks_prd052_scope_insert
BEFORE INSERT ON budget_blocks
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_block_owner_scope_mismatch');
END;
CREATE TRIGGER trg_budget_blocks_prd052_scope_update
BEFORE UPDATE OF tenant_id, environment_id, budget_rule_id, scope_type, scope_id
ON budget_blocks
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id
    AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id
)
BEGIN
  SELECT RAISE(ABORT, 'budget_block_owner_scope_mismatch');
END;

DROP TABLE prd078_roles_to_contract;
-- 最终 guard 在提交前验证 revision-13 目录、审批对象清理和数据库健康状态。
DROP TABLE IF EXISTS temp.prd078_down_final_guard;
CREATE TEMP TABLE prd078_down_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd078_down_final_guard_abort
BEFORE INSERT ON prd078_down_final_guard FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd078_down_final_invalid'); END;
-- 汇总回退后 RBAC、schema、预算约束、外键和完整性问题，非零即回滚全部变更。
INSERT INTO prd078_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=70
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=13)
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=188
      AND NOT EXISTS(SELECT 1 FROM permissions WHERE catalog_revision!=13 OR status!='active')
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates
        WHERE permission_code IN('approval.read','approval.decide'))
      AND NOT EXISTS(SELECT 1 FROM permissions
        WHERE code IN('approval.read','approval.decide'))
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE name LIKE 'approval_%' OR tbl_name LIKE 'approval_%')=0
    THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='budget_blocks'),'approval_approved')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT COUNT(*) FROM sqlite_schema
      WHERE tbl_name IN('rbac_permission_templates','permissions','budget_blocks')
         OR name IN('trg_tenants_rbac_initialize_prd059','trg_roles_owner_insert_prd059',
           'trg_roles_matrix_update_prd059','trg_role_permissions_owner_insert_prd059',
           'trg_role_permissions_delete_prd059','trg_rbac_role_templates_insert_prd059',
           'trg_rbac_role_templates_update_prd059','trg_rbac_role_templates_delete_prd059')
      )=31 AND (
      SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT type||char(31)||name||char(31)||tbl_name||char(31)||
          coalesce(sql,'<null>') AS record
        FROM sqlite_schema
        WHERE tbl_name IN('rbac_permission_templates','permissions','budget_blocks')
           OR name IN('trg_tenants_rbac_initialize_prd059','trg_roles_owner_insert_prd059',
             'trg_roles_matrix_update_prd059','trg_role_permissions_owner_insert_prd059',
             'trg_role_permissions_delete_prd059','trg_rbac_role_templates_insert_prd059',
             'trg_rbac_role_templates_update_prd059','trg_rbac_role_templates_delete_prd059')
        ORDER BY type,name
      ))='66b0cec689b0e5bde165df609aa83a8850623dee40d351a7c3054f22ccce8cdb'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd078_down_final_guard_abort;
DROP TABLE prd078_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
