-- PRD-079 正向迁移：套餐、单次试用、四类额度、并发预留、结算分摊与恢复型事件 outbox。
-- 金额只保存 CNY 微元整数；Prompt、Response、Provider Key、Token、幂等键明文、调整原因
-- 以外的请求正文和底层异常不得进入事件 payload、trigger 错误或验收证据。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首个持久 DDL 前验证 0071、revision-14 目录与默认矩阵哈希，并拒绝损坏数据库继续升级。
DROP TABLE IF EXISTS temp.prd079_up_guard;
CREATE TEMP TABLE prd079_up_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd079_up_guard_abort
BEFORE INSERT ON prd079_up_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd079_up_baseline_invalid'); END;
-- 写入基线问题计数并由临时触发器原子阻断任何不满足前置条件的升级。
INSERT INTO prd079_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0071'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=72
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=14)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='8a3781ececf4fe0e3d2607215a4f390d09712b0421051d6e776919c9a9c47c8a'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=194
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='26d89a9cde93032441d74150a8aa3eadda06745103172b90a099c4942794b421'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM sqlite_schema WHERE name IN(
      'plans','quota_buckets','quota_adjustments','quota_reservations',
      'quota_reservation_items','quota_usage_settlements','quota_usage_allocations',
      'quota_events','quota_event_outbox')) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd079_up_guard_abort;
DROP TABLE prd079_up_guard;

-- 只扩展仍精确匹配 revision-14 默认矩阵的 admin/finance_admin；定制矩阵保持原样。
CREATE TEMP TABLE prd079_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','finance_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '73bdfac7bfe9fc41b783580f2459d71c87aa8aae02e3359a45918c5024434495'
    WHEN 'finance_admin' THEN '5b2aef1ae06711890946de8894623ea7c740f724cca886ce223b0b175fbc472a'
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

-- 暂停不可变 RBAC trigger；目录切换仍全部位于同一个 IMMEDIATE 事务并在末尾复核。
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

CREATE TABLE rbac_permission_templates_prd079_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=15)
);
-- 原样复制既有权限模板行号与内容，仅把目录版本提升到 revision 15。
INSERT INTO rbac_permission_templates_prd079_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,15
FROM rbac_permission_templates;
-- 四项权限逐一对应套餐读写与额度读写，不能由 FinOps 权限或前端入口隐式推导。
INSERT INTO rbac_permission_templates_prd079_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('billing.plan.read','billing','读取套餐','读取当前租户套餐与试用状态。','read',15),
  ('billing.plan.write','billing','管理套餐','创建或修改当前租户套餐与试用周期。','critical',15),
  ('billing.quota.read','billing','读取额度','读取当前租户额度桶与分类消费摘要。','read',15),
  ('billing.quota.write','billing','调整额度','创建当前租户可审计的额度增减事实。','critical',15);
DROP TABLE rbac_permission_templates;
-- 以保留数据的新表原子替换旧权限模板表，继续维持原表名称合同。
ALTER TABLE rbac_permission_templates_prd079_new RENAME TO rbac_permission_templates;

-- 只为默认角色目录追加四项套餐与额度权限，不隐式扩展其他角色。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','billing.plan.read',1),('admin','billing.plan.write',1),
  ('admin','billing.quota.read',1),('admin','billing.quota.write',1),
  ('finance_admin','billing.plan.read',1),('finance_admin','billing.plan.write',1),
  ('finance_admin','billing.quota.read',1),('finance_admin','billing.quota.write',1);

CREATE TABLE permissions_prd079_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=15),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 原样复制各租户既有权限事实，仅同步目录版本为 revision 15。
INSERT INTO permissions_prd079_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       15,status,created_at,updated_at
FROM permissions;
-- 为每个租户生成四项独立权限行，并保持复合租户所有权约束。
INSERT INTO permissions_prd079_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       15,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant CROSS JOIN rbac_permission_templates template
WHERE template.permission_code IN(
  'billing.plan.read','billing.plan.write','billing.quota.read','billing.quota.write');
DROP TABLE permissions;
-- 用已完整复制并扩展的数据表替换旧权限表，随后恢复租户分类索引。
ALTER TABLE permissions_prd079_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

-- 仅向迁移前精确匹配默认矩阵的财务角色写入新增权限关系。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd079_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id
WHERE permission.code IN(
  'billing.plan.read','billing.plan.write','billing.quota.read','billing.quota.write');
-- 权限关系变化后递增角色 revision 并写入对应的冻结矩阵摘要。
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '9d92ea073fcfb7425ef0d46e881fc16cf50fdbb0192560812c0e8c5fe9c6946f'
    WHEN 'finance_admin' THEN '5311448a5233c5f2430620095b90bd33af8489e26bab0357c861437ae1916c39'
  END
WHERE role_id IN(SELECT role_id FROM prd079_roles_to_expand);

-- 恢复 revision-15 RBAC 不可变目录、owner 和新租户初始化守卫。
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
  OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '9d92ea073fcfb7425ef0d46e881fc16cf50fdbb0192560812c0e8c5fe9c6946f'
    WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '5311448a5233c5f2430620095b90bd33af8489e26bab0357c861437ae1916c39'
    WHEN 'security_admin' THEN '7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b'
    WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current WHERE current.role_id=NEW.role_id
    OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin'
  OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64
  OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
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
WHEN EXISTS(SELECT 1 FROM roles role WHERE role.tenant_id=OLD.tenant_id
  AND role.role_id=OLD.role_id AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户按 revision 15 冻结目录创建六个默认角色及其矩阵摘要。
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
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常和响应缓存。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。'
    END,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '9d92ea073fcfb7425ef0d46e881fc16cf50fdbb0192560812c0e8c5fe9c6946f'
      WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '5311448a5233c5f2430620095b90bd33af8489e26bab0357c861437ae1916c39'
      WHEN 'security_admin' THEN '7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b'
      WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 新租户复制完整权限目录，所有权限行均绑定 NEW tenant_id。
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,15,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  -- 新租户只按冻结角色模板写入默认授权关系，禁止跨租户关联。
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code WHERE role.tenant_id=NEW.tenant_id;
END;
DROP TABLE prd079_roles_to_expand;

-- PRD-079 AuditLog preservation rebuild; only the action/object allowlist changes.
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS audit_logs_prd079_new;
CREATE TABLE audit_logs_prd079_new (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created','provider.updated',
      'provider_key.created','provider_key.updated',
      'virtual_key.created','virtual_key.updated',
      'budget_rule.created','budget_rule.updated',
      'dlp_rule.created','dlp_rule.updated',
      'webhook_delivery.replayed','webhook_endpoint.secret_rotated',
      'webhook_dead_letter.archived',
      'usage_export.requested','usage_export.downloaded',
      'policy.created','policy.updated',
      'policy_snapshot.published','policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created','routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created','cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged','cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created','error_map.updated',
      'audit.exported','role.permission_changed',
      'user.invited','invitation.accepted',
      'config.imported','config.exported',
      'prompt_template.created','prompt_template.updated',
      'prompt_template.version_published',
      'policy_template.applied','policy_template.rolled_back',
      'experiment.created',
      'session_risk.freeze_session','session_risk.freeze_virtual_key',
      'session_risk.dismiss','session_risk.release_session',
      'session_risk.release_virtual_key',
      'response_cache.policy_created','response_cache.policy_updated',
      'response_cache.invalidated',
      'admin_assistant.queried',
      'plan.created','plan.updated','quota.adjustment_created'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider','provider_key','virtual_key','budget_rule','dlp_rule',
      'webhook_delivery','webhook_endpoint','webhook_dead_letter',
      'usage_export_job','policy','policy_snapshot','policy_explanation',
      'routing_policy','model_quality_score','cache_affinity',
      'finops_adjustment','cost_anomaly','provider_error_map','audit_export',
      'role_permission','invitation','config_transfer',
      'prompt_template','prompt_template_version',
      'policy_template_application','session_risk','experiment',
      'response_cache_policy','response_cache_invalidation',
      'assistant_query','plan','quota_adjustment'
    )
  ),
  object_id TEXT NOT NULL,
  object_name TEXT,
  before_summary_json TEXT NOT NULL CHECK (
    json_valid(before_summary_json) AND json_type(before_summary_json)='object'
  ),
  after_summary_json TEXT NOT NULL CHECK (
    json_valid(after_summary_json) AND json_type(after_summary_json)='object'
  ),
  changed_fields_json TEXT NOT NULL CHECK (
    json_valid(changed_fields_json) AND json_type(changed_fields_json)='array'
  ),
  client_ip TEXT,
  user_agent TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (tenant_id,audit_log_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO audit_logs_prd079_new(rowid,audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action, object_type, object_id, object_name, before_summary_json, after_summary_json, changed_fields_json, client_ip, user_agent, request_id, trace_id, created_at)
SELECT rowid,audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action, object_type, object_id, object_name, before_summary_json, after_summary_json, changed_fields_json, client_ip, user_agent, request_id, trace_id, created_at FROM audit_logs;
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd079_audit_copy_guard;
CREATE TEMP TABLE prd079_audit_copy_guard(issue_count INTEGER NOT NULL
  CONSTRAINT prd079_audit_copy_guard_ok CHECK(issue_count=0));
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd079_audit_copy_guard(issue_count)
SELECT ABS((SELECT COUNT(*) FROM audit_logs)
  -(SELECT COUNT(*) FROM audit_logs_prd079_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd079_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs_prd079_new
      EXCEPT SELECT rowid,* FROM audit_logs));
DROP TABLE prd079_audit_copy_guard;
DROP TABLE audit_logs;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE audit_logs_prd079_new RENAME TO audit_logs;
CREATE UNIQUE INDEX idx_audit_logs_config_transfer_request_unique
  ON audit_logs(tenant_id,actor_user_id,action,request_id)
  WHERE action IN ('config.imported','config.exported');
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_experiment_owner_unique
  ON audit_logs(tenant_id,object_id) WHERE action='experiment.created';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id,object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_invitation_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action IN ('user.invited','invitation.accepted');
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_policy_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN ('policy_template.applied','policy_template.rolled_back');
CREATE UNIQUE INDEX idx_audit_logs_prompt_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN (
    'prompt_template.created','prompt_template.updated',
    'prompt_template.version_published'
  );
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';
CREATE UNIQUE INDEX idx_audit_logs_session_risk_action_unique_prd070
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE action IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  );
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id,action,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id,actor_user_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id,object_type,object_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id,request_id);
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id,created_at DESC,audit_log_id DESC);
CREATE UNIQUE INDEX uq_audit_logs_owner_prd068
  ON audit_logs(tenant_id,audit_log_id);
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 每条新审计事实都登记导出序列，配置迁移审计不得绕过既有增量导出链。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.user_id=NEW.actor_user_id AND user.tenant_id=NEW.tenant_id
)
OR (
  NEW.action='finops.adjustment_created'
  AND (
    NEW.object_type!='finops_adjustment'
    OR NOT EXISTS (
      SELECT 1 FROM finops_adjustments adjustment
      WHERE adjustment.finops_adjustment_id=NEW.object_id
        AND adjustment.tenant_id=NEW.tenant_id
        AND adjustment.actor_user_id=NEW.actor_user_id
    )
  )
)
OR (NEW.object_type='finops_adjustment' AND NEW.action!='finops.adjustment_created')
OR (
  NEW.action IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
  AND (
    NEW.object_type!='cost_anomaly'
    OR NOT EXISTS (
      SELECT 1 FROM cost_anomaly_actions action
      JOIN cost_anomalies anomaly
        ON anomaly.tenant_id=action.tenant_id
       AND anomaly.cost_anomaly_id=action.cost_anomaly_id
      WHERE action.tenant_id=NEW.tenant_id
        AND action.cost_anomaly_id=NEW.object_id
        AND action.actor_user_id=NEW.actor_user_id
        AND action.operation_request_id=NEW.request_id
        AND action.operation_trace_id=NEW.trace_id
        AND CASE action.action_type
          WHEN 'acknowledge' THEN 'cost_anomaly.acknowledged'
          WHEN 'dismiss' THEN 'cost_anomaly.dismissed'
          WHEN 'apply_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_applied'
          WHEN 'release_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_released'
        END=NEW.action
    )
  )
)
OR (
  NEW.object_type='cost_anomaly'
  AND NEW.action NOT IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
)
OR (
  NEW.action IN ('error_map.created','error_map.updated')
  AND (
    NEW.object_type!='provider_error_map'
    OR NOT EXISTS (
      SELECT 1 FROM provider_error_map_events event
      JOIN provider_error_map_versions version
        ON version.tenant_id=event.tenant_id
       AND version.provider_error_map_version_id=event.provider_error_map_version_id
      JOIN provider_error_maps map
        ON map.tenant_id=event.tenant_id
       AND map.provider_error_map_id=event.provider_error_map_id
       AND map.current_version=event.rule_version
      WHERE event.tenant_id=NEW.tenant_id
        AND event.provider_error_map_id=NEW.object_id
        AND event.actor_user_id=NEW.actor_user_id
        AND event.request_id=NEW.request_id
        AND event.trace_id=NEW.trace_id
        AND (
          (NEW.action='error_map.created' AND event.operation='create')
          OR (NEW.action='error_map.updated' AND event.operation IN ('update','enable','disable'))
        )
    )
  )
)
OR (
  NEW.object_type='provider_error_map'
  AND NEW.action NOT IN ('error_map.created','error_map.updated')
)
OR (
  NEW.action='webhook_dead_letter.archived'
  AND (
    NEW.object_type!='webhook_dead_letter'
    OR NOT EXISTS (
      SELECT 1 FROM webhook_dead_letter_events dead_letter
      WHERE dead_letter.tenant_id=NEW.tenant_id
        AND dead_letter.dead_letter_event_id=NEW.object_id
        AND dead_letter.status='archived'
        AND dead_letter.archived_by=NEW.actor_user_id
    )
  )
)
OR (
  NEW.object_type='webhook_dead_letter'
  AND NEW.action!='webhook_dead_letter.archived'
)
OR (
  NEW.action='audit.exported'
  AND (
    NEW.object_type!='audit_export'
    OR NOT EXISTS (
      SELECT 1 FROM audit_exports export
      WHERE export.tenant_id=NEW.tenant_id
        AND export.audit_export_id=NEW.object_id
        AND export.audit_log_id=NEW.audit_log_id
        AND export.requested_by=NEW.actor_user_id
        AND export.request_id=NEW.request_id
        AND export.trace_id=NEW.trace_id
        AND export.created_at=NEW.created_at
    )
  )
)
OR (NEW.object_type='audit_export' AND NEW.action!='audit.exported')
OR (
  EXISTS (SELECT 1 FROM audit_exports export WHERE export.audit_log_id=NEW.audit_log_id)
  AND NOT (
    NEW.action='audit.exported' AND NEW.object_type='audit_export'
    AND EXISTS (
      SELECT 1 FROM audit_exports export
      WHERE export.tenant_id=NEW.tenant_id
        AND export.audit_export_id=NEW.object_id
        AND export.audit_log_id=NEW.audit_log_id
        AND export.requested_by=NEW.actor_user_id
        AND export.request_id=NEW.request_id
        AND export.trace_id=NEW.trace_id
        AND export.created_at=NEW.created_at
    )
  )
)
OR (
  NEW.action='role.permission_changed'
  AND (
    NEW.object_type!='role_permission'
    OR NOT EXISTS (
      SELECT 1 FROM roles role
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.object_id
        AND role.code!='admin' AND role.status='active'
        AND role.permission_revision=json_extract(NEW.after_summary_json,'$.permission_revision')
        AND role.matrix_sha256=json_extract(NEW.after_summary_json,'$.matrix_sha256')
        AND json_extract(NEW.before_summary_json,'$.permission_revision')+1=role.permission_revision
    )
  )
)
OR (NEW.object_type='role_permission' AND NEW.action!='role.permission_changed')
OR (
  NEW.action='config.imported'
  AND (
    NEW.object_type!='config_transfer' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM config_imports import_fact
      JOIN users actor
        ON actor.tenant_id=import_fact.tenant_id
       AND actor.user_id=import_fact.actor_user_id
      WHERE import_fact.tenant_id=NEW.tenant_id
        AND import_fact.config_import_id=NEW.object_id
        AND import_fact.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND import_fact.request_id=NEW.request_id
        AND import_fact.trace_id=NEW.trace_id
        AND import_fact.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=12
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'schema_version','manifest_sha256',
            'providers_created_count','providers_unchanged_count',
            'models_created_count','models_unchanged_count',
            'aliases_created_count','aliases_unchanged_count',
            'virtual_keys_created_count','virtual_keys_unchanged_count',
            'generated_virtual_key_count','disabled_virtual_key_count'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.schema_version')=import_fact.schema_version
        AND json_extract(NEW.after_summary_json,'$.manifest_sha256')=import_fact.manifest_sha256
        AND json_extract(NEW.after_summary_json,'$.providers_created_count')
          =import_fact.providers_created_count
        AND json_extract(NEW.after_summary_json,'$.providers_unchanged_count')
          =import_fact.providers_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.models_created_count')
          =import_fact.models_created_count
        AND json_extract(NEW.after_summary_json,'$.models_unchanged_count')
          =import_fact.models_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.aliases_created_count')
          =import_fact.aliases_created_count
        AND json_extract(NEW.after_summary_json,'$.aliases_unchanged_count')
          =import_fact.aliases_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_created_count')
          =import_fact.virtual_keys_created_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_unchanged_count')
          =import_fact.virtual_keys_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.generated_virtual_key_count')
          =import_fact.generated_virtual_key_count
        AND json_extract(NEW.after_summary_json,'$.disabled_virtual_key_count')
          =import_fact.disabled_virtual_key_count
        AND json(NEW.changed_fields_json)=json(
          '["aliases_created_count","aliases_unchanged_count",' ||
          '"disabled_virtual_key_count","generated_virtual_key_count",' ||
          '"manifest_sha256","models_created_count","models_unchanged_count",' ||
          '"providers_created_count","providers_unchanged_count","schema_version",' ||
          '"virtual_keys_created_count","virtual_keys_unchanged_count"]'
        )
        AND (SELECT COUNT(*) FROM config_import_rows row_fact
             WHERE row_fact.tenant_id=import_fact.tenant_id
               AND row_fact.config_import_id=import_fact.config_import_id)
          =import_fact.providers_created_count+import_fact.providers_unchanged_count
           +import_fact.models_created_count+import_fact.models_unchanged_count
           +import_fact.aliases_created_count+import_fact.aliases_unchanged_count
           +import_fact.virtual_keys_created_count+import_fact.virtual_keys_unchanged_count
    )
  )
)
OR (
  NEW.action='config.exported'
  AND (
    NEW.object_type!='config_transfer' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM config_exports export_fact
      JOIN users actor
        ON actor.tenant_id=export_fact.tenant_id
       AND actor.user_id=export_fact.actor_user_id
      WHERE export_fact.tenant_id=NEW.tenant_id
        AND export_fact.config_export_id=NEW.object_id
        AND export_fact.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND export_fact.request_id=NEW.request_id
        AND export_fact.trace_id=NEW.trace_id
        AND export_fact.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=6
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'schema_version','manifest_sha256','providers_count','models_count',
            'aliases_count','virtual_keys_count'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.schema_version')=export_fact.schema_version
        AND json_extract(NEW.after_summary_json,'$.manifest_sha256')=export_fact.manifest_sha256
        AND json_extract(NEW.after_summary_json,'$.providers_count')=export_fact.providers_count
        AND json_extract(NEW.after_summary_json,'$.models_count')=export_fact.models_count
        AND json_extract(NEW.after_summary_json,'$.aliases_count')=export_fact.aliases_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_count')
          =export_fact.virtual_keys_count

        AND json(NEW.changed_fields_json)=json(
          '["aliases_count","manifest_sha256","models_count","providers_count",' ||
          '"schema_version","virtual_keys_count"]'
        )
    )
  )
)
OR (
  NEW.object_type='config_transfer'
  AND NEW.action NOT IN ('config.imported','config.exported')
)
OR (
  NEW.action IN ('user.invited','invitation.accepted')
  AND (
    NEW.object_type!='invitation' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM invitations invitation
      JOIN tenants tenant
        ON tenant.tenant_id=invitation.tenant_id
      JOIN roles target_role
        ON target_role.tenant_id=invitation.tenant_id
       AND target_role.code=invitation.role_code
      JOIN users actor
        ON actor.tenant_id=invitation.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE invitation.tenant_id=NEW.tenant_id
        AND invitation.invitation_id=NEW.object_id
        AND tenant.status='active' AND target_role.status='active'
        AND actor.role=NEW.actor_role AND actor.status='active'
        AND (
          (
            NEW.action='user.invited'
            AND NEW.actor_email=actor.email
            AND invitation.status='pending'
            AND invitation.invited_by=NEW.actor_user_id
            AND invitation.created_at=NEW.created_at
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
            AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(NEW.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.after_summary_json,'$.status')='pending'
            AND json_extract(NEW.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(NEW.after_summary_json,'$.accepted_user_id')='null'
            AND json(NEW.changed_fields_json)
              =json('["email_fingerprint","expires_at","role_code","status"]')
          )
          OR
          (
            NEW.action='invitation.accepted'
            AND NEW.actor_email='invitation-acceptor@redacted.invalid'
            AND invitation.status='accepted'
            AND invitation.accepted_user_id=NEW.actor_user_id
            AND actor.role=invitation.role_code
            AND invitation.email=actor.email
            AND invitation.accepted_at=NEW.created_at
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=5
            AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.before_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(NEW.before_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.before_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.before_summary_json,'$.status')='pending'
            AND json_extract(NEW.before_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(NEW.before_summary_json,'$.accepted_user_id')='null'
            AND json_extract(NEW.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.after_summary_json,'$.status')='accepted'
            AND json_extract(NEW.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_extract(NEW.after_summary_json,'$.accepted_user_id')
              =invitation.accepted_user_id
            AND json(NEW.changed_fields_json)=json('["accepted_user_id","status"]')
          )
        )
    )
  )
)
OR (
  NEW.object_type='invitation'
  AND NEW.action NOT IN ('user.invited','invitation.accepted')
)
-- Template create/update 必须绑定当前同租户 head、actor 和安全摘要白名单。
OR (
  NEW.action IN ('prompt_template.created','prompt_template.updated')
  AND (
    NEW.object_type!='prompt_template'
    OR NOT EXISTS (
      SELECT 1
      FROM prompt_templates template
      JOIN prompt_template_versions version
        ON version.tenant_id=template.tenant_id
       AND version.prompt_template_id=template.prompt_template_id
       AND version.prompt_template_version_id=template.current_version_id
       AND version.state='published'
      JOIN users actor
        ON actor.tenant_id=template.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE template.tenant_id=NEW.tenant_id
        AND template.prompt_template_id=NEW.object_id
        AND template.updated_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active' AND template.name=NEW.object_name
        AND json_extract(NEW.after_summary_json,'$.status')=template.status
        AND json_extract(NEW.after_summary_json,'$.revision')=template.revision
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND (
          NEW.action!='prompt_template.created'
          OR (
            (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
            AND json_extract(NEW.after_summary_json,'$.version_number')
              =version.version_number
            AND json_extract(NEW.after_summary_json,'$.project_scope')
              =version.project_scope
            AND json_extract(NEW.after_summary_json,'$.project_count')=(
              SELECT COUNT(*) FROM prompt_template_version_projects binding
              WHERE binding.tenant_id=version.tenant_id
                AND binding.prompt_template_version_id=version.prompt_template_version_id
            )
            AND json_extract(NEW.after_summary_json,'$.recommended_model_count')
              =version.recommended_model_count
            AND json_extract(NEW.after_summary_json,'$.definition_size_bytes')
              =version.definition_size_bytes
            AND json_extract(NEW.after_summary_json,'$.variable_count')
              =version.variable_count
            AND json_extract(NEW.after_summary_json,'$.change_summary_hmac')
              =version.change_summary_hmac
          )
        )
        AND (
          NEW.action!='prompt_template.updated'
          OR json_extract(NEW.before_summary_json,'$.revision')=template.revision-1
        )
    )
  )
)
-- Version publication Audit 绑定已封印版本、当前 head、publisher 和全部安全计数。
OR (
  NEW.action='prompt_template.version_published'
  AND (
    NEW.object_type!='prompt_template_version'
    OR NOT EXISTS (
      SELECT 1
      FROM prompt_template_versions version
      JOIN prompt_templates template
        ON template.tenant_id=version.tenant_id
       AND template.prompt_template_id=version.prompt_template_id
       AND template.current_version_id=version.prompt_template_version_id
      JOIN users actor
        ON actor.tenant_id=version.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE version.tenant_id=NEW.tenant_id
        AND version.prompt_template_version_id=NEW.object_id
        AND version.state='published' AND version.created_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active' AND template.name=NEW.object_name
        AND NEW.created_at=version.published_at
        AND json_extract(NEW.after_summary_json,'$.status')=template.status
        AND json_extract(NEW.after_summary_json,'$.revision')=template.revision
        AND json_extract(NEW.after_summary_json,'$.version_number')=version.version_number
        AND json_extract(NEW.after_summary_json,'$.project_scope')=version.project_scope
        AND json_extract(NEW.after_summary_json,'$.project_count')=(
          SELECT COUNT(*) FROM prompt_template_version_projects binding
          WHERE binding.tenant_id=version.tenant_id
            AND binding.prompt_template_version_id=version.prompt_template_version_id
        )
        AND json_extract(NEW.after_summary_json,'$.recommended_model_count')
          =version.recommended_model_count
        AND json_extract(NEW.after_summary_json,'$.definition_size_bytes')
          =version.definition_size_bytes
        AND json_extract(NEW.after_summary_json,'$.variable_count')=version.variable_count
        AND json_extract(NEW.after_summary_json,'$.change_summary_hmac')
          =version.change_summary_hmac
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
    )
  )
)
OR (
  NEW.object_type='prompt_template'
  AND NEW.action NOT IN ('prompt_template.created','prompt_template.updated')
)
OR (
  NEW.object_type='prompt_template_version'
  AND NEW.action!='prompt_template.version_published'
)
OR (
  NEW.action='experiment.created'
  AND (
    NEW.object_type!='experiment' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM model_experiments experiment
      JOIN users actor
        ON actor.tenant_id=experiment.tenant_id
       AND actor.user_id=experiment.created_by
      WHERE experiment.tenant_id=NEW.tenant_id
        AND experiment.experiment_id=NEW.object_id
        AND experiment.created_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND experiment.operation_request_id=NEW.request_id
        AND experiment.operation_trace_id=NEW.trace_id
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=12
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'experiment_id','project_id','environment_id','model_alias_id',
            'model_a_id','model_b_id','target_sample_count','traffic_sample_bps',
            'model_a_allocation_bps','sampling_duration_seconds',
            'feedback_collection_enabled','feedback_window_seconds'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.experiment_id')
          =experiment.experiment_id
        AND json_extract(NEW.after_summary_json,'$.project_id')=experiment.project_id
        AND json_extract(NEW.after_summary_json,'$.environment_id')
          =experiment.environment_id
        AND json_extract(NEW.after_summary_json,'$.model_alias_id')
          =experiment.model_alias_id
        AND json_extract(NEW.after_summary_json,'$.model_a_id')=experiment.model_a_id
        AND json_extract(NEW.after_summary_json,'$.model_b_id')=experiment.model_b_id
        AND json_extract(NEW.after_summary_json,'$.target_sample_count')
          =experiment.target_sample_count
        AND json_extract(NEW.after_summary_json,'$.traffic_sample_bps')
          =experiment.traffic_sample_bps
        AND json_extract(NEW.after_summary_json,'$.model_a_allocation_bps')
          =experiment.model_a_allocation_bps
        AND json_extract(NEW.after_summary_json,'$.sampling_duration_seconds')
          =experiment.sampling_duration_seconds
        AND json_extract(NEW.after_summary_json,'$.feedback_collection_enabled')
          =experiment.feedback_collection_enabled
        AND json_extract(NEW.after_summary_json,'$.feedback_window_seconds')
          =experiment.feedback_window_seconds
        AND json(NEW.changed_fields_json)=json(
          '["environment_id","experiment_id","feedback_collection_enabled",' ||
          '"feedback_window_seconds","model_a_allocation_bps","model_a_id",' ||
          '"model_alias_id","model_b_id","project_id","sampling_duration_seconds",' ||
          '"target_sample_count","traffic_sample_bps"]'
        )
    )
  )
)
OR (NEW.object_type='experiment' AND NEW.action!='experiment.created')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_policy_template_owner_insert_prd068
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN ('policy_template.applied','policy_template.rolled_back')
  AND (
    NEW.object_type!='policy_template_application'
    OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM policy_template_applications application
      JOIN policy_templates template
        ON template.tenant_id=application.tenant_id
       AND template.policy_template_id=application.policy_template_id
      WHERE application.tenant_id=NEW.tenant_id
        AND application.application_id=NEW.object_id
        AND (
          (NEW.action='policy_template.applied'
            AND application.status IN ('applied','rolled_back')
            AND application.apply_audit_log_id=NEW.audit_log_id
            AND application.actor_user_id=NEW.actor_user_id
            AND application.request_id=NEW.request_id
            AND application.trace_id=NEW.trace_id
            AND application.applied_as_of=NEW.created_at)
          OR
          (NEW.action='policy_template.rolled_back'
            AND application.status='rolled_back'
            AND application.rollback_apply_audit_log_id=NEW.audit_log_id
            AND application.rollback_actor_user_id=NEW.actor_user_id
            AND application.rollback_request_id=NEW.request_id
            AND application.rollback_trace_id=NEW.trace_id
            AND application.rolled_back_at=NEW.created_at)
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.template_code')=template.template_code
        AND json_extract(NEW.after_summary_json,'$.template_version')=application.template_version
        AND json_extract(NEW.after_summary_json,'$.application_id')=application.application_id
        AND json_extract(NEW.after_summary_json,'$.generated_policy_id')=application.generated_policy_id
        AND json_extract(NEW.after_summary_json,'$.previous_snapshot_version')
          =application.previous_snapshot_version
        AND json_extract(NEW.after_summary_json,'$.result_snapshot_version')
          =application.result_snapshot_version
        AND json_extract(NEW.after_summary_json,'$.impact_hash')=application.impact_hash
        AND json_extract(NEW.after_summary_json,'$.bindings_hash')=application.bindings_hash
        AND json_extract(NEW.after_summary_json,'$.changed_resource_types')
          ='policy,policy_snapshot'
        AND (
          (
            NEW.action='policy_template.applied'
            AND NEW.before_summary_json='{}'
            AND json_extract(NEW.after_summary_json,'$.status')='applied'
            AND json_type(NEW.after_summary_json,'$.rollback_snapshot_version') IS NULL
          )
          OR
          (
            NEW.action='policy_template.rolled_back'
            AND json_extract(NEW.before_summary_json,'$.status')='applied'
            AND json_extract(NEW.before_summary_json,'$.template_code')=template.template_code
            AND json_extract(NEW.before_summary_json,'$.template_version')
              =application.template_version
            AND json_extract(NEW.before_summary_json,'$.application_id')
              =application.application_id
            AND json_extract(NEW.before_summary_json,'$.generated_policy_id')
              =application.generated_policy_id
            AND json_extract(NEW.before_summary_json,'$.previous_snapshot_version')
              =application.previous_snapshot_version
            AND json_extract(NEW.before_summary_json,'$.result_snapshot_version')
              =application.result_snapshot_version
            AND json_extract(NEW.before_summary_json,'$.impact_hash')=application.impact_hash
            AND json_extract(NEW.before_summary_json,'$.bindings_hash')=application.bindings_hash
            AND json_extract(NEW.before_summary_json,'$.changed_resource_types')
              ='policy,policy_snapshot'
            AND json_type(NEW.before_summary_json,'$.rollback_snapshot_version') IS NULL
            AND json_extract(NEW.after_summary_json,'$.status')='rolled_back'
            AND json_extract(NEW.after_summary_json,'$.rollback_snapshot_version')
              =application.rollback_snapshot_version
          )
        )
    )
  )
)
OR (
  NEW.object_type='policy_template_application'
  AND NEW.action NOT IN ('policy_template.applied','policy_template.rolled_back')
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_prd050_conflict_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_logs current
  WHERE current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.action IN ('policy.dry_run_executed','model_quality_score.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='finops.adjustment_created'
      AND current.action='finops.adjustment_created'
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
    )
    OR (
      NEW.action IN (
        'cost_anomaly.acknowledged','cost_anomaly.dismissed',
        'cost_anomaly.temporary_rate_limit_applied',
        'cost_anomaly.temporary_rate_limit_released'
      )
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('error_map.created','error_map.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('user.invited','invitation.accepted')
      AND current.action IN ('user.invited','invitation.accepted')
      AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('config.imported','config.exported')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN (
        'prompt_template.created','prompt_template.updated',
        'prompt_template.version_published'
      )
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='experiment.created' AND current.action=NEW.action
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_session_risk_owner_insert_prd070
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  )
  AND (
    NEW.object_type!='session_risk'
    OR NEW.object_name NOT IN ('enterprise_chat','api_session')
    OR NOT EXISTS (
      SELECT 1
      FROM session_risk_actions action
      JOIN session_risk_scores score
        ON score.tenant_id=action.tenant_id
       AND score.session_risk_score_id=action.session_risk_score_id
      JOIN session_risk_events event
        ON event.tenant_id=action.tenant_id
       AND event.session_risk_action_id=action.session_risk_action_id
      WHERE action.tenant_id=NEW.tenant_id
        AND action.session_risk_score_id=NEW.object_id
        AND action.actor_type='user'
        AND action.actor_id=NEW.actor_user_id
        AND action.source_request_id=NEW.request_id
        AND action.source_trace_id=NEW.trace_id
        AND action.created_at=NEW.created_at
        AND 'session_risk.' || action.action=NEW.action
        AND score.scope_type=NEW.object_name
        AND event.session_risk_score_id=action.session_risk_score_id
        AND event.session_risk_freeze_id IS action.session_risk_freeze_id
        AND event.event_type='security.session_high_risk'
        AND event.scope_type=score.scope_type
        AND event.scope_key=score.scope_key
        AND event.status=action.status_after
        AND event.target_type IS CASE action.action
          WHEN 'freeze_session' THEN 'session'
          WHEN 'release_session' THEN 'session'
          WHEN 'freeze_virtual_key' THEN 'virtual_key'
          WHEN 'release_virtual_key' THEN 'virtual_key'
          ELSE NULL END
        AND event.score=action.score_snapshot
        AND event.score_scale=score.score_scale
        AND event.state_revision=action.state_revision
        AND event.threshold_version=score.threshold_version
        AND event.actor_type='user'
        AND event.actor_id=action.actor_id
        AND event.source_request_id=action.source_request_id
        AND event.source_trace_id=action.source_trace_id
        AND event.occurred_at=action.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=6
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=6
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'scope_type','status','score','state_revision',
            'session_risk_freeze_id','target_type'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'scope_type','status','score','state_revision',
            'session_risk_freeze_id','target_type'
          )
        )
        AND json_extract(NEW.before_summary_json,'$.scope_type')=score.scope_type
        AND json_extract(NEW.after_summary_json,'$.scope_type')=score.scope_type
        AND json_extract(NEW.before_summary_json,'$.status')=action.status_before
        AND json_extract(NEW.after_summary_json,'$.status')=action.status_after
        AND json_extract(NEW.before_summary_json,'$.score')=action.score_snapshot
        AND json_extract(NEW.after_summary_json,'$.score')=action.score_snapshot
        AND json_extract(NEW.before_summary_json,'$.state_revision')=action.state_revision-1
        AND json_extract(NEW.after_summary_json,'$.state_revision')=action.state_revision
        AND json_extract(NEW.before_summary_json,'$.session_risk_freeze_id')
          IS CASE WHEN action.action IN ('release_session','release_virtual_key')
             THEN action.session_risk_freeze_id ELSE NULL END
        AND json_extract(NEW.after_summary_json,'$.session_risk_freeze_id')
          IS action.session_risk_freeze_id
        AND json_extract(NEW.before_summary_json,'$.target_type')
          IS CASE action.action
             WHEN 'release_session' THEN 'session'
             WHEN 'release_virtual_key' THEN 'virtual_key'
             ELSE NULL END
        AND json_extract(NEW.after_summary_json,'$.target_type')
          IS CASE action.action
             WHEN 'freeze_session' THEN 'session'
             WHEN 'release_session' THEN 'session'
             WHEN 'freeze_virtual_key' THEN 'virtual_key'
             WHEN 'release_virtual_key' THEN 'virtual_key'
             ELSE NULL END
        AND (SELECT COUNT(*) FROM json_each(NEW.changed_fields_json))
          =(SELECT COUNT(DISTINCT value) FROM json_each(NEW.changed_fields_json))
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.type!='text'
             OR field.value NOT IN (
               'scope_type','status','score','state_revision',
               'session_risk_freeze_id','target_type'
             )
        )
        AND NOT EXISTS (
          SELECT 1
          FROM json_each(
            '["scope_type","status","score","state_revision",' ||
            '"session_risk_freeze_id","target_type"]'
          ) expected
          WHERE (
            json_extract(NEW.before_summary_json,'$.' || expected.value)
              IS NOT json_extract(NEW.after_summary_json,'$.' || expected.value)
          ) != EXISTS (
            SELECT 1 FROM json_each(NEW.changed_fields_json) actual
            WHERE actual.value=expected.value
          )
        )
    )
  )
)
OR (
  NEW.object_type='session_risk'
  AND NEW.action NOT IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE UNIQUE INDEX idx_audit_logs_response_cache_owner_prd076
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN (
    'response_cache.policy_created','response_cache.policy_updated',
    'response_cache.invalidated'
  );
CREATE TRIGGER trg_audit_logs_response_cache_owner_insert_prd076
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN (
    'response_cache.policy_created','response_cache.policy_updated'
  )
  AND (
    NEW.object_type!='response_cache_policy' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS(
      SELECT 1 FROM response_cache_policy_events event
      JOIN response_cache_policies policy
        ON policy.tenant_id=event.tenant_id
       AND policy.cache_policy_id=event.cache_policy_id
      JOIN users actor
        ON actor.tenant_id=event.tenant_id
       AND actor.user_id=event.actor_user_id
      WHERE event.tenant_id=NEW.tenant_id
        AND event.cache_policy_id=NEW.object_id
        AND event.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND event.operation_request_id=NEW.request_id
        AND event.operation_trace_id=NEW.trace_id
        AND event.created_at=NEW.created_at
        AND event.event_type=CASE NEW.action
          WHEN 'response_cache.policy_created' THEN 'policy_created'
          ELSE 'policy_updated' END
        AND event.before_json=NEW.before_summary_json
        AND event.after_json=NEW.after_summary_json
        AND event.changed=1
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN(
            'tenant_id','cache_policy_id','model_alias_id','status','data_class',
            'ttl_seconds','charge_cache_hits','tags','version',
            'invalidation_generation'
          )
        )
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'tenant_id','cache_policy_id','model_alias_id','status','data_class',
            'ttl_seconds','charge_cache_hits','tags','version',
            'invalidation_generation'
          )
        )
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.type!='text' OR field.value NOT IN(
            'status','data_class','ttl_seconds','charge_cache_hits','tags',
            'version','invalidation_generation'
          )
        )
      )
  )
)
OR (
  NEW.action='response_cache.invalidated'
  AND (
    NEW.object_type!='response_cache_invalidation' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1 FROM response_cache_invalidations invalidation
      JOIN users actor
        ON actor.tenant_id=invalidation.tenant_id
       AND actor.user_id=invalidation.actor_user_id
      WHERE invalidation.tenant_id=NEW.tenant_id
        AND invalidation.cache_invalidation_id=NEW.object_id
        AND invalidation.actor_type='user'
        AND invalidation.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND invalidation.operation_request_id=NEW.request_id
        AND invalidation.operation_trace_id=NEW.trace_id
        AND invalidation.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'cache_invalidation_id','scope','match_mode','tag_count',
            'matched_entry_count','deleted_entry_count','fenced_fill_count',
            'reason_code'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.cache_invalidation_id')
            =invalidation.cache_invalidation_id
        AND json_extract(NEW.after_summary_json,'$.scope')=invalidation.scope
        AND json_extract(NEW.after_summary_json,'$.match_mode')
            =invalidation.match_mode
        AND json_extract(NEW.after_summary_json,'$.tag_count')
            =invalidation.tag_count
        AND json_extract(NEW.after_summary_json,'$.matched_entry_count')
            =invalidation.matched_entry_count
        AND json_extract(NEW.after_summary_json,'$.deleted_entry_count')
            =invalidation.deleted_entry_count
        AND json_extract(NEW.after_summary_json,'$.fenced_fill_count')
            =invalidation.fenced_fill_count
        AND json_extract(NEW.after_summary_json,'$.reason_code')
            =invalidation.reason_code
      )
  )
)
OR (
  NEW.object_type='response_cache_policy'
  AND NEW.action NOT IN(
    'response_cache.policy_created','response_cache.policy_updated'
  )
)
OR (
  NEW.object_type='response_cache_invalidation'
  AND NEW.action!='response_cache.invalidated'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
-- PRD-079 后续 Assistant AuditLog owner 索引与触发器随 preservation rebuild 恢复。
CREATE UNIQUE INDEX idx_audit_logs_admin_assistant_owner_prd077
  ON audit_logs(tenant_id,object_id)
  WHERE action='admin_assistant.queried';
CREATE TRIGGER trg_audit_logs_admin_assistant_owner_insert_prd077
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action='admin_assistant.queried'
  AND (
    NEW.object_type!='assistant_query' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1
      FROM assistant_audits assistant_audit
      JOIN assistant_queries query
        ON query.tenant_id=assistant_audit.tenant_id
       AND query.assistant_query_id=assistant_audit.assistant_query_id
      JOIN users actor
        ON actor.tenant_id=assistant_audit.tenant_id
       AND actor.user_id=assistant_audit.actor_user_id
      WHERE assistant_audit.tenant_id=NEW.tenant_id
        AND assistant_audit.assistant_query_id=NEW.object_id
        AND assistant_audit.audit_log_id=NEW.audit_log_id
        AND assistant_audit.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND assistant_audit.operation_request_id=NEW.request_id
        AND assistant_audit.operation_trace_id=NEW.trace_id
        AND assistant_audit.created_at=NEW.created_at
        AND query.query_type=json_extract(NEW.after_summary_json,'$.query_type')
        AND query.outcome=json_extract(NEW.after_summary_json,'$.outcome')
        AND query.claim_count=json_extract(NEW.after_summary_json,'$.claim_count')
        AND query.source_count=json_extract(NEW.after_summary_json,'$.source_count')
        AND query.redaction_count
            =json_extract(NEW.after_summary_json,'$.redaction_count')
        AND query.execution_mode
            =json_extract(NEW.after_summary_json,'$.execution_mode')
        AND query.model_call_count
            =json_extract(NEW.after_summary_json,'$.model_call_count')
        AND query.model_cost_status
            =json_extract(NEW.after_summary_json,'$.model_cost_status')
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'query_type','outcome','claim_count','source_count',
            'redaction_count','execution_mode','model_call_count',
            'model_cost_status'
          )
        )
        AND json_type(NEW.after_summary_json,'$.query_type')='text'
        AND json_type(NEW.after_summary_json,'$.outcome')='text'
        AND json_type(NEW.after_summary_json,'$.claim_count')='integer'
        AND json_type(NEW.after_summary_json,'$.source_count')='integer'
        AND json_type(NEW.after_summary_json,'$.redaction_count')='integer'
        AND json_type(NEW.after_summary_json,'$.execution_mode')='text'
        AND json_type(NEW.after_summary_json,'$.model_call_count')='integer'
        AND json_type(NEW.after_summary_json,'$.model_cost_status')='text'
        AND json(NEW.changed_fields_json)=json(
          '["claim_count","execution_mode","model_call_count",' ||
          '"model_cost_status","outcome","query_type","redaction_count",' ||
          '"source_count"]'
        )
    )
  )
)
OR (
  NEW.object_type='assistant_query'
  AND NEW.action!='admin_assistant.queried'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

-- Plan 是每个租户唯一当前套餐；试用 v1 只允许一次周期，所有持久时间使用 UTC Z 秒。
CREATE TABLE plans(
  plan_id TEXT PRIMARY KEY CHECK(plan_id GLOB 'pln_[0-9a-f]*' AND length(plan_id)=36),
  tenant_id TEXT NOT NULL,
  display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 80),
  base_tier TEXT NOT NULL CHECK(base_tier IN('free','paid')),
  status TEXT NOT NULL CHECK(status IN('active','suspended')),
  trial_status TEXT NOT NULL CHECK(trial_status IN('not_started','active','expired')),
  trial_started_at TEXT,
  trial_ends_at TEXT,
  trial_cycle INTEGER NOT NULL CHECK(trial_cycle IN(0,1)),
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  quota_generation INTEGER NOT NULL CHECK(quota_generation>=1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE(tenant_id),UNIQUE(tenant_id,plan_id),
  CHECK((trial_status='not_started' AND trial_cycle=0 AND trial_started_at IS NULL
      AND trial_ends_at IS NULL)
    OR (trial_status IN('active','expired') AND trial_cycle=1
      AND trial_started_at IS NOT NULL AND trial_ends_at IS NOT NULL
      AND unixepoch(trial_ends_at)>unixepoch(trial_started_at)))
);

-- 四个 Bucket 分别保存授予、已消费和并发预留微元；同类型 v1 只有一个当前桶。
CREATE TABLE quota_buckets(
  quota_bucket_id TEXT PRIMARY KEY CHECK(quota_bucket_id GLOB 'qbk_[0-9a-f]*'
    AND length(quota_bucket_id)=36),
  tenant_id TEXT NOT NULL,plan_id TEXT NOT NULL,
  quota_type TEXT NOT NULL CHECK(quota_type IN('paid','free','trial','gift')),
  granted_micros INTEGER NOT NULL CHECK(granted_micros BETWEEN 0 AND 1000000000000000),
  consumed_micros INTEGER NOT NULL CHECK(consumed_micros BETWEEN 0 AND 1000000000000000),
  reserved_micros INTEGER NOT NULL CHECK(reserved_micros BETWEEN 0 AND 1000000000000000),
  status TEXT NOT NULL CHECK(status IN('active','exhausted','expired','suspended')),
  expires_at TEXT,
  state_revision INTEGER NOT NULL CHECK(state_revision>=1),
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,plan_id) REFERENCES plans(tenant_id,plan_id),
  UNIQUE(tenant_id,quota_bucket_id),
  UNIQUE(tenant_id,plan_id,quota_type),
  UNIQUE(tenant_id,plan_id,quota_bucket_id,quota_type),
  CHECK(consumed_micros+reserved_micros<=granted_micros),
  CHECK((quota_type='trial') OR expires_at IS NULL)
);

-- 调整事实 append-only；只保存幂等键摘要和规范意图摘要，不保存幂等键明文。
CREATE TABLE quota_adjustments(
  quota_adjustment_id TEXT PRIMARY KEY CHECK(quota_adjustment_id GLOB 'qadj_[0-9a-f]*'
    AND length(quota_adjustment_id)=37),
  tenant_id TEXT NOT NULL,plan_id TEXT NOT NULL,quota_bucket_id TEXT NOT NULL,
  quota_type TEXT NOT NULL CHECK(quota_type IN('paid','free','trial','gift')),
  direction TEXT NOT NULL CHECK(direction IN('credit','debit')),
  amount_micros INTEGER NOT NULL CHECK(amount_micros BETWEEN 1 AND 1000000000000000),
  reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 500),
  actor_user_id TEXT NOT NULL,operation_request_id TEXT NOT NULL,operation_trace_id TEXT NOT NULL,
  expected_bucket_revision INTEGER NOT NULL CHECK(expected_bucket_revision>=1),
  resulting_bucket_revision INTEGER NOT NULL CHECK(resulting_bucket_revision=expected_bucket_revision+1),
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64
    AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,plan_id,quota_bucket_id,quota_type)
    REFERENCES quota_buckets(tenant_id,plan_id,quota_bucket_id,quota_type),
  UNIQUE(tenant_id,quota_adjustment_id),UNIQUE(tenant_id,idempotency_key_sha256)
);

-- Reservation header 以 request 为唯一 owner，防止并发请求重复占用同一额度。
CREATE TABLE quota_reservations(
  quota_reservation_id TEXT PRIMARY KEY CHECK(quota_reservation_id GLOB 'qres_[0-9a-f]*'
    AND length(quota_reservation_id)=37),
  tenant_id TEXT NOT NULL,plan_id TEXT NOT NULL,request_id TEXT NOT NULL,trace_id TEXT NOT NULL,
  estimated_micros INTEGER NOT NULL CHECK(estimated_micros BETWEEN 1 AND 1000000000000000),
  outstanding_micros INTEGER NOT NULL CHECK(outstanding_micros BETWEEN 0 AND 1000000000000000),
  actual_micros INTEGER CHECK(actual_micros IS NULL
    OR actual_micros BETWEEN 0 AND 1000000000000000),
  unallocated_micros INTEGER NOT NULL DEFAULT 0
    CHECK(unallocated_micros BETWEEN 0 AND 1000000000000000),
  order_version TEXT NOT NULL CHECK(order_version='trial_free_gift_paid_v1'),
  status TEXT NOT NULL CHECK(status IN(
    'active','exposure_pending','settled','released','overage','reconciliation_required')),
  expires_at TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,plan_id) REFERENCES plans(tenant_id,plan_id),
  UNIQUE(tenant_id,quota_reservation_id),UNIQUE(tenant_id,request_id),
  CHECK((actual_micros IS NULL AND unallocated_micros=0)
    OR (actual_micros IS NOT NULL AND unallocated_micros<=actual_micros)),
  CHECK((status='active' AND outstanding_micros=estimated_micros
      AND actual_micros IS NULL AND unallocated_micros=0)
    OR (status='exposure_pending' AND outstanding_micros BETWEEN 1 AND estimated_micros
      AND (actual_micros IS NOT NULL OR unallocated_micros=0))
    OR (status IN('settled','released','overage','reconciliation_required')
      AND outstanding_micros=0 AND actual_micros IS NOT NULL))
);

CREATE TABLE quota_reservation_items(
  quota_reservation_item_id TEXT PRIMARY KEY CHECK(
    quota_reservation_item_id GLOB 'qri_[0-9a-f]*' AND length(quota_reservation_item_id)=36),
  tenant_id TEXT NOT NULL,quota_reservation_id TEXT NOT NULL,plan_id TEXT NOT NULL,
  quota_bucket_id TEXT NOT NULL,quota_type TEXT NOT NULL
    CHECK(quota_type IN('paid','free','trial','gift')),
  priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 4),
  reserved_micros INTEGER NOT NULL CHECK(reserved_micros BETWEEN 1 AND 1000000000000000),
  consumed_micros INTEGER NOT NULL DEFAULT 0
    CHECK(consumed_micros BETWEEN 0 AND 1000000000000000),
  released_micros INTEGER NOT NULL DEFAULT 0
    CHECK(released_micros BETWEEN 0 AND 1000000000000000),
  status TEXT NOT NULL CHECK(status IN('active','exposure_pending','settled','released')),
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,quota_reservation_id)
    REFERENCES quota_reservations(tenant_id,quota_reservation_id),
  FOREIGN KEY(tenant_id,plan_id,quota_bucket_id,quota_type)
    REFERENCES quota_buckets(tenant_id,plan_id,quota_bucket_id,quota_type),
  UNIQUE(tenant_id,quota_reservation_item_id),
  UNIQUE(tenant_id,quota_reservation_id,quota_bucket_id),
  CHECK((status='active' AND consumed_micros=0 AND released_micros=0)
    OR (status='exposure_pending'
      AND consumed_micros+released_micros<reserved_micros)
    OR (status IN('settled','released')
      AND consumed_micros+released_micros=reserved_micros))
);

-- Settlement 与 canonical Usage/FinOps 一一对应；Allocation 才是额度分类报表的权威来源。
CREATE TABLE quota_usage_settlements(
  quota_usage_settlement_id TEXT PRIMARY KEY CHECK(
    quota_usage_settlement_id GLOB 'qset_[0-9a-f]*' AND length(quota_usage_settlement_id)=37),
  tenant_id TEXT NOT NULL,plan_id TEXT NOT NULL,quota_reservation_id TEXT NOT NULL,
  usage_ledger_id TEXT NOT NULL,finops_ledger_entry_id TEXT NOT NULL,
  request_id TEXT NOT NULL,trace_id TEXT NOT NULL,
  amount_micros INTEGER NOT NULL CHECK(amount_micros BETWEEN 0 AND 1000000000000000),
  exposure_outstanding_micros INTEGER NOT NULL
    CHECK(exposure_outstanding_micros BETWEEN 0 AND 1000000000000000),
  allocated_micros INTEGER NOT NULL CHECK(allocated_micros BETWEEN 0 AND 1000000000000000),
  unallocated_micros INTEGER NOT NULL
    CHECK(unallocated_micros BETWEEN 0 AND 1000000000000000),
  amount_basis TEXT NOT NULL CHECK(amount_basis IN('actual_cny','admission_estimate')),
  allocation_class TEXT NOT NULL CHECK(allocation_class IN(
    'paid','free','trial','gift','mixed','unallocated')),
  allocation_count INTEGER NOT NULL CHECK(allocation_count BETWEEN 0 AND 4),
  status TEXT NOT NULL CHECK(status IN('settled','overage')),
  plan_revision INTEGER NOT NULL CHECK(plan_revision>=1),
  order_version TEXT NOT NULL CHECK(order_version='trial_free_gift_paid_v1'),
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,plan_id) REFERENCES plans(tenant_id,plan_id),
  FOREIGN KEY(tenant_id,quota_reservation_id)
    REFERENCES quota_reservations(tenant_id,quota_reservation_id),
  FOREIGN KEY(tenant_id,usage_ledger_id) REFERENCES usage_ledger(tenant_id,usage_ledger_id),
  FOREIGN KEY(tenant_id,finops_ledger_entry_id)
    REFERENCES finops_ledger_entries(tenant_id,finops_ledger_entry_id),
  UNIQUE(tenant_id,quota_usage_settlement_id),UNIQUE(tenant_id,usage_ledger_id),
  UNIQUE(tenant_id,finops_ledger_entry_id),UNIQUE(tenant_id,quota_reservation_id),
  CHECK(amount_micros=allocated_micros+unallocated_micros),
  CHECK((status='settled' AND unallocated_micros=0)
    OR (status='overage' AND unallocated_micros>0)),
  CHECK((allocation_class='unallocated' AND allocation_count=0)
    OR (allocation_class IN('paid','free','trial','gift') AND allocation_count=1)
    OR (allocation_class='mixed' AND allocation_count BETWEEN 2 AND 4))
);

CREATE TABLE quota_usage_allocations(
  quota_usage_allocation_id TEXT PRIMARY KEY CHECK(
    quota_usage_allocation_id GLOB 'qalloc_[0-9a-f]*' AND length(quota_usage_allocation_id)=39),
  tenant_id TEXT NOT NULL,quota_usage_settlement_id TEXT NOT NULL,
  plan_id TEXT NOT NULL,quota_bucket_id TEXT NOT NULL,
  quota_type TEXT NOT NULL CHECK(quota_type IN('paid','free','trial','gift')),
  sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 4),
  amount_micros INTEGER NOT NULL CHECK(amount_micros BETWEEN 1 AND 1000000000000000),
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,quota_usage_settlement_id)
    REFERENCES quota_usage_settlements(tenant_id,quota_usage_settlement_id),
  FOREIGN KEY(tenant_id,plan_id,quota_bucket_id,quota_type)
    REFERENCES quota_buckets(tenant_id,plan_id,quota_bucket_id,quota_type),
  UNIQUE(tenant_id,quota_usage_allocation_id),
  UNIQUE(tenant_id,quota_usage_settlement_id,quota_bucket_id),
  UNIQUE(tenant_id,quota_usage_settlement_id,sequence)
);

-- Source event 与 outbox 同事务落盘；外部 Webhook 网络调用只允许在 commit 后由 Control worker 执行。
CREATE TABLE quota_events(
  quota_event_id TEXT PRIMARY KEY CHECK(quota_event_id GLOB 'qevt_[0-9a-f]*'
    AND length(quota_event_id)=37),
  tenant_id TEXT NOT NULL,plan_id TEXT NOT NULL,quota_bucket_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN('quota.exhausted','trial.expired')),
  quota_generation INTEGER,trial_cycle INTEGER,
  source_request_id TEXT NOT NULL,source_trace_id TEXT NOT NULL,actor_id TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
  occurred_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,plan_id) REFERENCES plans(tenant_id,plan_id),
  FOREIGN KEY(tenant_id,quota_bucket_id) REFERENCES quota_buckets(tenant_id,quota_bucket_id),
  UNIQUE(tenant_id,quota_event_id),
  CHECK((event_type='quota.exhausted' AND quota_generation>=1 AND trial_cycle IS NULL
      AND actor_id='quota-admission')
    OR (event_type='trial.expired' AND quota_generation IS NULL AND trial_cycle=1
      AND actor_id IN('quota-trial-scheduler','quota-expiry-guard')))
);

CREATE TABLE quota_event_outbox(
  quota_event_outbox_id TEXT PRIMARY KEY CHECK(
    quota_event_outbox_id GLOB 'qobx_[0-9a-f]*' AND length(quota_event_outbox_id)=37),
  tenant_id TEXT NOT NULL,quota_event_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','processing','dispatched','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  lease_generation INTEGER NOT NULL CHECK(lease_generation>=0),
  lease_owner_id TEXT,lease_token TEXT,lease_expires_at TEXT,next_attempt_at TEXT,
  delivery_snapshot_json TEXT CHECK(delivery_snapshot_json IS NULL OR
    (json_valid(delivery_snapshot_json) AND json_type(delivery_snapshot_json)='array')),
  snapshot_frozen_at TEXT,last_error_code TEXT,dispatched_at TEXT,
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,quota_event_id) REFERENCES quota_events(tenant_id,quota_event_id),
  UNIQUE(tenant_id,quota_event_outbox_id),UNIQUE(tenant_id,quota_event_id),
  CHECK((status='pending' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND dispatched_at IS NULL)
    OR (status='processing' AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND dispatched_at IS NULL)
    OR (status='dispatched' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND dispatched_at IS NOT NULL)
    OR (status='failed' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND dispatched_at IS NULL))
);

CREATE INDEX idx_quota_buckets_plan_type_prd079
  ON quota_buckets(tenant_id,plan_id,quota_type);
CREATE INDEX idx_quota_adjustments_tenant_time_prd079
  ON quota_adjustments(tenant_id,created_at DESC,quota_adjustment_id DESC);
CREATE INDEX idx_quota_reservations_due_prd079
  ON quota_reservations(status,expires_at,tenant_id);
CREATE INDEX idx_quota_allocations_report_prd079
  ON quota_usage_allocations(tenant_id,quota_type,created_at);
CREATE UNIQUE INDEX uq_quota_exhausted_generation_prd079
  ON quota_events(tenant_id,plan_id,quota_generation)
  WHERE event_type='quota.exhausted';
CREATE UNIQUE INDEX uq_trial_expired_cycle_prd079
  ON quota_events(tenant_id,plan_id,trial_cycle)
  WHERE event_type='trial.expired';
CREATE INDEX idx_quota_outbox_due_prd079
  ON quota_event_outbox(status,next_attempt_at,lease_expires_at,created_at);

-- owner、CAS 和 append-only trigger 把租户隔离与状态守恒落实到数据库最后一道边界。
CREATE TRIGGER trg_plans_owner_insert_prd079 BEFORE INSERT ON plans FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
BEGIN SELECT RAISE(ABORT,'plan_owner_mismatch'); END;
CREATE TRIGGER trg_plans_transition_prd079 BEFORE UPDATE ON plans FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.plan_id!=OLD.plan_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.state_revision!=OLD.state_revision+1
  OR NEW.quota_generation NOT IN(OLD.quota_generation,OLD.quota_generation+1)
  OR (OLD.trial_cycle=1 AND NEW.trial_cycle!=1)
  OR (OLD.trial_status='expired' AND NEW.trial_status!='expired')
  OR (OLD.trial_status='not_started' AND NEW.trial_status NOT IN('not_started','active','expired'))
  OR (OLD.trial_status='active' AND NEW.trial_status NOT IN('active','expired'))
BEGIN SELECT RAISE(ABORT,'plan_transition_invalid'); END;
CREATE TRIGGER trg_plans_delete_prd079 BEFORE DELETE ON plans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'plan_immutable'); END;

CREATE TRIGGER trg_quota_buckets_owner_insert_prd079 BEFORE INSERT ON quota_buckets FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM plans WHERE tenant_id=NEW.tenant_id AND plan_id=NEW.plan_id)
BEGIN SELECT RAISE(ABORT,'quota_bucket_owner_mismatch'); END;
CREATE TRIGGER trg_quota_buckets_transition_prd079 BEFORE UPDATE ON quota_buckets FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.quota_bucket_id!=OLD.quota_bucket_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.plan_id!=OLD.plan_id OR NEW.quota_type!=OLD.quota_type
  OR NEW.created_at!=OLD.created_at OR NEW.state_revision!=OLD.state_revision+1
  OR (NEW.quota_type!='trial' AND NEW.expires_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'quota_bucket_transition_invalid'); END;
CREATE TRIGGER trg_quota_buckets_delete_prd079 BEFORE DELETE ON quota_buckets FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_bucket_immutable'); END;

CREATE TRIGGER trg_quota_adjustments_owner_insert_prd079
BEFORE INSERT ON quota_adjustments FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM quota_buckets bucket
  WHERE bucket.tenant_id=NEW.tenant_id AND bucket.plan_id=NEW.plan_id
    AND bucket.quota_bucket_id=NEW.quota_bucket_id AND bucket.quota_type=NEW.quota_type
    AND bucket.state_revision=NEW.resulting_bucket_revision)
  OR NOT EXISTS(SELECT 1 FROM users user
    WHERE user.tenant_id=NEW.tenant_id AND user.user_id=NEW.actor_user_id)
BEGIN SELECT RAISE(ABORT,'quota_adjustment_owner_mismatch'); END;
CREATE TRIGGER trg_quota_adjustments_update_prd079 BEFORE UPDATE ON quota_adjustments FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_adjustment_immutable'); END;
CREATE TRIGGER trg_quota_adjustments_delete_prd079 BEFORE DELETE ON quota_adjustments FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_adjustment_immutable'); END;

CREATE TRIGGER trg_quota_reservations_owner_insert_prd079
BEFORE INSERT ON quota_reservations FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM plans WHERE tenant_id=NEW.tenant_id AND plan_id=NEW.plan_id)
BEGIN SELECT RAISE(ABORT,'quota_reservation_owner_mismatch'); END;
CREATE TRIGGER trg_quota_reservations_transition_prd079
BEFORE UPDATE ON quota_reservations FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.quota_reservation_id!=OLD.quota_reservation_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.plan_id!=OLD.plan_id
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.estimated_micros!=OLD.estimated_micros OR NEW.order_version!=OLD.order_version
  OR NEW.created_at!=OLD.created_at OR NEW.outstanding_micros>OLD.outstanding_micros
  OR (OLD.actual_micros IS NOT NULL AND NEW.actual_micros IS NOT OLD.actual_micros)
  OR OLD.status NOT IN('active','exposure_pending') OR NEW.status='active'
BEGIN SELECT RAISE(ABORT,'quota_reservation_transition_invalid'); END;
CREATE TRIGGER trg_quota_reservations_delete_prd079 BEFORE DELETE ON quota_reservations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_reservation_immutable'); END;

CREATE TRIGGER trg_quota_reservation_items_owner_insert_prd079
BEFORE INSERT ON quota_reservation_items FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM quota_reservations reservation
  JOIN quota_buckets bucket ON bucket.tenant_id=reservation.tenant_id
    AND bucket.plan_id=reservation.plan_id
  WHERE reservation.tenant_id=NEW.tenant_id
    AND reservation.quota_reservation_id=NEW.quota_reservation_id
    AND reservation.plan_id=NEW.plan_id AND reservation.status='active'
    AND bucket.quota_bucket_id=NEW.quota_bucket_id AND bucket.quota_type=NEW.quota_type)
BEGIN SELECT RAISE(ABORT,'quota_reservation_item_owner_mismatch'); END;
CREATE TRIGGER trg_quota_reservation_items_transition_prd079
BEFORE UPDATE ON quota_reservation_items FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.quota_reservation_item_id!=OLD.quota_reservation_item_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.quota_reservation_id!=OLD.quota_reservation_id
  OR NEW.plan_id!=OLD.plan_id OR NEW.quota_bucket_id!=OLD.quota_bucket_id
  OR NEW.quota_type!=OLD.quota_type OR NEW.priority!=OLD.priority
  OR NEW.reserved_micros!=OLD.reserved_micros OR NEW.created_at!=OLD.created_at
  OR NEW.consumed_micros<OLD.consumed_micros
  OR NEW.released_micros<OLD.released_micros
  OR OLD.status NOT IN('active','exposure_pending') OR NEW.status='active'
BEGIN SELECT RAISE(ABORT,'quota_reservation_item_transition_invalid'); END;
CREATE TRIGGER trg_quota_reservation_items_delete_prd079
BEFORE DELETE ON quota_reservation_items FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_reservation_item_immutable'); END;

CREATE TRIGGER trg_quota_settlements_owner_insert_prd079
BEFORE INSERT ON quota_usage_settlements FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM quota_reservations reservation
  JOIN usage_ledger usage ON usage.tenant_id=reservation.tenant_id
    AND usage.request_id=reservation.request_id
  JOIN finops_ledger_entries ledger ON ledger.tenant_id=usage.tenant_id
    AND ledger.usage_ledger_id=usage.usage_ledger_id
  WHERE reservation.tenant_id=NEW.tenant_id AND reservation.plan_id=NEW.plan_id
    AND reservation.quota_reservation_id=NEW.quota_reservation_id
    AND reservation.request_id=NEW.request_id AND reservation.trace_id=NEW.trace_id
    AND usage.usage_ledger_id=NEW.usage_ledger_id
    AND ledger.finops_ledger_entry_id=NEW.finops_ledger_entry_id
    AND ledger.source_request_id=reservation.request_id
    AND ledger.source_trace_id=reservation.trace_id
    AND ledger.entry_type='usage_charge' AND ledger.direction='debit'
    AND (
      (NEW.amount_basis='actual_cny'
        AND ledger.cny_normalization_status='normalized'
        AND ledger.normalized_cny_micros=NEW.amount_micros)
      OR (NEW.amount_basis='admission_estimate'
        AND ledger.cny_normalization_status='unavailable'
        AND ledger.normalized_cny_micros IS NULL
        AND NEW.amount_micros=(
          reservation.estimated_micros-NEW.exposure_outstanding_micros))
    )
    AND NOT EXISTS(SELECT 1 FROM quota_usage_settlements existing
      WHERE existing.tenant_id=reservation.tenant_id
        AND existing.quota_reservation_id=reservation.quota_reservation_id)
    AND (
      (reservation.status='exposure_pending'
        AND reservation.actual_micros=NEW.amount_micros
        AND reservation.unallocated_micros=NEW.unallocated_micros
        AND reservation.outstanding_micros=NEW.exposure_outstanding_micros
        AND NEW.exposure_outstanding_micros>0)
      OR (reservation.status IN('settled','overage')
        AND reservation.actual_micros=NEW.amount_micros
        AND reservation.unallocated_micros=NEW.unallocated_micros
        AND reservation.outstanding_micros=0
        AND NEW.exposure_outstanding_micros=0)
      OR (reservation.status IN('released','reconciliation_required')
        AND NEW.exposure_outstanding_micros=0 AND NEW.allocated_micros=0
        AND NEW.unallocated_micros=NEW.amount_micros
        AND ((NEW.amount_micros=0 AND NEW.status='settled')
          OR (NEW.amount_micros>0 AND NEW.status='overage')))
    ))
BEGIN SELECT RAISE(ABORT,'quota_settlement_owner_mismatch'); END;
CREATE TRIGGER trg_quota_settlements_update_prd079
BEFORE UPDATE ON quota_usage_settlements FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_settlement_immutable'); END;
CREATE TRIGGER trg_quota_settlements_delete_prd079
BEFORE DELETE ON quota_usage_settlements FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_settlement_immutable'); END;

CREATE TRIGGER trg_quota_allocations_owner_insert_prd079
BEFORE INSERT ON quota_usage_allocations FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM quota_usage_settlements settlement
  JOIN quota_buckets bucket ON bucket.tenant_id=settlement.tenant_id
    AND bucket.plan_id=settlement.plan_id
  WHERE settlement.tenant_id=NEW.tenant_id
    AND settlement.quota_usage_settlement_id=NEW.quota_usage_settlement_id
    AND settlement.plan_id=NEW.plan_id AND bucket.quota_bucket_id=NEW.quota_bucket_id
    AND bucket.quota_type=NEW.quota_type)
BEGIN SELECT RAISE(ABORT,'quota_allocation_owner_mismatch'); END;
CREATE TRIGGER trg_quota_allocations_update_prd079
BEFORE UPDATE ON quota_usage_allocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_allocation_immutable'); END;
CREATE TRIGGER trg_quota_allocations_delete_prd079
BEFORE DELETE ON quota_usage_allocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_allocation_immutable'); END;

CREATE TRIGGER trg_quota_events_owner_insert_prd079 BEFORE INSERT ON quota_events FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM plans WHERE tenant_id=NEW.tenant_id AND plan_id=NEW.plan_id)
  OR (NEW.quota_bucket_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM quota_buckets
    WHERE tenant_id=NEW.tenant_id AND quota_bucket_id=NEW.quota_bucket_id))
BEGIN SELECT RAISE(ABORT,'quota_event_owner_mismatch'); END;
CREATE TRIGGER trg_quota_events_update_prd079 BEFORE UPDATE ON quota_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_event_immutable'); END;
CREATE TRIGGER trg_quota_events_delete_prd079 BEFORE DELETE ON quota_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_event_immutable'); END;

CREATE TRIGGER trg_quota_outbox_owner_insert_prd079 BEFORE INSERT ON quota_event_outbox FOR EACH ROW
WHEN NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.lease_generation!=0
  OR NOT EXISTS(SELECT 1 FROM quota_events event
    WHERE event.tenant_id=NEW.tenant_id AND event.quota_event_id=NEW.quota_event_id)
BEGIN SELECT RAISE(ABORT,'quota_outbox_owner_mismatch'); END;
CREATE TRIGGER trg_quota_outbox_transition_prd079 BEFORE UPDATE ON quota_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.quota_event_outbox_id!=OLD.quota_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.quota_event_id!=OLD.quota_event_id
  OR NEW.created_at!=OLD.created_at OR NEW.attempt_count<OLD.attempt_count
  OR NEW.attempt_count>OLD.attempt_count+1 OR NEW.lease_generation<OLD.lease_generation
  OR NEW.lease_generation>OLD.lease_generation+1
  OR (OLD.status='dispatched' AND NEW.status!='dispatched')
  OR (OLD.status='failed' AND NEW.status!='failed')
  OR (OLD.status='pending' AND NEW.status NOT IN('pending','processing'))
  OR (OLD.status='processing' AND NEW.status NOT IN('pending','processing','dispatched','failed'))
  OR (OLD.delivery_snapshot_json IS NOT NULL
    AND NEW.delivery_snapshot_json IS NOT OLD.delivery_snapshot_json)
BEGIN SELECT RAISE(ABORT,'quota_outbox_transition_invalid'); END;
CREATE TRIGGER trg_quota_outbox_delete_prd079 BEFORE DELETE ON quota_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_outbox_immutable'); END;

-- Plan/Quota 审计索引防止同一命令或只追加调整重复落账；owner trigger 逐字段绑定领域事实。
CREATE UNIQUE INDEX idx_audit_logs_plan_request_prd079
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN('plan.created','plan.updated');
CREATE UNIQUE INDEX idx_audit_logs_quota_adjustment_prd079
  ON audit_logs(tenant_id,object_id)
  WHERE action='quota.adjustment_created';
CREATE TRIGGER trg_audit_logs_plan_billing_owner_insert_prd079
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN('plan.created','plan.updated')
  AND (
    NEW.object_type!='plan'
    OR NOT EXISTS(
      SELECT 1 FROM plans plan
      WHERE plan.tenant_id=NEW.tenant_id AND plan.plan_id=NEW.object_id
        AND plan.display_name=NEW.object_name
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=9
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'display_name','base_tier','status','trial_status','trial_started_at',
            'trial_ends_at','trial_cycle','state_revision','quota_generation'))
        AND json_extract(NEW.after_summary_json,'$.display_name')=plan.display_name
        AND json_extract(NEW.after_summary_json,'$.base_tier')=plan.base_tier
        AND json_extract(NEW.after_summary_json,'$.status')=plan.status
        AND json_extract(NEW.after_summary_json,'$.trial_status')=plan.trial_status
        AND json_extract(NEW.after_summary_json,'$.trial_started_at') IS plan.trial_started_at
        AND json_extract(NEW.after_summary_json,'$.trial_ends_at') IS plan.trial_ends_at
        AND json_extract(NEW.after_summary_json,'$.trial_cycle')=plan.trial_cycle
        AND json_extract(NEW.after_summary_json,'$.state_revision')=plan.state_revision
        AND json_extract(NEW.after_summary_json,'$.quota_generation')=plan.quota_generation
        AND (
          (NEW.action='plan.created' AND NEW.before_summary_json='{}'
            AND plan.state_revision=1)
          OR (NEW.action='plan.updated'
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=9
            AND json_extract(NEW.before_summary_json,'$.state_revision')+1
              =plan.state_revision)
        )
    )
  )
)
OR (NEW.object_type='plan' AND NEW.action NOT IN('plan.created','plan.updated'))
OR (
  NEW.action='quota.adjustment_created'
  AND (
    NEW.object_type!='quota_adjustment' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1 FROM quota_adjustments adjustment
      WHERE adjustment.tenant_id=NEW.tenant_id
        AND adjustment.quota_adjustment_id=NEW.object_id
        AND adjustment.actor_user_id=NEW.actor_user_id
        AND adjustment.operation_request_id=NEW.request_id
        AND adjustment.operation_trace_id=NEW.trace_id
        AND adjustment.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'plan_id','quota_bucket_id','quota_type','direction','amount',
            'expected_bucket_revision','resulting_bucket_revision','reason_sha256'))
        AND json_extract(NEW.after_summary_json,'$.plan_id')=adjustment.plan_id
        AND json_extract(NEW.after_summary_json,'$.quota_bucket_id')
          =adjustment.quota_bucket_id
        AND json_extract(NEW.after_summary_json,'$.quota_type')=adjustment.quota_type
        AND json_extract(NEW.after_summary_json,'$.direction')=adjustment.direction
        AND json_extract(NEW.after_summary_json,'$.amount')=
          printf('%d.%06d',adjustment.amount_micros/1000000,
            adjustment.amount_micros%1000000)
        AND json_extract(NEW.after_summary_json,'$.expected_bucket_revision')
          =adjustment.expected_bucket_revision
        AND json_extract(NEW.after_summary_json,'$.resulting_bucket_revision')
          =adjustment.resulting_bucket_revision
        AND json_extract(NEW.after_summary_json,'$.reason_sha256')
          =metahub_sha256(adjustment.reason)
    )
  )
)
OR (NEW.object_type='quota_adjustment' AND NEW.action!='quota.adjustment_created')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

-- 事务末尾复核 revision-15 目录、默认矩阵、九表、审计扩展、关键 trigger、完整性和外键。
DROP TABLE IF EXISTS temp.prd079_up_final_guard;
CREATE TEMP TABLE prd079_up_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd079_up_final_guard_abort
BEFORE INSERT ON prd079_up_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd079_up_final_invalid'); END;
-- 写入终态问题计数并在提交前原子校验目录、结构、触发器、外键和完整性。
INSERT INTO prd079_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=76
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=15)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='47302c3c32105b2dc441206459067defa428bd737a978a411325b937c071076f'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=202
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='3fbb05356550fb45ec1e9f685c5bc82cbc23d9641041ce7a042463ac37bdff86'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'plans','quota_buckets','quota_adjustments','quota_reservations',
      'quota_reservation_items','quota_usage_settlements','quota_usage_allocations',
      'quota_events','quota_event_outbox'))=9 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN(
      'trg_plans_owner_insert_prd079','trg_plans_transition_prd079',
      'trg_plans_delete_prd079','trg_quota_buckets_owner_insert_prd079',
      'trg_quota_buckets_transition_prd079','trg_quota_buckets_delete_prd079',
      'trg_quota_adjustments_owner_insert_prd079','trg_quota_adjustments_update_prd079',
      'trg_quota_adjustments_delete_prd079','trg_quota_reservations_owner_insert_prd079',
      'trg_quota_reservations_transition_prd079','trg_quota_reservations_delete_prd079',
      'trg_quota_reservation_items_owner_insert_prd079',
      'trg_quota_reservation_items_transition_prd079',
      'trg_quota_reservation_items_delete_prd079','trg_quota_settlements_owner_insert_prd079',
      'trg_quota_settlements_update_prd079','trg_quota_settlements_delete_prd079',
      'trg_quota_allocations_owner_insert_prd079','trg_quota_allocations_update_prd079',
      'trg_quota_allocations_delete_prd079','trg_quota_events_owner_insert_prd079',
      'trg_quota_events_update_prd079','trg_quota_events_delete_prd079',
      'trg_quota_outbox_owner_insert_prd079','trg_quota_outbox_transition_prd079',
      'trg_quota_outbox_delete_prd079',
      'trg_audit_logs_plan_billing_owner_insert_prd079'))=28 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN(
      'idx_audit_logs_plan_request_prd079',
      'idx_audit_logs_quota_adjustment_prd079'))=2
      AND instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
        '''plan.created'',''plan.updated'',''quota.adjustment_created''')>0
      AND instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
        '''assistant_query'',''plan'',''quota_adjustment''')>0
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd079_up_final_guard_abort;
DROP TABLE prd079_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
