-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
-- PRD-085 回滚：移除产品分析事实与权限，同时恢复修订十九的租户权限目录。

CREATE TEMP TABLE prd085_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'e9fc70ecbb0aee117f913bad242eb62d23ef0374a905497ee391caae31951272'
    WHEN 'ops_admin' THEN 'c86c645b5c090c3ad8b1152ed783335ba82c788fc9690c4d803dd76f14eba8e7'
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

-- 从 0077 权威来源重算允许保留的历史事实，回滚前用于拒绝不可逆实时数据。
CREATE TEMP TABLE prd085_down_expected_backfill(
  product_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor_user_id TEXT,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  operation_request_id TEXT,
  operation_trace_id TEXT,
  occurred_at TEXT NOT NULL
);
-- 从租户和首次成功种子记录重算唯一的注册历史事实。
INSERT INTO prd085_down_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    tenant.tenant_id,'tenant.registered',
    CASE WHEN seed.seed_run_id IS NULL THEN 'tenant' ELSE 'seed_run' END,
    COALESCE(seed.seed_run_id,tenant.tenant_id))),1,32),
  tenant.tenant_id,'tenant.registered',NULL,
  CASE WHEN seed.seed_run_id IS NULL THEN 'tenant' ELSE 'seed_run' END,
  COALESCE(seed.seed_run_id,tenant.tenant_id),NULL,NULL,
  COALESCE(seed.created_at,tenant.created_at)
FROM tenants tenant
LEFT JOIN seed_runs seed ON seed.seed_run_id=(
  SELECT candidate.seed_run_id FROM seed_runs candidate
  WHERE candidate.tenant_id=tenant.tenant_id AND candidate.status='applied'
  ORDER BY candidate.created_at,candidate.seed_run_id LIMIT 1);

-- 从审计日志重算 Provider、虚拟密钥、预算和邀请历史事实。
INSERT INTO prd085_down_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    audit.tenant_id,
    CASE audit.action
      WHEN 'provider.created' THEN 'provider.created'
      WHEN 'virtual_key.created' THEN 'virtual_key.created'
      WHEN 'budget_rule.created' THEN 'budget.configured'
      WHEN 'budget_rule.updated' THEN 'budget.configured'
      WHEN 'user.invited' THEN 'teammate.invited' END,
    'audit_log',audit.audit_log_id)),1,32),
  audit.tenant_id,
  CASE audit.action
    WHEN 'provider.created' THEN 'provider.created'
    WHEN 'virtual_key.created' THEN 'virtual_key.created'
    WHEN 'budget_rule.created' THEN 'budget.configured'
    WHEN 'budget_rule.updated' THEN 'budget.configured'
    WHEN 'user.invited' THEN 'teammate.invited' END,
  audit.actor_user_id,'audit_log',audit.audit_log_id,
  audit.request_id,audit.trace_id,audit.created_at
FROM audit_logs audit
WHERE audit.action IN(
  'provider.created','virtual_key.created','budget_rule.created',
  'budget_rule.updated','user.invited');

-- 配置导入确实创建 Provider 时重算对应的历史事实。
INSERT INTO prd085_down_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    audit.tenant_id,'provider.created','audit_log',audit.audit_log_id)),1,32),
  audit.tenant_id,'provider.created',audit.actor_user_id,
  'audit_log',audit.audit_log_id,audit.request_id,audit.trace_id,audit.created_at
FROM audit_logs audit
WHERE audit.action='config.imported'
  AND json_type(audit.after_summary_json,'$.providers_created_count')='integer'
  AND json_extract(audit.after_summary_json,'$.providers_created_count')>0;

-- 配置导入确实创建虚拟密钥时重算对应的历史事实。
INSERT INTO prd085_down_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    audit.tenant_id,'virtual_key.created','audit_log',audit.audit_log_id)),1,32),
  audit.tenant_id,'virtual_key.created',audit.actor_user_id,
  'audit_log',audit.audit_log_id,audit.request_id,audit.trace_id,audit.created_at
FROM audit_logs audit
WHERE audit.action='config.imported'
  AND json_type(audit.after_summary_json,'$.virtual_keys_created_count')='integer'
  AND json_extract(audit.after_summary_json,'$.virtual_keys_created_count')>0;

-- 从每个租户的最早成功请求重算首次网关调用历史事实。
INSERT INTO prd085_down_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    request.tenant_id,'gateway.first_call_succeeded','request_log',request.request_id)),1,32),
  request.tenant_id,'gateway.first_call_succeeded',NULL,
  'request_log',request.request_id,request.request_id,request.trace_id,request.created_at
FROM request_logs request
WHERE request.status='success' AND NOT EXISTS(
  SELECT 1 FROM request_logs earlier
  WHERE earlier.tenant_id=request.tenant_id AND earlier.status='success'
    AND(earlier.created_at<request.created_at
      OR(earlier.created_at=request.created_at AND earlier.request_id<request.request_id)));

-- 从请求日志查询审计记录重算日志查看历史事实。
INSERT INTO prd085_down_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    query.tenant_id,'request_logs.viewed','request_log_query',query.query_event_id)),1,32),
  query.tenant_id,'request_logs.viewed',query.actor_user_id,
  'request_log_query',query.query_event_id,
  query.operation_request_id,query.operation_trace_id,query.executed_at
FROM request_log_query_events query;

-- 清理本连接可能残留的回滚门禁，避免旧临时对象影响本次校验。
DROP TABLE IF EXISTS temp.prd085_down_guard;
CREATE TEMP TABLE prd085_down_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd085_down_guard_abort
BEFORE INSERT ON prd085_down_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd085_down_guard_failed'); END;
-- 校验版本、目录、矩阵及无实时投递副作用后才允许继续回滚。
INSERT INTO prd085_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0078' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=89
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=20)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='5f77eaca2da3b37026d9891ee7fab0f4aa121fbd2ac775bcc2b21d740aacca14'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=234
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='4602777ad3937aa0351a4b9949826756904a4ee3b485d65984cdbecdce127711'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM product_events WHERE capture_mode='live')
  + (SELECT COUNT(*) FROM product_event_outbox)
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE NOT json_valid(endpoint.event_types_json) OR EXISTS(
        SELECT 1 FROM json_each(endpoint.event_types_json) event
        WHERE event.value='product_event.recorded'))
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE event_type='product_event.recorded')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type='product_event.recorded')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
      WHERE event_type='product_event.recorded')
  + (SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
      LEFT JOIN prd085_roles_to_contract selected
        ON selected.tenant_id=relation.tenant_id AND selected.role_id=relation.role_id
      WHERE permission.code='product_analytics.read' AND selected.role_id IS NULL)
  + (SELECT COUNT(*) FROM roles role
      WHERE role.matrix_sha256 IN(
        'e9fc70ecbb0aee117f913bad242eb62d23ef0374a905497ee391caae31951272',
        'c86c645b5c090c3ad8b1152ed783335ba82c788fc9690c4d803dd76f14eba8e7')
        AND NOT EXISTS(SELECT 1 FROM prd085_roles_to_contract selected
          WHERE selected.tenant_id=role.tenant_id AND selected.role_id=role.role_id))
  + CASE WHEN NOT EXISTS(
      SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
        operation_request_id,operation_trace_id,occurred_at
      FROM product_events WHERE capture_mode='backfill'
      EXCEPT
      SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
        operation_request_id,operation_trace_id,occurred_at
      FROM prd085_down_expected_backfill)
      AND NOT EXISTS(
      SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
        operation_request_id,operation_trace_id,occurred_at
      FROM prd085_down_expected_backfill
      EXCEPT
      SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
        operation_request_id,operation_trace_id,occurred_at
      FROM product_events WHERE capture_mode='backfill')
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table'
      AND name IN('product_events','product_event_outbox'))=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE '%_prd085')=12 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd085_down_guard_abort;
DROP TABLE prd085_down_guard;

DROP TRIGGER trg_seed_runs_product_event_insert_prd085;
DROP TRIGGER trg_audit_logs_product_event_insert_prd085;
DROP TRIGGER trg_request_logs_product_event_insert_prd085;
DROP TRIGGER trg_request_logs_product_event_update_prd085;
DROP TRIGGER trg_request_log_queries_product_event_insert_prd085;
DROP TRIGGER trg_product_events_outbox_insert_prd085;
DROP TRIGGER trg_product_event_outbox_owner_insert_prd085;
DROP TRIGGER trg_product_event_outbox_transition_prd085;
DROP TRIGGER trg_product_event_outbox_delete_prd085;
DROP TRIGGER trg_product_events_owner_insert_prd085;
DROP TRIGGER trg_product_events_update_prd085;
DROP TRIGGER trg_product_events_delete_prd085;
DROP TABLE product_event_outbox;
DROP TABLE product_events;

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

-- 只撤销迁移时自动扩展的角色权限关系，不触碰用户自定义矩阵。
DELETE FROM role_permissions
WHERE permission_id IN(
  SELECT permission_id FROM permissions WHERE code='product_analytics.read')
  AND EXISTS(SELECT 1 FROM prd085_roles_to_contract selected
    WHERE selected.tenant_id=role_permissions.tenant_id
      AND selected.role_id=role_permissions.role_id);
-- 将被自动扩展角色的修订号和矩阵哈希恢复为迁移前审定值。
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
    WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
  END
WHERE role_id IN(SELECT role_id FROM prd085_roles_to_contract);

-- 从默认角色模板中删除产品分析权限的两条自动授予关系。
DELETE FROM rbac_role_permission_templates
WHERE permission_code='product_analytics.read';
CREATE TABLE rbac_permission_templates_prd085_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=19)
);
-- 排除产品分析权限并将其余模板保序复制回修订十九目录。
INSERT INTO rbac_permission_templates_prd085_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,19
FROM rbac_permission_templates WHERE permission_code!='product_analytics.read';
DROP TABLE rbac_permission_templates;
-- 用已完成过滤的新表原子恢复修订十九权限模板目录。
ALTER TABLE rbac_permission_templates_prd085_old RENAME TO rbac_permission_templates;

CREATE TABLE permissions_prd085_old(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=19),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 排除产品分析实例并将所有租户旧权限保序恢复到修订十九。
INSERT INTO permissions_prd085_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  19,status,created_at,updated_at
FROM permissions WHERE code!='product_analytics.read';
DROP TABLE permissions;
-- 用已恢复的租户权限表原子替换修订二十表结构。
ALTER TABLE permissions_prd085_old RENAME TO permissions;
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
    WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
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
  -- 回滚后新租户初始化六个内置角色，并绑定修订十九的冻结矩阵哈希。
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
      WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
      WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 为回滚后的新租户实例化修订十九目录中的全部权限。
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,19,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  -- 按修订十九内置模板写入租户内角色权限关系，禁止跨租户连接。
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

-- 清理本连接可能残留的回滚终态门禁，确保最终校验不受旧对象影响。
DROP TABLE IF EXISTS temp.prd085_down_final_guard;
CREATE TEMP TABLE prd085_down_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd085_down_final_guard_abort
BEFORE INSERT ON prd085_down_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd085_down_final_invalid'); END;
-- 聚合校验目录、矩阵、对象移除、外键与数据库完整性后再提交回滚。
INSERT INTO prd085_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=88
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
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=19
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
      AND NOT EXISTS(SELECT 1 FROM permissions WHERE code='product_analytics.read')
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM prd085_roles_to_contract selected
      JOIN roles role ON role.tenant_id=selected.tenant_id AND role.role_id=selected.role_id
      WHERE role.matrix_sha256!=CASE role.code
        WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
        WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
      END OR EXISTS(
        SELECT 1 FROM role_permissions relation JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
          AND permission.code='product_analytics.read'))
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM sqlite_schema WHERE name IN(
      'product_events','product_event_outbox')) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM sqlite_schema
      WHERE type='trigger' AND name LIKE '%_prd085') THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE json_valid(endpoint.event_types_json) AND EXISTS(
        SELECT 1 FROM json_each(endpoint.event_types_json) event
        WHERE event.value='product_event.recorded'))
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE event_type='product_event.recorded')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
      WHERE event_type='product_event.recorded')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd085_down_final_guard_abort;
DROP TABLE prd085_down_final_guard;
DROP TABLE prd085_down_expected_backfill;
DROP TABLE prd085_roles_to_contract;

PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
