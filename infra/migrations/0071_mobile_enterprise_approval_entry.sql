-- PRD-078 正向迁移：租户隔离的移动审批入口、二次验证、目标回写和恢复型事件 outbox。
-- 所有 HMAC 只保存版本和摘要；密码、票据、幂等 key、目标正文、Prompt、Response、Token
-- 与 Provider Key 明文不得进入表、trigger 错误或验收证据。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首个持久 DDL 前验证 migration owner、revision-13 RBAC 基线与数据库完整性。
DROP TABLE IF EXISTS temp.prd078_up_guard;
CREATE TEMP TABLE prd078_up_guard(
  issue_count INTEGER NOT NULL
    CONSTRAINT prd078_up_guard_ok CHECK(issue_count=0)
);
-- 原子写入基线问题计数；迁移版本、RBAC 哈希、旧表结构或数据库健康任一偏离即中止。
INSERT INTO prd078_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations WHERE status='applied')=70
      AND (SELECT COUNT(*) FROM schema_migrations WHERE version='0070' AND status='applied')=1
      AND (SELECT MAX(version) FROM schema_migrations WHERE status='applied')='0070'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=70
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=13)
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=188
      AND NOT EXISTS(SELECT 1 FROM permissions WHERE catalog_revision!=13)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||
          description||char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='fada45e389a47487f0fd295d9c545143ac284fdd973fef0e483c69d24ab3d0a2'
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='a3fae8dfe8b7c2cea23ebc5e61125f888af353155b559546f2a2df14b1bbc95d'
      AND NOT EXISTS(SELECT tenant.tenant_id,template.permission_code,
          template.category,template.name,template.description,template.risk_level,13,'active'
        FROM tenants tenant CROSS JOIN rbac_permission_templates template
        EXCEPT SELECT tenant_id,code,category,name,description,risk_level,
          catalog_revision,status FROM permissions)
      AND NOT EXISTS(SELECT tenant_id,code,category,name,description,risk_level,
          catalog_revision,status FROM permissions
        EXCEPT SELECT tenant.tenant_id,template.permission_code,
          template.category,template.name,template.description,template.risk_level,13,'active'
        FROM tenants tenant CROSS JOIN rbac_permission_templates template)
      AND NOT EXISTS(SELECT 1 FROM permissions permission
        WHERE NOT EXISTS(SELECT 1 FROM rbac_permission_templates template
          WHERE template.permission_code=permission.code
            AND template.category=permission.category AND template.name=permission.name
            AND template.description=permission.description
            AND template.risk_level=permission.risk_level))
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM sqlite_schema WHERE name IN(
      'approval_tasks','approval_decisions','approval_step_up_challenges',
      'approval_step_up_guards','approval_action_replays','approval_writebacks',
      'approval_events','approval_event_outbox','approval_scheduler_tenants',
      'budget_blocks_prd078_new','rbac_permission_templates_prd078_new',
      'permissions_prd078_new')) THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT COUNT(*) FROM sqlite_schema
      WHERE tbl_name IN('rbac_permission_templates','permissions','budget_blocks')
         OR name IN('trg_tenants_rbac_initialize_prd059','trg_roles_owner_insert_prd059',
           'trg_roles_matrix_update_prd059','trg_role_permissions_owner_insert_prd059',
           'trg_role_permissions_delete_prd059','trg_rbac_role_templates_insert_prd059',
           'trg_rbac_role_templates_update_prd059','trg_rbac_role_templates_delete_prd059')
      )=31 AND (
      SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT type||char(31)||name||char(31)||tbl_name||char(31)||
          coalesce(sql,'<null>') AS record
        FROM sqlite_schema
        WHERE tbl_name IN('rbac_permission_templates','permissions','budget_blocks')
           OR name IN('trg_tenants_rbac_initialize_prd059','trg_roles_owner_insert_prd059',
             'trg_roles_matrix_update_prd059','trg_role_permissions_owner_insert_prd059',
             'trg_role_permissions_delete_prd059','trg_rbac_role_templates_insert_prd059',
             'trg_rbac_role_templates_update_prd059','trg_rbac_role_templates_delete_prd059')
        ORDER BY type,name
      ))='66b0cec689b0e5bde165df609aa83a8850623dee40d351a7c3054f22ccce8cdb'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd078_up_guard;

-- 只扩展仍精确匹配 revision-13 默认矩阵的三个角色；自定义矩阵完整保留且不自动补权。
CREATE TEMP TABLE prd078_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','finance_admin','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d'
    WHEN 'finance_admin' THEN '63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8'
    WHEN 'security_admin' THEN '38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656'
  END
  AND NOT EXISTS(
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS(
      SELECT 1 FROM role_permissions relation JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code))
  AND NOT EXISTS(
    SELECT 1 FROM role_permissions relation JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS(SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code));

-- 暂停不可变目录 trigger；全部目录变更仍位于同一 IMMEDIATE 事务并在末尾复核。
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
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;

CREATE TABLE rbac_permission_templates_prd078_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=14)
);
-- 保留既有权限模板 rowid 与字段，仅把目录 revision 提升到 14。
INSERT INTO rbac_permission_templates_prd078_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,14
FROM rbac_permission_templates;
-- 增加只读和决策两项审批权限；目标域写权限仍必须由服务端叠加校验。
INSERT INTO rbac_permission_templates_prd078_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('approval.read','approval','读取审批任务',
   '读取当前租户内同时满足目标域授权的审批任务与安全审计摘要。','read',14),
  ('approval.decide','approval','处理审批任务',
   '批准或拒绝当前租户审批任务，且仍须满足目标域写权限与二次验证。','critical',14);
DROP TABLE rbac_permission_templates;
-- 以完成复制且受 revision-14 约束的新表替换旧权限模板目录。
ALTER TABLE rbac_permission_templates_prd078_new RENAME TO rbac_permission_templates;

-- 只为默认角色矩阵声明审批权限，租户自定义角色不会在迁移中被静默扩权。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','approval.read',1),('admin','approval.decide',1),
  ('finance_admin','approval.read',1),('finance_admin','approval.decide',1),
  ('security_admin','approval.read',1),('security_admin','approval.decide',1);

CREATE TABLE permissions_prd078_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=14),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 保留每个租户已有权限主键、rowid 与审计时间，仅同步目录 revision。
INSERT INTO permissions_prd078_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       14,status,created_at,updated_at
FROM permissions;
-- 为每个租户实例化审批权限，tenant_id 与权限 ID 在同一表约束下生成。
INSERT INTO permissions_prd078_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       14,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant CROSS JOIN rbac_permission_templates template
WHERE template.permission_code IN('approval.read','approval.decide');
DROP TABLE permissions;
-- 仅在旧权限数据完整复制并补齐租户审批权限后替换权威 permissions 表。
ALTER TABLE permissions_prd078_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

-- 只给迁移前仍精确匹配默认矩阵的角色实例补充审批权限关系。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd078_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id
WHERE permission.code IN('approval.read','approval.decide');
-- 同步已扩展角色的权限修订与矩阵摘要，使旧会话授权快照立即失效。
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '73bdfac7bfe9fc41b783580f2459d71c87aa8aae02e3359a45918c5024434495'
    WHEN 'finance_admin' THEN '5b2aef1ae06711890946de8894623ea7c740f724cca886ce223b0b175fbc472a'
    WHEN 'security_admin' THEN '7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b'
  END
WHERE role_id IN(SELECT role_id FROM prd078_roles_to_expand);

-- 保真重建 BudgetBlock，仅把审批批准加入固定 release reason；历史行与 rowid 均保留。
DROP TRIGGER trg_budget_blocks_owner_insert;
DROP TRIGGER trg_budget_blocks_owner_update;
DROP TRIGGER trg_budget_blocks_prd052_scope_insert;
DROP TRIGGER trg_budget_blocks_prd052_scope_update;
CREATE TABLE budget_blocks_prd078_new(
  budget_block_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK(scope_type IN('tenant','project','application','virtual_key')),
  scope_id TEXT NOT NULL,
  budget_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','released')),
  blocked_cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK(blocked_cost_cny>=0),
  budget_cny NUMERIC NOT NULL CHECK(budget_cny>0),
  blocked_request_id TEXT NOT NULL,
  blocked_trace_id TEXT NOT NULL,
  blocked_at TEXT NOT NULL,
  release_reason TEXT CHECK(release_reason IS NULL OR release_reason IN(
    'budget_raised','mode_alert_only','rule_disabled','below_threshold',
    'business_day_ended','approval_approved')),
  released_by TEXT,
  release_request_id TEXT,
  release_trace_id TEXT,
  released_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(environment_id) REFERENCES environments(environment_id),
  FOREIGN KEY(budget_rule_id) REFERENCES budget_rules(budget_rule_id),
  FOREIGN KEY(released_by) REFERENCES users(user_id)
);
-- 保留 BudgetBlock 全部主键、rowid、租户与链路字段，仅扩展受约束的释放理由。
INSERT INTO budget_blocks_prd078_new(
  rowid,budget_block_id,tenant_id,environment_id,budget_rule_id,scope_type,scope_id,
  budget_date,status,blocked_cost_cny,budget_cny,blocked_request_id,blocked_trace_id,
  blocked_at,release_reason,released_by,release_request_id,release_trace_id,released_at,
  created_at,updated_at
)
SELECT rowid,budget_block_id,tenant_id,environment_id,budget_rule_id,scope_type,scope_id,
  budget_date,status,blocked_cost_cny,budget_cny,blocked_request_id,blocked_trace_id,
  blocked_at,release_reason,released_by,release_request_id,release_trace_id,released_at,
  created_at,updated_at FROM budget_blocks;
DROP TABLE budget_blocks;
-- 数据保真复制后替换旧预算阻断表，再恢复租户范围索引和 owner trigger。
ALTER TABLE budget_blocks_prd078_new RENAME TO budget_blocks;
CREATE UNIQUE INDEX idx_budget_blocks_one_active_rule_day
  ON budget_blocks(tenant_id,budget_rule_id,budget_date) WHERE status='active';
CREATE INDEX idx_budget_blocks_tenant_rule_status
  ON budget_blocks(tenant_id,budget_rule_id,status,blocked_at DESC);
CREATE INDEX idx_budget_blocks_environment_date
  ON budget_blocks(tenant_id,environment_id,budget_date,status);

-- ApprovalTask 是所有决策、票据、回写和事件的 tenant-qualified owner。
CREATE TABLE approval_tasks(
  approval_task_id TEXT PRIMARY KEY CHECK(length(approval_task_id)=36
    AND approval_task_id GLOB 'apr_*' AND substr(approval_task_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN('budget','session_risk')),
  source_id TEXT NOT NULL CHECK(length(source_id) BETWEEN 1 AND 160),
  target_type TEXT NOT NULL CHECK(target_type IN('budget_block_release','session_risk_action')),
  target_id TEXT NOT NULL CHECK(length(target_id) BETWEEN 1 AND 160),
  target_action TEXT NOT NULL CHECK(target_action IN(
    'release','freeze_session','freeze_virtual_key','dismiss','release_session','release_virtual_key')),
  target_reason_code TEXT CHECK(target_reason_code IS NULL OR target_reason_code IN(
    'confirmed_attack','false_positive','verified_safe','incident_contained')),
  target_version TEXT NOT NULL CHECK(length(target_version) BETWEEN 1 AND 160),
  target_fingerprint TEXT NOT NULL CHECK(length(target_fingerprint)=64
    AND target_fingerprint NOT GLOB '*[^0-9a-f]*'),
  risk_level TEXT NOT NULL CHECK(risk_level IN('standard','high')),
  status TEXT NOT NULL CHECK(status IN('pending','approved','rejected')),
  resolution_reason TEXT CHECK(resolution_reason IS NULL OR resolution_reason IN(
    'approved','user_rejected','timeout')),
  state_revision INTEGER NOT NULL CHECK(typeof(state_revision)='integer' AND state_revision>=1),
  writeback_status TEXT NOT NULL CHECK(writeback_status IN('pending','completed','failed')),
  required_permission_code TEXT NOT NULL CHECK(required_permission_code IN(
    'billing.budget.write','session_risk.manage')),
  created_by_type TEXT NOT NULL CHECK(created_by_type IN('service','user')),
  created_by_id TEXT NOT NULL CHECK(length(created_by_id) BETWEEN 1 AND 160),
  decided_by_user_id TEXT,
  decided_at TEXT CHECK(decided_at IS NULL OR unixepoch(decided_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  create_hmac_key_version INTEGER NOT NULL CHECK(
    typeof(create_hmac_key_version)='integer' AND create_hmac_key_version>=1),
  create_idempotency_key_hmac TEXT NOT NULL CHECK(length(create_idempotency_key_hmac)=64
    AND create_idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  create_intent_hmac TEXT NOT NULL CHECK(length(create_intent_hmac)=64
    AND create_intent_hmac NOT GLOB '*[^0-9a-f]*'),
  source_request_id TEXT NOT NULL CHECK(length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK(length(source_trace_id) BETWEEN 1 AND 160),
  decision_request_id TEXT CHECK(decision_request_id IS NULL OR length(decision_request_id) BETWEEN 1 AND 128),
  decision_trace_id TEXT CHECK(decision_trace_id IS NULL OR length(decision_trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,decided_by_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,approval_task_id),
  UNIQUE(tenant_id,create_idempotency_key_hmac),
  CHECK(unixepoch(expires_at)>unixepoch(created_at)),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((target_type='budget_block_release' AND source_type='budget'
      AND target_action='release' AND target_reason_code IS NULL
      AND risk_level='standard' AND required_permission_code='billing.budget.write')
    OR (target_type='session_risk_action' AND source_type='session_risk'
      AND target_action!='release' AND target_reason_code IS NOT NULL
      AND risk_level='high' AND required_permission_code='session_risk.manage')),
  CHECK((status='pending' AND resolution_reason IS NULL AND decided_by_user_id IS NULL
      AND decided_at IS NULL AND decision_request_id IS NULL AND decision_trace_id IS NULL)
    OR (status='approved' AND resolution_reason='approved' AND decided_by_user_id IS NOT NULL
      AND decided_at IS NOT NULL AND decision_request_id IS NOT NULL AND decision_trace_id IS NOT NULL)
    OR (status='rejected' AND resolution_reason='user_rejected' AND decided_by_user_id IS NOT NULL
      AND decided_at IS NOT NULL AND decision_request_id IS NOT NULL AND decision_trace_id IS NOT NULL)
    OR (status='rejected' AND resolution_reason='timeout' AND decided_by_user_id IS NULL
      AND decided_at IS NOT NULL AND decision_request_id IS NOT NULL AND decision_trace_id IS NOT NULL))
);
CREATE UNIQUE INDEX uq_approval_tasks_pending_source_prd078
  ON approval_tasks(tenant_id,source_type,source_id,target_action) WHERE status='pending';
CREATE INDEX idx_approval_tasks_tenant_status_prd078
  ON approval_tasks(tenant_id,status,created_at DESC,approval_task_id DESC);
CREATE INDEX idx_approval_tasks_due_prd078
  ON approval_tasks(status,expires_at,tenant_id,approval_task_id) WHERE status='pending';

CREATE TABLE approval_decisions(
  approval_decision_id TEXT PRIMARY KEY CHECK(length(approval_decision_id)=36
    AND approval_decision_id GLOB 'apd_*' AND substr(approval_decision_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  approval_task_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN('approve','reject','timeout')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'approved_business_need','risk_unacceptable','budget_not_approved','no_longer_needed','timeout')),
  actor_type TEXT NOT NULL CHECK(actor_type IN('user','system')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 160),
  revision_before INTEGER NOT NULL CHECK(typeof(revision_before)='integer' AND revision_before>=1),
  revision_after INTEGER NOT NULL CHECK(revision_after=revision_before+1),
  step_up_used INTEGER NOT NULL CHECK(step_up_used IN(0,1)),
  writeback_status TEXT NOT NULL CHECK(writeback_status IN('pending','completed','failed')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,approval_task_id) REFERENCES approval_tasks(tenant_id,approval_task_id),
  UNIQUE(tenant_id,approval_decision_id),
  UNIQUE(tenant_id,approval_task_id),
  CHECK((decision='approve' AND reason_code='approved_business_need')
    OR (decision='reject' AND reason_code IN('risk_unacceptable','budget_not_approved','no_longer_needed'))
    OR (decision='timeout' AND reason_code='timeout')),
  CHECK((actor_type='system' AND actor_id='approval-timeout-scheduler' AND decision='timeout'
      AND step_up_used=0)
    OR (actor_type='user' AND actor_id!='approval-timeout-scheduler' AND decision!='timeout'))
);
CREATE INDEX idx_approval_decisions_task_prd078
  ON approval_decisions(tenant_id,approval_task_id,decided_at,approval_decision_id);

CREATE TABLE approval_step_up_challenges(
  approval_challenge_id TEXT PRIMARY KEY CHECK(length(approval_challenge_id)=37
    AND approval_challenge_id GLOB 'apch_*' AND substr(approval_challenge_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  approval_task_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN('approve','reject')),
  task_revision INTEGER NOT NULL CHECK(typeof(task_revision)='integer' AND task_revision>=1),
  authorization_fingerprint TEXT NOT NULL CHECK(length(authorization_fingerprint)=64
    AND authorization_fingerprint NOT GLOB '*[^0-9a-f]*'),
  hmac_key_version INTEGER NOT NULL CHECK(typeof(hmac_key_version)='integer' AND hmac_key_version>=1),
  token_hmac TEXT NOT NULL CHECK(length(token_hmac)=64 AND token_hmac NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','consumed','revoked')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,approval_task_id) REFERENCES approval_tasks(tenant_id,approval_task_id),
  FOREIGN KEY(tenant_id,user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(tenant_id,session_id) REFERENCES sessions(tenant_id,session_id),
  UNIQUE(tenant_id,approval_challenge_id),
  UNIQUE(tenant_id,token_hmac),
  CHECK(unixepoch(expires_at)=unixepoch(created_at)+120),
  CHECK((status='active' AND consumed_at IS NULL AND revoked_at IS NULL)
    OR (status='consumed' AND consumed_at IS NOT NULL AND revoked_at IS NULL)
    OR (status='revoked' AND consumed_at IS NULL AND revoked_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_approval_challenge_active_prd078
  ON approval_step_up_challenges(tenant_id,approval_task_id,user_id,action) WHERE status='active';

CREATE TABLE approval_step_up_guards(
  tenant_id TEXT NOT NULL,
  approval_task_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  failed_attempts INTEGER NOT NULL CHECK(typeof(failed_attempts)='integer' AND failed_attempts BETWEEN 0 AND 5),
  locked_until TEXT CHECK(locked_until IS NULL OR unixepoch(locked_until) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(tenant_id,approval_task_id,user_id),
  FOREIGN KEY(tenant_id,approval_task_id) REFERENCES approval_tasks(tenant_id,approval_task_id),
  FOREIGN KEY(tenant_id,user_id) REFERENCES users(tenant_id,user_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((failed_attempts<5 AND locked_until IS NULL)
    OR (failed_attempts=5 AND locked_until IS NOT NULL
      AND unixepoch(locked_until)=unixepoch(updated_at)+900))
);

CREATE TABLE approval_action_replays(
  approval_action_replay_id TEXT PRIMARY KEY CHECK(length(approval_action_replay_id)=38
    AND approval_action_replay_id GLOB 'aprep_*' AND substr(approval_action_replay_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  approval_task_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN('approve','reject')),
  expected_revision INTEGER NOT NULL CHECK(typeof(expected_revision)='integer' AND expected_revision>=1),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'approved_business_need','risk_unacceptable','budget_not_approved','no_longer_needed')),
  hmac_key_version INTEGER NOT NULL CHECK(typeof(hmac_key_version)='integer' AND hmac_key_version>=1),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  intent_hmac TEXT NOT NULL CHECK(length(intent_hmac)=64 AND intent_hmac NOT GLOB '*[^0-9a-f]*'),
  approval_decision_id TEXT NOT NULL,
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,approval_task_id) REFERENCES approval_tasks(tenant_id,approval_task_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(tenant_id,approval_decision_id)
    REFERENCES approval_decisions(tenant_id,approval_decision_id),
  UNIQUE(tenant_id,approval_action_replay_id),
  UNIQUE(tenant_id,approval_decision_id),
  CHECK(unixepoch(expires_at)=unixepoch(created_at)+86400)
);
CREATE INDEX idx_approval_replays_idempotency_expiry_prd078
  ON approval_action_replays(
    tenant_id,actor_user_id,hmac_key_version,idempotency_key_hmac,expires_at
  );

CREATE TABLE approval_writebacks(
  approval_writeback_id TEXT PRIMARY KEY CHECK(length(approval_writeback_id)=37
    AND approval_writeback_id GLOB 'apwb_*' AND substr(approval_writeback_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  approval_task_id TEXT NOT NULL,
  adapter_kind TEXT NOT NULL CHECK(adapter_kind IN(
    'budget_block_release','session_risk_store_sync','no_effect')),
  target_fact_id TEXT CHECK(target_fact_id IS NULL OR length(target_fact_id) BETWEEN 1 AND 160),
  target_revision INTEGER CHECK(target_revision IS NULL OR(
    typeof(target_revision)='integer' AND target_revision>=1)),
  status TEXT NOT NULL CHECK(status IN('pending','processing','completed','failed')),
  attempt_count INTEGER NOT NULL CHECK(typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 10),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer' AND lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'approval.writeback_unavailable','approval.target_conflict')),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,approval_task_id) REFERENCES approval_tasks(tenant_id,approval_task_id),
  UNIQUE(tenant_id,approval_writeback_id),
  UNIQUE(tenant_id,approval_task_id),
  CHECK((status='pending' AND attempt_count BETWEEN 0 AND 9 AND next_attempt_at IS NOT NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND completed_at IS NULL)
    OR (status='processing' AND attempt_count BETWEEN 1 AND 10 AND next_attempt_at IS NULL
      AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
      AND completed_at IS NULL AND last_error_code IS NULL)
    OR (status='completed' AND next_attempt_at IS NULL AND lease_owner_id IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL AND completed_at IS NOT NULL
      AND last_error_code IS NULL)
    OR (status='failed' AND attempt_count=10 AND next_attempt_at IS NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND completed_at IS NULL AND last_error_code='approval.writeback_unavailable'))
  ,CHECK((adapter_kind='session_risk_store_sync'
      AND target_fact_id IS NOT NULL AND target_revision IS NOT NULL)
    OR (adapter_kind IN('budget_block_release','no_effect')
      AND target_fact_id IS NULL AND target_revision IS NULL))
);
CREATE INDEX idx_approval_writebacks_due_prd078
  ON approval_writebacks(status,next_attempt_at,lease_expires_at,tenant_id,approval_writeback_id)
  WHERE status IN('pending','processing');

CREATE TABLE approval_events(
  approval_event_id TEXT PRIMARY KEY CHECK(length(approval_event_id)=38
    AND approval_event_id GLOB 'apevt_*' AND substr(approval_event_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  approval_task_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN('approval.created','approval.timeout')),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(CAST(payload_json AS BLOB))<=4096),
  actor_type TEXT NOT NULL CHECK(actor_type IN('service','user','system')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 160),
  source_request_id TEXT NOT NULL CHECK(length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK(length(source_trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,approval_task_id) REFERENCES approval_tasks(tenant_id,approval_task_id),
  UNIQUE(tenant_id,approval_event_id),
  UNIQUE(tenant_id,approval_task_id,event_type),
  CHECK((event_type='approval.created' AND actor_type IN('service','user')
      AND actor_id!='approval-timeout-scheduler')
    OR (event_type='approval.timeout' AND actor_type='system'
      AND actor_id='approval-timeout-scheduler')),
  CHECK(unixepoch(created_at)=unixepoch(occurred_at))
);
CREATE INDEX idx_approval_events_tenant_prd078
  ON approval_events(tenant_id,occurred_at,approval_event_id);

CREATE TABLE approval_event_outbox(
  approval_outbox_id TEXT PRIMARY KEY CHECK(length(approval_outbox_id)=37
    AND approval_outbox_id GLOB 'apob_*' AND substr(approval_outbox_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  approval_event_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 10),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer' AND lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  subscription_snapshot_json TEXT CHECK(subscription_snapshot_json IS NULL OR(
    json_valid(subscription_snapshot_json) AND json_type(subscription_snapshot_json)='array'
    AND length(CAST(subscription_snapshot_json AS BLOB))<=65536)),
  snapshot_frozen_at TEXT CHECK(snapshot_frozen_at IS NULL OR unixepoch(snapshot_frozen_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'webhook.payload_invalid','webhook.delivery_unavailable')),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,approval_event_id) REFERENCES approval_events(tenant_id,approval_event_id),
  UNIQUE(tenant_id,approval_outbox_id),
  UNIQUE(tenant_id,approval_event_id),
  CHECK((subscription_snapshot_json IS NULL AND snapshot_frozen_at IS NULL)
    OR (subscription_snapshot_json IS NOT NULL AND snapshot_frozen_at IS NOT NULL)),
  CHECK((status='pending' AND attempt_count BETWEEN 0 AND 9 AND next_attempt_at IS NOT NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND delivered_at IS NULL AND (last_error_code IS NULL
        OR last_error_code='webhook.delivery_unavailable'))
    OR (status='processing' AND attempt_count BETWEEN 1 AND 10 AND next_attempt_at IS NULL
      AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
      AND delivered_at IS NULL AND last_error_code IS NULL)
    OR (status='delivered' AND attempt_count BETWEEN 0 AND 10 AND next_attempt_at IS NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND delivered_at IS NOT NULL AND last_error_code IS NULL
      AND subscription_snapshot_json IS NOT NULL)
    OR (status='failed' AND attempt_count BETWEEN 1 AND 10 AND next_attempt_at IS NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND delivered_at IS NULL AND last_error_code IS NOT NULL))
);
CREATE INDEX idx_approval_outbox_due_prd078
  ON approval_event_outbox(status,next_attempt_at,lease_expires_at,tenant_id,approval_outbox_id)
  WHERE status IN('pending','processing');

CREATE TABLE approval_scheduler_tenants(
  tenant_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN('idle','leased')),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer' AND lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_scan_at TEXT NOT NULL CHECK(unixepoch(next_scan_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((status='idle' AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
    OR (status='leased' AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL))
);

-- 恢复 revision-14 不可变目录、租户 owner 与新租户初始化守卫。未在本迁移中删除的
-- role/role-permission rowid、update、delete 守卫继续原样保留。
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
CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level
    AND template.catalog_revision=NEW.catalog_revision
) OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS(SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
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
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '73bdfac7bfe9fc41b783580f2459d71c87aa8aae02e3359a45918c5024434495'
    WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '5b2aef1ae06711890946de8894623ea7c740f724cca886ce223b0b175fbc472a'
    WHEN 'security_admin' THEN '7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b'
    WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current
    WHERE current.role_id=NEW.role_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin'
  OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code
  OR NEW.name!=OLD.name OR NEW.description!=OLD.description
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template
    ON template.role_code=role.code AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.status='active' AND permission.permission_id=NEW.permission_id
    AND permission.status='active'
) OR EXISTS(SELECT 1 FROM role_permissions current
  WHERE current.role_permission_id=NEW.role_permission_id
     OR (current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
         AND current.permission_id=NEW.permission_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM roles role
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户从 revision-14 固定目录初始化角色，矩阵摘要与审批默认权限保持一致。
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
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常和响应缓存。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。' END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '73bdfac7bfe9fc41b783580f2459d71c87aa8aae02e3359a45918c5024434495'
      WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '5b2aef1ae06711890946de8894623ea7c740f724cca886ce223b0b175fbc472a'
      WHEN 'security_admin' THEN '7f8a81d9d8e2a1d8e3e3991bc274ca3f5dd26eadcc6435d633c6da276727da7b'
      WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 为新租户实例化完整权限目录，所有权限行都携带触发器中的 NEW.tenant_id。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,
    template.permission_code,template.category,template.name,template.description,
    template.risk_level,14,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates template;
  -- 按固定角色模板建立同租户授权关系，禁止角色或权限跨 tenant_id 关联。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  )
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code WHERE role.tenant_id=NEW.tenant_id;
END;
DROP TABLE prd078_roles_to_expand;

-- BudgetBlock 只扩展 release_reason；原 owner/scope 合同逐字恢复。
CREATE TRIGGER trg_budget_blocks_owner_insert
BEFORE INSERT ON budget_blocks
WHEN NOT EXISTS(
  SELECT 1 FROM budget_rules rule
  JOIN environments environment ON environment.environment_id=rule.environment_id
    AND environment.tenant_id=rule.tenant_id
  WHERE rule.budget_rule_id=NEW.budget_rule_id AND rule.tenant_id=NEW.tenant_id
    AND rule.environment_id=NEW.environment_id)
BEGIN SELECT RAISE(ABORT,'budget_block_owner_mismatch'); END;
CREATE TRIGGER trg_budget_blocks_owner_update
BEFORE UPDATE OF tenant_id,environment_id,budget_rule_id ON budget_blocks
WHEN NOT EXISTS(
  SELECT 1 FROM budget_rules rule
  JOIN environments environment ON environment.environment_id=rule.environment_id
    AND environment.tenant_id=rule.tenant_id
  WHERE rule.budget_rule_id=NEW.budget_rule_id AND rule.tenant_id=NEW.tenant_id
    AND rule.environment_id=NEW.environment_id)
  OR EXISTS(SELECT 1 FROM budget_block_events event
    WHERE event.budget_block_id=OLD.budget_block_id
      AND (event.tenant_id<>NEW.tenant_id OR event.environment_id<>NEW.environment_id
        OR event.budget_rule_id<>NEW.budget_rule_id))
BEGIN SELECT RAISE(ABORT,'budget_block_owner_mismatch'); END;
CREATE TRIGGER trg_budget_blocks_prd052_scope_insert
BEFORE INSERT ON budget_blocks FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id)
BEGIN SELECT RAISE(ABORT,'budget_block_owner_scope_mismatch'); END;
CREATE TRIGGER trg_budget_blocks_prd052_scope_update
BEFORE UPDATE OF tenant_id,environment_id,budget_rule_id,scope_type,scope_id
ON budget_blocks FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM budget_rules parent
  WHERE parent.budget_rule_id=NEW.budget_rule_id AND parent.tenant_id=NEW.tenant_id
    AND parent.environment_id=NEW.environment_id
    AND parent.scope_type=NEW.scope_type AND parent.scope_id=NEW.scope_id)
BEGIN SELECT RAISE(ABORT,'budget_block_owner_scope_mismatch'); END;

-- Task 创建只能引用同租户固定 registry 目标；初态、HMAC 和 owner 一次冻结。
CREATE TRIGGER trg_approval_tasks_owner_insert_prd078
BEFORE INSERT ON approval_tasks FOR EACH ROW
WHEN NEW.status!='pending' OR NEW.state_revision!=1 OR NEW.writeback_status!='pending'
  OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM tenants tenant
    WHERE tenant.tenant_id=NEW.tenant_id AND tenant.status='active')
  OR (NEW.created_by_type='user' AND NOT EXISTS(SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.created_by_id
      AND actor.status='active'))
  OR (NEW.target_type='budget_block_release' AND NOT EXISTS(
    SELECT 1 FROM budget_blocks block JOIN budget_rules rule
      ON rule.tenant_id=block.tenant_id AND rule.budget_rule_id=block.budget_rule_id
    WHERE block.tenant_id=NEW.tenant_id AND block.budget_block_id=NEW.source_id
      AND block.budget_rule_id=NEW.target_id AND block.status='active'))
  OR (NEW.target_type='session_risk_action' AND NOT EXISTS(
    SELECT 1 FROM session_risk_scores score
    WHERE score.tenant_id=NEW.tenant_id AND score.session_risk_score_id=NEW.target_id
      AND score.status IN('high_risk','frozen')))
  OR EXISTS(SELECT 1 FROM approval_tasks current
    WHERE current.approval_task_id=NEW.approval_task_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'approval_task_owner_mismatch'); END;
CREATE TRIGGER trg_approval_tasks_transition_prd078
BEFORE UPDATE ON approval_tasks FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.approval_task_id!=OLD.approval_task_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.source_type!=OLD.source_type
  OR NEW.source_id!=OLD.source_id OR NEW.target_type!=OLD.target_type
  OR NEW.target_id!=OLD.target_id OR NEW.target_action!=OLD.target_action
  OR NEW.target_reason_code IS NOT OLD.target_reason_code
  OR NEW.target_version!=OLD.target_version OR NEW.target_fingerprint!=OLD.target_fingerprint
  OR NEW.risk_level!=OLD.risk_level OR NEW.required_permission_code!=OLD.required_permission_code
  OR NEW.created_by_type!=OLD.created_by_type OR NEW.created_by_id!=OLD.created_by_id
  OR NEW.expires_at!=OLD.expires_at OR NEW.create_hmac_key_version!=OLD.create_hmac_key_version
  OR NEW.create_idempotency_key_hmac!=OLD.create_idempotency_key_hmac
  OR NEW.create_intent_hmac!=OLD.create_intent_hmac
  OR NEW.source_request_id!=OLD.source_request_id OR NEW.source_trace_id!=OLD.source_trace_id
  OR NEW.created_at!=OLD.created_at
  OR NOT (
    (OLD.status='pending' AND NEW.status IN('approved','rejected')
      AND NEW.state_revision=OLD.state_revision+1
      AND NEW.decided_at=NEW.updated_at AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
      AND ((NEW.status='approved' AND NEW.resolution_reason='approved'
          AND NEW.decided_by_user_id IS NOT NULL
          AND ((NEW.target_type='budget_block_release' AND NEW.writeback_status='completed')
            OR (NEW.target_type='session_risk_action' AND NEW.writeback_status='pending')))
        OR (NEW.status='rejected' AND NEW.resolution_reason IN('user_rejected','timeout')
          AND NEW.writeback_status='completed'
          AND ((NEW.resolution_reason='user_rejected' AND NEW.decided_by_user_id IS NOT NULL)
            OR (NEW.resolution_reason='timeout' AND NEW.decided_by_user_id IS NULL)))))
    OR (OLD.status='approved' AND NEW.status='approved'
      AND OLD.target_type='session_risk_action' AND OLD.writeback_status='pending'
      AND NEW.writeback_status IN('completed','failed')
      AND NEW.state_revision=OLD.state_revision
      AND NEW.resolution_reason=OLD.resolution_reason
      AND NEW.decided_by_user_id=OLD.decided_by_user_id
      AND NEW.decided_at=OLD.decided_at
      AND NEW.decision_request_id=OLD.decision_request_id
      AND NEW.decision_trace_id=OLD.decision_trace_id
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at))
  )
BEGIN SELECT RAISE(ABORT,'approval_task_transition_invalid'); END;
CREATE TRIGGER trg_approval_tasks_delete_prd078
BEFORE DELETE ON approval_tasks FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_task_immutable'); END;

-- Decision 是终态 Task 的唯一 append-only 审计；actor、revision、请求与时间逐字段绑定。
CREATE TRIGGER trg_approval_decisions_owner_insert_prd078
BEFORE INSERT ON approval_decisions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM approval_tasks task
  WHERE task.tenant_id=NEW.tenant_id AND task.approval_task_id=NEW.approval_task_id
    AND task.state_revision=NEW.revision_after
    AND task.writeback_status=NEW.writeback_status
    AND task.decision_request_id=NEW.request_id AND task.decision_trace_id=NEW.trace_id
    AND task.decided_at=NEW.decided_at
    AND ((NEW.decision='approve' AND task.status='approved'
        AND task.resolution_reason='approved' AND NEW.actor_type='user'
        AND task.decided_by_user_id=NEW.actor_id)
      OR (NEW.decision='reject' AND task.status='rejected'
        AND task.resolution_reason='user_rejected' AND NEW.actor_type='user'
        AND task.decided_by_user_id=NEW.actor_id)
      OR (NEW.decision='timeout' AND task.status='rejected'
        AND task.resolution_reason='timeout' AND NEW.actor_type='system'
        AND task.decided_by_user_id IS NULL))
    AND ((NEW.actor_type='system' AND task.risk_level IN('standard','high'))
      OR (NEW.actor_type='user'
        AND EXISTS(SELECT 1 FROM users actor WHERE actor.tenant_id=NEW.tenant_id
          AND actor.user_id=NEW.actor_id AND actor.status='active')
        AND NEW.step_up_used=CASE task.risk_level WHEN 'high' THEN 1 ELSE 0 END))
  )
  OR EXISTS(SELECT 1 FROM approval_decisions current
    WHERE current.approval_decision_id=NEW.approval_decision_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'approval_decision_owner_mismatch'); END;
CREATE TRIGGER trg_approval_decisions_update_prd078
BEFORE UPDATE ON approval_decisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_decision_immutable'); END;
CREATE TRIGGER trg_approval_decisions_delete_prd078
BEFORE DELETE ON approval_decisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_decision_immutable'); END;

CREATE TRIGGER trg_approval_challenges_owner_insert_prd078
BEFORE INSERT ON approval_step_up_challenges FOR EACH ROW
WHEN NEW.status!='active' OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM approval_tasks task
    JOIN users actor ON actor.tenant_id=task.tenant_id AND actor.user_id=NEW.user_id
    JOIN sessions session ON session.tenant_id=actor.tenant_id
      AND session.user_id=actor.user_id AND session.session_id=NEW.session_id
    WHERE task.tenant_id=NEW.tenant_id AND task.approval_task_id=NEW.approval_task_id
      AND task.status='pending' AND task.risk_level='high'
      AND task.state_revision=NEW.task_revision AND actor.status='active'
      AND session.status='active' AND unixepoch(session.expires_at)>unixepoch(NEW.created_at)
      AND unixepoch(task.expires_at)>unixepoch(NEW.created_at))
  OR EXISTS(SELECT 1 FROM approval_step_up_challenges current
    WHERE current.approval_challenge_id=NEW.approval_challenge_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'approval_challenge_owner_mismatch'); END;
CREATE TRIGGER trg_approval_challenges_transition_prd078
BEFORE UPDATE ON approval_step_up_challenges FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.approval_challenge_id!=OLD.approval_challenge_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.approval_task_id!=OLD.approval_task_id
  OR NEW.user_id!=OLD.user_id OR NEW.session_id!=OLD.session_id OR NEW.action!=OLD.action
  OR NEW.task_revision!=OLD.task_revision
  OR NEW.authorization_fingerprint!=OLD.authorization_fingerprint
  OR NEW.hmac_key_version!=OLD.hmac_key_version OR NEW.token_hmac!=OLD.token_hmac
  OR NEW.expires_at!=OLD.expires_at OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status NOT IN('consumed','revoked')
  OR NEW.updated_at!=CASE NEW.status WHEN 'consumed' THEN NEW.consumed_at ELSE NEW.revoked_at END
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'approval_challenge_transition_invalid'); END;
CREATE TRIGGER trg_approval_challenges_delete_prd078
BEFORE DELETE ON approval_step_up_challenges FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_challenge_immutable'); END;

CREATE TRIGGER trg_approval_guards_owner_insert_prd078
BEFORE INSERT ON approval_step_up_guards FOR EACH ROW
WHEN NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM approval_tasks task JOIN users actor
    ON actor.tenant_id=task.tenant_id AND actor.user_id=NEW.user_id
    WHERE task.tenant_id=NEW.tenant_id AND task.approval_task_id=NEW.approval_task_id
      AND task.status='pending' AND task.risk_level='high' AND actor.status='active')
BEGIN SELECT RAISE(ABORT,'approval_step_up_guard_owner_mismatch'); END;
CREATE TRIGGER trg_approval_guards_transition_prd078
BEFORE UPDATE ON approval_step_up_guards FOR EACH ROW
WHEN NEW.tenant_id!=OLD.tenant_id OR NEW.approval_task_id!=OLD.approval_task_id
  OR NEW.user_id!=OLD.user_id OR NEW.created_at!=OLD.created_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT ((NEW.failed_attempts=OLD.failed_attempts+1 AND OLD.failed_attempts<5)
    OR (NEW.failed_attempts=0 AND OLD.failed_attempts>0
      AND (OLD.locked_until IS NULL OR unixepoch(NEW.updated_at)>=unixepoch(OLD.locked_until))))
BEGIN SELECT RAISE(ABORT,'approval_step_up_guard_transition_invalid'); END;
CREATE TRIGGER trg_approval_guards_delete_prd078
BEFORE DELETE ON approval_step_up_guards FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_step_up_guard_immutable'); END;

CREATE TRIGGER trg_approval_replays_owner_insert_prd078
BEFORE INSERT ON approval_action_replays FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM approval_decisions decision
  JOIN approval_tasks task ON task.tenant_id=decision.tenant_id
    AND task.approval_task_id=decision.approval_task_id
  JOIN users actor ON actor.tenant_id=task.tenant_id AND actor.user_id=NEW.actor_user_id
  WHERE decision.tenant_id=NEW.tenant_id
    AND decision.approval_decision_id=NEW.approval_decision_id
    AND decision.approval_task_id=NEW.approval_task_id
    AND decision.actor_type='user' AND decision.actor_id=NEW.actor_user_id
    AND decision.decision=NEW.action AND decision.reason_code=NEW.reason_code
    AND decision.revision_before=NEW.expected_revision AND actor.status='active'
    AND NEW.created_at=decision.decided_at)
  OR EXISTS(SELECT 1 FROM approval_action_replays current
    WHERE current.approval_action_replay_id=NEW.approval_action_replay_id
      OR (current.tenant_id=NEW.tenant_id
        AND current.actor_user_id=NEW.actor_user_id
        AND current.hmac_key_version=NEW.hmac_key_version
        AND current.idempotency_key_hmac=NEW.idempotency_key_hmac
        -- BEGIN IMMEDIATE serializes writers. Retain expired history while
        -- rejecting a digest whose 24-hour replay window is still active.
        AND julianday(current.expires_at)>julianday(NEW.created_at))
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'approval_replay_owner_mismatch'); END;
CREATE TRIGGER trg_approval_replays_update_prd078
BEFORE UPDATE ON approval_action_replays FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_replay_immutable'); END;
CREATE TRIGGER trg_approval_replays_delete_prd078
BEFORE DELETE ON approval_action_replays FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_replay_immutable'); END;

-- 回写初态与 Decision/Task/目标事实绑定；只有 session-risk 对账可进入租约状态机。
CREATE TRIGGER trg_approval_writebacks_owner_insert_prd078
BEFORE INSERT ON approval_writebacks FOR EACH ROW
WHEN NEW.attempt_count!=0 OR NEW.lease_generation!=0 OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM approval_tasks task JOIN approval_decisions decision
    ON decision.tenant_id=task.tenant_id AND decision.approval_task_id=task.approval_task_id
    WHERE task.tenant_id=NEW.tenant_id AND task.approval_task_id=NEW.approval_task_id
      AND decision.writeback_status=task.writeback_status
      AND ((decision.decision='approve' AND task.target_type='budget_block_release'
          AND NEW.adapter_kind='budget_block_release' AND NEW.status='completed'
          AND task.writeback_status='completed' AND NEW.completed_at=NEW.created_at)
        OR (decision.decision='approve' AND task.target_type='session_risk_action'
          AND NEW.adapter_kind='session_risk_store_sync' AND NEW.status='pending'
          AND task.writeback_status='pending' AND NEW.next_attempt_at=NEW.created_at
          AND EXISTS(SELECT 1 FROM session_risk_actions action
            WHERE action.tenant_id=NEW.tenant_id
              AND action.session_risk_action_id=NEW.target_fact_id
              AND action.session_risk_score_id=task.target_id
              AND action.state_revision=NEW.target_revision))
        OR (decision.decision IN('reject','timeout') AND NEW.adapter_kind='no_effect'
          AND NEW.status='completed' AND task.writeback_status='completed'
          AND NEW.completed_at=NEW.created_at)))
  OR EXISTS(SELECT 1 FROM approval_writebacks current
    WHERE current.approval_writeback_id=NEW.approval_writeback_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'approval_writeback_owner_mismatch'); END;
CREATE TRIGGER trg_approval_writebacks_transition_prd078
BEFORE UPDATE ON approval_writebacks FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.approval_writeback_id!=OLD.approval_writeback_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.approval_task_id!=OLD.approval_task_id
  OR NEW.adapter_kind!=OLD.adapter_kind OR NEW.target_fact_id IS NOT OLD.target_fact_id
  OR NEW.target_revision IS NOT OLD.target_revision OR NEW.created_at!=OLD.created_at
  OR OLD.adapter_kind!='session_risk_store_sync'
  OR NEW.lease_generation!=OLD.lease_generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT (
    (OLD.status='pending' AND NEW.status='processing'
      AND NEW.attempt_count=OLD.attempt_count+1
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 160
      AND NEW.lease_token IS NOT NULL AND length(NEW.lease_token)=64
      AND NEW.lease_token NOT GLOB '*[^0-9a-f]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90)
    OR (OLD.status='processing' AND NEW.status='processing'
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count+1 AND NEW.lease_token!=OLD.lease_token
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 160
      AND NEW.lease_token IS NOT NULL AND length(NEW.lease_token)=64
      AND NEW.lease_token NOT GLOB '*[^0-9a-f]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90)
    OR (OLD.status='processing' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.attempt_count<10
      AND NEW.last_error_code='approval.writeback_unavailable'
      AND unixepoch(NEW.next_attempt_at)=unixepoch(NEW.updated_at)+CASE NEW.attempt_count
        WHEN 1 THEN 5 WHEN 2 THEN 30 ELSE 300 END)
    OR (OLD.status='processing' AND NEW.status='completed'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.completed_at=NEW.updated_at)
    OR (OLD.status='processing' AND NEW.status='failed'
      AND NEW.attempt_count=10 AND NEW.last_error_code='approval.writeback_unavailable')
  )
BEGIN SELECT RAISE(ABORT,'approval_writeback_transition_invalid'); END;
CREATE TRIGGER trg_approval_writebacks_task_progress_prd078
AFTER UPDATE OF status ON approval_writebacks FOR EACH ROW
WHEN NEW.status IN('completed','failed') AND OLD.status='processing'
BEGIN
  -- 仅把同租户已批准任务的 pending 回写推进到 worker 终态，决策事实保持只追加。
  UPDATE approval_tasks SET writeback_status=NEW.status,updated_at=NEW.updated_at
  WHERE tenant_id=NEW.tenant_id AND approval_task_id=NEW.approval_task_id
    AND status='approved' AND writeback_status='pending';
END;
CREATE TRIGGER trg_approval_writebacks_delete_prd078
BEFORE DELETE ON approval_writebacks FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_writeback_immutable'); END;

-- Event payload 是可由 Task 精确重建的安全 JSON，未知字段或业务正文不能进入 source。
CREATE TRIGGER trg_approval_events_owner_insert_prd078
BEFORE INSERT ON approval_events FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM approval_tasks task
  WHERE task.tenant_id=NEW.tenant_id AND task.approval_task_id=NEW.approval_task_id
    AND json_extract(NEW.payload_json,'$.schema_version')=1
    AND json_extract(NEW.payload_json,'$.approval_id')=task.approval_task_id
    AND json_extract(NEW.payload_json,'$.status')=task.status
    AND json_extract(NEW.payload_json,'$.target_type')=task.target_type
    AND json_extract(NEW.payload_json,'$.target_action')=task.target_action
    AND ((NEW.event_type='approval.created'
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=9
      AND NOT EXISTS(SELECT 1 FROM json_each(NEW.payload_json) field WHERE field.key NOT IN(
        'schema_version','approval_id','status','target_type','target_action','risk_level',
        'requires_step_up','expires_at','entry'))
      AND task.status='pending' AND NEW.actor_type=task.created_by_type
      AND NEW.actor_id=task.created_by_id AND NEW.source_request_id=task.source_request_id
      AND NEW.source_trace_id=task.source_trace_id AND NEW.occurred_at=task.created_at
      AND json_extract(NEW.payload_json,'$.risk_level')=task.risk_level
      AND json_extract(NEW.payload_json,'$.requires_step_up')=CASE task.risk_level
        WHEN 'high' THEN 1 ELSE 0 END
      AND json_extract(NEW.payload_json,'$.expires_at')=task.expires_at
      AND json_extract(NEW.payload_json,'$.entry')='approval-card')
    OR (NEW.event_type='approval.timeout'
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=7
      AND NOT EXISTS(SELECT 1 FROM json_each(NEW.payload_json) field WHERE field.key NOT IN(
        'schema_version','approval_id','status','target_type','target_action',
        'resolution_reason','decided_at'))
      AND task.status='rejected' AND task.resolution_reason='timeout'
      AND NEW.actor_type='system' AND NEW.actor_id='approval-timeout-scheduler'
      AND NEW.source_request_id=task.decision_request_id
      AND NEW.source_trace_id=task.decision_trace_id AND NEW.occurred_at=task.decided_at
      AND json_extract(NEW.payload_json,'$.resolution_reason')='timeout'
      AND json_extract(NEW.payload_json,'$.decided_at')=task.decided_at)))
  OR EXISTS(SELECT 1 FROM approval_events current
    WHERE current.approval_event_id=NEW.approval_event_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'approval_event_owner_mismatch'); END;
CREATE TRIGGER trg_approval_events_update_prd078
BEFORE UPDATE ON approval_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_event_immutable'); END;
CREATE TRIGGER trg_approval_events_delete_prd078
BEFORE DELETE ON approval_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_event_immutable'); END;

CREATE TRIGGER trg_approval_outbox_owner_insert_prd078
BEFORE INSERT ON approval_event_outbox FOR EACH ROW
WHEN NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.lease_generation!=0
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_token IS NOT NULL
  OR NEW.lease_expires_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
  OR NEW.delivered_at IS NOT NULL OR NEW.created_at!=NEW.updated_at
  OR NEW.next_attempt_at!=NEW.created_at
  OR NEW.subscription_snapshot_json IS NULL
  OR NEW.snapshot_frozen_at!=NEW.created_at
  OR NOT EXISTS(SELECT 1 FROM approval_events event
    WHERE event.tenant_id=NEW.tenant_id AND event.approval_event_id=NEW.approval_event_id
      AND event.created_at=NEW.created_at)
BEGIN SELECT RAISE(ABORT,'approval_outbox_owner_mismatch'); END;
CREATE TRIGGER trg_approval_outbox_transition_prd078
BEFORE UPDATE ON approval_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.approval_outbox_id!=OLD.approval_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.approval_event_id!=OLD.approval_event_id
  OR NEW.created_at!=OLD.created_at OR NEW.lease_generation!=OLD.lease_generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR (OLD.subscription_snapshot_json IS NOT NULL
    AND (NEW.subscription_snapshot_json!=OLD.subscription_snapshot_json
      OR NEW.snapshot_frozen_at!=OLD.snapshot_frozen_at))
  OR (OLD.subscription_snapshot_json IS NULL AND NEW.subscription_snapshot_json IS NOT NULL
    AND NEW.snapshot_frozen_at!=NEW.updated_at)
  OR NOT (
    (OLD.status='pending' AND NEW.status='processing'
      AND NEW.attempt_count=OLD.attempt_count+1
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 160
      AND NEW.lease_token IS NOT NULL AND length(NEW.lease_token)=64
      AND NEW.lease_token NOT GLOB '*[^0-9a-f]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90)
    OR (OLD.status='processing' AND NEW.status='processing'
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count+1 AND NEW.lease_token!=OLD.lease_token
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 160
      AND NEW.lease_token IS NOT NULL AND length(NEW.lease_token)=64
      AND NEW.lease_token NOT GLOB '*[^0-9a-f]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90)
    OR (OLD.status='processing' AND NEW.status='processing'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.lease_owner_id=OLD.lease_owner_id AND NEW.lease_token=OLD.lease_token
      AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at)
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90)
    OR (OLD.status='processing' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.attempt_count<10
      AND NEW.last_error_code='webhook.delivery_unavailable'
      AND unixepoch(NEW.next_attempt_at)=unixepoch(NEW.updated_at)+CASE NEW.attempt_count
        WHEN 1 THEN 5 WHEN 2 THEN 30 ELSE 300 END)
    OR (OLD.status='processing' AND NEW.status='delivered'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.delivered_at=NEW.updated_at
      AND NEW.subscription_snapshot_json IS NOT NULL)
    OR (OLD.status='processing' AND NEW.status='failed'
      AND NEW.attempt_count=OLD.attempt_count
      AND ((NEW.last_error_code='webhook.payload_invalid')
        OR (NEW.attempt_count=10 AND NEW.last_error_code='webhook.delivery_unavailable')))
  )
BEGIN SELECT RAISE(ABORT,'approval_outbox_transition_invalid'); END;
CREATE TRIGGER trg_approval_outbox_delete_prd078
BEFORE DELETE ON approval_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_outbox_immutable'); END;

CREATE TRIGGER trg_approval_scheduler_owner_insert_prd078
BEFORE INSERT ON approval_scheduler_tenants FOR EACH ROW
WHEN NEW.status!='idle' OR NEW.lease_generation!=0 OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM tenants tenant
    WHERE tenant.tenant_id=NEW.tenant_id AND tenant.status='active')
BEGIN SELECT RAISE(ABORT,'approval_scheduler_owner_mismatch'); END;
CREATE TRIGGER trg_approval_scheduler_transition_prd078
BEFORE UPDATE ON approval_scheduler_tenants FOR EACH ROW
WHEN NEW.tenant_id!=OLD.tenant_id OR NEW.created_at!=OLD.created_at
  OR NEW.lease_generation!=OLD.lease_generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT (
    (OLD.status='idle' AND NEW.status='leased'
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 160
      AND NEW.lease_token IS NOT NULL AND length(NEW.lease_token)=64
      AND NEW.lease_token NOT GLOB '*[^0-9a-f]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90)
    OR (OLD.status='leased' AND NEW.status='leased'
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)
      AND NEW.lease_token!=OLD.lease_token
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 160
      AND NEW.lease_token IS NOT NULL AND length(NEW.lease_token)=64
      AND NEW.lease_token NOT GLOB '*[^0-9a-f]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90)
    OR (OLD.status='leased' AND NEW.status='idle'
      AND NEW.lease_owner_id IS NULL AND NEW.lease_token IS NULL
      AND NEW.lease_expires_at IS NULL AND unixepoch(NEW.next_scan_at)>=unixepoch(NEW.updated_at))
  )
BEGIN SELECT RAISE(ABORT,'approval_scheduler_transition_invalid'); END;
CREATE TRIGGER trg_approval_scheduler_delete_prd078
BEFORE DELETE ON approval_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_scheduler_immutable'); END;

-- 事务末尾复核目录、默认矩阵、九表、BudgetBlock 与数据库健康；任何偏差原子回滚。
CREATE TEMP TABLE prd078_up_final_guard(
  issue_count INTEGER NOT NULL
    CONSTRAINT prd078_up_final_guard_ok CHECK(issue_count=0)
);
-- 提交前汇总目录、表、触发器、租户关系、外键和完整性问题，非零即回滚整次迁移。
INSERT INTO prd078_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=72
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=14)
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=194
      AND NOT EXISTS(SELECT 1 FROM permissions WHERE catalog_revision!=14)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||
          description||char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='8a3781ececf4fe0e3d2607215a4f390d09712b0421051d6e776919c9a9c47c8a'
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='26d89a9cde93032441d74150a8aa3eadda06745103172b90a099c4942794b421'
      AND NOT EXISTS(SELECT 1 FROM tenants tenant CROSS JOIN rbac_permission_templates template
        WHERE NOT EXISTS(SELECT 1 FROM permissions permission
          WHERE permission.tenant_id=tenant.tenant_id AND permission.code=template.permission_code))
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'approval_tasks','approval_decisions','approval_step_up_challenges',
      'approval_step_up_guards','approval_action_replays','approval_writebacks',
      'approval_events','approval_event_outbox','approval_scheduler_tenants'))=9
    THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='budget_blocks'),
      "'approval_approved'")>0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd078_up_final_guard;

COMMIT;
PRAGMA foreign_keys = ON;
