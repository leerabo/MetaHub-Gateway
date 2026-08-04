-- PRD-066：企业 Chat 演示入口的 RBAC revision 3 与短时执行元数据。
-- 安全边界：三张表只保存稳定 ID、状态、用量和固定治理摘要；Prompt、Response、正文摘要、
-- Provider Key、Key 前缀、Endpoint、邮箱及自由错误文本均不得进入本迁移的数据模型。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前验证数据库精确位于 0057 栈顶。TEMP CHECK 失败会原子中止，避免在未知
-- 核心结构或已损坏 RBAC 投影上继续换表，产生权限误授予或跨租户悬空关系。
DROP TABLE IF EXISTS temp.prd066_up_guard;
CREATE TEMP TABLE prd066_up_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd066_up_preflight_ok CHECK (issue_count = 0)
);
-- 把所有栈顶、核心对象和 RBAC 基线检查折叠为一个原子计数，任一偏差都在持久 DDL 前中止。
INSERT INTO prd066_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0057' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
     WHERE status='applied' AND version>'0057')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','users','sessions','projects','environments','model_aliases',
        'virtual_keys','roles','permissions','role_permissions',
        'rbac_permission_templates','rbac_role_permission_templates',
        'role_permission_events','webhook_endpoints','webhook_deliveries',
        'webhook_delivery_attempts','webhook_dead_letter_events',
        'role_dashboard_configs','console_page_events'
      ))=19 THEN 0 ELSE 1 END
  -- Chat 对象和换表暂存名必须全部空闲；同名未知对象不能被覆盖。
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'chat_sessions','chat_messages','chat_execution_grants',
      'rbac_permission_templates_prd066_new','permissions_prd066_new',
      'uq_sessions_tenant_session_prd066',
      'uq_chat_sessions_tenant_session_prd066',
      'idx_chat_sessions_tenant_owner_status_prd066',
      'idx_chat_sessions_tenant_expiry_prd066',
      'uq_chat_messages_tenant_message_prd066',
      'uq_chat_messages_tenant_session_client_prd066',
      'uq_chat_messages_tenant_gateway_request_prd066',
      'uq_chat_messages_tenant_session_inflight_prd066',
      'idx_chat_messages_tenant_session_sequence_prd066',
      'uq_chat_execution_grants_hash_prd066',
      'uq_chat_execution_grants_tenant_message_prd066',
      'idx_chat_execution_grants_tenant_session_status_prd066',
      'idx_chat_execution_grants_tenant_expiry_prd066'
    ))
  -- 0058 读取的核心父表列必须与 0057 冻结结构一致，不能把 NULL 旧字段误当通配。
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('sessions') ORDER BY cid
      )
    )='session_id,tenant_id,user_id,token_jti,token_hash,status,expires_at,revoked_at,last_seen_at,user_agent,created_ip,created_at,updated_at'
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('projects') ORDER BY cid
      )
    )='project_id,tenant_id,project_code,name,description,owner_user_id,status,created_by,updated_by,created_at,updated_at'
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('environments') ORDER BY cid
      )
    )='environment_id,tenant_id,code,name,description,status,created_by,updated_by,created_at,updated_at'
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('model_aliases') ORDER BY cid
      )
    )='model_alias_id,tenant_id,alias,environment,default_model_id,fallback_model_id,status,description,created_by,disabled_at,disabled_by,created_at,updated_at,environment_id'
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('virtual_keys') ORDER BY cid
      )
    )='virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,environment,description,last_used_at,created_by,disabled_at,disabled_by,project_id,application_id,environment_id'
    THEN 0 ELSE 1 END
  -- 除登录 Session 外，历史迁移已建立 Chat 需要的 tenant-qualified 父键。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN (
      'idx_users_tenant_user_prd059','uq_projects_tenant_project_prd051',
      'uq_environments_tenant_environment_prd051','uq_model_aliases_tenant_id_prd047',
      'uq_virtual_keys_tenant_key_prd051'
    ))=5 THEN 0 ELSE 1 END
  -- 目录必须是 0054 交付的完整 revision 2，不能在未知本地目录上猜测升级。
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=50
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=2)=50
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code='chat.use')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=131
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code='chat.use')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*50 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=2)
      =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  -- 每租户 Permission 必须仍是产品模板的逐字段不可变投影。
  + (SELECT COUNT(*)
     FROM permissions permission
     LEFT JOIN rbac_permission_templates template
       ON template.permission_code=permission.code
      AND template.category=permission.category
      AND template.name=permission.name
      AND template.description=permission.description
      AND template.risk_level=permission.risk_level
      AND template.catalog_revision=permission.catalog_revision
     WHERE template.permission_code IS NULL OR permission.status!='active')
  + CASE WHEN (SELECT COUNT(*) FROM roles)=(SELECT COUNT(*)*6 FROM tenants)
    THEN 0 ELSE 1 END
  -- Admin 不可变矩阵必须仍为 revision 2 默认全集；Developer/Viewer 的定制矩阵允许保留。
  + (SELECT COUNT(*) FROM roles role
     WHERE role.code='admin' AND (
       role.permission_revision NOT IN (1,2)
       OR role.matrix_sha256!='1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
       OR EXISTS (
         SELECT 1 FROM rbac_role_permission_templates template
         WHERE template.role_code='admin' AND NOT EXISTS (
           SELECT 1 FROM role_permissions relation
           JOIN permissions permission
             ON permission.tenant_id=relation.tenant_id
            AND permission.permission_id=relation.permission_id
           WHERE relation.tenant_id=role.tenant_id
             AND relation.role_id=role.role_id
             AND permission.code=template.permission_code
         )
       )
       OR EXISTS (
         SELECT 1 FROM role_permissions relation
         JOIN permissions permission
           ON permission.tenant_id=relation.tenant_id
          AND permission.permission_id=relation.permission_id
         WHERE relation.tenant_id=role.tenant_id
           AND relation.role_id=role.role_id
           AND NOT EXISTS (
             SELECT 1 FROM rbac_role_permission_templates template
             WHERE template.role_code='admin'
               AND template.permission_code=permission.code
           )
       )
     ))
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_tenants_rbac_initialize_prd059','trg_roles_owner_insert_prd059',
        'trg_roles_matrix_update_prd059','trg_role_permissions_owner_insert_prd059',
        'trg_role_permissions_delete_prd059','trg_permissions_owner_insert_prd059',
        'trg_permissions_update_prd059','trg_permissions_delete_prd059',
        'trg_permissions_rowid_insert_prd059',
        'trg_rbac_permission_templates_insert_prd059',
        'trg_rbac_permission_templates_update_prd059',
        'trg_rbac_permission_templates_delete_prd059',
        'trg_rbac_role_templates_insert_prd059',
        'trg_rbac_role_templates_update_prd059',
        'trg_rbac_role_templates_delete_prd059'
      ))=15 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd066_up_guard;

-- 只冻结“旧 hash + 合法 revision + 真实关系全集”三者同时匹配的角色。自定义矩阵即使角色名
-- 相同也不会被静默扩权；0054 后创建的角色 revision=1，历史升级角色可为 revision=2。
CREATE TEMP TABLE prd066_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','viewer')
  AND role.permission_revision IN (
    1,CASE WHEN role.code IN ('admin','developer') THEN 2 ELSE 1 END
  )
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
    WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
    WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
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

-- 产品 Permission 目录整体提升到 revision 3；旧 50 行保留 rowid 和展示元数据。
CREATE TABLE rbac_permission_templates_prd066_new (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=3)
);
-- 旧目录逐行保留 rowid 与展示语义，只把统一 catalog revision 提升到本次冻结版本。
INSERT INTO rbac_permission_templates_prd066_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,3
FROM rbac_permission_templates;
-- 企业 Chat 权限作为独立写权限追加，避免借用项目或模型广域读取权限造成默认扩权。
INSERT INTO rbac_permission_templates_prd066_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES(
  'chat.use','chat','使用企业 Chat',
  '使用受 Gateway 全链路治理且不长期保存正文的企业 Chat。','write',3
);

-- 模板是发布元数据，迁移仅在同一事务内短暂移除不可变门禁。
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TABLE rbac_permission_templates;
-- 换表只发生在同一事务且数据已完整复制后，保证目录主键和角色模板外键继续指向正式表名。
ALTER TABLE rbac_permission_templates_prd066_new RENAME TO rbac_permission_templates;

-- 只有 Admin、Developer、Viewer 默认及可授予企业 Chat；财务、安全和运维角色保持最小权限。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','chat.use',1),
  ('developer','chat.use',1),
  ('viewer','chat.use',1);

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

-- 租户 Permission 仍是产品模板逐字段投影；旧 ID、rowid 和时间全部保真，仅提升目录版本。
CREATE TABLE permissions_prd066_new (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=3),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 现有租户权限逐行保留 rowid、稳定 ID 和状态，只同步产品目录 revision，不改变授权关系。
INSERT INTO permissions_prd066_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       3,status,created_at,updated_at
FROM permissions;
-- 每个现有 tenant 都获得自己的 chat.use 权限实体，后续角色授权仍通过 tenant 复合关系约束。
INSERT INTO permissions_prd066_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT
  'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,
  3,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template ON template.permission_code='chat.use';

DROP TABLE permissions;
-- 所有租户权限实体写齐后再恢复正式表名，避免角色关系在迁移中观察到半完成目录。
ALTER TABLE permissions_prd066_new RENAME TO permissions;
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

-- 迁移自动补权允许 disabled 但规范的角色，因此短暂移除运行期 active owner 门禁；候选集合、
-- 双复合外键和唯一键仍共同限制写入范围。
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
-- 只为预检冻结的 canonical Admin/Developer/Viewer 增加 chat.use，自定义矩阵和其余角色保持原样。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT
  'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd066_roles_to_expand role
JOIN permissions permission
  ON permission.tenant_id=role.tenant_id AND permission.code='chat.use';
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

-- 只有确实新增关系的角色推进一次 CAS revision；自定义矩阵的 revision/hash 原样保留。
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
-- revision 与新矩阵 hash 必须同步更新，防止权限关系已扩展而认证快照仍声明旧矩阵。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
      WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
      WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
    END
WHERE role_id IN (SELECT role_id FROM prd066_roles_to_expand);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
    WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
    WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
    WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
    WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
    WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
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

-- 新租户直接使用 revision 3 目录；角色 revision 仍从 1 开始，因为它描述角色矩阵变更次数，
-- 不是产品 Permission catalog revision。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户先建立六个 canonical 角色，矩阵 hash 直接使用 revision 3 的冻结权限集合。
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
      WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
      WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
      WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
      WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
      WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
      WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 权限实体按新租户逐项投影完整目录，禁止跨租户复用 permission_id 或目录元数据。
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

  -- 最后按同 tenant 的角色和权限实体建立默认矩阵，避免初始化过程出现跨租户拼接。
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


-- 登录 Session 原表只有全局主键；补最小 tenant-qualified 唯一父键，不改变认证行为。
CREATE UNIQUE INDEX uq_sessions_tenant_session_prd066
  ON sessions(tenant_id,session_id);

-- ChatSession 冻结用户、登录 Session 和执行资源 owner，不保存任何对话正文。
CREATE TABLE chat_sessions (
  chat_session_id TEXT PRIMARY KEY CHECK (
    length(chat_session_id)=36 AND substr(chat_session_id,1,4)='chs_'
    AND substr(chat_session_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  owner_auth_session_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  model_alias TEXT NOT NULL CHECK (length(model_alias) BETWEEN 1 AND 160),
  virtual_key_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','cleared','expired')),
  message_sequence INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(message_sequence)='integer' AND message_sequence>=0
  ),
  state_revision INTEGER NOT NULL DEFAULT 1 CHECK (
    typeof(state_revision)='integer' AND state_revision>=1
  ),
  expires_at TEXT NOT NULL,
  cleared_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,owner_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,owner_auth_session_id) REFERENCES sessions(tenant_id,session_id),
  FOREIGN KEY (tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  FOREIGN KEY (tenant_id,environment_id) REFERENCES environments(tenant_id,environment_id),
  FOREIGN KEY (tenant_id,model_alias_id) REFERENCES model_aliases(tenant_id,model_alias_id),
  FOREIGN KEY (tenant_id,virtual_key_id) REFERENCES virtual_keys(tenant_id,virtual_key_id),
  CHECK (
    (status='cleared' AND cleared_at IS NOT NULL)
    OR (status IN ('active','expired') AND cleared_at IS NULL)
  )
);
CREATE UNIQUE INDEX uq_chat_sessions_tenant_session_prd066
  ON chat_sessions(tenant_id,chat_session_id);
CREATE INDEX idx_chat_sessions_tenant_owner_status_prd066
  ON chat_sessions(tenant_id,owner_user_id,status,updated_at DESC);
CREATE INDEX idx_chat_sessions_tenant_expiry_prd066
  ON chat_sessions(tenant_id,status,expires_at);

-- INSERT 门禁同时复核登录主体和所有执行资源；旧 NULL environment_id 仅可按唯一环境 code
-- 精确兼容，绝不作为任意环境通配。项目下 active Key 必须恰好一把。
CREATE TRIGGER trg_chat_sessions_owner_insert_prd066
BEFORE INSERT ON chat_sessions FOR EACH ROW
WHEN NEW.status!='active' OR NEW.message_sequence!=0 OR NEW.state_revision!=1
  OR NEW.cleared_at IS NOT NULL OR NEW.created_at IS NOT NEW.updated_at
  OR julianday(NEW.created_at) IS NULL OR julianday(NEW.expires_at) IS NULL
  OR julianday(NEW.expires_at)<=julianday(NEW.created_at)
  OR julianday(NEW.expires_at)>julianday(NEW.created_at,'+8 hours')
  OR NOT EXISTS (
    SELECT 1 FROM users user
    WHERE user.tenant_id=NEW.tenant_id AND user.user_id=NEW.owner_user_id
      AND user.status='active'
  )
  OR NOT EXISTS (
    SELECT 1 FROM sessions session
    WHERE session.tenant_id=NEW.tenant_id
      AND session.session_id=NEW.owner_auth_session_id
      AND session.user_id=NEW.owner_user_id AND session.status='active'
      AND julianday(session.expires_at)>julianday(NEW.created_at)
  )
  OR NOT EXISTS (
    SELECT 1 FROM projects project
    WHERE project.tenant_id=NEW.tenant_id AND project.project_id=NEW.project_id
      AND project.status='active'
  )
  OR NOT EXISTS (
    SELECT 1 FROM environments environment
    WHERE environment.tenant_id=NEW.tenant_id
      AND environment.environment_id=NEW.environment_id
      AND environment.status='active'
  )
  OR NOT EXISTS (
    SELECT 1 FROM model_aliases alias
    JOIN environments environment
      ON environment.tenant_id=alias.tenant_id
     AND environment.environment_id=NEW.environment_id
    WHERE alias.tenant_id=NEW.tenant_id
      AND alias.model_alias_id=NEW.model_alias_id
      AND alias.alias=NEW.model_alias AND alias.status='active'
      AND environment.status='active'
      AND (alias.environment_id=NEW.environment_id
           OR (alias.environment_id IS NULL AND alias.environment=environment.code))
  )
  OR NOT EXISTS (
    SELECT 1 FROM virtual_keys virtual_key
    JOIN environments environment
      ON environment.tenant_id=virtual_key.tenant_id
     AND environment.environment_id=NEW.environment_id
    WHERE virtual_key.tenant_id=NEW.tenant_id
      AND virtual_key.virtual_key_id=NEW.virtual_key_id
      AND virtual_key.project_id=NEW.project_id
      AND virtual_key.status='active' AND environment.status='active'
      AND (virtual_key.environment_id=NEW.environment_id
           OR (virtual_key.environment_id IS NULL
               AND virtual_key.environment=environment.code))
  )
  OR (
    SELECT COUNT(*) FROM virtual_keys virtual_key
    JOIN environments environment
      ON environment.tenant_id=virtual_key.tenant_id
     AND environment.environment_id=NEW.environment_id
    WHERE virtual_key.tenant_id=NEW.tenant_id
      AND virtual_key.project_id=NEW.project_id
      AND virtual_key.status='active' AND environment.status='active'
      AND (virtual_key.environment_id=NEW.environment_id
           OR (virtual_key.environment_id IS NULL
               AND virtual_key.environment=environment.code))
  )!=1
  OR EXISTS (
    SELECT 1 FROM chat_sessions current
    WHERE current.chat_session_id=NEW.chat_session_id
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'chat_session_owner_mismatch'); END;

-- 仅 active 会话可按 CAS 推进；身份和资源快照不可修改，终态不可回退。
CREATE TRIGGER trg_chat_sessions_state_update_prd066
BEFORE UPDATE ON chat_sessions FOR EACH ROW
WHEN OLD.status!='active'
  OR NEW.rowid!=OLD.rowid OR NEW.chat_session_id!=OLD.chat_session_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.owner_user_id!=OLD.owner_user_id
  OR NEW.owner_auth_session_id!=OLD.owner_auth_session_id
  OR NEW.project_id!=OLD.project_id OR NEW.environment_id!=OLD.environment_id
  OR NEW.model_alias_id!=OLD.model_alias_id OR NEW.model_alias!=OLD.model_alias
  OR NEW.virtual_key_id!=OLD.virtual_key_id OR NEW.expires_at!=OLD.expires_at
  -- 应用统一使用 UTC 秒；同一秒内 prepare/finalize 可共享时间文本，revision 仍防止倒退。
  OR NEW.created_at!=OLD.created_at OR NEW.updated_at<OLD.updated_at
  OR NEW.state_revision!=OLD.state_revision+1
  OR NEW.message_sequence NOT IN (OLD.message_sequence,OLD.message_sequence+1)
  OR NEW.status NOT IN ('active','cleared','expired')
  OR (NEW.status='active' AND NEW.cleared_at IS NOT NULL)
  OR (NEW.status='cleared' AND NEW.cleared_at IS NULL)
  OR (NEW.status='expired' AND NEW.cleared_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'chat_session_state_conflict'); END;
CREATE TRIGGER trg_chat_sessions_delete_prd066
BEFORE DELETE ON chat_sessions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'chat_session_immutable'); END;

-- ChatMessage 是一次发送的治理元数据，不含用户或模型正文。
CREATE TABLE chat_messages (
  chat_message_id TEXT PRIMARY KEY CHECK (
    length(chat_message_id)=36 AND substr(chat_message_id,1,4)='chm_'
    AND substr(chat_message_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  chat_session_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  client_message_id TEXT NOT NULL CHECK (
    length(client_message_id)=36
    AND substr(client_message_id,9,1)='-'
    AND substr(client_message_id,14,1)='-'
    AND substr(client_message_id,19,1)='-'
    AND substr(client_message_id,24,1)='-'
    AND replace(client_message_id,'-','') NOT GLOB '*[^0-9a-f]*'
  ),
  message_sequence INTEGER NOT NULL CHECK (
    typeof(message_sequence)='integer' AND message_sequence>=1
  ),
  gateway_request_id TEXT NOT NULL CHECK (
    length(gateway_request_id) BETWEEN 1 AND 128
    AND gateway_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  gateway_trace_id TEXT NOT NULL CHECK (
    length(gateway_trace_id) BETWEEN 1 AND 160
    AND gateway_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('in_flight','succeeded','failed')),
  input_tokens INTEGER CHECK (
    input_tokens IS NULL OR (typeof(input_tokens)='integer' AND input_tokens>=0)
  ),
  output_tokens INTEGER CHECK (
    output_tokens IS NULL OR (typeof(output_tokens)='integer' AND output_tokens>=0)
  ),
  total_tokens INTEGER CHECK (
    total_tokens IS NULL OR (typeof(total_tokens)='integer' AND total_tokens>=0)
  ),
  cache_tokens INTEGER CHECK (
    cache_tokens IS NULL OR (typeof(cache_tokens)='integer' AND cache_tokens>=0)
  ),
  cost_cny_micros INTEGER CHECK (
    cost_cny_micros IS NULL
    OR (typeof(cost_cny_micros)='integer' AND cost_cny_micros>=0)
  ),
  policy_decision TEXT CHECK (
    policy_decision IS NULL OR policy_decision IN ('allow','deny','route_to')
  ),
  policy_reason_code TEXT CHECK (
    policy_reason_code IS NULL OR (
      length(policy_reason_code) BETWEEN 1 AND 128
      AND policy_reason_code NOT GLOB '*[^A-Za-z0-9_.:-]*'
    )
  ),
  fallback_triggered INTEGER CHECK (
    fallback_triggered IS NULL OR fallback_triggered IN (0,1)
  ),
  cache_status TEXT CHECK (
    cache_status IS NULL
    OR cache_status IN ('hit','miss','not_configured','unknown')
  ),
  error_code TEXT CHECK (
    error_code IS NULL OR (
      length(error_code) BETWEEN 1 AND 128
      AND error_code NOT GLOB '*[^A-Za-z0-9_.:-]*'
    )
  ),
  webhook_event_id TEXT NOT NULL CHECK (
    webhook_event_id='evt_'||chat_message_id
  ),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,chat_session_id)
    REFERENCES chat_sessions(tenant_id,chat_session_id),
  FOREIGN KEY (tenant_id,owner_user_id) REFERENCES users(tenant_id,user_id),
  CHECK (
    total_tokens IS NULL OR input_tokens IS NULL OR output_tokens IS NULL
    OR total_tokens=input_tokens+output_tokens
  ),
  CHECK (
    (status='in_flight' AND completed_at IS NULL AND input_tokens IS NULL
      AND output_tokens IS NULL AND total_tokens IS NULL AND cache_tokens IS NULL
      AND cost_cny_micros IS NULL AND policy_decision IS NULL
      AND policy_reason_code IS NULL AND fallback_triggered IS NULL
      AND cache_status IS NULL AND error_code IS NULL)
    OR (status='succeeded' AND completed_at IS NOT NULL AND error_code IS NULL)
    OR (status='failed' AND completed_at IS NOT NULL AND error_code IS NOT NULL)
  )
);
CREATE UNIQUE INDEX uq_chat_messages_tenant_message_prd066
  ON chat_messages(tenant_id,chat_message_id);
CREATE UNIQUE INDEX uq_chat_messages_tenant_session_client_prd066
  ON chat_messages(tenant_id,chat_session_id,client_message_id);
CREATE UNIQUE INDEX uq_chat_messages_tenant_gateway_request_prd066
  ON chat_messages(tenant_id,gateway_request_id);
CREATE UNIQUE INDEX idx_chat_messages_tenant_session_sequence_prd066
  ON chat_messages(tenant_id,chat_session_id,message_sequence);
CREATE UNIQUE INDEX uq_chat_messages_tenant_session_inflight_prd066
  ON chat_messages(tenant_id,chat_session_id)
  WHERE status='in_flight';

-- 消息只能在已由 Session CAS 占用的新序列上创建；owner、过期和稳定 event ID 均由数据库复核。
CREATE TRIGGER trg_chat_messages_owner_insert_prd066
BEFORE INSERT ON chat_messages FOR EACH ROW
WHEN NEW.status!='in_flight' OR NEW.completed_at IS NOT NULL
  OR julianday(NEW.started_at) IS NULL
  OR NOT EXISTS (
    SELECT 1 FROM chat_sessions session
    WHERE session.tenant_id=NEW.tenant_id
      AND session.chat_session_id=NEW.chat_session_id
      AND session.owner_user_id=NEW.owner_user_id
      AND session.status='active'
      AND session.message_sequence=NEW.message_sequence
      AND julianday(session.expires_at)>julianday(NEW.started_at)
  )
  OR EXISTS (
    SELECT 1 FROM chat_messages current
    WHERE current.chat_message_id=NEW.chat_message_id
       OR current.webhook_event_id=NEW.webhook_event_id
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'chat_message_owner_mismatch'); END;

-- 一条消息只允许从 in_flight 终结一次；请求、owner、顺序和事件 ID 永久冻结。
CREATE TRIGGER trg_chat_messages_state_update_prd066
BEFORE UPDATE ON chat_messages FOR EACH ROW
WHEN OLD.status!='in_flight' OR NEW.status NOT IN ('succeeded','failed')
  OR NEW.rowid!=OLD.rowid OR NEW.chat_message_id!=OLD.chat_message_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.chat_session_id!=OLD.chat_session_id
  OR NEW.owner_user_id!=OLD.owner_user_id
  OR NEW.client_message_id!=OLD.client_message_id
  OR NEW.message_sequence!=OLD.message_sequence
  OR NEW.gateway_request_id!=OLD.gateway_request_id
  OR NEW.gateway_trace_id!=OLD.gateway_trace_id
  OR NEW.webhook_event_id!=OLD.webhook_event_id
  OR NEW.started_at!=OLD.started_at OR NEW.completed_at IS NULL
  OR julianday(NEW.completed_at) IS NULL
  OR julianday(NEW.completed_at)<julianday(OLD.started_at)
  OR (NEW.status='succeeded' AND NEW.error_code IS NOT NULL)
  OR (NEW.status='failed' AND NEW.error_code IS NULL)
BEGIN SELECT RAISE(ABORT,'chat_message_state_conflict'); END;
CREATE TRIGGER trg_chat_messages_delete_prd066
BEFORE DELETE ON chat_messages FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'chat_message_immutable'); END;

-- 单次 grant 只保存 256 bit 随机明文的 SHA-256，并冻结完整执行 owner 图。
CREATE TABLE chat_execution_grants (
  chat_execution_grant_id TEXT PRIMARY KEY CHECK (
    length(chat_execution_grant_id)=36 AND substr(chat_execution_grant_id,1,4)='chg_'
    AND substr(chat_execution_grant_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  grant_hash TEXT NOT NULL CHECK (
    length(grant_hash)=64 AND grant_hash NOT GLOB '*[^0-9a-f]*'
  ),
  chat_session_id TEXT NOT NULL,
  chat_message_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  owner_auth_session_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  virtual_key_id TEXT NOT NULL,
  gateway_request_id TEXT NOT NULL CHECK (
    length(gateway_request_id) BETWEEN 1 AND 128
    AND gateway_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  gateway_trace_id TEXT NOT NULL CHECK (
    length(gateway_trace_id) BETWEEN 1 AND 160
    AND gateway_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('issued','consumed','revoked')),
  expires_at TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  consumed_at TEXT,
  revoked_at TEXT,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,chat_session_id)
    REFERENCES chat_sessions(tenant_id,chat_session_id),
  FOREIGN KEY (tenant_id,chat_message_id)
    REFERENCES chat_messages(tenant_id,chat_message_id),
  FOREIGN KEY (tenant_id,owner_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,owner_auth_session_id) REFERENCES sessions(tenant_id,session_id),
  FOREIGN KEY (tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  FOREIGN KEY (tenant_id,environment_id) REFERENCES environments(tenant_id,environment_id),
  FOREIGN KEY (tenant_id,model_alias_id) REFERENCES model_aliases(tenant_id,model_alias_id),
  FOREIGN KEY (tenant_id,virtual_key_id) REFERENCES virtual_keys(tenant_id,virtual_key_id),
  CHECK (
    (status='issued' AND consumed_at IS NULL AND revoked_at IS NULL)
    OR (status='consumed' AND consumed_at IS NOT NULL AND revoked_at IS NULL)
    OR (status='revoked' AND revoked_at IS NOT NULL AND consumed_at IS NULL)
  )
);
CREATE UNIQUE INDEX uq_chat_execution_grants_hash_prd066
  ON chat_execution_grants(grant_hash);
-- 一条 ChatMessage 只能签发一张 grant；仅约束 hash 无法阻止同一请求被重复计费。
CREATE UNIQUE INDEX uq_chat_execution_grants_tenant_message_prd066
  ON chat_execution_grants(tenant_id,chat_message_id);
CREATE INDEX idx_chat_execution_grants_tenant_session_status_prd066
  ON chat_execution_grants(tenant_id,chat_session_id,status,issued_at DESC);
CREATE INDEX idx_chat_execution_grants_tenant_expiry_prd066
  ON chat_execution_grants(tenant_id,status,expires_at);

-- grant 创建时必须与同租户 Session/Message 的不可变快照逐项一致，并限制在 30 秒窗口。
CREATE TRIGGER trg_chat_execution_grants_owner_insert_prd066
BEFORE INSERT ON chat_execution_grants FOR EACH ROW
WHEN NEW.status!='issued' OR NEW.consumed_at IS NOT NULL OR NEW.revoked_at IS NOT NULL
  OR julianday(NEW.issued_at) IS NULL OR julianday(NEW.expires_at) IS NULL
  OR julianday(NEW.expires_at)<=julianday(NEW.issued_at)
  OR julianday(NEW.expires_at)>julianday(NEW.issued_at,'+30 seconds')
  OR NOT EXISTS (
    SELECT 1
    FROM chat_sessions session
    JOIN chat_messages message
      ON message.tenant_id=session.tenant_id
     AND message.chat_session_id=session.chat_session_id
    WHERE session.tenant_id=NEW.tenant_id
      AND session.chat_session_id=NEW.chat_session_id
      AND session.owner_user_id=NEW.owner_user_id
      AND session.owner_auth_session_id=NEW.owner_auth_session_id
      AND session.project_id=NEW.project_id
      AND session.environment_id=NEW.environment_id
      AND session.model_alias_id=NEW.model_alias_id
      AND session.virtual_key_id=NEW.virtual_key_id
      AND session.status='active'
      AND julianday(session.expires_at)>julianday(NEW.issued_at)
      AND message.chat_message_id=NEW.chat_message_id
      AND message.owner_user_id=NEW.owner_user_id
      AND message.gateway_request_id=NEW.gateway_request_id
      AND message.gateway_trace_id=NEW.gateway_trace_id
      AND message.status='in_flight'
  )
  OR NOT EXISTS (
    SELECT 1 FROM sessions auth_session
    WHERE auth_session.tenant_id=NEW.tenant_id
      AND auth_session.session_id=NEW.owner_auth_session_id
      AND auth_session.user_id=NEW.owner_user_id
      AND auth_session.status='active'
      AND julianday(auth_session.expires_at)>julianday(NEW.issued_at)
  )
  OR EXISTS (
    SELECT 1 FROM chat_execution_grants current
    WHERE current.chat_execution_grant_id=NEW.chat_execution_grant_id
       OR current.grant_hash=NEW.grant_hash
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'chat_grant_owner_mismatch'); END;

-- grant 只能 issued -> consumed/revoked；消费时间必须落在 30 秒有效窗内，所有 owner 字段不可改绑。
CREATE TRIGGER trg_chat_execution_grants_state_update_prd066
BEFORE UPDATE ON chat_execution_grants FOR EACH ROW
WHEN OLD.status!='issued' OR NEW.status NOT IN ('consumed','revoked')
  OR NEW.rowid!=OLD.rowid
  OR NEW.chat_execution_grant_id!=OLD.chat_execution_grant_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.grant_hash!=OLD.grant_hash
  OR NEW.chat_session_id!=OLD.chat_session_id
  OR NEW.chat_message_id!=OLD.chat_message_id
  OR NEW.owner_user_id!=OLD.owner_user_id
  OR NEW.owner_auth_session_id!=OLD.owner_auth_session_id
  OR NEW.project_id!=OLD.project_id OR NEW.environment_id!=OLD.environment_id
  OR NEW.model_alias_id!=OLD.model_alias_id OR NEW.virtual_key_id!=OLD.virtual_key_id
  OR NEW.gateway_request_id!=OLD.gateway_request_id
  OR NEW.gateway_trace_id!=OLD.gateway_trace_id
  OR NEW.expires_at!=OLD.expires_at OR NEW.issued_at!=OLD.issued_at
  OR (NEW.status='consumed' AND (
      NEW.consumed_at IS NULL OR NEW.revoked_at IS NOT NULL
      OR julianday(NEW.consumed_at) IS NULL
      OR julianday(NEW.consumed_at)<julianday(OLD.issued_at)
      OR julianday(NEW.consumed_at)>julianday(OLD.expires_at)
  ))
  OR (NEW.status='revoked' AND (
      NEW.revoked_at IS NULL OR NEW.consumed_at IS NOT NULL
      OR julianday(NEW.revoked_at) IS NULL
      OR julianday(NEW.revoked_at)<julianday(OLD.issued_at)
  ))
BEGIN SELECT RAISE(ABORT,'chat_grant_state_conflict'); END;
CREATE TRIGGER trg_chat_execution_grants_delete_prd066
BEFORE DELETE ON chat_execution_grants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'chat_grant_immutable'); END;

-- 提交前验证目录、默认补权、三表、索引、触发器和全部外键；测试不能替代迁移内原子断言。
DROP TABLE IF EXISTS temp.prd066_up_final_guard;
CREATE TEMP TABLE prd066_up_final_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd066_up_final_state_ok CHECK (issue_count = 0)
);
-- 提交前把目录数量、角色矩阵、Chat 对象和外键完整性汇总为零偏差断言，失败时整体回滚。
INSERT INTO prd066_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=51
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=3)=51
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code='chat.use' AND category='chat'
             AND risk_level='write')=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=134
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code='chat.use'
             AND role_code IN ('admin','developer','viewer'))=3
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*51 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=3)
      =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd066_roles_to_expand source
     JOIN roles role
       ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=source.permission_revision+1
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
          WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
          WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
        END
        OR (SELECT COUNT(*) FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=role.tenant_id
              AND relation.role_id=role.role_id
              AND permission.code='chat.use')!=1)
  + (SELECT COUNT(*) FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code='chat.use'
       AND NOT EXISTS (
         SELECT 1 FROM prd066_roles_to_expand source
         WHERE source.tenant_id=relation.tenant_id
           AND source.role_id=relation.role_id
       ))
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'chat_sessions','chat_messages','chat_execution_grants'
      ))=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_chat_%_prd066')=9
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name IN (
        'uq_sessions_tenant_session_prd066',
        'uq_chat_sessions_tenant_session_prd066',
        'idx_chat_sessions_tenant_owner_status_prd066',
        'idx_chat_sessions_tenant_expiry_prd066',
        'uq_chat_messages_tenant_message_prd066',
        'uq_chat_messages_tenant_session_client_prd066',
        'uq_chat_messages_tenant_gateway_request_prd066',
        'uq_chat_messages_tenant_session_inflight_prd066',
        'idx_chat_messages_tenant_session_sequence_prd066',
        'uq_chat_execution_grants_hash_prd066',
        'uq_chat_execution_grants_tenant_message_prd066',
        'idx_chat_execution_grants_tenant_session_status_prd066',
        'idx_chat_execution_grants_tenant_expiry_prd066'
      ))=13 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_foreign_key_list('chat_sessions'))=13
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('chat_messages'))=5
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('chat_execution_grants'))=17
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd066_up_final_guard;
DROP TABLE prd066_roles_to_expand;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
