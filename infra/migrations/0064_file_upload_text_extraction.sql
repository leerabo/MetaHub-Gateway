-- PRD-072：文件上传、文本抽取安全事实与 RBAC revision 8。
-- 原文件、抽取正文和原始文件名只保存认证密文或内部对象引用；所有业务关系均绑定 tenant。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前证明数据库精确位于 0063 栈顶和 revision 7，禁止跨未知 schema 升级。
DROP TABLE IF EXISTS temp.prd072_up_guard;
CREATE TEMP TABLE prd072_up_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd072_up_guard_ok CHECK (issue_count=0)
);
-- 汇总栈顶、目录、目标对象和 SQLite 完整性，任一漂移都由 CHECK 原子拒绝。
INSERT INTO prd072_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0063' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0063')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=59
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=7)=59
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=155
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*59 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=7)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'uploaded_files','file_extracts','file_dlp_matches','file_events',
      'rbac_permission_templates_prd072_new','permissions_prd072_new'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd072_up_guard;

-- 只认领关系全集和 revision 7 hash 都匹配产品模板的角色；租户自定义矩阵不得静默扩权。
CREATE TEMP TABLE prd072_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','viewer','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '792e003e6c0a188c124ef71ce7ab31658e7ef43a6dadaf6b83c339dfff2caf7f'
    WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
    WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
    WHEN 'security_admin' THEN 'de4701bc3ce4305c129704c69212d8a520ea5942a87c6119a36542e01784b78c'
  END
  AND NOT EXISTS (
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
  AND NOT EXISTS (
    SELECT 1 FROM role_permissions relation
    JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS (
        SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code
      )
  );

-- 迁移期间暂时移除产品模板、owner 和矩阵 CAS trigger，完成投影后以 revision 8 重建。
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

-- 产品目录保留既有 rowid/展示元数据，只提升 revision 并追加两个文件权限。
CREATE TABLE rbac_permission_templates_prd072_new (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=8)
);
-- 既有 59 项保留稳定 rowid 和展示元数据，只将 catalog revision 提升为 8。
INSERT INTO rbac_permission_templates_prd072_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,8
FROM rbac_permission_templates;
-- 两个新增权限使用固定中文说明和风险级别，不接受运行期动态目录内容。
INSERT INTO rbac_permission_templates_prd072_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('file.read','file','读取文件','读取当前租户文件处理状态、抽取文本和安全摘要。','read',8),
  ('file.write','file','上传文件','上传并解析当前租户的受支持文件。','write',8);
DROP TABLE rbac_permission_templates;
-- 完整复制并追加权限后恢复正式表名，随后重新启用不可变目录触发器。
ALTER TABLE rbac_permission_templates_prd072_new RENAME TO rbac_permission_templates;
CREATE TRIGGER trg_rbac_permission_templates_insert_prd059
BEFORE INSERT ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059
BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059
BEFORE DELETE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;

-- Admin/Developer/Viewer 获得读写，SecurityAdmin 只读；财务与运维角色不默认授予。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','file.read',1),('admin','file.write',1),
  ('developer','file.read',1),('developer','file.write',1),
  ('viewer','file.read',1),('viewer','file.write',1),
  ('security_admin','file.read',1);
CREATE TRIGGER trg_rbac_role_templates_insert_prd059
BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059
BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059
BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;

-- 租户 Permission 投影保留旧实体 ID/rowid/时间，并为每个 tenant 物化两项新权限。
CREATE TABLE permissions_prd072_new (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=8),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 每租户既有 Permission 保留 ID、rowid、时间和展示字段，只同步目录 revision。
INSERT INTO permissions_prd072_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       8,status,created_at,updated_at
FROM permissions;
-- 为每个 tenant 从产品模板物化两项新实体，随机 ID 永不跨租户复用。
INSERT INTO permissions_prd072_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       8,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template
  ON template.permission_code IN ('file.read','file.write');
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
-- 租户投影复制完成后恢复正式名称，现有 RolePermission 外键继续绑定稳定实体。
ALTER TABLE permissions_prd072_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level AND template.catalog_revision=NEW.catalog_revision
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

-- 只为升级前精确 canonical 的四类角色增加新关系，并将角色 CAS 推进一次。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd072_roles_to_expand role
JOIN rbac_role_permission_templates template ON template.role_code=role.code
JOIN permissions permission
  ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code
WHERE permission.code IN ('file.read','file.write');
-- 仅将已认领 canonical 角色推进一个 CAS revision 并写入 revision 8 精确 hash。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052'
      WHEN 'developer' THEN 'd1cc4e0cad810c33e57ca2061a2345ef9a7843b26d8af80cb218d4dbf34fae87'
      WHEN 'viewer' THEN '519abd5f24d6c0612727f010fc1eae824b8c05ff0b8a4b0ca32f5ee9c2d3ddb5'
      WHEN 'security_admin' THEN '6b090e14a83776cb75f9633a8cd49033b4b95fc79f18de6c33fbefd1f6f04ec0'
    END
WHERE role_id IN (SELECT role_id FROM prd072_roles_to_expand);

-- 新建角色与新 tenant 必须直接使用 revision 8 精确默认矩阵；运行时授权仍读取数据库关系。
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052'
    WHEN 'developer' THEN 'd1cc4e0cad810c33e57ca2061a2345ef9a7843b26d8af80cb218d4dbf34fae87'
    WHEN 'viewer' THEN '519abd5f24d6c0612727f010fc1eae824b8c05ff0b8a4b0ca32f5ee9c2d3ddb5'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN '6b090e14a83776cb75f9633a8cd49033b4b95fc79f18de6c33fbefd1f6f04ec0'
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
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template
    ON template.role_code=role.code AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id AND role.status='active'
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
  -- 新 tenant 先创建六个固定角色，角色 hash 与 revision 8 产品默认矩阵逐项对应。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格和成本异常。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型和路由运维。' END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052'
      WHEN 'developer' THEN 'd1cc4e0cad810c33e57ca2061a2345ef9a7843b26d8af80cb218d4dbf34fae87'
      WHEN 'viewer' THEN '519abd5f24d6c0612727f010fc1eae824b8c05ff0b8a4b0ca32f5ee9c2d3ddb5'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN '6b090e14a83776cb75f9633a8cd49033b4b95fc79f18de6c33fbefd1f6f04ec0'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 角色 owner 建立后物化 61 项同 tenant Permission，防止全局 ID 拼接越权。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后按 162 条产品模板建立角色关系，初始化始终处于同一 tenant 事务。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  )
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission
    ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

-- 复合唯一索引为用户和 DLP 规则提供 tenant-qualified 外键父键。
CREATE UNIQUE INDEX idx_users_tenant_user_prd072 ON users(tenant_id,user_id);
CREATE UNIQUE INDEX idx_dlp_rules_tenant_rule_prd072 ON dlp_rules(tenant_id,dlp_rule_id);

-- UploadedFile 只保存加密文件名、keyed fingerprint、安全状态和内部对象引用。
CREATE TABLE uploaded_files (
  file_id TEXT PRIMARY KEY CHECK (
    length(file_id)=37 AND file_id GLOB 'file_*'
    AND substr(file_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  original_name_ciphertext BLOB NOT NULL CHECK (
    typeof(original_name_ciphertext)='blob' AND length(original_name_ciphertext) BETWEEN 32 AND 4096
  ),
  file_extension TEXT NOT NULL CHECK (file_extension IN ('txt','docx','pdf')),
  media_type TEXT NOT NULL CHECK (media_type IN (
    'text/plain','application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  )),
  size_bytes INTEGER NOT NULL CHECK (
    typeof(size_bytes)='integer' AND size_bytes BETWEEN 1 AND 52428800
  ),
  content_fingerprint TEXT NOT NULL CHECK (
    length(content_fingerprint)=76 AND content_fingerprint GLOB 'hmac-sha256:*'
    AND substr(content_fingerprint,13) NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('processing','ready','blocked','failed')),
  virus_scan_status TEXT NOT NULL CHECK (virus_scan_status='not_configured'),
  dlp_scan_status TEXT NOT NULL CHECK (
    dlp_scan_status IN ('pending','clean','matched','blocked','unavailable')
  ),
  dlp_action TEXT CHECK (dlp_action IS NULL OR dlp_action IN ('allow','log_only','block')),
  effective_dlp_rule_id TEXT,
  match_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(match_count)='integer' AND match_count BETWEEN 0 AND 100
  ),
  ruleset_fingerprint TEXT CHECK (
    ruleset_fingerprint IS NULL OR (
      length(ruleset_fingerprint)=71 AND ruleset_fingerprint GLOB 'sha256:*'
      AND substr(ruleset_fingerprint,8) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  storage_backend TEXT CHECK (storage_backend IS NULL OR storage_backend='local_encrypted_v1'),
  original_object_key TEXT,
  failure_error_code TEXT CHECK (
    failure_error_code IS NULL OR failure_error_code IN (
      'file.structure_invalid','file.no_extractable_text','file.extract_limit_exceeded',
      'file.parse_failed','file.dlp_blocked','file.dlp_unavailable',
      'file.storage_unavailable','file.integrity_failed'
    )
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK (completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,uploaded_by) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,effective_dlp_rule_id) REFERENCES dlp_rules(tenant_id,dlp_rule_id),
  UNIQUE (tenant_id,file_id),
  CHECK (
    (file_extension='txt' AND media_type='text/plain')
    OR (file_extension='pdf' AND media_type='application/pdf')
    OR (file_extension='docx'
      AND media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
  ),
  CHECK (unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK (completed_at IS NULL OR unixepoch(completed_at)>=unixepoch(created_at)),
  CHECK (
    (dlp_scan_status='pending' AND dlp_action IS NULL
      AND effective_dlp_rule_id IS NULL AND match_count=0 AND ruleset_fingerprint IS NULL)
    OR (dlp_scan_status='clean' AND dlp_action IS NULL
      AND effective_dlp_rule_id IS NULL AND match_count=0 AND ruleset_fingerprint IS NOT NULL)
    OR (dlp_scan_status='matched' AND dlp_action IN ('allow','log_only')
      AND effective_dlp_rule_id IS NOT NULL AND match_count>=1 AND ruleset_fingerprint IS NOT NULL)
    OR (dlp_scan_status='blocked' AND dlp_action='block'
      AND effective_dlp_rule_id IS NOT NULL AND match_count>=1 AND ruleset_fingerprint IS NOT NULL)
    OR (dlp_scan_status='unavailable' AND dlp_action IS NULL
      AND effective_dlp_rule_id IS NULL AND match_count=0)
  ),
  CHECK (
    (status='processing' AND dlp_scan_status='pending' AND storage_backend IS NULL
      AND original_object_key IS NULL AND failure_error_code IS NULL AND completed_at IS NULL)
    OR (status='ready' AND dlp_scan_status IN ('clean','matched')
      AND storage_backend='local_encrypted_v1' AND original_object_key IS NOT NULL
      AND failure_error_code IS NULL AND completed_at IS NOT NULL)
    OR (status='blocked' AND dlp_scan_status='blocked' AND storage_backend IS NULL
      AND original_object_key IS NULL AND failure_error_code='file.dlp_blocked'
      AND completed_at IS NOT NULL)
    OR (status='failed' AND dlp_scan_status!='blocked' AND storage_backend IS NULL
      AND original_object_key IS NULL AND failure_error_code IS NOT NULL
      AND failure_error_code!='file.dlp_blocked' AND completed_at IS NOT NULL)
  )
);
CREATE INDEX idx_uploaded_files_tenant_request_prd072
  ON uploaded_files(tenant_id,source_request_id);
CREATE INDEX idx_uploaded_files_tenant_created_prd072
  ON uploaded_files(tenant_id,created_at DESC,file_id DESC);
CREATE INDEX idx_uploaded_files_tenant_status_prd072
  ON uploaded_files(tenant_id,status,created_at DESC);

-- FileExtract 一对一绑定 UploadedFile；只有 ready 保留抽取正文密文对象。
CREATE TABLE file_extracts (
  file_extract_id TEXT PRIMARY KEY CHECK (
    length(file_extract_id)=37 AND file_extract_id GLOB 'fext_*'
    AND substr(file_extract_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ready','blocked','failed')),
  extractor_version TEXT NOT NULL CHECK (extractor_version IN ('txt-v1','docx-v1','pdf-v1')),
  text_size_bytes INTEGER CHECK (
    text_size_bytes IS NULL OR (
      typeof(text_size_bytes)='integer' AND text_size_bytes BETWEEN 1 AND 8388608
    )
  ),
  text_code_points INTEGER CHECK (
    text_code_points IS NULL OR (
      typeof(text_code_points)='integer' AND text_code_points BETWEEN 1 AND 4000000
    )
  ),
  estimated_tokens INTEGER CHECK (
    estimated_tokens IS NULL OR (typeof(estimated_tokens)='integer' AND estimated_tokens>=1)
  ),
  estimator_version TEXT CHECK (
    estimator_version IS NULL OR estimator_version='file-token-estimator-v1'
  ),
  text_fingerprint TEXT CHECK (
    text_fingerprint IS NULL OR (
      length(text_fingerprint)=76 AND text_fingerprint GLOB 'hmac-sha256:*'
      AND substr(text_fingerprint,13) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  storage_backend TEXT CHECK (storage_backend IS NULL OR storage_backend='local_encrypted_v1'),
  extracted_object_key TEXT,
  failure_error_code TEXT CHECK (
    failure_error_code IS NULL OR failure_error_code IN (
      'file.structure_invalid','file.no_extractable_text','file.extract_limit_exceeded',
      'file.parse_failed','file.dlp_blocked','file.dlp_unavailable',
      'file.storage_unavailable','file.integrity_failed'
    )
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (
    unixepoch(updated_at) IS NOT NULL AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  FOREIGN KEY (tenant_id,file_id) REFERENCES uploaded_files(tenant_id,file_id),
  UNIQUE (tenant_id,file_id),
  UNIQUE (tenant_id,file_extract_id),
  UNIQUE (tenant_id,file_extract_id,file_id),
  CHECK (
    (text_size_bytes IS NULL AND text_code_points IS NULL AND estimated_tokens IS NULL
      AND estimator_version IS NULL AND text_fingerprint IS NULL)
    OR (text_size_bytes IS NOT NULL AND text_code_points IS NOT NULL
      AND estimated_tokens IS NOT NULL AND estimator_version='file-token-estimator-v1')
  ),
  CHECK (
    (status='ready' AND text_fingerprint IS NOT NULL
      AND storage_backend='local_encrypted_v1' AND extracted_object_key IS NOT NULL
      AND failure_error_code IS NULL)
    OR (status='blocked' AND text_fingerprint IS NOT NULL AND storage_backend IS NULL
      AND extracted_object_key IS NULL AND failure_error_code='file.dlp_blocked')
    OR (status='failed' AND storage_backend IS NULL AND extracted_object_key IS NULL
      AND failure_error_code IS NOT NULL AND failure_error_code!='file.dlp_blocked')
  )
);
CREATE INDEX idx_file_extracts_tenant_status_prd072
  ON file_extracts(tenant_id,status,updated_at DESC);

-- 每个 file/rule 只保存一条不含位置、正文或 hash 的安全命中摘要。
CREATE TABLE file_dlp_matches (
  file_dlp_match_id TEXT PRIMARY KEY CHECK (
    length(file_dlp_match_id)=37 AND file_dlp_match_id GLOB 'fdlm_*'
    AND substr(file_dlp_match_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  file_extract_id TEXT NOT NULL,
  dlp_rule_id TEXT NOT NULL,
  rule_type TEXT NOT NULL CHECK (rule_type IN ('keyword','regex')),
  action TEXT NOT NULL CHECK (action IN ('allow','log_only','block')),
  ruleset_fingerprint TEXT NOT NULL CHECK (
    length(ruleset_fingerprint)=71 AND ruleset_fingerprint GLOB 'sha256:*'
    AND substr(ruleset_fingerprint,8) NOT GLOB '*[^0-9a-f]*'
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY (tenant_id,file_id) REFERENCES uploaded_files(tenant_id,file_id),
  FOREIGN KEY (tenant_id,file_extract_id,file_id)
    REFERENCES file_extracts(tenant_id,file_extract_id,file_id),
  FOREIGN KEY (tenant_id,dlp_rule_id) REFERENCES dlp_rules(tenant_id,dlp_rule_id),
  UNIQUE (tenant_id,file_dlp_match_id),
  UNIQUE (tenant_id,file_id,dlp_rule_id)
);
CREATE INDEX idx_file_dlp_matches_tenant_file_prd072
  ON file_dlp_matches(tenant_id,file_id,created_at);

-- Source event 只冻结 Webhook 可公开的安全摘要；error_code 在成功事件中必须为 NULL。
CREATE TABLE file_events (
  file_event_id TEXT PRIMARY KEY CHECK (
    length(file_event_id)=37 AND file_event_id GLOB 'fevt_*'
    AND substr(file_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('file.uploaded','file.scanned')),
  actor_user_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  file_status TEXT NOT NULL CHECK (file_status IN ('processing','ready','blocked','failed')),
  media_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (
    typeof(size_bytes)='integer' AND size_bytes BETWEEN 1 AND 52428800
  ),
  virus_scan_status TEXT NOT NULL CHECK (virus_scan_status='not_configured'),
  dlp_scan_status TEXT NOT NULL CHECK (
    dlp_scan_status IN ('pending','clean','matched','blocked','unavailable')
  ),
  dlp_action TEXT CHECK (dlp_action IS NULL OR dlp_action IN ('allow','log_only','block')),
  match_count INTEGER NOT NULL CHECK (
    typeof(match_count)='integer' AND match_count BETWEEN 0 AND 100
  ),
  estimated_tokens INTEGER CHECK (
    estimated_tokens IS NULL OR (typeof(estimated_tokens)='integer' AND estimated_tokens>=1)
  ),
  extractor_version TEXT CHECK (
    extractor_version IS NULL OR extractor_version IN ('txt-v1','docx-v1','pdf-v1')
  ),
  error_code TEXT CHECK (
    error_code IS NULL OR error_code IN (
      'file.structure_invalid','file.no_extractable_text','file.extract_limit_exceeded',
      'file.parse_failed','file.dlp_blocked','file.dlp_unavailable',
      'file.storage_unavailable','file.integrity_failed'
    )
  ),
  occurred_at TEXT NOT NULL CHECK (unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY (tenant_id,file_id) REFERENCES uploaded_files(tenant_id,file_id),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE (tenant_id,file_event_id),
  UNIQUE (tenant_id,file_id,event_type),
  CHECK (
    (event_type='file.uploaded' AND file_status='processing' AND dlp_scan_status='pending'
      AND dlp_action IS NULL AND match_count=0 AND estimated_tokens IS NULL
      AND extractor_version IS NULL AND error_code IS NULL)
    OR (event_type='file.scanned' AND file_status IN ('ready','blocked','failed')
      AND extractor_version IS NOT NULL
      AND ((file_status='ready' AND estimated_tokens IS NOT NULL AND error_code IS NULL)
        OR (file_status='blocked' AND estimated_tokens IS NOT NULL
          AND error_code='file.dlp_blocked')
        OR (file_status='failed' AND error_code IS NOT NULL
          AND error_code!='file.dlp_blocked')))
  )
);
CREATE INDEX idx_file_events_tenant_time_prd072
  ON file_events(tenant_id,occurred_at DESC,file_event_id DESC);

-- UploadedFile 初始 insert 必须绑定同 tenant user，并严格从 processing 状态进入状态机。
CREATE TRIGGER trg_uploaded_files_owner_insert_prd072
BEFORE INSERT ON uploaded_files FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.tenant_id=NEW.tenant_id AND user.user_id=NEW.uploaded_by
)
  OR EXISTS (
    SELECT 1 FROM uploaded_files current
    WHERE current.file_id=NEW.file_id
       OR (current.tenant_id=NEW.tenant_id AND current.file_id=NEW.file_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
  OR NEW.status!='processing'
BEGIN SELECT RAISE(ABORT,'file_owner_mismatch'); END;

-- 只允许 processing 单向进入终态；终态 UPDATE 必须逐字段幂等，禁止重开或改写证据。
CREATE TRIGGER trg_uploaded_files_state_update_prd072
BEFORE UPDATE ON uploaded_files FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.file_id!=OLD.file_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.uploaded_by!=OLD.uploaded_by OR NEW.source_request_id!=OLD.source_request_id
  OR NEW.source_trace_id!=OLD.source_trace_id
  OR NEW.original_name_ciphertext IS NOT OLD.original_name_ciphertext
  OR NEW.file_extension!=OLD.file_extension OR NEW.media_type!=OLD.media_type
  OR NEW.size_bytes!=OLD.size_bytes OR NEW.content_fingerprint!=OLD.content_fingerprint
  OR NEW.virus_scan_status!=OLD.virus_scan_status OR NEW.created_at!=OLD.created_at
  OR (OLD.status='processing' AND NEW.status NOT IN ('ready','blocked','failed'))
  OR (OLD.status!='processing' AND (
    NEW.status!=OLD.status OR NEW.dlp_scan_status!=OLD.dlp_scan_status
    OR NEW.dlp_action IS NOT OLD.dlp_action
    OR NEW.effective_dlp_rule_id IS NOT OLD.effective_dlp_rule_id
    OR NEW.match_count!=OLD.match_count
    OR NEW.ruleset_fingerprint IS NOT OLD.ruleset_fingerprint
    OR NEW.storage_backend IS NOT OLD.storage_backend
    OR NEW.original_object_key IS NOT OLD.original_object_key
    OR NEW.failure_error_code IS NOT OLD.failure_error_code
    OR NEW.updated_at!=OLD.updated_at OR NEW.completed_at IS NOT OLD.completed_at
  ))
BEGIN SELECT RAISE(ABORT,'file_state_immutable'); END;
CREATE TRIGGER trg_uploaded_files_delete_prd072
BEFORE DELETE ON uploaded_files FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_immutable'); END;
CREATE TRIGGER trg_uploaded_files_rowid_insert_prd072
AFTER INSERT ON uploaded_files FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'file_immutable'); END;

-- Extract 必须与同 tenant 终态 file、格式、状态和 failure code 逐项一致。
CREATE TRIGGER trg_file_extracts_owner_insert_prd072
BEFORE INSERT ON file_extracts FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM uploaded_files file
  WHERE file.tenant_id=NEW.tenant_id AND file.file_id=NEW.file_id
    AND file.status=NEW.status
    AND NEW.extractor_version=CASE file.file_extension
      WHEN 'txt' THEN 'txt-v1' WHEN 'docx' THEN 'docx-v1' WHEN 'pdf' THEN 'pdf-v1' END
    AND NEW.failure_error_code IS file.failure_error_code
)
  OR EXISTS (
    SELECT 1 FROM file_extracts current
    WHERE current.file_extract_id=NEW.file_extract_id
       OR (current.tenant_id=NEW.tenant_id AND current.file_id=NEW.file_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'file_extract_owner_mismatch'); END;
CREATE TRIGGER trg_file_extracts_update_prd072
BEFORE UPDATE ON file_extracts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_extract_immutable'); END;
CREATE TRIGGER trg_file_extracts_delete_prd072
BEFORE DELETE ON file_extracts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_extract_immutable'); END;
CREATE TRIGGER trg_file_extracts_rowid_insert_prd072
AFTER INSERT ON file_extracts FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'file_extract_immutable'); END;

-- DLP 命中必须同时绑定同 tenant file/extract/rule，并复制规则类型、动作和本次 ruleset 摘要。
CREATE TRIGGER trg_file_dlp_matches_owner_insert_prd072
BEFORE INSERT ON file_dlp_matches FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM uploaded_files file
  JOIN file_extracts extract
    ON extract.tenant_id=file.tenant_id AND extract.file_id=file.file_id
  JOIN dlp_rules rule
    ON rule.tenant_id=file.tenant_id AND rule.dlp_rule_id=NEW.dlp_rule_id
  WHERE file.tenant_id=NEW.tenant_id AND file.file_id=NEW.file_id
    AND extract.file_extract_id=NEW.file_extract_id
    AND file.status IN ('ready','blocked') AND extract.status=file.status
    AND rule.rule_type=NEW.rule_type AND rule.action=NEW.action
    AND file.ruleset_fingerprint=NEW.ruleset_fingerprint
)
  OR EXISTS (
    SELECT 1 FROM file_dlp_matches current
    WHERE current.file_dlp_match_id=NEW.file_dlp_match_id
       OR (current.tenant_id=NEW.tenant_id AND current.file_id=NEW.file_id
           AND current.dlp_rule_id=NEW.dlp_rule_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'file_dlp_owner_mismatch'); END;
CREATE TRIGGER trg_file_dlp_matches_update_prd072
BEFORE UPDATE ON file_dlp_matches FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_dlp_match_immutable'); END;
CREATE TRIGGER trg_file_dlp_matches_delete_prd072
BEFORE DELETE ON file_dlp_matches FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_dlp_match_immutable'); END;
CREATE TRIGGER trg_file_dlp_matches_rowid_insert_prd072
AFTER INSERT ON file_dlp_matches FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'file_dlp_match_immutable'); END;

-- uploaded/scanned source event 必须逐字段反向证明 owner；事件永远 append-only。
CREATE TRIGGER trg_file_events_owner_insert_prd072
BEFORE INSERT ON file_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM uploaded_files file
  WHERE file.tenant_id=NEW.tenant_id AND file.file_id=NEW.file_id
    AND file.uploaded_by=NEW.actor_user_id
    AND file.source_request_id=NEW.source_request_id
    AND file.source_trace_id=NEW.source_trace_id
    AND file.status=NEW.file_status AND file.media_type=NEW.media_type
    AND file.size_bytes=NEW.size_bytes AND file.virus_scan_status=NEW.virus_scan_status
    AND file.dlp_scan_status=NEW.dlp_scan_status
    AND file.dlp_action IS NEW.dlp_action AND file.match_count=NEW.match_count
    AND file.failure_error_code IS NEW.error_code
    AND ((NEW.event_type='file.uploaded' AND file.created_at=NEW.occurred_at)
      OR (NEW.event_type='file.scanned' AND file.completed_at=NEW.occurred_at
        AND EXISTS (
          SELECT 1 FROM file_extracts extract
          WHERE extract.tenant_id=file.tenant_id AND extract.file_id=file.file_id
            AND extract.status=file.status
            AND extract.estimated_tokens IS NEW.estimated_tokens
            AND extract.extractor_version=NEW.extractor_version
            AND extract.failure_error_code IS NEW.error_code
        )))
)
  OR EXISTS (
    SELECT 1 FROM file_events current
    WHERE current.file_event_id=NEW.file_event_id
       OR (current.tenant_id=NEW.tenant_id AND current.file_id=NEW.file_id
           AND current.event_type=NEW.event_type)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'file_event_owner_mismatch'); END;
CREATE TRIGGER trg_file_events_update_prd072
BEFORE UPDATE ON file_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_event_immutable'); END;
CREATE TRIGGER trg_file_events_delete_prd072
BEFORE DELETE ON file_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_event_immutable'); END;
CREATE TRIGGER trg_file_events_rowid_insert_prd072
AFTER INSERT ON file_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'file_event_immutable'); END;

-- 提交前精确复核 revision 8、四表形状、角色 hash 与数据库完整性。
DROP TABLE IF EXISTS temp.prd072_final_guard;
CREATE TEMP TABLE prd072_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd072_final_guard_ok CHECK (issue_count=0)
);
-- 提交前再次聚合目录、投影、四表形状和 SQLite 完整性，拒绝半完成迁移。
INSERT INTO prd072_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=61
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=8)=61
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=162
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*61 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=8)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM roles role WHERE role.code='admin'
     AND role.matrix_sha256!='6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052')
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('uploaded_files'))=23
      AND (SELECT COUNT(*) FROM pragma_table_info('file_extracts'))=15
      AND (SELECT COUNT(*) FROM pragma_table_info('file_dlp_matches'))=9
      AND (SELECT COUNT(*) FROM pragma_table_info('file_events'))=18
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd072_final_guard;
DROP TABLE prd072_roles_to_expand;
COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
