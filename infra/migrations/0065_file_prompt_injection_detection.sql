-- PRD-073：文件上传、文本抽取安全事实与 RBAC revision 9。
-- 文件安全报告只保存固定枚举、计数与分域 HMAC；Prompt、OCR 正文和文件名不得进入事实表。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前证明数据库精确位于 0064 栈顶和 revision 8，禁止跨未知 schema 升级。
DROP TABLE IF EXISTS temp.prd073_up_guard;
CREATE TEMP TABLE prd073_up_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd073_up_guard_ok CHECK (issue_count=0)
);
-- 汇总栈顶、目录、目标对象和 SQLite 完整性，任一漂移都由 CHECK 原子拒绝。
INSERT INTO prd073_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0064' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0064')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=61
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=8)=61
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=162
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*61 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=8)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('uploaded_files'))=23
      AND (SELECT COUNT(*) FROM pragma_table_info('file_extracts'))=15
      AND (SELECT COUNT(*) FROM pragma_table_info('file_dlp_matches'))=9
      AND (SELECT COUNT(*) FROM pragma_table_info('file_events'))=18
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'file_security_scans','file_security_findings','file_security_events',
      'rbac_permission_templates_prd073_new','permissions_prd073_new'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd073_up_guard;

-- 只认领关系全集和 revision 8 hash 都匹配产品模板的角色；租户自定义矩阵不得静默扩权。
CREATE TEMP TABLE prd073_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','viewer','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052'
    WHEN 'developer' THEN 'd1cc4e0cad810c33e57ca2061a2345ef9a7843b26d8af80cb218d4dbf34fae87'
    WHEN 'viewer' THEN '519abd5f24d6c0612727f010fc1eae824b8c05ff0b8a4b0ca32f5ee9c2d3ddb5'
    WHEN 'security_admin' THEN '6b090e14a83776cb75f9633a8cd49033b4b95fc79f18de6c33fbefd1f6f04ec0'
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

-- 迁移期间暂时移除产品模板、owner 和矩阵 CAS trigger，完成投影后以 revision 9 重建。
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

-- 产品目录保留既有 rowid/展示元数据，只提升 revision 并追加文件安全扫描权限。
CREATE TABLE rbac_permission_templates_prd073_new (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=9)
);
-- 既有 61 项保留稳定 rowid 和展示元数据，只将 catalog revision 提升为 9。
INSERT INTO rbac_permission_templates_prd073_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,9
FROM rbac_permission_templates;
-- 新增权限使用固定中文说明和风险级别，不接受运行期动态目录内容。
INSERT INTO rbac_permission_templates_prd073_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('file.security.scan','file','扫描文件 Prompt Injection',
   '对当前租户文件执行隐藏 Prompt Injection 检测并写入不可变安全报告。','write',9);
DROP TABLE rbac_permission_templates;
-- 完整复制并追加权限后恢复正式表名，随后重新启用不可变目录触发器。
ALTER TABLE rbac_permission_templates_prd073_new RENAME TO rbac_permission_templates;
CREATE TRIGGER trg_rbac_permission_templates_insert_prd059
BEFORE INSERT ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059
BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059
BEFORE DELETE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;

-- Admin、Developer、Viewer、SecurityAdmin 获得扫描命令权限；财务与运维角色不默认授予。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','file.security.scan',1),
  ('developer','file.security.scan',1),
  ('viewer','file.security.scan',1),
  ('security_admin','file.security.scan',1);
CREATE TRIGGER trg_rbac_role_templates_insert_prd059
BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059
BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059
BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;

-- 租户 Permission 投影保留旧实体 ID/rowid/时间，并为每个 tenant 物化一项新权限。
CREATE TABLE permissions_prd073_new (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=9),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 每租户既有 Permission 保留 ID、rowid、时间和展示字段，只同步目录 revision。
INSERT INTO permissions_prd073_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       9,status,created_at,updated_at
FROM permissions;
-- 为每个 tenant 从产品模板物化一项新实体，随机 ID 永不跨租户复用。
INSERT INTO permissions_prd073_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       9,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template
  ON template.permission_code='file.security.scan';
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
-- 租户投影复制完成后恢复正式名称，现有 RolePermission 外键继续绑定稳定实体。
ALTER TABLE permissions_prd073_new RENAME TO permissions;
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
FROM prd073_roles_to_expand role
JOIN rbac_role_permission_templates template ON template.role_code=role.code
JOIN permissions permission
  ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code
WHERE permission.code='file.security.scan';
-- 仅将已认领 canonical 角色推进一个 CAS revision 并写入 revision 9 精确 hash。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN 'e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319'
      WHEN 'developer' THEN 'a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
    END
WHERE role_id IN (SELECT role_id FROM prd073_roles_to_expand);

-- 新建角色与新 tenant 必须直接使用 revision 9 精确默认矩阵；运行时授权仍读取数据库关系。
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
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
  -- 新 tenant 先创建六个固定角色，角色 hash 与 revision 9 产品默认矩阵逐项对应。
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
      WHEN 'admin' THEN 'e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319'
      WHEN 'developer' THEN 'a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 角色 owner 建立后物化 62 项同 tenant Permission，防止全局 ID 拼接越权。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后按 166 条产品模板建立角色关系，初始化始终处于同一 tenant 事务。
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

-- FileSecurityScan 只写终态；failed attempt 可重试，passed/blocked 则冻结为同内容同版本的权威结果。
CREATE TABLE file_security_scans (
  scan_id TEXT PRIMARY KEY CHECK (
    length(scan_id)=37 AND scan_id GLOB 'fscn_*'
    AND substr(scan_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  file_extract_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  attempt_no INTEGER NOT NULL CHECK (
    typeof(attempt_no)='integer' AND attempt_no BETWEEN 1 AND 10
  ),
  status TEXT NOT NULL CHECK (status IN ('passed','blocked','failed')),
  risk_level TEXT NOT NULL CHECK (
    risk_level IN ('none','low','medium','high','critical','unknown')
  ),
  risk_score INTEGER CHECK (
    risk_score IS NULL OR (typeof(risk_score)='integer' AND risk_score BETWEEN 0 AND 100)
  ),
  coverage_status TEXT NOT NULL CHECK (
    coverage_status IN ('complete','partial','unavailable')
  ),
  model_access_status TEXT NOT NULL CHECK (model_access_status IN ('allowed','blocked')),
  detector_version TEXT NOT NULL CHECK (detector_version='file-security-detector-v1'),
  ocr_status TEXT NOT NULL CHECK (
    ocr_status IN ('not_required','completed','partial','unavailable','failed')
  ),
  ocr_engine_version TEXT CHECK (
    ocr_engine_version IS NULL OR ocr_engine_version='rapidocr-onnxruntime-1.4.4'
  ),
  scanned_page_count INTEGER NOT NULL CHECK (
    typeof(scanned_page_count)='integer' AND scanned_page_count BETWEEN 0 AND 10000
  ),
  scanned_image_count INTEGER NOT NULL CHECK (
    typeof(scanned_image_count)='integer' AND scanned_image_count BETWEEN 0 AND 10000
  ),
  finding_count INTEGER NOT NULL CHECK (
    typeof(finding_count)='integer' AND finding_count BETWEEN 0 AND 256
  ),
  high_finding_count INTEGER NOT NULL CHECK (
    typeof(high_finding_count)='integer'
    AND high_finding_count BETWEEN 0 AND finding_count
  ),
  boundary_version TEXT NOT NULL CHECK (boundary_version='file-context-boundary-v1'),
  content_fingerprint TEXT NOT NULL CHECK (
    length(content_fingerprint)=76 AND content_fingerprint GLOB 'hmac-sha256:*'
    AND substr(content_fingerprint,13) NOT GLOB '*[^0-9a-f]*'
  ),
  extract_fingerprint TEXT NOT NULL CHECK (
    length(extract_fingerprint)=76 AND extract_fingerprint GLOB 'hmac-sha256:*'
    AND substr(extract_fingerprint,13) NOT GLOB '*[^0-9a-f]*'
  ),
  report_sha256 TEXT NOT NULL CHECK (
    length(report_sha256)=71 AND report_sha256 GLOB 'sha256:*'
    AND substr(report_sha256,8) NOT GLOB '*[^0-9a-f]*'
  ),
  failure_error_code TEXT CHECK (
    failure_error_code IS NULL
    OR failure_error_code IN ('file.security_scan_unavailable','file.integrity_failed')
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT NOT NULL CHECK (unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY (tenant_id,file_id) REFERENCES uploaded_files(tenant_id,file_id),
  FOREIGN KEY (tenant_id,file_extract_id,file_id)
    REFERENCES file_extracts(tenant_id,file_extract_id,file_id),
  FOREIGN KEY (tenant_id,requested_by) REFERENCES users(tenant_id,user_id),
  UNIQUE (tenant_id,scan_id),
  UNIQUE (tenant_id,scan_id,file_id),
  UNIQUE (tenant_id,file_id,detector_version,attempt_no),
  CHECK (unixepoch(updated_at)=unixepoch(created_at)),
  CHECK (unixepoch(completed_at)=unixepoch(created_at)),
  CHECK (
    (status='passed' AND risk_level IN ('none','low','medium') AND risk_score IS NOT NULL
      AND coverage_status='complete' AND model_access_status='allowed'
      AND ocr_status IN ('not_required','completed') AND failure_error_code IS NULL)
    OR (status='blocked' AND risk_score IS NOT NULL AND model_access_status='blocked'
      AND (risk_level IN ('high','critical')
        OR coverage_status IN ('partial','unavailable'))
      AND failure_error_code IS NULL)
    OR (status='failed' AND risk_level='unknown' AND risk_score IS NULL
      AND coverage_status='unavailable' AND model_access_status='blocked'
      AND ocr_status IN ('unavailable','failed') AND finding_count=0
      AND high_finding_count=0 AND failure_error_code IS NOT NULL)
  )
);
CREATE UNIQUE INDEX idx_file_security_scans_authoritative_prd073
  ON file_security_scans(
    tenant_id,file_id,content_fingerprint,extract_fingerprint,detector_version
  ) WHERE status IN ('passed','blocked');
CREATE INDEX idx_file_security_scans_tenant_file_prd073
  ON file_security_scans(tenant_id,file_id,created_at DESC,scan_id DESC);

-- Finding 永不保存命中正文；分域 HMAC 仅用于幂等去重，不能离线还原 Prompt 或 OCR 文本。
CREATE TABLE file_security_findings (
  finding_id TEXT PRIMARY KEY CHECK (
    length(finding_id)=37 AND finding_id GLOB 'fsfd_*'
    AND substr(finding_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  scan_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  detector_code TEXT NOT NULL CHECK (detector_code IN (
    'prompt.visible_instruction','visual.hidden_text','visual.white_text',
    'visual.tiny_text','visual.hidden_layer','ocr.prompt_instruction',
    'ocr.text_observed','container.nested_archive','coverage.incomplete'
  )),
  category TEXT NOT NULL CHECK (category IN (
    'prompt_injection','visual_hiding','hidden_layer','ocr','container','coverage'
  )),
  severity TEXT NOT NULL CHECK (severity IN ('info','low','medium','high','critical')),
  confidence_milli INTEGER NOT NULL CHECK (
    typeof(confidence_milli)='integer' AND confidence_milli BETWEEN 0 AND 1000
  ),
  source_kind TEXT NOT NULL CHECK (source_kind IN (
    'visible_text','docx_xml','docx_image','pdf_content','pdf_image',
    'document_container','scan_coverage'
  )),
  location_kind TEXT NOT NULL CHECK (
    location_kind IN ('document','page','part','image')
  ),
  location_index INTEGER CHECK (
    location_index IS NULL OR (
      typeof(location_index)='integer' AND location_index BETWEEN 1 AND 100000
    )
  ),
  evidence_fingerprint TEXT NOT NULL CHECK (
    length(evidence_fingerprint)=76 AND evidence_fingerprint GLOB 'hmac-sha256:*'
    AND substr(evidence_fingerprint,13) NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK (status='detected'),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY (tenant_id,file_id) REFERENCES uploaded_files(tenant_id,file_id),
  FOREIGN KEY (tenant_id,scan_id,file_id)
    REFERENCES file_security_scans(tenant_id,scan_id,file_id)
    DEFERRABLE INITIALLY DEFERRED,
  UNIQUE (
    tenant_id,scan_id,detector_code,source_kind,location_kind,location_index,
    evidence_fingerprint
  ),
  CHECK (
    (location_kind='document' AND location_index IS NULL)
    OR (location_kind!='document' AND location_index IS NOT NULL)
  ),
  CHECK (
    detector_code!='coverage.incomplete'
    OR (category='coverage' AND severity='high' AND source_kind='scan_coverage')
  ),
  CHECK (unixepoch(updated_at)=unixepoch(created_at))
);
CREATE INDEX idx_file_security_findings_scan_prd073
  ON file_security_findings(tenant_id,scan_id,severity,detector_code,finding_id);

-- SecurityEvent 只保存 Webhook 所需的固定安全摘要，不复制 finding 证据、文件名或报告哈希。
CREATE TABLE file_security_events (
  file_security_event_id TEXT PRIMARY KEY CHECK (
    length(file_security_event_id)=37 AND file_security_event_id GLOB 'fsev_*'
    AND substr(file_security_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  scan_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type='file.prompt_injection_detected'),
  actor_user_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('high','critical')),
  finding_count INTEGER NOT NULL CHECK (
    typeof(finding_count)='integer' AND finding_count BETWEEN 1 AND 256
  ),
  high_finding_count INTEGER NOT NULL CHECK (
    typeof(high_finding_count)='integer'
    AND high_finding_count BETWEEN 1 AND finding_count
  ),
  model_access_status TEXT NOT NULL CHECK (model_access_status='blocked'),
  detector_version TEXT NOT NULL CHECK (detector_version='file-security-detector-v1'),
  occurred_at TEXT NOT NULL CHECK (unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY (tenant_id,scan_id,file_id)
    REFERENCES file_security_scans(tenant_id,scan_id,file_id),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE (tenant_id,file_security_event_id),
  UNIQUE (tenant_id,scan_id,event_type)
);
CREATE INDEX idx_file_security_events_tenant_time_prd073
  ON file_security_events(tenant_id,occurred_at DESC,file_security_event_id DESC);

-- Finding 可以先于 Scan 插入，但必须先证明目标 file/extract 是同 tenant 的 ready owner。
CREATE TRIGGER trg_file_security_findings_owner_insert_prd073
BEFORE INSERT ON file_security_findings FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM uploaded_files file
  JOIN file_extracts extract
    ON extract.tenant_id=file.tenant_id AND extract.file_id=file.file_id
  WHERE file.tenant_id=NEW.tenant_id AND file.file_id=NEW.file_id
    AND file.status='ready' AND extract.status='ready'
)
BEGIN SELECT RAISE(ABORT,'file_security_finding_owner_mismatch'); END;

-- 服务层只收集 256 条仍不足以防止内部误用；数据库对同一次扫描的第 257 条事实再次失败关闭。
CREATE TRIGGER trg_file_security_findings_limit_insert_prd073
BEFORE INSERT ON file_security_findings FOR EACH ROW
WHEN (
  SELECT COUNT(*) FROM file_security_findings finding
  WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
)>=256
BEGIN SELECT RAISE(ABORT,'file_security_finding_limit_exceeded'); END;

-- Scan 插入时反向证明 ready owner、指纹、完整 finding 集合及最高风险，拒绝伪造报告计数。
CREATE TRIGGER trg_file_security_scans_owner_insert_prd073
BEFORE INSERT ON file_security_scans FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM uploaded_files file
  JOIN file_extracts extract
    ON extract.tenant_id=file.tenant_id AND extract.file_id=file.file_id
  JOIN users actor
    ON actor.tenant_id=file.tenant_id AND actor.user_id=NEW.requested_by
  WHERE file.tenant_id=NEW.tenant_id AND file.file_id=NEW.file_id
    AND file.status='ready' AND file.content_fingerprint=NEW.content_fingerprint
    AND extract.file_extract_id=NEW.file_extract_id AND extract.status='ready'
    AND extract.text_fingerprint=NEW.extract_fingerprint
)
OR NEW.finding_count!=(
  SELECT COUNT(*) FROM file_security_findings finding
  WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
    AND finding.file_id=NEW.file_id
)
OR NEW.high_finding_count!=(
  SELECT COUNT(*) FROM file_security_findings finding
  WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
    AND finding.file_id=NEW.file_id AND finding.severity IN ('high','critical')
)
OR EXISTS (
  SELECT 1 FROM file_security_findings finding
  WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
    AND finding.file_id=NEW.file_id
    AND (finding.created_at!=NEW.completed_at OR finding.updated_at!=NEW.completed_at)
)
OR (
  NEW.status='failed' AND (
    SELECT COUNT(*) FROM file_security_findings finding
    WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
  )!=0
)
OR (
  NEW.status!='failed' AND NEW.risk_level!=CASE
    WHEN EXISTS (
      SELECT 1 FROM file_security_findings finding
      WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
        AND finding.severity='critical'
    ) THEN 'critical'
    WHEN EXISTS (
      SELECT 1 FROM file_security_findings finding
      WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
        AND finding.severity='high'
    ) THEN 'high'
    WHEN EXISTS (
      SELECT 1 FROM file_security_findings finding
      WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
        AND finding.severity='medium'
    ) THEN 'medium'
    WHEN EXISTS (
      SELECT 1 FROM file_security_findings finding
      WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
        AND finding.severity IN ('low','info')
    ) THEN 'low'
    ELSE 'none'
  END
)
BEGIN SELECT RAISE(ABORT,'file_security_scan_integrity_failed'); END;

-- Prompt Injection 事件必须逐字段复制 blocked Scan，并证明至少存在一个固定指令类 finding。
CREATE TRIGGER trg_file_security_events_owner_insert_prd073
BEFORE INSERT ON file_security_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM file_security_scans scan
  WHERE scan.tenant_id=NEW.tenant_id AND scan.scan_id=NEW.scan_id
    AND scan.file_id=NEW.file_id AND scan.requested_by=NEW.actor_user_id
    AND scan.source_request_id=NEW.source_request_id
    AND scan.source_trace_id=NEW.source_trace_id
    AND scan.status='blocked' AND scan.risk_level=NEW.risk_level
    AND scan.finding_count=NEW.finding_count
    AND scan.high_finding_count=NEW.high_finding_count
    AND scan.model_access_status=NEW.model_access_status
    AND scan.detector_version=NEW.detector_version
    AND scan.completed_at=NEW.occurred_at
)
OR NOT EXISTS (
  SELECT 1 FROM file_security_findings finding
  WHERE finding.tenant_id=NEW.tenant_id AND finding.scan_id=NEW.scan_id
    AND finding.file_id=NEW.file_id
    AND finding.detector_code IN (
      'prompt.visible_instruction','ocr.prompt_instruction'
    )
)
BEGIN SELECT RAISE(ABORT,'file_security_event_owner_mismatch'); END;

-- 三类安全事实全部 append-only；更新、删除或负 rowid 会破坏报告 hash 和 Webhook 可追踪性。
CREATE TRIGGER trg_file_security_scans_update_prd073
BEFORE UPDATE ON file_security_scans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_scan_immutable'); END;
CREATE TRIGGER trg_file_security_scans_delete_prd073
BEFORE DELETE ON file_security_scans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_scan_immutable'); END;
CREATE TRIGGER trg_file_security_scans_rowid_insert_prd073
AFTER INSERT ON file_security_scans FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'file_security_scan_immutable'); END;
CREATE TRIGGER trg_file_security_findings_update_prd073
BEFORE UPDATE ON file_security_findings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_finding_immutable'); END;
CREATE TRIGGER trg_file_security_findings_delete_prd073
BEFORE DELETE ON file_security_findings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_finding_immutable'); END;
CREATE TRIGGER trg_file_security_findings_rowid_insert_prd073
AFTER INSERT ON file_security_findings FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'file_security_finding_immutable'); END;
CREATE TRIGGER trg_file_security_events_update_prd073
BEFORE UPDATE ON file_security_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_event_immutable'); END;
CREATE TRIGGER trg_file_security_events_delete_prd073
BEFORE DELETE ON file_security_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_event_immutable'); END;
CREATE TRIGGER trg_file_security_events_rowid_insert_prd073
AFTER INSERT ON file_security_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'file_security_event_immutable'); END;

-- 提交前复核 revision 9、三表形状、默认关系、角色哈希与数据库完整性。
DROP TABLE IF EXISTS temp.prd073_up_final_guard;
CREATE TEMP TABLE prd073_up_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd073_up_final_guard_ok CHECK (issue_count=0)
);
-- 把目录、投影、表形状与完整性偏差汇总为单值，任一非零都回滚整项 0065。
INSERT INTO prd073_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=62
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=9)=62
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=166
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*62 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=9)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('file_security_scans'))=28
      AND (SELECT COUNT(*) FROM pragma_table_info('file_security_findings'))=15
      AND (SELECT COUNT(*) FROM pragma_table_info('file_security_events'))=14
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates
      WHERE permission_code='file.security.scan' AND default_granted=1)=4
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd073_up_final_guard;
COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
