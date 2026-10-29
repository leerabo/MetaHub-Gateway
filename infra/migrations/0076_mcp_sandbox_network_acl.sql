-- PRD-083: isolated MCP sandbox network ACL, immutable policy revisions and durable facts.
-- Sensitive endpoint, credential, permit, arguments and response material is never stored here.
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

DROP TABLE IF EXISTS temp.prd083_up_guard;
CREATE TEMP TABLE prd083_up_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd083_up_guard_abort
BEFORE INSERT ON prd083_up_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd083.mcp_drain_required'); END;
INSERT INTO prd083_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0075' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=84
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=17)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='f287152f0ebc4d416e69efc756f8c2021fdcf2e1624218e3d7fbe1aee6371870'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=221
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='f57de6868c350c9bdfb2eda628f56114e25adce04cde235ea9c699dea6591d7d'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM mcp_server_operations WHERE status IN('queued','processing'))
  + (SELECT COUNT(*) FROM mcp_server_sync_jobs WHERE status IN('pending','processing'))
  + (SELECT COUNT(*) FROM mcp_invocations
      WHERE status IN('approval_pending','queued','probing','calling'))
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'mcp_server_operations','mcp_server_sync_jobs','mcp_invocations',
      'mcp_invocation_attempts','audit_logs'))=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd083_up_guard_abort;
DROP TABLE prd083_up_guard;

-- Expand only roles that still exactly match the revision-17 default relation set.
CREATE TEMP TABLE prd083_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','security_admin','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '74f0fa72ef84c474a86b7d8893c70eb271f53a4fa2c9385049016358cd6b64f5'
    WHEN 'security_admin' THEN '7fbaee66f9ce303159b9b55c62c9050969a18bcdb9c98f374aa0358a487b00c4'
    WHEN 'ops_admin' THEN 'a5a7af8d089fc9d3d7d33b753e89f4141f83cf67ad6dd2b5d36deba234ceae29'
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

CREATE TABLE rbac_permission_templates_prd083_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=18)
);
INSERT INTO rbac_permission_templates_prd083_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,18
FROM rbac_permission_templates;
INSERT INTO rbac_permission_templates_prd083_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES(
  'mcp.network.manage','mcp','管理 MCP 出站网络',
  '管理当前租户 MCP Server 的出站网络策略与熔断边界。','critical',18
);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd083_new RENAME TO rbac_permission_templates;

INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES('admin','mcp.network.manage',1),('security_admin','mcp.network.manage',1),
  ('ops_admin','mcp.network.manage',1);

CREATE TABLE permissions_prd083_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=18),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
INSERT INTO permissions_prd083_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  18,status,created_at,updated_at FROM permissions;
INSERT INTO permissions_prd083_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,18,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code='mcp.network.manage';
DROP TABLE permissions;
ALTER TABLE permissions_prd083_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd083_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id AND permission.code='mcp.network.manage';
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '5af655a2ffa8cb4f14d84bec71e1b0feb17e4a1e2d982c9fc9cbe711e74cead7'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN 'dc3641880859dff9a82069c6c4b1f32049320aa9406e0f885770ad643da04c1e'
  END
WHERE role_id IN(SELECT role_id FROM prd083_roles_to_expand);

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
    WHEN 'admin' THEN '5af655a2ffa8cb4f14d84bec71e1b0feb17e4a1e2d982c9fc9cbe711e74cead7'
    WHEN 'developer' THEN '69328ccc0d6741562250f7b83fe7cf3fce8509db4b092b6917b475e3dd446b8d'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN 'dc3641880859dff9a82069c6c4b1f32049320aa9406e0f885770ad643da04c1e'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current WHERE current.role_id=NEW.role_id
    OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin'
  OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64
  OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
    AND template.permission_code=permission.code
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
WHEN EXISTS(SELECT 1 FROM roles role WHERE role.tenant_id=OLD.tenant_id
  AND role.role_id=OLD.role_id AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户按 revision 18 冻结目录创建六个默认角色及其矩阵摘要。
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常、套餐额度和账单争议。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。'
    END,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '5af655a2ffa8cb4f14d84bec71e1b0feb17e4a1e2d982c9fc9cbe711e74cead7'
      WHEN 'developer' THEN '69328ccc0d6741562250f7b83fe7cf3fce8509db4b092b6917b475e3dd446b8d'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN 'dc3641880859dff9a82069c6c4b1f32049320aa9406e0f885770ad643da04c1e'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 新租户复制完整权限目录，所有权限行均绑定 NEW tenant_id。
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,18,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  -- 新租户只按冻结角色模板写入默认授权关系，禁止跨租户关联。
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code WHERE role.tenant_id=NEW.tenant_id;
END;
DROP TABLE prd083_roles_to_expand;

-- Rebuild the four PRD-082 state tables while preserving every legacy rowid and value.
CREATE TABLE mcp_server_operations_prd083_new(
  mcp_server_operation_id TEXT PRIMARY KEY CHECK(length(mcp_server_operation_id)=37
    AND substr(mcp_server_operation_id,1,5)='mcpo_'
    AND substr(mcp_server_operation_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  operation_type TEXT NOT NULL CHECK(operation_type IN(
    'register','refresh','rotate','update','disable','enable')),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  idempotency_key_hmac_version INTEGER NOT NULL CHECK(
    typeof(idempotency_key_hmac_version)='integer' AND idempotency_key_hmac_version>=1),
  intent_hmac TEXT NOT NULL CHECK(length(intent_hmac)=64
    AND intent_hmac NOT GLOB '*[^0-9a-f]*'),
  intent_hmac_version INTEGER NOT NULL CHECK(
    typeof(intent_hmac_version)='integer' AND intent_hmac_version>=1),
  actor_user_id TEXT NOT NULL,
  start_server_revision INTEGER NOT NULL CHECK(
    typeof(start_server_revision)='integer' AND start_server_revision>=1),
  start_credential_revision INTEGER CHECK(start_credential_revision IS NULL OR(
    typeof(start_credential_revision)='integer' AND start_credential_revision>=1)),
  candidate_credential_revision INTEGER CHECK(candidate_credential_revision IS NULL OR(
    typeof(candidate_credential_revision)='integer' AND candidate_credential_revision>=1)),
  status TEXT NOT NULL CHECK(status IN('queued','processing','succeeded','failed')),
  error_code TEXT CHECK(error_code IS NULL OR error_code IN(
    'mcp.executor_disabled','mcp.sync_failed','mcp.credential_invalid',
    'mcp.state_conflict','mcp.unavailable','mcp.network_blocked',
    'mcp.sandbox_unavailable')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_server_id) REFERENCES mcp_servers(tenant_id,mcp_server_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(tenant_id,mcp_server_id,candidate_credential_revision)
    REFERENCES mcp_server_credentials(tenant_id,mcp_server_id,credential_revision)
    DEFERRABLE INITIALLY DEFERRED,
  UNIQUE(tenant_id,mcp_server_operation_id),
  UNIQUE(tenant_id,actor_user_id,idempotency_key_hmac_version,idempotency_key_hmac),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((status IN('queued','processing') AND completed_at IS NULL AND error_code IS NULL)
    OR (status='succeeded' AND completed_at IS NOT NULL AND error_code IS NULL)
    OR (status='failed' AND completed_at IS NOT NULL AND error_code IS NOT NULL))
);
CREATE TABLE mcp_server_sync_jobs_prd083_new(
  mcp_server_sync_job_id TEXT PRIMARY KEY CHECK(length(mcp_server_sync_job_id)=37
    AND substr(mcp_server_sync_job_id,1,5)='mcpj_'
    AND substr(mcp_server_sync_job_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  mcp_server_operation_id TEXT NOT NULL,
  candidate_credential_revision INTEGER,
  job_type TEXT NOT NULL CHECK(job_type IN('discover','credential_probe')),
  status TEXT NOT NULL CHECK(status IN('waiting_policy','pending','processing','succeeded','failed')),
  attempt_count INTEGER NOT NULL CHECK(typeof(attempt_count)='integer'
    AND attempt_count BETWEEN 0 AND 10),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer'
    AND lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  deadline_at TEXT CHECK(deadline_at IS NULL OR unixepoch(deadline_at) IS NOT NULL),
  error_code TEXT CHECK(error_code IS NULL OR error_code IN(
    'mcp.executor_disabled','mcp.sync_failed','mcp.credential_invalid',
    'mcp.state_conflict','mcp.unavailable','mcp.network_blocked',
    'mcp.sandbox_unavailable')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_server_id) REFERENCES mcp_servers(tenant_id,mcp_server_id),
  FOREIGN KEY(tenant_id,mcp_server_operation_id)
    REFERENCES mcp_server_operations(tenant_id,mcp_server_operation_id),
  FOREIGN KEY(tenant_id,mcp_server_id,candidate_credential_revision)
    REFERENCES mcp_server_credentials(tenant_id,mcp_server_id,credential_revision),
  UNIQUE(tenant_id,mcp_server_sync_job_id),
  UNIQUE(tenant_id,mcp_server_operation_id),
  CHECK(deadline_at IS NULL OR unixepoch(deadline_at)>unixepoch(created_at)),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((status='waiting_policy' AND deadline_at IS NULL)
    OR (status!='waiting_policy' AND deadline_at IS NOT NULL)),
  CHECK((job_type='credential_probe' AND candidate_credential_revision IS NOT NULL)
    OR (job_type='discover')),
  CHECK((status='waiting_policy' AND attempt_count=0 AND lease_generation=0
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NULL AND deadline_at IS NULL AND started_at IS NULL
      AND completed_at IS NULL AND error_code IS NULL)
    OR (status='pending' AND attempt_count BETWEEN 0 AND 9
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NOT NULL AND completed_at IS NULL AND error_code IS NULL)
    OR (status='processing' AND attempt_count BETWEEN 1 AND 10
      AND lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 1 AND 160
      AND lease_token IS NOT NULL AND length(lease_token)=64
      AND lease_token NOT GLOB '*[^0-9a-f]*' AND lease_expires_at IS NOT NULL
      AND next_attempt_at IS NULL AND started_at IS NOT NULL AND completed_at IS NULL
      AND error_code IS NULL)
    OR (status='succeeded' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NULL AND completed_at IS NOT NULL
      AND error_code IS NULL)
    OR (status='failed' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NULL AND completed_at IS NOT NULL
      AND error_code IS NOT NULL))
);
CREATE TABLE mcp_invocations_prd083_new(
  mcp_invocation_id TEXT PRIMARY KEY CHECK(length(mcp_invocation_id)=37
    AND substr(mcp_invocation_id,1,5)='mcpi_'
    AND substr(mcp_invocation_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  mcp_tool_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  idempotency_key_hmac_version INTEGER NOT NULL CHECK(
    typeof(idempotency_key_hmac_version)='integer' AND idempotency_key_hmac_version>=1),
  intent_hmac TEXT NOT NULL CHECK(length(intent_hmac)=64
    AND intent_hmac NOT GLOB '*[^0-9a-f]*'),
  intent_hmac_version INTEGER NOT NULL CHECK(
    typeof(intent_hmac_version)='integer' AND intent_hmac_version>=1),
  server_state_revision INTEGER NOT NULL CHECK(
    typeof(server_state_revision)='integer' AND server_state_revision>=1),
  credential_revision INTEGER NOT NULL CHECK(
    typeof(credential_revision)='integer' AND credential_revision>=1),
  schema_revision INTEGER NOT NULL CHECK(typeof(schema_revision)='integer' AND schema_revision>=1),
  policy_revision INTEGER NOT NULL CHECK(typeof(policy_revision)='integer' AND policy_revision>=1),
  policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint)=64
    AND policy_fingerprint NOT GLOB '*[^0-9a-f]*'),
  input_schema_original_sha256 TEXT NOT NULL CHECK(length(input_schema_original_sha256)=64
    AND input_schema_original_sha256 NOT GLOB '*[^0-9a-f]*'),
  input_schema_effective_sha256 TEXT NOT NULL CHECK(length(input_schema_effective_sha256)=64
    AND input_schema_effective_sha256 NOT GLOB '*[^0-9a-f]*'),
  output_schema_original_sha256 TEXT CHECK(output_schema_original_sha256 IS NULL OR(
    length(output_schema_original_sha256)=64
    AND output_schema_original_sha256 NOT GLOB '*[^0-9a-f]*')),
  output_schema_effective_sha256 TEXT CHECK(output_schema_effective_sha256 IS NULL OR(
    length(output_schema_effective_sha256)=64
    AND output_schema_effective_sha256 NOT GLOB '*[^0-9a-f]*')),
  risk_level TEXT NOT NULL CHECK(risk_level IN('standard','high')),
  approval_required INTEGER NOT NULL CHECK(approval_required IN(0,1)),
  required_permissions_sha256 TEXT NOT NULL CHECK(length(required_permissions_sha256)=64
    AND required_permissions_sha256 NOT GLOB '*[^0-9a-f]*'),
  grant_sha256 TEXT NOT NULL CHECK(length(grant_sha256)=64
    AND grant_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_redaction_pointers_json TEXT NOT NULL CHECK(json_valid(result_redaction_pointers_json)
    AND json_type(result_redaction_pointers_json)='array'
    AND json_array_length(result_redaction_pointers_json) BETWEEN 0 AND 32
    AND length(CAST(result_redaction_pointers_json AS BLOB))<=8192),
  arguments_ciphertext TEXT NOT NULL CHECK(
    length(CAST(arguments_ciphertext AS BLOB)) BETWEEN 1 AND 1048576),
  arguments_encryption_key_version INTEGER NOT NULL CHECK(
    typeof(arguments_encryption_key_version)='integer'
    AND arguments_encryption_key_version>=1),
  arguments_bytes INTEGER NOT NULL CHECK(typeof(arguments_bytes)='integer'
    AND arguments_bytes BETWEEN 2 AND 1048576),
  arguments_hmac TEXT NOT NULL CHECK(length(arguments_hmac)=64
    AND arguments_hmac NOT GLOB '*[^0-9a-f]*'),
  arguments_hmac_key_version INTEGER NOT NULL CHECK(
    typeof(arguments_hmac_key_version)='integer' AND arguments_hmac_key_version>=1),
  approval_task_id TEXT,
  approval_deadline_at TEXT CHECK(approval_deadline_at IS NULL
    OR unixepoch(approval_deadline_at) IS NOT NULL),
  status TEXT NOT NULL CHECK(status IN(
    'approval_pending','queued','probing','calling','succeeded','failed',
    'blocked','rejected','outcome_unknown')),
  approval_gate_status TEXT GENERATED ALWAYS AS (
    CASE
      WHEN approval_task_id IS NULL THEN NULL
      WHEN status='approval_pending' THEN 'pending'
      WHEN status='rejected' THEN 'rejected'
      ELSE 'approved'
    END
  ) STORED,
  approval_gate_target_version TEXT GENERATED ALWAYS AS (
    CASE WHEN approval_task_id IS NULL THEN NULL ELSE
      CAST(server_state_revision AS TEXT)||':'||CAST(credential_revision AS TEXT)||':'
      ||CAST(schema_revision AS TEXT)||':'||CAST(policy_revision AS TEXT)
    END
  ) STORED,
  state_revision INTEGER NOT NULL CHECK(typeof(state_revision)='integer' AND state_revision>=1),
  input_dlp_status TEXT NOT NULL CHECK(input_dlp_status IN(
    'clean','log_only','blocked','unavailable')),
  input_dlp_match_count INTEGER NOT NULL CHECK(typeof(input_dlp_match_count)='integer'
    AND input_dlp_match_count BETWEEN 0 AND 1000),
  input_dlp_rule_id TEXT,
  input_dlp_ruleset_hmac TEXT NOT NULL CHECK(length(input_dlp_ruleset_hmac)=64
    AND input_dlp_ruleset_hmac NOT GLOB '*[^0-9a-f]*'),
  input_dlp_ruleset_hmac_key_version INTEGER NOT NULL CHECK(
    typeof(input_dlp_ruleset_hmac_key_version)='integer'
    AND input_dlp_ruleset_hmac_key_version>=1),
  input_dlp_scanned_at TEXT NOT NULL CHECK(unixepoch(input_dlp_scanned_at) IS NOT NULL),
  output_dlp_status TEXT CHECK(output_dlp_status IS NULL OR
    output_dlp_status IN('clean','log_only','blocked','unavailable')),
  output_dlp_match_count INTEGER CHECK(output_dlp_match_count IS NULL OR(
    typeof(output_dlp_match_count)='integer' AND output_dlp_match_count BETWEEN 0 AND 1000)),
  output_dlp_rule_id TEXT,
  output_dlp_ruleset_hmac TEXT CHECK(output_dlp_ruleset_hmac IS NULL OR(
    length(output_dlp_ruleset_hmac)=64
    AND output_dlp_ruleset_hmac NOT GLOB '*[^0-9a-f]*')),
  output_dlp_ruleset_hmac_key_version INTEGER CHECK(
    output_dlp_ruleset_hmac_key_version IS NULL OR(
      typeof(output_dlp_ruleset_hmac_key_version)='integer'
      AND output_dlp_ruleset_hmac_key_version>=1)),
  output_dlp_scanned_at TEXT CHECK(output_dlp_scanned_at IS NULL
    OR unixepoch(output_dlp_scanned_at) IS NOT NULL),
  result_ciphertext TEXT CHECK(result_ciphertext IS NULL OR
    length(CAST(result_ciphertext AS BLOB)) BETWEEN 1 AND 1048576),
  result_encryption_key_version INTEGER CHECK(result_encryption_key_version IS NULL OR(
    typeof(result_encryption_key_version)='integer' AND result_encryption_key_version>=1)),
  result_hmac TEXT CHECK(result_hmac IS NULL OR(
    length(result_hmac)=64 AND result_hmac NOT GLOB '*[^0-9a-f]*')),
  result_hmac_key_version INTEGER CHECK(result_hmac_key_version IS NULL OR(
    typeof(result_hmac_key_version)='integer' AND result_hmac_key_version>=1)),
  result_bytes INTEGER CHECK(result_bytes IS NULL OR(
    typeof(result_bytes)='integer' AND result_bytes BETWEEN 0 AND 1048576)),
  result_redaction_count INTEGER CHECK(result_redaction_count IS NULL OR(
    typeof(result_redaction_count)='integer' AND result_redaction_count BETWEEN 0 AND 1000)),
  probe_attempt_count INTEGER NOT NULL CHECK(typeof(probe_attempt_count)='integer'
    AND probe_attempt_count BETWEEN 0 AND 10),
  call_attempt_count INTEGER NOT NULL CHECK(typeof(call_attempt_count)='integer'
    AND call_attempt_count BETWEEN 0 AND 1),
  request_bytes INTEGER CHECK(request_bytes IS NULL OR(
    typeof(request_bytes)='integer' AND request_bytes BETWEEN 0 AND 1048576)),
  response_bytes INTEGER CHECK(response_bytes IS NULL OR(
    typeof(response_bytes)='integer' AND response_bytes BETWEEN 0 AND 1048576)),
  duration_ms INTEGER CHECK(duration_ms IS NULL OR(
    typeof(duration_ms)='integer' AND duration_ms BETWEEN 0 AND 300000)),
  execution_status TEXT NOT NULL CHECK(execution_status IN('not_called','called','unknown')),
  invocation_count INTEGER NOT NULL CHECK(invocation_count=1),
  tool_call_count INTEGER CHECK(tool_call_count IS NULL OR tool_call_count IN(0,1)),
  cost_status TEXT NOT NULL CHECK(cost_status IN('not_applicable','not_reported','unknown')),
  cost_micros INTEGER CHECK(cost_micros IS NULL),
  error_code TEXT CHECK(error_code IS NULL OR error_code IN(
    'mcp.executor_disabled','mcp.arguments_blocked','mcp.output_dlp_unavailable',
    'mcp.approval_rejected','mcp.approval_timeout','mcp.authorization_revoked',
    'mcp.server_unavailable','mcp.tool_failed','mcp.result_blocked',
    'mcp.result_invalid','mcp.outcome_unknown','mcp.state_conflict','mcp.unavailable',
    'mcp.network_blocked','mcp.sandbox_unavailable')),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer'
    AND lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  queued_at TEXT CHECK(queued_at IS NULL OR unixepoch(queued_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  call_started_at TEXT CHECK(call_started_at IS NULL OR unixepoch(call_started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,requested_by) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(tenant_id,mcp_server_id) REFERENCES mcp_servers(tenant_id,mcp_server_id),
  FOREIGN KEY(tenant_id,mcp_server_id,mcp_tool_id)
    REFERENCES mcp_tools(tenant_id,mcp_server_id,mcp_tool_id),
  FOREIGN KEY(tenant_id,mcp_server_id,credential_revision)
    REFERENCES mcp_server_credentials(tenant_id,mcp_server_id,credential_revision),
  FOREIGN KEY(tenant_id,input_dlp_rule_id) REFERENCES dlp_rules(tenant_id,dlp_rule_id),
  FOREIGN KEY(tenant_id,output_dlp_rule_id) REFERENCES dlp_rules(tenant_id,dlp_rule_id),
  FOREIGN KEY(
    tenant_id,approval_task_id,mcp_invocation_id,approval_gate_status,requested_by,
    approval_gate_target_version,policy_fingerprint,approval_deadline_at
  ) REFERENCES approval_tasks(
    tenant_id,approval_task_id,mcp_invocation_id,status,created_by_id,
    target_version,target_fingerprint,expires_at
  ) DEFERRABLE INITIALLY DEFERRED,
  UNIQUE(tenant_id,mcp_invocation_id),
  UNIQUE(tenant_id,mcp_invocation_id,mcp_server_id,mcp_tool_id),
  UNIQUE(tenant_id,requested_by,idempotency_key_hmac_version,idempotency_key_hmac),
  CHECK((output_schema_original_sha256 IS NULL AND output_schema_effective_sha256 IS NULL)
    OR (output_schema_original_sha256 IS NOT NULL AND output_schema_effective_sha256 IS NOT NULL)),
  CHECK((approval_required=1 AND (
      (approval_task_id IS NOT NULL AND approval_deadline_at IS NOT NULL)
      OR (approval_task_id IS NULL AND approval_deadline_at IS NULL
        AND status IN('blocked','failed') AND execution_status='not_called')))
    OR (approval_required=0 AND approval_task_id IS NULL AND approval_deadline_at IS NULL)),
  CHECK(risk_level!='high' OR approval_required=1),
  CHECK((input_dlp_status='clean' AND input_dlp_match_count=0)
    OR (input_dlp_status='log_only' AND input_dlp_match_count>=1)
    OR (input_dlp_status='blocked' AND input_dlp_match_count>=1)
    OR (input_dlp_status='unavailable' AND input_dlp_match_count=0)),
  CHECK((output_dlp_status IS NULL AND output_dlp_match_count IS NULL
      AND output_dlp_rule_id IS NULL AND output_dlp_ruleset_hmac IS NULL
      AND output_dlp_ruleset_hmac_key_version IS NULL AND output_dlp_scanned_at IS NULL)
    OR (output_dlp_status IS NOT NULL AND output_dlp_match_count IS NOT NULL
      AND output_dlp_ruleset_hmac IS NOT NULL
      AND output_dlp_ruleset_hmac_key_version IS NOT NULL AND output_dlp_scanned_at IS NOT NULL)),
  CHECK((result_ciphertext IS NULL AND result_encryption_key_version IS NULL
      AND result_hmac IS NULL AND result_hmac_key_version IS NULL
      AND result_bytes IS NULL)
    OR (result_ciphertext IS NOT NULL AND result_encryption_key_version IS NOT NULL
      AND result_hmac IS NOT NULL AND result_hmac_key_version IS NOT NULL
      AND result_bytes IS NOT NULL AND result_redaction_count IS NOT NULL)),
  CHECK(result_redaction_count IS NULL
    OR (status='succeeded' AND result_ciphertext IS NOT NULL)
    OR (status='blocked' AND execution_status='called'
      AND error_code='mcp.result_blocked' AND output_dlp_status='blocked'
      AND result_ciphertext IS NULL)
    OR (status='failed' AND execution_status='called'
      AND error_code IN('mcp.tool_failed','mcp.output_dlp_unavailable')
      AND result_ciphertext IS NULL)),
  CHECK(NOT (
      (status='blocked' AND execution_status='called'
        AND error_code='mcp.result_blocked')
      OR (status='failed' AND execution_status='called'
        AND error_code IN('mcp.tool_failed','mcp.output_dlp_unavailable')))
    OR result_redaction_count IS NOT NULL),
  CHECK(status!='failed' OR error_code!='mcp.tool_failed'
    OR output_dlp_status IN('clean','log_only')),
  CHECK(status!='failed' OR error_code!='mcp.output_dlp_unavailable'
    OR output_dlp_status='unavailable'),
  CHECK((execution_status='not_called' AND tool_call_count=0
      AND cost_status='not_applicable' AND call_attempt_count=0 AND call_started_at IS NULL)
    OR (execution_status='called' AND tool_call_count=1
      AND cost_status='not_reported' AND call_attempt_count=1 AND call_started_at IS NOT NULL)
    OR (execution_status='unknown' AND tool_call_count IS NULL
      AND cost_status='unknown' AND call_attempt_count=1 AND call_started_at IS NOT NULL)),
  CHECK((status IN('approval_pending','queued') AND lease_owner_id IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL AND completed_at IS NULL)
    OR (status IN('probing','calling') AND lease_owner_id IS NOT NULL
      AND length(lease_owner_id) BETWEEN 1 AND 160 AND lease_token IS NOT NULL
      AND length(lease_token)=64 AND lease_token NOT GLOB '*[^0-9a-f]*'
      AND lease_expires_at IS NOT NULL AND completed_at IS NULL)
    OR (status IN('succeeded','failed','blocked','rejected','outcome_unknown')
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NULL AND completed_at IS NOT NULL)),
  CHECK(status!='approval_pending' OR (approval_required=1 AND queued_at IS NULL)),
  CHECK(status!='succeeded' OR (execution_status='called' AND error_code IS NULL
    AND result_ciphertext IS NOT NULL AND output_dlp_status IN('clean','log_only'))),
  CHECK(status!='blocked' OR (
    (execution_status='not_called' AND error_code='mcp.arguments_blocked'
      AND input_dlp_status='blocked')
    OR (execution_status='not_called' AND error_code='mcp.network_blocked'
      AND input_dlp_status IN('clean','log_only'))
    OR (execution_status='called' AND error_code='mcp.result_blocked'
      AND output_dlp_status='blocked' AND result_ciphertext IS NULL))),
  CHECK(status!='rejected' OR (execution_status='not_called'
    AND error_code IN('mcp.approval_rejected','mcp.approval_timeout'))),
  CHECK(status!='outcome_unknown' OR (execution_status='unknown'
    AND error_code='mcp.outcome_unknown' AND result_ciphertext IS NULL)),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at))
);
CREATE TABLE mcp_invocation_attempts_prd083_new(
  mcp_invocation_attempt_id TEXT PRIMARY KEY CHECK(length(mcp_invocation_attempt_id)=37
    AND substr(mcp_invocation_attempt_id,1,5)='mcpa_'
    AND substr(mcp_invocation_attempt_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_invocation_id TEXT NOT NULL,
  attempt_kind TEXT NOT NULL CHECK(attempt_kind IN('probe','call')),
  attempt_number INTEGER NOT NULL CHECK(typeof(attempt_number)='integer'
    AND attempt_number BETWEEN 1 AND 10),
  status TEXT NOT NULL CHECK(status IN('processing','succeeded','failed','outcome_unknown')),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer'
    AND lease_generation>=1),
  error_code TEXT CHECK(error_code IS NULL OR error_code IN(
    'mcp.executor_disabled','mcp.arguments_blocked','mcp.authorization_revoked',
    'mcp.server_unavailable',
    'mcp.tool_failed','mcp.result_blocked','mcp.output_dlp_unavailable',
    'mcp.result_invalid','mcp.outcome_unknown','mcp.state_conflict','mcp.unavailable',
    'mcp.network_blocked','mcp.sandbox_unavailable')),
  request_bytes INTEGER CHECK(request_bytes IS NULL OR(
    typeof(request_bytes)='integer' AND request_bytes BETWEEN 0 AND 1048576)),
  response_bytes INTEGER CHECK(response_bytes IS NULL OR(
    typeof(response_bytes)='integer' AND response_bytes BETWEEN 0 AND 1048576)),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  call_started_at TEXT CHECK(call_started_at IS NULL OR unixepoch(call_started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  duration_ms INTEGER CHECK(duration_ms IS NULL OR(
    typeof(duration_ms)='integer' AND duration_ms BETWEEN 0 AND 300000)),
  FOREIGN KEY(tenant_id,mcp_invocation_id)
    REFERENCES mcp_invocations(tenant_id,mcp_invocation_id),
  UNIQUE(tenant_id,mcp_invocation_attempt_id),
  UNIQUE(tenant_id,mcp_invocation_id,attempt_kind,attempt_number),
  CHECK((status='processing' AND error_code IS NULL AND completed_at IS NULL
      AND duration_ms IS NULL)
    OR (status='succeeded' AND error_code IS NULL AND completed_at IS NOT NULL
      AND duration_ms IS NOT NULL)
    OR (status IN('failed','outcome_unknown') AND error_code IS NOT NULL
      AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)),
  CHECK((attempt_kind='probe' AND call_started_at IS NULL)
    OR (attempt_kind='call' AND call_started_at IS NOT NULL))
);

INSERT INTO mcp_server_operations_prd083_new(rowid,
  mcp_server_operation_id,tenant_id,mcp_server_id,operation_type,idempotency_key_hmac,
  idempotency_key_hmac_version,intent_hmac,intent_hmac_version,actor_user_id,
  start_server_revision,start_credential_revision,candidate_credential_revision,status,
  error_code,request_id,trace_id,created_at,completed_at,updated_at)
SELECT rowid,mcp_server_operation_id,tenant_id,mcp_server_id,operation_type,
  idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,intent_hmac_version,
  actor_user_id,start_server_revision,start_credential_revision,candidate_credential_revision,
  status,error_code,request_id,trace_id,created_at,completed_at,updated_at
FROM mcp_server_operations;
INSERT INTO mcp_server_sync_jobs_prd083_new(rowid,
  mcp_server_sync_job_id,tenant_id,mcp_server_id,mcp_server_operation_id,
  candidate_credential_revision,job_type,status,attempt_count,lease_generation,
  lease_owner_id,lease_token,lease_expires_at,next_attempt_at,deadline_at,error_code,
  created_at,started_at,completed_at,updated_at)
SELECT rowid,mcp_server_sync_job_id,tenant_id,mcp_server_id,mcp_server_operation_id,
  candidate_credential_revision,job_type,status,attempt_count,lease_generation,
  lease_owner_id,lease_token,lease_expires_at,next_attempt_at,deadline_at,error_code,
  created_at,started_at,completed_at,updated_at FROM mcp_server_sync_jobs;
INSERT INTO mcp_invocations_prd083_new(rowid,
  mcp_invocation_id,tenant_id,mcp_server_id,mcp_tool_id,requested_by,request_id,trace_id,
  idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,intent_hmac_version,
  server_state_revision,credential_revision,schema_revision,policy_revision,policy_fingerprint,
  input_schema_original_sha256,input_schema_effective_sha256,output_schema_original_sha256,
  output_schema_effective_sha256,risk_level,approval_required,required_permissions_sha256,
  grant_sha256,result_redaction_pointers_json,arguments_ciphertext,
  arguments_encryption_key_version,arguments_bytes,arguments_hmac,
  arguments_hmac_key_version,approval_task_id,approval_deadline_at,status,state_revision,
  input_dlp_status,input_dlp_match_count,input_dlp_rule_id,input_dlp_ruleset_hmac,
  input_dlp_ruleset_hmac_key_version,input_dlp_scanned_at,output_dlp_status,
  output_dlp_match_count,output_dlp_rule_id,output_dlp_ruleset_hmac,
  output_dlp_ruleset_hmac_key_version,output_dlp_scanned_at,result_ciphertext,
  result_encryption_key_version,result_hmac,result_hmac_key_version,result_bytes,
  result_redaction_count,probe_attempt_count,call_attempt_count,request_bytes,response_bytes,
  duration_ms,execution_status,invocation_count,tool_call_count,cost_status,cost_micros,
  error_code,lease_generation,lease_owner_id,lease_token,lease_expires_at,next_attempt_at,
  created_at,queued_at,started_at,call_started_at,completed_at,updated_at)
SELECT rowid,mcp_invocation_id,tenant_id,mcp_server_id,mcp_tool_id,requested_by,request_id,
  trace_id,idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,intent_hmac_version,
  server_state_revision,credential_revision,schema_revision,policy_revision,policy_fingerprint,
  input_schema_original_sha256,input_schema_effective_sha256,output_schema_original_sha256,
  output_schema_effective_sha256,risk_level,approval_required,required_permissions_sha256,
  grant_sha256,result_redaction_pointers_json,arguments_ciphertext,
  arguments_encryption_key_version,arguments_bytes,arguments_hmac,
  arguments_hmac_key_version,approval_task_id,approval_deadline_at,status,state_revision,
  input_dlp_status,input_dlp_match_count,input_dlp_rule_id,input_dlp_ruleset_hmac,
  input_dlp_ruleset_hmac_key_version,input_dlp_scanned_at,output_dlp_status,
  output_dlp_match_count,output_dlp_rule_id,output_dlp_ruleset_hmac,
  output_dlp_ruleset_hmac_key_version,output_dlp_scanned_at,result_ciphertext,
  result_encryption_key_version,result_hmac,result_hmac_key_version,result_bytes,
  result_redaction_count,probe_attempt_count,call_attempt_count,request_bytes,response_bytes,
  duration_ms,execution_status,invocation_count,tool_call_count,cost_status,cost_micros,
  error_code,lease_generation,lease_owner_id,lease_token,lease_expires_at,next_attempt_at,
  created_at,queued_at,started_at,call_started_at,completed_at,updated_at
FROM mcp_invocations;
INSERT INTO mcp_invocation_attempts_prd083_new(rowid,
  mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,attempt_number,status,
  lease_generation,error_code,request_bytes,response_bytes,started_at,call_started_at,
  completed_at,duration_ms)
SELECT rowid,mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
  attempt_number,status,lease_generation,error_code,request_bytes,response_bytes,started_at,
  call_started_at,completed_at,duration_ms FROM mcp_invocation_attempts;

CREATE TEMP TABLE prd083_state_copy_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd083_state_copy_guard(issue_count)
SELECT ABS((SELECT COUNT(*) FROM mcp_server_operations)
      -(SELECT COUNT(*) FROM mcp_server_operations_prd083_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM mcp_server_operations
      EXCEPT SELECT rowid,* FROM mcp_server_operations_prd083_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM mcp_server_operations_prd083_new
      EXCEPT SELECT rowid,* FROM mcp_server_operations))
  + ABS((SELECT COUNT(*) FROM mcp_server_sync_jobs)
      -(SELECT COUNT(*) FROM mcp_server_sync_jobs_prd083_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM mcp_server_sync_jobs
      EXCEPT SELECT rowid,* FROM mcp_server_sync_jobs_prd083_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM mcp_server_sync_jobs_prd083_new
      EXCEPT SELECT rowid,* FROM mcp_server_sync_jobs))
  + ABS((SELECT COUNT(*) FROM mcp_invocations)
      -(SELECT COUNT(*) FROM mcp_invocations_prd083_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM mcp_invocations
      EXCEPT SELECT rowid,* FROM mcp_invocations_prd083_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM mcp_invocations_prd083_new
      EXCEPT SELECT rowid,* FROM mcp_invocations))
  + ABS((SELECT COUNT(*) FROM mcp_invocation_attempts)
      -(SELECT COUNT(*) FROM mcp_invocation_attempts_prd083_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM mcp_invocation_attempts
      EXCEPT SELECT rowid,* FROM mcp_invocation_attempts_prd083_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM mcp_invocation_attempts_prd083_new
      EXCEPT SELECT rowid,* FROM mcp_invocation_attempts));
DROP TABLE prd083_state_copy_guard;

DROP TABLE mcp_invocation_attempts;
DROP TABLE mcp_invocations;
DROP TABLE mcp_server_sync_jobs;
DROP TABLE mcp_server_operations;
ALTER TABLE mcp_server_operations_prd083_new RENAME TO mcp_server_operations;
ALTER TABLE mcp_server_sync_jobs_prd083_new RENAME TO mcp_server_sync_jobs;
ALTER TABLE mcp_invocations_prd083_new RENAME TO mcp_invocations;
ALTER TABLE mcp_invocation_attempts_prd083_new RENAME TO mcp_invocation_attempts;

CREATE INDEX idx_mcp_operations_tenant_time_prd082
  ON mcp_server_operations(tenant_id,created_at DESC,mcp_server_operation_id DESC);
CREATE INDEX idx_mcp_sync_jobs_due_prd082
  ON mcp_server_sync_jobs(status,next_attempt_at,lease_expires_at,tenant_id,mcp_server_sync_job_id)
  WHERE status IN('pending','processing');
CREATE UNIQUE INDEX idx_mcp_sync_jobs_active_server_prd083
  ON mcp_server_sync_jobs(tenant_id,mcp_server_id)
  WHERE status IN('waiting_policy','pending','processing');
CREATE INDEX idx_mcp_invocations_tenant_time_prd082
  ON mcp_invocations(tenant_id,created_at DESC,mcp_invocation_id DESC);
CREATE INDEX idx_mcp_invocations_actor_time_prd082
  ON mcp_invocations(tenant_id,requested_by,created_at DESC,mcp_invocation_id DESC);
CREATE INDEX idx_mcp_invocations_due_prd082
  ON mcp_invocations(status,next_attempt_at,lease_expires_at,tenant_id,mcp_invocation_id)
  WHERE status IN('queued','probing','calling');
CREATE INDEX idx_mcp_attempts_invocation_prd082
  ON mcp_invocation_attempts(tenant_id,mcp_invocation_id,attempt_kind,attempt_number);

CREATE TRIGGER trg_mcp_operations_owner_insert_prd082
BEFORE INSERT ON mcp_server_operations FOR EACH ROW
WHEN NEW.status!='queued' OR NEW.completed_at IS NOT NULL OR NEW.error_code IS NOT NULL
  OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM mcp_servers server JOIN users actor
    ON actor.tenant_id=server.tenant_id
    WHERE server.tenant_id=NEW.tenant_id AND server.mcp_server_id=NEW.mcp_server_id
      AND server.state_revision=NEW.start_server_revision
      AND actor.user_id=NEW.actor_user_id AND actor.status='active')
  OR (NEW.start_credential_revision IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM mcp_server_credentials credential
    WHERE credential.tenant_id=NEW.tenant_id
      AND credential.mcp_server_id=NEW.mcp_server_id
      AND credential.credential_revision=NEW.start_credential_revision))
  OR EXISTS(SELECT 1 FROM mcp_server_operations current
    WHERE current.mcp_server_operation_id=NEW.mcp_server_operation_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_operation_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_operations_transition_prd082
BEFORE UPDATE ON mcp_server_operations FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.mcp_server_operation_id!=OLD.mcp_server_operation_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.mcp_server_id!=OLD.mcp_server_id
  OR NEW.operation_type!=OLD.operation_type
  OR NEW.idempotency_key_hmac!=OLD.idempotency_key_hmac
  OR NEW.idempotency_key_hmac_version!=OLD.idempotency_key_hmac_version
  OR NEW.intent_hmac!=OLD.intent_hmac OR NEW.intent_hmac_version!=OLD.intent_hmac_version
  OR NEW.actor_user_id!=OLD.actor_user_id
  OR NEW.start_server_revision!=OLD.start_server_revision
  OR NEW.start_credential_revision IS NOT OLD.start_credential_revision
  OR NEW.candidate_credential_revision IS NOT OLD.candidate_credential_revision
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.created_at!=OLD.created_at OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT ((OLD.status='queued' AND NEW.status IN('processing','succeeded','failed'))
    OR (OLD.status='processing' AND NEW.status IN('succeeded','failed')))
BEGIN SELECT RAISE(ABORT,'mcp_operation_transition_invalid'); END;
CREATE TRIGGER trg_mcp_operations_delete_prd082
BEFORE DELETE ON mcp_server_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_operation_immutable'); END;
CREATE TRIGGER trg_mcp_sync_jobs_owner_insert_prd082
BEFORE INSERT ON mcp_server_sync_jobs FOR EACH ROW
WHEN NEW.attempt_count!=0 OR NEW.lease_generation!=0 OR NEW.created_at!=NEW.updated_at
  OR NEW.started_at IS NOT NULL OR NEW.completed_at IS NOT NULL OR NEW.error_code IS NOT NULL
  OR NOT (
    (NEW.status='waiting_policy' AND NEW.next_attempt_at IS NULL AND NEW.deadline_at IS NULL
      AND NEW.lease_owner_id IS NULL AND NEW.lease_token IS NULL
      AND NEW.lease_expires_at IS NULL)
    OR (NEW.status='pending' AND NEW.next_attempt_at=NEW.created_at
      AND NEW.deadline_at IS NOT NULL AND NEW.lease_owner_id IS NULL
      AND NEW.lease_token IS NULL AND NEW.lease_expires_at IS NULL))
  OR NOT EXISTS(SELECT 1 FROM mcp_server_operations operation
    WHERE operation.tenant_id=NEW.tenant_id
      AND operation.mcp_server_id=NEW.mcp_server_id
      AND operation.mcp_server_operation_id=NEW.mcp_server_operation_id
      AND operation.status='queued')
  OR EXISTS(SELECT 1 FROM mcp_server_sync_jobs current
    WHERE current.mcp_server_sync_job_id=NEW.mcp_server_sync_job_id
      OR (current.tenant_id=NEW.tenant_id AND current.mcp_server_id=NEW.mcp_server_id
        AND current.status IN('waiting_policy','pending','processing'))
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_sync_job_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_sync_jobs_transition_prd082
BEFORE UPDATE ON mcp_server_sync_jobs FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.mcp_server_sync_job_id!=OLD.mcp_server_sync_job_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.mcp_server_id!=OLD.mcp_server_id
  OR NEW.mcp_server_operation_id!=OLD.mcp_server_operation_id
  OR NEW.candidate_credential_revision IS NOT OLD.candidate_credential_revision
  OR NEW.job_type!=OLD.job_type OR NEW.created_at!=OLD.created_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT (
    (OLD.status='waiting_policy' AND NEW.status='pending'
      AND OLD.attempt_count=0 AND NEW.attempt_count=0
      AND OLD.lease_generation=0 AND NEW.lease_generation=0
      AND OLD.deadline_at IS NULL AND NEW.deadline_at IS NOT NULL
      AND unixepoch(NEW.deadline_at)>unixepoch(NEW.updated_at)
      AND NEW.next_attempt_at=NEW.updated_at AND NEW.started_at IS NULL
      AND NEW.completed_at IS NULL AND NEW.error_code IS NULL
      AND NEW.lease_owner_id IS NULL AND NEW.lease_token IS NULL
      AND NEW.lease_expires_at IS NULL)
    OR (OLD.status='pending' AND NEW.status='processing'
      AND NEW.deadline_at=OLD.deadline_at AND NEW.lease_generation=OLD.lease_generation+1
      AND NEW.attempt_count=OLD.attempt_count+1
      AND NEW.started_at=NEW.updated_at
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.next_attempt_at)
      AND unixepoch(NEW.lease_expires_at)-unixepoch(NEW.updated_at) BETWEEN 30 AND 3600)
    OR (OLD.status='processing' AND NEW.status='processing'
      AND NEW.deadline_at=OLD.deadline_at AND NEW.lease_generation=OLD.lease_generation+1
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count+1 AND NEW.lease_token!=OLD.lease_token
      AND NEW.started_at IS OLD.started_at
      AND unixepoch(NEW.lease_expires_at)-unixepoch(NEW.updated_at) BETWEEN 30 AND 3600)
    OR (OLD.status='processing' AND NEW.status='pending'
      AND NEW.deadline_at=OLD.deadline_at AND NEW.lease_generation=OLD.lease_generation+1
      AND unixepoch(NEW.updated_at)<unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count AND NEW.attempt_count<10
      AND NEW.started_at IS OLD.started_at
      AND unixepoch(NEW.next_attempt_at)>=unixepoch(NEW.updated_at))
    OR (OLD.status='processing' AND NEW.status IN('succeeded','failed')
      AND NEW.deadline_at=OLD.deadline_at AND NEW.lease_generation=OLD.lease_generation+1
      AND unixepoch(NEW.updated_at)<unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count AND NEW.started_at IS OLD.started_at))
BEGIN SELECT RAISE(ABORT,'mcp_sync_job_transition_invalid'); END;
CREATE TRIGGER trg_mcp_sync_jobs_delete_prd082
BEFORE DELETE ON mcp_server_sync_jobs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_sync_job_immutable'); END;
CREATE TRIGGER trg_mcp_invocations_owner_insert_prd082
BEFORE INSERT ON mcp_invocations FOR EACH ROW
WHEN NEW.state_revision!=1 OR NEW.created_at!=NEW.updated_at
  OR NEW.probe_attempt_count!=0 OR NEW.call_attempt_count!=0
  OR NEW.status NOT IN('approval_pending','queued','blocked','failed')
  OR (NEW.status='approval_pending' AND (NEW.queued_at IS NOT NULL
      OR NEW.next_attempt_at IS NOT NULL OR NEW.completed_at IS NOT NULL))
  OR (NEW.status='queued' AND (NEW.queued_at!=NEW.created_at
      OR NEW.next_attempt_at!=NEW.created_at OR NEW.completed_at IS NOT NULL))
  OR (NEW.status IN('blocked','failed') AND NEW.completed_at!=NEW.created_at)
  OR NOT EXISTS(SELECT 1 FROM mcp_tools tool
    JOIN mcp_servers server ON server.tenant_id=tool.tenant_id
      AND server.mcp_server_id=tool.mcp_server_id
    JOIN mcp_server_credentials credential ON credential.tenant_id=server.tenant_id
      AND credential.mcp_server_id=server.mcp_server_id
      AND credential.credential_revision=server.active_credential_revision
    JOIN users actor ON actor.tenant_id=tool.tenant_id
    WHERE tool.tenant_id=NEW.tenant_id AND tool.mcp_server_id=NEW.mcp_server_id
      AND tool.mcp_tool_id=NEW.mcp_tool_id AND tool.status='active'
      AND server.status='active' AND server.state_revision=NEW.server_state_revision
      AND credential.credential_revision=NEW.credential_revision
      AND credential.status='active' AND actor.user_id=NEW.requested_by
      AND actor.status='active' AND tool.schema_revision=NEW.schema_revision
      AND tool.policy_revision=NEW.policy_revision
      AND tool.input_schema_original_sha256=NEW.input_schema_original_sha256
      AND tool.input_schema_effective_sha256=NEW.input_schema_effective_sha256
      AND tool.output_schema_original_sha256 IS NEW.output_schema_original_sha256
      AND tool.output_schema_effective_sha256 IS NEW.output_schema_effective_sha256
      AND tool.risk_level=NEW.risk_level
      AND tool.approval_required=NEW.approval_required
      AND tool.result_redaction_pointers_json=NEW.result_redaction_pointers_json)
  OR (NEW.approval_task_id IS NOT NULL AND EXISTS(
    SELECT 1 FROM approval_tasks task
    WHERE task.tenant_id=NEW.tenant_id AND task.approval_task_id=NEW.approval_task_id
      AND NOT (task.source_type='mcp' AND task.source_id=NEW.mcp_invocation_id
        AND task.target_type='mcp_tool_invocation'
        AND task.target_id=NEW.mcp_invocation_id AND task.target_action='invoke'
        AND task.target_reason_code IS NULL
        AND task.target_version=CAST(NEW.server_state_revision AS TEXT)||':'
          ||CAST(NEW.credential_revision AS TEXT)||':'
          ||CAST(NEW.schema_revision AS TEXT)||':'
          ||CAST(NEW.policy_revision AS TEXT)
        AND task.target_fingerprint=NEW.policy_fingerprint
        AND task.risk_level='high' AND task.status='pending'
        AND task.required_permission_code='mcp.approve'
        AND task.created_by_type='user' AND task.created_by_id=NEW.requested_by
        AND task.mcp_invocation_id=NEW.mcp_invocation_id
        AND task.expires_at=NEW.approval_deadline_at)))
  OR EXISTS(SELECT 1 FROM mcp_invocations current
    WHERE current.mcp_invocation_id=NEW.mcp_invocation_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_invocation_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_invocations_transition_prd082
BEFORE UPDATE ON mcp_invocations FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.mcp_invocation_id!=OLD.mcp_invocation_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.mcp_server_id!=OLD.mcp_server_id
  OR NEW.mcp_tool_id!=OLD.mcp_tool_id OR NEW.requested_by!=OLD.requested_by
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.idempotency_key_hmac!=OLD.idempotency_key_hmac
  OR NEW.idempotency_key_hmac_version!=OLD.idempotency_key_hmac_version
  OR NEW.intent_hmac!=OLD.intent_hmac OR NEW.intent_hmac_version!=OLD.intent_hmac_version
  OR NEW.server_state_revision!=OLD.server_state_revision
  OR NEW.credential_revision!=OLD.credential_revision
  OR NEW.schema_revision!=OLD.schema_revision OR NEW.policy_revision!=OLD.policy_revision
  OR NEW.policy_fingerprint!=OLD.policy_fingerprint
  OR NEW.input_schema_original_sha256!=OLD.input_schema_original_sha256
  OR NEW.input_schema_effective_sha256!=OLD.input_schema_effective_sha256
  OR NEW.output_schema_original_sha256 IS NOT OLD.output_schema_original_sha256
  OR NEW.output_schema_effective_sha256 IS NOT OLD.output_schema_effective_sha256
  OR NEW.risk_level!=OLD.risk_level OR NEW.approval_required!=OLD.approval_required
  OR NEW.required_permissions_sha256!=OLD.required_permissions_sha256
  OR NEW.grant_sha256!=OLD.grant_sha256
  OR NEW.result_redaction_pointers_json!=OLD.result_redaction_pointers_json
  OR NEW.arguments_ciphertext!=OLD.arguments_ciphertext
  OR NEW.arguments_encryption_key_version!=OLD.arguments_encryption_key_version
  OR NEW.arguments_bytes!=OLD.arguments_bytes OR NEW.arguments_hmac!=OLD.arguments_hmac
  OR NEW.arguments_hmac_key_version!=OLD.arguments_hmac_key_version
  OR NEW.approval_task_id IS NOT OLD.approval_task_id
  OR NEW.approval_deadline_at IS NOT OLD.approval_deadline_at
  OR (NEW.request_bytes IS NOT OLD.request_bytes
    AND NOT (OLD.status='calling' AND NEW.status IN('succeeded','failed','blocked')))
  OR NEW.invocation_count!=OLD.invocation_count OR NEW.cost_micros IS NOT OLD.cost_micros
  OR NEW.created_at!=OLD.created_at OR NEW.state_revision!=OLD.state_revision+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NEW.probe_attempt_count<OLD.probe_attempt_count
  OR NEW.probe_attempt_count>OLD.probe_attempt_count+1
  OR NEW.call_attempt_count<OLD.call_attempt_count
  OR NEW.call_attempt_count>OLD.call_attempt_count+1
  OR ((OLD.status IN('queued','probing') AND NEW.status='probing')
    AND (NEW.probe_attempt_count!=OLD.probe_attempt_count+1
      OR NEW.lease_generation!=OLD.lease_generation+1
      OR unixepoch(NEW.lease_expires_at)-unixepoch(NEW.updated_at) NOT BETWEEN 30 AND 3600
      OR (OLD.status='queued'
        AND unixepoch(NEW.updated_at)<unixepoch(OLD.next_attempt_at))
      OR (OLD.status='probing' AND (unixepoch(NEW.updated_at)<unixepoch(OLD.lease_expires_at)
        OR NEW.lease_token=OLD.lease_token))))
  OR (NOT (OLD.status IN('queued','probing') AND NEW.status='probing')
    AND NEW.probe_attempt_count!=OLD.probe_attempt_count)
  OR NOT (
    (OLD.status='approval_pending' AND NEW.status IN('queued','rejected')
      AND NEW.lease_generation=OLD.lease_generation)
    OR (OLD.status='queued' AND NEW.status IN('blocked','failed')
      AND NEW.lease_generation=OLD.lease_generation)
    OR (OLD.status='queued' AND NEW.status='probing'
      AND NEW.lease_generation=OLD.lease_generation+1)
    OR (OLD.status='probing' AND NEW.status IN(
        'probing','queued','calling','blocked','failed')
      AND NEW.lease_generation=OLD.lease_generation+1)
    OR (OLD.status='calling' AND NEW.status IN(
        'succeeded','failed','blocked','outcome_unknown')
      AND NEW.lease_generation=OLD.lease_generation+1))
  OR ((OLD.status='probing' AND NEW.status='calling')
    AND (NEW.call_attempt_count!=OLD.call_attempt_count+1
      OR unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)
      OR NEW.lease_owner_id!=OLD.lease_owner_id OR NEW.lease_token=OLD.lease_token
      OR unixepoch(NEW.lease_expires_at)-unixepoch(NEW.updated_at) NOT BETWEEN 30 AND 3600
      OR NEW.execution_status!='unknown' OR NEW.tool_call_count IS NOT NULL
      OR NEW.cost_status!='unknown' OR NEW.call_started_at!=NEW.updated_at
      OR NEW.response_bytes IS NOT OLD.response_bytes
      OR NEW.duration_ms IS NOT OLD.duration_ms OR NEW.error_code IS NOT OLD.error_code))
  OR (NOT (OLD.status='probing' AND NEW.status='calling')
    AND NEW.call_attempt_count!=OLD.call_attempt_count)
  OR ((NEW.input_dlp_status!=OLD.input_dlp_status
      OR NEW.input_dlp_match_count!=OLD.input_dlp_match_count
      OR NEW.input_dlp_rule_id IS NOT OLD.input_dlp_rule_id
      OR NEW.input_dlp_ruleset_hmac!=OLD.input_dlp_ruleset_hmac
      OR NEW.input_dlp_ruleset_hmac_key_version!=OLD.input_dlp_ruleset_hmac_key_version
      OR NEW.input_dlp_scanned_at!=OLD.input_dlp_scanned_at)
    AND NOT (OLD.status IN('queued','probing')
      AND NEW.status IN('probing','calling','blocked','failed')
      AND unixepoch(NEW.input_dlp_scanned_at)>=unixepoch(OLD.input_dlp_scanned_at)))
  OR ((NEW.output_dlp_status IS NOT OLD.output_dlp_status
      OR NEW.output_dlp_match_count IS NOT OLD.output_dlp_match_count
      OR NEW.output_dlp_rule_id IS NOT OLD.output_dlp_rule_id
      OR NEW.output_dlp_ruleset_hmac IS NOT OLD.output_dlp_ruleset_hmac
      OR NEW.output_dlp_ruleset_hmac_key_version IS NOT OLD.output_dlp_ruleset_hmac_key_version
      OR NEW.output_dlp_scanned_at IS NOT OLD.output_dlp_scanned_at
      OR NEW.result_ciphertext IS NOT OLD.result_ciphertext
      OR NEW.result_encryption_key_version IS NOT OLD.result_encryption_key_version
      OR NEW.result_hmac IS NOT OLD.result_hmac
      OR NEW.result_hmac_key_version IS NOT OLD.result_hmac_key_version
      OR NEW.result_bytes IS NOT OLD.result_bytes
      OR NEW.result_redaction_count IS NOT OLD.result_redaction_count)
    AND NOT (OLD.status='calling' AND NEW.status IN(
      'succeeded','failed','blocked','outcome_unknown')))
  OR (NEW.queued_at IS NOT OLD.queued_at
    AND NOT (OLD.status='approval_pending' AND NEW.status='queued'
      AND NEW.queued_at=NEW.updated_at))
  OR (NEW.started_at IS NOT OLD.started_at
    AND NOT (OLD.status='queued' AND NEW.status='probing'
      AND NEW.started_at=NEW.updated_at))
  OR (NEW.call_started_at IS NOT OLD.call_started_at
    AND NOT (OLD.status='probing' AND NEW.status='calling'
      AND NEW.call_started_at=NEW.updated_at))
  OR (NEW.completed_at IS NOT OLD.completed_at
    AND NOT (NEW.status IN('succeeded','failed','blocked','rejected','outcome_unknown')
      AND NEW.completed_at=NEW.updated_at))
  OR ((NEW.response_bytes IS NOT OLD.response_bytes OR NEW.duration_ms IS NOT OLD.duration_ms
      OR NEW.execution_status!=OLD.execution_status
      OR NEW.tool_call_count IS NOT OLD.tool_call_count OR NEW.cost_status!=OLD.cost_status
      OR NEW.error_code IS NOT OLD.error_code)
    AND NOT (NEW.status IN('succeeded','failed','blocked','rejected','outcome_unknown')
      OR (OLD.status='probing' AND NEW.status='calling')))
  OR (OLD.status!='calling' AND NEW.status IN('blocked','failed','rejected')
    AND (NEW.execution_status!='not_called' OR NEW.tool_call_count!=0
      OR NEW.cost_status!='not_applicable' OR NEW.call_attempt_count!=0
      OR NEW.call_started_at IS NOT NULL OR NEW.error_code IS NULL))
  OR (NEW.status='blocked' AND OLD.status!='calling'
    AND NOT ((NEW.error_code='mcp.arguments_blocked' AND NEW.input_dlp_status='blocked')
      OR (NEW.error_code='mcp.network_blocked'
        AND NEW.input_dlp_status IN('clean','log_only'))))
  OR (NEW.status='failed' AND OLD.status!='calling'
    AND NEW.error_code NOT IN('mcp.executor_disabled','mcp.authorization_revoked',
      'mcp.server_unavailable','mcp.state_conflict','mcp.unavailable',
      'mcp.sandbox_unavailable'))
  OR (NEW.status='probing' AND NEW.input_dlp_status NOT IN('clean','log_only'))
  OR (NEW.status='failed' AND NEW.input_dlp_status='unavailable'
    AND NEW.error_code!='mcp.unavailable')
  OR (OLD.status='approval_pending' AND NEW.status='queued'
    AND NEW.next_attempt_at!=NEW.updated_at)
  OR (OLD.status IN('queued','probing') AND NEW.status='probing'
    AND NEW.next_attempt_at IS NOT NULL)
  OR (OLD.status='probing' AND NEW.status='queued'
    AND (NEW.next_attempt_at IS NULL
      OR unixepoch(NEW.next_attempt_at)<unixepoch(NEW.updated_at)
      OR unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)))
  OR (OLD.status='probing' AND NEW.status='calling' AND NEW.next_attempt_at IS NOT NULL)
  OR (OLD.status='probing' AND NEW.status IN('blocked','failed')
    AND NOT (unixepoch(NEW.updated_at)<unixepoch(OLD.lease_expires_at)
      OR (NEW.status='failed' AND OLD.probe_attempt_count>=4
        AND NEW.error_code='mcp.server_unavailable'
        AND unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at))))
  OR (OLD.status='calling' AND NEW.status IN('succeeded','failed','blocked')
    AND (unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)
      OR NEW.execution_status!='called' OR NEW.tool_call_count!=1
      OR NEW.cost_status!='not_reported'))
  OR (OLD.status='calling' AND NEW.status='failed' AND NEW.error_code IS NULL)
  OR NOT ((OLD.status='approval_pending' AND NEW.status IN('queued','rejected'))
    OR (OLD.status='queued' AND NEW.status IN('probing','blocked','failed'))
    OR (OLD.status='probing' AND NEW.status IN(
      'probing','queued','calling','blocked','failed'))
    OR (OLD.status='calling' AND NEW.status IN(
      'succeeded','failed','blocked','outcome_unknown')))
BEGIN SELECT RAISE(ABORT,'mcp_invocation_transition_invalid'); END;
CREATE TRIGGER trg_mcp_invocations_delete_prd082
BEFORE DELETE ON mcp_invocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_invocation_immutable'); END;
CREATE TRIGGER trg_mcp_attempts_owner_insert_prd082
BEFORE INSERT ON mcp_invocation_attempts FOR EACH ROW
WHEN NEW.status!='processing' OR NEW.error_code IS NOT NULL
  OR NEW.completed_at IS NOT NULL OR NEW.duration_ms IS NOT NULL
  OR NEW.response_bytes IS NOT NULL
  OR (NEW.attempt_kind='probe' AND NEW.request_bytes IS NOT NULL)
  OR (NEW.attempt_kind='call' AND NEW.request_bytes IS NULL)
  OR NOT EXISTS(SELECT 1 FROM mcp_invocations invocation
    WHERE invocation.tenant_id=NEW.tenant_id
      AND invocation.mcp_invocation_id=NEW.mcp_invocation_id
      AND invocation.lease_generation=NEW.lease_generation
      AND ((NEW.attempt_kind='probe' AND invocation.status='probing'
          AND NEW.attempt_number=invocation.probe_attempt_count)
        OR (NEW.attempt_kind='call' AND invocation.status='calling'
          AND NEW.attempt_number=invocation.call_attempt_count)))
  OR EXISTS(SELECT 1 FROM mcp_invocation_attempts current
    WHERE current.mcp_invocation_attempt_id=NEW.mcp_invocation_attempt_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_attempt_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_attempts_transition_prd082
BEFORE UPDATE ON mcp_invocation_attempts FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.mcp_invocation_attempt_id!=OLD.mcp_invocation_attempt_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.mcp_invocation_id!=OLD.mcp_invocation_id
  OR NEW.attempt_kind!=OLD.attempt_kind OR NEW.attempt_number!=OLD.attempt_number
  OR NEW.lease_generation!=OLD.lease_generation OR NEW.started_at!=OLD.started_at
  OR NEW.call_started_at IS NOT OLD.call_started_at
  OR (NEW.attempt_kind='call' AND NEW.request_bytes IS NULL)
  OR (NEW.attempt_kind='probe' AND OLD.request_bytes IS NOT NULL
    AND NEW.request_bytes IS NOT OLD.request_bytes)
  OR (NEW.attempt_kind='probe' AND NEW.status='outcome_unknown')
  OR (NEW.attempt_kind='probe' AND NEW.status='failed'
    AND NEW.error_code NOT IN('mcp.executor_disabled','mcp.arguments_blocked',
      'mcp.authorization_revoked','mcp.server_unavailable','mcp.state_conflict',
      'mcp.unavailable','mcp.network_blocked','mcp.sandbox_unavailable'))
  OR (NEW.attempt_kind='call' AND NEW.status IN('failed','outcome_unknown')
    AND NEW.error_code NOT IN('mcp.tool_failed','mcp.result_blocked',
      'mcp.output_dlp_unavailable','mcp.result_invalid','mcp.outcome_unknown',
      'mcp.state_conflict','mcp.unavailable'))
  OR (NEW.status='outcome_unknown' AND NEW.error_code!='mcp.outcome_unknown')
  OR NOT EXISTS(
    SELECT 1 FROM mcp_invocations invocation
    WHERE invocation.tenant_id=OLD.tenant_id
      AND invocation.mcp_invocation_id=OLD.mcp_invocation_id
      AND invocation.lease_generation=OLD.lease_generation
      AND invocation.lease_owner_id IS NOT NULL
      AND invocation.lease_token IS NOT NULL
      AND unixepoch(NEW.completed_at)>=unixepoch(invocation.updated_at)
      AND ((OLD.attempt_kind='probe' AND invocation.status='probing'
          AND invocation.probe_attempt_count=OLD.attempt_number)
        OR (OLD.attempt_kind='call' AND invocation.status='calling'
          AND invocation.call_attempt_count=OLD.attempt_number))
      AND (
        unixepoch(NEW.completed_at)<unixepoch(invocation.lease_expires_at)
        OR (unixepoch(NEW.completed_at)>=unixepoch(invocation.lease_expires_at)
          AND ((OLD.attempt_kind='probe' AND NEW.status='failed'
              AND NEW.error_code='mcp.server_unavailable')
            OR (OLD.attempt_kind='call' AND NEW.status='outcome_unknown'
              AND NEW.error_code='mcp.outcome_unknown')))))
  OR OLD.status!='processing' OR NEW.status NOT IN('succeeded','failed','outcome_unknown')
BEGIN SELECT RAISE(ABORT,'mcp_attempt_transition_invalid'); END;
CREATE TRIGGER trg_mcp_attempts_delete_prd082
BEFORE DELETE ON mcp_invocation_attempts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_attempt_immutable'); END;

-- Immutable current policy and revision graph.
CREATE TABLE mcp_network_policy_revisions(
  mcp_network_policy_id TEXT NOT NULL CHECK(length(mcp_network_policy_id)=38
    AND substr(mcp_network_policy_id,1,6)='mcpnp_'
    AND substr(mcp_network_policy_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  policy_revision INTEGER NOT NULL CHECK(typeof(policy_revision)='integer'
    AND policy_revision>=1),
  policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint)=64
    AND policy_fingerprint NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('disabled','active')),
  egress_mode TEXT NOT NULL CHECK(egress_mode='registered_endpoint_only'),
  priority INTEGER NOT NULL CHECK(priority=1000),
  condition_expr TEXT NOT NULL CHECK(condition_expr='registered_endpoint_origin'),
  action TEXT NOT NULL CHECK(action='allow'),
  connect_timeout_seconds INTEGER NOT NULL CHECK(typeof(connect_timeout_seconds)='integer'
    AND connect_timeout_seconds BETWEEN 1 AND 10),
  probe_timeout_seconds INTEGER NOT NULL CHECK(typeof(probe_timeout_seconds)='integer'
    AND probe_timeout_seconds BETWEEN 1 AND 30),
  call_timeout_seconds INTEGER NOT NULL CHECK(typeof(call_timeout_seconds)='integer'
    AND call_timeout_seconds BETWEEN 1 AND 120),
  failure_threshold INTEGER NOT NULL CHECK(typeof(failure_threshold)='integer'
    AND failure_threshold BETWEEN 1 AND 10),
  open_seconds INTEGER NOT NULL CHECK(typeof(open_seconds)='integer'
    AND open_seconds BETWEEN 10 AND 3600),
  updated_by TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_server_id) REFERENCES mcp_servers(tenant_id,mcp_server_id),
  FOREIGN KEY(tenant_id,updated_by) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,mcp_network_policy_id,policy_revision),
  UNIQUE(tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision),
  UNIQUE(tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision,
    policy_fingerprint,status),
  CHECK(connect_timeout_seconds<=probe_timeout_seconds
    AND connect_timeout_seconds<=call_timeout_seconds)
);
CREATE INDEX idx_mcp_network_policy_revisions_tenant_prd083
  ON mcp_network_policy_revisions(
    tenant_id,mcp_network_policy_id,policy_revision DESC);

CREATE TABLE mcp_network_policies(
  mcp_network_policy_id TEXT PRIMARY KEY CHECK(length(mcp_network_policy_id)=38
    AND substr(mcp_network_policy_id,1,6)='mcpnp_'
    AND substr(mcp_network_policy_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  current_revision INTEGER NOT NULL CHECK(typeof(current_revision)='integer'
    AND current_revision>=1),
  current_fingerprint TEXT NOT NULL CHECK(length(current_fingerprint)=64
    AND current_fingerprint NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('disabled','active')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_server_id) REFERENCES mcp_servers(tenant_id,mcp_server_id),
  FOREIGN KEY(
    tenant_id,mcp_network_policy_id,mcp_server_id,current_revision,
    current_fingerprint,status
  ) REFERENCES mcp_network_policy_revisions(
    tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision,
    policy_fingerprint,status
  ) DEFERRABLE INITIALLY DEFERRED,
  UNIQUE(tenant_id,mcp_network_policy_id),
  UNIQUE(tenant_id,mcp_network_policy_id,mcp_server_id),
  UNIQUE(tenant_id,mcp_server_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at))
);
CREATE INDEX idx_mcp_network_policies_tenant_status_prd083
  ON mcp_network_policies(tenant_id,status,updated_at DESC,mcp_network_policy_id);

CREATE TRIGGER trg_mcp_network_revisions_owner_insert_prd083
BEFORE INSERT ON mcp_network_policy_revisions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM mcp_servers server JOIN users actor
    ON actor.tenant_id=server.tenant_id
    WHERE server.tenant_id=NEW.tenant_id AND server.mcp_server_id=NEW.mcp_server_id
      AND actor.user_id=NEW.updated_by AND actor.status='active')
  OR NOT (
    (NEW.policy_revision=1 AND NEW.status='disabled'
      AND NEW.egress_mode='registered_endpoint_only'
      AND NEW.policy_fingerprint=
        '1b0e1d1610dc98e12e09661d2ee481e0d8ee8961faec8a691f16fe6cc603c4f6'
      AND NEW.connect_timeout_seconds=5 AND NEW.probe_timeout_seconds=10
      AND NEW.call_timeout_seconds=30 AND NEW.failure_threshold=3
      AND NEW.open_seconds=60
      AND NOT EXISTS(SELECT 1 FROM mcp_network_policies current
        WHERE current.tenant_id=NEW.tenant_id
          AND current.mcp_server_id=NEW.mcp_server_id))
    OR EXISTS(SELECT 1 FROM mcp_network_policies current
      WHERE current.tenant_id=NEW.tenant_id
        AND current.mcp_server_id=NEW.mcp_server_id
        AND current.mcp_network_policy_id=NEW.mcp_network_policy_id
        AND NEW.policy_revision=current.current_revision+1
        AND NEW.created_at>=current.updated_at))
  OR EXISTS(SELECT 1 FROM mcp_network_policy_revisions current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.mcp_network_policy_id=NEW.mcp_network_policy_id
      AND current.policy_revision=NEW.policy_revision)
BEGIN SELECT RAISE(ABORT,'mcp_network_revision_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_network_revisions_update_prd083
BEFORE UPDATE ON mcp_network_policy_revisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_revision_immutable'); END;
CREATE TRIGGER trg_mcp_network_revisions_delete_prd083
BEFORE DELETE ON mcp_network_policy_revisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_revision_immutable'); END;

CREATE TRIGGER trg_mcp_network_policies_owner_insert_prd083
BEFORE INSERT ON mcp_network_policies FOR EACH ROW
WHEN NEW.current_revision!=1 OR NEW.status!='disabled'
  OR NEW.current_fingerprint!=
    '1b0e1d1610dc98e12e09661d2ee481e0d8ee8961faec8a691f16fe6cc603c4f6'
  OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM mcp_network_policy_revisions revision
    WHERE revision.tenant_id=NEW.tenant_id
      AND revision.mcp_network_policy_id=NEW.mcp_network_policy_id
      AND revision.mcp_server_id=NEW.mcp_server_id
      AND revision.policy_revision=1
      AND revision.policy_fingerprint=NEW.current_fingerprint
      AND revision.status=NEW.status)
  OR EXISTS(SELECT 1 FROM mcp_network_policies current
    WHERE current.mcp_network_policy_id=NEW.mcp_network_policy_id
      OR (current.tenant_id=NEW.tenant_id
        AND current.mcp_server_id=NEW.mcp_server_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_network_policy_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_network_policies_transition_prd083
BEFORE UPDATE ON mcp_network_policies FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.mcp_network_policy_id!=OLD.mcp_network_policy_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.mcp_server_id!=OLD.mcp_server_id
  OR NEW.created_at!=OLD.created_at OR NEW.current_revision!=OLD.current_revision+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT EXISTS(SELECT 1 FROM mcp_network_policy_revisions revision
    WHERE revision.tenant_id=NEW.tenant_id
      AND revision.mcp_network_policy_id=NEW.mcp_network_policy_id
      AND revision.mcp_server_id=NEW.mcp_server_id
      AND revision.policy_revision=NEW.current_revision
      AND revision.policy_fingerprint=NEW.current_fingerprint
      AND revision.status=NEW.status AND revision.created_at=NEW.updated_at)
BEGIN SELECT RAISE(ABORT,'mcp_network_policy_transition_invalid'); END;
CREATE TRIGGER trg_mcp_network_policies_delete_prd083
BEFORE DELETE ON mcp_network_policies FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_policy_immutable'); END;

-- Every existing and future server starts with an explicit deny-all revision.
INSERT INTO mcp_network_policy_revisions(
  mcp_network_policy_id,tenant_id,mcp_server_id,policy_revision,policy_fingerprint,
  status,egress_mode,priority,condition_expr,action,connect_timeout_seconds,
  probe_timeout_seconds,call_timeout_seconds,failure_threshold,open_seconds,
  updated_by,request_id,trace_id,created_at
)
SELECT 'mcpnp_'||substr(metahub_sha256(
    server.tenant_id||char(31)||server.mcp_server_id||char(31)||'network-policy'),1,32),
  server.tenant_id,server.mcp_server_id,1,
  '1b0e1d1610dc98e12e09661d2ee481e0d8ee8961faec8a691f16fe6cc603c4f6',
  'disabled','registered_endpoint_only',1000,'registered_endpoint_origin','allow',
  5,10,30,3,60,server.created_by,'migration:0076','migration:0076',server.created_at
FROM mcp_servers server;
INSERT INTO mcp_network_policies(
  mcp_network_policy_id,tenant_id,mcp_server_id,current_revision,current_fingerprint,
  status,created_at,updated_at
)
SELECT revision.mcp_network_policy_id,revision.tenant_id,revision.mcp_server_id,1,
  revision.policy_fingerprint,'disabled',revision.created_at,revision.created_at
FROM mcp_network_policy_revisions revision WHERE revision.policy_revision=1;

CREATE TRIGGER trg_mcp_servers_network_policy_insert_prd083
AFTER INSERT ON mcp_servers FOR EACH ROW
BEGIN
  INSERT INTO mcp_network_policy_revisions(
    mcp_network_policy_id,tenant_id,mcp_server_id,policy_revision,policy_fingerprint,
    status,egress_mode,priority,condition_expr,action,connect_timeout_seconds,
    probe_timeout_seconds,call_timeout_seconds,failure_threshold,open_seconds,
    updated_by,request_id,trace_id,created_at
  ) VALUES(
    'mcpnp_'||substr(metahub_sha256(
      NEW.tenant_id||char(31)||NEW.mcp_server_id||char(31)||'network-policy'),1,32),
    NEW.tenant_id,NEW.mcp_server_id,1,
    '1b0e1d1610dc98e12e09661d2ee481e0d8ee8961faec8a691f16fe6cc603c4f6',
    'disabled','registered_endpoint_only',1000,'registered_endpoint_origin','allow',
    5,10,30,3,60,NEW.created_by,'server-create','server-create',NEW.created_at
  );
  INSERT INTO mcp_network_policies(
    mcp_network_policy_id,tenant_id,mcp_server_id,current_revision,current_fingerprint,
    status,created_at,updated_at
  ) VALUES(
    'mcpnp_'||substr(metahub_sha256(
      NEW.tenant_id||char(31)||NEW.mcp_server_id||char(31)||'network-policy'),1,32),
    NEW.tenant_id,NEW.mcp_server_id,1,
    '1b0e1d1610dc98e12e09661d2ee481e0d8ee8961faec8a691f16fe6cc603c4f6',
    'disabled',NEW.created_at,NEW.created_at
  );
END;

CREATE TABLE mcp_invocation_network_bindings(
  mcp_invocation_network_binding_id TEXT PRIMARY KEY
    CHECK(length(mcp_invocation_network_binding_id)=38
      AND substr(mcp_invocation_network_binding_id,1,6)='mcpnb_'
      AND substr(mcp_invocation_network_binding_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_invocation_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  mcp_tool_id TEXT NOT NULL,
  mcp_network_policy_id TEXT NOT NULL,
  policy_revision INTEGER NOT NULL CHECK(typeof(policy_revision)='integer'
    AND policy_revision>=1),
  policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint)=64
    AND policy_fingerprint NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('disabled','active')),
  egress_mode TEXT NOT NULL CHECK(egress_mode='registered_endpoint_only'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_invocation_id,mcp_server_id,mcp_tool_id)
    REFERENCES mcp_invocations(
      tenant_id,mcp_invocation_id,mcp_server_id,mcp_tool_id),
  FOREIGN KEY(tenant_id,mcp_server_id)
    REFERENCES mcp_servers(tenant_id,mcp_server_id),
  FOREIGN KEY(tenant_id,mcp_server_id,mcp_tool_id)
    REFERENCES mcp_tools(tenant_id,mcp_server_id,mcp_tool_id),
  FOREIGN KEY(tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision,
    policy_fingerprint,status)
    REFERENCES mcp_network_policy_revisions(
      tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision,
      policy_fingerprint,status),
  UNIQUE(tenant_id,mcp_invocation_network_binding_id),
  UNIQUE(tenant_id,mcp_invocation_id),
  UNIQUE(tenant_id,mcp_invocation_id,mcp_server_id,mcp_tool_id,
    mcp_network_policy_id,policy_revision,policy_fingerprint)
);
CREATE INDEX idx_mcp_network_bindings_tenant_revision_prd083
  ON mcp_invocation_network_bindings(
    tenant_id,mcp_network_policy_id,policy_revision,mcp_invocation_id);
CREATE TRIGGER trg_mcp_network_bindings_owner_insert_prd083
BEFORE INSERT ON mcp_invocation_network_bindings FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM mcp_invocations invocation
    JOIN mcp_network_policies policy ON policy.tenant_id=invocation.tenant_id
      AND policy.mcp_server_id=invocation.mcp_server_id
    JOIN mcp_network_policy_revisions revision
      ON revision.tenant_id=policy.tenant_id
      AND revision.mcp_network_policy_id=policy.mcp_network_policy_id
      AND revision.policy_revision=policy.current_revision
    WHERE invocation.tenant_id=NEW.tenant_id
      AND invocation.mcp_invocation_id=NEW.mcp_invocation_id
      AND invocation.mcp_server_id=NEW.mcp_server_id
      AND invocation.mcp_tool_id=NEW.mcp_tool_id
      AND policy.mcp_network_policy_id=NEW.mcp_network_policy_id
      AND policy.current_revision=NEW.policy_revision
      AND policy.current_fingerprint=NEW.policy_fingerprint
      AND policy.status=NEW.status
      AND revision.egress_mode=NEW.egress_mode
      AND invocation.created_at=NEW.created_at)
  OR EXISTS(SELECT 1 FROM mcp_invocation_network_bindings current
    WHERE current.mcp_invocation_network_binding_id=
        NEW.mcp_invocation_network_binding_id
      OR (current.tenant_id=NEW.tenant_id
        AND current.mcp_invocation_id=NEW.mcp_invocation_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_network_binding_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_network_bindings_update_prd083
BEFORE UPDATE ON mcp_invocation_network_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_binding_immutable'); END;
CREATE TRIGGER trg_mcp_network_bindings_delete_prd083
BEFORE DELETE ON mcp_invocation_network_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_binding_immutable'); END;

INSERT INTO mcp_invocation_network_bindings(
  mcp_invocation_network_binding_id,tenant_id,mcp_invocation_id,mcp_server_id,
  mcp_tool_id,mcp_network_policy_id,policy_revision,policy_fingerprint,status,
  egress_mode,created_at
)
SELECT 'mcpnb_'||substr(metahub_sha256(
    invocation.tenant_id||char(31)||invocation.mcp_invocation_id||
      char(31)||'network-binding'),1,32),
  invocation.tenant_id,invocation.mcp_invocation_id,invocation.mcp_server_id,
  invocation.mcp_tool_id,policy.mcp_network_policy_id,policy.current_revision,
  policy.current_fingerprint,policy.status,revision.egress_mode,invocation.created_at
FROM mcp_invocations invocation JOIN mcp_network_policies policy
  ON policy.tenant_id=invocation.tenant_id
    AND policy.mcp_server_id=invocation.mcp_server_id
JOIN mcp_network_policy_revisions revision
  ON revision.tenant_id=policy.tenant_id
    AND revision.mcp_network_policy_id=policy.mcp_network_policy_id
    AND revision.policy_revision=policy.current_revision;

CREATE TRIGGER trg_mcp_invocations_network_binding_insert_prd083
AFTER INSERT ON mcp_invocations FOR EACH ROW
BEGIN
  INSERT INTO mcp_invocation_network_bindings(
    mcp_invocation_network_binding_id,tenant_id,mcp_invocation_id,mcp_server_id,
    mcp_tool_id,mcp_network_policy_id,policy_revision,policy_fingerprint,status,
    egress_mode,created_at
  )
  SELECT 'mcpnb_'||substr(metahub_sha256(
      NEW.tenant_id||char(31)||NEW.mcp_invocation_id||
        char(31)||'network-binding'),1,32),
    NEW.tenant_id,NEW.mcp_invocation_id,NEW.mcp_server_id,NEW.mcp_tool_id,
    policy.mcp_network_policy_id,policy.current_revision,policy.current_fingerprint,
    policy.status,revision.egress_mode,NEW.created_at
  FROM mcp_network_policies policy JOIN mcp_network_policy_revisions revision
    ON revision.tenant_id=policy.tenant_id
      AND revision.mcp_network_policy_id=policy.mcp_network_policy_id
      AND revision.policy_revision=policy.current_revision
  WHERE policy.tenant_id=NEW.tenant_id AND policy.mcp_server_id=NEW.mcp_server_id;
END;

CREATE TABLE mcp_network_decisions(
  mcp_network_decision_id TEXT PRIMARY KEY CHECK(length(mcp_network_decision_id)=38
    AND substr(mcp_network_decision_id,1,6)='mcpnd_'
    AND substr(mcp_network_decision_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  mcp_server_operation_id TEXT,
  mcp_invocation_id TEXT,
  mcp_network_policy_id TEXT NOT NULL,
  phase TEXT NOT NULL CHECK(phase IN('sync','probe','call_preflight')),
  result TEXT NOT NULL CHECK(result IN('allowed','blocked')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'allowed','policy_disabled','address_denied','mixed_dns_answers',
    'port_denied','redirect_denied')),
  policy_revision INTEGER NOT NULL CHECK(typeof(policy_revision)='integer'
    AND policy_revision>=1),
  policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint)=64
    AND policy_fingerprint NOT GLOB '*[^0-9a-f]*'),
  address_count INTEGER NOT NULL CHECK(typeof(address_count)='integer'
    AND address_count BETWEEN 0 AND 16),
  duration_ms INTEGER NOT NULL CHECK(typeof(duration_ms)='integer'
    AND duration_ms BETWEEN 0 AND 300000),
  attempt_number INTEGER NOT NULL CHECK(typeof(attempt_number)='integer'
    AND attempt_number BETWEEN 1 AND 10),
  dispatch_id TEXT NOT NULL CHECK(length(dispatch_id) BETWEEN 1 AND 160),
  permit_digest TEXT CHECK(permit_digest IS NULL OR(length(permit_digest)=64
    AND permit_digest NOT GLOB '*[^0-9a-f]*')),
  observed_circuit_revision INTEGER NOT NULL CHECK(
    typeof(observed_circuit_revision)='integer' AND observed_circuit_revision>=0),
  half_open_owner_type TEXT CHECK(half_open_owner_type IS NULL
    OR half_open_owner_type IN('operation','invocation')),
  half_open_owner_id TEXT,
  half_open_lease_generation INTEGER CHECK(half_open_lease_generation IS NULL
    OR(typeof(half_open_lease_generation)='integer' AND half_open_lease_generation>=1)),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_server_id)
    REFERENCES mcp_servers(tenant_id,mcp_server_id),
  FOREIGN KEY(tenant_id,mcp_server_operation_id)
    REFERENCES mcp_server_operations(tenant_id,mcp_server_operation_id),
  FOREIGN KEY(tenant_id,mcp_invocation_id)
    REFERENCES mcp_invocations(tenant_id,mcp_invocation_id),
  FOREIGN KEY(tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision)
    REFERENCES mcp_network_policy_revisions(
      tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision),
  UNIQUE(tenant_id,mcp_network_decision_id),
  UNIQUE(tenant_id,mcp_network_decision_id,mcp_server_id,mcp_server_operation_id,
    mcp_invocation_id,phase,result,reason_code,mcp_network_policy_id,
    policy_revision,policy_fingerprint,permit_digest),
  CHECK((mcp_server_operation_id IS NULL)!=(mcp_invocation_id IS NULL)),
  CHECK((result='allowed' AND reason_code='allowed')
    OR (result='blocked' AND reason_code!='allowed')),
  CHECK((result='allowed' AND phase='call_preflight' AND permit_digest IS NOT NULL)
    OR (NOT(result='allowed' AND phase='call_preflight') AND permit_digest IS NULL)),
  CHECK((half_open_owner_type IS NULL AND half_open_owner_id IS NULL
      AND half_open_lease_generation IS NULL)
    OR (half_open_owner_type IS NOT NULL AND half_open_owner_id IS NOT NULL
      AND half_open_lease_generation IS NOT NULL))
);
CREATE INDEX idx_mcp_network_decisions_tenant_time_prd083
  ON mcp_network_decisions(
    tenant_id,mcp_server_id,policy_revision,created_at DESC,mcp_network_decision_id);
CREATE TRIGGER trg_mcp_network_decisions_owner_insert_prd083
BEFORE INSERT ON mcp_network_decisions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM mcp_network_policy_revisions revision
    WHERE revision.tenant_id=NEW.tenant_id
      AND revision.mcp_network_policy_id=NEW.mcp_network_policy_id
      AND revision.mcp_server_id=NEW.mcp_server_id
      AND revision.policy_revision=NEW.policy_revision
      AND revision.policy_fingerprint=NEW.policy_fingerprint)
  OR (NEW.mcp_server_operation_id IS NOT NULL AND (
    NEW.phase NOT IN('sync','probe')
    OR NOT EXISTS(SELECT 1 FROM mcp_server_operations operation
      WHERE operation.tenant_id=NEW.tenant_id
        AND operation.mcp_server_id=NEW.mcp_server_id
        AND operation.mcp_server_operation_id=NEW.mcp_server_operation_id)))
  OR (NEW.mcp_invocation_id IS NOT NULL AND (
    NEW.phase NOT IN('probe','call_preflight')
    OR NOT EXISTS(SELECT 1 FROM mcp_invocations invocation
      WHERE invocation.tenant_id=NEW.tenant_id
        AND invocation.mcp_server_id=NEW.mcp_server_id
        AND invocation.mcp_invocation_id=NEW.mcp_invocation_id)))
  OR EXISTS(SELECT 1 FROM mcp_network_decisions current
    WHERE current.mcp_network_decision_id=NEW.mcp_network_decision_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_network_decision_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_network_decisions_update_prd083
BEFORE UPDATE ON mcp_network_decisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_decision_immutable'); END;
CREATE TRIGGER trg_mcp_network_decisions_delete_prd083
BEFORE DELETE ON mcp_network_decisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_decision_immutable'); END;

CREATE TABLE mcp_network_call_intents(
  mcp_network_call_intent_id TEXT PRIMARY KEY CHECK(length(mcp_network_call_intent_id)=38
    AND substr(mcp_network_call_intent_id,1,6)='mcpni_'
    AND substr(mcp_network_call_intent_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_invocation_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  mcp_tool_id TEXT NOT NULL,
  mcp_network_decision_id TEXT NOT NULL,
  mcp_network_policy_id TEXT NOT NULL,
  policy_revision INTEGER NOT NULL CHECK(typeof(policy_revision)='integer'
    AND policy_revision>=1),
  policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint)=64
    AND policy_fingerprint NOT GLOB '*[^0-9a-f]*'),
  permit_digest TEXT NOT NULL CHECK(length(permit_digest)=64
    AND permit_digest NOT GLOB '*[^0-9a-f]*'),
  endpoint_hmac TEXT NOT NULL CHECK(length(endpoint_hmac)=64
    AND endpoint_hmac NOT GLOB '*[^0-9a-f]*'),
  endpoint_hmac_key_version INTEGER NOT NULL CHECK(
    typeof(endpoint_hmac_key_version)='integer' AND endpoint_hmac_key_version>=1),
  credential_revision INTEGER NOT NULL CHECK(typeof(credential_revision)='integer'
    AND credential_revision>=1),
  schema_revision INTEGER NOT NULL CHECK(typeof(schema_revision)='integer'
    AND schema_revision>=1),
  tool_policy_revision INTEGER NOT NULL CHECK(typeof(tool_policy_revision)='integer'
    AND tool_policy_revision>=1),
  tool_policy_fingerprint TEXT NOT NULL CHECK(length(tool_policy_fingerprint)=64
    AND tool_policy_fingerprint NOT GLOB '*[^0-9a-f]*'),
  arguments_hmac TEXT NOT NULL CHECK(length(arguments_hmac)=64
    AND arguments_hmac NOT GLOB '*[^0-9a-f]*'),
  arguments_hmac_key_version INTEGER NOT NULL CHECK(
    typeof(arguments_hmac_key_version)='integer' AND arguments_hmac_key_version>=1),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer'
    AND lease_generation>=1),
  lease_owner_id TEXT NOT NULL CHECK(length(lease_owner_id) BETWEEN 1 AND 160),
  lease_token_hmac TEXT NOT NULL CHECK(length(lease_token_hmac)=64
    AND lease_token_hmac NOT GLOB '*[^0-9a-f]*'),
  authorized_at TEXT NOT NULL CHECK(unixepoch(authorized_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_invocation_id,mcp_server_id,mcp_tool_id)
    REFERENCES mcp_invocations(
      tenant_id,mcp_invocation_id,mcp_server_id,mcp_tool_id),
  FOREIGN KEY(tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision)
    REFERENCES mcp_network_policy_revisions(
      tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision),
  FOREIGN KEY(tenant_id,mcp_network_decision_id)
    REFERENCES mcp_network_decisions(tenant_id,mcp_network_decision_id),
  UNIQUE(tenant_id,mcp_network_call_intent_id),
  UNIQUE(tenant_id,mcp_invocation_id),
  CHECK(unixepoch(created_at)>=unixepoch(authorized_at))
);
CREATE INDEX idx_mcp_network_call_intents_tenant_prd083
  ON mcp_network_call_intents(tenant_id,mcp_server_id,created_at,mcp_invocation_id);
CREATE TRIGGER trg_mcp_network_call_intents_owner_insert_prd083
BEFORE INSERT ON mcp_network_call_intents FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM mcp_network_decisions decision
    JOIN mcp_invocations invocation ON invocation.tenant_id=decision.tenant_id
      AND invocation.mcp_invocation_id=decision.mcp_invocation_id
    JOIN mcp_servers server ON server.tenant_id=invocation.tenant_id
      AND server.mcp_server_id=invocation.mcp_server_id
    WHERE decision.tenant_id=NEW.tenant_id
      AND decision.mcp_network_decision_id=NEW.mcp_network_decision_id
      AND decision.mcp_invocation_id=NEW.mcp_invocation_id
      AND decision.mcp_server_id=NEW.mcp_server_id
      AND decision.phase='call_preflight' AND decision.result='allowed'
      AND decision.mcp_network_policy_id=NEW.mcp_network_policy_id
      AND decision.policy_revision=NEW.policy_revision
      AND decision.policy_fingerprint=NEW.policy_fingerprint
      AND decision.permit_digest=NEW.permit_digest
      AND invocation.mcp_tool_id=NEW.mcp_tool_id AND invocation.status='calling'
      AND invocation.credential_revision=NEW.credential_revision
      AND invocation.schema_revision=NEW.schema_revision
      AND invocation.policy_revision=NEW.tool_policy_revision
      AND invocation.policy_fingerprint=NEW.tool_policy_fingerprint
      AND invocation.arguments_hmac=NEW.arguments_hmac
      AND invocation.arguments_hmac_key_version=NEW.arguments_hmac_key_version
      AND invocation.lease_generation=NEW.lease_generation
      AND invocation.lease_owner_id=NEW.lease_owner_id
      AND server.endpoint_hmac=NEW.endpoint_hmac
      AND server.endpoint_hmac_key_version=NEW.endpoint_hmac_key_version)
  OR EXISTS(SELECT 1 FROM mcp_network_call_intents current
    WHERE current.mcp_network_call_intent_id=NEW.mcp_network_call_intent_id
      OR (current.tenant_id=NEW.tenant_id
        AND current.mcp_invocation_id=NEW.mcp_invocation_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_network_call_intent_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_network_call_intents_update_prd083
BEFORE UPDATE ON mcp_network_call_intents FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_call_intent_immutable'); END;
CREATE TRIGGER trg_mcp_network_call_intents_delete_prd083
BEFORE DELETE ON mcp_network_call_intents FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_call_intent_immutable'); END;

CREATE TABLE mcp_network_circuits(
  tenant_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  mcp_network_policy_id TEXT NOT NULL,
  policy_revision INTEGER NOT NULL CHECK(typeof(policy_revision)='integer'
    AND policy_revision>=1),
  state TEXT NOT NULL CHECK(state IN('closed','open','half_open')),
  failure_count INTEGER NOT NULL CHECK(typeof(failure_count)='integer'
    AND failure_count BETWEEN 0 AND 1000000),
  opened_until TEXT CHECK(opened_until IS NULL OR unixepoch(opened_until) IS NOT NULL),
  half_open_owner_type TEXT CHECK(half_open_owner_type IS NULL
    OR half_open_owner_type IN('operation','invocation')),
  half_open_owner_id TEXT,
  half_open_lease_generation INTEGER CHECK(half_open_lease_generation IS NULL
    OR(typeof(half_open_lease_generation)='integer' AND half_open_lease_generation>=1)),
  half_open_lease_owner_id TEXT,
  half_open_lease_token TEXT,
  half_open_lease_expires_at TEXT CHECK(half_open_lease_expires_at IS NULL
    OR unixepoch(half_open_lease_expires_at) IS NOT NULL),
  last_failure_code TEXT CHECK(last_failure_code IS NULL OR last_failure_code IN(
    'dns_unavailable','connect_unavailable','tls_unavailable','headers_timeout',
    'body_timeout','overall_timeout','mcp.sandbox_unavailable')),
  state_revision INTEGER NOT NULL CHECK(typeof(state_revision)='integer'
    AND state_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_server_id)
    REFERENCES mcp_servers(tenant_id,mcp_server_id),
  FOREIGN KEY(tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision)
    REFERENCES mcp_network_policy_revisions(
      tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision),
  PRIMARY KEY(tenant_id,mcp_server_id,policy_revision),
  UNIQUE(tenant_id,mcp_network_policy_id,mcp_server_id,policy_revision),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((state='closed' AND opened_until IS NULL
      AND half_open_owner_type IS NULL AND half_open_owner_id IS NULL
      AND half_open_lease_generation IS NULL AND half_open_lease_owner_id IS NULL
      AND half_open_lease_token IS NULL AND half_open_lease_expires_at IS NULL)
    OR (state='open' AND opened_until IS NOT NULL
      AND half_open_owner_type IS NULL AND half_open_owner_id IS NULL
      AND half_open_lease_generation IS NULL AND half_open_lease_owner_id IS NULL
      AND half_open_lease_token IS NULL AND half_open_lease_expires_at IS NULL)
    OR (state='half_open' AND opened_until IS NULL
      AND half_open_owner_type IS NOT NULL AND half_open_owner_id IS NOT NULL
      AND half_open_lease_generation IS NOT NULL
      AND half_open_lease_owner_id IS NOT NULL
      AND length(half_open_lease_owner_id) BETWEEN 1 AND 160
      AND half_open_lease_token IS NOT NULL AND length(half_open_lease_token)=64
      AND half_open_lease_token NOT GLOB '*[^0-9a-f]*'
      AND half_open_lease_expires_at IS NOT NULL))
);
CREATE INDEX idx_mcp_network_circuits_tenant_state_prd083
  ON mcp_network_circuits(tenant_id,state,opened_until,mcp_server_id,policy_revision);
CREATE TRIGGER trg_mcp_network_circuits_owner_insert_prd083
BEFORE INSERT ON mcp_network_circuits FOR EACH ROW
WHEN NEW.state!='closed' OR NEW.failure_count!=0 OR NEW.last_failure_code IS NOT NULL
  OR NEW.state_revision!=1 OR NEW.created_at!=NEW.updated_at
  OR NOT EXISTS(SELECT 1 FROM mcp_network_policy_revisions revision
    WHERE revision.tenant_id=NEW.tenant_id
      AND revision.mcp_server_id=NEW.mcp_server_id
      AND revision.mcp_network_policy_id=NEW.mcp_network_policy_id
      AND revision.policy_revision=NEW.policy_revision)
  OR EXISTS(SELECT 1 FROM mcp_network_circuits current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.mcp_server_id=NEW.mcp_server_id
      AND current.policy_revision=NEW.policy_revision)
BEGIN SELECT RAISE(ABORT,'mcp_network_circuit_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_network_circuits_transition_prd083
BEFORE UPDATE ON mcp_network_circuits FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.mcp_server_id!=OLD.mcp_server_id
  OR NEW.mcp_network_policy_id!=OLD.mcp_network_policy_id
  OR NEW.policy_revision!=OLD.policy_revision OR NEW.created_at!=OLD.created_at
  OR NEW.state_revision!=OLD.state_revision+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR (NEW.state='half_open' AND (
    NOT EXISTS(SELECT 1 FROM mcp_network_policies policy
      WHERE policy.tenant_id=NEW.tenant_id
        AND policy.mcp_server_id=NEW.mcp_server_id
        AND policy.mcp_network_policy_id=NEW.mcp_network_policy_id
        AND policy.current_revision=NEW.policy_revision
        AND policy.status='active')
    OR unixepoch(NEW.half_open_lease_expires_at)<=unixepoch(NEW.updated_at)
    OR NOT (
      (NEW.half_open_owner_type='operation' AND EXISTS(
        SELECT 1 FROM mcp_server_operations operation
        JOIN mcp_server_sync_jobs job
          ON job.tenant_id=operation.tenant_id
         AND job.mcp_server_id=operation.mcp_server_id
         AND job.mcp_server_operation_id=operation.mcp_server_operation_id
        WHERE operation.tenant_id=NEW.tenant_id
          AND operation.mcp_server_id=NEW.mcp_server_id
          AND operation.mcp_server_operation_id=NEW.half_open_owner_id
          AND operation.status='processing' AND job.status='processing'
          AND job.lease_generation=NEW.half_open_lease_generation
          AND job.lease_owner_id=NEW.half_open_lease_owner_id
          AND job.lease_token=NEW.half_open_lease_token
          AND job.lease_expires_at=NEW.half_open_lease_expires_at))
      OR (NEW.half_open_owner_type='invocation' AND EXISTS(
        SELECT 1 FROM mcp_invocations invocation
        WHERE invocation.tenant_id=NEW.tenant_id
          AND invocation.mcp_server_id=NEW.mcp_server_id
          AND invocation.mcp_invocation_id=NEW.half_open_owner_id
          AND invocation.status IN('probing','calling')
          AND invocation.lease_generation=NEW.half_open_lease_generation
          AND invocation.lease_owner_id=NEW.half_open_lease_owner_id
          AND invocation.lease_token=NEW.half_open_lease_token
          AND invocation.lease_expires_at=NEW.half_open_lease_expires_at)))))
  OR (OLD.state='half_open' AND NEW.state IN('closed','open')
    AND unixepoch(NEW.updated_at)<unixepoch(OLD.half_open_lease_expires_at)
    AND NOT (
      (OLD.half_open_owner_type='operation' AND EXISTS(
        SELECT 1 FROM mcp_server_operations operation
        JOIN mcp_server_sync_jobs job
          ON job.tenant_id=operation.tenant_id
         AND job.mcp_server_id=operation.mcp_server_id
         AND job.mcp_server_operation_id=operation.mcp_server_operation_id
        WHERE operation.tenant_id=OLD.tenant_id
          AND operation.mcp_server_id=OLD.mcp_server_id
          AND operation.mcp_server_operation_id=OLD.half_open_owner_id
          AND operation.status='processing' AND job.status='processing'
          AND job.lease_generation=OLD.half_open_lease_generation
          AND job.lease_owner_id=OLD.half_open_lease_owner_id
          AND job.lease_token=OLD.half_open_lease_token
          AND job.lease_expires_at=OLD.half_open_lease_expires_at))
      OR (OLD.half_open_owner_type='invocation' AND EXISTS(
        SELECT 1 FROM mcp_invocations invocation
        WHERE invocation.tenant_id=OLD.tenant_id
          AND invocation.mcp_server_id=OLD.mcp_server_id
          AND invocation.mcp_invocation_id=OLD.half_open_owner_id
          AND invocation.status IN('probing','calling')
          AND invocation.lease_generation=OLD.half_open_lease_generation
          AND invocation.lease_owner_id=OLD.half_open_lease_owner_id
          AND invocation.lease_token=OLD.half_open_lease_token
          AND invocation.lease_expires_at=OLD.half_open_lease_expires_at))))
  OR NOT ((OLD.state='closed' AND NEW.state IN('closed','open'))
    OR (OLD.state='open' AND NEW.state IN('open','half_open'))
    OR (OLD.state='half_open' AND NEW.state IN('closed','open')))
BEGIN SELECT RAISE(ABORT,'mcp_network_circuit_transition_invalid'); END;
CREATE TRIGGER trg_mcp_network_circuits_delete_prd083
BEFORE DELETE ON mcp_network_circuits FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_circuit_immutable'); END;

CREATE TABLE mcp_network_events(
  mcp_network_event_id TEXT PRIMARY KEY CHECK(length(mcp_network_event_id)=38
    AND substr(mcp_network_event_id,1,6)='mcpne_'
    AND substr(mcp_network_event_id,7) NOT GLOB '*[^0-9a-f]*'),
  mcp_network_decision_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  mcp_server_id TEXT NOT NULL,
  mcp_server_operation_id TEXT,
  mcp_invocation_id TEXT,
  phase TEXT NOT NULL CHECK(phase IN('sync','probe','call_preflight')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'policy_disabled','address_denied','mixed_dns_answers',
    'port_denied','redirect_denied')),
  policy_revision INTEGER NOT NULL CHECK(typeof(policy_revision)='integer'
    AND policy_revision>=1),
  address_count INTEGER NOT NULL CHECK(typeof(address_count)='integer'
    AND address_count BETWEEN 0 AND 16),
  attempt_number INTEGER NOT NULL CHECK(typeof(attempt_number)='integer'
    AND attempt_number BETWEEN 1 AND 10),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_network_decision_id)
    REFERENCES mcp_network_decisions(tenant_id,mcp_network_decision_id),
  FOREIGN KEY(tenant_id,mcp_server_operation_id)
    REFERENCES mcp_server_operations(tenant_id,mcp_server_operation_id),
  FOREIGN KEY(tenant_id,mcp_invocation_id)
    REFERENCES mcp_invocations(tenant_id,mcp_invocation_id),
  UNIQUE(tenant_id,mcp_network_event_id),
  UNIQUE(tenant_id,mcp_network_decision_id),
  CHECK((mcp_server_operation_id IS NULL)!=(mcp_invocation_id IS NULL))
);
CREATE INDEX idx_mcp_network_events_tenant_time_prd083
  ON mcp_network_events(tenant_id,occurred_at DESC,mcp_network_event_id);
CREATE TRIGGER trg_mcp_network_events_owner_insert_prd083
BEFORE INSERT ON mcp_network_events FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM mcp_network_decisions decision
    WHERE decision.tenant_id=NEW.tenant_id
      AND decision.mcp_network_decision_id=NEW.mcp_network_decision_id
      AND decision.mcp_server_id=NEW.mcp_server_id
      AND decision.mcp_server_operation_id IS NEW.mcp_server_operation_id
      AND decision.mcp_invocation_id IS NEW.mcp_invocation_id
      AND decision.phase=NEW.phase AND decision.result='blocked'
      AND decision.reason_code=NEW.reason_code
      AND decision.policy_revision=NEW.policy_revision
      AND decision.address_count=NEW.address_count
      AND decision.attempt_number=NEW.attempt_number
      AND decision.request_id=NEW.request_id AND decision.trace_id=NEW.trace_id
      AND decision.created_at=NEW.occurred_at)
  OR (NEW.mcp_invocation_id IS NOT NULL AND (
    EXISTS(SELECT 1 FROM mcp_network_call_intents intent
      WHERE intent.tenant_id=NEW.tenant_id
        AND intent.mcp_invocation_id=NEW.mcp_invocation_id)
    OR EXISTS(SELECT 1 FROM mcp_invocation_events event
      WHERE event.tenant_id=NEW.tenant_id
        AND event.mcp_invocation_id=NEW.mcp_invocation_id)
    OR NOT EXISTS(SELECT 1 FROM mcp_invocations invocation
      WHERE invocation.tenant_id=NEW.tenant_id
        AND invocation.mcp_invocation_id=NEW.mcp_invocation_id
        AND invocation.status='blocked'
        AND invocation.error_code='mcp.network_blocked'
        AND invocation.execution_status='not_called'
        AND invocation.tool_call_count=0
        AND invocation.cost_status='not_applicable')))
  OR (NEW.mcp_server_operation_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM mcp_server_operations operation
    WHERE operation.tenant_id=NEW.tenant_id
      AND operation.mcp_server_operation_id=NEW.mcp_server_operation_id
      AND operation.status='failed' AND operation.error_code='mcp.network_blocked'))
  OR EXISTS(SELECT 1 FROM mcp_network_events current
    WHERE current.mcp_network_event_id=NEW.mcp_network_event_id
      OR (current.tenant_id=NEW.tenant_id
        AND current.mcp_network_decision_id=NEW.mcp_network_decision_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_network_event_owner_mismatch'); END;

-- EVT-094 tool-called and EVT-095 network-blocked are mutually exclusive facts.
-- Keep the original PRD-082 owner trigger intact and add symmetric guards that
-- can be removed exactly by Down without rewriting the frozen 0075 trigger.
CREATE TRIGGER trg_mcp_network_call_intents_event_exclusion_prd083
BEFORE INSERT ON mcp_network_call_intents FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM mcp_network_events event
  WHERE event.tenant_id=NEW.tenant_id
    AND event.mcp_invocation_id=NEW.mcp_invocation_id)
BEGIN SELECT RAISE(ABORT,'mcp_network_call_intent_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_events_network_exclusion_prd083
BEFORE INSERT ON mcp_invocation_events FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM mcp_network_events event
  WHERE event.tenant_id=NEW.tenant_id
    AND event.mcp_invocation_id=NEW.mcp_invocation_id)
BEGIN SELECT RAISE(ABORT,'mcp_event_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_network_events_update_prd083
BEFORE UPDATE ON mcp_network_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_event_immutable'); END;
CREATE TRIGGER trg_mcp_network_events_delete_prd083
BEFORE DELETE ON mcp_network_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_event_immutable'); END;

CREATE TABLE mcp_network_event_outbox(
  mcp_network_event_outbox_id TEXT PRIMARY KEY
    CHECK(length(mcp_network_event_outbox_id)=38
      AND substr(mcp_network_event_outbox_id,1,6)='mcpno_'
      AND substr(mcp_network_event_outbox_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  mcp_network_event_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','processing','succeeded','failed')),
  attempt_count INTEGER NOT NULL CHECK(typeof(attempt_count)='integer'
    AND attempt_count BETWEEN 0 AND 10),
  lease_generation INTEGER NOT NULL CHECK(typeof(lease_generation)='integer'
    AND lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL
    OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL
    OR unixepoch(next_attempt_at) IS NOT NULL),
  subscription_snapshot_json TEXT CHECK(subscription_snapshot_json IS NULL OR(
    json_valid(subscription_snapshot_json)
    AND json_type(subscription_snapshot_json)='array'
    AND length(CAST(subscription_snapshot_json AS BLOB))<=262144)),
  snapshot_frozen_at TEXT CHECK(snapshot_frozen_at IS NULL
    OR unixepoch(snapshot_frozen_at) IS NOT NULL),
  error_code TEXT CHECK(error_code IS NULL OR error_code IN(
    'webhook.payload_invalid','webhook.delivery_unavailable','mcp.unavailable')),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,mcp_network_event_id)
    REFERENCES mcp_network_events(tenant_id,mcp_network_event_id),
  UNIQUE(tenant_id,mcp_network_event_outbox_id),
  UNIQUE(tenant_id,mcp_network_event_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((subscription_snapshot_json IS NULL AND snapshot_frozen_at IS NULL)
    OR (subscription_snapshot_json IS NOT NULL AND snapshot_frozen_at IS NOT NULL)),
  CHECK((status='pending' AND attempt_count BETWEEN 0 AND 9
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NOT NULL AND delivered_at IS NULL)
    OR (status='processing' AND attempt_count BETWEEN 1 AND 10
      AND lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 1 AND 160
      AND lease_token IS NOT NULL AND length(lease_token)=64
      AND lease_token NOT GLOB '*[^0-9a-f]*' AND lease_expires_at IS NOT NULL
      AND next_attempt_at IS NULL AND delivered_at IS NULL AND error_code IS NULL)
    OR (status='succeeded' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NULL
      AND subscription_snapshot_json IS NOT NULL AND snapshot_frozen_at IS NOT NULL
      AND delivered_at IS NOT NULL AND error_code IS NULL)
    OR (status='failed' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NULL
      AND delivered_at IS NULL AND error_code IS NOT NULL))
);
CREATE INDEX idx_mcp_network_event_outbox_due_prd083
  ON mcp_network_event_outbox(
    status,next_attempt_at,lease_expires_at,tenant_id,mcp_network_event_outbox_id)
  WHERE status IN('pending','processing');
CREATE TRIGGER trg_mcp_network_event_outbox_owner_insert_prd083
BEFORE INSERT ON mcp_network_event_outbox FOR EACH ROW
WHEN NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.lease_generation!=0
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_token IS NOT NULL
  OR NEW.lease_expires_at IS NOT NULL OR NEW.next_attempt_at!=NEW.created_at
  OR NEW.error_code IS NOT NULL OR NEW.delivered_at IS NOT NULL
  OR NEW.created_at!=NEW.updated_at
  OR (NEW.subscription_snapshot_json IS NOT NULL
    AND NEW.snapshot_frozen_at!=NEW.created_at)
  OR NOT EXISTS(SELECT 1 FROM mcp_network_events event
    WHERE event.tenant_id=NEW.tenant_id
      AND event.mcp_network_event_id=NEW.mcp_network_event_id
      AND event.occurred_at=NEW.created_at)
  OR EXISTS(SELECT 1 FROM mcp_network_event_outbox current
    WHERE current.mcp_network_event_outbox_id=NEW.mcp_network_event_outbox_id
      OR (current.tenant_id=NEW.tenant_id
        AND current.mcp_network_event_id=NEW.mcp_network_event_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'mcp_network_outbox_owner_mismatch'); END;
CREATE TRIGGER trg_mcp_network_event_outbox_transition_prd083
BEFORE UPDATE ON mcp_network_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.mcp_network_event_outbox_id!=OLD.mcp_network_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.mcp_network_event_id!=OLD.mcp_network_event_id
  OR NEW.created_at!=OLD.created_at OR NEW.lease_generation!=OLD.lease_generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR (OLD.subscription_snapshot_json IS NOT NULL
    AND (NEW.subscription_snapshot_json IS NOT OLD.subscription_snapshot_json
      OR NEW.snapshot_frozen_at IS NOT OLD.snapshot_frozen_at))
  OR NOT ((OLD.status='pending' AND NEW.status='processing'
      AND NEW.attempt_count=OLD.attempt_count+1
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.next_attempt_at)
      AND NEW.subscription_snapshot_json IS OLD.subscription_snapshot_json
      AND NEW.snapshot_frozen_at IS OLD.snapshot_frozen_at)
    OR (OLD.status='processing' AND NEW.status='processing'
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count+1 AND NEW.lease_token!=OLD.lease_token
      AND NEW.subscription_snapshot_json IS OLD.subscription_snapshot_json
      AND NEW.snapshot_frozen_at IS OLD.snapshot_frozen_at)
    OR (OLD.status='processing' AND NEW.status='processing'
      AND unixepoch(NEW.updated_at)<unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.lease_owner_id=OLD.lease_owner_id AND NEW.lease_token!=OLD.lease_token
      AND unixepoch(NEW.lease_expires_at)>=unixepoch(OLD.lease_expires_at)
      AND ((OLD.subscription_snapshot_json IS NULL
          AND NEW.subscription_snapshot_json IS NOT NULL
          AND NEW.snapshot_frozen_at=NEW.updated_at)
        OR (NEW.subscription_snapshot_json IS OLD.subscription_snapshot_json
          AND NEW.snapshot_frozen_at IS OLD.snapshot_frozen_at)))
    OR (OLD.status='processing' AND NEW.status='pending'
      AND unixepoch(NEW.updated_at)<unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count AND NEW.attempt_count<10
      AND unixepoch(NEW.next_attempt_at)>=unixepoch(NEW.updated_at)
      AND NEW.error_code IS NULL
      AND NEW.subscription_snapshot_json IS OLD.subscription_snapshot_json
      AND NEW.snapshot_frozen_at IS OLD.snapshot_frozen_at)
    OR (OLD.status='processing' AND NEW.status IN('succeeded','failed')
      AND unixepoch(NEW.updated_at)<unixepoch(OLD.lease_expires_at)
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.subscription_snapshot_json IS OLD.subscription_snapshot_json
      AND NEW.snapshot_frozen_at IS OLD.snapshot_frozen_at)
    OR (OLD.status='processing' AND NEW.status='failed'
      AND OLD.attempt_count=10 AND NEW.attempt_count=OLD.attempt_count
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.lease_expires_at)
      AND NEW.error_code='webhook.delivery_unavailable'
      AND NEW.subscription_snapshot_json IS OLD.subscription_snapshot_json
      AND NEW.snapshot_frozen_at IS OLD.snapshot_frozen_at))
BEGIN SELECT RAISE(ABORT,'mcp_network_outbox_transition_invalid'); END;
CREATE TRIGGER trg_mcp_network_event_outbox_delete_prd083
BEFORE DELETE ON mcp_network_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_outbox_immutable'); END;
CREATE TABLE audit_logs_prd083_new (
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
      'experiment.created',
      'session_risk.freeze_session','session_risk.freeze_virtual_key',
      'session_risk.dismiss','session_risk.release_session',
      'session_risk.release_virtual_key',
      'response_cache.policy_created','response_cache.policy_updated',
      'response_cache.invalidated',
      'admin_assistant.queried',
      'plan.created','plan.updated','quota.adjustment_created',
      'billing_dispute.created','billing_dispute.review_started',
      'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled',
      'mcp.server_registered','mcp.server_refreshed',
      'mcp.server_credential_rotated','mcp.server_updated',
      'mcp.server_disabled','mcp.server_enabled',
      'mcp.server_operation_completed','mcp.tool_refreshed',
      'mcp.tool_policy_updated','mcp.tool_grants_updated',
      'mcp.network_policy_updated'
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
      'policy_template_application','session_risk','experiment',
      'response_cache_policy','response_cache_invalidation',
      'assistant_query','plan','quota_adjustment','billing_dispute',
      'mcp_server','mcp_server_operation','mcp_tool','mcp_network_policy'
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
INSERT INTO audit_logs_prd083_new(
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
  object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
  client_ip,user_agent,request_id,trace_id,created_at
)
SELECT rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
  object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
  client_ip,user_agent,request_id,trace_id,created_at FROM audit_logs;
CREATE TEMP TABLE prd083_audit_copy_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd083_audit_copy_guard(issue_count)
SELECT ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd083_new))
  +(SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd083_new))
  +(SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs_prd083_new
      EXCEPT SELECT rowid,* FROM audit_logs));
DROP TABLE prd083_audit_copy_guard;
DROP TABLE audit_logs;
ALTER TABLE audit_logs_prd083_new RENAME TO audit_logs;
CREATE UNIQUE INDEX idx_audit_logs_admin_assistant_owner_prd077
  ON audit_logs(tenant_id,object_id)
  WHERE action='admin_assistant.queried';
CREATE UNIQUE INDEX idx_audit_logs_billing_dispute_transition_prd080
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN('billing_dispute.created','billing_dispute.review_started',
    'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled');
CREATE UNIQUE INDEX idx_audit_logs_config_transfer_request_unique
  ON audit_logs(tenant_id,actor_user_id,action,request_id)
  WHERE action IN ('config.imported','config.exported');
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_experiment_owner_unique
  ON audit_logs(tenant_id,object_id) WHERE action='experiment.created';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id,object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_invitation_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action IN ('user.invited','invitation.accepted');
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_plan_request_prd079
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN('plan.created','plan.updated');
CREATE UNIQUE INDEX idx_audit_logs_policy_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN ('policy_template.applied','policy_template.rolled_back');
CREATE UNIQUE INDEX idx_audit_logs_prompt_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN (
    'prompt_template.created','prompt_template.updated',
    'prompt_template.version_published'
  );
CREATE UNIQUE INDEX idx_audit_logs_quota_adjustment_prd079
  ON audit_logs(tenant_id,object_id)
  WHERE action='quota.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_response_cache_owner_prd076
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN (
    'response_cache.policy_created','response_cache.policy_updated',
    'response_cache.invalidated'
  );
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';
CREATE UNIQUE INDEX idx_audit_logs_session_risk_action_unique_prd070
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE action IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  );
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id,action,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id,actor_user_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id,object_type,object_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id,request_id);
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id,created_at DESC,audit_log_id DESC);
CREATE UNIQUE INDEX uq_audit_logs_owner_prd068
  ON audit_logs(tenant_id,audit_log_id);
CREATE TRIGGER trg_audit_logs_admin_assistant_owner_insert_prd077
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action='admin_assistant.queried'
  AND (
    NEW.object_type!='assistant_query' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1
      FROM assistant_audits assistant_audit
      JOIN assistant_queries query
        ON query.tenant_id=assistant_audit.tenant_id
       AND query.assistant_query_id=assistant_audit.assistant_query_id
      JOIN users actor
        ON actor.tenant_id=assistant_audit.tenant_id
       AND actor.user_id=assistant_audit.actor_user_id
      WHERE assistant_audit.tenant_id=NEW.tenant_id
        AND assistant_audit.assistant_query_id=NEW.object_id
        AND assistant_audit.audit_log_id=NEW.audit_log_id
        AND assistant_audit.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND assistant_audit.operation_request_id=NEW.request_id
        AND assistant_audit.operation_trace_id=NEW.trace_id
        AND assistant_audit.created_at=NEW.created_at
        AND query.query_type=json_extract(NEW.after_summary_json,'$.query_type')
        AND query.outcome=json_extract(NEW.after_summary_json,'$.outcome')
        AND query.claim_count=json_extract(NEW.after_summary_json,'$.claim_count')
        AND query.source_count=json_extract(NEW.after_summary_json,'$.source_count')
        AND query.redaction_count
            =json_extract(NEW.after_summary_json,'$.redaction_count')
        AND query.execution_mode
            =json_extract(NEW.after_summary_json,'$.execution_mode')
        AND query.model_call_count
            =json_extract(NEW.after_summary_json,'$.model_call_count')
        AND query.model_cost_status
            =json_extract(NEW.after_summary_json,'$.model_cost_status')
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'query_type','outcome','claim_count','source_count',
            'redaction_count','execution_mode','model_call_count',
            'model_cost_status'
          )
        )
        AND json_type(NEW.after_summary_json,'$.query_type')='text'
        AND json_type(NEW.after_summary_json,'$.outcome')='text'
        AND json_type(NEW.after_summary_json,'$.claim_count')='integer'
        AND json_type(NEW.after_summary_json,'$.source_count')='integer'
        AND json_type(NEW.after_summary_json,'$.redaction_count')='integer'
        AND json_type(NEW.after_summary_json,'$.execution_mode')='text'
        AND json_type(NEW.after_summary_json,'$.model_call_count')='integer'
        AND json_type(NEW.after_summary_json,'$.model_cost_status')='text'
        AND json(NEW.changed_fields_json)=json(
          '["claim_count","execution_mode","model_call_count",' ||
          '"model_cost_status","outcome","query_type","redaction_count",' ||
          '"source_count"]'
        )
    )
  )
)
OR (
  NEW.object_type='assistant_query'
  AND NEW.action!='admin_assistant.queried'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_billing_dispute_owner_insert_prd080
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN('billing_dispute.created','billing_dispute.review_started',
    'billing_dispute.approved','billing_dispute.rejected','billing_dispute.cancelled')
  AND (
    NEW.object_type!='billing_dispute' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS(
      SELECT 1 FROM billing_disputes dispute
      JOIN billing_dispute_transitions transition
        ON transition.tenant_id=dispute.tenant_id
       AND transition.billing_dispute_id=dispute.billing_dispute_id
      WHERE dispute.tenant_id=NEW.tenant_id AND dispute.billing_dispute_id=NEW.object_id
        AND transition.action=CASE NEW.action
          WHEN 'billing_dispute.created' THEN 'create'
          WHEN 'billing_dispute.review_started' THEN 'start_review'
          WHEN 'billing_dispute.approved' THEN 'approve'
          WHEN 'billing_dispute.rejected' THEN 'reject' ELSE 'cancel' END
        AND transition.actor_user_id=NEW.actor_user_id
        AND transition.operation_request_id=NEW.request_id
        AND transition.operation_trace_id=NEW.trace_id
        AND transition.to_status=dispute.status
        AND transition.new_revision=dispute.state_revision
        AND ((transition.action='create' AND NEW.before_summary_json='{}')
          OR (transition.action!='create'
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=7
            AND NOT EXISTS(SELECT 1 FROM json_each(NEW.before_summary_json) field
              WHERE field.key NOT IN('status','state_revision','scope_type','item_count',
                'adjustment_count','currency_totals_sha256','reason_sha256'))
            AND json_extract(NEW.before_summary_json,'$.status')=transition.from_status
            AND json_extract(NEW.before_summary_json,'$.state_revision')
              =transition.expected_revision
            AND json_extract(NEW.before_summary_json,'$.scope_type')=dispute.scope_type
            AND json_extract(NEW.before_summary_json,'$.item_count')=dispute.item_count
            AND json_extract(NEW.before_summary_json,'$.adjustment_count')=0
            AND json_extract(NEW.before_summary_json,'$.currency_totals_sha256')
              =metahub_sha256('{}')
            AND json_extract(NEW.before_summary_json,'$.reason_sha256')
              =dispute.reason_sha256))
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=7
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN('status','state_revision','scope_type','item_count',
            'adjustment_count','currency_totals_sha256','reason_sha256'))
        AND json_extract(NEW.after_summary_json,'$.status')=dispute.status
        AND json_extract(NEW.after_summary_json,'$.state_revision')=dispute.state_revision
        AND json_extract(NEW.after_summary_json,'$.scope_type')=dispute.scope_type
        AND json_extract(NEW.after_summary_json,'$.item_count')=dispute.item_count
        AND json_extract(NEW.after_summary_json,'$.adjustment_count')
          =transition.approved_item_count
        AND json_extract(NEW.after_summary_json,'$.currency_totals_sha256')=
          CASE transition.action
            WHEN 'create' THEN (
              SELECT metahub_sha256(json_extract(event.payload_json,'$.currency_totals'))
              FROM billing_dispute_events event
              WHERE event.tenant_id=dispute.tenant_id
                AND event.billing_dispute_id=dispute.billing_dispute_id
                AND event.event_type='billing_dispute.created')
            WHEN 'approve' THEN transition.approved_totals_sha256
            ELSE metahub_sha256('{}')
          END
        AND json_extract(NEW.after_summary_json,'$.reason_sha256')=dispute.reason_sha256
        AND NEW.changed_fields_json=CASE transition.action
          WHEN 'create' THEN
            '["adjustment_count","currency_totals_sha256","item_count","reason_sha256","scope_type","state_revision","status"]'
          WHEN 'approve' THEN
            '["adjustment_count","currency_totals_sha256","state_revision","status"]'
          ELSE '["state_revision","status"]' END
        AND (SELECT COUNT(*) FROM billing_dispute_adjustment_links link
          WHERE link.tenant_id=dispute.tenant_id
            AND link.billing_dispute_id=dispute.billing_dispute_id
            AND link.billing_dispute_transition_id=transition.billing_dispute_transition_id)
          =transition.approved_item_count
        AND ((transition.action IN('approve','reject','cancel')
            AND NOT EXISTS(SELECT 1 FROM billing_settlement_holds hold
              WHERE hold.tenant_id=dispute.tenant_id
                AND hold.billing_dispute_id=dispute.billing_dispute_id
                AND hold.status='active'))
          OR (transition.action IN('create','start_review')
            AND (SELECT COUNT(*) FROM billing_settlement_holds hold
              WHERE hold.tenant_id=dispute.tenant_id
                AND hold.billing_dispute_id=dispute.billing_dispute_id
                AND hold.status='active')=dispute.item_count))
    )
  )
)
OR (NEW.object_type='billing_dispute' AND NEW.action NOT IN(
  'billing_dispute.created','billing_dispute.review_started','billing_dispute.approved',
  'billing_dispute.rejected','billing_dispute.cancelled'))
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 每条新审计事实都登记导出序列，配置迁移审计不得绕过既有增量导出链。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
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
OR (
  NEW.action='experiment.created'
  AND (
    NEW.object_type!='experiment' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM model_experiments experiment
      JOIN users actor
        ON actor.tenant_id=experiment.tenant_id
       AND actor.user_id=experiment.created_by
      WHERE experiment.tenant_id=NEW.tenant_id
        AND experiment.experiment_id=NEW.object_id
        AND experiment.created_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND experiment.operation_request_id=NEW.request_id
        AND experiment.operation_trace_id=NEW.trace_id
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=12
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'experiment_id','project_id','environment_id','model_alias_id',
            'model_a_id','model_b_id','target_sample_count','traffic_sample_bps',
            'model_a_allocation_bps','sampling_duration_seconds',
            'feedback_collection_enabled','feedback_window_seconds'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.experiment_id')
          =experiment.experiment_id
        AND json_extract(NEW.after_summary_json,'$.project_id')=experiment.project_id
        AND json_extract(NEW.after_summary_json,'$.environment_id')
          =experiment.environment_id
        AND json_extract(NEW.after_summary_json,'$.model_alias_id')
          =experiment.model_alias_id
        AND json_extract(NEW.after_summary_json,'$.model_a_id')=experiment.model_a_id
        AND json_extract(NEW.after_summary_json,'$.model_b_id')=experiment.model_b_id
        AND json_extract(NEW.after_summary_json,'$.target_sample_count')
          =experiment.target_sample_count
        AND json_extract(NEW.after_summary_json,'$.traffic_sample_bps')
          =experiment.traffic_sample_bps
        AND json_extract(NEW.after_summary_json,'$.model_a_allocation_bps')
          =experiment.model_a_allocation_bps
        AND json_extract(NEW.after_summary_json,'$.sampling_duration_seconds')
          =experiment.sampling_duration_seconds
        AND json_extract(NEW.after_summary_json,'$.feedback_collection_enabled')
          =experiment.feedback_collection_enabled
        AND json_extract(NEW.after_summary_json,'$.feedback_window_seconds')
          =experiment.feedback_window_seconds
        AND json(NEW.changed_fields_json)=json(
          '["environment_id","experiment_id","feedback_collection_enabled",' ||
          '"feedback_window_seconds","model_a_allocation_bps","model_a_id",' ||
          '"model_alias_id","model_b_id","project_id","sampling_duration_seconds",' ||
          '"target_sample_count","traffic_sample_bps"]'
        )
    )
  )
)
OR (NEW.object_type='experiment' AND NEW.action!='experiment.created')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
CREATE TRIGGER trg_audit_logs_plan_billing_owner_insert_prd079
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN('plan.created','plan.updated')
  AND (
    NEW.object_type!='plan'
    OR NOT EXISTS(
      SELECT 1 FROM plans plan
      WHERE plan.tenant_id=NEW.tenant_id AND plan.plan_id=NEW.object_id
        AND plan.display_name=NEW.object_name
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=9
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'display_name','base_tier','status','trial_status','trial_started_at',
            'trial_ends_at','trial_cycle','state_revision','quota_generation'))
        AND json_extract(NEW.after_summary_json,'$.display_name')=plan.display_name
        AND json_extract(NEW.after_summary_json,'$.base_tier')=plan.base_tier
        AND json_extract(NEW.after_summary_json,'$.status')=plan.status
        AND json_extract(NEW.after_summary_json,'$.trial_status')=plan.trial_status
        AND json_extract(NEW.after_summary_json,'$.trial_started_at') IS plan.trial_started_at
        AND json_extract(NEW.after_summary_json,'$.trial_ends_at') IS plan.trial_ends_at
        AND json_extract(NEW.after_summary_json,'$.trial_cycle')=plan.trial_cycle
        AND json_extract(NEW.after_summary_json,'$.state_revision')=plan.state_revision
        AND json_extract(NEW.after_summary_json,'$.quota_generation')=plan.quota_generation
        AND (
          (NEW.action='plan.created' AND NEW.before_summary_json='{}'
            AND plan.state_revision=1)
          OR (NEW.action='plan.updated'
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=9
            AND json_extract(NEW.before_summary_json,'$.state_revision')+1
              =plan.state_revision)
        )
    )
  )
)
OR (NEW.object_type='plan' AND NEW.action NOT IN('plan.created','plan.updated'))
OR (
  NEW.action='quota.adjustment_created'
  AND (
    NEW.object_type!='quota_adjustment' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1 FROM quota_adjustments adjustment
      WHERE adjustment.tenant_id=NEW.tenant_id
        AND adjustment.quota_adjustment_id=NEW.object_id
        AND adjustment.actor_user_id=NEW.actor_user_id
        AND adjustment.operation_request_id=NEW.request_id
        AND adjustment.operation_trace_id=NEW.trace_id
        AND adjustment.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'plan_id','quota_bucket_id','quota_type','direction','amount',
            'expected_bucket_revision','resulting_bucket_revision','reason_sha256'))
        AND json_extract(NEW.after_summary_json,'$.plan_id')=adjustment.plan_id
        AND json_extract(NEW.after_summary_json,'$.quota_bucket_id')
          =adjustment.quota_bucket_id
        AND json_extract(NEW.after_summary_json,'$.quota_type')=adjustment.quota_type
        AND json_extract(NEW.after_summary_json,'$.direction')=adjustment.direction
        AND json_extract(NEW.after_summary_json,'$.amount')=
          printf('%d.%06d',adjustment.amount_micros/1000000,
            adjustment.amount_micros%1000000)
        AND json_extract(NEW.after_summary_json,'$.expected_bucket_revision')
          =adjustment.expected_bucket_revision
        AND json_extract(NEW.after_summary_json,'$.resulting_bucket_revision')
          =adjustment.resulting_bucket_revision
        AND json_extract(NEW.after_summary_json,'$.reason_sha256')
          =metahub_sha256(adjustment.reason)
    )
  )
)
OR (NEW.object_type='quota_adjustment' AND NEW.action!='quota.adjustment_created')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
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
    OR (
      NEW.action='experiment.created' AND current.action=NEW.action
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_response_cache_owner_insert_prd076
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN (
    'response_cache.policy_created','response_cache.policy_updated'
  )
  AND (
    NEW.object_type!='response_cache_policy' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS(
      SELECT 1 FROM response_cache_policy_events event
      JOIN response_cache_policies policy
        ON policy.tenant_id=event.tenant_id
       AND policy.cache_policy_id=event.cache_policy_id
      JOIN users actor
        ON actor.tenant_id=event.tenant_id
       AND actor.user_id=event.actor_user_id
      WHERE event.tenant_id=NEW.tenant_id
        AND event.cache_policy_id=NEW.object_id
        AND event.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND event.operation_request_id=NEW.request_id
        AND event.operation_trace_id=NEW.trace_id
        AND event.created_at=NEW.created_at
        AND event.event_type=CASE NEW.action
          WHEN 'response_cache.policy_created' THEN 'policy_created'
          ELSE 'policy_updated' END
        AND event.before_json=NEW.before_summary_json
        AND event.after_json=NEW.after_summary_json
        AND event.changed=1
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN(
            'tenant_id','cache_policy_id','model_alias_id','status','data_class',
            'ttl_seconds','charge_cache_hits','tags','version',
            'invalidation_generation'
          )
        )
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'tenant_id','cache_policy_id','model_alias_id','status','data_class',
            'ttl_seconds','charge_cache_hits','tags','version',
            'invalidation_generation'
          )
        )
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.type!='text' OR field.value NOT IN(
            'status','data_class','ttl_seconds','charge_cache_hits','tags',
            'version','invalidation_generation'
          )
        )
      )
  )
)
OR (
  NEW.action='response_cache.invalidated'
  AND (
    NEW.object_type!='response_cache_invalidation' OR NEW.object_name IS NOT NULL
    OR NEW.before_summary_json!='{}'
    OR NOT EXISTS(
      SELECT 1 FROM response_cache_invalidations invalidation
      JOIN users actor
        ON actor.tenant_id=invalidation.tenant_id
       AND actor.user_id=invalidation.actor_user_id
      WHERE invalidation.tenant_id=NEW.tenant_id
        AND invalidation.cache_invalidation_id=NEW.object_id
        AND invalidation.actor_type='user'
        AND invalidation.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND invalidation.operation_request_id=NEW.request_id
        AND invalidation.operation_trace_id=NEW.trace_id
        AND invalidation.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=8
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN(
            'cache_invalidation_id','scope','match_mode','tag_count',
            'matched_entry_count','deleted_entry_count','fenced_fill_count',
            'reason_code'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.cache_invalidation_id')
            =invalidation.cache_invalidation_id
        AND json_extract(NEW.after_summary_json,'$.scope')=invalidation.scope
        AND json_extract(NEW.after_summary_json,'$.match_mode')
            =invalidation.match_mode
        AND json_extract(NEW.after_summary_json,'$.tag_count')
            =invalidation.tag_count
        AND json_extract(NEW.after_summary_json,'$.matched_entry_count')
            =invalidation.matched_entry_count
        AND json_extract(NEW.after_summary_json,'$.deleted_entry_count')
            =invalidation.deleted_entry_count
        AND json_extract(NEW.after_summary_json,'$.fenced_fill_count')
            =invalidation.fenced_fill_count
        AND json_extract(NEW.after_summary_json,'$.reason_code')
            =invalidation.reason_code
      )
  )
)
OR (
  NEW.object_type='response_cache_policy'
  AND NEW.action NOT IN(
    'response_cache.policy_created','response_cache.policy_updated'
  )
)
OR (
  NEW.object_type='response_cache_invalidation'
  AND NEW.action!='response_cache.invalidated'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;
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

CREATE UNIQUE INDEX idx_audit_logs_mcp_operation_prd082
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE object_type='mcp_server_operation';
CREATE TRIGGER trg_audit_logs_mcp_owner_insert_prd082
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (NEW.action LIKE 'mcp.%' AND NEW.action!='mcp.network_policy_updated' AND (
    NEW.object_name IS NOT NULL
    OR (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))
      !=(SELECT COUNT(DISTINCT key) FROM json_each(NEW.before_summary_json))
    OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))
      !=(SELECT COUNT(DISTINCT key) FROM json_each(NEW.after_summary_json))
    OR (SELECT COUNT(*) FROM json_each(NEW.changed_fields_json))
      !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.changed_fields_json))
    OR EXISTS(SELECT 1 FROM json_each(NEW.before_summary_json) field
      WHERE (NEW.object_type='mcp_server' AND field.key NOT IN(
          'mcp_server_id','status','state_revision','active_credential_revision',
          'tool_count','active_tool_count'))
        OR (NEW.object_type='mcp_server_operation' AND field.key NOT IN(
          'mcp_server_operation_id','mcp_server_id','operation_type','status',
          'start_server_revision','candidate_credential_revision','error_code'))
        OR (NEW.object_type='mcp_tool' AND field.key NOT IN(
          'mcp_tool_id','mcp_server_id','status','schema_revision','policy_revision',
          'risk_level','approval_required','required_permission_count',
          'role_grant_count','user_grant_count','redaction_count')))
    OR EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
      WHERE (NEW.object_type='mcp_server' AND field.key NOT IN(
          'mcp_server_id','status','state_revision','active_credential_revision',
          'tool_count','active_tool_count'))
        OR (NEW.object_type='mcp_server_operation' AND field.key NOT IN(
          'mcp_server_operation_id','mcp_server_id','operation_type','status',
          'start_server_revision','candidate_credential_revision','error_code'))
        OR (NEW.object_type='mcp_tool' AND field.key NOT IN(
          'mcp_tool_id','mcp_server_id','status','schema_revision','policy_revision',
          'risk_level','approval_required','required_permission_count',
          'role_grant_count','user_grant_count','redaction_count')))
    OR EXISTS(SELECT 1 FROM json_each(NEW.changed_fields_json) field
      WHERE field.type!='text' OR
        (NEW.object_type='mcp_server' AND field.value NOT IN(
          'mcp_server_id','status','state_revision','active_credential_revision',
          'tool_count','active_tool_count')) OR
        (NEW.object_type='mcp_server_operation' AND field.value NOT IN(
          'mcp_server_operation_id','mcp_server_id','operation_type','status',
          'start_server_revision','candidate_credential_revision','error_code')) OR
        (NEW.object_type='mcp_tool' AND field.value NOT IN(
          'mcp_tool_id','mcp_server_id','status','schema_revision','policy_revision',
          'risk_level','approval_required','required_permission_count',
          'role_grant_count','user_grant_count','redaction_count')))
    OR EXISTS(
      SELECT 1 FROM json_each(CASE NEW.object_type
        WHEN 'mcp_server' THEN
          '["mcp_server_id","status","state_revision","active_credential_revision",' ||
          '"tool_count","active_tool_count"]'
        WHEN 'mcp_server_operation' THEN
          '["mcp_server_operation_id","mcp_server_id","operation_type","status",' ||
          '"start_server_revision","candidate_credential_revision","error_code"]'
        WHEN 'mcp_tool' THEN
          '["mcp_tool_id","mcp_server_id","status","schema_revision",' ||
          '"policy_revision","risk_level","approval_required",' ||
          '"required_permission_count","role_grant_count","user_grant_count",' ||
          '"redaction_count"]'
        ELSE '[]' END) expected
      WHERE (json_extract(NEW.before_summary_json,'$.'||expected.value)
          IS NOT json_extract(NEW.after_summary_json,'$.'||expected.value))
        !=EXISTS(SELECT 1 FROM json_each(NEW.changed_fields_json) actual
          WHERE actual.value=expected.value)
    )
    OR (NEW.object_type='mcp_server' AND NOT EXISTS(
      SELECT 1 FROM mcp_servers server WHERE server.tenant_id=NEW.tenant_id
        AND server.mcp_server_id=NEW.object_id))
    OR (NEW.object_type='mcp_server_operation' AND NOT EXISTS(
      SELECT 1 FROM mcp_server_operations operation WHERE operation.tenant_id=NEW.tenant_id
        AND operation.mcp_server_operation_id=NEW.object_id))
    OR (NEW.object_type='mcp_tool' AND NOT EXISTS(
      SELECT 1 FROM mcp_tools tool WHERE tool.tenant_id=NEW.tenant_id
        AND tool.mcp_tool_id=NEW.object_id))
    OR (NEW.action IN('mcp.server_registered','mcp.server_refreshed',
        'mcp.server_credential_rotated','mcp.server_updated',
        'mcp.server_disabled','mcp.server_enabled')
      AND NEW.object_type!='mcp_server')
    OR (NEW.action='mcp.server_operation_completed'
      AND NEW.object_type!='mcp_server_operation')
    OR (NEW.action IN('mcp.tool_refreshed','mcp.tool_policy_updated',
        'mcp.tool_grants_updated') AND NEW.object_type!='mcp_tool')
  ))
  OR (NEW.object_type IN('mcp_server','mcp_server_operation','mcp_tool')
    AND NEW.action NOT LIKE 'mcp.%')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_mcp_operation_insert_prd082
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NEW.action='mcp.server_operation_completed' AND (
  (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=7
  OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=7
  OR NOT EXISTS(
    SELECT 1
    FROM mcp_server_operations operation
    JOIN users actor ON actor.tenant_id=operation.tenant_id
      AND actor.user_id=operation.actor_user_id
    WHERE operation.tenant_id=NEW.tenant_id
      AND operation.mcp_server_operation_id=NEW.object_id
      AND operation.status IN('succeeded','failed')
      AND (operation.status='failed' OR actor.status='active')
      AND NEW.actor_user_id=actor.user_id
      AND NEW.actor_email=actor.email
      AND NEW.actor_role=actor.role
      AND NEW.request_id=operation.request_id
      AND NEW.trace_id=operation.trace_id
      AND NEW.created_at=operation.completed_at
      AND json_type(NEW.before_summary_json,'$.mcp_server_operation_id')='text'
      AND json_extract(NEW.before_summary_json,'$.mcp_server_operation_id')
        =operation.mcp_server_operation_id
      AND json_type(NEW.before_summary_json,'$.mcp_server_id')='text'
      AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=operation.mcp_server_id
      AND json_type(NEW.before_summary_json,'$.operation_type')='text'
      AND json_extract(NEW.before_summary_json,'$.operation_type')=operation.operation_type
      AND json_type(NEW.before_summary_json,'$.status')='text'
      AND json_extract(NEW.before_summary_json,'$.status')='processing'
      AND json_type(NEW.before_summary_json,'$.start_server_revision')='integer'
      AND json_extract(NEW.before_summary_json,'$.start_server_revision')
        =operation.start_server_revision
      AND json_type(NEW.before_summary_json,'$.candidate_credential_revision')
        IN('integer','null')
      AND json_extract(NEW.before_summary_json,'$.candidate_credential_revision')
        IS operation.candidate_credential_revision
      AND json_type(NEW.before_summary_json,'$.error_code')='null'
      AND json_type(NEW.after_summary_json,'$.mcp_server_operation_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_server_operation_id')
        =operation.mcp_server_operation_id
      AND json_type(NEW.after_summary_json,'$.mcp_server_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_server_id')=operation.mcp_server_id
      AND json_type(NEW.after_summary_json,'$.operation_type')='text'
      AND json_extract(NEW.after_summary_json,'$.operation_type')=operation.operation_type
      AND json_type(NEW.after_summary_json,'$.status')='text'
      AND json_extract(NEW.after_summary_json,'$.status')=operation.status
      AND json_type(NEW.after_summary_json,'$.start_server_revision')='integer'
      AND json_extract(NEW.after_summary_json,'$.start_server_revision')
        =operation.start_server_revision
      AND json_type(NEW.after_summary_json,'$.candidate_credential_revision')
        IN('integer','null')
      AND json_extract(NEW.after_summary_json,'$.candidate_credential_revision')
        IS operation.candidate_credential_revision
      AND json_type(NEW.after_summary_json,'$.error_code')
        =CASE WHEN operation.error_code IS NULL THEN 'null' ELSE 'text' END
      AND json_extract(NEW.after_summary_json,'$.error_code') IS operation.error_code
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_mcp_server_insert_prd082
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NEW.action IN(
  'mcp.server_registered','mcp.server_refreshed','mcp.server_credential_rotated',
  'mcp.server_updated','mcp.server_disabled','mcp.server_enabled'
) AND (
  (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=6
  OR (NEW.action='mcp.server_registered'
    AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=0)
  OR (NEW.action!='mcp.server_registered'
    AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=6)
  OR NOT EXISTS(
    SELECT 1
    FROM mcp_servers server
    JOIN mcp_server_operations operation
      ON operation.tenant_id=server.tenant_id
     AND operation.mcp_server_id=server.mcp_server_id
    JOIN users actor ON actor.tenant_id=operation.tenant_id
      AND actor.user_id=operation.actor_user_id
    WHERE server.tenant_id=NEW.tenant_id
      AND server.mcp_server_id=NEW.object_id
      AND actor.status='active'
      AND NEW.actor_user_id=actor.user_id
      AND NEW.actor_email=actor.email
      AND NEW.actor_role=actor.role
      AND server.updated_by=operation.actor_user_id
      AND NEW.request_id=operation.request_id
      AND NEW.trace_id=operation.trace_id
      AND operation.operation_type=CASE NEW.action
        WHEN 'mcp.server_registered' THEN 'register'
        WHEN 'mcp.server_refreshed' THEN 'refresh'
        WHEN 'mcp.server_credential_rotated' THEN 'rotate'
        WHEN 'mcp.server_updated' THEN 'update'
        WHEN 'mcp.server_disabled' THEN 'disable'
        WHEN 'mcp.server_enabled' THEN 'enable' END
      AND json_type(NEW.after_summary_json,'$.mcp_server_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_server_id')=server.mcp_server_id
      AND json_type(NEW.after_summary_json,'$.status')='text'
      AND json_extract(NEW.after_summary_json,'$.status')=server.status
      AND json_type(NEW.after_summary_json,'$.state_revision')='integer'
      AND json_extract(NEW.after_summary_json,'$.state_revision')=server.state_revision
      AND json_type(NEW.after_summary_json,'$.active_credential_revision')
        IN('integer','null')
      AND json_extract(NEW.after_summary_json,'$.active_credential_revision')
        IS server.active_credential_revision
      AND json_type(NEW.after_summary_json,'$.tool_count')='integer'
      AND json_extract(NEW.after_summary_json,'$.tool_count')=(
        SELECT COUNT(*) FROM mcp_tools tool
        WHERE tool.tenant_id=server.tenant_id
          AND tool.mcp_server_id=server.mcp_server_id)
      AND json_type(NEW.after_summary_json,'$.active_tool_count')='integer'
      AND json_extract(NEW.after_summary_json,'$.active_tool_count')=(
        SELECT COUNT(*) FROM mcp_tools tool
        WHERE tool.tenant_id=server.tenant_id
          AND tool.mcp_server_id=server.mcp_server_id AND tool.status='active')
      AND (
        (NEW.action='mcp.server_registered'
          AND operation.status='queued'
          AND NEW.created_at=operation.created_at
          AND server.created_at=operation.created_at
          AND server.updated_at=operation.created_at
          AND server.created_by=operation.actor_user_id
          AND operation.start_server_revision=1
          AND server.state_revision=1
          AND server.status='pending_sync'
          AND server.active_credential_revision IS NULL
          AND operation.candidate_credential_revision=1)
        OR
        (NEW.action!='mcp.server_registered'
          AND operation.status='succeeded'
          AND NEW.created_at=operation.completed_at
          AND server.updated_at=operation.completed_at
          AND json_type(NEW.before_summary_json,'$.mcp_server_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=server.mcp_server_id
          AND json_type(NEW.before_summary_json,'$.status')='text'
          AND json_extract(NEW.before_summary_json,'$.status')
            IN('pending_sync','active','sync_failed','disabled')
          AND json_type(NEW.before_summary_json,'$.state_revision')='integer'
          AND json_extract(NEW.before_summary_json,'$.state_revision')
            =operation.start_server_revision
          AND server.state_revision=operation.start_server_revision+1
          AND json_type(NEW.before_summary_json,'$.active_credential_revision')
            IN('integer','null')
          AND json_extract(NEW.before_summary_json,'$.active_credential_revision')
            IS operation.start_credential_revision
          AND json_type(NEW.before_summary_json,'$.tool_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.tool_count')>=0
          AND json_type(NEW.before_summary_json,'$.active_tool_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.active_tool_count')>=0
          AND (NEW.action!='mcp.server_disabled' OR server.status='disabled')
          AND (NEW.action!='mcp.server_enabled' OR server.status='active')
          AND (NEW.action NOT IN('mcp.server_refreshed','mcp.server_credential_rotated')
            OR server.status='active')
          AND (NEW.action!='mcp.server_credential_rotated'
            OR server.active_credential_revision=operation.candidate_credential_revision)
          AND (NEW.action='mcp.server_credential_rotated'
            OR operation.candidate_credential_revision IS NULL)
          AND (NEW.action IN('mcp.server_credential_rotated','mcp.server_registered')
            OR server.active_credential_revision IS operation.start_credential_revision))
      )
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_mcp_tool_insert_prd082
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NEW.action IN(
  'mcp.tool_refreshed','mcp.tool_policy_updated','mcp.tool_grants_updated'
) AND (
  (NEW.action='mcp.tool_refreshed' AND (
    (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=11
    OR (SELECT COUNT(*) FROM json_each(NEW.before_summary_json)) NOT IN(0,11)))
  OR (NEW.action='mcp.tool_policy_updated' AND (
    (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=9
    OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=9))
  OR (NEW.action='mcp.tool_grants_updated' AND (
    (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=5
    OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=5))
  OR NOT EXISTS(
    SELECT 1
    FROM mcp_tools tool
    JOIN users actor ON actor.tenant_id=tool.tenant_id
      AND actor.user_id=tool.updated_by
    WHERE tool.tenant_id=NEW.tenant_id
      AND tool.mcp_tool_id=NEW.object_id
      AND actor.status='active'
      AND NEW.actor_user_id=actor.user_id
      AND NEW.actor_email=actor.email
      AND NEW.actor_role=actor.role
      AND NEW.created_at=tool.updated_at
      AND json_type(NEW.after_summary_json,'$.mcp_tool_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_tool_id')=tool.mcp_tool_id
      AND json_type(NEW.after_summary_json,'$.mcp_server_id')='text'
      AND json_extract(NEW.after_summary_json,'$.mcp_server_id')=tool.mcp_server_id
      AND json_type(NEW.after_summary_json,'$.policy_revision')='integer'
      AND json_extract(NEW.after_summary_json,'$.policy_revision')=tool.policy_revision
      AND (
        (NEW.action='mcp.tool_refreshed'
          AND json_type(NEW.after_summary_json,'$.status')='text'
          AND json_extract(NEW.after_summary_json,'$.status')=tool.status
          AND json_type(NEW.after_summary_json,'$.schema_revision')='integer'
          AND json_extract(NEW.after_summary_json,'$.schema_revision')=tool.schema_revision
          AND json_type(NEW.after_summary_json,'$.risk_level')='text'
          AND json_extract(NEW.after_summary_json,'$.risk_level')=tool.risk_level
          AND json_type(NEW.after_summary_json,'$.approval_required') IN('true','false')
          AND json_extract(NEW.after_summary_json,'$.approval_required')
            =tool.approval_required
          AND json_type(NEW.after_summary_json,'$.required_permission_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.required_permission_count')
            =json_array_length(tool.required_permission_codes_json)
          AND json_type(NEW.after_summary_json,'$.role_grant_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.role_grant_count')=(
            SELECT COUNT(*) FROM mcp_tool_role_grants role_grant
            WHERE role_grant.tenant_id=tool.tenant_id
              AND role_grant.mcp_tool_id=tool.mcp_tool_id)
          AND json_type(NEW.after_summary_json,'$.user_grant_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.user_grant_count')=(
            SELECT COUNT(*) FROM mcp_tool_user_grants user_grant
            WHERE user_grant.tenant_id=tool.tenant_id
              AND user_grant.mcp_tool_id=tool.mcp_tool_id)
          AND json_type(NEW.after_summary_json,'$.redaction_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.redaction_count')
            =json_array_length(tool.result_redaction_pointers_json)
          AND EXISTS(
            SELECT 1 FROM mcp_server_operations operation
            WHERE operation.tenant_id=tool.tenant_id
              AND operation.mcp_server_id=tool.mcp_server_id
              AND operation.operation_type IN('register','refresh')
              AND operation.status='succeeded'
              AND operation.actor_user_id=tool.updated_by
              AND operation.request_id=NEW.request_id
              AND operation.trace_id=NEW.trace_id
              AND operation.completed_at=NEW.created_at
              AND (
                ((SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
                  AND tool.created_at=tool.updated_at
                  AND tool.schema_revision=1 AND tool.policy_revision=1)
                OR
                ((SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=11
                  AND operation.operation_type='refresh'
                  AND json_extract(NEW.before_summary_json,'$.mcp_tool_id')=tool.mcp_tool_id
                  AND json_extract(NEW.before_summary_json,'$.mcp_server_id')
                    =tool.mcp_server_id
                  AND json_type(NEW.before_summary_json,'$.status')='text'
                  AND json_extract(NEW.before_summary_json,'$.status')
                    IN('review_required','active','disabled')
                  AND json_type(NEW.before_summary_json,'$.schema_revision')='integer'
                  AND json_type(NEW.before_summary_json,'$.policy_revision')='integer'
                  AND json_type(NEW.before_summary_json,'$.risk_level')='text'
                  AND json_extract(NEW.before_summary_json,'$.risk_level')
                    IN('standard','high')
                  AND json_type(NEW.before_summary_json,'$.approval_required')
                    IN('true','false')
                  AND json_type(NEW.before_summary_json,'$.required_permission_count')
                    ='integer'
                  AND json_extract(NEW.before_summary_json,'$.required_permission_count')>=0
                  AND json_type(NEW.before_summary_json,'$.role_grant_count')='integer'
                  AND json_extract(NEW.before_summary_json,'$.role_grant_count')>=0
                  AND json_type(NEW.before_summary_json,'$.user_grant_count')='integer'
                  AND json_extract(NEW.before_summary_json,'$.user_grant_count')>=0
                  AND json_type(NEW.before_summary_json,'$.redaction_count')='integer'
                  AND json_extract(NEW.before_summary_json,'$.redaction_count')>=0
                  AND ((tool.schema_revision
                        =json_extract(NEW.before_summary_json,'$.schema_revision')+1
                      AND tool.policy_revision
                        =json_extract(NEW.before_summary_json,'$.policy_revision')+1
                      AND tool.status='review_required')
                    OR (tool.schema_revision
                        =json_extract(NEW.before_summary_json,'$.schema_revision')
                      AND tool.policy_revision
                        =json_extract(NEW.before_summary_json,'$.policy_revision')
                      AND tool.status='disabled')))))
        )
        OR
        (NEW.action='mcp.tool_policy_updated'
          AND json_type(NEW.before_summary_json,'$.mcp_tool_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_tool_id')=tool.mcp_tool_id
          AND json_type(NEW.before_summary_json,'$.mcp_server_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=tool.mcp_server_id
          AND json_type(NEW.before_summary_json,'$.policy_revision')='integer'
          AND json_extract(NEW.before_summary_json,'$.policy_revision')+1
            =tool.policy_revision
          AND json_type(NEW.before_summary_json,'$.schema_revision')='integer'
          AND json_extract(NEW.before_summary_json,'$.schema_revision')
            =tool.schema_revision
          AND json_type(NEW.before_summary_json,'$.status')='text'
          AND json_extract(NEW.before_summary_json,'$.status')
            IN('review_required','active','disabled')
          AND json_type(NEW.before_summary_json,'$.risk_level')='text'
          AND json_extract(NEW.before_summary_json,'$.risk_level') IN('standard','high')
          AND json_type(NEW.before_summary_json,'$.approval_required') IN('true','false')
          AND json_type(NEW.before_summary_json,'$.required_permission_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.required_permission_count')>=0
          AND json_type(NEW.before_summary_json,'$.redaction_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.redaction_count')>=0
          AND json_type(NEW.after_summary_json,'$.status')='text'
          AND json_extract(NEW.after_summary_json,'$.status')=tool.status
          AND json_type(NEW.after_summary_json,'$.schema_revision')='integer'
          AND json_extract(NEW.after_summary_json,'$.schema_revision')=tool.schema_revision
          AND json_type(NEW.after_summary_json,'$.risk_level')='text'
          AND json_extract(NEW.after_summary_json,'$.risk_level')=tool.risk_level
          AND json_type(NEW.after_summary_json,'$.approval_required') IN('true','false')
          AND json_extract(NEW.after_summary_json,'$.approval_required')
            =tool.approval_required
          AND json_type(NEW.after_summary_json,'$.required_permission_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.required_permission_count')
            =json_array_length(tool.required_permission_codes_json)
          AND json_type(NEW.after_summary_json,'$.redaction_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.redaction_count')
            =json_array_length(tool.result_redaction_pointers_json))
        OR
        (NEW.action='mcp.tool_grants_updated'
          AND json_type(NEW.before_summary_json,'$.mcp_tool_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_tool_id')=tool.mcp_tool_id
          AND json_type(NEW.before_summary_json,'$.mcp_server_id')='text'
          AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=tool.mcp_server_id
          AND json_type(NEW.before_summary_json,'$.policy_revision')='integer'
          AND json_extract(NEW.before_summary_json,'$.policy_revision')+1
            =tool.policy_revision
          AND json_type(NEW.before_summary_json,'$.role_grant_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.role_grant_count')>=0
          AND json_type(NEW.before_summary_json,'$.user_grant_count')='integer'
          AND json_extract(NEW.before_summary_json,'$.user_grant_count')>=0
          AND json_type(NEW.after_summary_json,'$.role_grant_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.role_grant_count')=(
            SELECT COUNT(*) FROM mcp_tool_role_grants role_grant
            WHERE role_grant.tenant_id=tool.tenant_id
              AND role_grant.mcp_tool_id=tool.mcp_tool_id)
          AND json_type(NEW.after_summary_json,'$.user_grant_count')='integer'
          AND json_extract(NEW.after_summary_json,'$.user_grant_count')=(
            SELECT COUNT(*) FROM mcp_tool_user_grants user_grant
            WHERE user_grant.tenant_id=tool.tenant_id
              AND user_grant.mcp_tool_id=tool.mcp_tool_id))
      )
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;


CREATE UNIQUE INDEX idx_audit_logs_mcp_network_policy_prd083
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE action='mcp.network_policy_updated';
CREATE TRIGGER trg_audit_logs_mcp_network_policy_insert_prd083
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (NEW.action='mcp.network_policy_updated' AND (
  NEW.object_type!='mcp_network_policy' OR NEW.object_name IS NOT NULL
  OR (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))!=11
  OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))!=11
  OR (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))
    !=(SELECT COUNT(DISTINCT key) FROM json_each(NEW.before_summary_json))
  OR (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))
    !=(SELECT COUNT(DISTINCT key) FROM json_each(NEW.after_summary_json))
  OR (SELECT COUNT(*) FROM json_each(NEW.changed_fields_json))
    !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.changed_fields_json))
  OR EXISTS(SELECT 1 FROM json_each(NEW.before_summary_json) field
    WHERE field.key NOT IN(
      'mcp_server_id','status','revision','egress_mode','destination_count',
      'connect_timeout_seconds','probe_timeout_seconds','call_timeout_seconds',
      'failure_threshold','open_seconds','waiting_job_resume_count'))
  OR EXISTS(SELECT 1 FROM json_each(NEW.after_summary_json) field
    WHERE field.key NOT IN(
      'mcp_server_id','status','revision','egress_mode','destination_count',
      'connect_timeout_seconds','probe_timeout_seconds','call_timeout_seconds',
      'failure_threshold','open_seconds','waiting_job_resume_count'))
  OR EXISTS(SELECT 1 FROM json_each(NEW.changed_fields_json) field
    WHERE field.type!='text' OR field.value NOT IN(
      'mcp_server_id','status','revision','egress_mode','destination_count',
      'connect_timeout_seconds','probe_timeout_seconds','call_timeout_seconds',
      'failure_threshold','open_seconds','waiting_job_resume_count'))
  OR NOT EXISTS(
    SELECT 1
    FROM mcp_network_policies policy
    JOIN mcp_network_policy_revisions current
      ON current.tenant_id=policy.tenant_id
     AND current.mcp_network_policy_id=policy.mcp_network_policy_id
     AND current.policy_revision=policy.current_revision
    JOIN mcp_network_policy_revisions previous
      ON previous.tenant_id=current.tenant_id
     AND previous.mcp_network_policy_id=current.mcp_network_policy_id
     AND previous.policy_revision=current.policy_revision-1
    JOIN users actor ON actor.tenant_id=current.tenant_id
      AND actor.user_id=current.updated_by
    WHERE policy.tenant_id=NEW.tenant_id
      AND policy.mcp_network_policy_id=NEW.object_id
      AND actor.status='active' AND NEW.actor_user_id=actor.user_id
      AND NEW.actor_email=actor.email AND NEW.actor_role=actor.role
      AND NEW.request_id=current.request_id AND NEW.trace_id=current.trace_id
      AND NEW.created_at=current.created_at AND policy.updated_at=current.created_at
      AND json_extract(NEW.before_summary_json,'$.mcp_server_id')=previous.mcp_server_id
      AND json_extract(NEW.after_summary_json,'$.mcp_server_id')=current.mcp_server_id
      AND json_extract(NEW.before_summary_json,'$.status')=previous.status
      AND json_extract(NEW.after_summary_json,'$.status')=current.status
      AND json_extract(NEW.before_summary_json,'$.revision')=previous.policy_revision
      AND json_extract(NEW.after_summary_json,'$.revision')=current.policy_revision
      AND json_extract(NEW.before_summary_json,'$.egress_mode')=previous.egress_mode
      AND json_extract(NEW.after_summary_json,'$.egress_mode')=current.egress_mode
      AND json_extract(NEW.before_summary_json,'$.destination_count')
        =CASE previous.status WHEN 'active' THEN 1 ELSE 0 END
      AND json_extract(NEW.after_summary_json,'$.destination_count')
        =CASE current.status WHEN 'active' THEN 1 ELSE 0 END
      AND json_extract(NEW.before_summary_json,'$.connect_timeout_seconds')
        =previous.connect_timeout_seconds
      AND json_extract(NEW.after_summary_json,'$.connect_timeout_seconds')
        =current.connect_timeout_seconds
      AND json_extract(NEW.before_summary_json,'$.probe_timeout_seconds')
        =previous.probe_timeout_seconds
      AND json_extract(NEW.after_summary_json,'$.probe_timeout_seconds')
        =current.probe_timeout_seconds
      AND json_extract(NEW.before_summary_json,'$.call_timeout_seconds')
        =previous.call_timeout_seconds
      AND json_extract(NEW.after_summary_json,'$.call_timeout_seconds')
        =current.call_timeout_seconds
      AND json_extract(NEW.before_summary_json,'$.failure_threshold')
        =previous.failure_threshold
      AND json_extract(NEW.after_summary_json,'$.failure_threshold')
        =current.failure_threshold
      AND json_extract(NEW.before_summary_json,'$.open_seconds')=previous.open_seconds
      AND json_extract(NEW.after_summary_json,'$.open_seconds')=current.open_seconds
      AND json_extract(NEW.before_summary_json,'$.waiting_job_resume_count')=0
      AND json_extract(NEW.after_summary_json,'$.waiting_job_resume_count')=CASE
        WHEN previous.status='disabled' AND current.status='active'
          AND EXISTS(SELECT 1 FROM mcp_server_sync_jobs job
          WHERE job.tenant_id=current.tenant_id
            AND job.mcp_server_id=current.mcp_server_id
            AND job.status='pending' AND job.attempt_count=0
            AND job.next_attempt_at=current.created_at
            AND job.updated_at=current.created_at)
        THEN 1 ELSE 0 END
  )
  OR EXISTS(
    SELECT 1 FROM json_each(
      '["mcp_server_id","status","revision","egress_mode","destination_count",'
      ||'"connect_timeout_seconds","probe_timeout_seconds","call_timeout_seconds",'
      ||'"failure_threshold","open_seconds","waiting_job_resume_count"]'
    ) expected
    WHERE (json_extract(NEW.before_summary_json,'$.'||expected.value)
        IS NOT json_extract(NEW.after_summary_json,'$.'||expected.value))
      !=EXISTS(SELECT 1 FROM json_each(NEW.changed_fields_json) actual
        WHERE actual.value=expected.value)
  )
)) OR (NEW.object_type='mcp_network_policy'
  AND NEW.action!='mcp.network_policy_updated')
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

DROP TABLE IF EXISTS temp.prd083_up_final_guard;
CREATE TEMP TABLE prd083_up_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd083_up_final_guard_abort
BEFORE INSERT ON prd083_up_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd083_up_final_invalid'); END;
INSERT INTO prd083_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=85
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=18)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='08eee7f1eb3c329ae7d27f205afdd47aa69db4d702647a09c75271aac0433492'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=224
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='cae76323d18bad139ecfd04950e2b3c50d37916dd41a2427a93bc3927acc7a84'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=18
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'mcp_network_policies','mcp_network_policy_revisions',
      'mcp_invocation_network_bindings','mcp_network_decisions',
      'mcp_network_call_intents','mcp_network_circuits','mcp_network_events',
      'mcp_network_event_outbox'))=8 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN(
      'trg_mcp_network_call_intents_event_exclusion_prd083',
      'trg_mcp_events_network_exclusion_prd083'))=2 THEN 0 ELSE 1 END
  + ABS((SELECT COUNT(*) FROM mcp_servers)
      -(SELECT COUNT(*) FROM mcp_network_policies))
  + ABS((SELECT COUNT(*) FROM mcp_servers)
      -(SELECT COUNT(*) FROM mcp_network_policy_revisions
          WHERE policy_revision=1 AND status='disabled'))
  + ABS((SELECT COUNT(*) FROM mcp_invocations)
      -(SELECT COUNT(*) FROM mcp_invocation_network_bindings))
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='mcp_server_sync_jobs'),'''waiting_policy''')>0
      AND instr((SELECT sql FROM sqlite_schema
        WHERE type='table' AND name='mcp_invocations'),'''mcp.network_blocked''')>0
      AND instr((SELECT sql FROM sqlite_schema
        WHERE type='table' AND name='audit_logs'),'''mcp.network_policy_updated''')>0
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd083_up_final_guard_abort;
DROP TABLE prd083_up_final_guard;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
