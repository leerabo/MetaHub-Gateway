-- PRD-067：企业 Prompt 模板库的 RBAC revision 4、加密版本元数据与安全调用事实。
-- 安全边界：模板正文、变量 schema、变量值与 change summary 只存在于独立 AES-GCM 密文；
-- Audit、Webhook、Invocation 和 Operation 只保存稳定 ID、有界计数、状态与 keyed HMAC。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首项持久 DDL 前验证数据库精确位于 0058 栈顶。TEMP CHECK 失败会原子中止，避免在未知
-- Audit/RBAC/Chat 结构上换表或增加复合父键，产生历史证据丢失与跨租户拼接。
DROP TABLE IF EXISTS temp.prd067_up_guard;
CREATE TEMP TABLE prd067_up_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd067_up_preflight_ok CHECK (issue_count = 0)
);
-- 把栈顶、对象清单、RBAC revision 3、Audit 17 列及现有 Chat 父键折叠为一个原子计数。
INSERT INTO prd067_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0058' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
     WHERE status='applied' AND version>'0058')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','users','sessions','projects','environments','model_aliases',
        'virtual_keys','roles','permissions','role_permissions',
        'rbac_permission_templates','rbac_role_permission_templates',
        'role_permission_events','webhook_endpoints','webhook_deliveries',
        'webhook_delivery_attempts','webhook_dead_letter_events',
        'audit_logs','audit_exports','audit_log_export_sequences','invitations',
        'config_imports','config_exports','chat_sessions','chat_messages',
        'chat_execution_grants'
      ))=26 THEN 0 ELSE 1 END
  -- 七张模板域表、新增索引和换表暂存名必须全部空闲；未知同名对象不能被覆盖。
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'prompt_templates','prompt_template_versions',
      'prompt_template_version_projects','prompt_template_version_models',
      'chat_prompt_template_invocations','prompt_template_events',
      'prompt_template_operations','rbac_permission_templates_prd067_new',
      'permissions_prd067_new','audit_logs_prd067_new',
      'uq_chat_sessions_template_owner_prd067',
      'uq_chat_messages_template_message_prd067',
      'uq_chat_messages_template_owner_request_prd067',
      'idx_audit_logs_prompt_template_request_unique'
    ))
  -- 0059 读取的 Chat 父表列必须与 0058 冻结结构一致，不能把独立 ID 拼成伪 owner 图。
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('chat_sessions') ORDER BY cid
      )
    )='chat_session_id,tenant_id,owner_user_id,owner_auth_session_id,project_id,environment_id,model_alias_id,model_alias,virtual_key_id,status,message_sequence,state_revision,expires_at,cleared_at,created_at,updated_at'
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('chat_messages') ORDER BY cid
      )
    )='chat_message_id,tenant_id,chat_session_id,owner_user_id,client_message_id,message_sequence,gateway_request_id,gateway_trace_id,status,input_tokens,output_tokens,total_tokens,cache_tokens,cost_cny_micros,policy_decision,policy_reason_code,fallback_triggered,cache_status,error_code,webhook_event_id,started_at,completed_at'
    THEN 0 ELSE 1 END
  -- AuditLog 必须是 0054 延续至 0058 的 17 列终态，历史 NULL 和 rowid 将在换表时逐值保真。
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('audit_logs') ORDER BY cid
      )
    )='audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at'
    THEN 0 ELSE 1 END
  -- 0058 的 Chat、用户、Project、Alias 候选键必须原样存在，0059 只增补 Invocation 所需组合。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN (
      'idx_users_tenant_user_prd059','uq_projects_tenant_project_prd051',
      'uq_model_aliases_tenant_id_prd047','uq_chat_sessions_tenant_session_prd066',
      'uq_chat_messages_tenant_message_prd066'
    ))=5 THEN 0 ELSE 1 END
  -- 目录必须是 0058 交付的完整 revision 3，不能在未知本地权限集上猜测升级。
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=51
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=3)=51
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code='chat.use' AND risk_level='write')=1
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code LIKE 'prompt_template.%')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=134
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code='chat.use')=3
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*51 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=3)
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
  -- Admin 不可变矩阵必须仍为 revision 3 默认全集；Developer/Viewer 的定制矩阵允许保留。
  + (SELECT COUNT(*) FROM roles role
     WHERE role.code='admin' AND (
       role.permission_revision NOT BETWEEN 1 AND 3
       OR role.matrix_sha256!='af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
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
  -- Audit 本地/外部 trigger 和 11 个显式索引必须完整，避免换表时悄然丢失后续 PRD 约束。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND tbl_name='audit_logs'
      AND name IN (
        'idx_audit_logs_tenant_time','idx_audit_logs_tenant_actor_time',
        'idx_audit_logs_tenant_object_time','idx_audit_logs_tenant_action_time',
        'idx_audit_logs_tenant_request','idx_audit_logs_dry_run_request_unique',
        'idx_audit_logs_model_quality_request_unique','idx_audit_logs_finops_adjustment_unique',
        'idx_audit_logs_role_permission_request_unique','idx_audit_logs_invitation_request_unique',
        'idx_audit_logs_config_transfer_request_unique'
      ))=11 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN (
      'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert','trg_audit_logs_export_sequence_insert_prd058',
      'trg_audit_exports_owner_insert_prd058',
      'trg_audit_log_export_sequences_owner_insert_prd058',
      'trg_invitation_events_owner_insert_prd060',
      'trg_role_permission_events_owner_insert_prd059'
    ))=10 THEN 0 ELSE 1 END
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
DROP TABLE prd067_up_guard;

-- 只冻结“revision 3 hash + 合法历史 revision + 真实关系全集”同时匹配的角色。自定义矩阵
-- 即使角色名相同也不会被静默扩权；历史 revision 范围来自 0058 的可逆合同。
CREATE TEMP TABLE prd067_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','viewer')
  AND role.permission_revision BETWEEN 1
      AND CASE WHEN role.code IN ('admin','developer') THEN 3 ELSE 2 END
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
    WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
    WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
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

-- 产品 Permission 目录整体提升到 revision 4；旧 51 行保留 rowid 和展示元数据。
CREATE TABLE rbac_permission_templates_prd067_new (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=4)
);
-- 旧目录逐行保留 rowid 与展示语义，只把统一 catalog revision 提升到本次冻结版本。
INSERT INTO rbac_permission_templates_prd067_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,4
FROM rbac_permission_templates;
-- 模板管理、读取与调用分成三项权限，避免 Viewer 因调用权限获得密文定义或管理接口。
INSERT INTO rbac_permission_templates_prd067_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('prompt_template.read','prompt_template','读取 Prompt 模板',
   '读取企业 Prompt 模板元数据、版本历史和授权后的模板定义。','read',4),
  ('prompt_template.write','prompt_template','管理 Prompt 模板',
   '创建、修改、启停企业 Prompt 模板并发布不可变版本。','critical',4),
  ('prompt_template.use','prompt_template','使用 Prompt 模板',
   '在企业 Chat 中选择并调用当前已发布的 Prompt 模板版本。','write',4);

-- 模板是发布元数据，迁移仅在同一事务内短暂移除不可变门禁。
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TABLE rbac_permission_templates;
-- 换表只发生在同一事务且数据已完整复制后，保证目录主键和角色模板外键继续指向正式表名。
ALTER TABLE rbac_permission_templates_prd067_new RENAME TO rbac_permission_templates;

-- Admin、Developer 获得三项，Viewer 只获得 use；财务、安全和运维职责角色保持最小权限。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','prompt_template.read',1),
  ('admin','prompt_template.write',1),
  ('admin','prompt_template.use',1),
  ('developer','prompt_template.read',1),
  ('developer','prompt_template.write',1),
  ('developer','prompt_template.use',1),
  ('viewer','prompt_template.use',1);

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
CREATE TABLE permissions_prd067_new (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=4),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 现有租户权限逐行保留 rowid、稳定 ID 和状态，只同步产品目录 revision，不改变授权关系。
INSERT INTO permissions_prd067_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       4,status,created_at,updated_at
FROM permissions;
-- 每个现有 tenant 获得三项独立投影；稳定 ID 每租户生成，绝不复制跨租户实体。
INSERT INTO permissions_prd067_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT
  'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,
  4,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template
  ON template.permission_code IN (
    'prompt_template.read','prompt_template.write','prompt_template.use'
  );

DROP TABLE permissions;
-- 所有租户权限实体写齐后再恢复正式表名，避免角色关系在迁移中观察到半完成目录。
ALTER TABLE permissions_prd067_new RENAME TO permissions;
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
-- 只按产品角色模板为冻结的 canonical 三角色补权；自定义矩阵和其余角色保持原样。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT
  'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd067_roles_to_expand role
JOIN permissions permission
  ON permission.tenant_id=role.tenant_id
JOIN rbac_role_permission_templates template
  ON template.role_code=role.code AND template.permission_code=permission.code
WHERE permission.code IN (
  'prompt_template.read','prompt_template.write','prompt_template.use'
);
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
      WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
      WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
      WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
    END
WHERE role_id IN (SELECT role_id FROM prd067_roles_to_expand);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
    WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
    WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
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

-- 新租户直接使用 revision 4 目录；角色 revision 仍从 1 开始，因为它描述角色矩阵变更次数，
-- 不是产品 Permission catalog revision。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户先建立六个 canonical 角色，矩阵 hash 直接使用 revision 4 的冻结权限集合。
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
      WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
      WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
      WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
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


-- PRD-067 Invocation 需要把 Session 登录 owner 与 Message 请求链路作为一个不可拆分父键引用。
-- 这些索引只增加候选键，不重写或删除任何 PRD-066 Chat 事实；down 会精确移除。
CREATE UNIQUE INDEX uq_chat_sessions_template_owner_prd067
  ON chat_sessions(tenant_id,chat_session_id,owner_user_id,owner_auth_session_id);
CREATE UNIQUE INDEX uq_chat_messages_template_message_prd067
  ON chat_messages(tenant_id,chat_session_id,chat_message_id);
CREATE UNIQUE INDEX uq_chat_messages_template_owner_request_prd067
  ON chat_messages(
    tenant_id,chat_session_id,chat_message_id,owner_user_id,
    gateway_request_id,gateway_trace_id
  );

-- Template head 不保存正文，只保存当前发布版本的稳定 owner。四列延迟外键使 v1 创建和后续发布
-- 可以在同一事务中先装配 building 版本，但 COMMIT 前必须完成 published seal。
CREATE TABLE prompt_templates (
  prompt_template_id TEXT PRIMARY KEY CHECK (
    length(prompt_template_id)=37 AND substr(prompt_template_id,1,5)='ptpl_'
    AND substr(prompt_template_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL COLLATE NOCASE CHECK (
    length(name) BETWEEN 1 AND 120
    AND length(CAST(name AS BLOB))<=360 AND name=trim(name)
    AND instr(name,char(9))=0 AND instr(name,char(10))=0 AND instr(name,char(13))=0
  ),
  description TEXT NOT NULL DEFAULT '' CHECK (
    length(description)<=1000 AND length(CAST(description AS BLOB))<=3000
  ),
  status TEXT NOT NULL CHECK (status IN ('active','disabled')),
  current_version_id TEXT NOT NULL,
  current_version_state TEXT NOT NULL CHECK (current_version_state='published'),
  revision INTEGER NOT NULL CHECK (typeof(revision)='integer' AND revision>=1),
  created_by TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  UNIQUE (tenant_id,prompt_template_id),
  UNIQUE (tenant_id,name),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,created_by) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,updated_by) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (
    tenant_id,prompt_template_id,current_version_id,current_version_state
  ) REFERENCES prompt_template_versions(
    tenant_id,prompt_template_id,prompt_template_version_id,state
  ) DEFERRABLE INITIALLY DEFERRED,
  CHECK (unixepoch(updated_at)>=unixepoch(created_at))
);
CREATE INDEX idx_prompt_templates_tenant_status_prd067
  ON prompt_templates(tenant_id,status,updated_at DESC,prompt_template_id);

-- INSERT 只接受同租户 active actor 和 revision 1；published head 的真实性由延迟四列 FK 在提交时证明。
CREATE TRIGGER trg_prompt_templates_owner_insert_prd067
BEFORE INSERT ON prompt_templates FOR EACH ROW
WHEN NEW.revision!=1 OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS (
    SELECT 1 FROM tenants tenant
    WHERE tenant.tenant_id=NEW.tenant_id AND tenant.status='active'
  )
  OR NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.created_by
      AND actor.user_id=NEW.updated_by AND actor.status='active'
  )
  OR EXISTS (
    SELECT 1 FROM prompt_templates current
    WHERE current.prompt_template_id=NEW.prompt_template_id
       OR (current.tenant_id=NEW.tenant_id AND current.name=NEW.name COLLATE NOCASE)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_owner_mismatch'); END;

-- 元数据、状态或 head 每次真实变化只推进一个 CAS revision；tenant、创建者和历史时间永不改绑。
CREATE TRIGGER trg_prompt_templates_state_update_prd067
BEFORE UPDATE ON prompt_templates FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.prompt_template_id!=OLD.prompt_template_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.created_by!=OLD.created_by
  OR NEW.created_at!=OLD.created_at OR NEW.current_version_state!='published'
  OR NEW.revision!=OLD.revision+1 OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR (
    NEW.name=OLD.name COLLATE NOCASE AND NEW.description=OLD.description
    AND NEW.status=OLD.status AND NEW.current_version_id=OLD.current_version_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.updated_by
      AND actor.status='active'
  )
  OR EXISTS (
    SELECT 1 FROM prompt_templates current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.name=NEW.name COLLATE NOCASE
      AND current.prompt_template_id!=NEW.prompt_template_id
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_state_conflict'); END;
CREATE TRIGGER trg_prompt_templates_delete_prd067
BEFORE DELETE ON prompt_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_immutable'); END;

-- Version 只保存独立加密定义和安全计数。四组候选键分别服务 tenant owner、head、published head
-- 与 Invocation 的 version-number 冻结，禁止从多个弱关系拼出不存在的版本事实。
CREATE TABLE prompt_template_versions (
  prompt_template_version_id TEXT PRIMARY KEY CHECK (
    length(prompt_template_version_id)=36
    AND substr(prompt_template_version_id,1,4)='ptv_'
    AND substr(prompt_template_version_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  prompt_template_id TEXT NOT NULL,
  version_number INTEGER NOT NULL CHECK (
    typeof(version_number)='integer' AND version_number>=1
  ),
  state TEXT NOT NULL CHECK (state IN ('building','published')),
  definition_schema_version TEXT NOT NULL CHECK (
    definition_schema_version='prompt-template-v1'
  ),
  definition_ciphertext TEXT NOT NULL CHECK (
    length(definition_ciphertext) BETWEEN 32 AND 262144
    AND definition_ciphertext GLOB 'ptpl-aesgcm-v1:v1:*:*'
  ),
  definition_size_bytes INTEGER NOT NULL CHECK (
    typeof(definition_size_bytes)='integer'
    AND definition_size_bytes BETWEEN 1 AND 131072
  ),
  variable_count INTEGER NOT NULL CHECK (
    typeof(variable_count)='integer' AND variable_count BETWEEN 0 AND 32
  ),
  project_scope TEXT NOT NULL CHECK (project_scope IN ('all','selected')),
  recommended_model_count INTEGER NOT NULL CHECK (
    typeof(recommended_model_count)='integer'
    AND recommended_model_count BETWEEN 0 AND 8
  ),
  change_summary_hmac TEXT NOT NULL CHECK (
    length(change_summary_hmac)=64
    AND change_summary_hmac NOT GLOB '*[^0-9a-f]*'
  ),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  published_at TEXT CHECK (
    published_at IS NULL OR unixepoch(published_at) IS NOT NULL
  ),
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 160
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  UNIQUE (tenant_id,prompt_template_version_id),
  UNIQUE (tenant_id,prompt_template_id,version_number),
  UNIQUE (tenant_id,request_id),
  UNIQUE (tenant_id,prompt_template_id,prompt_template_version_id),
  UNIQUE (tenant_id,prompt_template_id,prompt_template_version_id,state),
  UNIQUE (tenant_id,prompt_template_id,version_number,prompt_template_version_id),
  FOREIGN KEY (tenant_id,prompt_template_id)
    REFERENCES prompt_templates(tenant_id,prompt_template_id),
  FOREIGN KEY (tenant_id,created_by) REFERENCES users(tenant_id,user_id),
  CHECK (
    (state='building' AND published_at IS NULL)
    OR (state='published' AND published_at IS NOT NULL
      AND unixepoch(published_at)>=unixepoch(created_at))
  )
);
CREATE INDEX idx_prompt_template_versions_tenant_template_prd067
  ON prompt_template_versions(
    tenant_id,prompt_template_id,version_number DESC,prompt_template_version_id
  );

-- Version 必须按同模板连续编号且由 active actor 创建；初始 building 只在当前写事务内可见。
CREATE TRIGGER trg_prompt_template_versions_owner_insert_prd067
BEFORE INSERT ON prompt_template_versions FOR EACH ROW
WHEN NEW.state!='building' OR NEW.published_at IS NOT NULL
  OR NEW.version_number!=1+COALESCE((
    SELECT MAX(version.version_number)
    FROM prompt_template_versions version
    WHERE version.tenant_id=NEW.tenant_id
      AND version.prompt_template_id=NEW.prompt_template_id
  ),0)
  OR NOT EXISTS (
    SELECT 1 FROM prompt_templates template
    WHERE template.tenant_id=NEW.tenant_id
      AND template.prompt_template_id=NEW.prompt_template_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.created_by
      AND actor.status='active'
  )
  OR EXISTS (
    SELECT 1 FROM prompt_template_versions current
    WHERE current.prompt_template_version_id=NEW.prompt_template_version_id
       OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
       OR (current.tenant_id=NEW.tenant_id
           AND current.prompt_template_id=NEW.prompt_template_id
           AND current.version_number=NEW.version_number)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_version_owner_mismatch'); END;

-- building 只允许一次 seal；密文、header、计数、owner、链路和 rowid 在 published 前后完全相同。
CREATE TRIGGER trg_prompt_template_versions_seal_update_prd067
BEFORE UPDATE ON prompt_template_versions FOR EACH ROW
WHEN OLD.state!='building' OR NEW.state!='published' OR NEW.published_at IS NULL
  OR NEW.rowid!=OLD.rowid
  OR NEW.prompt_template_version_id!=OLD.prompt_template_version_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.prompt_template_id!=OLD.prompt_template_id
  OR NEW.version_number!=OLD.version_number
  OR NEW.definition_schema_version!=OLD.definition_schema_version
  OR NEW.definition_ciphertext!=OLD.definition_ciphertext
  OR NEW.definition_size_bytes!=OLD.definition_size_bytes
  OR NEW.variable_count!=OLD.variable_count OR NEW.project_scope!=OLD.project_scope
  OR NEW.recommended_model_count!=OLD.recommended_model_count
  OR NEW.change_summary_hmac!=OLD.change_summary_hmac
  OR NEW.created_by!=OLD.created_by OR NEW.created_at!=OLD.created_at
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR unixepoch(NEW.published_at)<unixepoch(OLD.created_at)
  OR NOT EXISTS (
    SELECT 1 FROM prompt_templates template
    WHERE template.tenant_id=NEW.tenant_id
      AND template.prompt_template_id=NEW.prompt_template_id
      AND template.current_version_id=NEW.prompt_template_version_id
      AND template.current_version_state='published'
  )
  OR (
    NEW.project_scope='all' AND EXISTS (
      SELECT 1 FROM prompt_template_version_projects binding
      WHERE binding.tenant_id=NEW.tenant_id
        AND binding.prompt_template_id=NEW.prompt_template_id
        AND binding.prompt_template_version_id=NEW.prompt_template_version_id
    )
  )
  OR (
    NEW.project_scope='selected' AND NOT EXISTS (
      SELECT 1 FROM prompt_template_version_projects binding
      JOIN projects project
        ON project.tenant_id=binding.tenant_id
       AND project.project_id=binding.project_id
      WHERE binding.tenant_id=NEW.tenant_id
        AND binding.prompt_template_id=NEW.prompt_template_id
        AND binding.prompt_template_version_id=NEW.prompt_template_version_id
        AND project.status='active'
    )
  )
  OR EXISTS (
    SELECT 1 FROM prompt_template_version_projects binding
    LEFT JOIN projects project
      ON project.tenant_id=binding.tenant_id AND project.project_id=binding.project_id
    WHERE binding.tenant_id=NEW.tenant_id
      AND binding.prompt_template_id=NEW.prompt_template_id
      AND binding.prompt_template_version_id=NEW.prompt_template_version_id
      AND (project.project_id IS NULL OR project.status!='active')
  )
  OR NEW.recommended_model_count!=(
    SELECT COUNT(*) FROM prompt_template_version_models binding
    WHERE binding.tenant_id=NEW.tenant_id
      AND binding.prompt_template_id=NEW.prompt_template_id
      AND binding.prompt_template_version_id=NEW.prompt_template_version_id
  )
  OR EXISTS (
    SELECT 1 FROM prompt_template_version_models binding
    LEFT JOIN model_aliases alias
      ON alias.tenant_id=binding.tenant_id
     AND alias.model_alias_id=binding.model_alias_id
    WHERE binding.tenant_id=NEW.tenant_id
      AND binding.prompt_template_id=NEW.prompt_template_id
      AND binding.prompt_template_version_id=NEW.prompt_template_version_id
      AND (alias.model_alias_id IS NULL OR alias.status!='active')
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_version_seal_conflict'); END;
CREATE TRIGGER trg_prompt_template_versions_delete_prd067
BEFORE DELETE ON prompt_template_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_version_immutable'); END;

-- Project 绑定只在 building 装配期插入；三列 Version FK 与两列 Project FK 同时证明 tenant owner。
CREATE TABLE prompt_template_version_projects (
  version_project_id TEXT PRIMARY KEY CHECK (
    length(version_project_id)=37 AND substr(version_project_id,1,5)='ptvp_'
    AND substr(version_project_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  prompt_template_id TEXT NOT NULL,
  prompt_template_version_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  UNIQUE (tenant_id,version_project_id),
  UNIQUE (tenant_id,prompt_template_version_id,project_id),
  FOREIGN KEY (tenant_id,prompt_template_id,prompt_template_version_id)
    REFERENCES prompt_template_versions(
      tenant_id,prompt_template_id,prompt_template_version_id
    ),
  FOREIGN KEY (tenant_id,project_id) REFERENCES projects(tenant_id,project_id)
);
CREATE INDEX idx_prompt_template_version_projects_owner_prd067
  ON prompt_template_version_projects(
    tenant_id,prompt_template_id,prompt_template_version_id,project_id
  );
CREATE TRIGGER trg_prompt_template_version_projects_owner_insert_prd067
BEFORE INSERT ON prompt_template_version_projects FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM prompt_template_versions version
    WHERE version.tenant_id=NEW.tenant_id
      AND version.prompt_template_id=NEW.prompt_template_id
      AND version.prompt_template_version_id=NEW.prompt_template_version_id
      AND version.state='building' AND version.project_scope='selected'
  )
  OR NOT EXISTS (
    SELECT 1 FROM projects project
    WHERE project.tenant_id=NEW.tenant_id AND project.project_id=NEW.project_id
      AND project.status='active'
  )
  OR EXISTS (
    SELECT 1 FROM prompt_template_version_projects current
    WHERE current.version_project_id=NEW.version_project_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.prompt_template_version_id=NEW.prompt_template_version_id
           AND current.project_id=NEW.project_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_project_owner_mismatch'); END;
CREATE TRIGGER trg_prompt_template_version_projects_update_prd067
BEFORE UPDATE ON prompt_template_version_projects FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_binding_immutable'); END;
CREATE TRIGGER trg_prompt_template_version_projects_delete_prd067
BEFORE DELETE ON prompt_template_version_projects FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM prompt_template_versions version
  WHERE version.tenant_id=OLD.tenant_id
    AND version.prompt_template_id=OLD.prompt_template_id
    AND version.prompt_template_version_id=OLD.prompt_template_version_id
    AND version.state='published'
)
BEGIN SELECT RAISE(ABORT,'prompt_template_binding_immutable'); END;

-- 推荐 ModelAlias 是有序提示，不是路由白名单；连续 display_order 和 active owner 只在 seal 时冻结。
CREATE TABLE prompt_template_version_models (
  version_model_id TEXT PRIMARY KEY CHECK (
    length(version_model_id)=37 AND substr(version_model_id,1,5)='ptvm_'
    AND substr(version_model_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  prompt_template_id TEXT NOT NULL,
  prompt_template_version_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  display_order INTEGER NOT NULL CHECK (
    typeof(display_order)='integer' AND display_order BETWEEN 0 AND 7
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  UNIQUE (tenant_id,version_model_id),
  UNIQUE (tenant_id,prompt_template_version_id,model_alias_id),
  UNIQUE (tenant_id,prompt_template_version_id,display_order),
  FOREIGN KEY (tenant_id,prompt_template_id,prompt_template_version_id)
    REFERENCES prompt_template_versions(
      tenant_id,prompt_template_id,prompt_template_version_id
    ),
  FOREIGN KEY (tenant_id,model_alias_id)
    REFERENCES model_aliases(tenant_id,model_alias_id)
);
CREATE INDEX idx_prompt_template_version_models_owner_prd067
  ON prompt_template_version_models(
    tenant_id,prompt_template_id,prompt_template_version_id,display_order
  );
CREATE TRIGGER trg_prompt_template_version_models_owner_insert_prd067
BEFORE INSERT ON prompt_template_version_models FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM prompt_template_versions version
    WHERE version.tenant_id=NEW.tenant_id
      AND version.prompt_template_id=NEW.prompt_template_id
      AND version.prompt_template_version_id=NEW.prompt_template_version_id
      AND version.state='building'
      AND NEW.display_order<version.recommended_model_count
  )
  OR NEW.display_order!=(
    SELECT COUNT(*) FROM prompt_template_version_models current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.prompt_template_version_id=NEW.prompt_template_version_id
  )
  OR NOT EXISTS (
    SELECT 1 FROM model_aliases alias
    WHERE alias.tenant_id=NEW.tenant_id
      AND alias.model_alias_id=NEW.model_alias_id AND alias.status='active'
  )
  OR EXISTS (
    SELECT 1 FROM prompt_template_version_models current
    WHERE current.version_model_id=NEW.version_model_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.prompt_template_version_id=NEW.prompt_template_version_id
           AND current.model_alias_id=NEW.model_alias_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_model_owner_mismatch'); END;
CREATE TRIGGER trg_prompt_template_version_models_update_prd067
BEFORE UPDATE ON prompt_template_version_models FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_binding_immutable'); END;
CREATE TRIGGER trg_prompt_template_version_models_delete_prd067
BEFORE DELETE ON prompt_template_version_models FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM prompt_template_versions version
  WHERE version.tenant_id=OLD.tenant_id
    AND version.prompt_template_id=OLD.prompt_template_id
    AND version.prompt_template_version_id=OLD.prompt_template_version_id
    AND version.state='published'
)
BEGIN SELECT RAISE(ABORT,'prompt_template_binding_immutable'); END;

-- Invocation 只保存调用 owner、版本和固定终态；Prompt、变量、history、Response 与密钥永不落表。
CREATE TABLE chat_prompt_template_invocations (
  prompt_template_invocation_id TEXT PRIMARY KEY CHECK (
    length(prompt_template_invocation_id)=38
    AND substr(prompt_template_invocation_id,1,6)='ptinv_'
    AND substr(prompt_template_invocation_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  prompt_template_id TEXT NOT NULL,
  prompt_template_version_id TEXT NOT NULL,
  version_number INTEGER NOT NULL CHECK (
    typeof(version_number)='integer' AND version_number>=1
  ),
  chat_session_id TEXT NOT NULL,
  chat_message_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_auth_session_id TEXT NOT NULL,
  gateway_request_id TEXT NOT NULL CHECK (
    length(gateway_request_id) BETWEEN 1 AND 128
    AND gateway_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  gateway_trace_id TEXT NOT NULL CHECK (
    length(gateway_trace_id) BETWEEN 1 AND 160
    AND gateway_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('in_flight','succeeded','failed')),
  failure_code TEXT CHECK (
    failure_code IS NULL OR (
      length(failure_code) BETWEEN 1 AND 128
      AND failure_code NOT GLOB '*[^A-Za-z0-9_.:-]*'
    )
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  completed_at TEXT CHECK (completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  UNIQUE (tenant_id,prompt_template_invocation_id),
  UNIQUE (tenant_id,chat_session_id,chat_message_id),
  FOREIGN KEY (
    tenant_id,prompt_template_id,version_number,prompt_template_version_id
  ) REFERENCES prompt_template_versions(
    tenant_id,prompt_template_id,version_number,prompt_template_version_id
  ),
  FOREIGN KEY (
    tenant_id,chat_session_id,actor_user_id,actor_auth_session_id
  ) REFERENCES chat_sessions(
    tenant_id,chat_session_id,owner_user_id,owner_auth_session_id
  ),
  FOREIGN KEY (
    tenant_id,chat_session_id,chat_message_id,actor_user_id,
    gateway_request_id,gateway_trace_id
  ) REFERENCES chat_messages(
    tenant_id,chat_session_id,chat_message_id,owner_user_id,
    gateway_request_id,gateway_trace_id
  ),
  CHECK (
    (status='in_flight' AND failure_code IS NULL AND completed_at IS NULL)
    OR (status='succeeded' AND failure_code IS NULL AND completed_at IS NOT NULL)
    OR (status='failed' AND failure_code IS NOT NULL AND completed_at IS NOT NULL)
  )
);
CREATE INDEX idx_chat_prompt_invocations_tenant_template_prd067
  ON chat_prompt_template_invocations(
    tenant_id,prompt_template_id,version_number,created_at DESC
  );
CREATE INDEX idx_chat_prompt_invocations_tenant_request_prd067
  ON chat_prompt_template_invocations(tenant_id,gateway_request_id);

-- INSERT trigger 复演整个 owner 图，并在线性化点要求 active/current/published 与项目适用性同时成立。
CREATE TRIGGER trg_chat_prompt_invocations_owner_insert_prd067
BEFORE INSERT ON chat_prompt_template_invocations FOR EACH ROW
WHEN NEW.status!='in_flight' OR NEW.failure_code IS NOT NULL OR NEW.completed_at IS NOT NULL
  OR NOT EXISTS (
    SELECT 1
    FROM prompt_template_versions version
    JOIN prompt_templates template
      ON template.tenant_id=version.tenant_id
     AND template.prompt_template_id=version.prompt_template_id
     AND template.current_version_id=version.prompt_template_version_id
     AND template.current_version_state=version.state
    JOIN chat_sessions chat_session
      ON chat_session.tenant_id=NEW.tenant_id
     AND chat_session.chat_session_id=NEW.chat_session_id
     AND chat_session.owner_user_id=NEW.actor_user_id
     AND chat_session.owner_auth_session_id=NEW.actor_auth_session_id
    JOIN chat_messages message
      ON message.tenant_id=chat_session.tenant_id
     AND message.chat_session_id=chat_session.chat_session_id
     AND message.chat_message_id=NEW.chat_message_id
     AND message.owner_user_id=NEW.actor_user_id
     AND message.gateway_request_id=NEW.gateway_request_id
     AND message.gateway_trace_id=NEW.gateway_trace_id
    WHERE version.tenant_id=NEW.tenant_id
      AND version.prompt_template_id=NEW.prompt_template_id
      AND version.prompt_template_version_id=NEW.prompt_template_version_id
      AND version.version_number=NEW.version_number AND version.state='published'
      AND template.status='active' AND chat_session.status='active'
      AND message.status='in_flight'
      AND (
        version.project_scope='all'
        OR EXISTS (
          SELECT 1 FROM prompt_template_version_projects binding
          WHERE binding.tenant_id=version.tenant_id
            AND binding.prompt_template_id=version.prompt_template_id
            AND binding.prompt_template_version_id=version.prompt_template_version_id
            AND binding.project_id=chat_session.project_id
        )
      )
  )
  OR EXISTS (
    SELECT 1 FROM chat_prompt_template_invocations current
    WHERE current.prompt_template_invocation_id=NEW.prompt_template_invocation_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.chat_session_id=NEW.chat_session_id
           AND current.chat_message_id=NEW.chat_message_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_invocation_owner_mismatch'); END;

-- Invocation 与 ChatMessage 必须在同一事务只终结一次；任何 owner 或链路字段变化都失败关闭。
CREATE TRIGGER trg_chat_prompt_invocations_state_update_prd067
BEFORE UPDATE ON chat_prompt_template_invocations FOR EACH ROW
WHEN OLD.status!='in_flight' OR NEW.status NOT IN ('succeeded','failed')
  OR NEW.rowid!=OLD.rowid
  OR NEW.prompt_template_invocation_id!=OLD.prompt_template_invocation_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.prompt_template_id!=OLD.prompt_template_id
  OR NEW.prompt_template_version_id!=OLD.prompt_template_version_id
  OR NEW.version_number!=OLD.version_number OR NEW.chat_session_id!=OLD.chat_session_id
  OR NEW.chat_message_id!=OLD.chat_message_id OR NEW.actor_user_id!=OLD.actor_user_id
  OR NEW.actor_auth_session_id!=OLD.actor_auth_session_id
  OR NEW.gateway_request_id!=OLD.gateway_request_id
  OR NEW.gateway_trace_id!=OLD.gateway_trace_id OR NEW.created_at!=OLD.created_at
  OR NEW.completed_at IS NULL OR unixepoch(NEW.completed_at)<unixepoch(OLD.created_at)
  OR (NEW.status='succeeded' AND NEW.failure_code IS NOT NULL)
  OR (NEW.status='failed' AND NEW.failure_code IS NULL)
  OR NOT EXISTS (
    SELECT 1 FROM chat_messages message
    WHERE message.tenant_id=NEW.tenant_id
      AND message.chat_session_id=NEW.chat_session_id
      AND message.chat_message_id=NEW.chat_message_id
      AND message.owner_user_id=NEW.actor_user_id
      AND message.gateway_request_id=NEW.gateway_request_id
      AND message.gateway_trace_id=NEW.gateway_trace_id
      AND message.status=NEW.status AND message.completed_at=NEW.completed_at
      AND (
        (NEW.status='succeeded' AND message.error_code IS NULL)
        OR (NEW.status='failed' AND message.error_code=NEW.failure_code)
      )
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_invocation_state_conflict'); END;
CREATE TRIGGER trg_chat_prompt_invocations_delete_prd067
BEFORE DELETE ON chat_prompt_template_invocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_invocation_immutable'); END;

-- Source event 只投递安全计数，并以 tenant-qualified FK 绑定 publication AuditLog。
CREATE TABLE prompt_template_events (
  prompt_template_event_id TEXT PRIMARY KEY CHECK (
    length(prompt_template_event_id)=37
    AND substr(prompt_template_event_id,1,5)='ptev_'
    AND substr(prompt_template_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  prompt_template_id TEXT NOT NULL,
  prompt_template_version_id TEXT NOT NULL,
  version_number INTEGER NOT NULL CHECK (
    typeof(version_number)='integer' AND version_number>=1
  ),
  event_type TEXT NOT NULL CHECK (event_type='prompt_template.published'),
  actor_user_id TEXT NOT NULL,
  publication_audit_log_id TEXT NOT NULL UNIQUE,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 160
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048
  ),
  delivery_status TEXT NOT NULL CHECK (
    delivery_status IN ('pending','dispatched','failed')
  ),
  last_error_code TEXT CHECK (
    last_error_code IS NULL OR last_error_code='webhook.delivery_unavailable'
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  dispatched_at TEXT CHECK (
    dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL
  ),
  UNIQUE (tenant_id,prompt_template_event_id),
  UNIQUE (tenant_id,prompt_template_version_id,event_type),
  UNIQUE (tenant_id,request_id),
  FOREIGN KEY (
    tenant_id,prompt_template_id,version_number,prompt_template_version_id
  ) REFERENCES prompt_template_versions(
    tenant_id,prompt_template_id,version_number,prompt_template_version_id
  ),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,publication_audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id),
  CHECK (
    (delivery_status='pending' AND last_error_code IS NULL
      AND dispatched_at IS NULL AND updated_at=created_at)
    OR (delivery_status='dispatched' AND last_error_code IS NULL
      AND dispatched_at IS NOT NULL AND dispatched_at=updated_at)
    OR (delivery_status='failed' AND last_error_code='webhook.delivery_unavailable'
      AND dispatched_at IS NOT NULL AND dispatched_at=updated_at)
  )
);
CREATE INDEX idx_prompt_template_events_tenant_status_prd067
  ON prompt_template_events(
    tenant_id,delivery_status,updated_at,prompt_template_event_id
  );

-- 事件 owner trigger 精确复演版本、当前 head、Audit 动作/链路和七字段安全 payload。
CREATE TRIGGER trg_prompt_template_events_owner_insert_prd067
BEFORE INSERT ON prompt_template_events FOR EACH ROW
WHEN NEW.delivery_status!='pending' OR NEW.last_error_code IS NOT NULL
  OR NEW.dispatched_at IS NOT NULL OR NEW.updated_at!=NEW.created_at
  OR NOT EXISTS (
    SELECT 1
    FROM prompt_template_versions version
    JOIN prompt_templates template
      ON template.tenant_id=version.tenant_id
     AND template.prompt_template_id=version.prompt_template_id
     AND template.current_version_id=version.prompt_template_version_id
    JOIN users actor
      ON actor.tenant_id=version.tenant_id AND actor.user_id=NEW.actor_user_id
    JOIN audit_logs audit
      ON audit.tenant_id=version.tenant_id
     AND audit.audit_log_id=NEW.publication_audit_log_id
    WHERE version.tenant_id=NEW.tenant_id
      AND version.prompt_template_id=NEW.prompt_template_id
      AND version.prompt_template_version_id=NEW.prompt_template_version_id
      AND version.version_number=NEW.version_number AND version.state='published'
      AND actor.status='active'
      AND audit.action='prompt_template.version_published'
      AND audit.object_type='prompt_template_version'
      AND audit.object_id=NEW.prompt_template_version_id
      AND audit.actor_user_id=NEW.actor_user_id
      AND audit.request_id=NEW.request_id AND audit.trace_id=NEW.trace_id
      AND audit.created_at=NEW.created_at
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=7
      AND NOT EXISTS (
        SELECT 1 FROM json_each(NEW.payload_json) field
        WHERE field.key NOT IN (
          'template_id','version_id','version_number','template_revision',
          'project_scope','project_count','recommended_model_count'
        )
      )
      AND json_extract(NEW.payload_json,'$.template_id')=NEW.prompt_template_id
      AND json_extract(NEW.payload_json,'$.version_id')=NEW.prompt_template_version_id
      AND json_extract(NEW.payload_json,'$.version_number')=NEW.version_number
      AND json_extract(NEW.payload_json,'$.template_revision')=template.revision
      AND json_extract(NEW.payload_json,'$.project_scope')=version.project_scope
      AND json_extract(NEW.payload_json,'$.project_count')=(
        SELECT COUNT(*) FROM prompt_template_version_projects binding
        WHERE binding.tenant_id=version.tenant_id
          AND binding.prompt_template_version_id=version.prompt_template_version_id
      )
      AND json_extract(NEW.payload_json,'$.recommended_model_count')
        =version.recommended_model_count
  )
  OR EXISTS (
    SELECT 1 FROM prompt_template_events current
    WHERE current.prompt_template_event_id=NEW.prompt_template_event_id
       OR current.publication_audit_log_id=NEW.publication_audit_log_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.prompt_template_version_id=NEW.prompt_template_version_id
           AND current.event_type=NEW.event_type)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_event_owner_mismatch'); END;

-- 网络投递只能推进 pending 事件状态，源事实、payload 和 Audit 绑定永久不可修改。
CREATE TRIGGER trg_prompt_template_events_state_update_prd067
BEFORE UPDATE ON prompt_template_events FOR EACH ROW
WHEN OLD.delivery_status!='pending' OR NEW.delivery_status NOT IN ('dispatched','failed')
  OR NEW.rowid!=OLD.rowid OR NEW.prompt_template_event_id!=OLD.prompt_template_event_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.prompt_template_id!=OLD.prompt_template_id
  OR NEW.prompt_template_version_id!=OLD.prompt_template_version_id
  OR NEW.version_number!=OLD.version_number OR NEW.event_type!=OLD.event_type
  OR NEW.actor_user_id!=OLD.actor_user_id
  OR NEW.publication_audit_log_id!=OLD.publication_audit_log_id
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.payload_json!=OLD.payload_json OR NEW.created_at!=OLD.created_at
  OR NEW.dispatched_at IS NULL OR NEW.dispatched_at!=NEW.updated_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR (NEW.delivery_status='dispatched' AND NEW.last_error_code IS NOT NULL)
  OR (NEW.delivery_status='failed'
      AND NEW.last_error_code!='webhook.delivery_unavailable')
BEGIN SELECT RAISE(ABORT,'prompt_template_event_state_conflict'); END;
CREATE TRIGGER trg_prompt_template_events_delete_prd067
BEFORE DELETE ON prompt_template_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_event_immutable'); END;

-- Operation 是低频管理写的幂等结果索引；intent 只保存分域 HMAC，不保存请求或响应正文快照。
CREATE TABLE prompt_template_operations (
  prompt_template_operation_id TEXT PRIMARY KEY CHECK (
    length(prompt_template_operation_id)=37
    AND substr(prompt_template_operation_id,1,5)='ptop_'
    AND substr(prompt_template_operation_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 160
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  operation TEXT NOT NULL CHECK (operation IN ('create','patch','publish')),
  intent_hmac TEXT NOT NULL CHECK (
    length(intent_hmac)=64 AND intent_hmac NOT GLOB '*[^0-9a-f]*'
  ),
  prompt_template_id TEXT NOT NULL,
  prompt_template_version_id TEXT,
  version_number INTEGER CHECK (
    version_number IS NULL
    OR (typeof(version_number)='integer' AND version_number>=1)
  ),
  result_template_revision INTEGER NOT NULL CHECK (
    typeof(result_template_revision)='integer' AND result_template_revision>=1
  ),
  result_audit_log_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  UNIQUE (tenant_id,prompt_template_operation_id),
  UNIQUE (tenant_id,request_id),
  FOREIGN KEY (tenant_id,prompt_template_id)
    REFERENCES prompt_templates(tenant_id,prompt_template_id),
  FOREIGN KEY (
    tenant_id,prompt_template_id,version_number,prompt_template_version_id
  ) REFERENCES prompt_template_versions(
    tenant_id,prompt_template_id,version_number,prompt_template_version_id
  ),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,result_audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id),
  CHECK (
    (operation IN ('create','publish')
      AND prompt_template_version_id IS NOT NULL AND version_number IS NOT NULL)
    OR (operation='patch'
      AND prompt_template_version_id IS NULL AND version_number IS NULL)
  )
);
CREATE INDEX idx_prompt_template_operations_tenant_template_prd067
  ON prompt_template_operations(tenant_id,prompt_template_id,created_at DESC);

-- Operation INSERT 反向绑定当前模板 revision、可选发布版本和唯一 AuditLog；重放只读该不可变事实。
CREATE TRIGGER trg_prompt_template_operations_owner_insert_prd067
BEFORE INSERT ON prompt_template_operations FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM prompt_templates template
    JOIN users actor
      ON actor.tenant_id=template.tenant_id AND actor.user_id=NEW.actor_user_id
    JOIN audit_logs audit
      ON audit.tenant_id=template.tenant_id
     AND audit.audit_log_id=NEW.result_audit_log_id
    WHERE template.tenant_id=NEW.tenant_id
      AND template.prompt_template_id=NEW.prompt_template_id
      AND template.revision=NEW.result_template_revision
      AND actor.status='active' AND audit.actor_user_id=NEW.actor_user_id
      AND audit.request_id=NEW.request_id AND audit.trace_id=NEW.trace_id
      AND audit.created_at=NEW.created_at
      AND audit.action=CASE NEW.operation
        WHEN 'create' THEN 'prompt_template.created'
        WHEN 'patch' THEN 'prompt_template.updated'
        WHEN 'publish' THEN 'prompt_template.version_published'
      END
      AND audit.object_type=CASE NEW.operation
        WHEN 'publish' THEN 'prompt_template_version' ELSE 'prompt_template'
      END
      AND audit.object_id=CASE NEW.operation
        WHEN 'publish' THEN NEW.prompt_template_version_id
        ELSE NEW.prompt_template_id
      END
  )
  OR (
    NEW.operation IN ('create','publish') AND NOT EXISTS (
      SELECT 1 FROM prompt_template_versions version
      WHERE version.tenant_id=NEW.tenant_id
        AND version.prompt_template_id=NEW.prompt_template_id
        AND version.prompt_template_version_id=NEW.prompt_template_version_id
        AND version.version_number=NEW.version_number AND version.state='published'
    )
  )
  OR EXISTS (
    SELECT 1 FROM prompt_template_operations current
    WHERE current.prompt_template_operation_id=NEW.prompt_template_operation_id
       OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
       OR current.result_audit_log_id=NEW.result_audit_log_id
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'prompt_template_operation_owner_mismatch'); END;
CREATE TRIGGER trg_prompt_template_operations_update_prd067
BEFORE UPDATE ON prompt_template_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_operation_immutable'); END;
CREATE TRIGGER trg_prompt_template_operations_delete_prd067
BEFORE DELETE ON prompt_template_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_operation_immutable'); END;

-- AuditLog 只扩展模板动作/对象枚举，17 个持久列、历史 rowid、NULL 语义和旧索引保持不变。
-- 模板摘要只允许状态、revision、版本号、scope/count、定义大小和 keyed HMAC，绝不保存正文或变量。
CREATE TABLE audit_logs_prd067_new (
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
      'prompt_template.version_published'
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
      'prompt_template','prompt_template_version'
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
INSERT INTO audit_logs_prd067_new(
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
CREATE TEMP TABLE prd067_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 复制后逐值核对行数与 17 列内容，任何差异都在删除旧审计表前失败关闭。
INSERT INTO prd067_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd067_new))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd067_new WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd067_new WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd067_new WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd067_new
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd067_new
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd067_audit_copy_guard;

-- legacy_alter_table 保持四个已核验的外部 owner trigger 原样指向正式表名；旧表本地
-- trigger/index 随 DROP 移除，重命名后按冻结定义逐项恢复。
DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后替换正式审计表，确保历史治理证据完整保真。
ALTER TABLE audit_logs_prd067_new RENAME TO audit_logs;
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

-- 提交前验证 RBAC revision 4、Audit 保真、七张业务表、候选键和全部完整性约束。
DROP TABLE IF EXISTS temp.prd067_up_final_guard;
CREATE TEMP TABLE prd067_up_final_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd067_up_final_state_ok CHECK (issue_count=0)
);
-- 迁移内 guard 是原子性的一部分；测试只能补充行为覆盖，不能替代提交前零偏差断言。
INSERT INTO prd067_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=54
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=4)=54
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code='prompt_template.read'
             AND category='prompt_template' AND risk_level='read')=1
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code='prompt_template.write'
             AND category='prompt_template' AND risk_level='critical')=1
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code='prompt_template.use'
             AND category='prompt_template' AND risk_level='write')=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=141
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code LIKE 'prompt_template.%')=7
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE role_code='admin' AND permission_code LIKE 'prompt_template.%')=3
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE role_code='developer' AND permission_code LIKE 'prompt_template.%')=3
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE role_code='viewer' AND permission_code='prompt_template.use')=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*54 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=4)
      =(SELECT COUNT(*) FROM permissions)
      AND (SELECT COUNT(*) FROM permissions WHERE code LIKE 'prompt_template.%')
      =(SELECT COUNT(*)*3 FROM tenants)
    THEN 0 ELSE 1 END
  -- 只允许 preflight 冻结的 canonical 角色获得新关系，并要求 revision/hash 同步推进一次。
  + (SELECT COUNT(*) FROM prd067_roles_to_expand source
     JOIN roles role
       ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=source.permission_revision+1
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
          WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
          WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
        END
        OR (SELECT COUNT(*) FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=role.tenant_id
              AND relation.role_id=role.role_id
              AND permission.code LIKE 'prompt_template.%')
          !=CASE role.code WHEN 'viewer' THEN 1 ELSE 3 END)
  + (SELECT COUNT(*) FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code LIKE 'prompt_template.%'
       AND NOT EXISTS (
         SELECT 1 FROM prd067_roles_to_expand source
         WHERE source.tenant_id=relation.tenant_id AND source.role_id=relation.role_id
       ))
  -- 七张模板表、三组 Chat 复合父键和 21 个域 trigger 必须全部存在。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN (
      'prompt_templates','prompt_template_versions',
      'prompt_template_version_projects','prompt_template_version_models',
      'chat_prompt_template_invocations','prompt_template_events',
      'prompt_template_operations'
    ))=7 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN (
      'uq_chat_sessions_template_owner_prd067',
      'uq_chat_messages_template_message_prd067',
      'uq_chat_messages_template_owner_request_prd067'
    ))=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_prompt_template%_prd067')=18
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_chat_prompt_invocations%_prd067')=3
    THEN 0 ELSE 1 END
  -- Audit 仍为 17 列、12 个显式索引、6 个本地 trigger，并保留四个历史外部 owner trigger。
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('audit_logs') ORDER BY cid
      )
    )='audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND tbl_name='audit_logs'
        AND name NOT LIKE 'sqlite_autoindex%')=12 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN (
      'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert','trg_audit_logs_export_sequence_insert_prd058',
      'trg_audit_exports_owner_insert_prd058',
      'trg_audit_log_export_sequences_owner_insert_prd058',
      'trg_invitation_events_owner_insert_prd060',
      'trg_role_permission_events_owner_insert_prd059'
    ))=10 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs'),'prompt_template.version_published')>0
      AND instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs'),'prompt_template_version')>0
    THEN 0 ELSE 1 END
  -- 每条 AuditLog 都必须仍登记增量导出序列；换表不得改写现有 sequence 或制造缺口。
  + CASE WHEN (SELECT COUNT(*) FROM audit_log_export_sequences)
      =(SELECT COUNT(*) FROM audit_logs)
      AND NOT EXISTS (
        SELECT 1 FROM audit_logs audit
        WHERE NOT EXISTS (
          SELECT 1 FROM audit_log_export_sequences sequence
          WHERE sequence.tenant_id=audit.tenant_id
            AND sequence.audit_log_id=audit.audit_log_id
        )
      )
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd067_up_final_guard;
DROP TABLE prd067_roles_to_expand;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
