-- PRD-062：为配置导入导出 CLI 建立 revision 2 权限目录、不可变批次事实和审计枚举。
-- 安全边界：迁移文件只保存租户、操作者、稳定 ID、规范清单哈希和有界计数；配置正文、Provider
-- Credential、Virtual Key 明文/哈希/前缀、Session Token、Prompt 与 Response 永不进入这些表。
--
-- audit_logs 与 permissions 都含无法原地扩展的 CHECK。SQLite 换表期间使用 legacy_alter_table，
-- 是为了让已在前置门禁中逐项确认的外部 owner trigger 保持原 SQL，不制造短暂的悬空重写；提交前
-- foreign_key_check、integrity_check 和触发器集合会再次复核，并在连接离开迁移前恢复默认 PRAGMA。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前验证 0053 顶部、旧 RBAC 目录和将被换表的核心对象。任何未知后续迁移、
-- 缺列、额外本地 trigger 或损坏投影都必须在业务结构变化前失败关闭。
CREATE TEMP TABLE prd062_up_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count = 0)
);
-- 汇总历史结构、目录与 owner 关系异常；任一计数非零都在持久 DDL 前原子终止升级。
INSERT INTO prd062_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0053' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0053')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','users','roles','permissions','role_permissions',
        'rbac_permission_templates','rbac_role_permission_templates','audit_logs',
        'audit_log_export_sequences'
      ))=9 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'config_imports','config_import_rows','config_exports',
      'rbac_permission_templates_prd062_new','permissions_prd062_new',
      'audit_logs_prd062_new'
    ))
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('rbac_permission_templates'))=6
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=48
      AND (SELECT COUNT(*) FROM rbac_permission_templates
        WHERE catalog_revision=1)=48
      AND (SELECT COUNT(*) FROM rbac_permission_templates
        WHERE permission_code IN ('config.transfer.read','config.transfer.write'))=0
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=125
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*48 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=1)
      =(SELECT COUNT(*) FROM permissions)
      THEN 0 ELSE 1 END
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
  + (SELECT COUNT(*) FROM roles
     WHERE typeof(permission_revision)!='integer' OR permission_revision<1
        OR typeof(matrix_sha256)!='text' OR length(matrix_sha256)!=64
        OR matrix_sha256 GLOB '*[^0-9a-f]*')
  -- Admin 在旧版本不可变，必须仍是完整 revision 1 默认矩阵；否则不能猜测如何补齐。
  + (SELECT COUNT(*) FROM roles role
     WHERE role.code='admin' AND (
       role.permission_revision!=1
       OR role.matrix_sha256!='0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2'
       OR (SELECT COUNT(*) FROM role_permissions relation
           WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id)!=48
     ))
  -- Developer/OpsAdmin 只有 revision/hash 和真实关系同时等于旧默认模板时才可自动扩权。
  + (SELECT COUNT(*) FROM roles role
     WHERE (
       (role.code='developer' AND role.permission_revision=1
        AND role.matrix_sha256='9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e')
       OR
       (role.code='ops_admin' AND role.permission_revision=1
        AND role.matrix_sha256='b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9')
     ) AND (
       EXISTS (
         SELECT 1 FROM rbac_role_permission_templates template
         WHERE template.role_code=role.code AND NOT EXISTS (
           SELECT 1 FROM role_permissions relation
           JOIN permissions permission
             ON permission.tenant_id=relation.tenant_id
            AND permission.permission_id=relation.permission_id
           WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
             AND permission.code=template.permission_code
         )
       )
       OR EXISTS (
         SELECT 1 FROM role_permissions relation
         JOIN permissions permission
           ON permission.tenant_id=relation.tenant_id
          AND permission.permission_id=relation.permission_id
         WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
           AND NOT EXISTS (
             SELECT 1 FROM rbac_role_permission_templates template
             WHERE template.role_code=role.code
               AND template.permission_code=permission.code
           )
       )
     ))
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('audit_logs'))=17
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_index_list('audit_logs') WHERE origin='c')=10
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND tbl_name='audit_logs' AND name IN (
        'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
        'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
        'trg_audit_logs_prd050_rowid_insert','trg_audit_logs_export_sequence_insert_prd058'
      ))=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND tbl_name='audit_logs')=6 THEN 0 ELSE 1 END
  + CASE WHEN instr(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs')),'invitation.accepted')>0
      AND instr(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs')),'config.imported')=0
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd062_up_guard;

-- 冻结本次允许自动补齐的角色。自定义矩阵不进入该临时集合，因此只获得可授予目录项，
-- 不会被迁移静默加入任何实际 RolePermission。
CREATE TEMP TABLE prd062_roles_to_expand AS
SELECT role_id,tenant_id,code,permission_revision,matrix_sha256
FROM roles
WHERE code='admin'
   OR (
     code='developer' AND permission_revision=1
     AND matrix_sha256='9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e'
   )
   OR (
     code='ops_admin' AND permission_revision=1
     AND matrix_sha256='b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9'
   );

-- 产品权限模板的 catalog_revision 是整目录版本，旧 48 项也必须随目录原子提升为 2；
-- 显式复制 rowid 保留历史模板的稳定内部顺序，新两项只保存治理元数据。
CREATE TABLE rbac_permission_templates_prd062_new (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=2)
);
-- 旧 48 项模板保留 rowid 与全部展示元数据，只把产品目录版本提升到 revision 2。
INSERT INTO rbac_permission_templates_prd062_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,2
FROM rbac_permission_templates;
-- 两项配置迁移权限使用独立风险等级，避免从普通资源 CRUD 权限隐式推导高风险导入能力。
INSERT INTO rbac_permission_templates_prd062_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('config.transfer.read','migration','导出配置',
   '读取并导出当前租户的严格白名单配置。','read',2),
  ('config.transfer.write','migration','导入配置',
   '以 create-only 方式批量导入配置并生成全新 Virtual Key。','critical',2);

-- 模板及角色上限均为发布元数据；先移除旧不可变 trigger 完成本次受控演进，再立即恢复。
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TABLE rbac_permission_templates;
-- 原子换表后沿用正式表名，后续外键、初始化触发器和服务查询无需分支兼容。
ALTER TABLE rbac_permission_templates_prd062_new RENAME TO rbac_permission_templates;

-- 仅 Admin、Developer、OpsAdmin 获得默认配置迁移权限，其他角色保持最小授权。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted) VALUES
  ('admin','config.transfer.read',1),
  ('admin','config.transfer.write',1),
  ('developer','config.transfer.read',1),
  ('developer','config.transfer.write',1),
  ('ops_admin','config.transfer.read',1),
  ('ops_admin','config.transfer.write',1);

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

-- 租户 Permission 是 revision 2 模板的逐字段不可变投影。旧 48 项保留 rowid、ID 与时间，
-- 仅目录版本按产品事实提升；新项为每个 tenant 生成独立随机 ID，禁止跨租户复用。
CREATE TABLE permissions_prd062_new (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=2),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 每租户旧 Permission 保留 rowid、稳定 ID 和时间，只同步目录版本字段。
INSERT INTO permissions_prd062_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  2,status,created_at,updated_at
FROM permissions;
-- 从产品模板为每个租户生成两项稳定权限投影，不复制任何跨租户关系。
INSERT INTO permissions_prd062_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT
  'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,
  2,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
CROSS JOIN rbac_permission_templates template
WHERE template.permission_code IN ('config.transfer.read','config.transfer.write');

DROP TABLE permissions;
-- 权限投影换表完成后恢复正式表名，复合 tenant 外键继续作为隔离边界。
ALTER TABLE permissions_prd062_new RENAME TO permissions;
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

-- 迁移关系写入必须覆盖 disabled 但仍合法的默认角色，因此短暂移除 owner trigger；候选集合已由
-- 前置门禁冻结，插入仍受双复合 FK、唯一键和产品角色模板约束，随后恢复运行期 owner 检查。
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
-- 只给精确匹配旧默认模板的三个角色补齐关系，自定义角色矩阵绝不静默扩权。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT
  'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd062_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE permission.code IN ('config.transfer.read','config.transfer.write');
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

-- 只推进真实新增了两项关系的角色矩阵版本。自定义 Developer/OpsAdmin 未进入候选集合，
-- revision/hash 均保持不变，后续必须由管理员通过正式矩阵 API 显式授予。
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
-- 只有迁移自动补权的角色提升 revision 并写入新规范哈希，保持 CAS 与权限快照一致。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
      WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
      WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
    END
WHERE role_id IN (SELECT role_id FROM prd062_roles_to_expand);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
    WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
    WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
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

-- 新租户直接从 revision 2 产品目录生成完整投影；角色自身从矩阵 revision 1 开始，
-- 该数字描述角色变更次数而不是产品 catalog revision，二者不得混用。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户先按产品角色模板创建本租户角色，稳定 ID 含 tenant_id 以避免跨租户碰撞。
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
      WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
      WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
      WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
      WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
      WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
      WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 权限目录逐租户投影，所有行都绑定 NEW.tenant_id，禁止借用其他租户 Permission ID。
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

  -- 默认授权通过同租户角色与权限联接生成，复合外键再次约束 tenant owner。
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

-- 导入批次仅保存规范清单指纹、计数和链路身份。总行数上限与解析器的 50000 节点门禁一致，
-- generated/disabled 计数必须精确分割新建 Virtual Key，避免报告与一次性交付事实分叉。
CREATE TABLE config_imports (
  config_import_id TEXT PRIMARY KEY CHECK (
    typeof(config_import_id)='text' AND length(config_import_id) BETWEEN 1 AND 80
    AND config_import_id=trim(config_import_id)
  ),
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL CHECK (
    typeof(manifest_sha256)='text' AND length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  schema_version TEXT NOT NULL CHECK (schema_version='metahub.config/v1'),
  providers_created_count INTEGER NOT NULL CHECK (
    typeof(providers_created_count)='integer' AND providers_created_count BETWEEN 0 AND 50000
  ),
  providers_unchanged_count INTEGER NOT NULL CHECK (
    typeof(providers_unchanged_count)='integer' AND providers_unchanged_count BETWEEN 0 AND 50000
  ),
  models_created_count INTEGER NOT NULL CHECK (
    typeof(models_created_count)='integer' AND models_created_count BETWEEN 0 AND 50000
  ),
  models_unchanged_count INTEGER NOT NULL CHECK (
    typeof(models_unchanged_count)='integer' AND models_unchanged_count BETWEEN 0 AND 50000
  ),
  aliases_created_count INTEGER NOT NULL CHECK (
    typeof(aliases_created_count)='integer' AND aliases_created_count BETWEEN 0 AND 50000
  ),
  aliases_unchanged_count INTEGER NOT NULL CHECK (
    typeof(aliases_unchanged_count)='integer' AND aliases_unchanged_count BETWEEN 0 AND 50000
  ),
  virtual_keys_created_count INTEGER NOT NULL CHECK (
    typeof(virtual_keys_created_count)='integer' AND virtual_keys_created_count BETWEEN 0 AND 50000
  ),
  virtual_keys_unchanged_count INTEGER NOT NULL CHECK (
    typeof(virtual_keys_unchanged_count)='integer' AND virtual_keys_unchanged_count BETWEEN 0 AND 50000
  ),
  generated_virtual_key_count INTEGER NOT NULL CHECK (
    typeof(generated_virtual_key_count)='integer'
    AND generated_virtual_key_count BETWEEN 0 AND 50000
  ),
  disabled_virtual_key_count INTEGER NOT NULL CHECK (
    typeof(disabled_virtual_key_count)='integer'
    AND disabled_virtual_key_count BETWEEN 0 AND 50000
  ),
  request_id TEXT NOT NULL CHECK (
    typeof(request_id)='text' AND length(request_id) BETWEEN 1 AND 160
    AND request_id=trim(request_id)
  ),
  trace_id TEXT NOT NULL CHECK (
    typeof(trace_id)='text' AND length(trace_id) BETWEEN 1 AND 160
    AND trace_id=trim(trace_id)
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    typeof(event_id)='text' AND length(event_id) BETWEEN 1 AND 80
    AND event_id=trim(event_id)
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND created_at GLOB '????-??-??T??:??:??Z'
    AND substr(created_at,1,4) BETWEEN '0001' AND '9999'
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'+0 seconds') IS NOT NULL
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'+0 seconds')=created_at
  ),
  CHECK (
    providers_created_count+providers_unchanged_count
    +models_created_count+models_unchanged_count
    +aliases_created_count+aliases_unchanged_count
    +virtual_keys_created_count+virtual_keys_unchanged_count <= 50000
  ),
  CHECK (
    generated_virtual_key_count+disabled_virtual_key_count=virtual_keys_created_count
  ),
  UNIQUE (tenant_id,config_import_id),
  UNIQUE (tenant_id,actor_user_id,request_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id)
);
CREATE INDEX idx_config_imports_tenant_created_prd062
  ON config_imports(tenant_id,created_at DESC,config_import_id);
CREATE TRIGGER trg_config_imports_owner_insert_prd062
BEFORE INSERT ON config_imports FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users actor
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
    AND actor.status='active'
)
BEGIN SELECT RAISE(ABORT,'config_import_owner_mismatch'); END;
CREATE TRIGGER trg_config_imports_immutable_update_prd062
BEFORE UPDATE ON config_imports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_import_immutable'); END;
CREATE TRIGGER trg_config_imports_immutable_delete_prd062
BEFORE DELETE ON config_imports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_import_immutable'); END;

-- 行级事实只保留集合、行号、动作、目标 ID 和业务键哈希；不保存名称、URL、description、
-- 输入 JSON 或任何凭据。复合 owner FK 阻断跨租户引用同一批次。
CREATE TABLE config_import_rows (
  tenant_id TEXT NOT NULL,
  config_import_id TEXT NOT NULL,
  collection TEXT NOT NULL CHECK (
    collection IN ('providers','models','aliases','virtual_keys')
  ),
  row_number INTEGER NOT NULL CHECK (
    typeof(row_number)='integer' AND row_number BETWEEN 1 AND 50000
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN ('provider','model','alias','virtual_key')
  ),
  action TEXT NOT NULL CHECK (action IN ('created','unchanged')),
  object_id TEXT NOT NULL CHECK (
    typeof(object_id)='text' AND length(object_id) BETWEEN 1 AND 160
    AND object_id=trim(object_id)
  ),
  business_key_sha256 TEXT NOT NULL CHECK (
    typeof(business_key_sha256)='text' AND length(business_key_sha256)=64
    AND business_key_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  PRIMARY KEY (config_import_id,collection,row_number),
  FOREIGN KEY (tenant_id,config_import_id)
    REFERENCES config_imports(tenant_id,config_import_id)
);
CREATE INDEX idx_config_import_rows_tenant_import_prd062
  ON config_import_rows(tenant_id,config_import_id,collection,row_number);
CREATE INDEX idx_config_import_rows_tenant_object_prd062
  ON config_import_rows(tenant_id,object_type,object_id);
CREATE TRIGGER trg_config_import_rows_owner_insert_prd062
BEFORE INSERT ON config_import_rows FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM config_imports parent
  WHERE parent.tenant_id=NEW.tenant_id
    AND parent.config_import_id=NEW.config_import_id
)
OR NOT (
  (NEW.collection='providers' AND NEW.object_type='provider')
  OR (NEW.collection='models' AND NEW.object_type='model')
  OR (NEW.collection='aliases' AND NEW.object_type='alias')
  OR (NEW.collection='virtual_keys' AND NEW.object_type='virtual_key')
)
OR NEW.row_number > COALESCE((
  SELECT CASE NEW.collection
    WHEN 'providers' THEN parent.providers_created_count+parent.providers_unchanged_count
    WHEN 'models' THEN parent.models_created_count+parent.models_unchanged_count
    WHEN 'aliases' THEN parent.aliases_created_count+parent.aliases_unchanged_count
    WHEN 'virtual_keys' THEN parent.virtual_keys_created_count+parent.virtual_keys_unchanged_count
  END
  FROM config_imports parent
  WHERE parent.tenant_id=NEW.tenant_id
    AND parent.config_import_id=NEW.config_import_id
),0)
BEGIN SELECT RAISE(ABORT,'config_import_row_owner_mismatch'); END;
CREATE TRIGGER trg_config_import_rows_immutable_update_prd062
BEFORE UPDATE ON config_import_rows FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_import_row_immutable'); END;
CREATE TRIGGER trg_config_import_rows_immutable_delete_prd062
BEFORE DELETE ON config_import_rows FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_import_row_immutable'); END;

-- 导出事实不保存 ConfigExport 正文，只保存一致性快照的规范哈希和四类规模；同 actor/request
-- 唯一键让网络重试定位到同一批次，不能重复制造审计事实。
CREATE TABLE config_exports (
  config_export_id TEXT PRIMARY KEY CHECK (
    typeof(config_export_id)='text' AND length(config_export_id) BETWEEN 1 AND 80
    AND config_export_id=trim(config_export_id)
  ),
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  schema_version TEXT NOT NULL CHECK (schema_version='metahub.config/v1'),
  manifest_sha256 TEXT NOT NULL CHECK (
    typeof(manifest_sha256)='text' AND length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  providers_count INTEGER NOT NULL CHECK (
    typeof(providers_count)='integer' AND providers_count BETWEEN 0 AND 50000
  ),
  models_count INTEGER NOT NULL CHECK (
    typeof(models_count)='integer' AND models_count BETWEEN 0 AND 50000
  ),
  aliases_count INTEGER NOT NULL CHECK (
    typeof(aliases_count)='integer' AND aliases_count BETWEEN 0 AND 50000
  ),
  virtual_keys_count INTEGER NOT NULL CHECK (
    typeof(virtual_keys_count)='integer' AND virtual_keys_count BETWEEN 0 AND 50000
  ),
  request_id TEXT NOT NULL CHECK (
    typeof(request_id)='text' AND length(request_id) BETWEEN 1 AND 160
    AND request_id=trim(request_id)
  ),
  trace_id TEXT NOT NULL CHECK (
    typeof(trace_id)='text' AND length(trace_id) BETWEEN 1 AND 160
    AND trace_id=trim(trace_id)
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND created_at GLOB '????-??-??T??:??:??Z'
    AND substr(created_at,1,4) BETWEEN '0001' AND '9999'
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'+0 seconds') IS NOT NULL
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'+0 seconds')=created_at
  ),
  CHECK (providers_count+models_count+aliases_count+virtual_keys_count<=50000),
  UNIQUE (tenant_id,config_export_id),
  UNIQUE (tenant_id,actor_user_id,request_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id)
);
CREATE INDEX idx_config_exports_tenant_created_prd062
  ON config_exports(tenant_id,created_at DESC,config_export_id);
CREATE TRIGGER trg_config_exports_owner_insert_prd062
BEFORE INSERT ON config_exports FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users actor
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
    AND actor.status='active'
)
BEGIN SELECT RAISE(ABORT,'config_export_owner_mismatch'); END;
CREATE TRIGGER trg_config_exports_immutable_update_prd062
BEFORE UPDATE ON config_exports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_export_immutable'); END;
CREATE TRIGGER trg_config_exports_immutable_delete_prd062
BEFORE DELETE ON config_exports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_export_immutable'); END;

-- AuditLog 只扩展动作/对象枚举，17 个持久列、历史 rowid、NULL 语义和全部旧索引保持不变。
-- 配置迁移摘要是扁平白名单计数，不保存导入/导出正文或任何业务名称。
CREATE TABLE audit_logs_prd062_new (
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
      'config.imported','config.exported'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider','provider_key','virtual_key','budget_rule','dlp_rule',
      'webhook_delivery','webhook_endpoint','webhook_dead_letter',
      'usage_export_job','policy','policy_snapshot','policy_explanation',
      'routing_policy','model_quality_score','cache_affinity',
      'finops_adjustment','cost_anomaly','provider_error_map','audit_export',
      'role_permission','invitation','config_transfer'
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
INSERT INTO audit_logs_prd062_new(
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
CREATE TEMP TABLE prd062_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 复制后逐值核对行数与 17 列内容，任何差异都在删除旧审计表前失败关闭。
INSERT INTO prd062_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd062_new))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd062_new WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd062_new WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd062_new WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd062_new
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd062_new
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd062_audit_copy_guard;

-- legacy_alter_table 保持四个已核验的外部 owner trigger 原样指向正式表名；旧表本地
-- trigger/index 随 DROP 移除，重命名后按冻结定义逐项恢复。
DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后替换正式审计表，确保历史治理证据完整保真。
ALTER TABLE audit_logs_prd062_new RENAME TO audit_logs;
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

-- 提交前复核目录、自动扩权、自定义矩阵边界、事实表、审计枚举及全部数据库完整性。
-- 这里的检查属于迁移原子性的一部分，不能由迁移完成后的测试替代。
CREATE TEMP TABLE prd062_up_final_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 提交前统一核对对象数、租户投影、外键与完整性，避免留下部分升级结构。
INSERT INTO prd062_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=50
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=2)=50
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=131
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*50 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=2)
        =(SELECT COUNT(*) FROM permissions)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd062_roles_to_expand source
     JOIN roles role ON role.role_id=source.role_id AND role.tenant_id=source.tenant_id
     WHERE role.permission_revision!=source.permission_revision+1
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
          WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
          WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
        END
        OR (SELECT COUNT(*) FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
              AND permission.code IN ('config.transfer.read','config.transfer.write'))!=2)
  + (SELECT COUNT(*) FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code IN ('config.transfer.read','config.transfer.write')
       AND NOT EXISTS (
         SELECT 1 FROM prd062_roles_to_expand source
         WHERE source.tenant_id=relation.tenant_id AND source.role_id=relation.role_id
       ))
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN ('config_imports','config_import_rows','config_exports'))=3
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_config_%_prd062')=9
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_index_list('audit_logs') WHERE origin='c')=11
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND tbl_name='audit_logs')=6 THEN 0 ELSE 1 END
  + CASE WHEN instr(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs')),'config.imported')>0
      AND instr(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs')),'config_transfer')>0
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_audit_exports_owner_insert_prd058',
        'trg_audit_log_export_sequences_owner_insert_prd058',
        'trg_invitation_events_owner_insert_prd060',
        'trg_role_permission_events_owner_insert_prd059'
      ))=4 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd062_up_final_guard;
DROP TABLE prd062_roles_to_expand;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
