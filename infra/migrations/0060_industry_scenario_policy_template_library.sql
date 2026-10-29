-- PRD-068：行业/场景策略模板库、RBAC revision 5 与 Gateway mask 安全证据。
-- 安全边界：模板只保存服务端冻结的蓝图和哈希；application、依赖、Audit/Event 只保存
-- tenant-bound ID 与有界摘要，禁止保存 Provider Key、Token、Prompt、Response 或脱敏命中文本。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前证明数据库精确位于 0059 栈顶。任何未知后续迁移、同名对象或损坏的
-- RBAC/RequestLog 基线都必须在原事务内失败，不能在不可信结构上猜测升级。
DROP TABLE IF EXISTS temp.prd068_up_guard;
CREATE TEMP TABLE prd068_up_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd068_up_preflight_ok CHECK (issue_count=0)
);
-- 聚合栈顶、对象、RBAC、RequestLog、Audit 与完整性问题，任一非零即原子终止升级。
INSERT INTO prd068_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0059' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
     WHERE status='applied' AND version>'0059')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=54
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=4)=54
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=141
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*54 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=4)
      =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  -- RequestLog 的 0059 栈顶必须恰为 50 列；0060 只在此确定基线上追加两个安全字段。
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('request_logs') ORDER BY cid
      )
    )=('request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,' ||
       'error_code,latency_ms,created_at,endpoint,request_model,resolved_model,provider_code,' ||
       'input_tokens,output_tokens,total_tokens,estimated_cost_amount,currency,price_schema_version,' ||
       'updated_at,budget_status,budget_rule_id,budget_scope_type,budget_scope_id,budget_action,' ||
       'budget_reason_code,budget_current_cost_cny,budget_limit_cny,dlp_status,dlp_rule_id,dlp_action,' ||
       'dlp_match_count,dlp_reason_code,project_id,application_id,environment_id,environment_code,' ||
       'fallback_triggered,routing_attempt_count,final_routing_attempt_id,stream_session_id,' ||
       'stream_status,snapshot_version,policy_snapshot_status,policy_decision,policy_reason_code,' ||
       'policy_id,policy_action_type,policy_target_model_id')
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND tbl_name='request_logs'
        AND name NOT LIKE 'sqlite_autoindex%')=24 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND tbl_name='request_logs')=12 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('audit_logs'))=17
      AND instr((SELECT sql FROM sqlite_schema
                 WHERE type='table' AND name='audit_logs'),
                'prompt_template.version_published')>0
    THEN 0 ELSE 1 END
  -- 所有 0060 正式对象及换表暂存名必须空闲，避免覆盖本地或未来 schema。
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'policy_templates','policy_template_applications',
      'policy_template_application_items','policy_template_application_dependencies',
      'policy_template_events','rbac_permission_templates_prd068_new',
      'permissions_prd068_new','audit_logs_prd068_new',
      'trg_tenants_policy_templates_initialize_prd068',
      'uq_policy_events_owner_prd068','uq_policy_snapshot_events_owner_prd068',
      'uq_audit_logs_owner_prd068','idx_audit_logs_policy_template_request_unique'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd068_up_guard;

-- 只冻结未被租户定制过的四类角色。Admin 的历史 revision 来自既有产品迁移；其余三类
-- 只有 revision=1 才能证明从未经过自定义再回退，避免 migration 静默恢复已撤销权限。
CREATE TEMP TABLE prd068_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','finance_admin','security_admin','ops_admin')
  AND (
    (role.code='admin' AND role.permission_revision BETWEEN 1 AND 4)
    OR (role.code IN ('finance_admin','security_admin') AND role.permission_revision=1)
    -- Ops 在 0059 前已由产品迁移合法推进到 revision 2；仍需 hash+关系全集共同证明 canonical。
    OR (role.code='ops_admin' AND role.permission_revision BETWEEN 1 AND 2)
  )
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
    WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
    WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
    WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
  END
  AND NOT EXISTS (
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS (
      SELECT 1 FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id
        AND relation.role_id=role.role_id
        AND permission.code=template.permission_code
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM role_permissions relation
    JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id
      AND relation.role_id=role.role_id
      AND NOT EXISTS (
        SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code
          AND template.permission_code=permission.code
      )
  );

-- 产品 Permission 目录提升到 revision 5；旧 54 行的 rowid 和展示元数据逐值保留。
CREATE TABLE rbac_permission_templates_prd068_new (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=5)
);
-- 旧目录逐行复制且只提升统一 revision，稳定 rowid 和中文产品元数据不得变化。
INSERT INTO rbac_permission_templates_prd068_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,5
FROM rbac_permission_templates;
-- 两项新权限独立建模，读取能力不能隐式获得关键应用命令。
INSERT INTO rbac_permission_templates_prd068_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('policy_template.read','security','读取策略模板',
   '读取当前租户内置策略模板、适配状态和安全影响预览。','read',5),
  ('policy_template.apply','security','应用策略模板',
   '应用服务端冻结的策略模板并原子发布策略快照。','critical',5);

-- 产品模板是不可变发布元数据；迁移只在同一事务内短暂解除旧 revision 的写保护。
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TABLE rbac_permission_templates;
-- 全量复制完成后才替换正式目录名，避免角色模板观察到半完成目录。
ALTER TABLE rbac_permission_templates_prd068_new RENAME TO rbac_permission_templates;

-- 两项权限的产品上限和默认值只授予四类职责管理员；Developer/Viewer 不获得页面或 API 能力。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','policy_template.read',1),
  ('admin','policy_template.apply',1),
  ('finance_admin','policy_template.read',1),
  ('finance_admin','policy_template.apply',1),
  ('security_admin','policy_template.read',1),
  ('security_admin','policy_template.apply',1),
  ('ops_admin','policy_template.read',1),
  ('ops_admin','policy_template.apply',1);

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

-- 每个租户获得完整 revision 5 Permission 投影；旧 ID、rowid、时间和状态保持不变。
CREATE TABLE permissions_prd068_new (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=5),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 现有租户 Permission 保留稳定 ID、rowid 与时间，只同步目录 revision。
INSERT INTO permissions_prd068_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       5,status,created_at,updated_at
FROM permissions;
-- 每个现有 tenant 独立生成两项投影，禁止跨租户复用 permission_id。
INSERT INTO permissions_prd068_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT
  'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,
  5,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template
  ON template.permission_code IN ('policy_template.read','policy_template.apply');

DROP TABLE permissions;
-- 租户投影全部写齐后恢复正式表名，再重建既有 owner 与不可变约束。
ALTER TABLE permissions_prd068_new RENAME TO permissions;
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

-- 只为迁移前冻结的 canonical 角色补关系；运行时仍逐请求读取数据库矩阵。
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
-- 新关系仅来自冻结角色集合和同 tenant Permission，不向定制矩阵补权。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT
  'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd068_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE permission.code IN ('policy_template.read','policy_template.apply');
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1
  FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template
    ON template.role_code=role.code AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.status='active'
    AND permission.tenant_id=NEW.tenant_id AND permission.permission_id=NEW.permission_id
    AND permission.status='active'
)
  OR EXISTS (
    SELECT 1 FROM role_permissions current
    WHERE current.role_permission_id=NEW.role_permission_id
       OR (current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
           AND current.permission_id=NEW.permission_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;

-- 关系和授权快照在同一事务内推进；没有获得新关系的自定义角色保持原 revision/hash。
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
-- 只有实际增加两项关系的角色推进一次 revision 并切换到对应 revision 5 hash。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
    END
WHERE role_id IN (SELECT role_id FROM prd068_roles_to_expand);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
    WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
    WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
    WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
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

-- 新 tenant 从第一笔事实起即使用 revision 5 的六角色和完整权限投影。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 先创建六个 canonical 角色，后续关系才能通过同 tenant 复合 owner 校验。
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
      WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
      WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
      WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 再按 revision 5 产品目录为新 tenant 生成独立 Permission 实体。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT
    'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后依据产品角色模板建立默认关系，初始化过程不拼接其他 tenant 的实体。
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

-- RequestLog 只追加两个非敏感 mask 事实。policy_mask_policy_id 在 422/503 时也保留，
-- applied=1 只表示完整 copy-on-write 已成功，因此必须同时具有命中的稳定 Policy ID。
ALTER TABLE request_logs ADD COLUMN policy_mask_applied INTEGER NOT NULL DEFAULT 0
  CHECK (policy_mask_applied IN (0,1));
-- 命中 ID 与 applied 状态分列保存，使 422/503 仍可追踪且不记录任何正文。
ALTER TABLE request_logs ADD COLUMN policy_mask_policy_id TEXT
  CHECK (
    (policy_mask_policy_id IS NULL OR (
      length(policy_mask_policy_id) BETWEEN 5 AND 128
      AND policy_mask_policy_id GLOB 'pol_*'
    ))
    AND (policy_mask_applied=0 OR policy_mask_policy_id IS NOT NULL)
  );

-- application 的 deferred 环边需要显式 tenant owner 候选键，不能仅靠全局事件主键。
CREATE UNIQUE INDEX uq_policy_events_owner_prd068
  ON policy_events(tenant_id,policy_event_id);
CREATE UNIQUE INDEX uq_policy_snapshot_events_owner_prd068
  ON policy_snapshot_events(tenant_id,policy_snapshot_event_id);
CREATE UNIQUE INDEX uq_audit_logs_owner_prd068
  ON audit_logs(tenant_id,audit_log_id);

-- 五个内置模板按 tenant 物化。蓝图仅用于只读展示；运行时必须同时核对代码内目录和
-- definition_hash，绝不能对数据库文本执行 format/eval。
CREATE TABLE policy_templates (
  tenant_id TEXT NOT NULL,
  policy_template_id TEXT NOT NULL CHECK (
    length(policy_template_id) BETWEEN 8 AND 128
    AND policy_template_id GLOB 'ptpl_*'
  ),
  template_code TEXT NOT NULL CHECK (template_code IN (
    'low_cost_mode','high_security_mode','domestic_models_only',
    'budget_burst_guard','customer_service_redaction'
  )),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 500),
  category TEXT NOT NULL CHECK (category IN ('cost','security','budget','privacy')),
  scenario TEXT NOT NULL CHECK (length(scenario) BETWEEN 2 AND 80),
  status TEXT NOT NULL CHECK (status='active'),
  version INTEGER NOT NULL CHECK (version=1),
  priority INTEGER NOT NULL CHECK (
    (template_code='low_cost_mode' AND priority=1000)
    OR (template_code!='low_cost_mode' AND priority=10)
  ),
  dsl_version TEXT NOT NULL CHECK (dsl_version='policy-dsl-v1'),
  condition_expr TEXT NOT NULL CHECK (
    length(condition_expr) BETWEEN 1 AND 1024
    AND length(CAST(condition_expr AS BLOB))<=2048
    AND instr(condition_expr,char(0))=0
  ),
  action_json TEXT NOT NULL CHECK (
    json_valid(action_json) AND json_type(action_json)='object'
    AND length(CAST(action_json AS BLOB))<=2048
  ),
  binding_schema_json TEXT NOT NULL CHECK (
    json_valid(binding_schema_json) AND json_type(binding_schema_json)='array'
    AND json_array_length(binding_schema_json) BETWEEN 1 AND 2
    AND length(CAST(binding_schema_json AS BLOB))<=4096
  ),
  definition_hash TEXT NOT NULL CHECK (
    length(definition_hash)=64 AND definition_hash NOT GLOB '*[^0-9a-f]*'
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  PRIMARY KEY (tenant_id,policy_template_id),
  UNIQUE (tenant_id,template_code),
  UNIQUE (tenant_id,policy_template_id,version,definition_hash),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);
CREATE INDEX idx_policy_templates_tenant_category_prd068
  ON policy_templates(tenant_id,category,template_code);

-- 迁移 seed 与未来 tenant trigger 共用完全相同的五条冻结定义，保证路径 ID、顺序和 hash 稳定。
-- 现有 tenant 逐个生成本地模板行，稳定路径 ID 可以重复但 tenant owner 绝不共享。
INSERT INTO policy_templates(
  tenant_id,policy_template_id,template_code,name,description,category,scenario,
  status,version,priority,dsl_version,condition_expr,action_json,
  binding_schema_json,definition_hash,created_at,updated_at
)
SELECT tenant.tenant_id,definition.*
FROM tenants tenant
CROSS JOIN (
  SELECT
    'ptpl_low_cost_mode','low_cost_mode','低成本模式',
    '在不绕过既有安全策略的前提下，将目标项目固定路由到当前合格的最低成本模型。',
    'cost','cost_optimization','active',1,1000,'policy-dsl-v1',
    'project == "$project_id"',
    '{"type":"route_to","target_model_id":"$derived_model_id"}',
    '[{"name":"project_id","type":"resource_id","required":true}]',
    'cdeeba7fae097a0050b94d259b79b5dc202412990ef387e6596e5d3dfb92e20f',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  UNION ALL SELECT
    'ptpl_high_security_mode','high_security_mode','高安全模式',
    '将目标项目限制在选定环境和模型别名内，阻断未批准的模型访问。',
    'security','approved_model_only','active',1,10,'policy-dsl-v1',
    'project == "$project_id" and (env != "$environment_code" or model != "$model_alias")',
    '{"type":"deny"}',
    '[{"name":"project_id","type":"resource_id","required":true},{"name":"model_alias_id","type":"resource_id","required":true}]',
    '1536ca9581138a80b900e8db8d6cbd69f810e0d7a89665503166f46ebe564060',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  UNION ALL SELECT
    'ptpl_domestic_models_only','domestic_models_only','只用国产模型',
    '只允许目标项目在冻结环境集合中使用服务端证明全环境覆盖的国产模型别名。',
    'security','domestic_models_only','active',1,10,'policy-dsl-v1',
    'project == "$project_id" and (env not in $environment_codes or model not in $model_aliases)',
    '{"type":"deny"}',
    '[{"name":"project_id","type":"resource_id","required":true}]',
    '7617ea2cd178e2de41e06bb0fd09e9b670ed1c0c27a58354d94666a6bfbb52f1',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  UNION ALL SELECT
    'ptpl_budget_burst_guard','budget_burst_guard','预算防爆',
    '当目标项目的权威最小预算余额低于阈值时，在调用上游前拒绝请求。',
    'budget','budget_burst_guard','active',1,10,'policy-dsl-v1',
    'project == "$project_id" and budget_remaining < $threshold_cny',
    '{"type":"deny"}',
    '[{"name":"project_id","type":"resource_id","required":true},{"name":"threshold_cny","type":"decimal","required":true}]',
    'c11bed7d12ab2d388587377fd51e9ca4a1b7d4190dbf597432888c48462f365f',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  UNION ALL SELECT
    'ptpl_customer_service_redaction','customer_service_redaction','客服脱敏',
    '在路由、DLP、预算和 Provider 前对目标项目支持的用户输入执行固定占位脱敏。',
    'privacy','customer_service_redaction','active',1,10,'policy-dsl-v1',
    'project == "$project_id"','{"type":"mask"}',
    '[{"name":"project_id","type":"resource_id","required":true}]',
    '77e32f51cfbf27c98b7c5506b70b14124eaf292458a71f07541d2619516ca4ac',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
) AS definition;

CREATE TRIGGER trg_policy_templates_update_prd068
BEFORE UPDATE ON policy_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_immutable'); END;
CREATE TRIGGER trg_policy_templates_delete_prd068
BEFORE DELETE ON policy_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_immutable'); END;
CREATE TRIGGER trg_policy_templates_insert_prd068
BEFORE INSERT ON policy_templates FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR EXISTS (
    SELECT 1 FROM policy_templates current
    WHERE (current.tenant_id=NEW.tenant_id
           AND (current.policy_template_id=NEW.policy_template_id
                OR current.template_code=NEW.template_code))
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'policy_template_immutable'); END;

CREATE TRIGGER trg_tenants_policy_templates_initialize_prd068
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新 tenant 在同一事务内物化五条冻结模板，不能从其他租户复制业务实体。
  INSERT INTO policy_templates(
    tenant_id,policy_template_id,template_code,name,description,category,scenario,
    status,version,priority,dsl_version,condition_expr,action_json,
    binding_schema_json,definition_hash,created_at,updated_at
  ) VALUES
    (NEW.tenant_id,'ptpl_low_cost_mode','low_cost_mode','低成本模式',
     '在不绕过既有安全策略的前提下，将目标项目固定路由到当前合格的最低成本模型。',
     'cost','cost_optimization','active',1,1000,'policy-dsl-v1','project == "$project_id"',
     '{"type":"route_to","target_model_id":"$derived_model_id"}',
     '[{"name":"project_id","type":"resource_id","required":true}]',
     'cdeeba7fae097a0050b94d259b79b5dc202412990ef387e6596e5d3dfb92e20f',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    (NEW.tenant_id,'ptpl_high_security_mode','high_security_mode','高安全模式',
     '将目标项目限制在选定环境和模型别名内，阻断未批准的模型访问。',
     'security','approved_model_only','active',1,10,'policy-dsl-v1',
     'project == "$project_id" and (env != "$environment_code" or model != "$model_alias")',
     '{"type":"deny"}',
     '[{"name":"project_id","type":"resource_id","required":true},{"name":"model_alias_id","type":"resource_id","required":true}]',
     '1536ca9581138a80b900e8db8d6cbd69f810e0d7a89665503166f46ebe564060',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    (NEW.tenant_id,'ptpl_domestic_models_only','domestic_models_only','只用国产模型',
     '只允许目标项目在冻结环境集合中使用服务端证明全环境覆盖的国产模型别名。',
     'security','domestic_models_only','active',1,10,'policy-dsl-v1',
     'project == "$project_id" and (env not in $environment_codes or model not in $model_aliases)',
     '{"type":"deny"}',
     '[{"name":"project_id","type":"resource_id","required":true}]',
     '7617ea2cd178e2de41e06bb0fd09e9b670ed1c0c27a58354d94666a6bfbb52f1',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    (NEW.tenant_id,'ptpl_budget_burst_guard','budget_burst_guard','预算防爆',
     '当目标项目的权威最小预算余额低于阈值时，在调用上游前拒绝请求。',
     'budget','budget_burst_guard','active',1,10,'policy-dsl-v1',
     'project == "$project_id" and budget_remaining < $threshold_cny','{"type":"deny"}',
     '[{"name":"project_id","type":"resource_id","required":true},{"name":"threshold_cny","type":"decimal","required":true}]',
     'c11bed7d12ab2d388587377fd51e9ca4a1b7d4190dbf597432888c48462f365f',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    (NEW.tenant_id,'ptpl_customer_service_redaction','customer_service_redaction','客服脱敏',
     '在路由、DLP、预算和 Provider 前对目标项目支持的用户输入执行固定占位脱敏。',
     'privacy','customer_service_redaction','active',1,10,'policy-dsl-v1',
     'project == "$project_id"','{"type":"mask"}',
     '[{"name":"project_id","type":"resource_id","required":true}]',
     '77e32f51cfbf27c98b7c5506b70b14124eaf292458a71f07541d2619516ca4ac',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;

-- 每次应用只生成一个 Policy 和一个单调 Snapshot。幂等 request、preview nonce、前后快照、
-- generated Policy 及十个 Audit/Event fact 都在 tenant 内一对一，避免恢复流程猜测 owner。
CREATE TABLE policy_template_applications (
  application_id TEXT PRIMARY KEY CHECK (
    length(application_id) BETWEEN 9 AND 128 AND application_id GLOB 'ptapp_*'
  ),
  tenant_id TEXT NOT NULL,
  policy_template_id TEXT NOT NULL,
  template_version INTEGER NOT NULL CHECK (template_version=1),
  template_definition_hash TEXT NOT NULL CHECK (
    length(template_definition_hash)=64
    AND template_definition_hash NOT GLOB '*[^0-9a-f]*'
  ),
  project_id TEXT NOT NULL CHECK (length(project_id) BETWEEN 1 AND 128),
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (
    length(request_fingerprint)=64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  preview_nonce TEXT NOT NULL CHECK (
    length(preview_nonce)=64 AND preview_nonce NOT GLOB '*[^0-9a-f]*'
  ),
  preview_as_of TEXT NOT NULL CHECK (length(preview_as_of) BETWEEN 1 AND 64),
  applied_as_of TEXT NOT NULL CHECK (length(applied_as_of) BETWEEN 1 AND 64),
  bindings_json TEXT NOT NULL CHECK (
    json_valid(bindings_json) AND json_type(bindings_json)='object'
    AND length(CAST(bindings_json AS BLOB))<=4096
  ),
  bindings_hash TEXT NOT NULL CHECK (
    length(bindings_hash)=64 AND bindings_hash NOT GLOB '*[^0-9a-f]*'
  ),
  impact_hash TEXT NOT NULL CHECK (
    length(impact_hash)=64 AND impact_hash NOT GLOB '*[^0-9a-f]*'
  ),
  applicability_fingerprint TEXT NOT NULL CHECK (
    length(applicability_fingerprint)=64
    AND applicability_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  before_candidate_hash TEXT NOT NULL CHECK (
    length(before_candidate_hash)=64 AND before_candidate_hash NOT GLOB '*[^0-9a-f]*'
  ),
  after_candidate_hash TEXT NOT NULL CHECK (
    length(after_candidate_hash)=64 AND after_candidate_hash NOT GLOB '*[^0-9a-f]*'
  ),
  previous_snapshot_id TEXT NOT NULL,
  previous_snapshot_version INTEGER NOT NULL CHECK (
    typeof(previous_snapshot_version)='integer' AND previous_snapshot_version>=1
  ),
  result_snapshot_id TEXT NOT NULL,
  result_snapshot_version INTEGER NOT NULL CHECK (
    typeof(result_snapshot_version)='integer'
    AND result_snapshot_version=previous_snapshot_version+1
  ),
  generated_policy_id TEXT NOT NULL CHECK (
    length(generated_policy_id) BETWEEN 5 AND 128 AND generated_policy_id GLOB 'pol_*'
  ),
  generated_policy_version INTEGER NOT NULL CHECK (generated_policy_version=1),
  status TEXT NOT NULL CHECK (status IN ('applied','rolled_back')),
  policy_audit_log_id TEXT NOT NULL,
  policy_event_id TEXT NOT NULL,
  snapshot_audit_log_id TEXT NOT NULL,
  snapshot_event_id TEXT NOT NULL,
  apply_audit_log_id TEXT NOT NULL,
  template_event_id TEXT NOT NULL,
  rollback_snapshot_id TEXT,
  rollback_snapshot_version INTEGER CHECK (
    rollback_snapshot_version IS NULL
    OR (typeof(rollback_snapshot_version)='integer'
        AND rollback_snapshot_version>result_snapshot_version)
  ),
  rolled_back_policy_version INTEGER CHECK (
    rolled_back_policy_version IS NULL OR rolled_back_policy_version=2
  ),
  rollback_actor_user_id TEXT,
  rollback_request_id TEXT CHECK (
    rollback_request_id IS NULL OR length(rollback_request_id) BETWEEN 1 AND 128
  ),
  rollback_trace_id TEXT CHECK (
    rollback_trace_id IS NULL OR length(rollback_trace_id) BETWEEN 1 AND 128
  ),
  rollback_request_fingerprint TEXT CHECK (
    rollback_request_fingerprint IS NULL OR (
      length(rollback_request_fingerprint)=64
      AND rollback_request_fingerprint NOT GLOB '*[^0-9a-f]*'
    )
  ),
  rollback_policy_audit_log_id TEXT,
  rollback_snapshot_audit_log_id TEXT,
  rollback_apply_audit_log_id TEXT,
  rollback_snapshot_event_id TEXT,
  rolled_back_at TEXT CHECK (
    rolled_back_at IS NULL OR length(rolled_back_at) BETWEEN 1 AND 64
  ),
  rolled_back_by TEXT,
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  UNIQUE (tenant_id,application_id),
  UNIQUE (tenant_id,request_id),
  UNIQUE (tenant_id,preview_nonce),
  UNIQUE (tenant_id,previous_snapshot_id),
  UNIQUE (tenant_id,result_snapshot_id),
  UNIQUE (tenant_id,generated_policy_id),
  UNIQUE (tenant_id,rollback_snapshot_id),
  UNIQUE (tenant_id,policy_audit_log_id),
  UNIQUE (tenant_id,policy_event_id),
  UNIQUE (tenant_id,snapshot_audit_log_id),
  UNIQUE (tenant_id,snapshot_event_id),
  UNIQUE (tenant_id,apply_audit_log_id),
  UNIQUE (tenant_id,template_event_id),
  UNIQUE (tenant_id,rollback_policy_audit_log_id),
  UNIQUE (tenant_id,rollback_snapshot_audit_log_id),
  UNIQUE (tenant_id,rollback_apply_audit_log_id),
  UNIQUE (tenant_id,rollback_snapshot_event_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,policy_template_id,template_version,template_definition_hash)
    REFERENCES policy_templates(
      tenant_id,policy_template_id,version,definition_hash
    ),
  FOREIGN KEY (tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,previous_snapshot_id,previous_snapshot_version)
    REFERENCES policy_snapshots(tenant_id,policy_snapshot_id,snapshot_version),
  FOREIGN KEY (tenant_id,result_snapshot_id,result_snapshot_version)
    REFERENCES policy_snapshots(tenant_id,policy_snapshot_id,snapshot_version),
  FOREIGN KEY (tenant_id,generated_policy_id) REFERENCES policies(tenant_id,policy_id),
  FOREIGN KEY (tenant_id,policy_audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id),
  FOREIGN KEY (tenant_id,policy_event_id)
    REFERENCES policy_events(tenant_id,policy_event_id),
  FOREIGN KEY (tenant_id,snapshot_audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id),
  FOREIGN KEY (tenant_id,snapshot_event_id)
    REFERENCES policy_snapshot_events(tenant_id,policy_snapshot_event_id),
  -- Template Audit/Event 与 application 互相绑定，因此只有这两条 apply 环边延迟到提交校验。
  FOREIGN KEY (tenant_id,apply_audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id,template_event_id)
    REFERENCES policy_template_events(tenant_id,policy_template_event_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id,rollback_snapshot_id,rollback_snapshot_version)
    REFERENCES policy_snapshots(tenant_id,policy_snapshot_id,snapshot_version)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id,rollback_actor_user_id)
    REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,rolled_back_by)
    REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,rollback_policy_audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id,rollback_snapshot_audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id,rollback_apply_audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id,rollback_snapshot_event_id)
    REFERENCES policy_snapshot_events(tenant_id,policy_snapshot_event_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (applied_as_of>=preview_as_of),
  CHECK (
    (status='applied'
      AND rollback_snapshot_id IS NULL AND rollback_snapshot_version IS NULL
      AND rolled_back_policy_version IS NULL AND rollback_actor_user_id IS NULL
      AND rollback_request_id IS NULL AND rollback_trace_id IS NULL
      AND rollback_request_fingerprint IS NULL
      AND rollback_policy_audit_log_id IS NULL
      AND rollback_snapshot_audit_log_id IS NULL
      AND rollback_apply_audit_log_id IS NULL
      AND rollback_snapshot_event_id IS NULL
      AND rolled_back_at IS NULL AND rolled_back_by IS NULL)
    OR
    (status='rolled_back'
      AND rollback_snapshot_id IS NOT NULL AND rollback_snapshot_version IS NOT NULL
      AND rolled_back_policy_version=2 AND rollback_actor_user_id IS NOT NULL
      AND rollback_request_id IS NOT NULL AND rollback_trace_id IS NOT NULL
      AND rollback_request_fingerprint IS NOT NULL
      AND rollback_policy_audit_log_id IS NOT NULL
      AND rollback_snapshot_audit_log_id IS NOT NULL
      AND rollback_apply_audit_log_id IS NOT NULL
      AND rollback_snapshot_event_id IS NOT NULL
      AND rolled_back_at IS NOT NULL
      AND rolled_back_by=rollback_actor_user_id)
  )
);
CREATE INDEX idx_policy_template_applications_tenant_status_prd068
  ON policy_template_applications(tenant_id,status,created_at DESC);
CREATE INDEX idx_policy_template_applications_tenant_template_prd068
  ON policy_template_applications(tenant_id,policy_template_id,created_at DESC);
CREATE UNIQUE INDEX uq_policy_template_applications_active_project_prd068
  ON policy_template_applications(tenant_id,project_id)
  WHERE status='applied';

-- 插入时绑定当前 tenant 的 Project、actor、模板、Policy 与前后 published Snapshot。
-- generated_policy_version 是应用时事实而非当前 Policy version 外键，受控撤销才能推进到 version 2。
CREATE TRIGGER trg_policy_template_applications_owner_insert_prd068
BEFORE INSERT ON policy_template_applications FOR EACH ROW
WHEN NEW.status!='applied'
  OR NOT EXISTS (
    SELECT 1 FROM projects project
    WHERE project.tenant_id=NEW.tenant_id AND project.project_id=NEW.project_id
      AND project.status='active'
  )
  OR NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
      AND actor.status='active'
  )
  OR NOT EXISTS (
    SELECT 1 FROM policies policy
    WHERE policy.tenant_id=NEW.tenant_id AND policy.policy_id=NEW.generated_policy_id
      AND policy.version=1 AND policy.status='active'
      AND policy.created_by=NEW.actor_user_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM policy_snapshots previous
    WHERE previous.tenant_id=NEW.tenant_id
      AND previous.policy_snapshot_id=NEW.previous_snapshot_id
      AND previous.snapshot_version=NEW.previous_snapshot_version
      AND previous.state='published'
  )
  OR NOT EXISTS (
    SELECT 1 FROM policy_snapshots result
    WHERE result.tenant_id=NEW.tenant_id
      AND result.policy_snapshot_id=NEW.result_snapshot_id
      AND result.snapshot_version=NEW.result_snapshot_version
      AND result.state='published' AND result.operation='publish'
      AND result.previous_snapshot_id=NEW.previous_snapshot_id
      AND result.previous_snapshot_version=NEW.previous_snapshot_version
      AND result.request_id=NEW.request_id AND result.trace_id=NEW.trace_id
      AND result.created_by=NEW.actor_user_id
  )
  OR EXISTS (
    SELECT 1 FROM policy_template_applications current
    WHERE current.application_id=NEW.application_id
       OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
       OR (current.tenant_id=NEW.tenant_id AND current.preview_nonce=NEW.preview_nonce)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'policy_template_application_owner_mismatch'); END;

-- 唯一允许的更新是同事务补偿撤销：先冻结十个未来 fact ID 并把状态推进到 rolled_back，
-- 随后才能精确停用 generated Policy；任何其他字段漂移或二次状态跳转均失败关闭。
CREATE TRIGGER trg_policy_template_applications_state_update_prd068
BEFORE UPDATE ON policy_template_applications FOR EACH ROW
WHEN NOT (
  OLD.status='applied' AND NEW.status='rolled_back'
  AND NEW.rowid=OLD.rowid
  AND NEW.application_id=OLD.application_id AND NEW.tenant_id=OLD.tenant_id
  AND NEW.policy_template_id=OLD.policy_template_id
  AND NEW.template_version=OLD.template_version
  AND NEW.template_definition_hash=OLD.template_definition_hash
  AND NEW.project_id=OLD.project_id AND NEW.actor_user_id=OLD.actor_user_id
  AND NEW.request_id=OLD.request_id AND NEW.trace_id=OLD.trace_id
  AND NEW.request_fingerprint=OLD.request_fingerprint
  AND NEW.preview_nonce=OLD.preview_nonce AND NEW.preview_as_of=OLD.preview_as_of
  AND NEW.applied_as_of=OLD.applied_as_of AND NEW.bindings_json=OLD.bindings_json
  AND NEW.bindings_hash=OLD.bindings_hash AND NEW.impact_hash=OLD.impact_hash
  AND NEW.applicability_fingerprint=OLD.applicability_fingerprint
  AND NEW.before_candidate_hash=OLD.before_candidate_hash
  AND NEW.after_candidate_hash=OLD.after_candidate_hash
  AND NEW.previous_snapshot_id=OLD.previous_snapshot_id
  AND NEW.previous_snapshot_version=OLD.previous_snapshot_version
  AND NEW.result_snapshot_id=OLD.result_snapshot_id
  AND NEW.result_snapshot_version=OLD.result_snapshot_version
  AND NEW.generated_policy_id=OLD.generated_policy_id
  AND NEW.generated_policy_version=OLD.generated_policy_version
  AND NEW.policy_audit_log_id=OLD.policy_audit_log_id
  AND NEW.policy_event_id=OLD.policy_event_id
  AND NEW.snapshot_audit_log_id=OLD.snapshot_audit_log_id
  AND NEW.snapshot_event_id=OLD.snapshot_event_id
  AND NEW.apply_audit_log_id=OLD.apply_audit_log_id
  AND NEW.template_event_id=OLD.template_event_id
  AND NEW.created_at=OLD.created_at
  AND EXISTS (
    SELECT 1 FROM policies policy
    WHERE policy.tenant_id=OLD.tenant_id AND policy.policy_id=OLD.generated_policy_id
      AND policy.status='active' AND policy.version=1
  )
)
BEGIN SELECT RAISE(ABORT,'policy_template_application_immutable'); END;
CREATE TRIGGER trg_policy_template_applications_delete_prd068
BEFORE DELETE ON policy_template_applications FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_application_immutable'); END;

-- 每次应用固定写 policy/create 与 policy_snapshot/publish 两条无正文摘要。
CREATE TABLE policy_template_application_items (
  application_item_id TEXT PRIMARY KEY CHECK (
    length(application_item_id) BETWEEN 10 AND 128
    AND application_item_id GLOB 'ptitem_*'
  ),
  tenant_id TEXT NOT NULL,
  application_id TEXT NOT NULL,
  item_order INTEGER NOT NULL CHECK (item_order IN (0,1)),
  resource_type TEXT NOT NULL CHECK (resource_type IN ('policy','policy_snapshot')),
  operation TEXT NOT NULL CHECK (operation IN ('create','publish')),
  resource_id TEXT NOT NULL CHECK (length(resource_id) BETWEEN 5 AND 128),
  before_version INTEGER CHECK (
    before_version IS NULL OR (typeof(before_version)='integer' AND before_version>=1)
  ),
  before_hash TEXT CHECK (
    before_hash IS NULL OR (
      length(before_hash)=64 AND before_hash NOT GLOB '*[^0-9a-f]*'
    )
  ),
  after_version INTEGER NOT NULL CHECK (
    typeof(after_version)='integer' AND after_version>=1
  ),
  after_hash TEXT NOT NULL CHECK (
    length(after_hash)=64 AND after_hash NOT GLOB '*[^0-9a-f]*'
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  UNIQUE (tenant_id,application_item_id),
  UNIQUE (tenant_id,application_id,item_order),
  UNIQUE (tenant_id,application_id,resource_type,resource_id),
  FOREIGN KEY (tenant_id,application_id)
    REFERENCES policy_template_applications(tenant_id,application_id),
  CHECK (
    (item_order=0 AND resource_type='policy' AND operation='create'
      AND before_version IS NULL AND before_hash IS NULL AND after_version=1)
    OR
    (item_order=1 AND resource_type='policy_snapshot' AND operation='publish'
      AND before_version IS NOT NULL AND before_hash IS NOT NULL)
  )
);
CREATE TRIGGER trg_policy_template_application_items_owner_insert_prd068
BEFORE INSERT ON policy_template_application_items FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=NEW.tenant_id
    AND application.application_id=NEW.application_id
    AND (
      (NEW.resource_type='policy'
       AND NEW.resource_id=application.generated_policy_id
       AND NEW.after_version=application.generated_policy_version)
      OR
      (NEW.resource_type='policy_snapshot'
       AND NEW.resource_id=application.result_snapshot_id
       AND NEW.before_version=application.previous_snapshot_version
       AND NEW.after_version=application.result_snapshot_version)
    )
)
  OR EXISTS (
    SELECT 1 FROM policy_template_application_items current
    WHERE current.application_item_id=NEW.application_item_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.application_id=NEW.application_id
           AND current.item_order=NEW.item_order)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'policy_template_application_item_owner_mismatch'); END;
CREATE TRIGGER trg_policy_template_application_items_update_prd068
BEFORE UPDATE ON policy_template_application_items FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_application_item_immutable'); END;
CREATE TRIGGER trg_policy_template_application_items_delete_prd068
BEFORE DELETE ON policy_template_application_items FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_application_item_immutable'); END;

-- 适配依赖只冻结安全摘要和 tenant-bound 资源 ID，不保存 URL、Key、价格正文或预算余额。
CREATE TABLE policy_template_application_dependencies (
  application_dependency_id TEXT PRIMARY KEY CHECK (
    length(application_dependency_id) BETWEEN 9 AND 128
    AND application_dependency_id GLOB 'ptdep_*'
  ),
  tenant_id TEXT NOT NULL,
  application_id TEXT NOT NULL,
  resource_type TEXT NOT NULL CHECK (resource_type IN (
    'project','environment','model_alias','model','provider','price_plan',
    'budget_rule','virtual_key','runtime_capability'
  )),
  resource_id TEXT NOT NULL CHECK (length(resource_id) BETWEEN 1 AND 128),
  relation TEXT NOT NULL CHECK (
    length(relation) BETWEEN 1 AND 80
    AND relation=lower(relation)
    AND relation NOT GLOB '*[^a-z0-9._-]*'
  ),
  resource_name TEXT CHECK (
    resource_name IS NULL OR length(resource_name) BETWEEN 1 AND 128
  ),
  version_or_hash TEXT NOT NULL CHECK (length(version_or_hash) BETWEEN 1 AND 256),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  UNIQUE (tenant_id,application_dependency_id),
  UNIQUE (tenant_id,application_id,resource_type,resource_id,relation),
  FOREIGN KEY (tenant_id,application_id)
    REFERENCES policy_template_applications(tenant_id,application_id),
  CHECK (
    (resource_type='runtime_capability'
      AND resource_id='policy-mask-v1' AND resource_name IS NULL)
    OR resource_type!='runtime_capability'
  )
);
CREATE INDEX idx_policy_template_dependencies_resource_prd068
  ON policy_template_application_dependencies(
    tenant_id,resource_type,resource_id,application_id
  );
CREATE TRIGGER trg_policy_template_dependencies_owner_insert_prd068
BEFORE INSERT ON policy_template_application_dependencies FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM policy_template_applications application
    WHERE application.tenant_id=NEW.tenant_id
      AND application.application_id=NEW.application_id
  )
  OR NOT (
    (NEW.resource_type='project' AND EXISTS (
      SELECT 1 FROM projects resource
      WHERE resource.tenant_id=NEW.tenant_id AND resource.project_id=NEW.resource_id
    ))
    OR (NEW.resource_type='environment' AND EXISTS (
      SELECT 1 FROM environments resource
      WHERE resource.tenant_id=NEW.tenant_id AND resource.environment_id=NEW.resource_id
    ))
    OR (NEW.resource_type='model_alias' AND EXISTS (
      SELECT 1 FROM model_aliases resource
      WHERE resource.tenant_id=NEW.tenant_id AND resource.model_alias_id=NEW.resource_id
        AND NEW.resource_name=resource.alias
    ))
    OR (NEW.resource_type='model' AND EXISTS (
      SELECT 1 FROM models resource
      WHERE resource.tenant_id=NEW.tenant_id AND resource.model_id=NEW.resource_id
    ))
    OR (NEW.resource_type='provider' AND EXISTS (
      SELECT 1 FROM providers resource
      WHERE resource.tenant_id=NEW.tenant_id AND resource.provider_id=NEW.resource_id
    ))
    OR (NEW.resource_type='price_plan' AND EXISTS (
      SELECT 1 FROM model_price_plans resource
      WHERE resource.tenant_id=NEW.tenant_id AND resource.price_plan_id=NEW.resource_id
    ))
    OR (NEW.resource_type='budget_rule' AND EXISTS (
      SELECT 1 FROM budget_rules resource
      WHERE resource.tenant_id=NEW.tenant_id AND resource.budget_rule_id=NEW.resource_id
    ))
    OR (NEW.resource_type='virtual_key' AND EXISTS (
      SELECT 1 FROM virtual_keys resource
      WHERE resource.tenant_id=NEW.tenant_id AND resource.virtual_key_id=NEW.resource_id
    ))
    OR (NEW.resource_type='runtime_capability'
        AND NEW.resource_id='policy-mask-v1')
  )
  OR EXISTS (
    SELECT 1 FROM policy_template_application_dependencies current
    WHERE current.application_dependency_id=NEW.application_dependency_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.application_id=NEW.application_id
           AND current.resource_type=NEW.resource_type
           AND current.resource_id=NEW.resource_id
           AND current.relation=NEW.relation)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_owner_mismatch'); END;
CREATE TRIGGER trg_policy_template_dependencies_update_prd068
BEFORE UPDATE ON policy_template_application_dependencies FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_dependencies_delete_prd068
BEFORE DELETE ON policy_template_application_dependencies FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;

-- 唯一 source event 只承载九个安全摘要键；投递状态由既有 Webhook 表独立管理。
CREATE TABLE policy_template_events (
  policy_template_event_id TEXT PRIMARY KEY CHECK (
    length(policy_template_event_id) BETWEEN 8 AND 128
    AND policy_template_event_id GLOB 'ptevt_*'
  ),
  tenant_id TEXT NOT NULL,
  application_id TEXT NOT NULL,
  policy_template_id TEXT NOT NULL,
  template_version INTEGER NOT NULL CHECK (template_version=1),
  result_snapshot_id TEXT NOT NULL,
  result_snapshot_version INTEGER NOT NULL CHECK (
    typeof(result_snapshot_version)='integer' AND result_snapshot_version>=2
  ),
  event_type TEXT NOT NULL CHECK (event_type='policy_template.applied'),
  actor_user_id TEXT NOT NULL,
  audit_log_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=8192
    AND json_remove(
      payload_json,
      '$.template_code','$.template_version','$.application_id',
      '$.generated_policy_id','$.previous_snapshot_version',
      '$.result_snapshot_version','$.impact_hash','$.bindings_hash',
      '$.changed_resource_types'
    )='{}'
    AND json_type(payload_json,'$.template_code')='text'
    AND json_type(payload_json,'$.template_version')='integer'
    AND json_type(payload_json,'$.application_id')='text'
    AND json_type(payload_json,'$.generated_policy_id')='text'
    AND json_type(payload_json,'$.previous_snapshot_version')='integer'
    AND json_type(payload_json,'$.result_snapshot_version')='integer'
    AND json_type(payload_json,'$.impact_hash')='text'
    AND length(json_extract(payload_json,'$.impact_hash'))=64
    AND json_extract(payload_json,'$.impact_hash') NOT GLOB '*[^0-9a-f]*'
    AND json_type(payload_json,'$.bindings_hash')='text'
    AND length(json_extract(payload_json,'$.bindings_hash'))=64
    AND json_extract(payload_json,'$.bindings_hash') NOT GLOB '*[^0-9a-f]*'
    AND json_type(payload_json,'$.changed_resource_types')='array'
    -- 事件消费者按固定顺序解释两类变更；只限制数组长度会允许伪造其他资源类型。
    AND json_array_length(payload_json,'$.changed_resource_types')=2
    AND json_extract(payload_json,'$.changed_resource_types[0]')='policy'
    AND json_extract(payload_json,'$.changed_resource_types[1]')='policy_snapshot'
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  UNIQUE (tenant_id,policy_template_event_id),
  UNIQUE (tenant_id,application_id,event_type),
  UNIQUE (tenant_id,request_id,event_type),
  FOREIGN KEY (tenant_id,application_id)
    REFERENCES policy_template_applications(tenant_id,application_id),
  FOREIGN KEY (tenant_id,policy_template_id)
    REFERENCES policy_templates(tenant_id,policy_template_id),
  FOREIGN KEY (tenant_id,result_snapshot_id,result_snapshot_version)
    REFERENCES policy_snapshots(tenant_id,policy_snapshot_id,snapshot_version),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,audit_log_id) REFERENCES audit_logs(tenant_id,audit_log_id)
);
CREATE INDEX idx_policy_template_events_tenant_time_prd068
  ON policy_template_events(tenant_id,created_at DESC,policy_template_event_id DESC);
CREATE TRIGGER trg_policy_template_events_owner_insert_prd068
BEFORE INSERT ON policy_template_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1
  FROM policy_template_applications application
  JOIN policy_templates template
    ON template.tenant_id=application.tenant_id
   AND template.policy_template_id=application.policy_template_id
  WHERE application.tenant_id=NEW.tenant_id
    AND application.application_id=NEW.application_id
    AND application.policy_template_id=NEW.policy_template_id
    AND application.template_version=NEW.template_version
    AND application.result_snapshot_id=NEW.result_snapshot_id
    AND application.result_snapshot_version=NEW.result_snapshot_version
    AND application.actor_user_id=NEW.actor_user_id
    AND application.apply_audit_log_id=NEW.audit_log_id
    AND application.request_id=NEW.request_id
    AND application.trace_id=NEW.trace_id
    AND application.template_event_id=NEW.policy_template_event_id
    AND json_extract(NEW.payload_json,'$.template_code')=template.template_code
    AND json_extract(NEW.payload_json,'$.template_version')=application.template_version
    AND json_extract(NEW.payload_json,'$.application_id')=application.application_id
    AND json_extract(NEW.payload_json,'$.generated_policy_id')=application.generated_policy_id
    AND json_extract(NEW.payload_json,'$.previous_snapshot_version')
      =application.previous_snapshot_version
    AND json_extract(NEW.payload_json,'$.result_snapshot_version')
      =application.result_snapshot_version
    AND json_extract(NEW.payload_json,'$.impact_hash')=application.impact_hash
    AND json_extract(NEW.payload_json,'$.bindings_hash')=application.bindings_hash
  )
  OR EXISTS (
    SELECT 1 FROM policy_template_events current
    WHERE current.policy_template_event_id=NEW.policy_template_event_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.application_id=NEW.application_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'policy_template_event_owner_mismatch'); END;
CREATE TRIGGER trg_policy_template_events_update_prd068
BEFORE UPDATE ON policy_template_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_event_immutable'); END;
CREATE TRIGGER trg_policy_template_events_delete_prd068
BEFORE DELETE ON policy_template_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_event_immutable'); END;

CREATE TABLE audit_logs_prd068_new (
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
      'policy_template.applied','policy_template.rolled_back'
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
      'policy_template_application'
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
-- 审计换表逐列复制并保留 rowid，历史 17 个持久列和 NULL 语义不得发生漂移。
INSERT INTO audit_logs_prd068_new(
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

-- 删除父表前以行数、NULL 分布和含 rowid 双向差集复核复制，不能只比较主键数量。
CREATE TEMP TABLE prd068_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 复制后逐值核对行数与 17 列内容，任何差异都在删除旧审计表前失败关闭。
INSERT INTO prd068_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd068_new))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd068_new WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd068_new WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd068_new WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd068_new
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd068_new
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd068_audit_copy_guard;

-- legacy_alter_table 保持四个已核验的外部 owner trigger 原样指向正式表名；旧表本地
-- trigger/index 随 DROP 移除，重命名后按冻结定义逐项恢复。
DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后替换正式审计表，确保历史治理证据完整保真。
ALTER TABLE audit_logs_prd068_new RENAME TO audit_logs;
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
CREATE UNIQUE INDEX idx_audit_logs_config_transfer_request_unique
  ON audit_logs(tenant_id,actor_user_id,action,request_id)
  WHERE action IN ('config.imported','config.exported');
-- v1 的 created 与 version_published 共用 request_id，因此 action 是唯一键的一部分。
CREATE UNIQUE INDEX idx_audit_logs_prompt_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN (
    'prompt_template.created','prompt_template.updated',
    'prompt_template.version_published'
  );

-- tenant owner 候选键在 Audit 换表后必须显式恢复，供 application 的 deferred 环边使用。
CREATE UNIQUE INDEX uq_audit_logs_owner_prd068
  ON audit_logs(tenant_id,audit_log_id);
CREATE UNIQUE INDEX idx_audit_logs_policy_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN ('policy_template.applied','policy_template.rolled_back');

-- owner trigger 保留 0052 的全部旧领域约束，并为两类配置迁移动作增加同租户 source fact、
-- actor、request/trace、时间与精确摘要的双向绑定。任一不一致都回滚业务与审计事务。
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
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

-- Template Audit 必须反向绑定同 tenant application、actor、request/trace 与预生成 fact ID。
-- 摘要只允许稳定治理字段，明确排除绑定值、价格、预算余额、URL、Key 和请求正文。
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
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 每条新审计事实都登记导出序列，配置迁移审计不得绕过既有增量导出链。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;

-- generated Policy 全图是 application 的不可变来源事实。普通 CRUD 不得修改、停用或删除；
-- 唯一例外是 application 已先转为 rolled_back 后的 active/v1 -> disabled/v2 精确补偿跳转。
CREATE TRIGGER trg_policy_template_generated_policy_update_prd068
BEFORE UPDATE ON policies FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
AND NOT EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
    AND application.status='rolled_back'
    AND OLD.status='active' AND OLD.version=1
    AND NEW.status='disabled' AND NEW.version=2
    AND NEW.rowid=OLD.rowid AND NEW.policy_id=OLD.policy_id
    AND NEW.tenant_id=OLD.tenant_id AND NEW.name=OLD.name
    AND NEW.description IS OLD.description AND NEW.priority=OLD.priority
    AND NEW.dsl_version=OLD.dsl_version AND NEW.created_by=OLD.created_by
    AND NEW.created_at=OLD.created_at
    AND NEW.updated_by=application.rollback_actor_user_id
    AND NEW.updated_at=application.rolled_back_at
    AND application.rolled_back_policy_version=NEW.version
  )
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;
CREATE TRIGGER trg_policy_template_generated_policy_delete_prd068
BEFORE DELETE ON policies FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;

CREATE TRIGGER trg_policy_template_generated_rule_insert_prd068
BEFORE INSERT ON policy_rules FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=NEW.tenant_id
    AND application.generated_policy_id=NEW.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;
CREATE TRIGGER trg_policy_template_generated_rule_update_prd068
BEFORE UPDATE ON policy_rules FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;
CREATE TRIGGER trg_policy_template_generated_rule_delete_prd068
BEFORE DELETE ON policy_rules FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;

CREATE TRIGGER trg_policy_template_generated_action_insert_prd068
BEFORE INSERT ON policy_actions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=NEW.tenant_id
    AND application.generated_policy_id=NEW.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;
CREATE TRIGGER trg_policy_template_generated_action_update_prd068
BEFORE UPDATE ON policy_actions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;
CREATE TRIGGER trg_policy_template_generated_action_delete_prd068
BEFORE DELETE ON policy_actions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;

-- Project 可在事故处理中 active -> disabled 以失败关闭，但 owner/id 不可改变、disabled 不可
-- 在 application 仍 active 时重新启用；删除会破坏绑定和回滚证据，因此始终阻断。
CREATE TRIGGER trg_policy_template_project_update_prd068
BEFORE UPDATE ON projects FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='project'
    AND dependency.resource_id=OLD.project_id
    AND application.status='applied'
)
AND (
  NEW.rowid!=OLD.rowid OR NEW.project_id!=OLD.project_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR (OLD.status!='active' AND NEW.status='active')
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_project_delete_prd068
BEFORE DELETE ON projects FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='project'
    AND dependency.resource_id=OLD.project_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;

-- 国产模板冻结预览时 Environment 的 code；新建环境不在 frozen allow list 中，运行表达式
-- 本身会默认 deny，因此无需阻断企业正常创建环境。
CREATE TRIGGER trg_policy_template_environment_update_prd068
BEFORE UPDATE ON environments FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='environment'
    AND dependency.resource_id=OLD.environment_id
    AND application.status='applied'
)
AND (
  NEW.rowid!=OLD.rowid OR NEW.environment_id!=OLD.environment_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code
  OR (OLD.status!='active' AND NEW.status='active')
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_environment_delete_prd068
BEFORE DELETE ON environments FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='environment'
    AND dependency.resource_id=OLD.environment_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;

-- Alias 的名称、环境、status 和 default/fallback 图共同决定高安全/国产证明；尤其
-- active -> disabled 可能触发 Gateway 同名 direct Model 回退，因此任意 status 变化都必须阻断。
CREATE TRIGGER trg_policy_template_model_alias_insert_prd068
BEFORE INSERT ON model_aliases FOR EACH ROW
WHEN NEW.status='active' AND EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  JOIN policy_templates template
    ON template.tenant_id=application.tenant_id
   AND template.policy_template_id=application.policy_template_id
  WHERE dependency.tenant_id=NEW.tenant_id
    AND dependency.resource_type='model_alias'
    AND dependency.resource_name=NEW.alias
    AND application.status='applied'
    AND template.template_code='domestic_models_only'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_model_alias_update_prd068
BEFORE UPDATE ON model_aliases FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='model_alias'
    AND dependency.resource_id=OLD.model_alias_id
    AND application.status='applied'
)
AND (
  NEW.rowid!=OLD.rowid OR NEW.model_alias_id!=OLD.model_alias_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.alias!=OLD.alias
  OR NEW.environment!=OLD.environment OR NEW.environment_id IS NOT OLD.environment_id
  OR NEW.default_model_id!=OLD.default_model_id
  OR NEW.fallback_model_id IS NOT OLD.fallback_model_id
  OR NEW.status!=OLD.status
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_model_alias_delete_prd068
BEFORE DELETE ON model_aliases FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='model_alias'
    AND dependency.resource_id=OLD.model_alias_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;

-- Model 的 Provider 归属、真实 upstream 名称和核心 capabilities 共同冻结；价格和展示字段
-- 允许维护，active -> disabled 仍作为故障处置的 fail-closed 开关。
CREATE TRIGGER trg_policy_template_model_update_prd068
BEFORE UPDATE ON models FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='model'
    AND dependency.resource_id=OLD.model_id
    AND application.status='applied'
)
AND (
  NEW.rowid!=OLD.rowid OR NEW.model_id!=OLD.model_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.provider_id!=OLD.provider_id
  OR NEW.provider_model_id IS NOT OLD.provider_model_id
  OR NEW.upstream_model_name!=OLD.upstream_model_name
  OR NEW.capabilities!=OLD.capabilities
  OR (OLD.status!='active' AND NEW.status='active')
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_model_delete_prd068
BEFORE DELETE ON models FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='model'
    AND dependency.resource_id=OLD.model_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;

-- Provider 类型、base URL 和 auth 模式决定国产/migration/local 分类，不能在 ID 不变时偷换上游。
-- 健康字段和密钥池轮换不在此冻结，Gateway 每次请求仍按最新状态失败关闭。
CREATE TRIGGER trg_policy_template_provider_update_prd068
BEFORE UPDATE ON providers FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='provider'
    AND dependency.resource_id=OLD.provider_id
    AND application.status='applied'
)
AND (
  NEW.rowid!=OLD.rowid OR NEW.provider_id!=OLD.provider_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.provider_type!=OLD.provider_type
  OR NEW.base_url!=OLD.base_url OR NEW.auth_type!=OLD.auth_type
  OR (OLD.status!='active' AND NEW.status='active')
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_provider_delete_prd068
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='provider'
    AND dependency.resource_id=OLD.provider_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;

-- 提交前验证 revision 5、五模板、RequestLog/Audit 扩展、全部 owner 结构和数据库完整性。
DROP TABLE IF EXISTS temp.prd068_up_final_guard;
CREATE TEMP TABLE prd068_up_final_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd068_up_final_state_ok CHECK (issue_count=0)
);
-- 提交前重新聚合目录、角色、模板、Audit/RequestLog 和数据库完整性，防止部分状态落盘。
INSERT INTO prd068_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=56
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=5)=56
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code LIKE 'policy_template.%')=2
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=149
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code LIKE 'policy_template.%')=8
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*56 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=5)
      =(SELECT COUNT(*) FROM permissions)
      AND (SELECT COUNT(*) FROM permissions WHERE code LIKE 'policy_template.%')
      =(SELECT COUNT(*)*2 FROM tenants)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd068_roles_to_expand source
     JOIN roles role
       ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=source.permission_revision+1
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
          WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
          WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
          WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
        END)
  + CASE WHEN (SELECT COUNT(*) FROM policy_templates)
      =(SELECT COUNT(*)*5 FROM tenants)
      AND (SELECT COUNT(*) FROM policy_templates WHERE status='active' AND version=1)
      =(SELECT COUNT(*) FROM policy_templates)
      AND (SELECT COUNT(*) FROM policy_templates
           WHERE template_code='low_cost_mode' AND priority=1000)
      =(SELECT COUNT(*) FROM tenants)
      AND (SELECT COUNT(*) FROM policy_templates
           WHERE template_code!='low_cost_mode' AND priority=10)
      =(SELECT COUNT(*)*4 FROM tenants)
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('request_logs') ORDER BY cid
      )
    ) LIKE '%,policy_target_model_id,policy_mask_applied,policy_mask_policy_id'
    THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs'),'policy_template.rolled_back')>0
      AND instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs'),'policy_template_application')>0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN (
      'policy_templates','policy_template_applications',
      'policy_template_application_items','policy_template_application_dependencies',
      'policy_template_events'
    ))=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN (
      'uq_policy_events_owner_prd068','uq_policy_snapshot_events_owner_prd068',
      'uq_audit_logs_owner_prd068','idx_audit_logs_policy_template_request_unique',
      'uq_policy_template_applications_active_project_prd068'
    ))=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd068_up_final_guard;
DROP TABLE prd068_roles_to_expand;
COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
