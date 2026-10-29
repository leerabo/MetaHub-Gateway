-- PRD-074: tenant-isolated asynchronous Batch Scheduler and Worker.
-- Sensitive names, operation parameters, prompts, and responses are stored only as AES-GCM envelopes.
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

DROP TABLE IF EXISTS temp.prd074_up_guard;
CREATE TEMP TABLE prd074_up_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd074_up_guard_ok CHECK(issue_count=0)
);
INSERT INTO prd074_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0066' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0066')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=62
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=9)=62
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=166
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*62 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=9)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'batch_jobs','batch_tasks','batch_attempts','batch_execution_grants',
      'batch_budget_holds','batch_budget_allocations','batch_scheduler_tenants',
      'batch_job_events','batch_job_event_outbox','batch_reconciliation_events',
      'rbac_permission_templates_prd074_new','permissions_prd074_new'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd074_up_guard;

-- Only roles whose revision-9 hash and complete relationship set match the product template are expanded.
CREATE TEMP TABLE prd074_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319'
    WHEN 'developer' THEN 'a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f'
    WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
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

CREATE TABLE rbac_permission_templates_prd074_new(
  permission_code TEXT PRIMARY KEY CHECK(
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=10)
);
INSERT INTO rbac_permission_templates_prd074_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,10
FROM rbac_permission_templates;
INSERT INTO rbac_permission_templates_prd074_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('batch.read','batch','读取批量任务',
   '读取当前租户批量任务、任务状态、成本摘要和授权后的结果。','read',10),
  ('batch.write','batch','管理批量任务',
   '创建、取消和执行当前租户受治理的异步批量任务。','write',10);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd074_new RENAME TO rbac_permission_templates;

INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','batch.read',1),('admin','batch.write',1),
  ('developer','batch.read',1),('developer','batch.write',1),
  ('ops_admin','batch.read',1),('ops_admin','batch.write',1),
  ('ops_admin','file.read',1);

CREATE TABLE permissions_prd074_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=10),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
INSERT INTO permissions_prd074_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       10,status,created_at,updated_at
FROM permissions;
INSERT INTO permissions_prd074_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       10,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template
  ON template.permission_code IN ('batch.read','batch.write');
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
ALTER TABLE permissions_prd074_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd074_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE permission.code IN ('batch.read','batch.write')
   OR (role.code='ops_admin' AND permission.code='file.read');
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN 'ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262'
      WHEN 'developer' THEN '89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30'
      WHEN 'ops_admin' THEN '72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6'
    END
WHERE role_id IN (SELECT role_id FROM prd074_roles_to_expand);

-- Restore immutable RBAC catalog and live-matrix triggers with revision-10 hashes.
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
    AND template.risk_level=NEW.risk_level AND template.catalog_revision=NEW.catalog_revision
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
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN 'ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262'
    WHEN 'developer' THEN '89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
    WHEN 'ops_admin' THEN '72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6'
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
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id AND role.status='active'
    AND permission.permission_id=NEW.permission_id AND permission.status='active'
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
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格和成本异常。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由和批量任务运维。' END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN 'ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262'
      WHEN 'developer' THEN '89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
      WHEN 'ops_admin' THEN '72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  ) SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    10,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  ) SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission
    ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

DROP TABLE prd074_roles_to_expand;

-- Composite owner support for Batch budget allocations.
CREATE UNIQUE INDEX uq_budget_rules_tenant_rule_prd074
  ON budget_rules(tenant_id,budget_rule_id);

CREATE TABLE batch_jobs(
  batch_job_id TEXT PRIMARY KEY CHECK(length(batch_job_id)=37 AND batch_job_id GLOB 'bjob_*'
    AND substr(batch_job_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  submitted_by_user_id TEXT NOT NULL,
  idempotency_key_hash TEXT NOT NULL CHECK(length(idempotency_key_hash)=64
    AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
  request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  virtual_key_id TEXT NOT NULL,
  route_plan_json TEXT NOT NULL CHECK(json_valid(route_plan_json)
    AND json_type(route_plan_json)='object' AND length(CAST(route_plan_json AS BLOB))<=16384),
  name_ciphertext TEXT NOT NULL CHECK(length(name_ciphertext) BETWEEN 32 AND 8192),
  operation TEXT NOT NULL CHECK(operation IN ('summarize','translate','classify')),
  operation_spec_ciphertext TEXT NOT NULL CHECK(length(operation_spec_ciphertext) BETWEEN 32 AND 32768),
  operation_template_version TEXT NOT NULL CHECK(operation_template_version='batch-operation-templates-v1'),
  priority TEXT NOT NULL CHECK(priority IN ('low','normal','high')),
  max_duration_seconds INTEGER NOT NULL CHECK(typeof(max_duration_seconds)='integer'
    AND max_duration_seconds BETWEEN 30 AND 3600),
  max_attempts INTEGER NOT NULL CHECK(typeof(max_attempts)='integer' AND max_attempts BETWEEN 1 AND 3),
  max_output_tokens INTEGER NOT NULL CHECK(typeof(max_output_tokens)='integer'
    AND max_output_tokens BETWEEN 16 AND 4096),
  task_count INTEGER NOT NULL CHECK(typeof(task_count)='integer' AND task_count BETWEEN 1 AND 100),
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','completed','partial_failed','failed','cancelled','expired')),
  estimated_input_tokens INTEGER NOT NULL CHECK(estimated_input_tokens>=0),
  estimated_output_tokens INTEGER NOT NULL CHECK(estimated_output_tokens>=0),
  estimated_cost_cny TEXT NOT NULL CHECK(typeof(estimated_cost_cny)='text'
    AND length(estimated_cost_cny) BETWEEN 1 AND 24
    AND estimated_cost_cny GLOB '[0-9]*' AND estimated_cost_cny NOT GLOB '*[^0-9.]*'
    AND (instr(estimated_cost_cny,'.')=0 OR (
      instr(substr(estimated_cost_cny,instr(estimated_cost_cny,'.')+1),'.')=0
      AND length(substr(estimated_cost_cny,instr(estimated_cost_cny,'.')+1)) BETWEEN 1 AND 6))),
  posted_cost_cny TEXT NOT NULL DEFAULT '0' CHECK(typeof(posted_cost_cny)='text'
    AND length(posted_cost_cny) BETWEEN 1 AND 24
    AND posted_cost_cny GLOB '[0-9]*' AND posted_cost_cny NOT GLOB '*[^0-9.]*'
    AND (instr(posted_cost_cny,'.')=0 OR (
      instr(substr(posted_cost_cny,instr(posted_cost_cny,'.')+1),'.')=0
      AND length(substr(posted_cost_cny,instr(posted_cost_cny,'.')+1)) BETWEEN 1 AND 6))),
  possible_cost_cny TEXT NOT NULL DEFAULT '0' CHECK(typeof(possible_cost_cny)='text'
    AND length(possible_cost_cny) BETWEEN 1 AND 24
    AND possible_cost_cny GLOB '[0-9]*' AND possible_cost_cny NOT GLOB '*[^0-9.]*'
    AND (instr(possible_cost_cny,'.')=0 OR (
      instr(substr(possible_cost_cny,instr(possible_cost_cny,'.')+1),'.')=0
      AND length(substr(possible_cost_cny,instr(possible_cost_cny,'.')+1)) BETWEEN 1 AND 6))),
  reconciliation_pending INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_pending IN (0,1)),
  deadline_at TEXT NOT NULL CHECK(unixepoch(deadline_at) IS NOT NULL),
  cancel_requested_at TEXT CHECK(cancel_requested_at IS NULL OR unixepoch(cancel_requested_at) IS NOT NULL),
  cancelled_by_user_id TEXT,
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  terminal_error_code TEXT,
  source_request_id TEXT NOT NULL CHECK(length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK(length(source_trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,submitted_by_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  FOREIGN KEY(tenant_id,environment_id) REFERENCES environments(tenant_id,environment_id),
  FOREIGN KEY(tenant_id,model_alias_id) REFERENCES model_aliases(tenant_id,model_alias_id),
  FOREIGN KEY(tenant_id,virtual_key_id) REFERENCES virtual_keys(tenant_id,virtual_key_id),
  FOREIGN KEY(tenant_id,cancelled_by_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,batch_job_id),
  UNIQUE(tenant_id,idempotency_key_hash),
  CHECK(unixepoch(deadline_at)=unixepoch(created_at)+max_duration_seconds),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at))
);
CREATE INDEX idx_batch_jobs_tenant_created_prd074
  ON batch_jobs(tenant_id,created_at DESC,batch_job_id DESC);
CREATE INDEX idx_batch_jobs_status_deadline_prd074
  ON batch_jobs(status,deadline_at,tenant_id,batch_job_id)
  WHERE status IN ('queued','running');

CREATE TABLE batch_tasks(
  batch_task_id TEXT PRIMARY KEY CHECK(length(batch_task_id)=37 AND batch_task_id GLOB 'btsk_*'
    AND substr(batch_task_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  batch_job_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  task_index INTEGER NOT NULL CHECK(typeof(task_index)='integer' AND task_index BETWEEN 0 AND 99),
  priority_rank INTEGER NOT NULL CHECK(priority_rank IN (1,2,3)),
  status TEXT NOT NULL CHECK(status IN
    ('queued','leased','calling','retry_wait','succeeded','failed','cancelled','expired','outcome_unknown')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 3),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  cancel_requested_at TEXT CHECK(cancel_requested_at IS NULL OR unixepoch(cancel_requested_at) IS NOT NULL),
  lease_token TEXT,
  lease_owner_id TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(typeof(lease_generation)='integer' AND lease_generation>=0),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  current_attempt_id TEXT,
  terminal_request_id TEXT,
  terminal_trace_id TEXT,
  failure_code TEXT,
  result_available INTEGER NOT NULL DEFAULT 0 CHECK(result_available IN (0,1)),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,batch_job_id) REFERENCES batch_jobs(tenant_id,batch_job_id),
  FOREIGN KEY(tenant_id,file_id) REFERENCES uploaded_files(tenant_id,file_id),
  UNIQUE(tenant_id,batch_task_id),
  UNIQUE(tenant_id,batch_job_id,task_index),
  UNIQUE(tenant_id,batch_job_id,file_id),
  CHECK((status IN ('leased','calling') AND lease_token IS NOT NULL
      AND lease_owner_id IS NOT NULL AND lease_expires_at IS NOT NULL AND current_attempt_id IS NOT NULL)
    OR (status NOT IN ('leased','calling') AND lease_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX idx_batch_tasks_due_prd074
  ON batch_tasks(status,next_attempt_at,priority_rank DESC,created_at,batch_task_id)
  WHERE status IN ('queued','retry_wait');
CREATE INDEX idx_batch_tasks_lease_prd074
  ON batch_tasks(status,lease_expires_at,tenant_id,batch_task_id)
  WHERE status IN ('leased','calling');
CREATE INDEX idx_batch_tasks_job_prd074
  ON batch_tasks(tenant_id,batch_job_id,task_index,batch_task_id);

CREATE TABLE batch_attempts(
  batch_attempt_id TEXT PRIMARY KEY CHECK(length(batch_attempt_id)=37 AND batch_attempt_id GLOB 'batm_*'
    AND substr(batch_attempt_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  batch_job_id TEXT NOT NULL,
  batch_task_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL CHECK(typeof(attempt_no)='integer' AND attempt_no BETWEEN 1 AND 3),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  status TEXT NOT NULL CHECK(status IN
    ('prepared','calling','provider_succeeded','known_failed','outcome_unknown')),
  execution_outcome TEXT NOT NULL CHECK(execution_outcome IN
    ('not_invoked','known_failed','succeeded','unknown')),
  retry_safe INTEGER NOT NULL DEFAULT 0 CHECK(retry_safe IN (0,1)),
  gateway_error_code TEXT,
  request_payload_ciphertext TEXT NOT NULL CHECK(length(request_payload_ciphertext) BETWEEN 32 AND 4194304),
  request_payload_key_version INTEGER NOT NULL CHECK(request_payload_key_version=1),
  request_payload_hmac TEXT NOT NULL CHECK(length(request_payload_hmac)=64
    AND request_payload_hmac NOT GLOB '*[^0-9a-f]*'),
  route_plan_json TEXT NOT NULL CHECK(json_valid(route_plan_json)
    AND json_type(route_plan_json)='object' AND length(CAST(route_plan_json AS BLOB))<=16384),
  gateway_write_deadline_at TEXT CHECK(gateway_write_deadline_at IS NULL
    OR unixepoch(gateway_write_deadline_at) IS NOT NULL),
  result_state TEXT NOT NULL DEFAULT 'none' CHECK(result_state IN ('none','available','discarded')),
  result_ciphertext TEXT,
  result_key_version INTEGER CHECK(result_key_version IS NULL OR result_key_version=1),
  result_hmac TEXT CHECK(result_hmac IS NULL OR (length(result_hmac)=64
    AND result_hmac NOT GLOB '*[^0-9a-f]*')),
  input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens>=0),
  output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens>=0),
  cost_cny TEXT CHECK(cost_cny IS NULL OR (typeof(cost_cny)='text'
    AND length(cost_cny) BETWEEN 1 AND 24
    AND cost_cny GLOB '[0-9]*' AND cost_cny NOT GLOB '*[^0-9.]*'
    AND (instr(cost_cny,'.')=0 OR (
      instr(substr(cost_cny,instr(cost_cny,'.')+1),'.')=0
      AND length(substr(cost_cny,instr(cost_cny,'.')+1)) BETWEEN 1 AND 6)))),
  provider_started_at TEXT CHECK(provider_started_at IS NULL OR unixepoch(provider_started_at) IS NOT NULL),
  gateway_completed_at TEXT CHECK(gateway_completed_at IS NULL OR unixepoch(gateway_completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,batch_job_id) REFERENCES batch_jobs(tenant_id,batch_job_id),
  FOREIGN KEY(tenant_id,batch_task_id) REFERENCES batch_tasks(tenant_id,batch_task_id),
  UNIQUE(tenant_id,batch_attempt_id),
  UNIQUE(tenant_id,batch_task_id,attempt_no),
  UNIQUE(request_id),
  UNIQUE(trace_id),
  CHECK((result_state='none' AND result_ciphertext IS NULL AND result_key_version IS NULL AND result_hmac IS NULL)
    OR (result_state='available' AND result_ciphertext IS NOT NULL
      AND result_key_version=1 AND result_hmac IS NOT NULL)
    OR (result_state='discarded' AND result_ciphertext IS NULL
      AND result_key_version IS NULL AND result_hmac IS NOT NULL))
);
CREATE INDEX idx_batch_attempts_task_prd074
  ON batch_attempts(tenant_id,batch_task_id,attempt_no,batch_attempt_id);

CREATE TABLE batch_execution_grants(
  batch_execution_grant_id TEXT PRIMARY KEY CHECK(length(batch_execution_grant_id)=37
    AND batch_execution_grant_id GLOB 'bgrt_*'
    AND substr(batch_execution_grant_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  batch_job_id TEXT NOT NULL,
  batch_task_id TEXT NOT NULL,
  batch_attempt_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  lease_token TEXT NOT NULL,
  lease_generation INTEGER NOT NULL CHECK(lease_generation>=1),
  grant_generation INTEGER NOT NULL CHECK(grant_generation>=1),
  grant_hash TEXT NOT NULL UNIQUE CHECK(length(grant_hash)=64
    AND grant_hash NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN ('issued','consumed','expired','revoked')),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at)=unixepoch(issued_at)+60),
  used_at TEXT CHECK(used_at IS NULL OR unixepoch(used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,batch_job_id) REFERENCES batch_jobs(tenant_id,batch_job_id),
  FOREIGN KEY(tenant_id,batch_task_id) REFERENCES batch_tasks(tenant_id,batch_task_id),
  FOREIGN KEY(tenant_id,batch_attempt_id) REFERENCES batch_attempts(tenant_id,batch_attempt_id),
  FOREIGN KEY(tenant_id,virtual_key_id) REFERENCES virtual_keys(tenant_id,virtual_key_id),
  FOREIGN KEY(tenant_id,model_alias_id) REFERENCES model_aliases(tenant_id,model_alias_id),
  UNIQUE(tenant_id,batch_execution_grant_id),
  UNIQUE(tenant_id,batch_attempt_id,grant_generation),
  CHECK((status='issued' AND used_at IS NULL AND revoked_at IS NULL)
    OR (status='consumed' AND used_at IS NOT NULL AND revoked_at IS NULL)
    OR (status IN ('expired','revoked') AND used_at IS NULL))
);
CREATE UNIQUE INDEX idx_batch_grants_one_issued_prd074
  ON batch_execution_grants(tenant_id,batch_attempt_id) WHERE status='issued';
CREATE UNIQUE INDEX idx_batch_grants_one_consumed_prd074
  ON batch_execution_grants(tenant_id,batch_attempt_id) WHERE status='consumed';

CREATE TABLE batch_budget_holds(
  batch_budget_hold_id TEXT PRIMARY KEY CHECK(length(batch_budget_hold_id)=37
    AND batch_budget_hold_id GLOB 'bbhd_*'
    AND substr(batch_budget_hold_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  batch_job_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK(scope_type IN ('tenant','virtual_key','project','application')),
  scope_id TEXT NOT NULL,
  budget_date TEXT NOT NULL CHECK(date(budget_date)=budget_date),
  reserved_cost_cny TEXT NOT NULL CHECK(typeof(reserved_cost_cny)='text'
    AND length(reserved_cost_cny) BETWEEN 1 AND 24
    AND reserved_cost_cny GLOB '[0-9]*' AND reserved_cost_cny NOT GLOB '*[^0-9.]*'
    AND (instr(reserved_cost_cny,'.')=0 OR (
      instr(substr(reserved_cost_cny,instr(reserved_cost_cny,'.')+1),'.')=0
      AND length(substr(reserved_cost_cny,instr(reserved_cost_cny,'.')+1)) BETWEEN 1 AND 6))),
  remaining_cost_cny TEXT NOT NULL CHECK(typeof(remaining_cost_cny)='text'
    AND length(remaining_cost_cny) BETWEEN 1 AND 24
    AND remaining_cost_cny GLOB '[0-9]*' AND remaining_cost_cny NOT GLOB '*[^0-9.]*'
    AND (instr(remaining_cost_cny,'.')=0 OR (
      instr(substr(remaining_cost_cny,instr(remaining_cost_cny,'.')+1),'.')=0
      AND length(substr(remaining_cost_cny,instr(remaining_cost_cny,'.')+1)) BETWEEN 1 AND 6))),
  reconciliation_cost_cny TEXT NOT NULL DEFAULT '0' CHECK(typeof(reconciliation_cost_cny)='text'
    AND length(reconciliation_cost_cny) BETWEEN 1 AND 24
    AND reconciliation_cost_cny GLOB '[0-9]*'
    AND reconciliation_cost_cny NOT GLOB '*[^0-9.]*'
    AND (instr(reconciliation_cost_cny,'.')=0 OR (
      instr(substr(reconciliation_cost_cny,instr(reconciliation_cost_cny,'.')+1),'.')=0
      AND length(substr(reconciliation_cost_cny,instr(reconciliation_cost_cny,'.')+1)) BETWEEN 1 AND 6))),
  status TEXT NOT NULL CHECK(status IN ('active','released','settled','reconciliation')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,batch_job_id) REFERENCES batch_jobs(tenant_id,batch_job_id),
  FOREIGN KEY(tenant_id,budget_rule_id) REFERENCES budget_rules(tenant_id,budget_rule_id),
  FOREIGN KEY(tenant_id,environment_id) REFERENCES environments(tenant_id,environment_id),
  UNIQUE(tenant_id,batch_budget_hold_id),
  UNIQUE(tenant_id,batch_job_id,budget_rule_id,budget_date),
  CHECK(CAST(remaining_cost_cny AS NUMERIC)+CAST(reconciliation_cost_cny AS NUMERIC)
    <=CAST(reserved_cost_cny AS NUMERIC))
);
CREATE INDEX idx_batch_holds_budget_prd074
  ON batch_budget_holds(tenant_id,budget_rule_id,budget_date,status);

CREATE TABLE batch_budget_allocations(
  batch_budget_allocation_id TEXT PRIMARY KEY CHECK(length(batch_budget_allocation_id)=37
    AND batch_budget_allocation_id GLOB 'bbal_*'
    AND substr(batch_budget_allocation_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  batch_job_id TEXT NOT NULL,
  batch_task_id TEXT NOT NULL,
  batch_attempt_id TEXT NOT NULL,
  batch_budget_hold_id TEXT NOT NULL,
  budget_rule_id TEXT NOT NULL,
  budget_date TEXT NOT NULL CHECK(date(budget_date)=budget_date),
  reserved_cost_cny TEXT NOT NULL CHECK(typeof(reserved_cost_cny)='text'
    AND length(reserved_cost_cny) BETWEEN 1 AND 24
    AND reserved_cost_cny GLOB '[0-9]*' AND reserved_cost_cny NOT GLOB '*[^0-9.]*'
    AND (instr(reserved_cost_cny,'.')=0 OR (
      instr(substr(reserved_cost_cny,instr(reserved_cost_cny,'.')+1),'.')=0
      AND length(substr(reserved_cost_cny,instr(reserved_cost_cny,'.')+1)) BETWEEN 1 AND 6))),
  route_plan_hash TEXT NOT NULL CHECK(length(route_plan_hash)=64
    AND route_plan_hash NOT GLOB '*[^0-9a-f]*'),
  execution_outcome TEXT NOT NULL CHECK(execution_outcome IN
    ('not_invoked','known_failed','succeeded','unknown')),
  charge_state TEXT NOT NULL CHECK(charge_state IN ('none','posted','possible')),
  status TEXT NOT NULL CHECK(status IN ('active','posted','released','reconciliation')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,batch_job_id) REFERENCES batch_jobs(tenant_id,batch_job_id),
  FOREIGN KEY(tenant_id,batch_task_id) REFERENCES batch_tasks(tenant_id,batch_task_id),
  FOREIGN KEY(tenant_id,batch_attempt_id) REFERENCES batch_attempts(tenant_id,batch_attempt_id),
  FOREIGN KEY(tenant_id,batch_budget_hold_id) REFERENCES batch_budget_holds(tenant_id,batch_budget_hold_id),
  FOREIGN KEY(tenant_id,budget_rule_id) REFERENCES budget_rules(tenant_id,budget_rule_id),
  UNIQUE(tenant_id,batch_budget_allocation_id),
  UNIQUE(tenant_id,batch_attempt_id,budget_rule_id,budget_date)
);
CREATE INDEX idx_batch_allocations_budget_prd074
  ON batch_budget_allocations(tenant_id,budget_rule_id,budget_date,status);

CREATE TABLE batch_scheduler_tenants(
  tenant_id TEXT PRIMARY KEY,
  last_dispatched_at TEXT NOT NULL CHECK(unixepoch(last_dispatched_at) IS NOT NULL),
  dispatch_revision INTEGER NOT NULL CHECK(typeof(dispatch_revision)='integer' AND dispatch_revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
);

CREATE TABLE batch_job_events(
  batch_job_event_id TEXT PRIMARY KEY CHECK(length(batch_job_event_id)=37
    AND batch_job_event_id GLOB 'bjev_*'
    AND substr(batch_job_event_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  batch_job_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('batch_job.created','batch_job.completed')),
  actor_type TEXT NOT NULL CHECK(actor_type IN ('user','system')),
  actor_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK(length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK(length(source_trace_id) BETWEEN 1 AND 160),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(CAST(payload_json AS BLOB))<=8192),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,batch_job_id) REFERENCES batch_jobs(tenant_id,batch_job_id),
  UNIQUE(tenant_id,batch_job_event_id),
  UNIQUE(tenant_id,batch_job_id,event_type)
);
CREATE INDEX idx_batch_job_events_tenant_prd074
  ON batch_job_events(tenant_id,occurred_at,batch_job_event_id);

CREATE TABLE batch_job_event_outbox(
  batch_job_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  dispatch_status TEXT NOT NULL CHECK(dispatch_status IN ('pending','processing','dispatched','failed')),
  dispatch_attempts INTEGER NOT NULL CHECK(typeof(dispatch_attempts)='integer'
    AND dispatch_attempts BETWEEN 0 AND 10),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  dispatched_at TEXT CHECK(dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL),
  claim_token TEXT,
  lease_owner_id TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  delivery_snapshot_frozen_at TEXT CHECK(delivery_snapshot_frozen_at IS NULL
    OR unixepoch(delivery_snapshot_frozen_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN
    ('webhook.payload_invalid','webhook.delivery_unavailable')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,batch_job_event_id)
    REFERENCES batch_job_events(tenant_id,batch_job_event_id),
  UNIQUE(tenant_id,batch_job_event_id),
  CHECK(
    (dispatch_status='pending' AND dispatch_attempts BETWEEN 0 AND 9
      AND next_attempt_at IS NOT NULL AND dispatched_at IS NULL
      AND claim_token IS NULL AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND (last_error_code IS NULL OR last_error_code='webhook.delivery_unavailable'))
    OR (dispatch_status='processing' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL AND last_error_code IS NULL
      AND claim_token IS NOT NULL AND length(claim_token)=32
      AND claim_token NOT GLOB '*[^0-9a-f]*'
      AND lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 1 AND 128
      AND lease_owner_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
      AND lease_expires_at IS NOT NULL)
    OR (dispatch_status='dispatched' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NOT NULL
      AND claim_token IS NULL AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND last_error_code IS NULL AND delivery_snapshot_frozen_at IS NOT NULL)
    OR (dispatch_status='failed' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL
      AND claim_token IS NULL AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND (last_error_code='webhook.payload_invalid'
        OR (dispatch_attempts=10 AND last_error_code='webhook.delivery_unavailable')))
  )
);
CREATE INDEX idx_batch_job_outbox_due_prd074
  ON batch_job_event_outbox(dispatch_status,next_attempt_at,lease_expires_at,created_at,batch_job_event_id)
  WHERE dispatch_status IN ('pending','processing');

CREATE TABLE batch_reconciliation_events(
  batch_reconciliation_event_id TEXT PRIMARY KEY CHECK(length(batch_reconciliation_event_id)=37
    AND batch_reconciliation_event_id GLOB 'brec_*'
    AND substr(batch_reconciliation_event_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  batch_job_id TEXT NOT NULL,
  batch_task_id TEXT NOT NULL,
  batch_attempt_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  idempotency_key_hash TEXT NOT NULL CHECK(length(idempotency_key_hash)=64
    AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
  decision TEXT NOT NULL CHECK(decision IN ('confirm_not_charged','confirm_charged')),
  adjustment_id TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,batch_job_id) REFERENCES batch_jobs(tenant_id,batch_job_id),
  FOREIGN KEY(tenant_id,batch_task_id) REFERENCES batch_tasks(tenant_id,batch_task_id),
  FOREIGN KEY(tenant_id,batch_attempt_id) REFERENCES batch_attempts(tenant_id,batch_attempt_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,batch_reconciliation_event_id),
  UNIQUE(tenant_id,idempotency_key_hash),
  CHECK((decision='confirm_not_charged' AND adjustment_id IS NULL)
    OR (decision='confirm_charged' AND adjustment_id IS NOT NULL))
);

-- Immutable identity and owner guards. State transitions remain CAS-controlled by services.
CREATE TRIGGER trg_batch_jobs_owner_insert_prd074
BEFORE INSERT ON batch_jobs FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM users WHERE tenant_id=NEW.tenant_id AND user_id=NEW.submitted_by_user_id)
  OR NOT EXISTS(SELECT 1 FROM projects WHERE tenant_id=NEW.tenant_id AND project_id=NEW.project_id AND status='active')
  OR NOT EXISTS(SELECT 1 FROM environments WHERE tenant_id=NEW.tenant_id AND environment_id=NEW.environment_id AND status='active')
  OR NOT EXISTS(SELECT 1 FROM model_aliases WHERE tenant_id=NEW.tenant_id
    AND model_alias_id=NEW.model_alias_id AND environment_id=NEW.environment_id AND status='active')
  OR NOT EXISTS(SELECT 1 FROM virtual_keys WHERE tenant_id=NEW.tenant_id
    AND virtual_key_id=NEW.virtual_key_id AND project_id=NEW.project_id
    AND environment_id=NEW.environment_id AND status='active')
BEGIN SELECT RAISE(ABORT,'batch_job_owner_mismatch'); END;
CREATE TRIGGER trg_batch_jobs_update_prd074
BEFORE UPDATE ON batch_jobs FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.batch_job_id!=OLD.batch_job_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.submitted_by_user_id!=OLD.submitted_by_user_id
  OR NEW.idempotency_key_hash!=OLD.idempotency_key_hash OR NEW.request_fingerprint!=OLD.request_fingerprint
  OR NEW.project_id!=OLD.project_id OR NEW.environment_id!=OLD.environment_id
  OR NEW.model_alias_id!=OLD.model_alias_id OR NEW.virtual_key_id!=OLD.virtual_key_id
  OR NEW.route_plan_json!=OLD.route_plan_json OR NEW.name_ciphertext!=OLD.name_ciphertext
  OR NEW.operation!=OLD.operation OR NEW.operation_spec_ciphertext!=OLD.operation_spec_ciphertext
  OR NEW.operation_template_version!=OLD.operation_template_version
  OR NEW.priority!=OLD.priority OR NEW.max_duration_seconds!=OLD.max_duration_seconds
  OR NEW.max_attempts!=OLD.max_attempts OR NEW.max_output_tokens!=OLD.max_output_tokens
  OR NEW.task_count!=OLD.task_count OR NEW.deadline_at!=OLD.deadline_at
  OR NEW.source_request_id!=OLD.source_request_id OR NEW.source_trace_id!=OLD.source_trace_id
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'batch_job_immutable'); END;
CREATE TRIGGER trg_batch_jobs_delete_prd074 BEFORE DELETE ON batch_jobs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_job_immutable'); END;

CREATE TRIGGER trg_batch_tasks_owner_insert_prd074
BEFORE INSERT ON batch_tasks FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM batch_jobs job WHERE job.tenant_id=NEW.tenant_id
  AND job.batch_job_id=NEW.batch_job_id)
 OR NOT EXISTS(SELECT 1 FROM uploaded_files file WHERE file.tenant_id=NEW.tenant_id
  AND file.file_id=NEW.file_id)
BEGIN SELECT RAISE(ABORT,'batch_task_owner_mismatch'); END;
CREATE TRIGGER trg_batch_tasks_update_prd074
BEFORE UPDATE ON batch_tasks FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.batch_task_id!=OLD.batch_task_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.batch_job_id!=OLD.batch_job_id OR NEW.file_id!=OLD.file_id
  OR NEW.task_index!=OLD.task_index OR NEW.priority_rank!=OLD.priority_rank
  OR NEW.created_at!=OLD.created_at OR NEW.attempt_count<OLD.attempt_count
  OR NEW.lease_generation<OLD.lease_generation
BEGIN SELECT RAISE(ABORT,'batch_task_immutable'); END;
CREATE TRIGGER trg_batch_tasks_delete_prd074 BEFORE DELETE ON batch_tasks FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_task_immutable'); END;

CREATE TRIGGER trg_batch_attempts_owner_insert_prd074
BEFORE INSERT ON batch_attempts FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM batch_tasks task
  JOIN batch_jobs job ON job.tenant_id=task.tenant_id
    AND job.batch_job_id=task.batch_job_id
  WHERE task.tenant_id=NEW.tenant_id AND task.batch_task_id=NEW.batch_task_id
    AND task.batch_job_id=NEW.batch_job_id AND NEW.attempt_no<=job.max_attempts
)
BEGIN SELECT RAISE(ABORT,'batch_attempt_owner_mismatch'); END;
CREATE TRIGGER trg_batch_attempts_update_prd074
BEFORE UPDATE ON batch_attempts FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.batch_attempt_id!=OLD.batch_attempt_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.batch_job_id!=OLD.batch_job_id
  OR NEW.batch_task_id!=OLD.batch_task_id OR NEW.attempt_no!=OLD.attempt_no
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.request_payload_ciphertext!=OLD.request_payload_ciphertext
  OR NEW.request_payload_key_version!=OLD.request_payload_key_version
  OR NEW.request_payload_hmac!=OLD.request_payload_hmac OR NEW.route_plan_json!=OLD.route_plan_json
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'batch_attempt_immutable'); END;
CREATE TRIGGER trg_batch_attempts_delete_prd074 BEFORE DELETE ON batch_attempts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_attempt_immutable'); END;
CREATE TRIGGER trg_batch_grants_owner_insert_prd074
BEFORE INSERT ON batch_execution_grants FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM batch_attempts attempt
  JOIN batch_tasks task ON task.tenant_id=attempt.tenant_id
    AND task.batch_task_id=attempt.batch_task_id
    AND task.batch_job_id=attempt.batch_job_id
  JOIN batch_jobs job ON job.tenant_id=attempt.tenant_id
    AND job.batch_job_id=attempt.batch_job_id
  WHERE attempt.tenant_id=NEW.tenant_id
    AND attempt.batch_attempt_id=NEW.batch_attempt_id
    AND attempt.batch_task_id=NEW.batch_task_id
    AND attempt.batch_job_id=NEW.batch_job_id
    AND attempt.request_id=NEW.request_id AND attempt.trace_id=NEW.trace_id
    AND attempt.status='prepared'
    AND task.status='leased' AND task.current_attempt_id=NEW.batch_attempt_id
    AND task.lease_token=NEW.lease_token
    AND task.lease_generation=NEW.lease_generation
    AND job.virtual_key_id=NEW.virtual_key_id
    AND job.model_alias_id=NEW.model_alias_id
)
BEGIN SELECT RAISE(ABORT,'batch_grant_owner_mismatch'); END;
CREATE TRIGGER trg_batch_grants_update_prd074
BEFORE UPDATE ON batch_execution_grants FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.batch_execution_grant_id!=OLD.batch_execution_grant_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.batch_job_id!=OLD.batch_job_id
  OR NEW.batch_task_id!=OLD.batch_task_id OR NEW.batch_attempt_id!=OLD.batch_attempt_id
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.virtual_key_id!=OLD.virtual_key_id OR NEW.model_alias_id!=OLD.model_alias_id
  OR NEW.lease_token!=OLD.lease_token OR NEW.lease_generation!=OLD.lease_generation
  OR NEW.grant_generation!=OLD.grant_generation OR NEW.grant_hash!=OLD.grant_hash
  OR NEW.issued_at!=OLD.issued_at OR NEW.expires_at!=OLD.expires_at
BEGIN SELECT RAISE(ABORT,'batch_grant_immutable'); END;
CREATE TRIGGER trg_batch_grants_delete_prd074 BEFORE DELETE ON batch_execution_grants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_grant_immutable'); END;
CREATE TRIGGER trg_batch_holds_owner_insert_prd074
BEFORE INSERT ON batch_budget_holds FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM batch_jobs job
  JOIN budget_rules rule ON rule.tenant_id=job.tenant_id
    AND rule.budget_rule_id=NEW.budget_rule_id
  WHERE job.tenant_id=NEW.tenant_id AND job.batch_job_id=NEW.batch_job_id
    AND job.environment_id=NEW.environment_id
    AND rule.environment_id=NEW.environment_id AND rule.status='active'
    AND rule.scope_type=NEW.scope_type AND rule.scope_id=NEW.scope_id
)
BEGIN SELECT RAISE(ABORT,'batch_budget_owner_mismatch'); END;
CREATE TRIGGER trg_batch_holds_update_prd074
BEFORE UPDATE ON batch_budget_holds FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.batch_budget_hold_id!=OLD.batch_budget_hold_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.batch_job_id!=OLD.batch_job_id
  OR NEW.budget_rule_id!=OLD.budget_rule_id OR NEW.environment_id!=OLD.environment_id
  OR NEW.scope_type!=OLD.scope_type OR NEW.scope_id!=OLD.scope_id
  OR NEW.budget_date!=OLD.budget_date OR NEW.reserved_cost_cny!=OLD.reserved_cost_cny
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'batch_budget_immutable'); END;
CREATE TRIGGER trg_batch_holds_amount_nonincrease_prd074
BEFORE UPDATE ON batch_budget_holds FOR EACH ROW
WHEN CAST(NEW.remaining_cost_cny AS NUMERIC)>CAST(OLD.remaining_cost_cny AS NUMERIC)
  OR CAST(NEW.reconciliation_cost_cny AS NUMERIC)
     >CAST(OLD.reconciliation_cost_cny AS NUMERIC)
  OR CAST(NEW.remaining_cost_cny AS NUMERIC)+CAST(NEW.reconciliation_cost_cny AS NUMERIC)
     >CAST(OLD.remaining_cost_cny AS NUMERIC)+CAST(OLD.reconciliation_cost_cny AS NUMERIC)
BEGIN SELECT RAISE(ABORT,'batch_budget_amount_growth'); END;
CREATE TRIGGER trg_batch_holds_delete_prd074 BEFORE DELETE ON batch_budget_holds FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_budget_immutable'); END;
CREATE TRIGGER trg_batch_allocations_owner_insert_prd074
BEFORE INSERT ON batch_budget_allocations FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM batch_attempts attempt
  JOIN batch_tasks task ON task.tenant_id=attempt.tenant_id
    AND task.batch_task_id=attempt.batch_task_id
    AND task.batch_job_id=attempt.batch_job_id
  JOIN batch_budget_holds hold ON hold.tenant_id=attempt.tenant_id
    AND hold.batch_job_id=attempt.batch_job_id
  WHERE attempt.tenant_id=NEW.tenant_id
    AND attempt.batch_attempt_id=NEW.batch_attempt_id
    AND attempt.batch_task_id=NEW.batch_task_id
    AND attempt.batch_job_id=NEW.batch_job_id
    AND hold.batch_budget_hold_id=NEW.batch_budget_hold_id
    AND hold.budget_rule_id=NEW.budget_rule_id
    AND hold.budget_date=NEW.budget_date
)
BEGIN SELECT RAISE(ABORT,'batch_budget_owner_mismatch'); END;
CREATE TRIGGER trg_batch_allocations_update_prd074
BEFORE UPDATE ON batch_budget_allocations FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.batch_budget_allocation_id!=OLD.batch_budget_allocation_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.batch_job_id!=OLD.batch_job_id
  OR NEW.batch_task_id!=OLD.batch_task_id OR NEW.batch_attempt_id!=OLD.batch_attempt_id
  OR NEW.batch_budget_hold_id!=OLD.batch_budget_hold_id
  OR NEW.budget_rule_id!=OLD.budget_rule_id OR NEW.budget_date!=OLD.budget_date
  OR NEW.reserved_cost_cny!=OLD.reserved_cost_cny OR NEW.route_plan_hash!=OLD.route_plan_hash
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'batch_budget_immutable'); END;
CREATE TRIGGER trg_batch_allocations_delete_prd074 BEFORE DELETE ON batch_budget_allocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_budget_immutable'); END;
CREATE TRIGGER trg_batch_scheduler_update_prd074
BEFORE UPDATE ON batch_scheduler_tenants FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.dispatch_revision!=OLD.dispatch_revision+1
  OR unixepoch(NEW.last_dispatched_at)<unixepoch(OLD.last_dispatched_at)
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'batch_scheduler_cursor_invalid'); END;
CREATE TRIGGER trg_batch_scheduler_delete_prd074
BEFORE DELETE ON batch_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_scheduler_cursor_immutable'); END;
CREATE TRIGGER trg_batch_events_owner_insert_prd074
BEFORE INSERT ON batch_job_events FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM batch_jobs job WHERE job.tenant_id=NEW.tenant_id
  AND job.batch_job_id=NEW.batch_job_id)
 OR (NEW.actor_type='user' AND NOT EXISTS(SELECT 1 FROM users user
   WHERE user.tenant_id=NEW.tenant_id AND user.user_id=NEW.actor_id))
BEGIN SELECT RAISE(ABORT,'batch_event_owner_mismatch'); END;
CREATE TRIGGER trg_batch_events_update_prd074 BEFORE UPDATE ON batch_job_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_event_immutable'); END;
CREATE TRIGGER trg_batch_events_delete_prd074 BEFORE DELETE ON batch_job_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_event_immutable'); END;
CREATE TRIGGER trg_batch_reconciliation_owner_insert_prd074
BEFORE INSERT ON batch_reconciliation_events FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM batch_attempts attempt
  JOIN batch_tasks task ON task.tenant_id=attempt.tenant_id
    AND task.batch_task_id=attempt.batch_task_id
    AND task.batch_job_id=attempt.batch_job_id
  JOIN users actor ON actor.tenant_id=attempt.tenant_id
    AND actor.user_id=NEW.actor_user_id
  WHERE attempt.tenant_id=NEW.tenant_id
    AND attempt.batch_attempt_id=NEW.batch_attempt_id
    AND attempt.batch_task_id=NEW.batch_task_id
    AND attempt.batch_job_id=NEW.batch_job_id
)
BEGIN SELECT RAISE(ABORT,'batch_reconciliation_owner_mismatch'); END;
CREATE TRIGGER trg_batch_reconciliation_update_prd074 BEFORE UPDATE ON batch_reconciliation_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_reconciliation_immutable'); END;
CREATE TRIGGER trg_batch_reconciliation_delete_prd074 BEFORE DELETE ON batch_reconciliation_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_reconciliation_immutable'); END;

CREATE TRIGGER trg_batch_events_create_outbox_prd074
AFTER INSERT ON batch_job_events FOR EACH ROW
BEGIN
  INSERT INTO batch_job_event_outbox(
    batch_job_event_id,tenant_id,dispatch_status,dispatch_attempts,next_attempt_at,
    dispatched_at,claim_token,lease_owner_id,lease_expires_at,
    delivery_snapshot_frozen_at,last_error_code,created_at,updated_at
  ) VALUES(NEW.batch_job_event_id,NEW.tenant_id,'pending',0,NEW.occurred_at,
    NULL,NULL,NULL,NULL,NULL,NULL,NEW.occurred_at,NEW.occurred_at);
END;
CREATE TRIGGER trg_batch_outbox_validate_insert_prd074
BEFORE INSERT ON batch_job_event_outbox FOR EACH ROW
WHEN NOT (
  NEW.dispatch_status='pending' AND NEW.dispatch_attempts=0
  AND NEW.next_attempt_at=NEW.created_at AND NEW.updated_at=NEW.created_at
  AND NEW.dispatched_at IS NULL AND NEW.claim_token IS NULL
  AND NEW.lease_owner_id IS NULL AND NEW.lease_expires_at IS NULL
  AND NEW.delivery_snapshot_frozen_at IS NULL AND NEW.last_error_code IS NULL
  AND EXISTS(SELECT 1 FROM batch_job_events source
    WHERE source.tenant_id=NEW.tenant_id
      AND source.batch_job_event_id=NEW.batch_job_event_id
      AND source.occurred_at=NEW.created_at)
)
BEGIN SELECT RAISE(ABORT,'batch_outbox_invalid'); END;
CREATE TRIGGER trg_batch_outbox_conflict_insert_prd074
BEFORE INSERT ON batch_job_event_outbox FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM batch_job_event_outbox current
  WHERE current.batch_job_event_id=NEW.batch_job_event_id
     OR (current.tenant_id=NEW.tenant_id
       AND current.batch_job_event_id=NEW.batch_job_event_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'batch_outbox_immutable'); END;
CREATE TRIGGER trg_batch_outbox_update_prd074
BEFORE UPDATE ON batch_job_event_outbox FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid AND NEW.batch_job_event_id=OLD.batch_job_event_id
  AND NEW.tenant_id=OLD.tenant_id AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    NEW.dispatch_status NOT IN ('dispatched','failed')
    OR NOT EXISTS(
      SELECT 1 FROM webhook_deliveries delivery
      JOIN batch_job_events source
        ON source.tenant_id=NEW.tenant_id
       AND source.batch_job_event_id=NEW.batch_job_event_id
      WHERE delivery.tenant_id=NEW.tenant_id
        AND delivery.event_id=NEW.batch_job_event_id
        AND delivery.event_type=source.event_type
        AND delivery.delivery_kind='original' AND delivery.status='pending'
    )
  )
  AND (
    (
      ((OLD.dispatch_status='pending'
          AND unixepoch(OLD.next_attempt_at)<=unixepoch(NEW.updated_at))
        OR (OLD.dispatch_status='processing' AND OLD.dispatch_attempts BETWEEN 1 AND 9
          AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)))
      AND NEW.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts+1
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token IS NOT NULL AND length(NEW.claim_token)=32
      AND NEW.claim_token NOT GLOB '*[^0-9a-f]*'
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 128
      AND NEW.lease_owner_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90
      AND NEW.delivery_snapshot_frozen_at IS OLD.delivery_snapshot_frozen_at
      AND NEW.last_error_code IS NULL
    )
    OR (
      OLD.dispatch_status='processing' AND OLD.dispatch_attempts BETWEEN 1 AND 10
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.dispatch_status='failed'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token IS NULL AND NEW.lease_owner_id IS NULL
      AND NEW.lease_expires_at IS NULL
      AND NEW.delivery_snapshot_frozen_at IS OLD.delivery_snapshot_frozen_at
      AND NEW.last_error_code='webhook.payload_invalid'
    )
    OR (
      OLD.dispatch_status='processing' AND NEW.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token=OLD.claim_token AND NEW.lease_owner_id=OLD.lease_owner_id
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.lease_expires_at)>=unixepoch(OLD.lease_expires_at)
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90
      AND NEW.delivery_snapshot_frozen_at IS OLD.delivery_snapshot_frozen_at
      AND NEW.last_error_code IS NULL
    )
    OR (
      OLD.dispatch_status='processing' AND NEW.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token=OLD.claim_token AND NEW.lease_owner_id=OLD.lease_owner_id
      AND OLD.delivery_snapshot_frozen_at IS NULL
      AND NEW.delivery_snapshot_frozen_at=NEW.updated_at
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.lease_expires_at)>=unixepoch(OLD.lease_expires_at)
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+90
      AND NEW.last_error_code IS NULL
    )
    OR (
      OLD.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.claim_token IS NULL AND NEW.lease_owner_id IS NULL
      AND NEW.lease_expires_at IS NULL
      AND NEW.delivery_snapshot_frozen_at IS OLD.delivery_snapshot_frozen_at
      AND (
        (unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
          AND (
            (NEW.dispatch_status='dispatched' AND NEW.dispatched_at=NEW.updated_at
              AND NEW.next_attempt_at IS NULL AND NEW.last_error_code IS NULL)
            OR (OLD.dispatch_attempts<10 AND NEW.dispatch_status='pending'
              AND NEW.dispatched_at IS NULL
              AND NEW.last_error_code='webhook.delivery_unavailable'
              AND unixepoch(NEW.next_attempt_at)=unixepoch(NEW.updated_at)+CASE
                WHEN OLD.dispatch_attempts=1 THEN 5
                WHEN OLD.dispatch_attempts=2 THEN 30
                ELSE 300 END)
            OR (OLD.dispatch_attempts=10 AND NEW.dispatch_status='failed'
              AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
              AND NEW.last_error_code='webhook.delivery_unavailable')
          ))
        OR (OLD.dispatch_attempts=10
          AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)
          AND NEW.dispatch_status='failed' AND NEW.next_attempt_at IS NULL
          AND NEW.dispatched_at IS NULL
          AND NEW.last_error_code='webhook.delivery_unavailable')
      )
    )
  )
)
BEGIN SELECT RAISE(ABORT,'batch_outbox_state_invalid'); END;
CREATE TRIGGER trg_batch_outbox_delete_prd074 BEFORE DELETE ON batch_job_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_outbox_immutable'); END;
CREATE TRIGGER trg_batch_outbox_rowid_prd074
AFTER INSERT ON batch_job_event_outbox FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'batch_outbox_immutable'); END;

DROP TABLE IF EXISTS temp.prd074_up_final_guard;
CREATE TEMP TABLE prd074_up_final_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd074_up_final_guard_ok CHECK(issue_count=0)
);
INSERT INTO prd074_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=64
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=10)=64
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=173
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*64 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=10)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'batch_jobs','batch_tasks','batch_attempts','batch_execution_grants',
      'batch_budget_holds','batch_budget_allocations','batch_scheduler_tenants',
      'batch_job_events','batch_job_event_outbox','batch_reconciliation_events'
    ))=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd074_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
PRAGMA legacy_alter_table = OFF;
