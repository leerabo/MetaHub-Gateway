-- PRD-062 回滚：仅在没有任何配置迁移、导出、审计或显式权限变更证据时恢复 revision 1。
-- 一旦产生业务事实，删除表或目录项会破坏审计链，必须在首项持久 DDL 前失败关闭。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 业务事实与结构漂移分开报错：前者明确表示回滚会销毁证据，调用方不得把它误判为可重试的
-- SQL 兼容问题。临时 trigger 在任何持久 DDL 前抛出稳定错误并回滚整个 down。
CREATE TEMP TABLE prd062_down_business_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd062_down_business_guard
BEFORE INSERT ON prd062_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd062_down_business_facts_present'); END;
-- 先汇总配置事实、审计及显式新权限事件；存在任一治理证据都以稳定原因拒绝降级。
INSERT INTO prd062_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM config_imports)
  + (SELECT COUNT(*) FROM config_import_rows)
  + (SELECT COUNT(*) FROM config_exports)
  + (SELECT COUNT(*) FROM audit_logs
     WHERE action IN ('config.imported','config.exported')
        OR object_type='config_transfer')
  + (SELECT COUNT(*)
     FROM role_permission_events event
     WHERE EXISTS (
       SELECT 1 FROM json_each(event.added_permissions_json) item
       WHERE item.value IN ('config.transfer.read','config.transfer.write')
     ) OR EXISTS (
       SELECT 1 FROM json_each(event.removed_permissions_json) item
       WHERE item.value IN ('config.transfer.read','config.transfer.write')
     ));
DROP TRIGGER trg_prd062_down_business_guard;
DROP TABLE prd062_down_business_guard;

CREATE TEMP TABLE prd062_down_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 业务门禁通过后再核对栈顶、对象、默认矩阵与完整性，结构漂移不得带病回滚。
INSERT INTO prd062_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0054' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0054')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'config_imports','config_import_rows','config_exports',
        'rbac_permission_templates','rbac_role_permission_templates',
        'permissions','roles','role_permissions','audit_logs'
      ))=9 THEN 0 ELSE 1 END
  -- 任一导入、行级、导出或审计事实都会形成不可删除证据，回滚必须整体拒绝。
  + (SELECT COUNT(*) FROM config_imports)
  + (SELECT COUNT(*) FROM config_import_rows)
  + (SELECT COUNT(*) FROM config_exports)
  + (SELECT COUNT(*) FROM audit_logs
     WHERE action IN ('config.imported','config.exported')
        OR object_type='config_transfer')
  -- 管理员若在迁移后显式授予或撤销新权限，source event 本身也是治理证据，不能降级目录。
  + (SELECT COUNT(*)
     FROM role_permission_events event
     WHERE EXISTS (
       SELECT 1 FROM json_each(event.added_permissions_json) item
       WHERE item.value IN ('config.transfer.read','config.transfer.write')
     ) OR EXISTS (
       SELECT 1 FROM json_each(event.removed_permissions_json) item
       WHERE item.value IN ('config.transfer.read','config.transfer.write')
     ))
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=50
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=2)=50
      AND (SELECT COUNT(*) FROM rbac_permission_templates
        WHERE permission_code IN ('config.transfer.read','config.transfer.write'))=2
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=131
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
        WHERE permission_code IN ('config.transfer.read','config.transfer.write'))=6
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*50 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=2)
        =(SELECT COUNT(*) FROM permissions)
      THEN 0 ELSE 1 END
  -- 新权限关系必须是 Admin/Developer/OpsAdmin 的完整二元组，并与新默认 hash 对应；
  -- 任意单项、其他角色或自定义 hash 都表示迁移后的显式业务变更，不能猜测回滚。
  + (SELECT COUNT(*) FROM roles role
     WHERE (
       SELECT COUNT(*) FROM role_permissions relation
       JOIN permissions permission
         ON permission.tenant_id=relation.tenant_id
        AND permission.permission_id=relation.permission_id
       WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
         AND permission.code IN ('config.transfer.read','config.transfer.write')
     ) NOT IN (0,2))
  + (SELECT COUNT(*) FROM roles role
     WHERE EXISTS (
       SELECT 1 FROM role_permissions relation
       JOIN permissions permission
         ON permission.tenant_id=relation.tenant_id
        AND permission.permission_id=relation.permission_id
       WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
         AND permission.code IN ('config.transfer.read','config.transfer.write')
     ) AND (
       role.code NOT IN ('admin','developer','ops_admin')
       OR role.permission_revision NOT IN (1,2)
       OR role.matrix_sha256!=CASE role.code
         WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
         WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
         WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
       END
     ))
  + (SELECT COUNT(*) FROM roles role
     WHERE role.matrix_sha256 IN (
       '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73',
       '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f',
       'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
     ) AND NOT EXISTS (
       SELECT 1 FROM role_permissions relation
       JOIN permissions permission
         ON permission.tenant_id=relation.tenant_id
        AND permission.permission_id=relation.permission_id
       WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
         AND permission.code IN ('config.transfer.read','config.transfer.write')
     ))
  + CASE WHEN (SELECT COUNT(*) FROM pragma_index_list('audit_logs') WHERE origin='c')=11
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND tbl_name='audit_logs')=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_config_%_prd062')=9
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (
         instr(lower(object.sql),'config_imports')>0
         OR instr(lower(object.sql),'config_import_rows')>0
         OR instr(lower(object.sql),'config_exports')>0
       )
       AND object.name NOT IN (
         'config_imports','config_import_rows','config_exports',
         'idx_config_imports_tenant_created_prd062',
         'idx_config_import_rows_tenant_import_prd062',
         'idx_config_import_rows_tenant_object_prd062',
         'idx_config_exports_tenant_created_prd062',
         'trg_config_imports_owner_insert_prd062',
         'trg_config_imports_immutable_update_prd062',
         'trg_config_imports_immutable_delete_prd062',
         'trg_config_import_rows_owner_insert_prd062',
         'trg_config_import_rows_immutable_update_prd062',
         'trg_config_import_rows_immutable_delete_prd062',
         'trg_config_exports_owner_insert_prd062',
         'trg_config_exports_immutable_update_prd062',
         'trg_config_exports_immutable_delete_prd062',
         'trg_audit_logs_owner_insert'
       ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd062_down_guard;

-- 记录可安全收缩的迁移默认角色。revision 2 来自存量升级，revision 1 来自 0054 后新建 tenant；
-- 两者都恢复为 v1 hash，而角色自定义 revision/hash 不在集合中，保持原样。
CREATE TEMP TABLE prd062_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
    WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
    WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
  END;

DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;

-- 先移除仅属于 revision 2 的关系，再收缩产品可授予上限；外键关闭只服务换表，
-- 删除集合由稳定 permission code 和前置角色门禁限定，不使用动态表名或用户输入。
DELETE FROM role_permissions
WHERE permission_id IN (
  SELECT permission_id FROM permissions
  WHERE code IN ('config.transfer.read','config.transfer.write')
);
-- 产品默认授权关系同步收缩，避免 revision 1 模板仍引用已删除的新权限。
DELETE FROM rbac_role_permission_templates
WHERE permission_code IN ('config.transfer.read','config.transfer.write');

-- 产品模板恢复 48 项 revision 1，并保留全部旧模板 rowid。
CREATE TABLE rbac_permission_templates_prd062_old (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=1)
);
-- 旧 48 项模板保留 rowid 和展示字段，仅把目录版本精确恢复为 revision 1。
INSERT INTO rbac_permission_templates_prd062_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,1
FROM rbac_permission_templates
WHERE permission_code NOT IN ('config.transfer.read','config.transfer.write');
DROP TABLE rbac_permission_templates;
-- 模板换表完成后恢复正式表名，使 0050 的外键和触发器合同重新生效。
ALTER TABLE rbac_permission_templates_prd062_old RENAME TO rbac_permission_templates;
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

-- 租户投影同样只删除新两项，旧 Permission 的 rowid、ID、时间和展示元数据全部保真。
CREATE TABLE permissions_prd062_old (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=1),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 每租户仅复制旧目录权限并保留 rowid、稳定 ID 与时间，杜绝跨租户重建关系。
INSERT INTO permissions_prd062_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  1,status,created_at,updated_at
FROM permissions
WHERE code NOT IN ('config.transfer.read','config.transfer.write');
DROP TABLE permissions;
-- 权限投影换表后恢复 0050 正式表名，复合 tenant owner 外键保持不变。
ALTER TABLE permissions_prd062_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059
  ON permissions(tenant_id,category,code);
CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code
    AND template.category=NEW.category AND template.name=NEW.name
    AND template.description=NEW.description AND template.risk_level=NEW.risk_level
    AND template.catalog_revision=NEW.catalog_revision
)
  OR NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS (
    SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
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

-- 只收缩迁移记录的规范角色；显式定制角色已被前置门禁拒绝，不能猜测其旧矩阵。
UPDATE roles
SET permission_revision=CASE WHEN permission_revision=2 THEN 1 ELSE permission_revision END,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2'
      WHEN 'developer' THEN '9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e'
      WHEN 'ops_admin' THEN 'b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9'
    END
WHERE role_id IN (SELECT role_id FROM prd062_roles_to_contract);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2'
    WHEN 'developer' THEN '9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e'
    WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
    WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
    WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
    WHEN 'ops_admin' THEN 'b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS (
    SELECT 1 FROM roles current
    WHERE current.role_id=NEW.role_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin'
  OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id AND role.code='admin'
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 降级后的新租户按 revision 1 产品模板创建本租户角色，不继承配置迁移权限。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT
    'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code
      WHEN 'admin' THEN 'Admin'
      WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer'
      WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin'
      WHEN 'ops_admin' THEN 'OpsAdmin'
    END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格和成本异常。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型和路由运维。'
    END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2'
      WHEN 'developer' THEN '9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e'
      WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
      WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
      WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
      WHEN 'ops_admin' THEN 'b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 48 项 Permission 全部以 NEW.tenant_id 投影，稳定 owner 约束阻止跨租户复用。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT
    'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 默认角色关系只在同一租户内联接，复合外键继续执行后端租户隔离。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  )
  SELECT
    'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission
    ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

-- 审计枚举恢复到 0053：前置门禁已证明没有 config action/object，故可逐行无损复制。
CREATE TABLE audit_logs_prd062_old (
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
      'user.invited','invitation.accepted'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider','provider_key','virtual_key','budget_rule','dlp_rule',
      'webhook_delivery','webhook_endpoint','webhook_dead_letter',
      'usage_export_job','policy','policy_snapshot','policy_explanation',
      'routing_policy','model_quality_score','cache_affinity',
      'finops_adjustment','cost_anomaly','provider_error_map','audit_export',
      'role_permission','invitation'
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
-- 回滚审计换表逐列复制并保留 rowid，所有既有审计事实必须与 0053 结构逐值一致。
INSERT INTO audit_logs_prd062_old(
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
)
SELECT
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
FROM audit_logs;

CREATE TEMP TABLE prd062_audit_down_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 删除新表前核对行数和 17 个持久列，任何 NULL 或治理字段漂移都原子终止回滚。
INSERT INTO prd062_audit_down_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd062_old))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd062_old
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd062_old
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd062_audit_down_copy_guard;

DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后恢复正式审计表名，历史索引和触发器随后按冻结定义重建。
ALTER TABLE audit_logs_prd062_old RENAME TO audit_logs;
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id,created_at DESC,audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id,actor_user_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id,object_type,object_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id,action,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id,request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id,object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';
CREATE UNIQUE INDEX idx_audit_logs_invitation_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action IN ('user.invited','invitation.accepted');

-- 恢复 0052 owner 合同；所有旧领域 source fact、Invitation 隐私和审计导出约束原样保留。
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
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
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
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 降级后新增审计仍必须进入既有导出序列，防止回滚破坏增量治理链。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;

-- 审计已不再引用三张 PRD-062 表，且门禁证明其为空，按子表到父表顺序删除不会销毁证据。
DROP TABLE config_import_rows;
DROP TABLE config_imports;
DROP TABLE config_exports;

CREATE TEMP TABLE prd062_down_final_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 提交前核对 0050 对象、48 项租户投影、外键和完整性，部分回滚不得落盘。
INSERT INTO prd062_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=48
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=1)=48
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=125
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*48 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=1)
        =(SELECT COUNT(*) FROM permissions)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd062_roles_to_contract source
     JOIN roles role ON role.role_id=source.role_id AND role.tenant_id=source.tenant_id
     WHERE role.permission_revision!=1
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN '0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2'
          WHEN 'developer' THEN '9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e'
          WHEN 'ops_admin' THEN 'b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9'
        END)
  + CASE WHEN (SELECT COUNT(*) FROM pragma_index_list('audit_logs') WHERE origin='c')=10
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND tbl_name='audit_logs')=6 THEN 0 ELSE 1 END
  + CASE WHEN instr(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs')),'config.imported')=0
      AND instr(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs')),'invitation.accepted')>0
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema
     WHERE name LIKE 'config_import%' OR name LIKE 'config_export%')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd062_down_final_guard;
DROP TABLE prd062_roles_to_contract;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
