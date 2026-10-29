-- PRD-080 正向迁移：账单争议、逐项冲正关联、结算暂缓与恢复型 created event outbox。
-- 所有业务事实以 tenant_id 为第一 owner；金额保存整数微元，reason、幂等键明文、Prompt、
-- Response、Provider Key、Token 与底层异常不得进入事件 payload、trigger 错误或验收证据。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首个持久 DDL 前精确验证 0072、revision-15 目录、默认矩阵、关键 owner 与数据库完整性。
DROP TABLE IF EXISTS temp.prd080_up_guard;
CREATE TEMP TABLE prd080_up_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd080_up_guard_abort
BEFORE INSERT ON prd080_up_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd080_up_baseline_invalid'); END;
INSERT INTO prd080_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0072'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=76
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
      'billing_disputes','billing_dispute_items','billing_settlement_holds',
      'billing_dispute_transitions','billing_dispute_adjustment_links',
      'billing_dispute_events','billing_dispute_event_outbox',
      'billing_dispute_scheduler_tenants'))=0 THEN 0 ELSE 1 END
  + CASE WHEN EXISTS(SELECT 1 FROM sqlite_schema WHERE type='trigger'
      AND name='trg_audit_logs_plan_billing_owner_insert_prd079')
      AND instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
        '''plan.created'',''plan.updated'',''quota.adjustment_created''')>0
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd080_up_guard_abort;
DROP TABLE prd080_up_guard;

CREATE TEMP TABLE prd080_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','finance_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '9d92ea073fcfb7425ef0d46e881fc16cf50fdbb0192560812c0e8c5fe9c6946f'
    WHEN 'finance_admin' THEN '5311448a5233c5f2430620095b90bd33af8489e26bab0357c861437ae1916c39'
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

CREATE TABLE rbac_permission_templates_prd080_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=16)
);
-- 原样复制既有权限模板行号与内容，仅把目录版本提升到 revision 16。
INSERT INTO rbac_permission_templates_prd080_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,16
FROM rbac_permission_templates;
-- 三项权限分别表达争议读取、提交和审核；批准仍由服务层同时复核 billing.finops.write。
INSERT INTO rbac_permission_templates_prd080_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('billing.dispute.read','billing','读取账单争议','读取当前租户账单争议、暂缓标记与调整链路。','read',16),
  ('billing.dispute.create','billing','提交账单争议','提交或取消当前租户本人创建的账单争议。','write',16),
  ('billing.dispute.review','billing','审核账单争议','审核当前租户账单争议；批准仍需正式账本写权限。','critical',16);
DROP TABLE rbac_permission_templates;
-- 以保留数据的新表原子替换旧权限模板表，继续维持原表名称合同。
ALTER TABLE rbac_permission_templates_prd080_new RENAME TO rbac_permission_templates;

-- 默认只向 admin 与 finance_admin 目录追加争议权限，其他固定角色不被扩权。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','billing.dispute.read',1),('admin','billing.dispute.create',1),
  ('admin','billing.dispute.review',1),
  ('finance_admin','billing.dispute.read',1),('finance_admin','billing.dispute.create',1),
  ('finance_admin','billing.dispute.review',1);

CREATE TABLE permissions_prd080_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=16),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 原样复制各租户既有权限事实，仅同步目录版本为 revision 16。
INSERT INTO permissions_prd080_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       16,status,created_at,updated_at
FROM permissions;
-- 为每个租户生成三项独立权限行，并保持复合租户所有权约束。
INSERT INTO permissions_prd080_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       16,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant CROSS JOIN rbac_permission_templates template
WHERE template.permission_code IN(
  'billing.dispute.read','billing.dispute.create','billing.dispute.review');
DROP TABLE permissions;
-- 用已完整复制并扩展的数据表替换旧权限表，随后恢复租户分类索引。
ALTER TABLE permissions_prd080_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

-- 仅向迁移前精确匹配默认矩阵的财务角色写入新增权限关系。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd080_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id
WHERE permission.code IN(
  'billing.dispute.read','billing.dispute.create','billing.dispute.review');
-- 权限关系变化后递增角色 revision 并写入对应的冻结矩阵摘要。
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN 'c56c271d650b89b1d4d6d49c1b8e326c89b075bb274116930a335f4c8f230e16'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
  END
WHERE role_id IN(SELECT role_id FROM prd080_roles_to_expand);

-- 恢复 revision-16 RBAC 不可变目录、owner 和新租户初始化守卫。
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
    WHEN 'admin' THEN 'c56c271d650b89b1d4d6d49c1b8e326c89b075bb274116930a335f4c8f230e16'
    WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
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
  -- 新租户按 revision 16 冻结目录创建六个默认角色及其矩阵摘要。
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
      WHEN 'admin' THEN 'c56c271d650b89b1d4d6d49c1b8e326c89b075bb274116930a335f4c8f230e16'
      WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN '7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b'
      WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 新租户复制完整权限目录，所有权限行均绑定 NEW tenant_id。
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,16,'active',
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
DROP TABLE prd080_roles_to_expand;

-- PRD-080 AuditLog 保真重建只扩展争议 action/object allowlist；全部历史行与 owner 守卫原样恢复。
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS audit_logs_prd080_new;
CREATE TABLE audit_logs_prd080_new (
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
      'plan.created','plan.updated','quota.adjustment_created',
      'billing_dispute.created','billing_dispute.review_started',
      'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled'
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
      'assistant_query','plan','quota_adjustment','billing_dispute'
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
INSERT INTO audit_logs_prd080_new(rowid,audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action, object_type, object_id, object_name, before_summary_json, after_summary_json, changed_fields_json, client_ip, user_agent, request_id, trace_id, created_at)
SELECT rowid,audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action, object_type, object_id, object_name, before_summary_json, after_summary_json, changed_fields_json, client_ip, user_agent, request_id, trace_id, created_at FROM audit_logs;
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd080_audit_copy_guard;
CREATE TEMP TABLE prd080_audit_copy_guard(issue_count INTEGER NOT NULL
  CONSTRAINT prd080_audit_copy_guard_ok CHECK(issue_count=0));
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd080_audit_copy_guard(issue_count)
SELECT ABS((SELECT COUNT(*) FROM audit_logs)
  -(SELECT COUNT(*) FROM audit_logs_prd080_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd080_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs_prd080_new
      EXCEPT SELECT rowid,* FROM audit_logs));
DROP TABLE prd080_audit_copy_guard;
DROP TABLE audit_logs;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE audit_logs_prd080_new RENAME TO audit_logs;
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

-- AUDIT_REBUILD_END
-- Audit 表重建后恢复 PRD-079 Plan/Quota 索引与 owner trigger，确保 0072 合同不回退。
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

-- Dispute head 是唯一可变业务头；状态、revision、reviewer 与终态字段由 CHECK 和 transition trigger 双重冻结。
CREATE TABLE billing_disputes(
  billing_dispute_sequence INTEGER PRIMARY KEY AUTOINCREMENT
    CHECK(typeof(billing_dispute_sequence)='integer' AND billing_dispute_sequence>0),
  billing_dispute_id TEXT NOT NULL UNIQUE CHECK(length(billing_dispute_id)=37
    AND billing_dispute_id GLOB 'bdsp_[0-9a-f]*'
    AND substr(billing_dispute_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK(scope_type IN('request_ids','time_range')),
  period_start TEXT CHECK(period_start IS NULL OR(length(period_start)=20
    AND period_start GLOB '????-??-??T??:??:??Z' AND unixepoch(period_start) IS NOT NULL)),
  period_end TEXT CHECK(period_end IS NULL OR(length(period_end)=20
    AND period_end GLOB '????-??-??T??:??:??Z' AND unixepoch(period_end) IS NOT NULL)),
  status TEXT NOT NULL CHECK(status IN(
    'submitted','under_review','approved','rejected','cancelled')),
  state_revision INTEGER NOT NULL CHECK(typeof(state_revision)='integer' AND state_revision>=1),
  item_count INTEGER NOT NULL CHECK(typeof(item_count)='integer' AND item_count BETWEEN 1 AND 100),
  reason TEXT NOT NULL CHECK(typeof(reason)='text' AND reason=trim(reason)
    AND length(CAST(reason AS BLOB)) BETWEEN 1 AND 2000 AND instr(reason,char(0))=0),
  reason_sha256 TEXT NOT NULL CHECK(length(reason_sha256)=64
    AND reason_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_by_user_id TEXT NOT NULL,
  reviewer_user_id TEXT,
  decision_reason TEXT CHECK(decision_reason IS NULL OR(typeof(decision_reason)='text'
    AND decision_reason=trim(decision_reason)
    AND length(CAST(decision_reason AS BLOB)) BETWEEN 1 AND 2000
    AND instr(decision_reason,char(0))=0)),
  decision_reason_sha256 TEXT CHECK(decision_reason_sha256 IS NULL OR(
    length(decision_reason_sha256)=64 AND decision_reason_sha256 NOT GLOB '*[^0-9a-f]*')),
  create_request_id TEXT NOT NULL CHECK(length(create_request_id) BETWEEN 1 AND 128),
  create_trace_id TEXT NOT NULL CHECK(length(create_trace_id) BETWEEN 1 AND 160),
  create_idempotency_sha256 TEXT NOT NULL CHECK(length(create_idempotency_sha256)=64
    AND create_idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  review_started_at TEXT CHECK(review_started_at IS NULL OR(length(review_started_at)=20
    AND review_started_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(review_started_at) IS NOT NULL)),
  resolved_at TEXT CHECK(resolved_at IS NULL OR(length(resolved_at)=20
    AND resolved_at GLOB '????-??-??T??:??:??Z' AND unixepoch(resolved_at) IS NOT NULL)),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,created_by_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(tenant_id,reviewer_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,billing_dispute_id),
  UNIQUE(tenant_id,create_idempotency_sha256),
  CHECK((scope_type='request_ids' AND period_start IS NULL AND period_end IS NULL)
    OR (scope_type='time_range' AND period_start IS NOT NULL AND period_end IS NOT NULL
      AND unixepoch(period_end)>unixepoch(period_start)
      AND unixepoch(period_end)-unixepoch(period_start)<=2678400)),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((status='submitted' AND state_revision=1 AND reviewer_user_id IS NULL
      AND decision_reason IS NULL AND decision_reason_sha256 IS NULL
      AND review_started_at IS NULL AND resolved_at IS NULL)
    OR (status='under_review' AND state_revision=2 AND reviewer_user_id IS NOT NULL
      AND decision_reason IS NULL AND decision_reason_sha256 IS NULL
      AND review_started_at IS NOT NULL AND resolved_at IS NULL)
    OR (status IN('approved','rejected') AND state_revision=3
      AND reviewer_user_id IS NOT NULL AND decision_reason IS NOT NULL
      AND decision_reason_sha256 IS NOT NULL AND review_started_at IS NOT NULL
      AND resolved_at IS NOT NULL)
    OR (status='cancelled' AND state_revision=2 AND reviewer_user_id IS NULL
      AND decision_reason IS NOT NULL AND decision_reason_sha256 IS NOT NULL
      AND review_started_at IS NULL AND resolved_at IS NOT NULL))
);

-- Item 冻结创建时 canonical Usage/FinOps/Price owner 与可冲正余额，之后只能由新争议覆盖迟到分录。
CREATE TABLE billing_dispute_items(
  billing_dispute_item_id TEXT PRIMARY KEY CHECK(length(billing_dispute_item_id)=37
    AND billing_dispute_item_id GLOB 'bdsi_[0-9a-f]*'
    AND substr(billing_dispute_item_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  billing_dispute_id TEXT NOT NULL,
  finops_ledger_entry_id TEXT NOT NULL,
  usage_ledger_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK(length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK(length(source_trace_id) BETWEEN 1 AND 160),
  ledger_sequence INTEGER NOT NULL CHECK(typeof(ledger_sequence)='integer' AND ledger_sequence>0),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  amount_micros INTEGER NOT NULL CHECK(typeof(amount_micros)='integer'
    AND amount_micros BETWEEN 1 AND 1000000000000000),
  disputable_micros INTEGER NOT NULL CHECK(typeof(disputable_micros)='integer'
    AND disputable_micros BETWEEN 1 AND 1000000000000000
    AND disputable_micros<=amount_micros),
  price_snapshot_id TEXT NOT NULL,
  price_schema_version TEXT NOT NULL CHECK(length(price_schema_version) BETWEEN 1 AND 128),
  price_snapshot_source TEXT NOT NULL CHECK(length(price_snapshot_source) BETWEEN 1 AND 128),
  price_evidence_status TEXT NOT NULL CHECK(price_evidence_status IN(
    'priced','unpriced','legacy_amount_only')),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,billing_dispute_id)
    REFERENCES billing_disputes(tenant_id,billing_dispute_id),
  FOREIGN KEY(tenant_id,finops_ledger_entry_id)
    REFERENCES finops_ledger_entries(tenant_id,finops_ledger_entry_id),
  FOREIGN KEY(tenant_id,usage_ledger_id) REFERENCES usage_ledger(tenant_id,usage_ledger_id),
  FOREIGN KEY(tenant_id,price_snapshot_id)
    REFERENCES price_snapshots(tenant_id,price_snapshot_id),
  UNIQUE(tenant_id,billing_dispute_item_id),
  UNIQUE(tenant_id,billing_dispute_id,finops_ledger_entry_id)
);

-- Transition 是所有状态变化、actor、幂等与 as-of 双水位查询的 append-only 权威事实。
CREATE TABLE billing_dispute_transitions(
  billing_dispute_transition_sequence INTEGER PRIMARY KEY AUTOINCREMENT
    CHECK(typeof(billing_dispute_transition_sequence)='integer'
      AND billing_dispute_transition_sequence>0),
  billing_dispute_transition_id TEXT NOT NULL UNIQUE CHECK(
    length(billing_dispute_transition_id)=37
    AND billing_dispute_transition_id GLOB 'bdtr_[0-9a-f]*'
    AND substr(billing_dispute_transition_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  billing_dispute_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN('create','start_review','approve','reject','cancel')),
  from_status TEXT CHECK(from_status IS NULL OR from_status IN('submitted','under_review')),
  to_status TEXT NOT NULL CHECK(to_status IN(
    'submitted','under_review','approved','rejected','cancelled')),
  expected_revision INTEGER NOT NULL CHECK(typeof(expected_revision)='integer'
    AND expected_revision>=0),
  new_revision INTEGER NOT NULL CHECK(typeof(new_revision)='integer'
    AND new_revision=expected_revision+1),
  actor_user_id TEXT NOT NULL,
  decision_reason_sha256 TEXT CHECK(decision_reason_sha256 IS NULL OR(
    length(decision_reason_sha256)=64 AND decision_reason_sha256 NOT GLOB '*[^0-9a-f]*')),
  approved_item_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(approved_item_count)='integer' AND approved_item_count BETWEEN 0 AND 100),
  approved_totals_sha256 TEXT CHECK(approved_totals_sha256 IS NULL OR(
    length(approved_totals_sha256)=64 AND approved_totals_sha256 NOT GLOB '*[^0-9a-f]*')),
  operation_request_id TEXT NOT NULL CHECK(length(operation_request_id) BETWEEN 1 AND 128),
  operation_trace_id TEXT NOT NULL CHECK(length(operation_trace_id) BETWEEN 1 AND 160),
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64
    AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,billing_dispute_id)
    REFERENCES billing_disputes(tenant_id,billing_dispute_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,billing_dispute_transition_id),
  UNIQUE(tenant_id,idempotency_key_sha256),
  UNIQUE(tenant_id,billing_dispute_id,new_revision),
  CHECK((action='create' AND from_status IS NULL AND to_status='submitted'
      AND expected_revision=0 AND new_revision=1 AND decision_reason_sha256 IS NULL
      AND approved_item_count=0 AND approved_totals_sha256 IS NULL)
    OR (action='start_review' AND from_status='submitted' AND to_status='under_review'
      AND expected_revision=1 AND new_revision=2 AND decision_reason_sha256 IS NULL
      AND approved_item_count=0 AND approved_totals_sha256 IS NULL)
    OR (action='approve' AND from_status='under_review' AND to_status='approved'
      AND expected_revision=2 AND new_revision=3 AND decision_reason_sha256 IS NOT NULL
      AND approved_item_count BETWEEN 1 AND 100 AND approved_totals_sha256 IS NOT NULL)
    OR (action='reject' AND from_status='under_review' AND to_status='rejected'
      AND expected_revision=2 AND new_revision=3 AND decision_reason_sha256 IS NOT NULL
      AND approved_item_count=0 AND approved_totals_sha256 IS NULL)
    OR (action='cancel' AND from_status='submitted' AND to_status='cancelled'
      AND expected_revision=1 AND new_revision=2 AND decision_reason_sha256 IS NOT NULL
      AND approved_item_count=0 AND approved_totals_sha256 IS NULL))
);

-- Hold 只覆盖结算展示，不改写正式账本；创建与释放均由即时 transition FK 固定 as-of 水位。
CREATE TABLE billing_settlement_holds(
  billing_settlement_hold_id TEXT PRIMARY KEY CHECK(length(billing_settlement_hold_id)=38
    AND billing_settlement_hold_id GLOB 'bhold_[0-9a-f]*'
    AND substr(billing_settlement_hold_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  billing_dispute_id TEXT NOT NULL,
  billing_dispute_item_id TEXT NOT NULL,
  finops_ledger_entry_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','released')),
  release_reason TEXT CHECK(release_reason IS NULL OR release_reason IN(
    'approved','rejected','cancelled')),
  state_revision INTEGER NOT NULL CHECK(state_revision IN(1,2)),
  created_transition_id TEXT NOT NULL,
  released_transition_id TEXT,
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR(length(released_at)=20
    AND released_at GLOB '????-??-??T??:??:??Z' AND unixepoch(released_at) IS NOT NULL)),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,billing_dispute_id)
    REFERENCES billing_disputes(tenant_id,billing_dispute_id),
  FOREIGN KEY(tenant_id,billing_dispute_item_id)
    REFERENCES billing_dispute_items(tenant_id,billing_dispute_item_id),
  FOREIGN KEY(tenant_id,finops_ledger_entry_id)
    REFERENCES finops_ledger_entries(tenant_id,finops_ledger_entry_id),
  FOREIGN KEY(tenant_id,created_transition_id)
    REFERENCES billing_dispute_transitions(tenant_id,billing_dispute_transition_id),
  FOREIGN KEY(tenant_id,released_transition_id)
    REFERENCES billing_dispute_transitions(tenant_id,billing_dispute_transition_id),
  UNIQUE(tenant_id,billing_settlement_hold_id),
  UNIQUE(tenant_id,billing_dispute_item_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((status='active' AND state_revision=1 AND release_reason IS NULL
      AND released_transition_id IS NULL AND released_at IS NULL
      AND updated_at=created_at)
    OR (status='released' AND state_revision=2 AND release_reason IS NOT NULL
      AND released_transition_id IS NOT NULL AND released_at IS NOT NULL
      AND updated_at=released_at AND unixepoch(released_at)>=unixepoch(created_at)))
);

-- Link 把每个批准 item 精确绑定到一条既有 reversal Adjustment 与正式 ledger entry。
CREATE TABLE billing_dispute_adjustment_links(
  billing_dispute_adjustment_link_id TEXT PRIMARY KEY CHECK(
    length(billing_dispute_adjustment_link_id)=37
    AND billing_dispute_adjustment_link_id GLOB 'bdal_[0-9a-f]*'
    AND substr(billing_dispute_adjustment_link_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  billing_dispute_id TEXT NOT NULL,
  billing_dispute_item_id TEXT NOT NULL,
  finops_adjustment_id TEXT NOT NULL,
  reversal_finops_ledger_entry_id TEXT NOT NULL,
  amount_micros INTEGER NOT NULL CHECK(typeof(amount_micros)='integer'
    AND amount_micros BETWEEN 1 AND 1000000000000000),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  billing_dispute_transition_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,billing_dispute_id)
    REFERENCES billing_disputes(tenant_id,billing_dispute_id),
  FOREIGN KEY(tenant_id,billing_dispute_item_id)
    REFERENCES billing_dispute_items(tenant_id,billing_dispute_item_id),
  FOREIGN KEY(tenant_id,finops_adjustment_id)
    REFERENCES finops_adjustments(tenant_id,finops_adjustment_id),
  FOREIGN KEY(tenant_id,reversal_finops_ledger_entry_id)
    REFERENCES finops_ledger_entries(tenant_id,finops_ledger_entry_id),
  FOREIGN KEY(tenant_id,billing_dispute_transition_id)
    REFERENCES billing_dispute_transitions(tenant_id,billing_dispute_transition_id),
  UNIQUE(tenant_id,billing_dispute_adjustment_link_id),
  UNIQUE(tenant_id,billing_dispute_item_id),
  UNIQUE(tenant_id,finops_adjustment_id),
  UNIQUE(tenant_id,reversal_finops_ledger_entry_id)
);

-- Created source 与 outbox 同事务落盘；外部 HTTP 采用稳定 delivery ID 的 at-least-once 语义。
CREATE TABLE billing_dispute_events(
  billing_dispute_event_id TEXT PRIMARY KEY CHECK(length(billing_dispute_event_id)=38
    AND billing_dispute_event_id GLOB 'bdevt_[0-9a-f]*'
    AND substr(billing_dispute_event_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  billing_dispute_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type='billing_dispute.created'),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(CAST(payload_json AS BLOB))<=4096),
  actor_user_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK(length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK(length(source_trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(length(occurred_at)=20
    AND occurred_at GLOB '????-??-??T??:??:??Z' AND unixepoch(occurred_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,billing_dispute_id)
    REFERENCES billing_disputes(tenant_id,billing_dispute_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,billing_dispute_event_id),
  UNIQUE(tenant_id,billing_dispute_id,event_type),
  CHECK(created_at=occurred_at)
);

CREATE TABLE billing_dispute_event_outbox(
  billing_dispute_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(billing_dispute_event_outbox_id)=37
    AND billing_dispute_event_outbox_id GLOB 'bdob_[0-9a-f]*'
    AND substr(billing_dispute_event_outbox_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  billing_dispute_event_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','processing','succeeded','failed')),
  attempt_count INTEGER NOT NULL CHECK(typeof(attempt_count)='integer'
    AND attempt_count BETWEEN 0 AND 10),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer'
    AND lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR(length(lease_expires_at)=20
    AND lease_expires_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(lease_expires_at) IS NOT NULL)),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR(length(next_attempt_at)=20
    AND next_attempt_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(next_attempt_at) IS NOT NULL)),
  delivery_snapshot_json TEXT NOT NULL CHECK(
    json_valid(delivery_snapshot_json) AND json_type(delivery_snapshot_json)='array'
    AND length(CAST(delivery_snapshot_json AS BLOB))<=65536),
  snapshot_frozen_at TEXT NOT NULL CHECK(length(snapshot_frozen_at)=20
    AND snapshot_frozen_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(snapshot_frozen_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'webhook.payload_invalid','webhook.delivery_unavailable')),
  succeeded_at TEXT CHECK(succeeded_at IS NULL OR(length(succeeded_at)=20
    AND succeeded_at GLOB '????-??-??T??:??:??Z' AND unixepoch(succeeded_at) IS NOT NULL)),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,billing_dispute_event_id)
    REFERENCES billing_dispute_events(tenant_id,billing_dispute_event_id),
  UNIQUE(tenant_id,billing_dispute_event_outbox_id),
  UNIQUE(tenant_id,billing_dispute_event_id),
  CHECK(snapshot_frozen_at=created_at),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((status='pending' AND attempt_count BETWEEN 0 AND 9
      AND next_attempt_at IS NOT NULL AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND succeeded_at IS NULL
      AND (last_error_code IS NULL OR last_error_code='webhook.delivery_unavailable'))
    OR (status='processing' AND attempt_count BETWEEN 0 AND 9
      AND next_attempt_at IS NULL AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND succeeded_at IS NULL AND last_error_code IS NULL)
    OR (status='succeeded' AND attempt_count BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND lease_owner_id IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL AND succeeded_at IS NOT NULL
      AND last_error_code IS NULL)
    OR (status='failed' AND attempt_count BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND succeeded_at IS NULL AND last_error_code IS NOT NULL))
);

-- 每次成功 claim 在同一事务取得全局单调序列；历史 claim 数不同也不会破坏 tenant 轮转。
CREATE TABLE billing_dispute_scheduler_tenants(
  tenant_id TEXT PRIMARY KEY,
  last_claimed_at TEXT NOT NULL CHECK(length(last_claimed_at)=20
    AND last_claimed_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(last_claimed_at) IS NOT NULL),
  last_claim_sequence INTEGER NOT NULL UNIQUE CHECK(typeof(last_claim_sequence)='integer'
    AND last_claim_sequence>=1),
  state_revision INTEGER NOT NULL CHECK(typeof(state_revision)='integer'
    AND state_revision>=1),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK(unixepoch(last_claimed_at)>=unixepoch(created_at))
);

CREATE INDEX idx_billing_disputes_tenant_sequence_prd080
  ON billing_disputes(tenant_id,billing_dispute_sequence DESC);
CREATE INDEX idx_billing_disputes_tenant_status_prd080
  ON billing_disputes(tenant_id,status,billing_dispute_sequence DESC);
CREATE INDEX idx_billing_dispute_items_owner_prd080
  ON billing_dispute_items(tenant_id,billing_dispute_id,ledger_sequence);
CREATE INDEX idx_billing_dispute_transitions_owner_prd080
  ON billing_dispute_transitions(
    tenant_id,billing_dispute_id,billing_dispute_transition_sequence);
CREATE INDEX idx_billing_holds_dispute_prd080
  ON billing_settlement_holds(tenant_id,billing_dispute_id,status);
CREATE UNIQUE INDEX uq_billing_holds_active_entry_prd080
  ON billing_settlement_holds(tenant_id,finops_ledger_entry_id) WHERE status='active';
CREATE INDEX idx_billing_adjustment_links_dispute_prd080
  ON billing_dispute_adjustment_links(tenant_id,billing_dispute_id,created_at);
CREATE INDEX idx_billing_dispute_events_tenant_prd080
  ON billing_dispute_events(tenant_id,occurred_at,billing_dispute_event_id);
CREATE INDEX idx_billing_dispute_outbox_due_prd080
  ON billing_dispute_event_outbox(
    status,next_attempt_at,lease_expires_at,tenant_id,billing_dispute_event_outbox_id)
  WHERE status IN('pending','processing');

-- Head 插入与更新都验证 reason 摘要、tenant actor 和固定状态机，客户端不能绕过服务层伪造 owner。
CREATE TRIGGER trg_billing_disputes_owner_insert_prd080
BEFORE INSERT ON billing_disputes FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM users user
    WHERE user.tenant_id=NEW.tenant_id AND user.user_id=NEW.created_by_user_id
      AND user.status='active')
  OR NEW.reason_sha256!=metahub_sha256(NEW.reason)
BEGIN SELECT RAISE(ABORT,'billing_dispute_owner_mismatch'); END;

CREATE TRIGGER trg_billing_disputes_transition_prd080
BEFORE UPDATE ON billing_disputes FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.billing_dispute_sequence!=OLD.billing_dispute_sequence
  OR NEW.billing_dispute_id!=OLD.billing_dispute_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.scope_type!=OLD.scope_type OR NEW.period_start IS NOT OLD.period_start
  OR NEW.period_end IS NOT OLD.period_end OR NEW.item_count!=OLD.item_count
  OR NEW.reason!=OLD.reason OR NEW.reason_sha256!=OLD.reason_sha256
  OR NEW.created_by_user_id!=OLD.created_by_user_id
  OR NEW.create_request_id!=OLD.create_request_id OR NEW.create_trace_id!=OLD.create_trace_id
  OR NEW.create_idempotency_sha256!=OLD.create_idempotency_sha256
  OR NEW.payload_sha256!=OLD.payload_sha256 OR NEW.created_at!=OLD.created_at
  OR NEW.state_revision!=OLD.state_revision+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR (OLD.status='submitted' AND NEW.status NOT IN('under_review','cancelled'))
  OR (OLD.status='under_review' AND NEW.status NOT IN('approved','rejected'))
  OR OLD.status IN('approved','rejected','cancelled')
  OR (NEW.status IN('under_review','approved','rejected')
    AND (NEW.reviewer_user_id=NEW.created_by_user_id OR NOT EXISTS(
      SELECT 1 FROM users user WHERE user.tenant_id=NEW.tenant_id
        AND user.user_id=NEW.reviewer_user_id AND user.status='active')))
  OR (NEW.decision_reason IS NOT NULL
    AND NEW.decision_reason_sha256!=metahub_sha256(NEW.decision_reason))
BEGIN SELECT RAISE(ABORT,'billing_dispute_transition_invalid'); END;

CREATE TRIGGER trg_billing_disputes_delete_prd080
BEFORE DELETE ON billing_disputes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_immutable'); END;

-- Item 必须逐字段等于同租户 canonical usage_charge，并冻结当时全部剩余可冲正金额。
CREATE TRIGGER trg_billing_dispute_items_owner_insert_prd080
BEFORE INSERT ON billing_dispute_items FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM billing_disputes dispute
    JOIN finops_ledger_entries entry
      ON entry.tenant_id=dispute.tenant_id
     AND entry.finops_ledger_entry_id=NEW.finops_ledger_entry_id
    JOIN usage_ledger usage
      ON usage.tenant_id=entry.tenant_id AND usage.usage_ledger_id=entry.usage_ledger_id
    JOIN price_snapshots snapshot
      ON snapshot.tenant_id=entry.tenant_id
     AND snapshot.price_snapshot_id=entry.price_snapshot_id
    WHERE dispute.tenant_id=NEW.tenant_id
      AND dispute.billing_dispute_id=NEW.billing_dispute_id
      AND dispute.status='submitted' AND dispute.state_revision=1
      AND entry.entry_type='usage_charge' AND entry.status='posted' AND entry.amount_micros>0
      AND entry.ledger_sequence=NEW.ledger_sequence
      AND entry.usage_ledger_id=NEW.usage_ledger_id
      AND entry.source_request_id=NEW.source_request_id
      AND entry.source_trace_id=NEW.source_trace_id
      AND usage.request_id=NEW.source_request_id
      AND entry.currency=NEW.currency AND entry.amount_micros=NEW.amount_micros
      AND entry.price_snapshot_id=NEW.price_snapshot_id
      AND entry.price_schema_version=NEW.price_schema_version
      AND entry.price_snapshot_source=NEW.price_snapshot_source
      AND entry.price_evidence_status=NEW.price_evidence_status
      AND snapshot.request_id=NEW.source_request_id
      AND snapshot.currency=NEW.currency
      AND snapshot.price_schema_version=NEW.price_schema_version
      AND snapshot.source=NEW.price_snapshot_source
      AND NEW.disputable_micros=entry.amount_micros-COALESCE((
        SELECT SUM(reversal.amount_micros) FROM finops_ledger_entries reversal
        WHERE reversal.tenant_id=entry.tenant_id AND reversal.entry_type='reversal'
          AND reversal.reversal_of_entry_id=entry.finops_ledger_entry_id),0)
      AND (SELECT COUNT(*) FROM billing_dispute_items current
        WHERE current.tenant_id=dispute.tenant_id
          AND current.billing_dispute_id=dispute.billing_dispute_id)<dispute.item_count
  )
BEGIN SELECT RAISE(ABORT,'billing_dispute_item_owner_mismatch'); END;

CREATE TRIGGER trg_billing_dispute_items_update_prd080
BEFORE UPDATE ON billing_dispute_items FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_item_immutable'); END;
CREATE TRIGGER trg_billing_dispute_items_delete_prd080
BEFORE DELETE ON billing_dispute_items FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_item_immutable'); END;

-- Transition 在 head CAS 之后插入；即时 FK 和 owner trigger 证明目标 revision、actor 与链路一致。
CREATE TRIGGER trg_billing_dispute_transitions_owner_insert_prd080
BEFORE INSERT ON billing_dispute_transitions FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM billing_disputes dispute JOIN users actor
      ON actor.tenant_id=dispute.tenant_id AND actor.user_id=NEW.actor_user_id
    WHERE dispute.tenant_id=NEW.tenant_id
      AND dispute.billing_dispute_id=NEW.billing_dispute_id
      AND actor.status='active' AND dispute.status=NEW.to_status
      AND dispute.state_revision=NEW.new_revision
      AND (
        (NEW.action='create' AND NEW.actor_user_id=dispute.created_by_user_id
          AND NEW.operation_request_id=dispute.create_request_id
          AND NEW.operation_trace_id=dispute.create_trace_id
          AND NEW.idempotency_key_sha256=dispute.create_idempotency_sha256
          AND NEW.payload_sha256=dispute.payload_sha256
          AND NEW.created_at=dispute.created_at)
        OR (NEW.action='start_review' AND NEW.actor_user_id!=dispute.created_by_user_id
          AND NEW.actor_user_id=dispute.reviewer_user_id
          AND NEW.created_at=dispute.review_started_at
          AND NEW.created_at=dispute.updated_at)
        OR (NEW.action IN('approve','reject')
          AND NEW.actor_user_id!=dispute.created_by_user_id
          AND NEW.actor_user_id=dispute.reviewer_user_id
          AND NEW.decision_reason_sha256=dispute.decision_reason_sha256
          AND NEW.created_at=dispute.resolved_at AND NEW.created_at=dispute.updated_at)
        OR (NEW.action='cancel' AND NEW.actor_user_id=dispute.created_by_user_id
          AND dispute.reviewer_user_id IS NULL
          AND NEW.decision_reason_sha256=dispute.decision_reason_sha256
          AND NEW.created_at=dispute.resolved_at AND NEW.created_at=dispute.updated_at)
      )
      AND (NEW.action!='approve' OR NEW.approved_item_count<=dispute.item_count)
  )
BEGIN SELECT RAISE(ABORT,'billing_dispute_transition_owner_mismatch'); END;

CREATE TRIGGER trg_billing_dispute_transitions_update_prd080
BEFORE UPDATE ON billing_dispute_transitions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_transition_immutable'); END;
CREATE TRIGGER trg_billing_dispute_transitions_delete_prd080
BEFORE DELETE ON billing_dispute_transitions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_transition_immutable'); END;

-- Hold 创建必须引用 create transition；释放只能引用同 dispute 的已提交终态 transition。
CREATE TRIGGER trg_billing_settlement_holds_owner_insert_prd080
BEFORE INSERT ON billing_settlement_holds FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM billing_dispute_items item
    JOIN billing_disputes dispute ON dispute.tenant_id=item.tenant_id
      AND dispute.billing_dispute_id=item.billing_dispute_id
    JOIN billing_dispute_transitions transition ON transition.tenant_id=item.tenant_id
      AND transition.billing_dispute_transition_id=NEW.created_transition_id
    WHERE item.tenant_id=NEW.tenant_id
      AND item.billing_dispute_item_id=NEW.billing_dispute_item_id
      AND item.billing_dispute_id=NEW.billing_dispute_id
      AND item.finops_ledger_entry_id=NEW.finops_ledger_entry_id
      AND dispute.status='submitted' AND transition.billing_dispute_id=item.billing_dispute_id
      AND transition.action='create' AND transition.new_revision=1
      AND NEW.status='active' AND NEW.state_revision=1
      AND NEW.created_at=transition.created_at AND NEW.updated_at=NEW.created_at
  )
BEGIN SELECT RAISE(ABORT,'billing_settlement_hold_owner_mismatch'); END;

CREATE TRIGGER trg_billing_settlement_holds_transition_prd080
BEFORE UPDATE ON billing_settlement_holds FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.billing_settlement_hold_id!=OLD.billing_settlement_hold_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.billing_dispute_id!=OLD.billing_dispute_id
  OR NEW.billing_dispute_item_id!=OLD.billing_dispute_item_id
  OR NEW.finops_ledger_entry_id!=OLD.finops_ledger_entry_id
  OR NEW.created_transition_id!=OLD.created_transition_id OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status!='released'
  OR NEW.state_revision!=OLD.state_revision+1
  OR NOT EXISTS(
    SELECT 1 FROM billing_disputes dispute
    JOIN billing_dispute_transitions transition
      ON transition.tenant_id=dispute.tenant_id
     AND transition.billing_dispute_transition_id=NEW.released_transition_id
    WHERE dispute.tenant_id=NEW.tenant_id
      AND dispute.billing_dispute_id=NEW.billing_dispute_id
      AND dispute.status=NEW.release_reason
      AND dispute.state_revision=transition.new_revision
      AND transition.billing_dispute_id=dispute.billing_dispute_id
      AND transition.action=CASE NEW.release_reason
        WHEN 'approved' THEN 'approve' WHEN 'rejected' THEN 'reject' ELSE 'cancel' END
      AND transition.to_status=NEW.release_reason
      AND NEW.released_at=transition.created_at AND NEW.updated_at=NEW.released_at
  )
BEGIN SELECT RAISE(ABORT,'billing_settlement_hold_transition_invalid'); END;

CREATE TRIGGER trg_billing_settlement_holds_delete_prd080
BEFORE DELETE ON billing_settlement_holds FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_settlement_hold_immutable'); END;

-- Link owner 必须同时证明 item、approve transition、Adjustment 与 reversal Entry 全部同租户同金额同链路。
CREATE TRIGGER trg_billing_dispute_adjustment_links_owner_insert_prd080
BEFORE INSERT ON billing_dispute_adjustment_links FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM billing_dispute_items item
    JOIN billing_disputes dispute ON dispute.tenant_id=item.tenant_id
      AND dispute.billing_dispute_id=item.billing_dispute_id
    JOIN billing_dispute_transitions transition ON transition.tenant_id=item.tenant_id
      AND transition.billing_dispute_transition_id=NEW.billing_dispute_transition_id
    JOIN finops_adjustments adjustment ON adjustment.tenant_id=item.tenant_id
      AND adjustment.finops_adjustment_id=NEW.finops_adjustment_id
    JOIN finops_ledger_entries reversal ON reversal.tenant_id=item.tenant_id
      AND reversal.finops_ledger_entry_id=NEW.reversal_finops_ledger_entry_id
    WHERE item.tenant_id=NEW.tenant_id
      AND item.billing_dispute_item_id=NEW.billing_dispute_item_id
      AND item.billing_dispute_id=NEW.billing_dispute_id
      AND dispute.status='approved' AND dispute.state_revision=transition.new_revision
      AND transition.billing_dispute_id=dispute.billing_dispute_id
      AND transition.action='approve' AND transition.to_status='approved'
      AND adjustment.adjustment_type='reversal'
      AND adjustment.reversal_of_entry_id=item.finops_ledger_entry_id
      AND adjustment.amount_micros=NEW.amount_micros
      AND adjustment.currency=NEW.currency
      AND adjustment.actor_user_id=transition.actor_user_id
      AND adjustment.operation_request_id=transition.operation_request_id
      AND adjustment.operation_trace_id=transition.operation_trace_id
      AND reversal.entry_type='reversal'
      AND reversal.finops_adjustment_id=adjustment.finops_adjustment_id
      AND reversal.reversal_of_entry_id=item.finops_ledger_entry_id
      AND reversal.amount_micros=NEW.amount_micros AND reversal.currency=NEW.currency
      AND reversal.actor_user_id=transition.actor_user_id
      AND reversal.operation_request_id=transition.operation_request_id
      AND reversal.operation_trace_id=transition.operation_trace_id
      AND NEW.currency=item.currency AND NEW.amount_micros<=item.disputable_micros
      AND NEW.created_at=transition.created_at
  )
BEGIN SELECT RAISE(ABORT,'billing_dispute_adjustment_link_owner_mismatch'); END;

CREATE TRIGGER trg_billing_dispute_adjustment_links_update_prd080
BEFORE UPDATE ON billing_dispute_adjustment_links FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_adjustment_link_immutable'); END;
CREATE TRIGGER trg_billing_dispute_adjustment_links_delete_prd080
BEFORE DELETE ON billing_dispute_adjustment_links FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_adjustment_link_immutable'); END;

-- Source payload 精确绑定 immutable item 汇总；自由文本、请求列表和价格证据无法借额外键出网。
CREATE TRIGGER trg_billing_dispute_events_owner_insert_prd080
BEFORE INSERT ON billing_dispute_events FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM billing_disputes dispute
    WHERE dispute.tenant_id=NEW.tenant_id
      AND dispute.billing_dispute_id=NEW.billing_dispute_id
      AND dispute.status='submitted' AND dispute.state_revision=1
      AND dispute.created_by_user_id=NEW.actor_user_id
      AND dispute.create_request_id=NEW.source_request_id
      AND dispute.create_trace_id=NEW.source_trace_id
      AND dispute.created_at=NEW.occurred_at
      AND (SELECT COUNT(*) FROM billing_dispute_items item
        WHERE item.tenant_id=dispute.tenant_id
          AND item.billing_dispute_id=dispute.billing_dispute_id)=dispute.item_count
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=7
      AND NOT EXISTS(SELECT 1 FROM json_each(NEW.payload_json) field
        WHERE field.key NOT IN('schema_version','billing_dispute_id','scope_type',
          'item_count','currency_totals','status','created_at'))
      AND json_type(NEW.payload_json,'$.schema_version')='integer'
      AND json_extract(NEW.payload_json,'$.schema_version')=1
      AND json_extract(NEW.payload_json,'$.billing_dispute_id')=dispute.billing_dispute_id
      AND json_extract(NEW.payload_json,'$.scope_type')=dispute.scope_type
      AND json_extract(NEW.payload_json,'$.item_count')=dispute.item_count
      AND json_extract(NEW.payload_json,'$.status')='submitted'
      AND json_extract(NEW.payload_json,'$.created_at')=dispute.created_at
      AND json_type(NEW.payload_json,'$.currency_totals')='object'
      AND NOT EXISTS(SELECT 1 FROM json_each(NEW.payload_json,'$.currency_totals') total
        WHERE total.key NOT IN('CNY','USD') OR total.type!='text')
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json,'$.currency_totals'))
        =(SELECT COUNT(DISTINCT item.currency) FROM billing_dispute_items item
          WHERE item.tenant_id=dispute.tenant_id
            AND item.billing_dispute_id=dispute.billing_dispute_id)
      AND ((NOT EXISTS(SELECT 1 FROM billing_dispute_items item
            WHERE item.tenant_id=dispute.tenant_id
              AND item.billing_dispute_id=dispute.billing_dispute_id AND item.currency='CNY')
          AND json_type(NEW.payload_json,'$.currency_totals.CNY') IS NULL)
        OR json_extract(NEW.payload_json,'$.currency_totals.CNY')=(
          SELECT printf('%d.%06d',SUM(item.disputable_micros)/1000000,
            SUM(item.disputable_micros)%1000000)
          FROM billing_dispute_items item WHERE item.tenant_id=dispute.tenant_id
            AND item.billing_dispute_id=dispute.billing_dispute_id AND item.currency='CNY'))
      AND ((NOT EXISTS(SELECT 1 FROM billing_dispute_items item
            WHERE item.tenant_id=dispute.tenant_id
              AND item.billing_dispute_id=dispute.billing_dispute_id AND item.currency='USD')
          AND json_type(NEW.payload_json,'$.currency_totals.USD') IS NULL)
        OR json_extract(NEW.payload_json,'$.currency_totals.USD')=(
          SELECT printf('%d.%06d',SUM(item.disputable_micros)/1000000,
            SUM(item.disputable_micros)%1000000)
          FROM billing_dispute_items item WHERE item.tenant_id=dispute.tenant_id
            AND item.billing_dispute_id=dispute.billing_dispute_id AND item.currency='USD'))
  )
BEGIN SELECT RAISE(ABORT,'billing_dispute_event_owner_mismatch'); END;

CREATE TRIGGER trg_billing_dispute_events_update_prd080
BEFORE UPDATE ON billing_dispute_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_event_immutable'); END;
CREATE TRIGGER trg_billing_dispute_events_delete_prd080
BEFORE DELETE ON billing_dispute_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_event_immutable'); END;

-- Outbox 初始事实必须与 source 同事务创建；claim、续租、重试和终态只允许 generation/token CAS。
CREATE TRIGGER trg_billing_dispute_outbox_owner_insert_prd080
BEFORE INSERT ON billing_dispute_event_outbox FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM billing_dispute_events event
    WHERE event.tenant_id=NEW.tenant_id
      AND event.billing_dispute_event_id=NEW.billing_dispute_event_id
      AND event.created_at=NEW.created_at)
  OR NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.lease_generation!=0
  OR NEW.snapshot_frozen_at!=NEW.created_at
  OR NEW.delivery_snapshot_json IS NOT (
    SELECT json_group_array(webhook_delivery_id) FROM (
      SELECT webhook_delivery_id FROM webhook_deliveries
      WHERE tenant_id=NEW.tenant_id AND event_id=NEW.billing_dispute_event_id
        AND delivery_kind='original'
      ORDER BY webhook_delivery_id))
  OR EXISTS(SELECT 1 FROM json_each(NEW.delivery_snapshot_json) delivery
    WHERE delivery.type!='text' OR length(delivery.value)!=36
      OR delivery.value NOT GLOB 'whd_[0-9a-f]*'
      OR substr(delivery.value,5) GLOB '*[^0-9a-f]*')
  OR EXISTS(SELECT 1 FROM json_each(NEW.delivery_snapshot_json) left_delivery
    JOIN json_each(NEW.delivery_snapshot_json) right_delivery
      ON left_delivery.key<right_delivery.key
     AND left_delivery.value=right_delivery.value)
  OR EXISTS(SELECT 1 FROM webhook_deliveries delivery
    WHERE delivery.tenant_id=NEW.tenant_id
      AND delivery.event_id=NEW.billing_dispute_event_id
      AND delivery.delivery_kind='original'
      AND delivery.created_at!=NEW.created_at)
  OR NEW.last_error_code IS NOT NULL OR NEW.succeeded_at IS NOT NULL
  OR NEW.created_at!=NEW.updated_at
BEGIN SELECT RAISE(ABORT,'billing_dispute_outbox_owner_mismatch'); END;

CREATE TRIGGER trg_billing_dispute_outbox_transition_prd080
BEFORE UPDATE ON billing_dispute_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.billing_dispute_event_outbox_id!=OLD.billing_dispute_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.billing_dispute_event_id!=OLD.billing_dispute_event_id
  OR NEW.created_at!=OLD.created_at OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR OLD.status IN('succeeded','failed')
  OR NEW.lease_generation!=OLD.lease_generation+1
  OR (OLD.status='pending' AND NOT(
    NEW.status='processing' AND NEW.attempt_count=OLD.attempt_count))
  OR (OLD.status='processing' AND NOT(
    (NEW.status='processing' AND NEW.attempt_count=OLD.attempt_count)
    OR (NEW.status='pending' AND (
      (NEW.attempt_count=OLD.attempt_count+1)
      OR (NEW.attempt_count=OLD.attempt_count AND NEW.last_error_code IS NULL
        AND NEW.next_attempt_at=NEW.updated_at)))
    OR (NEW.status IN('succeeded','failed')
      AND NEW.attempt_count=OLD.attempt_count+1)))
  OR NEW.delivery_snapshot_json IS NOT OLD.delivery_snapshot_json
  OR NEW.snapshot_frozen_at IS NOT OLD.snapshot_frozen_at
BEGIN SELECT RAISE(ABORT,'billing_dispute_outbox_transition_invalid'); END;

CREATE TRIGGER trg_billing_dispute_outbox_delete_prd080
BEFORE DELETE ON billing_dispute_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_outbox_immutable'); END;

CREATE TRIGGER trg_billing_dispute_scheduler_owner_insert_prd080
BEFORE INSERT ON billing_dispute_scheduler_tenants FOR EACH ROW
WHEN NEW.last_claim_sequence!=(SELECT COALESCE(MAX(last_claim_sequence),0)+1
      FROM billing_dispute_scheduler_tenants)
  OR NEW.state_revision!=1 OR NEW.last_claimed_at!=NEW.created_at
  OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
BEGIN SELECT RAISE(ABORT,'billing_dispute_scheduler_owner_mismatch'); END;

CREATE TRIGGER trg_billing_dispute_scheduler_transition_prd080
BEFORE UPDATE ON billing_dispute_scheduler_tenants FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.state_revision!=OLD.state_revision+1
  OR NEW.last_claim_sequence!=(SELECT MAX(last_claim_sequence)+1
      FROM billing_dispute_scheduler_tenants)
  OR unixepoch(NEW.last_claimed_at)<unixepoch(OLD.last_claimed_at)
  OR NEW.last_claimed_at!=NEW.updated_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'billing_dispute_scheduler_cursor_invalid'); END;

CREATE TRIGGER trg_billing_dispute_scheduler_delete_prd080
BEFORE DELETE ON billing_dispute_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_scheduler_cursor_immutable'); END;

-- 每次状态迁移只能有一条争议 Audit；owner trigger 将安全摘要绑定 transition 与持久业务终态。
CREATE UNIQUE INDEX idx_audit_logs_billing_dispute_transition_prd080
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN('billing_dispute.created','billing_dispute.review_started',
    'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled');
CREATE TRIGGER trg_audit_logs_billing_dispute_owner_insert_prd080
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN('billing_dispute.created','billing_dispute.review_started',
    'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled')
  AND (
    NEW.object_type!='billing_dispute' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS(
      SELECT 1 FROM billing_disputes dispute
      JOIN billing_dispute_transitions transition
        ON transition.tenant_id=dispute.tenant_id
       AND transition.billing_dispute_id=dispute.billing_dispute_id
      WHERE dispute.tenant_id=NEW.tenant_id AND dispute.billing_dispute_id=NEW.object_id
        AND transition.action=CASE NEW.action
          WHEN 'billing_dispute.created' THEN 'create'
          WHEN 'billing_dispute.review_started' THEN 'start_review'
          WHEN 'billing_dispute.approved' THEN 'approve'
          WHEN 'billing_dispute.rejected' THEN 'reject' ELSE 'cancel' END
        AND transition.actor_user_id=NEW.actor_user_id
        AND transition.operation_request_id=NEW.request_id
        AND transition.operation_trace_id=NEW.trace_id
        AND transition.to_status=dispute.status
        AND transition.new_revision=dispute.state_revision
        AND ((transition.action='create' AND NEW.before_summary_json='{}')
          OR (transition.action!='create'
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=7
            AND NOT EXISTS(SELECT 1 FROM json_each(NEW.before_summary_json) field
              WHERE field.key NOT IN('status','state_revision','scope_type','item_count',
                'adjustment_count','currency_totals_sha256','reason_sha256'))
            AND json_extract(NEW.before_summary_json,'$.status')=transition.from_status
            AND json_extract(NEW.before_summary_json,'$.state_revision')
              =transition.expected_revision
            AND json_extract(NEW.before_summary_json,'$.scope_type')=dispute.scope_type
            AND json_extract(NEW.before_summary_json,'$.item_count')=dispute.item_count
            AND json_extract(NEW.before_summary_json,'$.adjustment_count')=0
            AND json_extract(NEW.before_summary_json,'$.currency_totals_sha256')
              =metahub_sha256('{}')
            AND json_extract(NEW.before_summary_json,'$.reason_sha256')
              =dispute.reason_sha256))
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=7
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN('status','state_revision','scope_type','item_count',
            'adjustment_count','currency_totals_sha256','reason_sha256'))
        AND json_extract(NEW.after_summary_json,'$.status')=dispute.status
        AND json_extract(NEW.after_summary_json,'$.state_revision')=dispute.state_revision
        AND json_extract(NEW.after_summary_json,'$.scope_type')=dispute.scope_type
        AND json_extract(NEW.after_summary_json,'$.item_count')=dispute.item_count
        AND json_extract(NEW.after_summary_json,'$.adjustment_count')
          =transition.approved_item_count
        AND json_extract(NEW.after_summary_json,'$.currency_totals_sha256')=
          CASE transition.action
            WHEN 'create' THEN (
              SELECT metahub_sha256(json_extract(event.payload_json,'$.currency_totals'))
              FROM billing_dispute_events event
              WHERE event.tenant_id=dispute.tenant_id
                AND event.billing_dispute_id=dispute.billing_dispute_id
                AND event.event_type='billing_dispute.created')
            WHEN 'approve' THEN transition.approved_totals_sha256
            ELSE metahub_sha256('{}')
          END
        AND json_extract(NEW.after_summary_json,'$.reason_sha256')=dispute.reason_sha256
        AND NEW.changed_fields_json=CASE transition.action
          WHEN 'create' THEN
            '["adjustment_count","currency_totals_sha256","item_count","reason_sha256","scope_type","state_revision","status"]'
          WHEN 'approve' THEN
            '["adjustment_count","currency_totals_sha256","state_revision","status"]'
          ELSE '["state_revision","status"]' END
        AND (SELECT COUNT(*) FROM billing_dispute_adjustment_links link
          WHERE link.tenant_id=dispute.tenant_id
            AND link.billing_dispute_id=dispute.billing_dispute_id
            AND link.billing_dispute_transition_id=transition.billing_dispute_transition_id)
          =transition.approved_item_count
        AND ((transition.action IN('approve','reject','cancel')
            AND NOT EXISTS(SELECT 1 FROM billing_settlement_holds hold
              WHERE hold.tenant_id=dispute.tenant_id
                AND hold.billing_dispute_id=dispute.billing_dispute_id
                AND hold.status='active'))
          OR (transition.action IN('create','start_review')
            AND (SELECT COUNT(*) FROM billing_settlement_holds hold
              WHERE hold.tenant_id=dispute.tenant_id
                AND hold.billing_dispute_id=dispute.billing_dispute_id
                AND hold.status='active')=dispute.item_count))
    )
  )
)
OR (NEW.object_type='billing_dispute' AND NEW.action NOT IN(
  'billing_dispute.created','billing_dispute.review_started','billing_dispute.approved',
  'billing_dispute.rejected','billing_dispute.cancelled'))
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

-- 提交前验证 revision-16 hash、八表、即时 owner/CAS、Audit 扩展与数据库完整性。
DROP TABLE IF EXISTS temp.prd080_up_final_guard;
CREATE TEMP TABLE prd080_up_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd080_up_final_guard_abort
BEFORE INSERT ON prd080_up_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd080_up_final_invalid'); END;
INSERT INTO prd080_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=79
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=16)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='91355c246758e6cc1584130371d86da44f1fe618beb869e85cbfdacadce0e5b9'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=208
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='33a6e57cbce2c26014fb1f39c8f69f0c3f12f98c0f639766b1579e3c908ad757'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=16
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'billing_disputes','billing_dispute_items','billing_settlement_holds',
      'billing_dispute_transitions','billing_dispute_adjustment_links',
      'billing_dispute_events','billing_dispute_event_outbox',
      'billing_dispute_scheduler_tenants'))=8 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT group_concat(name,',') FROM pragma_table_info(
      'billing_dispute_scheduler_tenants'))=
      'tenant_id,last_claimed_at,last_claim_sequence,state_revision,created_at,updated_at'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN(
      'idx_billing_disputes_tenant_sequence_prd080','idx_billing_disputes_tenant_status_prd080',
      'idx_billing_dispute_items_owner_prd080','idx_billing_dispute_transitions_owner_prd080',
      'idx_billing_holds_dispute_prd080','uq_billing_holds_active_entry_prd080',
      'idx_billing_adjustment_links_dispute_prd080','idx_billing_dispute_events_tenant_prd080',
      'idx_billing_dispute_outbox_due_prd080',
      'idx_audit_logs_billing_dispute_transition_prd080'))=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN(
      'trg_billing_disputes_owner_insert_prd080','trg_billing_disputes_transition_prd080',
      'trg_billing_disputes_delete_prd080','trg_billing_dispute_items_owner_insert_prd080',
      'trg_billing_dispute_items_update_prd080','trg_billing_dispute_items_delete_prd080',
      'trg_billing_dispute_transitions_owner_insert_prd080',
      'trg_billing_dispute_transitions_update_prd080',
      'trg_billing_dispute_transitions_delete_prd080',
      'trg_billing_settlement_holds_owner_insert_prd080',
      'trg_billing_settlement_holds_transition_prd080',
      'trg_billing_settlement_holds_delete_prd080',
      'trg_billing_dispute_adjustment_links_owner_insert_prd080',
      'trg_billing_dispute_adjustment_links_update_prd080',
      'trg_billing_dispute_adjustment_links_delete_prd080',
      'trg_billing_dispute_events_owner_insert_prd080',
      'trg_billing_dispute_events_update_prd080','trg_billing_dispute_events_delete_prd080',
      'trg_billing_dispute_outbox_owner_insert_prd080',
      'trg_billing_dispute_outbox_transition_prd080','trg_billing_dispute_outbox_delete_prd080',
      'trg_billing_dispute_scheduler_owner_insert_prd080',
      'trg_billing_dispute_scheduler_transition_prd080',
      'trg_billing_dispute_scheduler_delete_prd080',
      'trg_audit_logs_billing_dispute_owner_insert_prd080'))=25 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
      '''billing_dispute.created'',''billing_dispute.review_started''')>0
      AND instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),
        '''quota_adjustment'',''billing_dispute''')>0
      AND EXISTS(SELECT 1 FROM sqlite_schema WHERE type='trigger'
        AND name='trg_audit_logs_plan_billing_owner_insert_prd079')
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd080_up_final_guard_abort;
DROP TABLE prd080_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
