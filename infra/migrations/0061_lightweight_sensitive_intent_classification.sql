-- PRD-069：轻量敏感意图分类结果、source event、RequestLog 调度证据与 RBAC revision 6。
-- 安全边界：两张事实表只保存有界安全元数据和 tenant-bound ID，禁止保存 Prompt、Response、
-- 命中片段、输入 hash、Provider Key、Token、Webhook Secret 或任意异常正文。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前冻结 0060 栈顶和精确基线；任何未知迁移、同名对象或损坏 owner 图都失败关闭。
DROP TABLE IF EXISTS temp.prd069_up_guard;
CREATE TEMP TABLE prd069_up_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd069_up_preflight_ok CHECK (issue_count=0)
);
-- 将栈顶、目录、日志形状、对象空间和数据库完整性折叠为单一原子门禁计数。
INSERT INTO prd069_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0060' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0060')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=56
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=5)=56
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=149
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*56 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=5)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
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
       'policy_id,policy_action_type,policy_target_model_id,policy_mask_applied,policy_mask_policy_id')
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND tbl_name='request_logs' AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND tbl_name='request_logs')=12 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('gateway_request_claims'))=7
      AND (SELECT COUNT(*) FROM pragma_table_info('users') WHERE name IN ('tenant_id','user_id'))=2
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'intent_risk_results','intent_risk_events','rbac_permission_templates_prd069_new',
      'permissions_prd069_new','trg_intent_risk_results_owner_insert_prd069',
      'trg_intent_risk_results_update_prd069','trg_intent_risk_results_delete_prd069',
      'trg_intent_risk_events_owner_insert_prd069','trg_intent_risk_events_update_prd069',
      'trg_intent_risk_events_delete_prd069','trg_request_logs_intent_insert_prd069',
      'trg_request_logs_intent_update_prd069'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd069_up_guard;

-- 只冻结仍等于 revision 5 产品矩阵的 Admin/SecurityAdmin。关系全集与 hash 必须同时匹配，
-- 防止仅凭角色名称给租户自定义矩阵静默补权。
CREATE TEMP TABLE prd069_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','security_admin')
  AND ((role.code='admin' AND role.permission_revision BETWEEN 1 AND 5)
       OR (role.code='security_admin' AND role.permission_revision BETWEEN 1 AND 2))
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
    WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
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

-- Admin 是不可修改的恢复入口，因此每个 tenant 的 Admin 都必须进入 canonical 扩展集合。
DROP TABLE IF EXISTS temp.prd069_admin_guard;
CREATE TEMP TABLE prd069_admin_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd069_admin_canonical CHECK (issue_count=0)
);
-- 所有 Admin 都必须仍为 canonical，否则不可变恢复入口已损坏，升级不得继续。
INSERT INTO prd069_admin_guard(issue_count)
SELECT COUNT(*) FROM roles role
WHERE role.code='admin' AND NOT EXISTS (
  SELECT 1 FROM prd069_roles_to_expand source
  WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
);
DROP TABLE prd069_admin_guard;

-- 产品 Permission 目录提升为 revision 6；已有 56 项逐行保留 rowid 和展示元数据。
CREATE TABLE rbac_permission_templates_prd069_new (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=6)
);
-- 旧目录逐行保留 rowid，只把统一 catalog revision 提升到 6。
INSERT INTO rbac_permission_templates_prd069_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,6
FROM rbac_permission_templates;
-- 新权限使用固定中文元数据，不接受租户或运行时输入覆盖产品目录。
INSERT INTO rbac_permission_templates_prd069_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES(
  'intent_risk.classify','security','分类敏感意图',
  '对一次性文本执行本地敏感意图分类，不保存输入正文或命中片段。','write',6
);

DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TABLE rbac_permission_templates;
-- 全量复制完成后才切换正式表名，避免关系模板观察到半完成目录。
ALTER TABLE rbac_permission_templates_prd069_new RENAME TO rbac_permission_templates;

-- 新能力只属于 Admin 与 SecurityAdmin 的产品上限；其余四类角色不获得默认授权。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES ('admin','intent_risk.classify',1),('security_admin','intent_risk.classify',1);
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

-- 每租户 Permission 投影同步提升到 revision 6；旧实体 ID、rowid 和时间保持不变。
CREATE TABLE permissions_prd069_new (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=6),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 现有租户 Permission 保留稳定 ID、rowid、状态和时间，只提升统一 revision。
INSERT INTO permissions_prd069_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       6,status,created_at,updated_at
FROM permissions;
-- 每个 tenant 独立生成新 Permission ID，禁止跨租户复用权限实体。
INSERT INTO permissions_prd069_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       6,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template ON template.permission_code='intent_risk.classify';
DROP TABLE permissions;
-- 租户投影复制完整后恢复正式表名，再重建不可变 owner 约束。
ALTER TABLE permissions_prd069_new RENAME TO permissions;
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

-- 只给迁移前冻结的 canonical 角色补关系，自定义 SecurityAdmin 的关系、revision 和 hash 不变。
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
-- 新关系只连接同 tenant 的冻结角色与新 Permission，不能拼接其他租户实体。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd069_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE permission.code='intent_risk.classify';
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

-- canonical 角色每次产品补权只推进一次 revision；新租户仍从 revision 1 开始。
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
-- 关系与授权快照在同一事务推进，只有实际补权的角色 revision 增加一次。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
      WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94'
    END
WHERE role_id IN (SELECT role_id FROM prd069_roles_to_expand);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
    WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
    WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94'
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

-- 新 tenant 直接物化 revision 6 目录，但所有角色 permission_revision 仍按既有合同初始化为 1。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 先建立六个 revision 1 角色；hash 表示 revision 6 默认集合而非历史修改次数。
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
      WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
      WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
      WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 再为新 tenant 生成完整 57 项独立 Permission 投影。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后依据产品模板建立同 tenant 默认关系，避免初始化期间出现悬空 owner。
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

-- 终态结果使用单一来源唯一键；Gateway owner 只锚定先于 RequestLog 存在的 claim。
CREATE TABLE intent_risk_results (
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
    classification_mode IN ('sync_strong','async_light','manual_sync')
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
  error_code TEXT CHECK (
    error_code IS NULL OR error_code='intent_risk.classifier_failed'
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  completed_at TEXT NOT NULL CHECK (
    unixepoch(completed_at) IS NOT NULL AND unixepoch(completed_at)>=unixepoch(created_at)
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id,intent_risk_result_id),
  UNIQUE (tenant_id,source_kind,source_request_id),
  CHECK (
    (source_kind='gateway' AND actor_type='virtual_key'
      AND classification_mode IN ('sync_strong','async_light'))
    OR (source_kind='manual' AND actor_type='user' AND classification_mode='manual_sync'
      AND input_truncated=0)
  ),
  CHECK (
    (status='clean' AND classification_mode IN ('async_light','manual_sync')
      AND risk_type='none' AND risk_score=0 AND action='allow'
      AND reason_code='no_risk_detected' AND error_code IS NULL)
    OR (status='detected' AND classification_mode='async_light'
      AND risk_type!='none' AND risk_score>0 AND action='observe' AND error_code IS NULL)
    OR (status='detected' AND classification_mode='manual_sync'
      AND risk_type!='none' AND risk_score>0 AND action='review' AND error_code IS NULL)
    OR (status='blocked' AND source_kind='gateway' AND classification_mode='sync_strong'
      AND risk_type!='none' AND risk_score BETWEEN 9500 AND 10000
      AND action='block' AND error_code IS NULL)
    OR (status='failed'
      AND ((source_kind='gateway' AND classification_mode IN ('sync_strong','async_light'))
        OR (source_kind='manual' AND classification_mode='manual_sync'))
      AND risk_type='none' AND risk_score=0 AND action='none'
      AND reason_code='classifier_failed' AND error_code='intent_risk.classifier_failed')
  ),
  -- risk_type 与固定原因码必须一一属于同一 taxonomy，防止伪造不一致安全摘要。
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
CREATE INDEX idx_intent_risk_results_status_time_prd069
  ON intent_risk_results(tenant_id,status,completed_at DESC,intent_risk_result_id DESC);
CREATE INDEX idx_intent_risk_results_type_time_prd069
  ON intent_risk_results(tenant_id,risk_type,completed_at DESC);

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

-- source event 完整复制 result 的非敏感摘要，并通过 tenant-qualified 唯一键阻断跨租户拼接。
CREATE TABLE intent_risk_events (
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
    classification_mode IN ('sync_strong','async_light','manual_sync')
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
CREATE INDEX idx_intent_risk_events_tenant_time_prd069
  ON intent_risk_events(tenant_id,created_at DESC,intent_risk_event_id DESC);
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

-- RequestLog up 只追加列，不重建 52 列主表，避免触碰现有 24 个索引和 12 个 trigger。
ALTER TABLE request_logs ADD COLUMN intent_risk_result_id TEXT CHECK (
  intent_risk_result_id IS NULL OR (
    length(intent_risk_result_id) BETWEEN 5 AND 128 AND intent_risk_result_id GLOB 'irr_*'
  )
);
-- 调度状态区分未启用、已入队、跳过、同步阻断和强规则失败关闭。
ALTER TABLE request_logs ADD COLUMN intent_risk_schedule_status TEXT NOT NULL DEFAULT 'disabled'
  CHECK (intent_risk_schedule_status IN ('disabled','queued','skipped','blocked','failed_closed'));
-- 日志只记录 Gateway 的异步轻分类或同步强规则模式，不接受 manual_sync。
ALTER TABLE request_logs ADD COLUMN intent_risk_mode TEXT
  CHECK (intent_risk_mode IS NULL OR intent_risk_mode IN ('async_light','sync_strong'));
-- 只有同步 blocked 才在日志冻结风险类型，异步终态从独立结果表读取。
ALTER TABLE request_logs ADD COLUMN intent_risk_type TEXT CHECK (
  intent_risk_type IS NULL OR intent_risk_type IN (
    'credential_exfiltration','system_prompt_extraction','policy_bypass','jailbreak'
  )
);
-- 分数使用 0..10000 整数基点，禁止浮点量纲进入跨服务日志。
ALTER TABLE request_logs ADD COLUMN intent_risk_score INTEGER CHECK (
  intent_risk_score IS NULL OR (
    typeof(intent_risk_score)='integer' AND intent_risk_score BETWEEN 0 AND 10000
  )
);
-- 日志动作只允许同步阻断的 block 或失败关闭的 none；异步调度阶段保持空值。
ALTER TABLE request_logs ADD COLUMN intent_risk_action TEXT
  CHECK (intent_risk_action IS NULL OR intent_risk_action IN ('block','none'));
-- 原因码是固定安全摘要，禁止异常正文或命中片段写入 RequestLog。
ALTER TABLE request_logs ADD COLUMN intent_risk_reason_code TEXT CHECK (
  intent_risk_reason_code IS NULL OR intent_risk_reason_code IN (
    'queued','queue_full','worker_unavailable','classifier_failed','result_store_unavailable',
    'explicit_credential_extraction','explicit_system_prompt_request',
    'explicit_policy_bypass','explicit_instruction_override'
  )
);
-- detector 版本用于历史追踪并严格有界，不能携带正文或 NUL 分隔数据。
ALTER TABLE request_logs ADD COLUMN intent_risk_detector_version TEXT CHECK (
  intent_risk_detector_version IS NULL OR (
    length(intent_risk_detector_version) BETWEEN 1 AND 128
    AND instr(intent_risk_detector_version,char(0))=0
  )
);
-- 截断标志默认 0，使 0060 历史行明确表示旧版本未启用本分类链路。
ALTER TABLE request_logs ADD COLUMN intent_risk_input_truncated INTEGER NOT NULL DEFAULT 0
  CHECK (intent_risk_input_truncated IN (0,1));

-- 组合 trigger 是 RequestLog 安全摘要的最终防线；blocked/可选 failed 必须命中同 tenant/result/request。
CREATE TRIGGER trg_request_logs_intent_insert_prd069
BEFORE INSERT ON request_logs FOR EACH ROW
WHEN (
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
        AND result.action=NEW.intent_risk_action AND result.reason_code=NEW.intent_risk_reason_code
        AND result.detector_version=NEW.intent_risk_detector_version
        AND result.input_truncated=NEW.intent_risk_input_truncated
    ))
  OR (NEW.intent_risk_schedule_status='failed_closed'
    AND NEW.intent_risk_mode='sync_strong' AND NEW.intent_risk_type IS NULL
    AND NEW.intent_risk_score IS NULL AND NEW.intent_risk_action='none'
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
            AND result.status='failed' AND result.classification_mode='sync_strong'
            AND result.detector_version=NEW.intent_risk_detector_version
            AND result.input_truncated=NEW.intent_risk_input_truncated
        )))
    ))
) IS NOT TRUE
BEGIN SELECT RAISE(ABORT,'request_log_intent_state_invalid'); END;

CREATE TRIGGER trg_request_logs_intent_update_prd069
BEFORE UPDATE OF
  tenant_id,request_id,intent_risk_result_id,intent_risk_schedule_status,intent_risk_mode,
  intent_risk_type,intent_risk_score,intent_risk_action,intent_risk_reason_code,
  intent_risk_detector_version,intent_risk_input_truncated
ON request_logs FOR EACH ROW
WHEN
  -- 已形成的安全事实不得被普通 upsert 覆盖、回退或切换 detector/result identity。
  (OLD.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_result_id IS NOT OLD.intent_risk_result_id)
  OR (OLD.intent_risk_schedule_status!='disabled'
      AND NEW.intent_risk_schedule_status IS NOT OLD.intent_risk_schedule_status)
  OR (OLD.intent_risk_mode IS NOT NULL AND NEW.intent_risk_mode IS NOT OLD.intent_risk_mode)
  OR (OLD.intent_risk_detector_version IS NOT NULL
      AND NEW.intent_risk_detector_version IS NOT OLD.intent_risk_detector_version)
  OR (
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
          AND result.action=NEW.intent_risk_action AND result.reason_code=NEW.intent_risk_reason_code
          AND result.detector_version=NEW.intent_risk_detector_version
          AND result.input_truncated=NEW.intent_risk_input_truncated
      ))
    OR (NEW.intent_risk_schedule_status='failed_closed'
      AND NEW.intent_risk_mode='sync_strong' AND NEW.intent_risk_type IS NULL
      AND NEW.intent_risk_score IS NULL AND NEW.intent_risk_action='none'
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
              AND result.status='failed' AND result.classification_mode='sync_strong'
              AND result.detector_version=NEW.intent_risk_detector_version
              AND result.input_truncated=NEW.intent_risk_input_truncated
          )))
      ))
  ) IS NOT TRUE
BEGIN SELECT RAISE(ABORT,'request_log_intent_state_invalid'); END;

-- 提交前复核目录计数、租户投影、目标表和 RequestLog 形状，防止半完成迁移提交。
DROP TABLE IF EXISTS temp.prd069_up_final_guard;
CREATE TEMP TABLE prd069_up_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd069_up_final_ok CHECK (issue_count=0)
);
-- 提交前统一复核 57/151、角色 hash、表列数、外键和 SQLite 页面完整性。
INSERT INTO prd069_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=57
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=6)=57
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=151
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*57 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=6)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd069_roles_to_expand source
     JOIN roles role ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=source.permission_revision+1
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
          WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94' END)
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=61
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='request_logs')=14 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('intent_risk_results'))=22
      AND (SELECT COUNT(*) FROM pragma_table_info('intent_risk_events'))=19
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd069_up_final_guard;
DROP TABLE prd069_roles_to_expand;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
