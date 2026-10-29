-- PRD-070：多轮会话风险、独立冻结覆盖层、revision 7 权限与同步意图兼容升级。
-- 所有持久业务事实均 tenant-qualified；表中只保存固定摘要和版本化 HMAC，不保存正文、Header 或命中片段。
-- SQLite 换表期间暂时停用即时 FK 执行；最终 guard 会在提交前运行 foreign_key_check。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项 DDL 前证明数据库精确处于 0061 合同，避免在未知后续 schema 上执行换表。
DROP TABLE IF EXISTS temp.prd070_up_guard;
CREATE TEMP TABLE prd070_up_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd070_up_guard_ok CHECK (issue_count=0)
);
-- 目录、租户投影、Intent/RequestLog 形状和目标对象必须全部匹配 0061。
INSERT INTO prd070_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=57
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=6)=57
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=151
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*57 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=6)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('intent_risk_results'))=22
      AND (SELECT COUNT(*) FROM pragma_table_info('intent_risk_events'))=19
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=61
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='request_logs')=14 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema object WHERE object.name IN (
      'session_risk_scores','session_risk_observations','session_risk_freezes',
      'session_risk_actions','session_risk_events','rbac_permission_templates_prd070_new',
      'permissions_prd070_new','intent_risk_results_prd070_new',
      'intent_risk_events_prd070_new'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd070_up_guard;

-- 只认领仍精确匹配 revision 6 默认矩阵的 Admin/SecurityAdmin；自定义 SecurityAdmin 不扩权。
DROP TABLE IF EXISTS temp.prd070_roles_to_expand;
CREATE TEMP TABLE prd070_roles_to_expand (
  role_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  role_code TEXT NOT NULL,
  permission_revision INTEGER NOT NULL
);
-- hash 与真实关系集合双重相等才属于 canonical 产品矩阵，避免仅伪造 hash 获得新权限。
INSERT INTO prd070_roles_to_expand(role_id,tenant_id,role_code,permission_revision)
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
    WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94' END
  AND NOT EXISTS (
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code
      AND NOT EXISTS (
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

-- RBAC 换表期间暂时移除产品模板和 owner trigger，完成目录/投影后按 revision 7 重建。
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

-- 产品 Permission 模板换表保留原 57 行 rowid，只提升 revision 并追加两个会话风险权限。
CREATE TABLE rbac_permission_templates_prd070_new (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=7)
);
-- 既有模板的展示元数据与 rowid 原样保留，catalog revision 统一提升到 7。
INSERT INTO rbac_permission_templates_prd070_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,7
FROM rbac_permission_templates;
-- 新增权限使用固定中文元数据，API 与迁移共享 code/risk 边界。
INSERT INTO rbac_permission_templates_prd070_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('session_risk.read','security','读取会话风险',
   '读取当前租户高危会话、冻结状态和不可变处置摘要。','read',7),
  ('session_risk.manage','security','处置会话风险',
   '冻结或释放已识别会话及其关联 Virtual Key 风险覆盖层。','critical',7);
DROP TABLE rbac_permission_templates;
-- 新目录已完整复制并追加权限，恢复正式表名后才能重新启用不可变模板触发器。
ALTER TABLE rbac_permission_templates_prd070_new RENAME TO rbac_permission_templates;

-- revision 7 产品模板继续无条件不可变，后续变更必须通过新迁移。
CREATE TRIGGER trg_rbac_permission_templates_insert_prd059
BEFORE INSERT ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059
BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059
BEFORE DELETE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;

-- 角色产品模板只给 Admin/SecurityAdmin 增加两项；其他四角色保持原集合。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','session_risk.read',1),('admin','session_risk.manage',1),
  ('security_admin','session_risk.read',1),('security_admin','session_risk.manage',1);
CREATE TRIGGER trg_rbac_role_templates_insert_prd059
BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059
BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059
BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;

-- tenant Permission 投影换表保留 0061 的 ID/rowid/时间，并为每个 tenant 生成两项新实体。
CREATE TABLE permissions_prd070_new (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=7),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 既有实体原样复制，仅升级目录 revision；稳定 ID 防止已有审计/关系失联。
INSERT INTO permissions_prd070_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       7,status,created_at,updated_at
FROM permissions;
-- 每个 tenant 从产品模板物化两项新权限，时间由同一 SQLite 秒产生且不携带租户输入。
INSERT INTO permissions_prd070_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       7,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template
  ON template.permission_code IN ('session_risk.read','session_risk.manage');
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
-- 租户权限投影复制完成后恢复正式表名，稳定 ID 与关系外键继续指向同一实体。
ALTER TABLE permissions_prd070_new RENAME TO permissions;
-- tenant/category 索引与不可变 owner trigger 在新表上恢复。
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

-- 仅为已认领 canonical 角色建立四类新关系，自定义矩阵保持原样且不静默扩权。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd070_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE permission.code IN ('session_risk.read','session_risk.manage');
-- 被扩展角色只推进一个 CAS revision，并写入 revision 7 精确默认 hash。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '792e003e6c0a188c124ef71ce7ab31658e7ef43a6dadaf6b83c339dfff2caf7f'
      WHEN 'security_admin' THEN 'de4701bc3ce4305c129704c69212d8a520ea5942a87c6119a36542e01784b78c' END
WHERE role_id IN (SELECT role_id FROM prd070_roles_to_expand);

-- 角色 owner 和 CAS 更新继续使用固定六角色合同；新 tenant 从 revision 1 起步。
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '792e003e6c0a188c124ef71ce7ab31658e7ef43a6dadaf6b83c339dfff2caf7f'
    WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
    WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN 'de4701bc3ce4305c129704c69212d8a520ea5942a87c6119a36542e01784b78c'
    WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b' END
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

-- 新 tenant 原子物化 revision 7 的 59 权限、155 关系和六个 revision 1 角色。
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户先创建六个固定角色，后续权限与关系都以这些同租户 owner 为锚点。
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
      WHEN 'admin' THEN '792e003e6c0a188c124ef71ce7ab31658e7ef43a6dadaf6b83c339dfff2caf7f'
      WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
      WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN 'de4701bc3ce4305c129704c69212d8a520ea5942a87c6119a36542e01784b78c'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 角色存在后再物化 revision 7 的完整权限目录，禁止跨租户复用 Permission ID。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后按产品模板建立同租户角色关系，使新租户初始化在单一触发器事务内完成。
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

-- Intent 两表使用安全换表扩展 sync_session；旧 0061 行的 rowid、值和唯一关系全部保真。
CREATE TABLE intent_risk_results_prd070_new (
  intent_risk_result_id TEXT PRIMARY KEY CHECK (
    length(intent_risk_result_id) BETWEEN 5 AND 128 AND intent_risk_result_id GLOB 'irr_*'
  ),
  tenant_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('gateway','manual')),
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('virtual_key','user')),
  actor_id TEXT NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 128),
  status TEXT NOT NULL CHECK (status IN ('clean','detected','blocked','failed')),
  classification_mode TEXT NOT NULL CHECK (
    classification_mode IN ('sync_strong','async_light','manual_sync','sync_session')
  ),
  risk_type TEXT NOT NULL CHECK (
    risk_type IN ('none','credential_exfiltration','system_prompt_extraction','policy_bypass','jailbreak')
  ),
  risk_score INTEGER NOT NULL CHECK (
    typeof(risk_score)='integer' AND risk_score BETWEEN 0 AND 10000
  ),
  risk_score_scale INTEGER NOT NULL CHECK (risk_score_scale=10000),
  action TEXT NOT NULL CHECK (action IN ('none','allow','observe','block','review')),
  reason_code TEXT NOT NULL CHECK (reason_code IN (
    'no_risk_detected','explicit_credential_extraction','explicit_system_prompt_request',
    'explicit_policy_bypass','explicit_instruction_override','credential_access_request',
    'system_prompt_access_request','classifier_failed'
  )),
  detector_version TEXT NOT NULL CHECK (
    length(detector_version) BETWEEN 1 AND 128 AND instr(detector_version,char(0))=0
  ),
  ruleset_version TEXT NOT NULL CHECK (
    length(ruleset_version) BETWEEN 1 AND 128 AND instr(ruleset_version,char(0))=0
  ),
  input_code_points INTEGER NOT NULL CHECK (
    typeof(input_code_points)='integer' AND input_code_points BETWEEN 0 AND 20000
  ),
  input_truncated INTEGER NOT NULL CHECK (input_truncated IN (0,1)),
  duration_ms INTEGER NOT NULL CHECK (
    typeof(duration_ms)='integer' AND duration_ms BETWEEN 0 AND 86400000
  ),
  error_code TEXT CHECK (error_code IS NULL OR error_code='intent_risk.classifier_failed'),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  completed_at TEXT NOT NULL CHECK (
    unixepoch(completed_at) IS NOT NULL AND unixepoch(completed_at)>=unixepoch(created_at)
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id,intent_risk_result_id),
  UNIQUE (tenant_id,source_kind,source_request_id),
  CHECK (
    (source_kind='gateway' AND actor_type='virtual_key'
      AND classification_mode IN ('sync_strong','async_light','sync_session'))
    OR (source_kind='manual' AND actor_type='user' AND classification_mode='manual_sync'
      AND input_truncated=0)
  ),
  CHECK (
    (status='clean' AND classification_mode IN ('async_light','manual_sync','sync_session')
      AND risk_type='none' AND risk_score=0 AND action='allow'
      AND reason_code='no_risk_detected' AND error_code IS NULL)
    OR (status='detected' AND classification_mode IN ('async_light','sync_session')
      AND risk_type!='none' AND risk_score>0 AND action='observe' AND error_code IS NULL)
    OR (status='detected' AND classification_mode='manual_sync'
      AND risk_type!='none' AND risk_score>0 AND action='review' AND error_code IS NULL)
    OR (status='blocked' AND source_kind='gateway'
      AND classification_mode IN ('sync_strong','sync_session')
      AND risk_type!='none' AND risk_score BETWEEN 9500 AND 10000
      AND action='block' AND error_code IS NULL)
    OR (status='failed'
      AND ((source_kind='gateway'
        AND classification_mode IN ('sync_strong','async_light','sync_session'))
        OR (source_kind='manual' AND classification_mode='manual_sync'))
      AND risk_type='none' AND risk_score=0 AND action='none'
      AND reason_code='classifier_failed' AND error_code='intent_risk.classifier_failed')
  ),
  CHECK (
    (risk_type='none' AND reason_code IN ('no_risk_detected','classifier_failed'))
    OR (risk_type='credential_exfiltration'
      AND reason_code IN ('explicit_credential_extraction','credential_access_request'))
    OR (risk_type='system_prompt_extraction'
      AND reason_code IN ('explicit_system_prompt_request','system_prompt_access_request'))
    OR (risk_type='policy_bypass' AND reason_code='explicit_policy_bypass')
    OR (risk_type='jailbreak' AND reason_code='explicit_instruction_override')
  )
);
-- 旧结果逐列复制并保留 rowid，换表不会重新生成任何安全事实。
INSERT INTO intent_risk_results_prd070_new(
  rowid,intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
  actor_type,actor_id,status,classification_mode,risk_type,risk_score,risk_score_scale,
  action,reason_code,detector_version,ruleset_version,input_code_points,input_truncated,
  duration_ms,error_code,created_at,completed_at
)
SELECT rowid,intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
  actor_type,actor_id,status,classification_mode,risk_type,risk_score,risk_score_scale,
  action,reason_code,detector_version,ruleset_version,input_code_points,input_truncated,
  duration_ms,error_code,created_at,completed_at
FROM intent_risk_results;

-- 新事件表只扩展 classification_mode，payload 字段集合仍与 PRD-069 完全相同。
CREATE TABLE intent_risk_events_prd070_new (
  intent_risk_event_id TEXT PRIMARY KEY CHECK (
    length(intent_risk_event_id) BETWEEN 7 AND 128 AND intent_risk_event_id GLOB 'irevt_*'
  ),
  tenant_id TEXT NOT NULL,
  intent_risk_result_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type='security.intent_detected'),
  source_request_id TEXT NOT NULL,
  source_trace_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('gateway','manual')),
  classification_mode TEXT NOT NULL CHECK (
    classification_mode IN ('sync_strong','async_light','manual_sync','sync_session')
  ),
  risk_type TEXT NOT NULL CHECK (
    risk_type IN ('credential_exfiltration','system_prompt_extraction','policy_bypass','jailbreak')
  ),
  risk_score INTEGER NOT NULL CHECK (
    typeof(risk_score)='integer' AND risk_score BETWEEN 1 AND 10000
  ),
  risk_score_scale INTEGER NOT NULL CHECK (risk_score_scale=10000),
  action TEXT NOT NULL CHECK (action IN ('observe','block','review')),
  reason_code TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  ruleset_version TEXT NOT NULL,
  input_truncated INTEGER NOT NULL CHECK (input_truncated IN (0,1)),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('virtual_key','user')),
  actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,intent_risk_result_id)
    REFERENCES intent_risk_results(tenant_id,intent_risk_result_id),
  UNIQUE (tenant_id,intent_risk_event_id),
  UNIQUE (tenant_id,intent_risk_result_id)
);
-- 旧 source event 保留 rowid 和逐字段摘要，不进行重新分类或重签名。
INSERT INTO intent_risk_events_prd070_new(
  rowid,intent_risk_event_id,tenant_id,intent_risk_result_id,event_type,
  source_request_id,source_trace_id,source_kind,classification_mode,risk_type,
  risk_score,risk_score_scale,action,reason_code,detector_version,ruleset_version,
  input_truncated,actor_type,actor_id,created_at
)
SELECT rowid,intent_risk_event_id,tenant_id,intent_risk_result_id,event_type,
  source_request_id,source_trace_id,source_kind,classification_mode,risk_type,
  risk_score,risk_score_scale,action,reason_code,detector_version,ruleset_version,
  input_truncated,actor_type,actor_id,created_at
FROM intent_risk_events;

-- child-to-parent 删除 0061 两表，随后把新表恢复正式名称并重建索引/owner/immutable trigger。
DROP TABLE intent_risk_events;
DROP TABLE intent_risk_results;
-- 结果父表先恢复正式名称，事件表的复合外键才能重新绑定正确 owner。
ALTER TABLE intent_risk_results_prd070_new RENAME TO intent_risk_results;
-- 父表恢复后再恢复事件子表，避免 rename 阶段产生悬空引用。
ALTER TABLE intent_risk_events_prd070_new RENAME TO intent_risk_events;
-- 结果/事件 tenant-first 索引恢复原名称，保持既有查询计划。
CREATE INDEX idx_intent_risk_results_status_time_prd069
  ON intent_risk_results(tenant_id,status,completed_at DESC,intent_risk_result_id DESC);
CREATE INDEX idx_intent_risk_results_type_time_prd069
  ON intent_risk_results(tenant_id,risk_type,completed_at DESC);
CREATE INDEX idx_intent_risk_events_tenant_time_prd069
  ON intent_risk_events(tenant_id,created_at DESC,intent_risk_event_id DESC);
CREATE TRIGGER trg_intent_risk_results_owner_insert_prd069
BEFORE INSERT ON intent_risk_results FOR EACH ROW
WHEN (NEW.source_kind='gateway' AND NOT EXISTS (
    SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id AND claim.request_id=NEW.source_request_id
      AND claim.trace_id=NEW.source_trace_id AND claim.virtual_key_id IS NEW.actor_id
  ))
  OR (NEW.source_kind='manual' AND NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_id
      AND actor.status='active'
  ))
  OR EXISTS (
    SELECT 1 FROM intent_risk_results current
    WHERE current.intent_risk_result_id=NEW.intent_risk_result_id
       OR (current.tenant_id=NEW.tenant_id AND current.source_kind=NEW.source_kind
           AND current.source_request_id=NEW.source_request_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'intent_risk_result_owner_mismatch'); END;
CREATE TRIGGER trg_intent_risk_results_update_prd069
BEFORE UPDATE ON intent_risk_results FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_result_immutable'); END;
CREATE TRIGGER trg_intent_risk_results_delete_prd069
BEFORE DELETE ON intent_risk_results FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_result_immutable'); END;
CREATE TRIGGER trg_intent_risk_events_owner_insert_prd069
BEFORE INSERT ON intent_risk_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM intent_risk_results result
  WHERE result.tenant_id=NEW.tenant_id
    AND result.intent_risk_result_id=NEW.intent_risk_result_id
    AND result.status IN ('detected','blocked')
    AND result.source_request_id=NEW.source_request_id
    AND result.source_trace_id=NEW.source_trace_id
    AND result.source_kind=NEW.source_kind
    AND result.classification_mode=NEW.classification_mode
    AND result.risk_type=NEW.risk_type AND result.risk_score=NEW.risk_score
    AND result.risk_score_scale=NEW.risk_score_scale AND result.action=NEW.action
    AND result.reason_code=NEW.reason_code
    AND result.detector_version=NEW.detector_version
    AND result.ruleset_version=NEW.ruleset_version
    AND result.input_truncated=NEW.input_truncated
    AND result.actor_type=NEW.actor_type AND result.actor_id=NEW.actor_id
    AND result.completed_at=NEW.created_at
)
  OR EXISTS (
    SELECT 1 FROM intent_risk_events current
    WHERE current.intent_risk_event_id=NEW.intent_risk_event_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.intent_risk_result_id=NEW.intent_risk_result_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'intent_risk_event_owner_mismatch'); END;
CREATE TRIGGER trg_intent_risk_events_update_prd069
BEFORE UPDATE ON intent_risk_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_event_immutable'); END;
CREATE TRIGGER trg_intent_risk_events_delete_prd069
BEFORE DELETE ON intent_risk_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_event_immutable'); END;

-- RequestLog 先移除 0061 组合 trigger，再以列级安全换位扩展 completed/sync_session 等枚举。
DROP TRIGGER trg_request_logs_intent_insert_prd069;
DROP TRIGGER trg_request_logs_intent_update_prd069;
-- 旧调度列先改为迁移私有名称，为原位置创建扩展 CHECK 且保留全部旧值。
ALTER TABLE request_logs RENAME COLUMN intent_risk_schedule_status TO intent_risk_schedule_status_prd069;
-- 新调度列追加 completed，旧值在同一事务逐行复制后移除临时列。
ALTER TABLE request_logs ADD COLUMN intent_risk_schedule_status TEXT NOT NULL DEFAULT 'disabled'
  CHECK (intent_risk_schedule_status IN (
    'disabled','queued','skipped','blocked','failed_closed','completed'
  ));
-- 逐行复制旧调度事实，迁移不重新解释任何历史请求的分类状态。
UPDATE request_logs
SET intent_risk_schedule_status=intent_risk_schedule_status_prd069;
-- 所有旧值进入新 CHECK 后移除临时列，避免同一事实保留两份可漂移副本。
ALTER TABLE request_logs DROP COLUMN intent_risk_schedule_status_prd069;

-- 分类模式列采用同样的安全换位，只新增 Gateway 可信同步会话模式。
ALTER TABLE request_logs RENAME COLUMN intent_risk_mode TO intent_risk_mode_prd069;
-- 新模式只增加可信 session 同步路径，manual_sync 仍不能进入 Gateway RequestLog。
ALTER TABLE request_logs ADD COLUMN intent_risk_mode TEXT CHECK (
  intent_risk_mode IS NULL OR intent_risk_mode IN ('async_light','sync_strong','sync_session')
);
-- 原模式值原样进入新列，不把历史异步或强同步请求改写为会话模式。
UPDATE request_logs SET intent_risk_mode=intent_risk_mode_prd069;
-- 新列复制成功后删除旧模式临时列，保持 RequestLog 单一权威字段。
ALTER TABLE request_logs DROP COLUMN intent_risk_mode_prd069;

-- 动作列先改名再扩展 allow/observe，避免直接重建整张高关联日志表。
ALTER TABLE request_logs RENAME COLUMN intent_risk_action TO intent_risk_action_prd069;
-- completed 可记录 clean/observe/block，失败关闭仍只允许 none，禁止自由动作进入日志。
ALTER TABLE request_logs ADD COLUMN intent_risk_action TEXT CHECK (
  intent_risk_action IS NULL OR intent_risk_action IN ('allow','observe','block','none')
);
-- 历史 block/none 动作逐值保留，不从风险分数重新推导动作。
UPDATE request_logs SET intent_risk_action=intent_risk_action_prd069;
-- 删除已完成复制的动作临时列，防止后续 upsert 更新错误字段。
ALTER TABLE request_logs DROP COLUMN intent_risk_action_prd069;

-- 原因列安全换位后才能加入 clean 与弱分类原因，同时继续拒绝自由文本。
ALTER TABLE request_logs RENAME COLUMN intent_risk_reason_code TO intent_risk_reason_code_prd069;
-- 原因目录扩展 clean 与两条弱分类结果，禁止自由文本或命中片段进入 RequestLog。
ALTER TABLE request_logs ADD COLUMN intent_risk_reason_code TEXT CHECK (
  intent_risk_reason_code IS NULL OR intent_risk_reason_code IN (
    'queued','queue_full','worker_unavailable','classifier_failed','result_store_unavailable',
    'no_risk_detected','explicit_credential_extraction','explicit_system_prompt_request',
    'explicit_policy_bypass','explicit_instruction_override','credential_access_request',
    'system_prompt_access_request'
  )
);
-- 旧固定原因逐值复制，任何不满足新目录的异常历史都会在事务内失败关闭。
UPDATE request_logs SET intent_risk_reason_code=intent_risk_reason_code_prd069;
-- 原因事实完成复制后删除迁移临时列，保持查询与索引只读正式字段。
ALTER TABLE request_logs DROP COLUMN intent_risk_reason_code_prd069;

-- 会话风险摘要只记录状态、score identity/type/state 与固定失败原因；不保存 scope key 或 turn digest。
ALTER TABLE request_logs ADD COLUMN session_risk_status TEXT NOT NULL DEFAULT 'not_applicable'
  CHECK (session_risk_status IN (
    'not_applicable','disabled','clean','monitoring','high_risk','frozen',
    'duplicate','degraded','failed'
  ));
-- score ID 只保存固定前缀低熵标识，不把 scope key 或正文写入请求日志。
ALTER TABLE request_logs ADD COLUMN session_risk_score_id TEXT CHECK (
  session_risk_score_id IS NULL OR (
    length(session_risk_score_id)=36 AND substr(session_risk_score_id,1,4)='srs_'
    AND substr(session_risk_score_id,5) NOT GLOB '*[^0-9a-f]*'
  )
);
-- scope 摘要仅区分企业 Chat 与公开 API，会话原始标识继续留在安全事实表。
ALTER TABLE request_logs ADD COLUMN session_risk_scope_type TEXT CHECK (
  session_risk_scope_type IS NULL
  OR session_risk_scope_type IN ('enterprise_chat','api_session')
);
-- 状态摘要使用固定状态机枚举，避免日志层形成第二套自由状态来源。
ALTER TABLE request_logs ADD COLUMN session_risk_state TEXT CHECK (
  session_risk_state IS NULL
  OR session_risk_state IN ('monitoring','high_risk','frozen','resolved','expired')
);
-- 原因摘要仅允许稳定错误目录，敏感命中片段和存储异常详情不得持久化。
ALTER TABLE request_logs ADD COLUMN session_risk_reason_code TEXT CHECK (
  session_risk_reason_code IS NULL OR session_risk_reason_code IN (
    'feature_disabled','no_session_identifiers','clean_turn','risk_accumulated','duplicate_turn',
    'store_degraded','store_unavailable','evidence_unavailable','session_frozen',
    'key_frozen','turn_conflict','hmac_version_unavailable'
  )
);

-- Intent 与 session-risk 摘要在 INSERT 时共同 exact-validate，任一半组字段都失败关闭。
CREATE TRIGGER trg_request_logs_intent_insert_prd069
BEFORE INSERT ON request_logs FOR EACH ROW
WHEN (
  (
    (NEW.intent_risk_schedule_status='disabled'
      AND NEW.intent_risk_result_id IS NULL AND NEW.intent_risk_mode IS NULL
      AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
      AND NEW.intent_risk_action IS NULL AND NEW.intent_risk_reason_code IS NULL
      AND NEW.intent_risk_detector_version IS NULL AND NEW.intent_risk_input_truncated=0)
    OR (NEW.intent_risk_schedule_status='queued'
      AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='async_light'
      AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
      AND NEW.intent_risk_action IS NULL AND NEW.intent_risk_reason_code='queued'
      AND NEW.intent_risk_detector_version IS NOT NULL)
    OR (NEW.intent_risk_schedule_status='skipped'
      AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='async_light'
      AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
      AND NEW.intent_risk_action IS NULL
      AND NEW.intent_risk_reason_code IN ('queue_full','worker_unavailable')
      AND NEW.intent_risk_detector_version IS NOT NULL)
    OR (NEW.intent_risk_schedule_status='blocked'
      AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='sync_strong'
      AND NEW.intent_risk_type IS NOT NULL AND NEW.intent_risk_score BETWEEN 9500 AND 10000
      AND NEW.intent_risk_action='block' AND NEW.intent_risk_detector_version IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM intent_risk_results result
        WHERE result.tenant_id=NEW.tenant_id AND result.source_kind='gateway'
          AND result.source_request_id=NEW.request_id
          AND result.intent_risk_result_id=NEW.intent_risk_result_id
          AND result.status='blocked' AND result.classification_mode=NEW.intent_risk_mode
          AND result.risk_type=NEW.intent_risk_type AND result.risk_score=NEW.intent_risk_score
          AND result.action=NEW.intent_risk_action
          AND result.reason_code=NEW.intent_risk_reason_code
          AND result.detector_version=NEW.intent_risk_detector_version
          AND result.input_truncated=NEW.intent_risk_input_truncated
      ))
    OR (NEW.intent_risk_schedule_status='completed'
      AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='sync_session'
      AND EXISTS (
        SELECT 1 FROM intent_risk_results result
        WHERE result.tenant_id=NEW.tenant_id AND result.source_kind='gateway'
          AND result.source_request_id=NEW.request_id
          AND result.intent_risk_result_id=NEW.intent_risk_result_id
          AND result.classification_mode='sync_session'
          AND result.status IN ('clean','detected','blocked')
          AND NEW.intent_risk_type IS CASE
            WHEN result.risk_type='none' THEN NULL ELSE result.risk_type END
          AND NEW.intent_risk_score=result.risk_score
          AND NEW.intent_risk_action=result.action
          AND NEW.intent_risk_reason_code=result.reason_code
          AND NEW.intent_risk_detector_version=result.detector_version
          AND NEW.intent_risk_input_truncated=result.input_truncated
      ))
    OR (NEW.intent_risk_schedule_status='failed_closed'
      AND NEW.intent_risk_mode IN ('sync_strong','sync_session')
      AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
      AND NEW.intent_risk_action='none'
      AND NEW.intent_risk_reason_code IN ('classifier_failed','result_store_unavailable')
      AND NEW.intent_risk_detector_version IS NOT NULL
      AND (
        (NEW.intent_risk_reason_code='result_store_unavailable'
          AND NEW.intent_risk_result_id IS NULL)
        OR (NEW.intent_risk_reason_code='classifier_failed'
          AND (NEW.intent_risk_result_id IS NULL OR EXISTS (
            SELECT 1 FROM intent_risk_results result
            WHERE result.tenant_id=NEW.tenant_id AND result.source_kind='gateway'
              AND result.source_request_id=NEW.request_id
              AND result.intent_risk_result_id=NEW.intent_risk_result_id
              AND result.status='failed' AND result.classification_mode=NEW.intent_risk_mode
              AND result.detector_version=NEW.intent_risk_detector_version
              AND result.input_truncated=NEW.intent_risk_input_truncated
          )))
      ))
  )
  AND (
    (NEW.session_risk_status='not_applicable'
      AND NEW.session_risk_score_id IS NULL AND NEW.session_risk_scope_type IS NULL
      AND NEW.session_risk_state IS NULL AND NEW.session_risk_reason_code IS NULL)
    OR (NEW.session_risk_status='disabled'
      AND NEW.session_risk_score_id IS NULL AND NEW.session_risk_scope_type IS NULL
      AND NEW.session_risk_state IS NULL
      AND NEW.session_risk_reason_code IN ('feature_disabled','no_session_identifiers'))
    OR (NEW.session_risk_status='clean'
      AND NEW.session_risk_score_id IS NULL AND NEW.session_risk_scope_type IS NOT NULL
      AND NEW.session_risk_state IS NULL AND NEW.session_risk_reason_code='clean_turn')
    OR (NEW.session_risk_status IN ('monitoring','high_risk','frozen')
      AND NEW.session_risk_score_id IS NOT NULL AND NEW.session_risk_scope_type IS NOT NULL
      AND NEW.session_risk_state=NEW.session_risk_status
      AND NEW.session_risk_reason_code IN ('risk_accumulated','session_frozen','key_frozen'))
    OR (NEW.session_risk_status='duplicate'
      AND NEW.session_risk_scope_type IS NOT NULL
      AND NEW.session_risk_reason_code='duplicate_turn')
    OR (NEW.session_risk_status='degraded'
      AND NEW.session_risk_score_id IS NULL AND NEW.session_risk_scope_type IS NOT NULL
      AND NEW.session_risk_state IS NULL AND NEW.session_risk_reason_code='store_degraded')
    OR (NEW.session_risk_status='failed'
      AND NEW.session_risk_reason_code IN (
        'store_unavailable','evidence_unavailable','session_frozen','key_frozen',
        'turn_conflict','hmac_version_unavailable'
      ))
  )
) IS NOT TRUE
BEGIN SELECT RAISE(ABORT,'request_log_session_risk_state_invalid'); END;

-- 活跃 score cycle 是 tenant-qualified 持久快照；scope_key/API HMAC 不进入任何公开响应。
CREATE TABLE session_risk_scores (
  session_risk_score_id TEXT PRIMARY KEY CHECK (
    length(session_risk_score_id)=36 AND substr(session_risk_score_id,1,4)='srs_'
    AND substr(session_risk_score_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('enterprise_chat','api_session')),
  scope_key TEXT NOT NULL CHECK (length(scope_key) BETWEEN 1 AND 160),
  hmac_key_version INTEGER NOT NULL CHECK (
    typeof(hmac_key_version)='integer' AND hmac_key_version>=1
  ),
  virtual_key_id TEXT NOT NULL,
  chat_session_id TEXT,
  status TEXT NOT NULL CHECK (
    status IN ('monitoring','high_risk','frozen','resolved','expired')
  ),
  score INTEGER NOT NULL CHECK (typeof(score)='integer' AND score BETWEEN 1 AND 10000),
  score_scale INTEGER NOT NULL CHECK (score_scale=10000),
  risky_turn_count INTEGER NOT NULL CHECK (
    typeof(risky_turn_count)='integer' AND risky_turn_count>=1
  ),
  jailbreak_count INTEGER NOT NULL CHECK (
    typeof(jailbreak_count)='integer' AND jailbreak_count>=0
  ),
  sensitive_count INTEGER NOT NULL CHECK (
    typeof(sensitive_count)='integer' AND sensitive_count>=0
  ),
  tool_inducement_count INTEGER NOT NULL CHECK (
    typeof(tool_inducement_count)='integer' AND tool_inducement_count>=0
  ),
  state_revision INTEGER NOT NULL CHECK (
    typeof(state_revision)='integer' AND state_revision>=1
  ),
  threshold_version TEXT NOT NULL CHECK (threshold_version='session-risk-threshold-v1'),
  detector_version TEXT NOT NULL CHECK (length(detector_version) BETWEEN 1 AND 128),
  ruleset_version TEXT NOT NULL CHECK (length(ruleset_version) BETWEEN 1 AND 128),
  last_source_request_id TEXT NOT NULL CHECK (length(last_source_request_id) BETWEEN 1 AND 128),
  last_source_trace_id TEXT NOT NULL CHECK (length(last_source_trace_id) BETWEEN 1 AND 128),
  window_started_at TEXT NOT NULL CHECK (unixepoch(window_started_at) IS NOT NULL),
  last_risk_at TEXT NOT NULL CHECK (
    unixepoch(last_risk_at) IS NOT NULL
    AND unixepoch(last_risk_at)>=unixepoch(window_started_at)
  ),
  window_expires_at TEXT CHECK (
    window_expires_at IS NULL OR (
      unixepoch(window_expires_at) IS NOT NULL
      AND unixepoch(window_expires_at)>unixepoch(last_risk_at)
    )
  ),
  first_high_risk_at TEXT CHECK (first_high_risk_at IS NULL OR unixepoch(first_high_risk_at) IS NOT NULL),
  frozen_at TEXT CHECK (frozen_at IS NULL OR unixepoch(frozen_at) IS NOT NULL),
  resolved_at TEXT CHECK (resolved_at IS NULL OR unixepoch(resolved_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (
    unixepoch(updated_at) IS NOT NULL AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,virtual_key_id)
    REFERENCES virtual_keys(tenant_id,virtual_key_id),
  UNIQUE (tenant_id,session_risk_score_id),
  CHECK (
    (scope_type='enterprise_chat' AND chat_session_id IS NOT NULL
      AND scope_key=chat_session_id
      AND length(chat_session_id)=36 AND substr(chat_session_id,1,4)='chs_'
      AND substr(chat_session_id,5) NOT GLOB '*[^0-9a-f]*')
    OR (scope_type='api_session' AND chat_session_id IS NULL
      AND length(scope_key)=68 AND substr(scope_key,1,4)='srh_'
      AND substr(scope_key,5) NOT GLOB '*[^0-9a-f]*')
  ),
  CHECK (
    jailbreak_count<=risky_turn_count AND sensitive_count<=risky_turn_count
    AND tool_inducement_count<=risky_turn_count
    AND jailbreak_count+sensitive_count+tool_inducement_count>=1
  ),
  CHECK (
    (status='monitoring' AND first_high_risk_at IS NULL AND frozen_at IS NULL
      AND resolved_at IS NULL AND window_expires_at IS NOT NULL)
    OR (status='high_risk' AND score>=7000 AND risky_turn_count>=2
      AND first_high_risk_at IS NOT NULL AND frozen_at IS NULL
      AND resolved_at IS NULL AND window_expires_at IS NOT NULL)
    OR (status='frozen' AND score>=7000 AND risky_turn_count>=2
      AND first_high_risk_at IS NOT NULL AND frozen_at IS NOT NULL
      AND resolved_at IS NULL AND window_expires_at IS NULL)
    OR (status='resolved' AND resolved_at IS NOT NULL AND window_expires_at IS NULL)
    OR (status='expired' AND resolved_at IS NOT NULL AND frozen_at IS NULL
      AND first_high_risk_at IS NULL AND window_expires_at IS NULL)
  )
);
-- 一个 tenant/scope 最多一个活动 cycle，历史 resolved/expired 保留并允许新 cycle。
CREATE UNIQUE INDEX uq_session_risk_scores_active_scope_prd070
  ON session_risk_scores(tenant_id,scope_type,scope_key)
  WHERE status IN ('monitoring','high_risk','frozen');
CREATE INDEX idx_session_risk_scores_status_time_prd070
  ON session_risk_scores(tenant_id,status,last_risk_at DESC,session_risk_score_id DESC);
CREATE INDEX idx_session_risk_scores_key_status_prd070
  ON session_risk_scores(tenant_id,virtual_key_id,status);
CREATE INDEX idx_session_risk_scores_chat_status_prd070
  ON session_risk_scores(tenant_id,chat_session_id,status);
CREATE TRIGGER trg_session_risk_scores_owner_insert_prd070
BEFORE INSERT ON session_risk_scores FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM virtual_keys key
  WHERE key.tenant_id=NEW.tenant_id AND key.virtual_key_id=NEW.virtual_key_id
)
  OR (NEW.scope_type='enterprise_chat' AND NOT EXISTS (
    SELECT 1 FROM chat_sessions session
    WHERE session.tenant_id=NEW.tenant_id
      AND session.chat_session_id=NEW.chat_session_id
      AND session.virtual_key_id=NEW.virtual_key_id
  ))
  OR NOT EXISTS (
    SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id
      AND claim.request_id=NEW.last_source_request_id
      AND claim.trace_id=NEW.last_source_trace_id
      AND claim.virtual_key_id=NEW.virtual_key_id
  )
  OR EXISTS (
    SELECT 1 FROM session_risk_scores current
    WHERE current.session_risk_score_id=NEW.session_risk_score_id
       OR (current.tenant_id=NEW.tenant_id AND current.scope_type=NEW.scope_type
           AND current.scope_key=NEW.scope_key
           AND current.status IN ('monitoring','high_risk','frozen'))
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'session_risk_score_owner_mismatch'); END;
CREATE TRIGGER trg_session_risk_scores_state_update_prd070
BEFORE UPDATE ON session_risk_scores FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.session_risk_score_id!=OLD.session_risk_score_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.scope_type!=OLD.scope_type
  OR NEW.scope_key!=OLD.scope_key OR NEW.hmac_key_version!=OLD.hmac_key_version
  OR NEW.virtual_key_id!=OLD.virtual_key_id OR NEW.chat_session_id IS NOT OLD.chat_session_id
  OR NEW.score_scale!=OLD.score_scale OR NEW.threshold_version!=OLD.threshold_version
  OR NEW.created_at!=OLD.created_at OR NEW.state_revision!=OLD.state_revision+1
  OR NEW.score<OLD.score OR NEW.risky_turn_count<OLD.risky_turn_count
  OR NEW.jailbreak_count<OLD.jailbreak_count OR NEW.sensitive_count<OLD.sensitive_count
  OR NEW.tool_inducement_count<OLD.tool_inducement_count
  OR NOT (
    (OLD.status='monitoring' AND NEW.status IN ('monitoring','high_risk','frozen','expired'))
    OR (OLD.status='high_risk' AND NEW.status IN ('high_risk','frozen','resolved'))
    OR (OLD.status='frozen' AND NEW.status IN ('frozen','resolved'))
  )
BEGIN SELECT RAISE(ABORT,'session_risk_score_state_invalid'); END;
CREATE TRIGGER trg_session_risk_scores_delete_prd070
BEFORE DELETE ON session_risk_scores FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_score_immutable'); END;

-- observation 是逐风险 turn 的 append-only 证据，只保存 HMAC digest 与固定安全信号。
CREATE TABLE session_risk_observations (
  session_risk_observation_id TEXT PRIMARY KEY CHECK (
    length(session_risk_observation_id)=36 AND substr(session_risk_observation_id,1,4)='sro_'
    AND substr(session_risk_observation_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  session_risk_score_id TEXT NOT NULL,
  turn_identity_type TEXT NOT NULL CHECK (turn_identity_type IN ('chat_message','api_turn')),
  turn_key_digest TEXT NOT NULL CHECK (
    length(turn_key_digest)=68 AND substr(turn_key_digest,1,4)='srh_'
    AND substr(turn_key_digest,5) NOT GLOB '*[^0-9a-f]*'
  ),
  turn_intent_digest TEXT NOT NULL CHECK (
    length(turn_intent_digest)=68 AND substr(turn_intent_digest,1,4)='srh_'
    AND substr(turn_intent_digest,5) NOT GLOB '*[^0-9a-f]*'
  ),
  hmac_key_version INTEGER NOT NULL CHECK (
    typeof(hmac_key_version)='integer' AND hmac_key_version>=1
  ),
  chat_message_id TEXT,
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  intent_risk_result_id TEXT,
  intent_risk_type TEXT NOT NULL CHECK (
    intent_risk_type IN ('none','credential_exfiltration','system_prompt_extraction','policy_bypass','jailbreak')
  ),
  intent_risk_score INTEGER NOT NULL CHECK (
    typeof(intent_risk_score)='integer' AND intent_risk_score BETWEEN 0 AND 10000
  ),
  intent_risk_score_scale INTEGER NOT NULL CHECK (intent_risk_score_scale=10000),
  dlp_action TEXT NOT NULL CHECK (dlp_action IN ('allow','log_only','block')),
  dlp_current_turn_match_count INTEGER NOT NULL CHECK (
    typeof(dlp_current_turn_match_count)='integer'
    AND dlp_current_turn_match_count BETWEEN 0 AND 100
  ),
  tool_inducement_detected INTEGER NOT NULL CHECK (tool_inducement_detected IN (0,1)),
  turn_delta INTEGER NOT NULL CHECK (
    typeof(turn_delta)='integer' AND turn_delta BETWEEN 1 AND 4000
  ),
  score_before INTEGER NOT NULL CHECK (
    typeof(score_before)='integer' AND score_before BETWEEN 0 AND 10000
  ),
  score_after INTEGER NOT NULL CHECK (
    typeof(score_after)='integer' AND score_after BETWEEN 1 AND 10000
  ),
  risky_turn_index INTEGER NOT NULL CHECK (
    typeof(risky_turn_index)='integer' AND risky_turn_index>=1
  ),
  state_revision INTEGER NOT NULL CHECK (
    typeof(state_revision)='integer' AND state_revision>=1
  ),
  input_truncated INTEGER NOT NULL CHECK (input_truncated IN (0,1)),
  detector_version TEXT NOT NULL CHECK (length(detector_version) BETWEEN 1 AND 128),
  ruleset_version TEXT NOT NULL CHECK (length(ruleset_version) BETWEEN 1 AND 128),
  threshold_version TEXT NOT NULL CHECK (threshold_version='session-risk-threshold-v1'),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,session_risk_score_id)
    REFERENCES session_risk_scores(tenant_id,session_risk_score_id),
  FOREIGN KEY (tenant_id,intent_risk_result_id)
    REFERENCES intent_risk_results(tenant_id,intent_risk_result_id),
  UNIQUE (tenant_id,session_risk_observation_id),
  UNIQUE (tenant_id,session_risk_score_id,turn_key_digest),
  CHECK (
    (turn_identity_type='chat_message' AND chat_message_id IS NOT NULL)
    OR (turn_identity_type='api_turn' AND chat_message_id IS NULL)
  ),
  CHECK (score_after=min(10000,score_before+turn_delta)),
  CHECK (
    (intent_risk_type='none' AND intent_risk_score=0)
    OR (intent_risk_type!='none' AND intent_risk_score>0)
  ),
  CHECK (
    (dlp_action='allow' AND dlp_current_turn_match_count=0)
    OR (dlp_action IN ('log_only','block') AND dlp_current_turn_match_count>=1)
  )
);
CREATE INDEX idx_session_risk_observations_score_time_prd070
  ON session_risk_observations(tenant_id,session_risk_score_id,created_at DESC);
CREATE INDEX idx_session_risk_observations_request_prd070
  ON session_risk_observations(tenant_id,source_request_id);
CREATE TRIGGER trg_session_risk_observations_owner_insert_prd070
BEFORE INSERT ON session_risk_observations FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM session_risk_scores score
  WHERE score.tenant_id=NEW.tenant_id
    AND score.session_risk_score_id=NEW.session_risk_score_id
    AND score.hmac_key_version=NEW.hmac_key_version
    AND score.score=NEW.score_after AND score.risky_turn_count=NEW.risky_turn_index
    AND score.state_revision=NEW.state_revision
    AND score.last_source_request_id=NEW.source_request_id
    AND score.last_source_trace_id=NEW.source_trace_id
)
  OR (NEW.turn_identity_type='chat_message' AND NOT EXISTS (
    SELECT 1 FROM session_risk_scores score
    JOIN chat_messages message
      ON message.tenant_id=score.tenant_id
     AND message.chat_session_id=score.chat_session_id
    WHERE score.tenant_id=NEW.tenant_id
      AND score.session_risk_score_id=NEW.session_risk_score_id
      AND message.chat_message_id=NEW.chat_message_id
      AND message.gateway_request_id=NEW.source_request_id
      AND message.gateway_trace_id=NEW.source_trace_id
  ))
  OR (NEW.intent_risk_result_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM intent_risk_results result
    WHERE result.tenant_id=NEW.tenant_id
      AND result.intent_risk_result_id=NEW.intent_risk_result_id
      AND result.source_request_id=NEW.source_request_id
      AND result.risk_type=NEW.intent_risk_type
      AND result.risk_score=NEW.intent_risk_score
      AND result.risk_score_scale=NEW.intent_risk_score_scale
  ))
  OR EXISTS (
    SELECT 1 FROM session_risk_observations current
    WHERE current.session_risk_observation_id=NEW.session_risk_observation_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.session_risk_score_id=NEW.session_risk_score_id
           AND current.turn_key_digest=NEW.turn_key_digest)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'session_risk_observation_owner_mismatch'); END;
CREATE TRIGGER trg_session_risk_observations_update_prd070
BEFORE UPDATE ON session_risk_observations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_observation_immutable'); END;
CREATE TRIGGER trg_session_risk_observations_delete_prd070
BEFORE DELETE ON session_risk_observations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_observation_immutable'); END;

-- Freeze 是独立安全覆盖层，不改写 Virtual Key 或 ChatSession 的原业务状态。
CREATE TABLE session_risk_freezes (
  session_risk_freeze_id TEXT PRIMARY KEY CHECK (
    length(session_risk_freeze_id)=36 AND substr(session_risk_freeze_id,1,4)='srf_'
    AND substr(session_risk_freeze_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  source_session_risk_score_id TEXT NOT NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('session','virtual_key')),
  target_scope_type TEXT NOT NULL CHECK (
    target_scope_type IN ('enterprise_chat','api_session')
  ),
  target_scope_key TEXT NOT NULL CHECK (length(target_scope_key) BETWEEN 1 AND 160),
  virtual_key_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','released')),
  state_revision INTEGER NOT NULL CHECK (
    typeof(state_revision)='integer' AND state_revision>=1
  ),
  created_by_type TEXT NOT NULL CHECK (created_by_type IN ('system','user')),
  created_by_id TEXT NOT NULL CHECK (length(created_by_id) BETWEEN 1 AND 128),
  released_by_type TEXT CHECK (released_by_type IS NULL OR released_by_type='user'),
  released_by_id TEXT,
  created_source_request_id TEXT NOT NULL CHECK (
    length(created_source_request_id) BETWEEN 1 AND 128
  ),
  created_source_trace_id TEXT NOT NULL CHECK (
    length(created_source_trace_id) BETWEEN 1 AND 128
  ),
  released_source_request_id TEXT,
  released_source_trace_id TEXT,
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  released_at TEXT CHECK (
    released_at IS NULL OR (
      unixepoch(released_at) IS NOT NULL AND unixepoch(released_at)>=unixepoch(created_at)
    )
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,source_session_risk_score_id)
    REFERENCES session_risk_scores(tenant_id,session_risk_score_id),
  FOREIGN KEY (tenant_id,virtual_key_id)
    REFERENCES virtual_keys(tenant_id,virtual_key_id),
  UNIQUE (tenant_id,session_risk_freeze_id),
  CHECK (
    (status='active' AND state_revision=1 AND released_by_type IS NULL
      AND released_by_id IS NULL AND released_source_request_id IS NULL
      AND released_source_trace_id IS NULL AND released_at IS NULL)
    OR (status='released' AND state_revision>=2 AND released_by_type='user'
      AND released_by_id IS NOT NULL AND released_source_request_id IS NOT NULL
      AND released_source_trace_id IS NOT NULL AND released_at IS NOT NULL)
  )
);
-- 同一来源 score 的同类 active overlay 只有一条；不同来源对同 Key 的冻结分别保留。
CREATE UNIQUE INDEX uq_session_risk_freezes_active_target_prd070
  ON session_risk_freezes(tenant_id,source_session_risk_score_id,target_type)
  WHERE status='active';
CREATE INDEX idx_session_risk_freezes_session_lookup_prd070
  ON session_risk_freezes(tenant_id,target_scope_type,target_scope_key,status);
CREATE INDEX idx_session_risk_freezes_key_lookup_prd070
  ON session_risk_freezes(tenant_id,virtual_key_id,status);
CREATE TRIGGER trg_session_risk_freezes_owner_insert_prd070
BEFORE INSERT ON session_risk_freezes FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM session_risk_scores score
  WHERE score.tenant_id=NEW.tenant_id
    AND score.session_risk_score_id=NEW.source_session_risk_score_id
    AND score.scope_type=NEW.target_scope_type AND score.scope_key=NEW.target_scope_key
    AND score.virtual_key_id=NEW.virtual_key_id
    AND score.status IN ('high_risk','frozen')
    AND score.score>=7000 AND score.risky_turn_count>=2
)
  OR (NEW.created_by_type='system' AND (
    NEW.target_type!='session' OR NEW.created_by_id!='session-risk-engine'
  ))
  OR (NEW.created_by_type='user' AND NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.created_by_id
      AND actor.status='active'
  ))
  OR EXISTS (
    SELECT 1 FROM session_risk_freezes current
    WHERE current.session_risk_freeze_id=NEW.session_risk_freeze_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.source_session_risk_score_id=NEW.source_session_risk_score_id
           AND current.target_type=NEW.target_type AND current.status='active')
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'session_risk_freeze_owner_mismatch'); END;
CREATE TRIGGER trg_session_risk_freezes_release_update_prd070
BEFORE UPDATE ON session_risk_freezes FOR EACH ROW
WHEN OLD.status!='active' OR NEW.status!='released'
  OR NEW.rowid!=OLD.rowid OR NEW.session_risk_freeze_id!=OLD.session_risk_freeze_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.source_session_risk_score_id!=OLD.source_session_risk_score_id
  OR NEW.target_type!=OLD.target_type OR NEW.target_scope_type!=OLD.target_scope_type
  OR NEW.target_scope_key!=OLD.target_scope_key OR NEW.virtual_key_id!=OLD.virtual_key_id
  OR NEW.created_by_type!=OLD.created_by_type OR NEW.created_by_id!=OLD.created_by_id
  OR NEW.created_source_request_id!=OLD.created_source_request_id
  OR NEW.created_source_trace_id!=OLD.created_source_trace_id
  OR NEW.created_at!=OLD.created_at OR NEW.state_revision!=OLD.state_revision+1
  OR NEW.released_by_type!='user' OR NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.released_by_id
      AND actor.status='active'
  )
BEGIN SELECT RAISE(ABORT,'session_risk_freeze_state_invalid'); END;
CREATE TRIGGER trg_session_risk_freezes_delete_prd070
BEFORE DELETE ON session_risk_freezes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_freeze_immutable'); END;

-- Action 是自动跃迁与人工处置的 append-only 历史；低熵幂等键只保存域分隔 HMAC。
CREATE TABLE session_risk_actions (
  session_risk_action_id TEXT PRIMARY KEY CHECK (
    length(session_risk_action_id)=36 AND substr(session_risk_action_id,1,4)='sra_'
    AND substr(session_risk_action_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  session_risk_score_id TEXT NOT NULL,
  session_risk_freeze_id TEXT,
  action TEXT NOT NULL CHECK (action IN (
    'high_risk','freeze_session','freeze_virtual_key','dismiss',
    'release_session','release_virtual_key','expire'
  )),
  reason_code TEXT NOT NULL CHECK (reason_code IN (
    'threshold_reached','confirmed_attack','false_positive','verified_safe',
    'incident_contained','window_expired'
  )),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('system','user')),
  actor_id TEXT NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 128),
  status_before TEXT NOT NULL CHECK (
    status_before IN ('monitoring','high_risk','frozen','resolved','expired')
  ),
  status_after TEXT NOT NULL CHECK (
    status_after IN ('high_risk','frozen','resolved','expired')
  ),
  score_snapshot INTEGER NOT NULL CHECK (
    typeof(score_snapshot)='integer' AND score_snapshot BETWEEN 1 AND 10000
  ),
  state_revision INTEGER NOT NULL CHECK (
    typeof(state_revision)='integer' AND state_revision>=1
  ),
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  digest_key_version INTEGER CHECK (
    digest_key_version IS NULL OR (typeof(digest_key_version)='integer' AND digest_key_version>=1)
  ),
  idempotency_key_digest TEXT CHECK (
    idempotency_key_digest IS NULL OR (
      length(idempotency_key_digest)=68 AND substr(idempotency_key_digest,1,4)='srh_'
      AND substr(idempotency_key_digest,5) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  intent_digest TEXT CHECK (
    intent_digest IS NULL OR (
      length(intent_digest)=68 AND substr(intent_digest,1,4)='srh_'
      AND substr(intent_digest,5) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  idempotency_expires_at TEXT CHECK (
    idempotency_expires_at IS NULL OR unixepoch(idempotency_expires_at) IS NOT NULL
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,session_risk_score_id)
    REFERENCES session_risk_scores(tenant_id,session_risk_score_id),
  FOREIGN KEY (tenant_id,session_risk_freeze_id)
    REFERENCES session_risk_freezes(tenant_id,session_risk_freeze_id),
  UNIQUE (tenant_id,session_risk_action_id),
  CHECK (
    (actor_type='system' AND actor_id='session-risk-engine'
      AND digest_key_version IS NULL AND idempotency_key_digest IS NULL
      AND intent_digest IS NULL AND idempotency_expires_at IS NULL)
    OR (actor_type='user' AND digest_key_version IS NOT NULL
      AND idempotency_key_digest IS NOT NULL AND intent_digest IS NOT NULL
      AND idempotency_expires_at IS NOT NULL
      AND unixepoch(idempotency_expires_at)>unixepoch(created_at))
  ),
  CHECK (
    (action IN ('high_risk','dismiss','expire') AND session_risk_freeze_id IS NULL)
    OR (action IN ('freeze_session','freeze_virtual_key','release_session','release_virtual_key')
      AND session_risk_freeze_id IS NOT NULL)
  )
);
CREATE INDEX idx_session_risk_actions_score_time_prd070
  ON session_risk_actions(tenant_id,session_risk_score_id,created_at DESC);
CREATE INDEX idx_session_risk_actions_idempotency_expiry_prd070
  ON session_risk_actions(
    tenant_id,actor_id,digest_key_version,idempotency_key_digest,idempotency_expires_at
  );
CREATE TRIGGER trg_session_risk_actions_owner_insert_prd070
BEFORE INSERT ON session_risk_actions FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM session_risk_scores score
  WHERE score.tenant_id=NEW.tenant_id
    AND score.session_risk_score_id=NEW.session_risk_score_id
    AND score.score=NEW.score_snapshot AND score.state_revision=NEW.state_revision
    AND score.status=NEW.status_after
)
  OR (NEW.session_risk_freeze_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM session_risk_freezes freeze
    WHERE freeze.tenant_id=NEW.tenant_id
      AND freeze.session_risk_freeze_id=NEW.session_risk_freeze_id
      AND freeze.source_session_risk_score_id=NEW.session_risk_score_id
      AND ((NEW.action IN ('freeze_session','release_session')
          AND freeze.target_type='session')
        OR (NEW.action IN ('freeze_virtual_key','release_virtual_key')
          AND freeze.target_type='virtual_key'))
  ))
  OR (NEW.actor_type='user' AND NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_id
      AND actor.status='active'
  ))
  OR EXISTS (
    SELECT 1 FROM session_risk_actions current
    WHERE current.session_risk_action_id=NEW.session_risk_action_id
       OR (NEW.idempotency_key_digest IS NOT NULL
           AND current.tenant_id=NEW.tenant_id AND current.actor_id=NEW.actor_id
           AND current.digest_key_version=NEW.digest_key_version
           AND current.idempotency_key_digest=NEW.idempotency_key_digest
           -- BEGIN IMMEDIATE 串行化同库写入；只拒绝在 NEW.created_at 时仍有效的事实，
           -- 已过 24 小时的历史 action 保持 append-only，同时释放幂等键供新意图使用。
           AND julianday(current.idempotency_expires_at)>julianday(NEW.created_at))
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'session_risk_action_owner_mismatch'); END;
CREATE TRIGGER trg_session_risk_actions_update_prd070
BEFORE UPDATE ON session_risk_actions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_action_immutable'); END;
CREATE TRIGGER trg_session_risk_actions_delete_prd070
BEFORE DELETE ON session_risk_actions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_action_immutable'); END;

-- Source event 复制一次状态跃迁的最小安全摘要，供异步 Webhook 派发且不阻塞 Gateway。
CREATE TABLE session_risk_events (
  session_risk_event_id TEXT PRIMARY KEY CHECK (
    length(session_risk_event_id)=38 AND substr(session_risk_event_id,1,6)='srevt_'
    AND substr(session_risk_event_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  session_risk_score_id TEXT NOT NULL,
  session_risk_action_id TEXT NOT NULL,
  session_risk_freeze_id TEXT,
  event_type TEXT NOT NULL CHECK (event_type='security.session_high_risk'),
  scope_type TEXT NOT NULL CHECK (scope_type IN ('enterprise_chat','api_session')),
  scope_key TEXT NOT NULL CHECK (length(scope_key) BETWEEN 1 AND 160),
  status TEXT NOT NULL CHECK (status IN ('high_risk','frozen','resolved','expired')),
  target_type TEXT CHECK (target_type IS NULL OR target_type IN ('session','virtual_key')),
  score INTEGER NOT NULL CHECK (typeof(score)='integer' AND score BETWEEN 1 AND 10000),
  score_scale INTEGER NOT NULL CHECK (score_scale=10000),
  risky_turn_count INTEGER NOT NULL CHECK (
    typeof(risky_turn_count)='integer' AND risky_turn_count>=1
  ),
  jailbreak_count INTEGER NOT NULL CHECK (
    typeof(jailbreak_count)='integer' AND jailbreak_count>=0
  ),
  sensitive_count INTEGER NOT NULL CHECK (
    typeof(sensitive_count)='integer' AND sensitive_count>=0
  ),
  tool_inducement_count INTEGER NOT NULL CHECK (
    typeof(tool_inducement_count)='integer' AND tool_inducement_count>=0
  ),
  state_revision INTEGER NOT NULL CHECK (
    typeof(state_revision)='integer' AND state_revision>=1
  ),
  threshold_version TEXT NOT NULL CHECK (threshold_version='session-risk-threshold-v1'),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('system','user')),
  actor_id TEXT NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 128),
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK (unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,session_risk_score_id)
    REFERENCES session_risk_scores(tenant_id,session_risk_score_id),
  FOREIGN KEY (tenant_id,session_risk_action_id)
    REFERENCES session_risk_actions(tenant_id,session_risk_action_id),
  FOREIGN KEY (tenant_id,session_risk_freeze_id)
    REFERENCES session_risk_freezes(tenant_id,session_risk_freeze_id),
  UNIQUE (tenant_id,session_risk_event_id),
  UNIQUE (tenant_id,session_risk_score_id,state_revision),
  CHECK (
    (session_risk_freeze_id IS NULL AND target_type IS NULL)
    OR (session_risk_freeze_id IS NOT NULL AND target_type IS NOT NULL)
  ),
  CHECK (
    jailbreak_count<=risky_turn_count AND sensitive_count<=risky_turn_count
    AND tool_inducement_count<=risky_turn_count
  )
);
CREATE INDEX idx_session_risk_events_status_time_prd070
  ON session_risk_events(tenant_id,status,occurred_at DESC,session_risk_event_id DESC);
CREATE TRIGGER trg_session_risk_events_owner_insert_prd070
BEFORE INSERT ON session_risk_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM session_risk_scores score
  JOIN session_risk_actions action
    ON action.tenant_id=score.tenant_id
   AND action.session_risk_score_id=score.session_risk_score_id
  WHERE score.tenant_id=NEW.tenant_id
    AND score.session_risk_score_id=NEW.session_risk_score_id
    AND action.session_risk_action_id=NEW.session_risk_action_id
    AND score.scope_type=NEW.scope_type AND score.scope_key=NEW.scope_key
    AND score.status=NEW.status AND score.score=NEW.score
    AND score.score_scale=NEW.score_scale
    AND score.risky_turn_count=NEW.risky_turn_count
    AND score.jailbreak_count=NEW.jailbreak_count
    AND score.sensitive_count=NEW.sensitive_count
    AND score.tool_inducement_count=NEW.tool_inducement_count
    AND score.state_revision=NEW.state_revision
    AND score.threshold_version=NEW.threshold_version
    AND action.state_revision=NEW.state_revision
    AND action.actor_type=NEW.actor_type AND action.actor_id=NEW.actor_id
    AND action.source_request_id=NEW.source_request_id
    AND action.source_trace_id=NEW.source_trace_id
    AND action.created_at=NEW.occurred_at
)
  OR (NEW.session_risk_freeze_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM session_risk_freezes freeze
    WHERE freeze.tenant_id=NEW.tenant_id
      AND freeze.session_risk_freeze_id=NEW.session_risk_freeze_id
      AND freeze.source_session_risk_score_id=NEW.session_risk_score_id
      AND freeze.target_type=NEW.target_type
  ))
  OR EXISTS (
    SELECT 1 FROM session_risk_events current
    WHERE current.session_risk_event_id=NEW.session_risk_event_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.session_risk_score_id=NEW.session_risk_score_id
           AND current.state_revision=NEW.state_revision)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'session_risk_event_owner_mismatch'); END;
CREATE TRIGGER trg_session_risk_events_update_prd070
BEFORE UPDATE ON session_risk_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_event_immutable'); END;
CREATE TRIGGER trg_session_risk_events_delete_prd070
BEFORE DELETE ON session_risk_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_event_immutable'); END;

-- AuditLog 沿用 0061 的 17 列、rowid、索引和全部旧 owner 约束，只扩展五类管理员处置动作。
-- 换表前后的双向差集和 NULL 分布由继承的 PRD-068 guard 验证，历史审计事实不得漂移。
DROP TABLE IF EXISTS audit_logs_prd070_new;
CREATE TABLE audit_logs_prd070_new (
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
      'session_risk.freeze_session','session_risk.freeze_virtual_key',
      'session_risk.dismiss','session_risk.release_session',
      'session_risk.release_virtual_key'
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
      'policy_template_application','session_risk'
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
INSERT INTO audit_logs_prd070_new(
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
CREATE TEMP TABLE prd070_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 复制后逐值核对行数与 17 列内容，任何差异都在删除旧审计表前失败关闭。
INSERT INTO prd070_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd070_new))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd070_new WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd070_new WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd070_new WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd070_new
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd070_new
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd070_audit_copy_guard;

-- legacy_alter_table 保持四个已核验的外部 owner trigger 原样指向正式表名；旧表本地
-- trigger/index 随 DROP 移除，重命名后按冻结定义逐项恢复。
DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后替换正式审计表，确保历史治理证据完整保真。
ALTER TABLE audit_logs_prd070_new RENAME TO audit_logs;
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

-- 每个管理员处置 source action 只允许一条同 score/request 审计；重复写入不能伪造第二份事实。
CREATE UNIQUE INDEX idx_audit_logs_session_risk_action_unique_prd070
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE action IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  );

-- SessionRisk Audit 必须反向绑定同租户 action、score、event、actor、request/trace 和逐字段摘要。
-- object_name 只保存 scope_type；API HMAC、幂等键、Prompt、Response 和命中片段均不得进入审计。
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

-- 提交前精确复核 revision 7、59/155、五表、RequestLog 66 列、owner trigger 和数据库完整性。
DROP TABLE IF EXISTS temp.prd070_up_final_guard;
CREATE TEMP TABLE prd070_up_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd070_up_final_ok CHECK (issue_count=0)
);
-- 同一事务汇总目录、表形状、角色矩阵与数据库完整性，任一偏差都会阻止提交。
INSERT INTO prd070_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=59
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=7)=59
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=155
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*59 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=7)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd070_roles_to_expand source
     JOIN roles role ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=source.permission_revision+1
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN '792e003e6c0a188c124ef71ce7ab31658e7ef43a6dadaf6b83c339dfff2caf7f'
          WHEN 'security_admin' THEN 'de4701bc3ce4305c129704c69212d8a520ea5942a87c6119a36542e01784b78c' END)
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=66
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='request_logs')=13 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('intent_risk_results'))=22
      AND (SELECT COUNT(*) FROM pragma_table_info('intent_risk_events'))=19
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_scores'))=28
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_observations'))=27
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_freezes'))=19
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_actions'))=19
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_events'))=23
    THEN 0 ELSE 1 END
  -- Audit 换表属于本迁移不可分割的一部分：除 17 列形状外，还要证明全部旧索引/trigger
  -- 与会话风险专用唯一键、owner trigger 同时存在，避免仅扩展 CHECK 却丢失旧治理约束。
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('audit_logs'))=17
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND tbl_name='audit_logs' AND name NOT LIKE 'sqlite_autoindex%')=15
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='audit_logs')=8
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND name='idx_audit_logs_session_risk_action_unique_prd070'
           AND tbl_name='audit_logs')=1
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND name='trg_audit_logs_session_risk_owner_insert_prd070'
           AND tbl_name='audit_logs')=1
      AND EXISTS (
        SELECT 1 FROM sqlite_schema object
        WHERE object.type='table' AND object.name='audit_logs'
          AND instr(object.sql,"'session_risk.freeze_session'")>0
          AND instr(object.sql,"'session_risk.freeze_virtual_key'")>0
          AND instr(object.sql,"'session_risk.dismiss'")>0
          AND instr(object.sql,"'session_risk.release_session'")>0
          AND instr(object.sql,"'session_risk.release_virtual_key'")>0
          AND instr(object.sql,"'session_risk'")>0
      )
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd070_up_final_guard;
DROP TABLE prd070_roles_to_expand;

-- 已形成的 Intent/session-risk 安全事实不可由普通 usage upsert 回退或换 identity。
CREATE TRIGGER trg_request_logs_intent_update_prd069
BEFORE UPDATE OF
  tenant_id,request_id,intent_risk_result_id,intent_risk_schedule_status,intent_risk_mode,
  intent_risk_type,intent_risk_score,intent_risk_action,intent_risk_reason_code,
  intent_risk_detector_version,intent_risk_input_truncated,session_risk_status,
  session_risk_score_id,session_risk_scope_type,session_risk_state,session_risk_reason_code
ON request_logs FOR EACH ROW
WHEN
  (OLD.intent_risk_result_id IS NOT NULL
    AND NEW.intent_risk_result_id IS NOT OLD.intent_risk_result_id)
  OR (OLD.intent_risk_schedule_status!='disabled'
    AND NEW.intent_risk_schedule_status IS NOT OLD.intent_risk_schedule_status)
  OR (OLD.intent_risk_mode IS NOT NULL AND NEW.intent_risk_mode IS NOT OLD.intent_risk_mode)
  OR (OLD.intent_risk_detector_version IS NOT NULL
    AND NEW.intent_risk_detector_version IS NOT OLD.intent_risk_detector_version)
  OR (OLD.session_risk_status!='not_applicable'
    AND NEW.session_risk_status IS NOT OLD.session_risk_status)
  OR (OLD.session_risk_score_id IS NOT NULL
    AND NEW.session_risk_score_id IS NOT OLD.session_risk_score_id)
  OR (OLD.session_risk_scope_type IS NOT NULL
    AND NEW.session_risk_scope_type IS NOT OLD.session_risk_scope_type)
  OR (OLD.session_risk_state IS NOT NULL
    AND NEW.session_risk_state IS NOT OLD.session_risk_state)
  OR (
    (
      (NEW.intent_risk_schedule_status='disabled'
        AND NEW.intent_risk_result_id IS NULL AND NEW.intent_risk_mode IS NULL
        AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
        AND NEW.intent_risk_action IS NULL AND NEW.intent_risk_reason_code IS NULL
        AND NEW.intent_risk_detector_version IS NULL AND NEW.intent_risk_input_truncated=0)
      OR (NEW.intent_risk_schedule_status='queued'
        AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='async_light'
        AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
        AND NEW.intent_risk_action IS NULL AND NEW.intent_risk_reason_code='queued'
        AND NEW.intent_risk_detector_version IS NOT NULL)
      OR (NEW.intent_risk_schedule_status='skipped'
        AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='async_light'
        AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
        AND NEW.intent_risk_action IS NULL
        AND NEW.intent_risk_reason_code IN ('queue_full','worker_unavailable')
        AND NEW.intent_risk_detector_version IS NOT NULL)
      OR (NEW.intent_risk_schedule_status='blocked'
        AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='sync_strong'
        AND NEW.intent_risk_type IS NOT NULL AND NEW.intent_risk_score BETWEEN 9500 AND 10000
        AND NEW.intent_risk_action='block' AND NEW.intent_risk_detector_version IS NOT NULL)
      OR (NEW.intent_risk_schedule_status='completed'
        AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='sync_session'
        AND EXISTS (
          SELECT 1 FROM intent_risk_results result
          WHERE result.tenant_id=NEW.tenant_id AND result.source_kind='gateway'
            AND result.source_request_id=NEW.request_id
            AND result.intent_risk_result_id=NEW.intent_risk_result_id
            AND result.classification_mode='sync_session'
            AND NEW.intent_risk_type IS CASE
              WHEN result.risk_type='none' THEN NULL ELSE result.risk_type END
            AND NEW.intent_risk_score=result.risk_score
            AND NEW.intent_risk_action=result.action
            AND NEW.intent_risk_reason_code=result.reason_code
            AND NEW.intent_risk_detector_version=result.detector_version
            AND NEW.intent_risk_input_truncated=result.input_truncated
        ))
      OR (NEW.intent_risk_schedule_status='failed_closed'
        AND NEW.intent_risk_mode IN ('sync_strong','sync_session')
        AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
        AND NEW.intent_risk_action='none'
        AND NEW.intent_risk_reason_code IN ('classifier_failed','result_store_unavailable')
        AND NEW.intent_risk_detector_version IS NOT NULL)
    )
    AND (
      (NEW.session_risk_status='not_applicable'
        AND NEW.session_risk_score_id IS NULL AND NEW.session_risk_scope_type IS NULL
        AND NEW.session_risk_state IS NULL AND NEW.session_risk_reason_code IS NULL)
      OR (NEW.session_risk_status='disabled'
        AND NEW.session_risk_score_id IS NULL AND NEW.session_risk_scope_type IS NULL
        AND NEW.session_risk_state IS NULL
        AND NEW.session_risk_reason_code IN ('feature_disabled','no_session_identifiers'))
      OR (NEW.session_risk_status='clean'
        AND NEW.session_risk_score_id IS NULL AND NEW.session_risk_scope_type IS NOT NULL
        AND NEW.session_risk_state IS NULL AND NEW.session_risk_reason_code='clean_turn')
      OR (NEW.session_risk_status IN ('monitoring','high_risk','frozen')
        AND NEW.session_risk_score_id IS NOT NULL AND NEW.session_risk_scope_type IS NOT NULL
        AND NEW.session_risk_state=NEW.session_risk_status
        AND NEW.session_risk_reason_code IN ('risk_accumulated','session_frozen','key_frozen'))
      OR (NEW.session_risk_status='duplicate'
        AND NEW.session_risk_scope_type IS NOT NULL
        AND NEW.session_risk_reason_code='duplicate_turn')
      OR (NEW.session_risk_status='degraded'
        AND NEW.session_risk_score_id IS NULL AND NEW.session_risk_scope_type IS NOT NULL
        AND NEW.session_risk_state IS NULL AND NEW.session_risk_reason_code='store_degraded')
      OR (NEW.session_risk_status='failed'
        AND NEW.session_risk_reason_code IN (
          'store_unavailable','evidence_unavailable','session_frozen','key_frozen',
          'turn_conflict','hmac_version_unavailable'
        ))
    )
  ) IS NOT TRUE
BEGIN SELECT RAISE(ABORT,'request_log_session_risk_state_invalid'); END;

-- 最后一个跨表 update trigger 必须也在同一事务中创建并验收，禁止先提交后补 DDL 留下半迁移。
DROP TABLE IF EXISTS temp.prd070_up_trigger_guard;
CREATE TEMP TABLE prd070_up_trigger_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd070_up_trigger_ok CHECK (issue_count=0)
);
-- 最终跨表触发器创建后再次复核数量与完整性，避免提交缺少安全更新门禁的半迁移。
INSERT INTO prd070_up_trigger_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
             AND tbl_name='request_logs')=14 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd070_up_trigger_guard;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
