-- PRD-073 回滚：仅在没有安全扫描事实、Webhook 证据、自定义持权和后续依赖时恢复 0064。
-- 所有失败关闭门禁均位于首项持久 DDL 前，绝不通过删除安全证据制造“可降级”。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 只认领关系全集和 hash 都精确匹配 revision 9 产品模板的四类默认角色。
CREATE TEMP TABLE prd073_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','viewer','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319'
    WHEN 'developer' THEN 'a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
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

-- 业务门禁聚合不可逆扫描、Webhook、授权历史和自定义持有者，首项持久 DDL 前原子失败。
DROP TABLE IF EXISTS temp.prd073_down_business_guard;
DROP TRIGGER IF EXISTS temp.trg_prd073_down_business_guard;
CREATE TEMP TABLE prd073_down_business_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd073_down_business_guard
BEFORE INSERT ON prd073_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd073_down_business_facts_present'); END;
-- 安全事实和下游 Webhook 事实都不可在降级中静默丢失，统一计数后由 trigger 拒绝。
INSERT INTO prd073_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM file_security_events)
  + (SELECT COUNT(*) FROM file_security_findings)
  + (SELECT COUNT(*) FROM file_security_scans)
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE (json_valid(endpoint.event_types_json) AND EXISTS (
       SELECT 1 FROM json_each(endpoint.event_types_json) item
       WHERE item.value='file.prompt_injection_detected'
     )) OR lower(endpoint.event_types_json) LIKE '%file.prompt_injection_detected%')
  + (SELECT COUNT(*) FROM webhook_deliveries
     WHERE event_type='file.prompt_injection_detected')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='file.prompt_injection_detected')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
     WHERE event_type='file.prompt_injection_detected')
  + (SELECT COUNT(*) FROM role_permission_events event
     WHERE (json_valid(event.added_permissions_json) AND EXISTS (
       SELECT 1 FROM json_each(event.added_permissions_json) item
       WHERE item.value='file.security.scan'
     )) OR (json_valid(event.removed_permissions_json) AND EXISTS (
       SELECT 1 FROM json_each(event.removed_permissions_json) item
       WHERE item.value='file.security.scan'
     )) OR lower(event.added_permissions_json) LIKE '%file.security.scan%'
       OR lower(event.removed_permissions_json) LIKE '%file.security.scan%')
  + (SELECT COUNT(*) FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code='file.security.scan'
       AND NOT EXISTS (
         SELECT 1 FROM prd073_roles_to_contract source
         WHERE source.tenant_id=relation.tenant_id AND source.role_id=relation.role_id
       ));
DROP TRIGGER trg_prd073_down_business_guard;
DROP TABLE prd073_down_business_guard;

-- 结构门禁冻结 0065 栈顶、三张新表、revision 9 与所有未知后续对象引用。
DROP TABLE IF EXISTS temp.prd073_down_guard;
CREATE TEMP TABLE prd073_down_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd073_down_guard_ok CHECK (issue_count=0)
);
-- 降级首项持久 DDL 前复核 revision、对象形状和完整性，防止从未知 schema 强行回退。
INSERT INTO prd073_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0065' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0065')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=62
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=9)=62
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=166
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*62 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=9)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM roles role WHERE role.code='admin' AND NOT EXISTS (
      SELECT 1 FROM prd073_roles_to_contract source
      WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
    ))
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('uploaded_files'))=23
      AND (SELECT COUNT(*) FROM pragma_table_info('file_extracts'))=15
      AND (SELECT COUNT(*) FROM pragma_table_info('file_security_scans'))=28
      AND (SELECT COUNT(*) FROM pragma_table_info('file_security_findings'))=15
      AND (SELECT COUNT(*) FROM pragma_table_info('file_security_events'))=14
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (instr(lower(object.sql),'file_security_scans')>0
         OR instr(lower(object.sql),'file_security_findings')>0
         OR instr(lower(object.sql),'file_security_events')>0)
       AND object.tbl_name NOT IN (
         'file_security_scans','file_security_findings','file_security_events'
       )
       AND object.name NOT IN (
         'file_security_scans','file_security_findings','file_security_events'
       ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd073_down_guard;

-- 首项持久 DDL：业务门禁已证明三表为空，按 child-to-parent 顺序移除 PRD-073 对象。
DROP TABLE file_security_events;
DROP TABLE file_security_findings;
DROP TABLE file_security_scans;

-- RBAC 收缩期间短暂移除不可变与 owner trigger；自定义持有者已在首项 DDL 前拒绝。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;

-- 仅删除文件安全扫描权限的 canonical 关系，自定义持有者已由业务门禁拒绝。
DELETE FROM role_permissions
WHERE permission_id IN (
  SELECT permission_id FROM permissions WHERE code='file.security.scan'
);
-- 删除四条产品默认授权关系，使角色上限精确恢复 revision 8。
DELETE FROM rbac_role_permission_templates
WHERE permission_code='file.security.scan';

-- 产品 Permission 目录恢复 revision 8 的 61 项，旧 rowid 和展示元数据全部保真。
CREATE TABLE rbac_permission_templates_prd073_old (
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
-- 旧 61 项保留 rowid 与展示元数据，只把 catalog revision 恢复为 8。
INSERT INTO rbac_permission_templates_prd073_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,8
FROM rbac_permission_templates WHERE permission_code!='file.security.scan';
DROP TABLE rbac_permission_templates;
-- 目录复制完整后恢复正式名称，再重新启用产品模板不可变触发器。
ALTER TABLE rbac_permission_templates_prd073_old RENAME TO rbac_permission_templates;
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

-- 租户 Permission 投影移除一项新实体，其余 ID/rowid/时间和关系 owner 保持不变。
CREATE TABLE permissions_prd073_old (
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
-- 每租户旧 Permission 保留稳定身份与时间，只排除 revision 9 新增的文件权限。
INSERT INTO permissions_prd073_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       8,status,created_at,updated_at
FROM permissions WHERE code!='file.security.scan';
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
-- 租户权限投影恢复正式名称，现有旧关系继续指向同一 Permission ID。
ALTER TABLE permissions_prd073_old RENAME TO permissions;
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

-- 只回退仍精确 canonical 的角色；revision 1 保持下限，避免新租户产生 revision 0。
UPDATE roles
SET permission_revision=CASE WHEN permission_revision>1 THEN permission_revision-1 ELSE 1 END,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052'
      WHEN 'developer' THEN 'd1cc4e0cad810c33e57ca2061a2345ef9a7843b26d8af80cb218d4dbf34fae87'
      WHEN 'viewer' THEN '519abd5f24d6c0612727f010fc1eae824b8c05ff0b8a4b0ca32f5ee9c2d3ddb5'
      WHEN 'security_admin' THEN '6b090e14a83776cb75f9633a8cd49033b4b95fc79f18de6c33fbefd1f6f04ec0'
    END
WHERE role_id IN (SELECT role_id FROM prd073_roles_to_contract);

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

-- 降级后的新 tenant 继续原子物化 revision 8 的 61 权限、162 关系和六角色 hash。
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 降级后新 tenant 先建立六个 revision 8 默认角色，角色 revision 仍从 1 起步。
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

  -- 再物化 revision 8 的 61 项同 tenant Permission，禁止跨租户复用 ID。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后建立 162 条产品默认关系，完整初始化保持单一事务原子性。
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

-- 最终证明 0064 的 revision 8、PRD-072 文件对象保留且无 PRD-073 遗留对象。
DROP TABLE IF EXISTS temp.prd073_final_guard;
CREATE TEMP TABLE prd073_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd073_final_guard_ok CHECK (issue_count=0)
);
-- 回退完成后必须精确恢复 0064 目录和文件事实，任何漂移都让本次 down 原子失败。
INSERT INTO prd073_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=61
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
      'rbac_permission_templates_prd073_old','permissions_prd073_old'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd073_final_guard;
DROP TABLE prd073_roles_to_contract;
COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
