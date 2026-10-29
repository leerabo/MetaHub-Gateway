-- PRD-076 正向迁移：租户隔离的可选 Response Cache、结算事实与恢复型 outbox。
-- 迁移只保存不可逆摘要、密文和安全财务证据；Prompt、Response、Provider Key、Token
-- 与 Credential 明文不得进入任何表、trigger 错误或验收证据。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 首个持久 DDL 前冻结 revision-11 基线。目录或租户投影漂移时必须原子失败，不能在
-- 损坏矩阵上猜测补齐 Response Cache 权限。
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd076_up_guard;
CREATE TEMP TABLE prd076_up_guard(
  issue_count INTEGER NOT NULL
    CONSTRAINT prd076_up_guard_ok CHECK(issue_count=0)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd076_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=66 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates
               WHERE catalog_revision!=11)=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=179 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM rbac_permission_templates
      WHERE permission_code IN(
        'response_cache.read','response_cache.write','response_cache.invalidate'
      )
    ) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*66 FROM tenants) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions
               WHERE catalog_revision!=11 OR status!='active')=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT tenant.tenant_id,template.permission_code
      FROM tenants tenant CROSS JOIN rbac_permission_templates template
      EXCEPT SELECT tenant_id,code FROM permissions
    ) AND NOT EXISTS(
      SELECT tenant_id,code FROM permissions
      EXCEPT
      SELECT tenant.tenant_id,template.permission_code
      FROM tenants tenant CROSS JOIN rbac_permission_templates template
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM roles role
      WHERE role.code='admin'
        AND role.matrix_sha256!='33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148'
    ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'rbac_permission_templates_prd076_new','permissions_prd076_new',
      'usage_ledger_prd076_new','finops_ledger_entries_prd076_old',
      'audit_logs_prd076_new',
      'output_safety_scans_prd076_new','output_safety_events_prd076_new',
      'response_cache_key_state','response_cache_policies',
      'response_cache_policy_events','response_cache_entries',
      'response_cache_entry_tags','response_cache_tag_generations',
      'response_cache_key_generations','response_cache_fill_leases',
      'response_cache_fill_tags','response_cache_request_facts',
      'response_cache_hits','response_cache_invalidations',
      'response_cache_events','response_cache_event_outbox'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd076_up_guard;

-- 只扩展仍保持 revision-11 默认矩阵的角色。finance_admin 的定制矩阵不被迁移覆盖；
-- admin 为不可变完整矩阵，基线漂移已由 preflight 阻断。
CREATE TEMP TABLE prd076_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','finance_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
  END
  AND NOT EXISTS(
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS(
      SELECT 1 FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code
    )
  )
  AND NOT EXISTS(
    SELECT 1 FROM role_permissions relation
    JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS(
        SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code
          AND template.permission_code=permission.code
      )
  );

-- 暂时解除产品目录守卫；所有写入仍在同一 IMMEDIATE 事务内，并在末尾 exact 复核。
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

CREATE TABLE rbac_permission_templates_prd076_new(
  permission_code TEXT PRIMARY KEY CHECK(
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=12)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO rbac_permission_templates_prd076_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,12
FROM rbac_permission_templates;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO rbac_permission_templates_prd076_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('response_cache.read','response_cache','读取响应缓存策略',
   '读取当前租户 Response Cache 的安全策略、状态和节省金额摘要。','read',12),
  ('response_cache.write','response_cache','管理响应缓存策略',
   '创建或修改当前租户 Response Cache 的显式启用、TTL、收费和标签策略。','critical',12),
  ('response_cache.invalidate','response_cache','失效响应缓存',
   '按当前租户的受控选择器推进围栏并主动失效 Response Cache。','critical',12);
DROP TABLE rbac_permission_templates;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE rbac_permission_templates_prd076_new RENAME TO rbac_permission_templates;

-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','response_cache.read',1),
  ('admin','response_cache.write',1),
  ('admin','response_cache.invalidate',1),
  ('finance_admin','response_cache.read',1),
  ('finance_admin','response_cache.write',1),
  ('finance_admin','response_cache.invalidate',1);

CREATE TABLE permissions_prd076_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=12),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO permissions_prd076_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       12,status,created_at,updated_at
FROM permissions;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO permissions_prd076_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       12,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code IN(
    'response_cache.read','response_cache.write','response_cache.invalidate'
  );
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE permissions_prd076_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059
  ON permissions(tenant_id,category,code);

-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd076_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE permission.code IN(
  'response_cache.read','response_cache.write','response_cache.invalidate'
);
-- 正向迁移更新冻结的版本或权限事实，并由终态检查确认没有越权扩散。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN 'cda333482b406b02032f50eba4ee765658b584152f4d9e31f71005b101cfdd01'
      WHEN 'finance_admin' THEN '2348a956d71e8b5cf0f897700c8e91e62dca91070c58010c79330ae60b36fa76'
    END
WHERE role_id IN(SELECT role_id FROM prd076_roles_to_expand);

-- 恢复不可变目录与租户 owner 守卫。
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
  OR NEW.code NOT IN(
    'admin','developer','viewer','finance_admin','security_admin','ops_admin'
  )
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN 'cda333482b406b02032f50eba4ee765658b584152f4d9e31f71005b101cfdd01'
    WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '2348a956d71e8b5cf0f897700c8e91e62dca91070c58010c79330ae60b36fa76'
    WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
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
    ON template.role_code=role.code
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
WHEN EXISTS(SELECT 1 FROM roles role
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id
    AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin'
      WHEN 'developer' THEN 'Developer' WHEN 'viewer' THEN 'Viewer'
      WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin'
      WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常和响应缓存。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。'
    END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN 'cda333482b406b02032f50eba4ee765658b584152f4d9e31f71005b101cfdd01'
      WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '2348a956d71e8b5cf0f897700c8e91e62dca91070c58010c79330ae60b36fa76'
      WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
      WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,
    template.permission_code,template.category,template.name,
    template.description,template.risk_level,12,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  )
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

DROP TABLE prd076_roles_to_expand;

-- 0068 FinOps 表将 CNY 一律视为已归一化；Response Cache 的未定价引用必须保留
-- unavailable 证据，不能被零金额伪装成 priced。先保真复制全部历史账本，再仅扩展
-- response_cache_reference + unpriced-v1 这一种证据组合。
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE finops_ledger_entries
  RENAME TO finops_ledger_entries_prd076_old;
CREATE TABLE finops_ledger_entries (
  ledger_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (ledger_sequence>0),
  finops_ledger_entry_id TEXT NOT NULL UNIQUE CHECK (
    length(finops_ledger_entry_id)=36
    AND substr(finops_ledger_entry_id, 1, 4)='fle_'
    AND substr(finops_ledger_entry_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    length(event_id)=38
    AND substr(event_id, 1, 6)='flevt_'
    AND substr(event_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  entry_type TEXT NOT NULL CHECK (
    entry_type IN (
      'usage_charge', 'manual_debit', 'manual_credit', 'reversal',
      'gift_credit', 'trial_credit'
    )
  ),
  direction TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
  amount_micros INTEGER NOT NULL CHECK (
    typeof(amount_micros)='integer'
    AND amount_micros BETWEEN 0 AND 1000000000000000
  ),
  currency TEXT NOT NULL CHECK (currency IN ('CNY', 'USD')),
  normalized_cny_micros INTEGER CHECK (
    normalized_cny_micros IS NULL
    OR (
      typeof(normalized_cny_micros)='integer'
      AND normalized_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  cny_normalization_status TEXT NOT NULL CHECK (
    cny_normalization_status IN ('normalized', 'unavailable')
  ),
  price_evidence_status TEXT NOT NULL CHECK (
    price_evidence_status IN ('priced', 'unpriced', 'legacy_amount_only', 'not_applicable')
  ),
  price_snapshot_source TEXT,
  posting_source TEXT NOT NULL CHECK (
    posting_source IN ('direct', 'spool_replay', 'legacy_backfill')
  ),
  quota_type TEXT NOT NULL CHECK (quota_type IN ('paid', 'free', 'trial', 'gift')),
  usage_ledger_id TEXT,
  finops_adjustment_id TEXT,
  source_request_id TEXT,
  source_trace_id TEXT,
  operation_request_id TEXT NOT NULL CHECK (length(operation_request_id) BETWEEN 1 AND 128),
  operation_trace_id TEXT NOT NULL CHECK (length(operation_trace_id) BETWEEN 1 AND 128),
  price_snapshot_id TEXT,
  price_schema_version TEXT,
  reversal_of_entry_id TEXT,
  actor_user_id TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(input_tokens)='integer' AND input_tokens>=0
  ),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(output_tokens)='integer' AND output_tokens>=0
  ),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(total_tokens)='integer' AND total_tokens>=0
  ),
  cache_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(cache_tokens)='integer' AND cache_tokens>=0
  ),
  model_id TEXT,
  provider_id TEXT,
  virtual_key_id TEXT,
  environment_id TEXT,
  project_id TEXT,
  application_id TEXT,
  estimated INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1)),
  status TEXT NOT NULL DEFAULT 'posted' CHECK (status='posted'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, usage_ledger_id)
    REFERENCES usage_ledger(tenant_id, usage_ledger_id),
  FOREIGN KEY (tenant_id, finops_adjustment_id)
    REFERENCES finops_adjustments(tenant_id, finops_adjustment_id),
  FOREIGN KEY (tenant_id, source_request_id)
    REFERENCES request_logs(tenant_id, request_id),
  FOREIGN KEY (tenant_id, price_snapshot_id)
    REFERENCES price_snapshots(tenant_id, price_snapshot_id),
  FOREIGN KEY (tenant_id, reversal_of_entry_id)
    REFERENCES finops_ledger_entries(tenant_id, finops_ledger_entry_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, provider_id) REFERENCES providers(tenant_id, provider_id),
  FOREIGN KEY (tenant_id, virtual_key_id) REFERENCES virtual_keys(tenant_id, virtual_key_id),
  FOREIGN KEY (tenant_id, environment_id)
    REFERENCES environments(tenant_id, environment_id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects(tenant_id, project_id),
  FOREIGN KEY (tenant_id, application_id)
    REFERENCES applications(tenant_id, application_id),
  UNIQUE (tenant_id, finops_ledger_entry_id),
  UNIQUE (tenant_id, event_id),
  UNIQUE (tenant_id, usage_ledger_id),
  UNIQUE (tenant_id, finops_adjustment_id),
  CHECK (created_at=updated_at),
  CHECK (entry_type='usage_charge' OR amount_micros>0),
  CHECK (
    (entry_type IN ('usage_charge', 'manual_debit') AND direction='debit')
    OR (entry_type IN ('manual_credit', 'gift_credit', 'trial_credit') AND direction='credit')
    OR entry_type='reversal'
  ),
  CHECK (
    (currency='CNY'
      AND price_evidence_status='unpriced'
      AND price_snapshot_source='response_cache_reference'
      AND price_schema_version='unpriced-v1'
      AND amount_micros=0
      AND cny_normalization_status='unavailable'
      AND normalized_cny_micros IS NULL)
    OR (currency='CNY'
      AND NOT (
        price_evidence_status='unpriced'
        AND price_snapshot_source='response_cache_reference'
        AND price_schema_version='unpriced-v1'
      )
      AND cny_normalization_status='normalized'
      AND normalized_cny_micros=amount_micros)
    OR (currency='USD' AND cny_normalization_status='unavailable'
      AND normalized_cny_micros IS NULL)
  ),
  CHECK (
    (entry_type='usage_charge' AND usage_ledger_id IS NOT NULL
      AND finops_adjustment_id IS NULL AND actor_user_id IS NULL
      AND source_request_id IS NOT NULL AND source_trace_id IS NOT NULL)
    OR (entry_type!='usage_charge' AND usage_ledger_id IS NULL
      AND finops_adjustment_id IS NOT NULL AND actor_user_id IS NOT NULL)
  ),
  CHECK (
    (entry_type='reversal' AND reversal_of_entry_id IS NOT NULL)
    OR (entry_type!='reversal' AND reversal_of_entry_id IS NULL)
  ),
  CHECK (
    (entry_type='usage_charge' AND quota_type='paid')
    OR (entry_type IN ('manual_debit', 'manual_credit') AND quota_type='paid')
    OR (entry_type='gift_credit' AND quota_type='gift')
    OR (entry_type='trial_credit' AND quota_type='trial')
    OR entry_type='reversal'
  ),
  CHECK (
    (price_evidence_status='not_applicable'
      AND price_snapshot_id IS NULL AND price_schema_version IS NULL
      AND price_snapshot_source IS NULL)
    OR (price_evidence_status='unpriced'
      AND price_snapshot_id IS NOT NULL AND price_schema_version='unpriced-v1'
      AND price_snapshot_source IN ('finops_unpriced', 'response_cache_reference'))
    OR (price_evidence_status='legacy_amount_only'
      AND price_snapshot_id IS NOT NULL
      AND price_schema_version='legacy-unversioned-v1'
      AND price_snapshot_source='finops_legacy_backfill')
    OR (price_evidence_status='priced'
      AND price_snapshot_id IS NOT NULL
      AND NULLIF(trim(price_schema_version), '') IS NOT NULL
      AND NULLIF(trim(price_snapshot_source), '') IS NOT NULL
      AND price_snapshot_source NOT IN ('finops_unpriced', 'finops_legacy_backfill')
      AND NOT (
        price_snapshot_source='response_cache_reference'
        AND price_schema_version='unpriced-v1'
      ))
  ),
  CHECK (entry_type!='usage_charge' OR price_evidence_status!='not_applicable'),
  CHECK (
    entry_type='usage_charge'
    OR entry_type='reversal'
    OR price_evidence_status='not_applicable'
  ),
  CHECK (
    entry_type='usage_charge'
    OR (
      input_tokens=0 AND output_tokens=0 AND total_tokens=0
      AND cache_tokens=0 AND estimated=0
    )
  )
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO finops_ledger_entries(
  ledger_sequence,finops_ledger_entry_id,event_id,tenant_id,entry_type,direction,
  amount_micros,currency,normalized_cny_micros,cny_normalization_status,
  price_evidence_status,price_snapshot_source,posting_source,quota_type,
  usage_ledger_id,finops_adjustment_id,source_request_id,source_trace_id,
  operation_request_id,operation_trace_id,price_snapshot_id,price_schema_version,
  reversal_of_entry_id,actor_user_id,input_tokens,output_tokens,total_tokens,
  cache_tokens,model_id,provider_id,virtual_key_id,environment_id,project_id,
  application_id,estimated,status,created_at,updated_at
)
SELECT
  ledger_sequence,finops_ledger_entry_id,event_id,tenant_id,entry_type,direction,
  amount_micros,currency,normalized_cny_micros,cny_normalization_status,
  price_evidence_status,price_snapshot_source,posting_source,quota_type,
  usage_ledger_id,finops_adjustment_id,source_request_id,source_trace_id,
  operation_request_id,operation_trace_id,price_snapshot_id,price_schema_version,
  reversal_of_entry_id,actor_user_id,input_tokens,output_tokens,total_tokens,
  cache_tokens,model_id,provider_id,virtual_key_id,environment_id,project_id,
  application_id,estimated,status,created_at,updated_at
FROM finops_ledger_entries_prd076_old
ORDER BY ledger_sequence;
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd076_finops_copy_guard;
CREATE TEMP TABLE prd076_finops_copy_guard(
  issue_count INTEGER NOT NULL
    CONSTRAINT prd076_finops_copy_guard_ok CHECK(issue_count=0)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd076_finops_copy_guard(issue_count)
SELECT ABS(
    (SELECT COUNT(*) FROM finops_ledger_entries_prd076_old)
    -(SELECT COUNT(*) FROM finops_ledger_entries)
  )
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM finops_ledger_entries_prd076_old
      EXCEPT SELECT rowid,* FROM finops_ledger_entries
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM finops_ledger_entries
      EXCEPT SELECT rowid,* FROM finops_ledger_entries_prd076_old
    ));
DROP TABLE prd076_finops_copy_guard;
DROP TABLE finops_ledger_entries_prd076_old;

CREATE INDEX idx_finops_ledger_tenant_sequence
  ON finops_ledger_entries(tenant_id, ledger_sequence DESC);
CREATE INDEX idx_finops_ledger_tenant_time
  ON finops_ledger_entries(tenant_id, created_at DESC, ledger_sequence DESC);
CREATE INDEX idx_finops_ledger_source_request
  ON finops_ledger_entries(tenant_id, source_request_id, ledger_sequence DESC);
CREATE INDEX idx_finops_ledger_price_snapshot
  ON finops_ledger_entries(tenant_id, price_snapshot_id, ledger_sequence DESC);
CREATE INDEX idx_finops_ledger_reversal_target
  ON finops_ledger_entries(tenant_id, reversal_of_entry_id, ledger_sequence DESC)
  WHERE reversal_of_entry_id IS NOT NULL;
CREATE INDEX idx_finops_ledger_type_direction
  ON finops_ledger_entries(tenant_id, entry_type, direction, ledger_sequence DESC);
CREATE INDEX idx_finops_budget_tenant_usage_prd052
  ON finops_ledger_entries(
    tenant_id, environment_id, created_at, ledger_sequence
  )
  WHERE entry_type='usage_charge' AND direction='debit' AND status='posted';
CREATE INDEX idx_finops_budget_project_usage_prd052
  ON finops_ledger_entries(
    tenant_id, environment_id, project_id, created_at, ledger_sequence
  )
  WHERE entry_type='usage_charge' AND direction='debit' AND status='posted';
CREATE INDEX idx_finops_budget_application_usage_prd052
  ON finops_ledger_entries(
    tenant_id, environment_id, application_id, created_at, ledger_sequence
  )
  WHERE entry_type='usage_charge' AND direction='debit' AND status='posted';
CREATE INDEX idx_finops_budget_key_usage_prd052
  ON finops_ledger_entries(
    tenant_id, environment_id, virtual_key_id, created_at, ledger_sequence
  )
  WHERE entry_type='usage_charge' AND direction='debit' AND status='posted';
CREATE UNIQUE INDEX idx_finops_ledger_request_owner_prd071
  ON finops_ledger_entries(tenant_id,source_request_id,finops_ledger_entry_id);

CREATE TRIGGER trg_finops_ledger_owner_insert_prd051
BEFORE INSERT ON finops_ledger_entries
FOR EACH ROW
WHEN (
  NEW.entry_type='usage_charge'
  AND NOT EXISTS (
    SELECT 1
    FROM usage_ledger usage
    JOIN request_logs request
      ON request.tenant_id=usage.tenant_id AND request.request_id=usage.request_id
    JOIN price_snapshots snapshot
      ON snapshot.tenant_id=usage.tenant_id
     AND snapshot.price_snapshot_id=NEW.price_snapshot_id
     AND snapshot.request_id=usage.request_id
    WHERE usage.tenant_id=NEW.tenant_id
      AND usage.usage_ledger_id=NEW.usage_ledger_id
      AND usage.request_id=NEW.source_request_id
      AND request.trace_id=NEW.source_trace_id
      AND NEW.operation_request_id=NEW.source_request_id
      AND NEW.operation_trace_id=NEW.source_trace_id
      AND snapshot.currency=NEW.currency
      AND snapshot.price_schema_version=NEW.price_schema_version
      AND snapshot.source=NEW.price_snapshot_source
      AND (
        usage.price_snapshot_id=snapshot.price_snapshot_id
        OR (
          snapshot.source IN ('finops_unpriced', 'finops_legacy_backfill')
          AND json_extract(
            snapshot.billing_dimensions, '$.finops_usage_ledger_id'
          )=usage.usage_ledger_id
        )
      )
      AND NEW.amount_micros=(
        SELECT
          CAST(parts.integer_text AS INTEGER) * 1000000
          + CASE WHEN parts.fraction_text=''
              THEN 0
              ELSE CAST(substr(parts.fraction_text || '000000', 1, 6) AS INTEGER)
            END
        FROM (
          SELECT
            CASE WHEN instr(value.amount_text, '.')=0
              THEN value.amount_text
              ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1)
            END AS integer_text,
            CASE WHEN instr(value.amount_text, '.')=0
              THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1)
            END AS fraction_text
          FROM (
            SELECT CASE
              WHEN json_type(usage.normalized_usage, '$.cost_amount')='text'
                THEN json_extract(usage.normalized_usage, '$.cost_amount')
              ELSE printf('%.6f', usage.cost_amount)
            END AS amount_text
          ) value
        ) parts
      )
      AND NEW.input_tokens=usage.input_tokens
      AND NEW.output_tokens=usage.output_tokens
      AND NEW.total_tokens=usage.total_tokens
      AND NEW.cache_tokens=usage.cache_tokens
      AND NEW.model_id IS usage.model_id
      AND NEW.provider_id IS usage.provider_id
      AND NEW.virtual_key_id IS usage.virtual_key_id
      AND NEW.environment_id IS usage.environment_id
      AND NEW.project_id IS usage.project_id
      AND NEW.application_id IS usage.application_id
      AND NEW.estimated=usage.estimated
      AND NEW.price_evidence_status=CASE
        WHEN snapshot.source='finops_unpriced' THEN 'unpriced'
        WHEN snapshot.source='finops_legacy_backfill' THEN 'legacy_amount_only'
        WHEN snapshot.source='response_cache_reference'
          AND snapshot.price_schema_version='unpriced-v1' THEN 'unpriced'
        ELSE 'priced'
      END
  )
)
OR (
  NEW.entry_type!='usage_charge'
  AND NOT EXISTS (
    SELECT 1
    FROM finops_adjustments adjustment
    LEFT JOIN request_logs request
      ON request.tenant_id=adjustment.tenant_id
     AND request.request_id=adjustment.related_request_id
    LEFT JOIN finops_ledger_entries target
      ON target.tenant_id=adjustment.tenant_id
     AND target.finops_ledger_entry_id=adjustment.reversal_of_entry_id
    LEFT JOIN finops_ledger_entries related_usage
      ON related_usage.tenant_id=adjustment.tenant_id
     AND related_usage.entry_type='usage_charge'
     AND related_usage.source_request_id=adjustment.related_request_id
    WHERE adjustment.tenant_id=NEW.tenant_id
      AND adjustment.finops_adjustment_id=NEW.finops_adjustment_id
      AND adjustment.adjustment_type=NEW.entry_type
      AND adjustment.amount_micros=NEW.amount_micros
      AND adjustment.currency=NEW.currency
      AND adjustment.actor_user_id=NEW.actor_user_id
      AND adjustment.operation_request_id=NEW.operation_request_id
      AND adjustment.operation_trace_id=NEW.operation_trace_id
      AND adjustment.reversal_of_entry_id IS NEW.reversal_of_entry_id
      AND (
        (
          adjustment.adjustment_type='reversal'
          AND target.source_request_id IS NEW.source_request_id
          AND target.source_trace_id IS NEW.source_trace_id
        )
        OR (
          adjustment.adjustment_type!='reversal'
          AND adjustment.related_request_id IS NEW.source_request_id
          AND request.trace_id IS NEW.source_trace_id
        )
      )
      AND NEW.posting_source='direct'
      AND (
        (adjustment.adjustment_type='manual_debit' AND NEW.direction='debit')
        OR (adjustment.adjustment_type IN (
          'manual_credit', 'gift_credit', 'trial_credit'
        ) AND NEW.direction='credit')
        OR (
          adjustment.adjustment_type='reversal'
          AND target.entry_type!='reversal'
          AND NEW.direction=CASE target.direction WHEN 'debit' THEN 'credit' ELSE 'debit' END
          AND NEW.quota_type=target.quota_type
          AND NEW.price_evidence_status=target.price_evidence_status
          AND NEW.price_snapshot_source IS target.price_snapshot_source
          AND NEW.price_snapshot_id IS target.price_snapshot_id
          AND NEW.price_schema_version IS target.price_schema_version
          AND NEW.model_id IS target.model_id
          AND NEW.provider_id IS target.provider_id
          AND NEW.virtual_key_id IS target.virtual_key_id
          AND NEW.environment_id IS target.environment_id
          AND NEW.project_id IS target.project_id
          AND NEW.application_id IS target.application_id
        )
      )
      AND (
        adjustment.adjustment_type='reversal'
        OR (
          NEW.price_evidence_status='not_applicable'
          AND (
            (adjustment.related_request_id IS NULL
              AND NEW.model_id IS NULL AND NEW.provider_id IS NULL
              AND NEW.virtual_key_id IS NULL AND NEW.environment_id IS NULL
              AND NEW.project_id IS NULL AND NEW.application_id IS NULL)
            OR (
              adjustment.related_request_id IS NOT NULL
              AND NEW.model_id IS related_usage.model_id
              AND NEW.provider_id IS related_usage.provider_id
              AND NEW.virtual_key_id IS related_usage.virtual_key_id
              AND NEW.environment_id IS related_usage.environment_id
              AND NEW.project_id IS related_usage.project_id
              AND NEW.application_id IS related_usage.application_id
            )
          )
        )
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_owner_mismatch');
END;
CREATE TRIGGER trg_finops_ledger_conflict_insert_prd051
BEFORE INSERT ON finops_ledger_entries
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM finops_ledger_entries current
  WHERE current.finops_ledger_entry_id=NEW.finops_ledger_entry_id
     OR current.event_id=NEW.event_id
     OR (NEW.ledger_sequence!=-1 AND current.ledger_sequence=NEW.ledger_sequence)
     OR (
       NEW.usage_ledger_id IS NOT NULL
       AND current.tenant_id=NEW.tenant_id
       AND current.usage_ledger_id=NEW.usage_ledger_id
     )
     OR (
       NEW.finops_adjustment_id IS NOT NULL
       AND current.tenant_id=NEW.tenant_id
       AND current.finops_adjustment_id=NEW.finops_adjustment_id
     )
)
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_append_only');
END;
CREATE TRIGGER trg_finops_ledger_immutable_update_prd051
BEFORE UPDATE ON finops_ledger_entries
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_append_only');
END;
CREATE TRIGGER trg_finops_ledger_immutable_delete_prd051
BEFORE DELETE ON finops_ledger_entries
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_append_only');
END;
CREATE TRIGGER trg_finops_experiment_settlement_prd075
AFTER INSERT ON finops_ledger_entries FOR EACH ROW
WHEN NEW.entry_type='usage_charge' AND NEW.direction='debit'
  AND NEW.quota_type='paid' AND NEW.status='posted'
  AND NEW.posting_source IN ('direct','spool_replay')
BEGIN
  -- 迁移 experiment_usage_settlements 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  UPDATE experiment_usage_settlements
  SET status='recorded',usage_ledger_id=NEW.usage_ledger_id,
      finops_ledger_entry_id=NEW.finops_ledger_entry_id,
      gross_cny_micros=NEW.normalized_cny_micros,
      normalization_status=CASE NEW.cny_normalization_status
        WHEN 'normalized' THEN 'normalized' ELSE 'unavailable' END,
      settlement_reason='usage_charge_recorded',settled_at=NEW.created_at,
      updated_at=NEW.created_at
  WHERE status='pending' AND tenant_id=NEW.tenant_id
    AND EXISTS(
      SELECT 1 FROM experiment_assignments assignment
      JOIN model_experiments experiment
        ON experiment.tenant_id=assignment.tenant_id
       AND experiment.experiment_id=assignment.experiment_id
      WHERE assignment.tenant_id=NEW.tenant_id
        AND assignment.assignment_id=experiment_usage_settlements.assignment_id
        AND assignment.request_id=NEW.source_request_id
        AND assignment.model_id=NEW.model_id AND assignment.provider_id=NEW.provider_id
        AND experiment.status!='completed'
    );
END;

-- 单例 key state 只保存版本与非敏感 fingerprint。全零 fingerprint 是“尚未配置”的迁移
-- 默认值；受信初始化/轮换必须在 EXCLUSIVE 事务中清空派生数据并推进 generation。
CREATE TABLE response_cache_key_state(
  key_state_id INTEGER PRIMARY KEY CHECK(key_state_id=1),
  key_schema_version INTEGER NOT NULL CHECK(
    typeof(key_schema_version)='integer' AND key_schema_version=1
  ),
  active_hmac_key_version INTEGER NOT NULL CHECK(
    typeof(active_hmac_key_version)='integer' AND active_hmac_key_version>=1
  ),
  active_encryption_key_version INTEGER NOT NULL CHECK(
    typeof(active_encryption_key_version)='integer'
    AND active_encryption_key_version>=1
  ),
  hmac_secret_fingerprint TEXT NOT NULL CHECK(
    length(hmac_secret_fingerprint)=64
    AND hmac_secret_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  encryption_secret_fingerprint TEXT NOT NULL CHECK(
    length(encryption_secret_fingerprint)=64
    AND encryption_secret_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  generation INTEGER NOT NULL CHECK(
    typeof(generation)='integer' AND generation>=1
  ),
  updated_at TEXT NOT NULL CHECK(
    length(updated_at) BETWEEN 1 AND 64 AND unixepoch(updated_at) IS NOT NULL
  )
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO response_cache_key_state(
  key_state_id,key_schema_version,active_hmac_key_version,
  active_encryption_key_version,hmac_secret_fingerprint,
  encryption_secret_fingerprint,generation,updated_at
) VALUES(
  1,1,1,1,
  '0000000000000000000000000000000000000000000000000000000000000000',
  '0000000000000000000000000000000000000000000000000000000000000000',
  1,strftime('%Y-%m-%dT%H:%M:%SZ','now')
);
CREATE TRIGGER trg_response_cache_key_state_insert_prd076
BEFORE INSERT ON response_cache_key_state FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_key_state_singleton'); END;
CREATE TRIGGER trg_response_cache_key_state_delete_prd076
BEFORE DELETE ON response_cache_key_state FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_key_state_singleton'); END;
CREATE TRIGGER trg_response_cache_key_state_update_prd076
BEFORE UPDATE ON response_cache_key_state FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.key_state_id!=OLD.key_state_id
  OR NEW.key_schema_version!=OLD.key_schema_version
  OR NEW.active_hmac_key_version<OLD.active_hmac_key_version
  OR NEW.active_encryption_key_version<OLD.active_encryption_key_version
  OR NEW.generation!=OLD.generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR (
    NEW.active_hmac_key_version=OLD.active_hmac_key_version
    AND NEW.active_encryption_key_version=OLD.active_encryption_key_version
    AND NEW.hmac_secret_fingerprint=OLD.hmac_secret_fingerprint
    AND NEW.encryption_secret_fingerprint=OLD.encryption_secret_fingerprint
  )
BEGIN SELECT RAISE(ABORT,'response_cache_key_state_transition_invalid'); END;

-- 每个 Alias 至多一个策略。enabled 策略必须由当前更新 actor 显式确认单一 data class，
-- disabled 策略不得保留旧确认事实。tags_json 只接受去重的 machine-code 数组。
CREATE TABLE response_cache_policies(
  cache_policy_id TEXT PRIMARY KEY CHECK(
    length(cache_policy_id)=36 AND substr(cache_policy_id,1,4)='rcp_'
    AND substr(cache_policy_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('enabled','disabled')),
  data_class TEXT CHECK(
    data_class IS NULL OR (
      length(data_class) BETWEEN 1 AND 64 AND data_class=lower(data_class)
      AND data_class NOT GLOB '*[^a-z0-9._-]*'
    )
  ),
  ttl_seconds INTEGER NOT NULL CHECK(
    typeof(ttl_seconds)='integer' AND ttl_seconds BETWEEN 60 AND 86400
  ),
  charge_cache_hits INTEGER NOT NULL CHECK(charge_cache_hits IN (0,1)),
  tags_json TEXT NOT NULL CHECK(
    json_valid(tags_json) AND json_type(tags_json)='array'
    AND json_array_length(tags_json) BETWEEN 0 AND 16
  ),
  single_data_class_confirmed INTEGER NOT NULL CHECK(
    single_data_class_confirmed IN (0,1)
  ),
  data_class_confirmed_by TEXT,
  data_class_confirmed_version INTEGER CHECK(
    data_class_confirmed_version IS NULL OR (
      typeof(data_class_confirmed_version)='integer'
      AND data_class_confirmed_version>=1
    )
  ),
  version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>=1),
  invalidation_generation INTEGER NOT NULL CHECK(
    typeof(invalidation_generation)='integer' AND invalidation_generation>=1
  ),
  created_by TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(
    unixepoch(updated_at) IS NOT NULL
    AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  UNIQUE(tenant_id,model_alias_id),
  UNIQUE(tenant_id,cache_policy_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  CHECK(
    (status='enabled' AND data_class IS NOT NULL
      AND single_data_class_confirmed=1
      AND data_class_confirmed_by=updated_by
      AND data_class_confirmed_version=version)
    OR
    (status='disabled' AND single_data_class_confirmed=0
      AND data_class_confirmed_by IS NULL
      AND data_class_confirmed_version IS NULL)
  )
);
CREATE INDEX idx_response_cache_policies_tenant_status_prd076
  ON response_cache_policies(tenant_id,status,updated_at,cache_policy_id);
CREATE TRIGGER trg_response_cache_policies_owner_insert_prd076
BEFORE INSERT ON response_cache_policies FOR EACH ROW
WHEN NEW.version!=1 OR NEW.invalidation_generation!=1
  OR NOT EXISTS(
    SELECT 1 FROM model_aliases alias
    WHERE alias.tenant_id=NEW.tenant_id
      AND alias.model_alias_id=NEW.model_alias_id
  )
  OR NOT EXISTS(
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.created_by
      AND actor.status='active'
  )
  OR NOT EXISTS(
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.updated_by
      AND actor.status='active'
  )
  OR NEW.created_by!=NEW.updated_by
  OR EXISTS(
    SELECT 1 FROM json_each(NEW.tags_json) tag
    WHERE tag.type!='text' OR length(tag.value) NOT BETWEEN 1 AND 64
      OR tag.value!=lower(tag.value) OR tag.value GLOB '*[^a-z0-9._-]*'
  )
  OR (SELECT COUNT(*) FROM json_each(NEW.tags_json))
     !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.tags_json))
  OR EXISTS(
    SELECT 1
    FROM json_each(NEW.tags_json) current_tag
    JOIN json_each(NEW.tags_json) next_tag
      ON next_tag.key=current_tag.key+1
    WHERE current_tag.value>=next_tag.value
  )
  OR EXISTS(
    SELECT 1 FROM response_cache_policies current
    WHERE current.cache_policy_id=NEW.cache_policy_id
      OR (current.tenant_id=NEW.tenant_id
          AND current.model_alias_id=NEW.model_alias_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'response_cache_policy_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_policies_update_prd076
BEFORE UPDATE ON response_cache_policies FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.cache_policy_id!=OLD.cache_policy_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.model_alias_id!=OLD.model_alias_id
  OR NEW.created_by!=OLD.created_by OR NEW.created_at!=OLD.created_at
  OR NEW.version!=OLD.version+1
  OR NEW.invalidation_generation!=OLD.invalidation_generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT EXISTS(
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.updated_by
      AND actor.status='active'
  )
  OR EXISTS(
    SELECT 1 FROM json_each(NEW.tags_json) tag
    WHERE tag.type!='text' OR length(tag.value) NOT BETWEEN 1 AND 64
      OR tag.value!=lower(tag.value) OR tag.value GLOB '*[^a-z0-9._-]*'
  )
  OR (SELECT COUNT(*) FROM json_each(NEW.tags_json))
     !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.tags_json))
  OR EXISTS(
    SELECT 1
    FROM json_each(NEW.tags_json) current_tag
    JOIN json_each(NEW.tags_json) next_tag
      ON next_tag.key=current_tag.key+1
    WHERE current_tag.value>=next_tag.value
  )
  OR (
    NEW.status=OLD.status AND NEW.data_class IS OLD.data_class
    AND NEW.ttl_seconds=OLD.ttl_seconds
    AND NEW.charge_cache_hits=OLD.charge_cache_hits
    AND NEW.tags_json=OLD.tags_json
    AND NEW.single_data_class_confirmed=OLD.single_data_class_confirmed
    AND NEW.data_class_confirmed_by IS OLD.data_class_confirmed_by
    AND NEW.data_class_confirmed_version IS OLD.data_class_confirmed_version
  )
BEGIN SELECT RAISE(ABORT,'response_cache_policy_transition_invalid'); END;
CREATE TRIGGER trg_response_cache_policies_delete_prd076
BEFORE DELETE ON response_cache_policies FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_policy_immutable'); END;

-- policy_noop 保存幂等重放快照，但必须镜像当前 version，且所有变化/失效计数为零。
CREATE TABLE response_cache_policy_events(
  response_cache_policy_event_id TEXT PRIMARY KEY CHECK(
    length(response_cache_policy_event_id)=39
    AND substr(response_cache_policy_event_id,1,7)='rcpevt_'
    AND substr(response_cache_policy_event_id,8) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  cache_policy_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  idempotency_hmac_version INTEGER NOT NULL CHECK(
    typeof(idempotency_hmac_version)='integer' AND idempotency_hmac_version>=1
  ),
  idempotency_key_hmac TEXT NOT NULL CHECK(
    length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'
  ),
  request_fingerprint TEXT NOT NULL CHECK(
    length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  event_type TEXT NOT NULL CHECK(
    event_type IN ('policy_created','policy_updated','policy_noop')
  ),
  before_json TEXT NOT NULL CHECK(
    json_valid(before_json) AND json_type(before_json)='object'
  ),
  after_json TEXT NOT NULL CHECK(
    json_valid(after_json) AND json_type(after_json)='object'
  ),
  response_json TEXT NOT NULL CHECK(
    json_valid(response_json) AND json_type(response_json)='object'
  ),
  result_version INTEGER NOT NULL CHECK(
    typeof(result_version)='integer' AND result_version>=1
  ),
  changed INTEGER NOT NULL CHECK(changed IN (0,1)),
  invalidated_entry_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(invalidated_entry_count)='integer' AND invalidated_entry_count>=0
  ),
  fenced_fill_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(fenced_fill_count)='integer' AND fenced_fill_count>=0
  ),
  operation_request_id TEXT NOT NULL CHECK(
    length(operation_request_id) BETWEEN 1 AND 128
    AND instr(operation_request_id,char(0))=0
  ),
  operation_trace_id TEXT NOT NULL CHECK(
    length(operation_trace_id) BETWEEN 1 AND 128
    AND instr(operation_trace_id,char(0))=0
  ),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(tenant_id,response_cache_policy_event_id),
  UNIQUE(
    tenant_id,actor_user_id,idempotency_hmac_version,idempotency_key_hmac
  ),
  FOREIGN KEY(tenant_id,cache_policy_id)
    REFERENCES response_cache_policies(tenant_id,cache_policy_id),
  CHECK(
    (event_type='policy_noop' AND changed=0
      AND invalidated_entry_count=0 AND fenced_fill_count=0
      AND before_json=after_json)
    OR (event_type IN ('policy_created','policy_updated') AND changed=1)
  ),
  CHECK(event_type!='policy_created' OR before_json='{}')
);
CREATE INDEX idx_response_cache_policy_events_owner_prd076
  ON response_cache_policy_events(
    tenant_id,cache_policy_id,created_at,response_cache_policy_event_id
  );
CREATE TRIGGER trg_response_cache_policy_events_owner_insert_prd076
BEFORE INSERT ON response_cache_policy_events FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM response_cache_policies policy
    JOIN users actor ON actor.tenant_id=policy.tenant_id
      AND actor.user_id=NEW.actor_user_id AND actor.status='active'
    WHERE policy.tenant_id=NEW.tenant_id
      AND policy.cache_policy_id=NEW.cache_policy_id
      AND policy.version=NEW.result_version
      AND (
        (NEW.event_type='policy_created' AND policy.version=1
          AND policy.created_by=NEW.actor_user_id)
        OR (NEW.event_type='policy_updated'
          AND policy.updated_by=NEW.actor_user_id)
        OR NEW.event_type='policy_noop'
      )
  )
  OR EXISTS(
    SELECT 1 FROM response_cache_policy_events current
    WHERE current.response_cache_policy_event_id
            =NEW.response_cache_policy_event_id
      OR (current.tenant_id=NEW.tenant_id
          AND current.actor_user_id=NEW.actor_user_id
          AND current.idempotency_hmac_version=NEW.idempotency_hmac_version
          AND current.idempotency_key_hmac=NEW.idempotency_key_hmac)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
  OR EXISTS(
    SELECT 1 FROM json_each(NEW.response_json) field
    WHERE field.key NOT IN(
      'tenant_id','cache_policy','idempotent_replay','request_id','trace_id'
    )
  )
BEGIN SELECT RAISE(ABORT,'response_cache_policy_event_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_policy_events_update_prd076
BEFORE UPDATE ON response_cache_policy_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_policy_event_immutable'); END;
CREATE TRIGGER trg_response_cache_policy_events_delete_prd076
BEFORE DELETE ON response_cache_policy_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_policy_event_immutable'); END;

-- Tag/key generation 都是单调围栏。摘要固定 64 位小写十六进制，不保存 tag value、
-- Prompt hash 或 cache canonical body。
CREATE TABLE response_cache_tag_generations(
  tenant_id TEXT NOT NULL,
  hmac_key_version INTEGER NOT NULL CHECK(
    typeof(hmac_key_version)='integer' AND hmac_key_version>=1
  ),
  tag_kind TEXT NOT NULL CHECK(tag_kind IN(
    'policy','model_alias','model','virtual_key','data_class','tag'
  )),
  tag_digest TEXT NOT NULL CHECK(
    length(tag_digest)=64 AND tag_digest NOT GLOB '*[^0-9a-f]*'
  ),
  generation INTEGER NOT NULL CHECK(
    typeof(generation)='integer' AND generation>=1
  ),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(tenant_id,hmac_key_version,tag_kind,tag_digest),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
) WITHOUT ROWID;
CREATE TRIGGER trg_response_cache_tag_generations_insert_prd076
BEFORE INSERT ON response_cache_tag_generations FOR EACH ROW
WHEN NEW.generation!=1 OR NOT EXISTS(
  SELECT 1 FROM response_cache_key_state state
  WHERE state.key_state_id=1 AND state.active_hmac_key_version=NEW.hmac_key_version
)
BEGIN SELECT RAISE(ABORT,'response_cache_tag_generation_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_tag_generations_update_prd076
BEFORE UPDATE ON response_cache_tag_generations FOR EACH ROW
WHEN NEW.tenant_id!=OLD.tenant_id
  OR NEW.hmac_key_version!=OLD.hmac_key_version
  OR NEW.tag_kind!=OLD.tag_kind OR NEW.tag_digest!=OLD.tag_digest
  OR NEW.generation!=OLD.generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'response_cache_tag_generation_transition_invalid'); END;
CREATE TRIGGER trg_response_cache_tag_generations_delete_prd076
BEFORE DELETE ON response_cache_tag_generations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_tag_generation_immutable'); END;

CREATE TABLE response_cache_key_generations(
  tenant_id TEXT NOT NULL,
  hmac_key_version INTEGER NOT NULL CHECK(
    typeof(hmac_key_version)='integer' AND hmac_key_version>=1
  ),
  cache_identity_digest TEXT NOT NULL CHECK(
    length(cache_identity_digest)=64
    AND cache_identity_digest NOT GLOB '*[^0-9a-f]*'
  ),
  generation INTEGER NOT NULL CHECK(
    typeof(generation)='integer' AND generation>=1
  ),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(tenant_id,hmac_key_version,cache_identity_digest),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
) WITHOUT ROWID;
CREATE TRIGGER trg_response_cache_key_generations_insert_prd076
BEFORE INSERT ON response_cache_key_generations FOR EACH ROW
WHEN NEW.generation!=1 OR NOT EXISTS(
  SELECT 1 FROM response_cache_key_state state
  WHERE state.key_state_id=1 AND state.active_hmac_key_version=NEW.hmac_key_version
)
BEGIN SELECT RAISE(ABORT,'response_cache_key_generation_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_key_generations_update_prd076
BEFORE UPDATE ON response_cache_key_generations FOR EACH ROW
WHEN NEW.tenant_id!=OLD.tenant_id
  OR NEW.hmac_key_version!=OLD.hmac_key_version
  OR NEW.cache_identity_digest!=OLD.cache_identity_digest
  OR NEW.generation!=OLD.generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'response_cache_key_generation_transition_invalid'); END;
CREATE TRIGGER trg_response_cache_key_generations_delete_prd076
BEFORE DELETE ON response_cache_key_generations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_key_generation_immutable'); END;

CREATE TABLE response_cache_entries(
  response_cache_entry_id TEXT PRIMARY KEY CHECK(
    length(response_cache_entry_id)=36
    AND substr(response_cache_entry_id,1,4)='rce_'
    AND substr(response_cache_entry_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  cache_policy_id TEXT NOT NULL,
  cache_policy_version INTEGER NOT NULL CHECK(cache_policy_version>=1),
  policy_invalidation_generation INTEGER NOT NULL CHECK(
    typeof(policy_invalidation_generation)='integer'
    AND policy_invalidation_generation>=1
  ),
  key_schema_version INTEGER NOT NULL CHECK(key_schema_version=1),
  hmac_key_version INTEGER NOT NULL CHECK(hmac_key_version>=1),
  encryption_key_version INTEGER NOT NULL CHECK(encryption_key_version>=1),
  cache_identity_digest TEXT NOT NULL CHECK(
    length(cache_identity_digest)=64
    AND cache_identity_digest NOT GLOB '*[^0-9a-f]*'
  ),
  key_generation INTEGER NOT NULL CHECK(
    typeof(key_generation)='integer' AND key_generation>=1
  ),
  cache_key_digest TEXT NOT NULL CHECK(
    length(cache_key_digest)=64
    AND cache_key_digest NOT GLOB '*[^0-9a-f]*'
  ),
  model_alias_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  route_revision_digest TEXT NOT NULL CHECK(
    length(route_revision_digest)=64
    AND route_revision_digest NOT GLOB '*[^0-9a-f]*'
  ),
  credential_revision_digest TEXT NOT NULL CHECK(
    length(credential_revision_digest)=64
    AND credential_revision_digest NOT GLOB '*[^0-9a-f]*'
  ),
  nonce BLOB NOT NULL CHECK(typeof(nonce)='blob' AND length(nonce)=12),
  ciphertext BLOB NOT NULL CHECK(
    typeof(ciphertext)='blob' AND length(ciphertext) BETWEEN 16 AND 1048592
  ),
  plaintext_byte_count INTEGER NOT NULL CHECK(
    typeof(plaintext_byte_count)='integer'
    AND plaintext_byte_count BETWEEN 1 AND 1048576
  ),
  input_tokens INTEGER NOT NULL CHECK(
    typeof(input_tokens)='integer' AND input_tokens>=0
  ),
  output_tokens INTEGER NOT NULL CHECK(
    typeof(output_tokens)='integer' AND output_tokens>=0
  ),
  total_tokens INTEGER NOT NULL CHECK(
    typeof(total_tokens)='integer' AND total_tokens=input_tokens+output_tokens
  ),
  source_request_id TEXT NOT NULL,
  source_trace_id TEXT NOT NULL,
  source_usage_ledger_id TEXT NOT NULL,
  source_finops_ledger_entry_id TEXT NOT NULL,
  source_generation_cost_status TEXT NOT NULL CHECK(
    source_generation_cost_status IN ('known','unavailable')
  ),
  source_generation_cost_cny_micros INTEGER CHECK(
    source_generation_cost_cny_micros IS NULL OR (
      typeof(source_generation_cost_cny_micros)='integer'
      AND source_generation_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(
    unixepoch(created_at) IS NOT NULL
    AND unixepoch(expires_at)>unixepoch(created_at)
  ),
  UNIQUE(tenant_id,response_cache_entry_id),
  UNIQUE(tenant_id,key_schema_version,cache_key_digest),
  UNIQUE(encryption_key_version,nonce),
  FOREIGN KEY(tenant_id,cache_policy_id)
    REFERENCES response_cache_policies(tenant_id,cache_policy_id),
  FOREIGN KEY(tenant_id,hmac_key_version,cache_identity_digest)
    REFERENCES response_cache_key_generations(
      tenant_id,hmac_key_version,cache_identity_digest
    ),
  CHECK(
    (source_generation_cost_status='known'
      AND source_generation_cost_cny_micros IS NOT NULL)
    OR (source_generation_cost_status='unavailable'
      AND source_generation_cost_cny_micros IS NULL)
  )
);
CREATE INDEX idx_response_cache_entries_expiry_prd076
  ON response_cache_entries(expires_at,tenant_id,response_cache_entry_id);
CREATE INDEX idx_response_cache_entries_tenant_policy_prd076
  ON response_cache_entries(
    tenant_id,cache_policy_id,policy_invalidation_generation,created_at
  );
CREATE TRIGGER trg_response_cache_entries_owner_insert_prd076
BEFORE INSERT ON response_cache_entries FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM response_cache_key_state state
    WHERE state.key_state_id=1 AND state.key_schema_version=NEW.key_schema_version
      AND state.active_hmac_key_version=NEW.hmac_key_version
      AND state.active_encryption_key_version=NEW.encryption_key_version
  )
  OR NOT EXISTS(
    SELECT 1 FROM response_cache_policies policy
    WHERE policy.tenant_id=NEW.tenant_id
      AND policy.cache_policy_id=NEW.cache_policy_id
      AND policy.model_alias_id=NEW.model_alias_id
      AND policy.status='enabled' AND policy.version=NEW.cache_policy_version
      AND policy.invalidation_generation=NEW.policy_invalidation_generation
  )
  OR NOT EXISTS(
    SELECT 1 FROM response_cache_key_generations generation
    WHERE generation.tenant_id=NEW.tenant_id
      AND generation.hmac_key_version=NEW.hmac_key_version
      AND generation.cache_identity_digest=NEW.cache_identity_digest
      AND generation.generation=NEW.key_generation
  )
  OR NOT EXISTS(
    SELECT 1 FROM model_aliases alias
    WHERE alias.tenant_id=NEW.tenant_id
      AND alias.model_alias_id=NEW.model_alias_id
  )
  OR NOT EXISTS(
    SELECT 1 FROM models model
    WHERE model.tenant_id=NEW.tenant_id AND model.model_id=NEW.model_id
      AND model.provider_id=NEW.provider_id
  )
  OR NOT EXISTS(
    SELECT 1 FROM providers provider
    WHERE provider.tenant_id=NEW.tenant_id
      AND provider.provider_id=NEW.provider_id
  )
  OR NOT EXISTS(
    SELECT 1 FROM request_logs request
    WHERE request.tenant_id=NEW.tenant_id
      AND request.request_id=NEW.source_request_id
      AND request.trace_id=NEW.source_trace_id AND request.status='success'
      AND request.model_id=NEW.model_id AND request.provider_id=NEW.provider_id
  )
  OR NOT EXISTS(
    SELECT 1 FROM usage_ledger usage
    WHERE usage.tenant_id=NEW.tenant_id
      AND usage.usage_ledger_id=NEW.source_usage_ledger_id
      AND usage.request_id=NEW.source_request_id
      AND usage.input_tokens=NEW.input_tokens
      AND usage.output_tokens=NEW.output_tokens
      AND usage.total_tokens=NEW.total_tokens
  )
  OR NOT EXISTS(
    SELECT 1 FROM finops_ledger_entries charge
    WHERE charge.tenant_id=NEW.tenant_id
      AND charge.finops_ledger_entry_id=NEW.source_finops_ledger_entry_id
      AND charge.usage_ledger_id=NEW.source_usage_ledger_id
      AND charge.source_request_id=NEW.source_request_id
      AND charge.source_trace_id=NEW.source_trace_id
      AND charge.entry_type='usage_charge'
      AND (
        (NEW.source_generation_cost_status='known'
          AND charge.cny_normalization_status='normalized'
          AND charge.normalized_cny_micros
              =NEW.source_generation_cost_cny_micros)
        OR NEW.source_generation_cost_status='unavailable'
      )
  )
  OR EXISTS(
    SELECT 1 FROM response_cache_entries current
    WHERE current.response_cache_entry_id=NEW.response_cache_entry_id
      OR (current.tenant_id=NEW.tenant_id
          AND current.key_schema_version=NEW.key_schema_version
          AND current.cache_key_digest=NEW.cache_key_digest)
      OR (current.encryption_key_version=NEW.encryption_key_version
          AND current.nonce=NEW.nonce)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'response_cache_entry_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_entries_update_prd076
BEFORE UPDATE ON response_cache_entries FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_entry_immutable'); END;

CREATE TABLE response_cache_entry_tags(
  tenant_id TEXT NOT NULL,
  hmac_key_version INTEGER NOT NULL CHECK(hmac_key_version>=1),
  response_cache_entry_id TEXT NOT NULL,
  tag_kind TEXT NOT NULL CHECK(tag_kind IN(
    'policy','model_alias','model','virtual_key','data_class','tag'
  )),
  tag_digest TEXT NOT NULL CHECK(
    length(tag_digest)=64 AND tag_digest NOT GLOB '*[^0-9a-f]*'
  ),
  tag_generation INTEGER NOT NULL CHECK(
    typeof(tag_generation)='integer' AND tag_generation>=1
  ),
  PRIMARY KEY(
    tenant_id,response_cache_entry_id,tag_kind,tag_digest
  ),
  FOREIGN KEY(tenant_id,response_cache_entry_id)
    REFERENCES response_cache_entries(tenant_id,response_cache_entry_id)
    ON DELETE CASCADE,
  FOREIGN KEY(tenant_id,hmac_key_version,tag_kind,tag_digest)
    REFERENCES response_cache_tag_generations(
      tenant_id,hmac_key_version,tag_kind,tag_digest
    )
) WITHOUT ROWID;
CREATE INDEX idx_response_cache_entry_tags_lookup_prd076
  ON response_cache_entry_tags(
    tenant_id,hmac_key_version,tag_kind,tag_digest,tag_generation
  );
CREATE TRIGGER trg_response_cache_entry_tags_owner_insert_prd076
BEFORE INSERT ON response_cache_entry_tags FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM response_cache_entries entry
    WHERE entry.tenant_id=NEW.tenant_id
      AND entry.response_cache_entry_id=NEW.response_cache_entry_id
      AND entry.hmac_key_version=NEW.hmac_key_version
  )
  OR NOT EXISTS(
    SELECT 1 FROM response_cache_tag_generations generation
    WHERE generation.tenant_id=NEW.tenant_id
      AND generation.hmac_key_version=NEW.hmac_key_version
      AND generation.tag_kind=NEW.tag_kind
      AND generation.tag_digest=NEW.tag_digest
      AND generation.generation=NEW.tag_generation
  )
BEGIN SELECT RAISE(ABORT,'response_cache_entry_tag_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_entry_tags_update_prd076
BEFORE UPDATE ON response_cache_entry_tags FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_entry_tag_immutable'); END;
CREATE TRIGGER trg_response_cache_entry_tags_delete_prd076
BEFORE DELETE ON response_cache_entry_tags FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM response_cache_entries entry
  WHERE entry.tenant_id=OLD.tenant_id
    AND entry.response_cache_entry_id=OLD.response_cache_entry_id
)
BEGIN SELECT RAISE(ABORT,'response_cache_entry_tag_immutable'); END;

CREATE TABLE response_cache_fill_leases(
  response_cache_fill_lease_id TEXT PRIMARY KEY CHECK(
    length(response_cache_fill_lease_id)=37
    AND substr(response_cache_fill_lease_id,1,5)='rcfl_'
    AND substr(response_cache_fill_lease_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  key_schema_version INTEGER NOT NULL CHECK(key_schema_version=1),
  hmac_key_version INTEGER NOT NULL CHECK(hmac_key_version>=1),
  encryption_key_version INTEGER NOT NULL CHECK(encryption_key_version>=1),
  cache_identity_digest TEXT NOT NULL CHECK(
    length(cache_identity_digest)=64
    AND cache_identity_digest NOT GLOB '*[^0-9a-f]*'
  ),
  key_generation INTEGER NOT NULL CHECK(key_generation>=1),
  cache_key_digest TEXT NOT NULL CHECK(
    length(cache_key_digest)=64
    AND cache_key_digest NOT GLOB '*[^0-9a-f]*'
  ),
  cache_policy_id TEXT NOT NULL,
  cache_policy_version INTEGER NOT NULL CHECK(cache_policy_version>=1),
  policy_invalidation_generation INTEGER NOT NULL CHECK(
    policy_invalidation_generation>=1
  ),
  owner_request_id TEXT NOT NULL,
  owner_trace_id TEXT NOT NULL,
  lease_generation INTEGER NOT NULL CHECK(lease_generation>=1),
  lease_token TEXT NOT NULL CHECK(
    length(lease_token)=64 AND lease_token NOT GLOB '*[^0-9a-f]*'
  ),
  lease_owner_id TEXT NOT NULL CHECK(
    length(lease_owner_id) BETWEEN 1 AND 128
    AND instr(lease_owner_id,char(0))=0
  ),
  route_revision_digest TEXT NOT NULL CHECK(
    length(route_revision_digest)=64
    AND route_revision_digest NOT GLOB '*[^0-9a-f]*'
  ),
  credential_revision_digest TEXT NOT NULL CHECK(
    length(credential_revision_digest)=64
    AND credential_revision_digest NOT GLOB '*[^0-9a-f]*'
  ),
  lease_expires_at TEXT NOT NULL CHECK(unixepoch(lease_expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(
    unixepoch(updated_at) IS NOT NULL
    AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  UNIQUE(tenant_id,response_cache_fill_lease_id),
  UNIQUE(tenant_id,hmac_key_version,cache_key_digest),
  FOREIGN KEY(tenant_id,cache_policy_id)
    REFERENCES response_cache_policies(tenant_id,cache_policy_id),
  FOREIGN KEY(tenant_id,hmac_key_version,cache_identity_digest)
    REFERENCES response_cache_key_generations(
      tenant_id,hmac_key_version,cache_identity_digest
    )
);
CREATE INDEX idx_response_cache_fill_leases_expiry_prd076
  ON response_cache_fill_leases(
    lease_expires_at,tenant_id,response_cache_fill_lease_id
  );
CREATE TRIGGER trg_response_cache_fill_leases_owner_insert_prd076
BEFORE INSERT ON response_cache_fill_leases FOR EACH ROW
WHEN NEW.lease_generation!=1
  OR NOT EXISTS(
    SELECT 1 FROM response_cache_key_state state
    WHERE state.key_state_id=1 AND state.key_schema_version=NEW.key_schema_version
      AND state.active_hmac_key_version=NEW.hmac_key_version
      AND state.active_encryption_key_version=NEW.encryption_key_version
  )
  OR NOT EXISTS(
    SELECT 1 FROM response_cache_policies policy
    WHERE policy.tenant_id=NEW.tenant_id
      AND policy.cache_policy_id=NEW.cache_policy_id
      AND policy.status='enabled' AND policy.version=NEW.cache_policy_version
      AND policy.invalidation_generation=NEW.policy_invalidation_generation
  )
  OR NOT EXISTS(
    SELECT 1 FROM response_cache_key_generations generation
    WHERE generation.tenant_id=NEW.tenant_id
      AND generation.hmac_key_version=NEW.hmac_key_version
      AND generation.cache_identity_digest=NEW.cache_identity_digest
      AND generation.generation=NEW.key_generation
  )
  OR NOT EXISTS(
    SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id
      AND claim.request_id=NEW.owner_request_id
      AND claim.trace_id=NEW.owner_trace_id
  )
  OR EXISTS(
    SELECT 1 FROM response_cache_fill_leases current
    WHERE current.response_cache_fill_lease_id=NEW.response_cache_fill_lease_id
      OR (current.tenant_id=NEW.tenant_id
          AND current.hmac_key_version=NEW.hmac_key_version
          AND current.cache_key_digest=NEW.cache_key_digest)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'response_cache_fill_lease_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_fill_leases_update_prd076
BEFORE UPDATE ON response_cache_fill_leases FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.response_cache_fill_lease_id!=OLD.response_cache_fill_lease_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.key_schema_version!=OLD.key_schema_version
  OR NEW.hmac_key_version!=OLD.hmac_key_version
  OR NEW.encryption_key_version!=OLD.encryption_key_version
  OR NEW.cache_identity_digest!=OLD.cache_identity_digest
  OR NEW.key_generation!=OLD.key_generation
  OR NEW.cache_key_digest!=OLD.cache_key_digest
  OR NEW.cache_policy_id!=OLD.cache_policy_id
  OR NEW.cache_policy_version!=OLD.cache_policy_version
  OR NEW.policy_invalidation_generation!=OLD.policy_invalidation_generation
  OR NEW.owner_request_id!=OLD.owner_request_id
  OR NEW.owner_trace_id!=OLD.owner_trace_id
  OR NEW.route_revision_digest!=OLD.route_revision_digest
  OR NEW.credential_revision_digest!=OLD.credential_revision_digest
  OR NEW.created_at!=OLD.created_at
  OR NEW.lease_generation!=OLD.lease_generation+1
  OR NEW.lease_token=OLD.lease_token
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'response_cache_fill_lease_transition_invalid'); END;

CREATE TABLE response_cache_fill_tags(
  tenant_id TEXT NOT NULL,
  response_cache_fill_lease_id TEXT NOT NULL,
  hmac_key_version INTEGER NOT NULL CHECK(hmac_key_version>=1),
  tag_kind TEXT NOT NULL CHECK(tag_kind IN(
    'policy','model_alias','model','virtual_key','data_class','tag'
  )),
  tag_digest TEXT NOT NULL CHECK(
    length(tag_digest)=64 AND tag_digest NOT GLOB '*[^0-9a-f]*'
  ),
  expected_generation INTEGER NOT NULL CHECK(expected_generation>=1),
  PRIMARY KEY(
    tenant_id,response_cache_fill_lease_id,tag_kind,tag_digest
  ),
  FOREIGN KEY(tenant_id,response_cache_fill_lease_id)
    REFERENCES response_cache_fill_leases(
      tenant_id,response_cache_fill_lease_id
    ) ON DELETE CASCADE,
  FOREIGN KEY(tenant_id,hmac_key_version,tag_kind,tag_digest)
    REFERENCES response_cache_tag_generations(
      tenant_id,hmac_key_version,tag_kind,tag_digest
    )
) WITHOUT ROWID;
CREATE INDEX idx_response_cache_fill_tags_lookup_prd076
  ON response_cache_fill_tags(
    tenant_id,hmac_key_version,tag_kind,tag_digest,expected_generation
  );
CREATE TRIGGER trg_response_cache_fill_tags_owner_insert_prd076
BEFORE INSERT ON response_cache_fill_tags FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM response_cache_fill_leases lease
    WHERE lease.tenant_id=NEW.tenant_id
      AND lease.response_cache_fill_lease_id=NEW.response_cache_fill_lease_id
      AND lease.hmac_key_version=NEW.hmac_key_version
  )
  OR NOT EXISTS(
    SELECT 1 FROM response_cache_tag_generations generation
    WHERE generation.tenant_id=NEW.tenant_id
      AND generation.hmac_key_version=NEW.hmac_key_version
      AND generation.tag_kind=NEW.tag_kind
      AND generation.tag_digest=NEW.tag_digest
      AND generation.generation=NEW.expected_generation
  )
BEGIN SELECT RAISE(ABORT,'response_cache_fill_tag_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_fill_tags_update_prd076
BEFORE UPDATE ON response_cache_fill_tags FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_fill_tag_immutable'); END;
CREATE TRIGGER trg_response_cache_fill_tags_delete_prd076
BEFORE DELETE ON response_cache_fill_tags FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM response_cache_fill_leases lease
  WHERE lease.tenant_id=OLD.tenant_id
    AND lease.response_cache_fill_lease_id=OLD.response_cache_fill_lease_id
)
BEGIN SELECT RAISE(ABORT,'response_cache_fill_tag_immutable'); END;

-- 每个 Gateway request 只保存安全投影，不保存 entry ID、cache key、tag、data class 或正文。
CREATE TABLE response_cache_request_facts(
  response_cache_request_fact_id TEXT PRIMARY KEY CHECK(
    length(response_cache_request_fact_id)=37
    AND substr(response_cache_request_fact_id,1,5)='rcrf_'
    AND substr(response_cache_request_fact_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  cache_policy_id TEXT,
  cache_policy_version INTEGER CHECK(
    cache_policy_version IS NULL OR cache_policy_version>=1
  ),
  status TEXT NOT NULL CHECK(status IN(
    'eligible','disabled','ineligible','bypassed_experiment','unavailable_bypass',
    'miss_owner','miss_non_owner','corrupt_miss','fill_stored','fill_skipped',
    'hit_candidate','hit','hit_blocked','settlement_failed'
  )),
  response_cache_hit_id TEXT,
  hit INTEGER NOT NULL CHECK(hit IN (0,1)),
  charge_cache_hits INTEGER CHECK(
    charge_cache_hits IS NULL OR charge_cache_hits IN (0,1)
  ),
  source_generation_cost_status TEXT NOT NULL CHECK(
    source_generation_cost_status IN ('not_applicable','known','unavailable')
  ),
  source_generation_cost_cny_micros INTEGER CHECK(
    source_generation_cost_cny_micros IS NULL OR (
      typeof(source_generation_cost_cny_micros)='integer'
      AND source_generation_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  current_reference_cost_status TEXT NOT NULL CHECK(
    current_reference_cost_status IN ('not_applicable','known','unavailable')
  ),
  current_reference_cost_cny_micros INTEGER CHECK(
    current_reference_cost_cny_micros IS NULL OR (
      typeof(current_reference_cost_cny_micros)='integer'
      AND current_reference_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  saved_cost_status TEXT NOT NULL CHECK(
    saved_cost_status IN ('not_applicable','known','unavailable')
  ),
  saved_cost_cny_micros INTEGER CHECK(
    saved_cost_cny_micros IS NULL OR (
      typeof(saved_cost_cny_micros)='integer'
      AND saved_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  billed_cost_status TEXT NOT NULL CHECK(
    billed_cost_status IN ('not_applicable','known','unavailable')
  ),
  billed_cost_cny_micros INTEGER CHECK(
    billed_cost_cny_micros IS NULL OR (
      typeof(billed_cost_cny_micros)='integer'
      AND billed_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  failure_code TEXT CHECK(
    failure_code IS NULL OR (
      length(failure_code) BETWEEN 1 AND 128
      AND instr(failure_code,char(0))=0
    )
  ),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(
    unixepoch(updated_at) IS NOT NULL
    AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  UNIQUE(tenant_id,response_cache_request_fact_id),
  UNIQUE(tenant_id,request_id),
  UNIQUE(tenant_id,response_cache_hit_id),
  FOREIGN KEY(tenant_id,request_id)
    REFERENCES gateway_request_claims(tenant_id,request_id),
  CHECK(
    (cache_policy_id IS NULL AND cache_policy_version IS NULL
      AND charge_cache_hits IS NULL)
    OR (cache_policy_id IS NOT NULL AND cache_policy_version IS NOT NULL
      AND charge_cache_hits IS NOT NULL)
  ),
  CHECK(
    (hit=1 AND status='hit' AND response_cache_hit_id IS NOT NULL)
    OR (hit=0 AND status!='hit' AND response_cache_hit_id IS NULL)
  ),
  CHECK(
    (status='hit'
      AND source_generation_cost_status IN ('known','unavailable')
      AND current_reference_cost_status IN ('known','unavailable')
      AND saved_cost_status IN ('known','unavailable')
      AND billed_cost_status IN ('known','unavailable'))
    OR (status!='hit'
      AND source_generation_cost_status='not_applicable'
      AND current_reference_cost_status='not_applicable'
      AND saved_cost_status='not_applicable'
      AND billed_cost_status='not_applicable'
      AND source_generation_cost_cny_micros IS NULL
      AND current_reference_cost_cny_micros IS NULL
      AND saved_cost_cny_micros IS NULL
      AND billed_cost_cny_micros IS NULL)
  ),
  CHECK(
    (source_generation_cost_status='known'
      AND source_generation_cost_cny_micros IS NOT NULL)
    OR (source_generation_cost_status!='known'
      AND source_generation_cost_cny_micros IS NULL)
  ),
  CHECK(
    (current_reference_cost_status='known'
      AND current_reference_cost_cny_micros IS NOT NULL)
    OR (current_reference_cost_status!='known'
      AND current_reference_cost_cny_micros IS NULL)
  ),
  CHECK(
    (saved_cost_status='known' AND saved_cost_cny_micros IS NOT NULL)
    OR (saved_cost_status!='known' AND saved_cost_cny_micros IS NULL)
  ),
  CHECK(
    (billed_cost_status='known' AND billed_cost_cny_micros IS NOT NULL)
    OR (billed_cost_status!='known' AND billed_cost_cny_micros IS NULL)
  ),
  CHECK(
    saved_cost_status!='known'
    OR (
      current_reference_cost_status='known' AND billed_cost_status='known'
      AND saved_cost_cny_micros=CASE
        WHEN current_reference_cost_cny_micros>=billed_cost_cny_micros
        THEN current_reference_cost_cny_micros-billed_cost_cny_micros
        ELSE 0 END
    )
  )
);
CREATE INDEX idx_response_cache_request_facts_tenant_status_prd076
  ON response_cache_request_facts(tenant_id,status,created_at,request_id);
CREATE TRIGGER trg_response_cache_request_facts_owner_insert_prd076
BEFORE INSERT ON response_cache_request_facts FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id AND claim.request_id=NEW.request_id
      AND claim.trace_id=NEW.trace_id
  )
  OR (NEW.cache_policy_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM response_cache_policies policy
    WHERE policy.tenant_id=NEW.tenant_id
      AND policy.cache_policy_id=NEW.cache_policy_id
      AND policy.version=NEW.cache_policy_version
      AND policy.charge_cache_hits=NEW.charge_cache_hits
  ))
  OR NEW.status NOT IN(
    'eligible','disabled','ineligible','bypassed_experiment','unavailable_bypass'
  )
  OR NEW.hit!=0 OR NEW.response_cache_hit_id IS NOT NULL
  OR EXISTS(
    SELECT 1 FROM response_cache_request_facts current
    WHERE current.response_cache_request_fact_id
            =NEW.response_cache_request_fact_id
      OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'response_cache_request_fact_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_request_facts_update_prd076
BEFORE UPDATE ON response_cache_request_facts FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.response_cache_request_fact_id!=OLD.response_cache_request_fact_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.cache_policy_id IS NOT OLD.cache_policy_id
  OR NEW.cache_policy_version IS NOT OLD.cache_policy_version
  OR NEW.charge_cache_hits IS NOT OLD.charge_cache_hits
  OR NEW.created_at!=OLD.created_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT (
    (OLD.status='eligible' AND NEW.status IN(
      'miss_owner','miss_non_owner','hit_candidate','corrupt_miss',
      'unavailable_bypass'
    ))
    OR (OLD.status='miss_owner' AND NEW.status IN('fill_stored','fill_skipped'))
    OR (OLD.status='corrupt_miss' AND NEW.status IN('fill_stored','fill_skipped'))
    OR (OLD.status='hit_candidate' AND NEW.status IN(
      'hit','hit_blocked','settlement_failed','miss_owner','miss_non_owner',
      'corrupt_miss','unavailable_bypass'
    ))
  )
BEGIN SELECT RAISE(ABORT,'response_cache_request_fact_transition_invalid'); END;
CREATE TRIGGER trg_response_cache_request_facts_delete_prd076
BEFORE DELETE ON response_cache_request_facts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_request_fact_immutable'); END;

CREATE TABLE response_cache_hits(
  response_cache_hit_id TEXT PRIMARY KEY CHECK(
    length(response_cache_hit_id)=36
    AND substr(response_cache_hit_id,1,4)='rch_'
    AND substr(response_cache_hit_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  cache_policy_id TEXT NOT NULL,
  cache_policy_version INTEGER NOT NULL CHECK(cache_policy_version>=1),
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  source_response_cache_entry_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL,
  source_trace_id TEXT NOT NULL,
  source_usage_ledger_id TEXT NOT NULL,
  source_finops_ledger_entry_id TEXT NOT NULL,
  charge_cache_hits INTEGER NOT NULL CHECK(charge_cache_hits IN (0,1)),
  source_generation_cost_status TEXT NOT NULL CHECK(
    source_generation_cost_status IN ('known','unavailable')
  ),
  source_generation_cost_cny_micros INTEGER CHECK(
    source_generation_cost_cny_micros IS NULL OR (
      typeof(source_generation_cost_cny_micros)='integer'
      AND source_generation_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  current_reference_cost_status TEXT NOT NULL CHECK(
    current_reference_cost_status IN ('known','unavailable')
  ),
  current_reference_cost_cny_micros INTEGER CHECK(
    current_reference_cost_cny_micros IS NULL OR (
      typeof(current_reference_cost_cny_micros)='integer'
      AND current_reference_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  saved_cost_status TEXT NOT NULL CHECK(
    saved_cost_status IN ('known','unavailable')
  ),
  saved_cost_cny_micros INTEGER CHECK(
    saved_cost_cny_micros IS NULL OR (
      typeof(saved_cost_cny_micros)='integer'
      AND saved_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  billed_cost_status TEXT NOT NULL CHECK(
    billed_cost_status IN ('known','unavailable')
  ),
  billed_cost_cny_micros INTEGER CHECK(
    billed_cost_cny_micros IS NULL OR (
      typeof(billed_cost_cny_micros)='integer'
      AND billed_cost_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  source_price_snapshot_id TEXT NOT NULL,
  current_reference_price_snapshot_id TEXT NOT NULL,
  billed_price_snapshot_id TEXT NOT NULL,
  usage_ledger_id TEXT NOT NULL,
  finops_ledger_entry_id TEXT NOT NULL,
  pricing_observed_at TEXT NOT NULL CHECK(unixepoch(pricing_observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(tenant_id,response_cache_hit_id),
  UNIQUE(tenant_id,request_id),
  UNIQUE(tenant_id,usage_ledger_id),
  UNIQUE(tenant_id,finops_ledger_entry_id),
  FOREIGN KEY(tenant_id,cache_policy_id)
    REFERENCES response_cache_policies(tenant_id,cache_policy_id),
  CHECK(
    (source_generation_cost_status='known'
      AND source_generation_cost_cny_micros IS NOT NULL)
    OR (source_generation_cost_status='unavailable'
      AND source_generation_cost_cny_micros IS NULL)
  ),
  CHECK(
    (current_reference_cost_status='known'
      AND current_reference_cost_cny_micros IS NOT NULL)
    OR (current_reference_cost_status='unavailable'
      AND current_reference_cost_cny_micros IS NULL)
  ),
  CHECK(
    (saved_cost_status='known' AND saved_cost_cny_micros IS NOT NULL)
    OR (saved_cost_status='unavailable' AND saved_cost_cny_micros IS NULL)
  ),
  CHECK(
    (billed_cost_status='known' AND billed_cost_cny_micros IS NOT NULL)
    OR (billed_cost_status='unavailable' AND billed_cost_cny_micros IS NULL)
  ),
  CHECK(
    saved_cost_status!='known' OR (
      current_reference_cost_status='known' AND billed_cost_status='known'
      AND saved_cost_cny_micros=CASE
        WHEN current_reference_cost_cny_micros>=billed_cost_cny_micros
        THEN current_reference_cost_cny_micros-billed_cost_cny_micros
        ELSE 0 END
    )
  )
);
CREATE INDEX idx_response_cache_hits_tenant_time_prd076
  ON response_cache_hits(tenant_id,created_at,response_cache_hit_id);
CREATE TRIGGER trg_response_cache_hits_owner_insert_prd076
BEFORE INSERT ON response_cache_hits FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM response_cache_entries entry
    WHERE entry.tenant_id=NEW.tenant_id
      AND entry.response_cache_entry_id=NEW.source_response_cache_entry_id
      AND entry.cache_policy_id=NEW.cache_policy_id
      AND entry.cache_policy_version=NEW.cache_policy_version
      AND entry.model_id=NEW.model_id AND entry.provider_id=NEW.provider_id
      AND entry.source_request_id=NEW.source_request_id
      AND entry.source_trace_id=NEW.source_trace_id
      AND entry.source_usage_ledger_id=NEW.source_usage_ledger_id
      AND entry.source_finops_ledger_entry_id=NEW.source_finops_ledger_entry_id
      AND entry.source_generation_cost_status=NEW.source_generation_cost_status
      AND entry.source_generation_cost_cny_micros
          IS NEW.source_generation_cost_cny_micros
  )
  OR NOT EXISTS(
    SELECT 1 FROM request_logs request
    WHERE request.tenant_id=NEW.tenant_id AND request.request_id=NEW.request_id
      AND request.trace_id=NEW.trace_id AND request.status='success'
      AND request.virtual_key_id=NEW.virtual_key_id
      AND request.model_id=NEW.model_id AND request.provider_id=NEW.provider_id
  )
  OR NOT EXISTS(
    SELECT 1 FROM price_snapshots source_price
    JOIN price_snapshots reference_price
      ON reference_price.tenant_id=source_price.tenant_id
    JOIN price_snapshots billed_price
      ON billed_price.tenant_id=source_price.tenant_id
    WHERE source_price.tenant_id=NEW.tenant_id
      AND source_price.price_snapshot_id=NEW.source_price_snapshot_id
      AND source_price.request_id=NEW.source_request_id
      AND reference_price.price_snapshot_id
          =NEW.current_reference_price_snapshot_id
      AND reference_price.request_id=NEW.request_id
      AND reference_price.source='response_cache_reference'
      AND reference_price.created_at=NEW.pricing_observed_at
      AND billed_price.price_snapshot_id=NEW.billed_price_snapshot_id
      AND billed_price.request_id=NEW.request_id
      AND billed_price.created_at=NEW.pricing_observed_at
      AND billed_price.source IN(
        'response_cache_reference','response_cache_no_charge'
      )
  )
  OR NOT EXISTS(
    SELECT 1 FROM usage_ledger usage
    WHERE usage.tenant_id=NEW.tenant_id
      AND usage.usage_ledger_id=NEW.usage_ledger_id
      AND usage.request_id=NEW.request_id
      AND usage.usage_source='response_cache' AND usage.cache_tokens=0
      AND usage.price_snapshot_id=NEW.billed_price_snapshot_id
  )
  OR NOT EXISTS(
    SELECT 1 FROM finops_ledger_entries charge
    WHERE charge.tenant_id=NEW.tenant_id
      AND charge.finops_ledger_entry_id=NEW.finops_ledger_entry_id
      AND charge.usage_ledger_id=NEW.usage_ledger_id
      AND charge.source_request_id=NEW.request_id
      AND charge.source_trace_id=NEW.trace_id
      AND charge.entry_type='usage_charge'
      AND (
        (NEW.billed_cost_status='known'
          AND charge.cny_normalization_status='normalized'
          AND charge.normalized_cny_micros=NEW.billed_cost_cny_micros)
        OR (NEW.billed_cost_status='unavailable'
          AND charge.cny_normalization_status='unavailable'
          AND charge.normalized_cny_micros IS NULL
          AND charge.price_evidence_status='unpriced')
      )
  )
  OR EXISTS(
    SELECT 1 FROM response_cache_hits current
    WHERE current.response_cache_hit_id=NEW.response_cache_hit_id
      OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
      OR (current.tenant_id=NEW.tenant_id
          AND current.usage_ledger_id=NEW.usage_ledger_id)
      OR (current.tenant_id=NEW.tenant_id
          AND current.finops_ledger_entry_id=NEW.finops_ledger_entry_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'response_cache_hit_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_hits_update_prd076
BEFORE UPDATE ON response_cache_hits FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_hit_immutable'); END;
CREATE TRIGGER trg_response_cache_hits_delete_prd076
BEFORE DELETE ON response_cache_hits FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_hit_immutable'); END;
CREATE TRIGGER trg_response_cache_request_facts_hit_update_prd076
BEFORE UPDATE ON response_cache_request_facts FOR EACH ROW
WHEN NEW.status='hit' AND NOT EXISTS(
  SELECT 1 FROM response_cache_hits hit
  WHERE hit.tenant_id=NEW.tenant_id
    AND hit.response_cache_hit_id=NEW.response_cache_hit_id
    AND hit.request_id=NEW.request_id AND hit.trace_id=NEW.trace_id
    AND hit.cache_policy_id=NEW.cache_policy_id
    AND hit.cache_policy_version=NEW.cache_policy_version
    AND hit.charge_cache_hits=NEW.charge_cache_hits
    AND hit.source_generation_cost_status=NEW.source_generation_cost_status
    AND hit.source_generation_cost_cny_micros
        IS NEW.source_generation_cost_cny_micros
    AND hit.current_reference_cost_status=NEW.current_reference_cost_status
    AND hit.current_reference_cost_cny_micros
        IS NEW.current_reference_cost_cny_micros
    AND hit.saved_cost_status=NEW.saved_cost_status
    AND hit.saved_cost_cny_micros IS NEW.saved_cost_cny_micros
    AND hit.billed_cost_status=NEW.billed_cost_status
    AND hit.billed_cost_cny_micros IS NEW.billed_cost_cny_micros
)
BEGIN SELECT RAISE(ABORT,'response_cache_request_fact_hit_owner_mismatch'); END;

CREATE TABLE response_cache_invalidations(
  cache_invalidation_id TEXT PRIMARY KEY CHECK(
    length(cache_invalidation_id)=36
    AND substr(cache_invalidation_id,1,4)='rci_'
    AND substr(cache_invalidation_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  actor_type TEXT NOT NULL CHECK(actor_type IN ('user','system')),
  actor_user_id TEXT,
  idempotency_hmac_version INTEGER CHECK(
    idempotency_hmac_version IS NULL OR (
      typeof(idempotency_hmac_version)='integer'
      AND idempotency_hmac_version>=1
    )
  ),
  idempotency_key_hmac TEXT CHECK(
    idempotency_key_hmac IS NULL OR (
      length(idempotency_key_hmac)=64
      AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'
    )
  ),
  request_fingerprint TEXT NOT NULL CHECK(
    length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  scope TEXT NOT NULL CHECK(scope IN ('all','tags')),
  match_mode TEXT NOT NULL CHECK(match_mode='all'),
  selector_hmac TEXT NOT NULL CHECK(
    length(selector_hmac)=64 AND selector_hmac NOT GLOB '*[^0-9a-f]*'
  ),
  tag_count INTEGER NOT NULL CHECK(
    typeof(tag_count)='integer' AND tag_count BETWEEN 0 AND 16
  ),
  matched_entry_count INTEGER NOT NULL CHECK(
    typeof(matched_entry_count)='integer' AND matched_entry_count>=0
  ),
  deleted_entry_count INTEGER NOT NULL CHECK(
    typeof(deleted_entry_count)='integer' AND deleted_entry_count>=0
    AND deleted_entry_count<=matched_entry_count
  ),
  fenced_fill_count INTEGER NOT NULL CHECK(
    typeof(fenced_fill_count)='integer' AND fenced_fill_count>=0
  ),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'manual_finops','manual_security','manual_configuration','manual_other',
    'policy_changed','system_corrupt','system_output_blocked'
  )),
  response_json TEXT NOT NULL CHECK(
    json_valid(response_json) AND json_type(response_json)='object'
  ),
  operation_request_id TEXT NOT NULL CHECK(
    length(operation_request_id) BETWEEN 1 AND 128
    AND instr(operation_request_id,char(0))=0
  ),
  operation_trace_id TEXT NOT NULL CHECK(
    length(operation_trace_id) BETWEEN 1 AND 128
    AND instr(operation_trace_id,char(0))=0
  ),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(tenant_id,cache_invalidation_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  CHECK(
    (scope='all' AND match_mode='all' AND tag_count=0)
    OR (scope='tags' AND tag_count BETWEEN 1 AND 16)
  ),
  CHECK(
    (actor_type='user' AND actor_user_id IS NOT NULL
      AND idempotency_hmac_version IS NOT NULL
      AND idempotency_key_hmac IS NOT NULL
      AND reason_code IN(
        'manual_finops','manual_security','manual_configuration','manual_other',
        'policy_changed'
      ))
    OR (actor_type='system' AND actor_user_id IS NULL
      AND idempotency_hmac_version IS NULL AND idempotency_key_hmac IS NULL
      AND reason_code IN('system_corrupt','system_output_blocked'))
  )
);
CREATE UNIQUE INDEX idx_response_cache_invalidations_user_idempotency_prd076
  ON response_cache_invalidations(
    tenant_id,actor_user_id,idempotency_hmac_version,idempotency_key_hmac
  ) WHERE actor_type='user';
CREATE INDEX idx_response_cache_invalidations_tenant_time_prd076
  ON response_cache_invalidations(
    tenant_id,created_at,cache_invalidation_id
  );
CREATE TRIGGER trg_response_cache_invalidations_owner_insert_prd076
BEFORE INSERT ON response_cache_invalidations FOR EACH ROW
WHEN (NEW.actor_type='user' AND NOT EXISTS(
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
      AND actor.status='active'
  ))
  OR EXISTS(
    SELECT 1 FROM response_cache_invalidations current
    WHERE current.cache_invalidation_id=NEW.cache_invalidation_id
      OR (NEW.actor_type='user' AND current.actor_type='user'
          AND current.tenant_id=NEW.tenant_id
          AND current.actor_user_id=NEW.actor_user_id
          AND current.idempotency_hmac_version=NEW.idempotency_hmac_version
          AND current.idempotency_key_hmac=NEW.idempotency_key_hmac)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
  OR EXISTS(
    SELECT 1 FROM json_each(NEW.response_json) field
    WHERE field.key NOT IN(
      'tenant_id','invalidation','idempotent_replay','request_id','trace_id'
    )
  )
BEGIN SELECT RAISE(ABORT,'response_cache_invalidation_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_invalidations_update_prd076
BEFORE UPDATE ON response_cache_invalidations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_invalidation_immutable'); END;
CREATE TRIGGER trg_response_cache_invalidations_delete_prd076
BEFORE DELETE ON response_cache_invalidations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_invalidation_immutable'); END;

-- source event 与 outbox 使用同一个稳定事件 ID；payload 仅允许公开安全摘要。
CREATE TABLE response_cache_events(
  response_cache_event_id TEXT PRIMARY KEY CHECK(
    length(response_cache_event_id)=37
    AND substr(response_cache_event_id,1,5)='rcev_'
    AND substr(response_cache_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN('cache.hit','cache.invalidated')),
  object_id TEXT NOT NULL,
  actor_user_id TEXT,
  trace_id TEXT NOT NULL CHECK(
    length(trace_id) BETWEEN 1 AND 128 AND instr(trace_id,char(0))=0
  ),
  payload_json TEXT NOT NULL CHECK(
    json_valid(payload_json) AND json_type(payload_json)='object'
  ),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  UNIQUE(tenant_id,response_cache_event_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
);
CREATE INDEX idx_response_cache_events_tenant_time_prd076
  ON response_cache_events(tenant_id,occurred_at,response_cache_event_id);
CREATE TRIGGER trg_response_cache_events_owner_insert_prd076
BEFORE INSERT ON response_cache_events FOR EACH ROW
WHEN (
    NEW.event_type='cache.hit' AND (
      NEW.actor_user_id IS NOT NULL OR NOT EXISTS(
        SELECT 1 FROM response_cache_hits hit
        WHERE hit.tenant_id=NEW.tenant_id
          AND hit.response_cache_hit_id=NEW.object_id
          AND hit.trace_id=NEW.trace_id AND hit.created_at=NEW.occurred_at
          AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=13
          AND NOT EXISTS(
            SELECT 1 FROM json_each(NEW.payload_json) field
            WHERE field.key NOT IN(
              'schema_version','cache_hit_id','cache_policy_id',
              'cache_policy_version','charge_cache_hits',
              'source_generation_cost_status','source_generation_cost_cny',
              'current_reference_cost_status','current_reference_cost_cny',
              'saved_cost_status','saved_cost_cny','billed_cost_status',
              'billed_cost_cny'
            )
          )
          AND json_extract(NEW.payload_json,'$.schema_version')=1
          AND json_extract(NEW.payload_json,'$.cache_hit_id')
              =hit.response_cache_hit_id
          AND json_extract(NEW.payload_json,'$.cache_policy_id')
              =hit.cache_policy_id
          AND json_extract(NEW.payload_json,'$.cache_policy_version')
              =hit.cache_policy_version
          AND json_extract(NEW.payload_json,'$.charge_cache_hits')
              =hit.charge_cache_hits
          AND json_type(NEW.payload_json,'$.schema_version')='integer'
          AND json_type(NEW.payload_json,'$.cache_hit_id')='text'
          AND json_type(NEW.payload_json,'$.cache_policy_id')='text'
          AND json_type(NEW.payload_json,'$.cache_policy_version')='integer'
          AND json_type(NEW.payload_json,'$.charge_cache_hits') IN('true','false')
          AND json_type(NEW.payload_json,'$.source_generation_cost_status')='text'
          AND json_extract(NEW.payload_json,'$.source_generation_cost_status')
              =hit.source_generation_cost_status
          AND (
            (hit.source_generation_cost_status='known'
              AND json_type(NEW.payload_json,'$.source_generation_cost_cny')='text'
              AND json_extract(NEW.payload_json,'$.source_generation_cost_cny')
                  =printf('%d.%06d',hit.source_generation_cost_cny_micros/1000000,
                    hit.source_generation_cost_cny_micros%1000000))
            OR (hit.source_generation_cost_status='unavailable'
              AND json_type(NEW.payload_json,'$.source_generation_cost_cny')='null')
          )
          AND json_type(NEW.payload_json,'$.current_reference_cost_status')='text'
          AND json_extract(NEW.payload_json,'$.current_reference_cost_status')
              =hit.current_reference_cost_status
          AND (
            (hit.current_reference_cost_status='known'
              AND json_type(NEW.payload_json,'$.current_reference_cost_cny')='text'
              AND json_extract(NEW.payload_json,'$.current_reference_cost_cny')
                  =printf('%d.%06d',hit.current_reference_cost_cny_micros/1000000,
                    hit.current_reference_cost_cny_micros%1000000))
            OR (hit.current_reference_cost_status='unavailable'
              AND json_type(NEW.payload_json,'$.current_reference_cost_cny')='null')
          )
          AND json_type(NEW.payload_json,'$.saved_cost_status')='text'
          AND json_extract(NEW.payload_json,'$.saved_cost_status')=hit.saved_cost_status
          AND (
            (hit.saved_cost_status='known'
              AND json_type(NEW.payload_json,'$.saved_cost_cny')='text'
              AND json_extract(NEW.payload_json,'$.saved_cost_cny')
                  =printf('%d.%06d',hit.saved_cost_cny_micros/1000000,
                    hit.saved_cost_cny_micros%1000000))
            OR (hit.saved_cost_status='unavailable'
              AND json_type(NEW.payload_json,'$.saved_cost_cny')='null')
          )
          AND json_type(NEW.payload_json,'$.billed_cost_status')='text'
          AND json_extract(NEW.payload_json,'$.billed_cost_status')=hit.billed_cost_status
          AND (
            (hit.billed_cost_status='known'
              AND json_type(NEW.payload_json,'$.billed_cost_cny')='text'
              AND json_extract(NEW.payload_json,'$.billed_cost_cny')
                  =printf('%d.%06d',hit.billed_cost_cny_micros/1000000,
                    hit.billed_cost_cny_micros%1000000))
            OR (hit.billed_cost_status='unavailable'
              AND json_type(NEW.payload_json,'$.billed_cost_cny')='null')
          )
      )
    )
  )
  OR (
    NEW.event_type='cache.invalidated' AND NOT EXISTS(
      SELECT 1 FROM response_cache_invalidations invalidation
      WHERE invalidation.tenant_id=NEW.tenant_id
        AND invalidation.cache_invalidation_id=NEW.object_id
        AND invalidation.actor_user_id IS NEW.actor_user_id
        AND invalidation.operation_trace_id=NEW.trace_id
        AND invalidation.created_at=NEW.occurred_at
        AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=9
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.payload_json) field
          WHERE field.key NOT IN(
            'schema_version','cache_invalidation_id','scope','match_mode',
            'tag_count','matched_entry_count','deleted_entry_count',
            'fenced_fill_count','reason_code'
          )
        )
        AND json_extract(NEW.payload_json,'$.schema_version')=1
        AND json_extract(NEW.payload_json,'$.cache_invalidation_id')
            =invalidation.cache_invalidation_id
        AND json_extract(NEW.payload_json,'$.scope')=invalidation.scope
        AND json_extract(NEW.payload_json,'$.match_mode')=invalidation.match_mode
        AND json_extract(NEW.payload_json,'$.tag_count')=invalidation.tag_count
        AND json_extract(NEW.payload_json,'$.matched_entry_count')
            =invalidation.matched_entry_count
        AND json_extract(NEW.payload_json,'$.deleted_entry_count')
            =invalidation.deleted_entry_count
        AND json_extract(NEW.payload_json,'$.fenced_fill_count')
            =invalidation.fenced_fill_count
        AND json_extract(NEW.payload_json,'$.reason_code')
            =invalidation.reason_code
    )
  )
  OR EXISTS(
    SELECT 1 FROM response_cache_events current
    WHERE current.response_cache_event_id=NEW.response_cache_event_id
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'response_cache_event_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_events_update_prd076
BEFORE UPDATE ON response_cache_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_event_immutable'); END;
CREATE TRIGGER trg_response_cache_events_delete_prd076
BEFORE DELETE ON response_cache_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_event_immutable'); END;

CREATE TABLE response_cache_event_outbox(
  tenant_id TEXT NOT NULL,
  response_cache_event_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(
    status IN('pending','leased','dispatched','dead_letter')
  ),
  attempt_count INTEGER NOT NULL CHECK(
    typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 10
  ),
  generation INTEGER NOT NULL CHECK(
    typeof(generation)='integer' AND generation>=1
  ),
  lease_owner_id TEXT,
  lease_token TEXT CHECK(
    lease_token IS NULL OR (
      length(lease_token)=64 AND lease_token NOT GLOB '*[^0-9a-f]*'
    )
  ),
  lease_expires_at TEXT CHECK(
    lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL
  ),
  subscription_snapshot_json TEXT CHECK(
    subscription_snapshot_json IS NULL OR (
      json_valid(subscription_snapshot_json)
      AND json_type(subscription_snapshot_json)='array'
    )
  ),
  snapshot_frozen_at TEXT CHECK(
    snapshot_frozen_at IS NULL OR unixepoch(snapshot_frozen_at) IS NOT NULL
  ),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  last_error_code TEXT CHECK(
    last_error_code IS NULL OR (
      length(last_error_code) BETWEEN 1 AND 128
      AND instr(last_error_code,char(0))=0
    )
  ),
  dispatched_at TEXT CHECK(
    dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL
  ),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(
    unixepoch(updated_at) IS NOT NULL
    AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  PRIMARY KEY(tenant_id,response_cache_event_id),
  FOREIGN KEY(tenant_id,response_cache_event_id)
    REFERENCES response_cache_events(tenant_id,response_cache_event_id),
  CHECK(
    (status='pending' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND dispatched_at IS NULL)
    OR (status='leased' AND lease_owner_id IS NOT NULL
      AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
      AND dispatched_at IS NULL)
    OR (status='dispatched' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND dispatched_at IS NOT NULL
      AND last_error_code IS NULL)
    OR (status='dead_letter' AND attempt_count=10
      AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND dispatched_at IS NULL
      AND last_error_code IS NOT NULL)
  ),
  CHECK(
    (subscription_snapshot_json IS NULL AND snapshot_frozen_at IS NULL)
    OR (subscription_snapshot_json IS NOT NULL AND snapshot_frozen_at IS NOT NULL)
  )
) WITHOUT ROWID;
CREATE INDEX idx_response_cache_event_outbox_due_prd076
  ON response_cache_event_outbox(
    status,next_attempt_at,lease_expires_at,tenant_id,response_cache_event_id
  );
CREATE TRIGGER trg_response_cache_event_outbox_owner_insert_prd076
BEFORE INSERT ON response_cache_event_outbox FOR EACH ROW
WHEN NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.generation!=1
  OR NOT EXISTS(
    SELECT 1 FROM response_cache_events event
    WHERE event.tenant_id=NEW.tenant_id
      AND event.response_cache_event_id=NEW.response_cache_event_id
      AND event.occurred_at=NEW.created_at
  )
BEGIN SELECT RAISE(ABORT,'response_cache_event_outbox_owner_mismatch'); END;
CREATE TRIGGER trg_response_cache_event_outbox_update_prd076
BEFORE UPDATE ON response_cache_event_outbox FOR EACH ROW
WHEN NEW.tenant_id!=OLD.tenant_id
  OR NEW.response_cache_event_id!=OLD.response_cache_event_id
  OR NEW.created_at!=OLD.created_at
  OR NEW.generation!=OLD.generation+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT (
    (OLD.status='pending' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count)
    OR (OLD.status='leased' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.lease_token!=OLD.lease_token)
    OR (OLD.status='leased' AND NEW.status='dispatched'
      AND NEW.attempt_count=OLD.attempt_count+1)
    OR (OLD.status='leased' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count+1 AND NEW.attempt_count<10)
    OR (OLD.status='leased' AND NEW.status='dead_letter'
      AND NEW.attempt_count=10)
  )
BEGIN SELECT RAISE(ABORT,'response_cache_event_outbox_transition_invalid'); END;
CREATE TRIGGER trg_response_cache_event_outbox_delete_prd076
BEFORE DELETE ON response_cache_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_event_outbox_immutable'); END;

-- source 与 outbox 必须同事务一一创建，任何单边事实均拒绝提交。
CREATE TRIGGER trg_response_cache_events_outbox_insert_prd076
AFTER INSERT ON response_cache_events FOR EACH ROW
BEGIN
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO response_cache_event_outbox(
    tenant_id,response_cache_event_id,status,attempt_count,generation,
    lease_owner_id,lease_token,lease_expires_at,subscription_snapshot_json,
    snapshot_frozen_at,next_attempt_at,last_error_code,dispatched_at,
    created_at,updated_at
  ) VALUES(
    NEW.tenant_id,NEW.response_cache_event_id,'pending',0,1,
    NULL,NULL,NULL,NULL,NULL,NEW.occurred_at,NULL,NULL,
    NEW.occurred_at,NEW.occurred_at
  );
END;

-- Usage 唯一语义变化是允许 usage_source=response_cache；其余列、rowid、NULL、
-- 索引、append-only、FinOps 投影和导出序列 trigger 全部按 0068 原样恢复。
DROP TABLE IF EXISTS usage_ledger_prd076_new;
CREATE TABLE usage_ledger_prd076_new (
  usage_ledger_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  raw_usage TEXT NOT NULL DEFAULT '{}',
  normalized_usage TEXT NOT NULL DEFAULT '{}',
  cost_amount NUMERIC NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'CNY',
  price_schema_version TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'recorded' CHECK (status IN ('recorded')),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
  cache_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_tokens >= 0),
  cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (cost_cny >= 0),
  cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  estimated INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1)),
  usage_source TEXT NOT NULL DEFAULT 'upstream'
    CHECK (usage_source IN ('upstream', 'estimated', 'response_cache')),
  price_snapshot_id TEXT,
  model_id TEXT,
  provider_id TEXT,
  virtual_key_id TEXT,
  endpoint TEXT,
  request_model TEXT,
  resolved_model TEXT,
  provider_code TEXT,
  updated_at TEXT,
  project_id TEXT,
  application_id TEXT,
  environment_id TEXT REFERENCES environments(environment_id),
  environment_code TEXT,
  routing_attempt_id TEXT,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, request_id) REFERENCES request_logs(tenant_id, request_id)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO usage_ledger_prd076_new(rowid,usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, raw_usage, normalized_usage, cost_amount, currency, price_schema_version, created_at, status, total_tokens, cache_tokens, cost_cny, cost_usd, estimated, usage_source, price_snapshot_id, model_id, provider_id, virtual_key_id, endpoint, request_model, resolved_model, provider_code, updated_at, project_id, application_id, environment_id, environment_code, routing_attempt_id)
SELECT rowid,usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, raw_usage, normalized_usage, cost_amount, currency, price_schema_version, created_at, status, total_tokens, cache_tokens, cost_cny, cost_usd, estimated, usage_source, price_snapshot_id, model_id, provider_id, virtual_key_id, endpoint, request_model, resolved_model, provider_code, updated_at, project_id, application_id, environment_id, environment_code, routing_attempt_id FROM usage_ledger;
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd076_usage_copy_guard;
CREATE TEMP TABLE prd076_usage_copy_guard(issue_count INTEGER NOT NULL
  CONSTRAINT prd076_usage_copy_guard_ok CHECK(issue_count=0));
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd076_usage_copy_guard(issue_count)
SELECT ABS((SELECT COUNT(*) FROM usage_ledger)
  -(SELECT COUNT(*) FROM usage_ledger_prd076_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM usage_ledger
      EXCEPT SELECT rowid,* FROM usage_ledger_prd076_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM usage_ledger_prd076_new
      EXCEPT SELECT rowid,* FROM usage_ledger));
DROP TABLE prd076_usage_copy_guard;
DROP TABLE usage_ledger;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE usage_ledger_prd076_new RENAME TO usage_ledger;
CREATE INDEX idx_usage_ledger_application_time
  ON usage_ledger(tenant_id, application_id, created_at DESC);
CREATE INDEX idx_usage_ledger_environment_time
  ON usage_ledger(tenant_id, environment_id, created_at DESC);
CREATE INDEX idx_usage_ledger_estimated_time
  ON usage_ledger(tenant_id, estimated, created_at DESC);
CREATE INDEX idx_usage_ledger_model_time
  ON usage_ledger(tenant_id, model_id, created_at DESC);
CREATE INDEX idx_usage_ledger_project_time
  ON usage_ledger(tenant_id, project_id, created_at DESC);
CREATE INDEX idx_usage_ledger_request_id ON usage_ledger(request_id);
CREATE UNIQUE INDEX idx_usage_ledger_request_owner_prd071
  ON usage_ledger(tenant_id,request_id,usage_ledger_id);
CREATE INDEX idx_usage_ledger_request_prd054
  ON usage_ledger(tenant_id, request_id, created_at, usage_ledger_id);
CREATE INDEX idx_usage_ledger_routing_attempt
  ON usage_ledger(tenant_id, routing_attempt_id);
CREATE INDEX idx_usage_ledger_tenant_id ON usage_ledger(tenant_id);
CREATE INDEX idx_usage_ledger_tenant_time
  ON usage_ledger(tenant_id, created_at DESC);
CREATE INDEX idx_usage_ledger_virtual_key_time
  ON usage_ledger(tenant_id, virtual_key_id, created_at DESC);
CREATE UNIQUE INDEX uq_usage_ledger_tenant_usage_prd051
  ON usage_ledger(tenant_id, usage_ledger_id);
CREATE TRIGGER trg_usage_ledger_export_append_only_delete
BEFORE DELETE ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;
CREATE TRIGGER trg_usage_ledger_export_append_only_update
BEFORE UPDATE ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;
CREATE TRIGGER trg_usage_ledger_export_sequence_insert
AFTER INSERT ON usage_ledger
FOR EACH ROW
BEGIN
  -- 新 Usage 与导出序列保持同事务追加，避免导出水位漏记账本。
  INSERT INTO usage_export_ledger_sequences(tenant_id, usage_ledger_id)
  VALUES(NEW.tenant_id, NEW.usage_ledger_id);
END;
CREATE TRIGGER trg_usage_ledger_finops_amount_insert_prd051
BEFORE INSERT ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NOT json_valid(NEW.normalized_usage)
    OR json_type(NEW.normalized_usage)!='object'
    THEN RAISE(ABORT, 'usage_finops_amount_invalid') END;
  -- cache hit 的 FinOps owner 必须在事务外预生成，供 commit-unknown 精确回读；
  -- 普通 Provider Usage 禁止借用这两个私有投影字段改变既有随机 owner 合同。
  SELECT CASE WHEN NEW.usage_source='response_cache' AND (
      json_type(NEW.normalized_usage,'$.finops_ledger_entry_id')!='text'
      OR length(json_extract(NEW.normalized_usage,'$.finops_ledger_entry_id'))!=36
      OR substr(json_extract(NEW.normalized_usage,'$.finops_ledger_entry_id'),1,4)!='fle_'
      OR substr(json_extract(NEW.normalized_usage,'$.finops_ledger_entry_id'),5)
           GLOB '*[^0-9a-f]*'
      OR json_type(NEW.normalized_usage,'$.finops_event_id')!='text'
      OR length(json_extract(NEW.normalized_usage,'$.finops_event_id'))!=38
      OR substr(json_extract(NEW.normalized_usage,'$.finops_event_id'),1,6)!='flevt_'
      OR substr(json_extract(NEW.normalized_usage,'$.finops_event_id'),7)
           GLOB '*[^0-9a-f]*'
    ) THEN RAISE(ABORT, 'usage_finops_owner_mismatch') END;
  SELECT CASE WHEN NEW.usage_source!='response_cache' AND (
      json_type(NEW.normalized_usage,'$.finops_ledger_entry_id') IS NOT NULL
      OR json_type(NEW.normalized_usage,'$.finops_event_id') IS NOT NULL
    ) THEN RAISE(ABORT, 'usage_finops_owner_mismatch') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') NOT IN ('text')
      AND json_type(NEW.normalized_usage, '$.cost_amount') IS NOT NULL
    THEN RAISE(ABORT, 'usage_finops_amount_invalid') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM (
      SELECT
        value.amount_text,
        CASE WHEN instr(value.amount_text, '.')=0
          THEN value.amount_text
          ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1)
        END AS integer_text,
        CASE WHEN instr(value.amount_text, '.')=0
          THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1)
        END AS fraction_text
      FROM (
        SELECT CASE
          WHEN json_type(NEW.normalized_usage, '$.cost_amount')='text'
            THEN json_extract(NEW.normalized_usage, '$.cost_amount')
          ELSE printf('%.6f', NEW.cost_amount)
        END AS amount_text
      ) value
    ) parts
    WHERE parts.amount_text=''
       OR parts.amount_text GLOB '*[^0-9.]*'
       OR parts.integer_text=''
       OR parts.integer_text GLOB '*[^0-9]*'
       OR length(parts.integer_text) NOT BETWEEN 1 AND 10
       OR (length(parts.integer_text)>1 AND substr(parts.integer_text, 1, 1)='0')
       OR (
         instr(parts.amount_text, '.')>0
         AND (
           parts.fraction_text=''
           OR parts.fraction_text GLOB '*[^0-9]*'
           OR length(parts.fraction_text) NOT BETWEEN 1 AND 6
         )
       )
       OR instr(substr(parts.amount_text, instr(parts.amount_text, '.')+1), '.')>0
       OR CAST(parts.integer_text AS INTEGER) * 1000000
          + CASE WHEN parts.fraction_text=''
              THEN 0
              ELSE CAST(substr(parts.fraction_text || '000000', 1, 6) AS INTEGER)
            END > 1000000000000000
  ) THEN RAISE(ABORT, 'usage_finops_amount_invalid') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount')='text'
    AND printf('%.6f', NEW.cost_amount) != (
      SELECT parts.integer_text || '.' || substr(parts.fraction_text || '000000', 1, 6)
      FROM (
        SELECT
          CASE WHEN instr(value.amount_text, '.')=0
            THEN value.amount_text
            ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1)
          END AS integer_text,
          CASE WHEN instr(value.amount_text, '.')=0
            THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1)
          END AS fraction_text
        FROM (
          SELECT json_extract(NEW.normalized_usage, '$.cost_amount') AS amount_text
        ) value
      ) parts
    ) THEN RAISE(ABORT, 'usage_finops_amount_mismatch') END;
  SELECT CASE WHEN NEW.currency NOT IN ('CNY', 'USD')
    THEN RAISE(ABORT, 'usage_finops_currency_invalid') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.currency') IS NOT NULL
    AND (
      json_type(NEW.normalized_usage, '$.currency')!='text'
      OR json_extract(NEW.normalized_usage, '$.currency')!=NEW.currency
    ) THEN RAISE(ABORT, 'usage_finops_currency_invalid') END;
  SELECT CASE WHEN NEW.currency='CNY'
    AND json_type(NEW.normalized_usage, '$.cost_amount')='text'
    AND (
      json_type(NEW.normalized_usage, '$.cost_cny')!='text'
      OR json_extract(NEW.normalized_usage, '$.cost_cny')
        !=json_extract(NEW.normalized_usage, '$.cost_amount')
    ) THEN RAISE(ABORT, 'usage_finops_cny_mismatch') END;
  SELECT CASE WHEN NEW.currency='USD'
    AND json_type(NEW.normalized_usage, '$.cost_cny') NOT IN ('null')
    AND json_type(NEW.normalized_usage, '$.cost_cny') IS NOT NULL
    THEN RAISE(ABORT, 'usage_finops_cny_unavailable') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.posting_source') IS NOT NULL
    AND (
      json_type(NEW.normalized_usage, '$.posting_source')!='text'
      OR json_extract(NEW.normalized_usage, '$.posting_source')
        NOT IN ('direct', 'spool_replay')
    ) THEN RAISE(ABORT, 'usage_finops_posting_source_invalid') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.price_snapshot_id')='text'
    AND json_extract(NEW.normalized_usage, '$.price_snapshot_id') IS NOT NEW.price_snapshot_id
    THEN RAISE(ABORT, 'usage_finops_snapshot_mismatch') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.price_schema_version')='text'
    AND json_extract(NEW.normalized_usage, '$.price_schema_version') IS NOT NEW.price_schema_version
    THEN RAISE(ABORT, 'usage_finops_snapshot_mismatch') END;
  -- 新 canonical Usage 的非零金额必须已经有可信版本快照；零价才允许自动补 unpriced-v1。
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount')='text'
    AND (
      SELECT CAST(parts.integer_text AS INTEGER) * 1000000
        + CASE WHEN parts.fraction_text=''
            THEN 0 ELSE CAST(substr(parts.fraction_text || '000000', 1, 6) AS INTEGER) END
      FROM (
        SELECT
          CASE WHEN instr(value.amount_text, '.')=0
            THEN value.amount_text
            ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1) END AS integer_text,
          CASE WHEN instr(value.amount_text, '.')=0
            THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1) END AS fraction_text
        FROM (
          SELECT json_extract(NEW.normalized_usage, '$.cost_amount') AS amount_text
        ) value
      ) parts
    )>0
    AND NOT EXISTS (
      SELECT 1 FROM price_snapshots snapshot
      WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
        AND snapshot.tenant_id=NEW.tenant_id
        AND snapshot.request_id=NEW.request_id
        AND snapshot.currency=NEW.currency
        AND NULLIF(trim(snapshot.price_schema_version), '') IS NOT NULL
        AND snapshot.price_schema_version=NEW.price_schema_version
    ) THEN RAISE(ABORT, 'usage_finops_price_evidence_missing') END;
END;
CREATE TRIGGER trg_usage_ledger_finops_conflict_insert_prd051
BEFORE INSERT ON usage_ledger
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM usage_ledger current
  WHERE current.usage_ledger_id=NEW.usage_ledger_id
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;
CREATE TRIGGER trg_usage_ledger_finops_owner_insert_prd051
BEFORE INSERT ON usage_ledger
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM request_logs request
  WHERE request.tenant_id=NEW.tenant_id AND request.request_id=NEW.request_id
)
OR (
  NEW.price_snapshot_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM price_snapshots snapshot
    WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
      AND snapshot.tenant_id=NEW.tenant_id
      AND snapshot.request_id=NEW.request_id
      AND snapshot.currency=NEW.currency
  )
)
OR (
  NEW.model_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM models model
    WHERE model.model_id=NEW.model_id AND model.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.provider_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM providers provider
    WHERE provider.provider_id=NEW.provider_id AND provider.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.virtual_key_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM virtual_keys key
    WHERE key.virtual_key_id=NEW.virtual_key_id AND key.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.environment_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM environments environment
    WHERE environment.environment_id=NEW.environment_id
      AND environment.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.project_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM projects project
    WHERE project.project_id=NEW.project_id AND project.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.application_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM applications application
    WHERE application.application_id=NEW.application_id
      AND application.tenant_id=NEW.tenant_id
      AND (NEW.project_id IS NULL OR application.project_id=NEW.project_id)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'usage_finops_owner_mismatch');
END;
CREATE TRIGGER trg_usage_ledger_finops_projection_insert_prd051
AFTER INSERT ON usage_ledger
FOR EACH ROW
BEGIN
  -- 缺失或不可信的价格快照只创建当前 Usage 专属证据，禁止借用当前模型价格回算历史金额。
  INSERT INTO price_snapshots(
    price_snapshot_id, tenant_id, request_id, model_id, provider_id, provider_code,
    currency, input_price_per_1k, output_price_per_1k, price_schema_version,
    billing_dimensions, provider_raw_mapping, source, status,
    created_at, updated_at, price_plan_id, effective_from
  )
  SELECT
    'prs_' || lower(hex(randomblob(16))),
    NEW.tenant_id,
    NEW.request_id,
    NEW.model_id,
    NEW.provider_id,
    NEW.provider_code,
    NEW.currency,
    CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
      THEN COALESCE((
        SELECT snapshot.input_price_per_1k FROM price_snapshots snapshot
        WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
      ), 0) ELSE 0 END,
    CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
      THEN COALESCE((
        SELECT snapshot.output_price_per_1k FROM price_snapshots snapshot
        WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
      ), 0) ELSE 0 END,
    CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
      THEN 'legacy-unversioned-v1' ELSE 'unpriced-v1' END,
    json_object(
      'finops_usage_ledger_id', NEW.usage_ledger_id,
      'price_evidence_status', CASE
        WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
          THEN 'legacy_amount_only' ELSE 'unpriced' END
    ),
    '{}',
    CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
      THEN 'finops_legacy_backfill' ELSE 'finops_unpriced' END,
    'active',
    NEW.created_at,
    NEW.created_at,
    NULL,
    NEW.created_at
  WHERE NOT EXISTS (
    SELECT 1 FROM price_snapshots snapshot
    WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
      AND snapshot.tenant_id=NEW.tenant_id
      AND snapshot.request_id=NEW.request_id
      AND snapshot.currency=NEW.currency
      AND NULLIF(trim(snapshot.price_schema_version), '') IS NOT NULL
      AND snapshot.price_schema_version=NEW.price_schema_version
  );

  -- 正式分录与新 Usage 在同一事务产生，唯一约束保证 tenant+Usage 只投影一次。
  INSERT INTO finops_ledger_entries(
    finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
    amount_micros, currency, normalized_cny_micros, cny_normalization_status,
    price_evidence_status, price_snapshot_source, posting_source, quota_type,
    usage_ledger_id, finops_adjustment_id, source_request_id, source_trace_id,
    operation_request_id, operation_trace_id, price_snapshot_id,
    price_schema_version, reversal_of_entry_id, actor_user_id,
    input_tokens, output_tokens, total_tokens, cache_tokens,
    model_id, provider_id, virtual_key_id, environment_id, project_id,
    application_id, estimated, status, created_at, updated_at
  )
  SELECT
    CASE WHEN NEW.usage_source='response_cache'
      THEN json_extract(NEW.normalized_usage,'$.finops_ledger_entry_id')
      ELSE 'fle_' || lower(hex(randomblob(16))) END,
    CASE WHEN NEW.usage_source='response_cache'
      THEN json_extract(NEW.normalized_usage,'$.finops_event_id')
      ELSE 'flevt_' || lower(hex(randomblob(16))) END,
    NEW.tenant_id,
    'usage_charge',
    'debit',
    amount.amount_micros,
    NEW.currency,
    CASE WHEN NEW.currency='CNY'
      AND NOT (
        snapshot.source='response_cache_reference'
        AND snapshot.price_schema_version='unpriced-v1'
      ) THEN amount.amount_micros ELSE NULL END,
    CASE WHEN NEW.currency='CNY'
      AND NOT (
        snapshot.source='response_cache_reference'
        AND snapshot.price_schema_version='unpriced-v1'
      ) THEN 'normalized' ELSE 'unavailable' END,
    CASE
      WHEN snapshot.source='finops_unpriced' THEN 'unpriced'
      WHEN snapshot.source='finops_legacy_backfill' THEN 'legacy_amount_only'
      WHEN snapshot.source='response_cache_reference'
        AND snapshot.price_schema_version='unpriced-v1' THEN 'unpriced'
      ELSE 'priced'
    END,
    snapshot.source,
    CASE WHEN json_extract(NEW.normalized_usage, '$.posting_source')='spool_replay'
      THEN 'spool_replay' ELSE 'direct' END,
    'paid',
    NEW.usage_ledger_id,
    NULL,
    NEW.request_id,
    request.trace_id,
    NEW.request_id,
    request.trace_id,
    snapshot.price_snapshot_id,
    snapshot.price_schema_version,
    NULL,
    NULL,
    NEW.input_tokens,
    NEW.output_tokens,
    NEW.total_tokens,
    NEW.cache_tokens,
    NEW.model_id,
    NEW.provider_id,
    NEW.virtual_key_id,
    NEW.environment_id,
    NEW.project_id,
    NEW.application_id,
    NEW.estimated,
    'posted',
    NEW.created_at,
    NEW.created_at
  FROM (
    SELECT
      CAST(parts.integer_text AS INTEGER) * 1000000
      + CASE WHEN parts.fraction_text=''
          THEN 0 ELSE CAST(substr(parts.fraction_text || '000000', 1, 6) AS INTEGER) END
        AS amount_micros
    FROM (
      SELECT
        CASE WHEN instr(value.amount_text, '.')=0
          THEN value.amount_text
          ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1) END AS integer_text,
        CASE WHEN instr(value.amount_text, '.')=0
          THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1) END AS fraction_text
      FROM (
        SELECT CASE
          WHEN json_type(NEW.normalized_usage, '$.cost_amount')='text'
            THEN json_extract(NEW.normalized_usage, '$.cost_amount')
          ELSE printf('%.6f', NEW.cost_amount)
        END AS amount_text
      ) value
    ) parts
  ) amount
  JOIN request_logs request
    ON request.tenant_id=NEW.tenant_id AND request.request_id=NEW.request_id
  JOIN price_snapshots snapshot
    ON snapshot.tenant_id=NEW.tenant_id
   AND snapshot.price_snapshot_id=COALESCE(
     (
       SELECT trusted.price_snapshot_id FROM price_snapshots trusted
       WHERE trusted.price_snapshot_id=NEW.price_snapshot_id
         AND trusted.tenant_id=NEW.tenant_id
         AND trusted.request_id=NEW.request_id
         AND trusted.currency=NEW.currency
         AND NULLIF(trim(trusted.price_schema_version), '') IS NOT NULL
         AND trusted.price_schema_version=NEW.price_schema_version
     ),
     (
       SELECT synthetic.price_snapshot_id FROM price_snapshots synthetic
       WHERE synthetic.tenant_id=NEW.tenant_id
         AND synthetic.source IN ('finops_unpriced', 'finops_legacy_backfill')
         AND json_extract(
           synthetic.billing_dimensions, '$.finops_usage_ledger_id'
         )=NEW.usage_ledger_id
     )
   );
END;
CREATE TRIGGER trg_usage_routing_attempt_insert
BEFORE INSERT ON usage_ledger
FOR EACH ROW
WHEN NEW.routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
      AND ra.status='succeeded'
  ) THEN RAISE(ABORT, 'usage_routing_attempt_mismatch') END;
END;

-- PRD-076 Output Safety preservation rebuild.
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS output_safety_scans_prd076_new;
CREATE TABLE output_safety_scans_prd076_new (
  output_safety_scan_id TEXT PRIMARY KEY CHECK (
    length(output_safety_scan_id)=36
    AND substr(output_safety_scan_id,1,4)='oss_'
    AND substr(output_safety_scan_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128 AND instr(request_id,char(0))=0
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128 AND instr(trace_id,char(0))=0
  ),
  stream_session_id TEXT,
  scan_mode TEXT NOT NULL CHECK (scan_mode IN ('non_stream_full','stream_window')),
  scan_status TEXT NOT NULL CHECK (
    scan_status IN ('pending','not_configured','completed','unavailable','not_applicable')
  ),
  decision TEXT NOT NULL CHECK (decision IN ('none','clean','matched','blocked','unknown')),
  action TEXT CHECK (action IS NULL OR action IN ('allow','log_only','block')),
  dlp_rule_id TEXT,
  rule_type TEXT CHECK (rule_type IS NULL OR rule_type IN ('keyword','regex')),
  match_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(match_count)='integer' AND match_count>=0
  ),
  ruleset_fingerprint TEXT CHECK (
    ruleset_fingerprint IS NULL OR (
      length(ruleset_fingerprint)=64
      AND ruleset_fingerprint NOT GLOB '*[^0-9a-f]*'
    )
  ),
  detector_version TEXT NOT NULL DEFAULT 'output-safety-dlp-v1'
    CHECK (detector_version='output-safety-dlp-v1'),
  interval_tokens INTEGER CHECK (
    interval_tokens IS NULL OR (
      typeof(interval_tokens)='integer' AND interval_tokens BETWEEN 8 AND 256
    )
  ),
  window_tokens INTEGER CHECK (
    window_tokens IS NULL OR (
      typeof(window_tokens)='integer' AND window_tokens BETWEEN 32 AND 1024
    )
  ),
  checkpoint_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(checkpoint_count)='integer' AND checkpoint_count>=0
  ),
  observed_output_token_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(observed_output_token_count)='integer' AND observed_output_token_count>=0
  ),
  scanned_token_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(scanned_token_count)='integer' AND scanned_token_count>=0
  ),
  emitted_token_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(emitted_token_count)='integer' AND emitted_token_count>=0
  ),
  emitted_sse_event_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(emitted_sse_event_count)='integer' AND emitted_sse_event_count>=0
  ),
  emitted_bytes INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(emitted_bytes)='integer' AND emitted_bytes>=0
  ),
  exposure_state TEXT NOT NULL CHECK (
    exposure_state IN ('none','partial','complete','unknown')
  ),
  billing_disposition TEXT NOT NULL CHECK (
    billing_disposition IN ('not_required','dispute_pending','not_billable_response_cache_candidate')
  ),
  failure_reason_code TEXT CHECK (
    failure_reason_code IS NULL OR failure_reason_code IN (
      'output_safety.fragment_limit_exceeded',
      'output_safety.response_shape_invalid',
      'output_safety.response_too_large',
      'output_safety.evidence_unavailable',
      'output_safety.rule_invalid',
      'output_safety.rule_snapshot_unavailable',
      'output_safety.sse_event_incomplete',
      'output_safety.sse_event_too_large',
      'output_safety.sse_json_duplicate_key',
      'output_safety.sse_json_invalid',
      'output_safety.sse_utf8_invalid',
      'output_safety.stream_incomplete',
      'output_safety.window_capacity_exceeded'
    )
  ),
  started_at TEXT NOT NULL CHECK (
    length(started_at) BETWEEN 1 AND 64 AND unixepoch(started_at) IS NOT NULL
  ),
  completed_at TEXT CHECK (
    completed_at IS NULL OR (
      length(completed_at) BETWEEN 1 AND 64 AND unixepoch(completed_at) IS NOT NULL
      AND unixepoch(completed_at)>=unixepoch(started_at)
    )
  ),
  UNIQUE (tenant_id,request_id),
  UNIQUE (tenant_id,output_safety_scan_id),
  UNIQUE (tenant_id,request_id,output_safety_scan_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,request_id)
    REFERENCES gateway_request_claims(tenant_id,request_id),
  FOREIGN KEY (tenant_id,dlp_rule_id)
    REFERENCES dlp_rules(tenant_id,dlp_rule_id),
  FOREIGN KEY (tenant_id,request_id,stream_session_id)
    REFERENCES stream_sessions(tenant_id,request_id,stream_session_id),
  -- 非流式没有会话/窗口参数；流式三者必须同时存在且窗口不得小于检查间隔。
  CHECK (
    (scan_mode='non_stream_full' AND stream_session_id IS NULL
      AND interval_tokens IS NULL AND window_tokens IS NULL)
    OR (scan_mode='stream_window' AND stream_session_id IS NOT NULL
      AND interval_tokens IS NOT NULL AND window_tokens IS NOT NULL
      AND window_tokens>=interval_tokens)
  ),
  CHECK (emitted_token_count<=scanned_token_count),
  CHECK (scanned_token_count<=observed_output_token_count),
  CHECK (scan_status!='completed' OR checkpoint_count>=1),
  -- 非流式阻断发生在完整响应返回前，数据库不得接受 partial 或任何已交付计数。
  CHECK (
    scan_status!='completed' OR decision!='blocked' OR scan_mode!='non_stream_full'
    OR (exposure_state='none' AND emitted_token_count=0
      AND emitted_sse_event_count=0 AND emitted_bytes=0)
  ),
  -- 状态矩阵把“不配置”“完整 clean”和“不可证明”严格分开，任何未知历史都不能伪造成 clean。
  CHECK (
    (scan_status='pending' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NULL)
    OR
    (scan_status='not_configured' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NOT NULL)
    OR
    (scan_status='not_applicable' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='clean' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND exposure_state='complete'
      AND billing_disposition='not_required' AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='matched' AND action IN ('allow','log_only')
      AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL AND match_count>=1
      AND ruleset_fingerprint IS NOT NULL AND exposure_state='complete'
      AND billing_disposition='not_required' AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='blocked' AND action='block'
      AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL AND match_count>=1
      AND ruleset_fingerprint IS NOT NULL AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL
      AND (
        (billing_disposition='dispute_pending'
          AND exposure_state IN ('none','partial'))
        OR
        (billing_disposition='not_billable_response_cache_candidate'
          AND scan_mode='non_stream_full' AND exposure_state='none'
          AND emitted_token_count=0 AND emitted_sse_event_count=0
          AND emitted_bytes=0)
      ))
    OR
    (scan_status='unavailable' AND decision IN ('unknown','matched')
      AND exposure_state IN ('none','partial','unknown')
      AND billing_disposition='not_required' AND failure_reason_code IS NOT NULL
      AND completed_at IS NOT NULL
      AND ((decision='unknown' AND action IS NULL AND dlp_rule_id IS NULL
          AND rule_type IS NULL AND match_count=0)
        OR (decision='matched' AND action IN ('allow','log_only')
          AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL
          AND match_count>=1 AND ruleset_fingerprint IS NOT NULL)))
  )
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO output_safety_scans_prd076_new(rowid,output_safety_scan_id, tenant_id, request_id, trace_id, stream_session_id, scan_mode, scan_status, decision, action, dlp_rule_id, rule_type, match_count, ruleset_fingerprint, detector_version, interval_tokens, window_tokens, checkpoint_count, observed_output_token_count, scanned_token_count, emitted_token_count, emitted_sse_event_count, emitted_bytes, exposure_state, billing_disposition, failure_reason_code, started_at, completed_at)
SELECT rowid,output_safety_scan_id, tenant_id, request_id, trace_id, stream_session_id, scan_mode, scan_status, decision, action, dlp_rule_id, rule_type, match_count, ruleset_fingerprint, detector_version, interval_tokens, window_tokens, checkpoint_count, observed_output_token_count, scanned_token_count, emitted_token_count, emitted_sse_event_count, emitted_bytes, exposure_state, billing_disposition, failure_reason_code, started_at, completed_at FROM output_safety_scans;
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS output_safety_events_prd076_new;
CREATE TABLE output_safety_events_prd076_new (
  event_id TEXT PRIMARY KEY CHECK (
    length(event_id)=38 AND substr(event_id,1,6)='osevt_'
    AND substr(event_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  output_safety_scan_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type='security.output_blocked'),
  schema_version INTEGER NOT NULL CHECK (schema_version=1),
  scan_mode TEXT NOT NULL CHECK (scan_mode IN ('non_stream_full','stream_window')),
  scan_status TEXT NOT NULL CHECK (scan_status='completed'),
  decision TEXT NOT NULL CHECK (decision='blocked'),
  error_code TEXT NOT NULL CHECK (error_code='content_policy_violation'),
  exposure_state TEXT NOT NULL CHECK (exposure_state IN ('none','partial')),
  emitted_token_count INTEGER NOT NULL CHECK (
    typeof(emitted_token_count)='integer' AND emitted_token_count>=0
  ),
  checkpoint_count INTEGER NOT NULL CHECK (
    typeof(checkpoint_count)='integer' AND checkpoint_count>=0
  ),
  dlp_rule_id TEXT NOT NULL,
  detector_version TEXT NOT NULL CHECK (detector_version='output-safety-dlp-v1'),
  billing_disposition TEXT NOT NULL CHECK (
    billing_disposition IN (
      'dispute_pending','not_billable_response_cache_candidate'
    )
  ),
  occurred_at TEXT NOT NULL CHECK (
    length(occurred_at) BETWEEN 1 AND 64 AND unixepoch(occurred_at) IS NOT NULL
  ),
  UNIQUE (tenant_id,output_safety_scan_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,request_id)
    REFERENCES gateway_request_claims(tenant_id,request_id),
  FOREIGN KEY (tenant_id,request_id,output_safety_scan_id)
    REFERENCES output_safety_scans(tenant_id,request_id,output_safety_scan_id),
  CHECK (
    billing_disposition!='not_billable_response_cache_candidate'
    OR (
      scan_mode='non_stream_full' AND exposure_state='none'
      AND emitted_token_count=0
    )
  )
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO output_safety_events_prd076_new(rowid,event_id, tenant_id, output_safety_scan_id, request_id, trace_id, event_type, schema_version, scan_mode, scan_status, decision, error_code, exposure_state, emitted_token_count, checkpoint_count, dlp_rule_id, detector_version, billing_disposition, occurred_at)
SELECT rowid,event_id, tenant_id, output_safety_scan_id, request_id, trace_id, event_type, schema_version, scan_mode, scan_status, decision, error_code, exposure_state, emitted_token_count, checkpoint_count, dlp_rule_id, detector_version, billing_disposition, occurred_at FROM output_safety_events;
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd076_output_safety_copy_guard;
CREATE TEMP TABLE prd076_output_safety_copy_guard(issue_count INTEGER NOT NULL
  CONSTRAINT prd076_output_safety_copy_guard_ok CHECK(issue_count=0));
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd076_output_safety_copy_guard(issue_count)
SELECT ABS((SELECT COUNT(*) FROM output_safety_scans)
  -(SELECT COUNT(*) FROM output_safety_scans_prd076_new))
 + ABS((SELECT COUNT(*) FROM output_safety_events)
  -(SELECT COUNT(*) FROM output_safety_events_prd076_new))
 + (SELECT COUNT(*) FROM (SELECT rowid,* FROM output_safety_scans
      EXCEPT SELECT rowid,* FROM output_safety_scans_prd076_new))
 + (SELECT COUNT(*) FROM (SELECT rowid,* FROM output_safety_scans_prd076_new
      EXCEPT SELECT rowid,* FROM output_safety_scans))
 + (SELECT COUNT(*) FROM (SELECT rowid,* FROM output_safety_events
      EXCEPT SELECT rowid,* FROM output_safety_events_prd076_new))
 + (SELECT COUNT(*) FROM (SELECT rowid,* FROM output_safety_events_prd076_new
      EXCEPT SELECT rowid,* FROM output_safety_events));
DROP TABLE prd076_output_safety_copy_guard;
DROP TABLE output_safety_events;
DROP TABLE output_safety_scans;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE output_safety_scans_prd076_new RENAME TO output_safety_scans;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE output_safety_events_prd076_new RENAME TO output_safety_events;
CREATE INDEX idx_output_safety_scans_tenant_status_time_prd071
  ON output_safety_scans(tenant_id,scan_status,decision,started_at DESC);
CREATE INDEX idx_output_safety_scans_tenant_time_prd071
  ON output_safety_scans(tenant_id,started_at DESC,output_safety_scan_id DESC);
CREATE TRIGGER trg_output_safety_scans_conflict_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM output_safety_scans current
  WHERE current.output_safety_scan_id=NEW.output_safety_scan_id
     OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_immutable'); END;
CREATE TRIGGER trg_output_safety_scans_delete_prd071
BEFORE DELETE ON output_safety_scans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_scan_immutable'); END;
CREATE TRIGGER trg_output_safety_scans_owner_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id AND claim.request_id=NEW.request_id
      AND claim.trace_id=NEW.trace_id
  )
  OR (NEW.scan_mode='stream_window' AND NOT EXISTS (
    SELECT 1 FROM stream_sessions session
    WHERE session.tenant_id=NEW.tenant_id AND session.request_id=NEW.request_id
      AND session.stream_session_id=NEW.stream_session_id
      AND session.trace_id=NEW.trace_id
  ))
  OR (NEW.dlp_rule_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM dlp_rules rule
    WHERE rule.tenant_id=NEW.tenant_id AND rule.dlp_rule_id=NEW.dlp_rule_id
      AND rule.rule_type=NEW.rule_type
  ))
BEGIN SELECT RAISE(ABORT,'output_safety_scan_owner_mismatch'); END;
CREATE TRIGGER trg_output_safety_scans_owner_update_prd071
BEFORE UPDATE ON output_safety_scans FOR EACH ROW
WHEN NEW.rowid IS NOT OLD.rowid
  OR NEW.output_safety_scan_id IS NOT OLD.output_safety_scan_id
  OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.request_id IS NOT OLD.request_id
  OR NEW.trace_id IS NOT OLD.trace_id
  OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.scan_mode IS NOT OLD.scan_mode
  OR NEW.detector_version IS NOT OLD.detector_version
  OR NEW.interval_tokens IS NOT OLD.interval_tokens
  OR NEW.window_tokens IS NOT OLD.window_tokens
  OR NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT,'output_safety_scan_owner_immutable'); END;
CREATE TRIGGER trg_output_safety_scans_rule_update_prd071
BEFORE UPDATE OF dlp_rule_id,rule_type ON output_safety_scans FOR EACH ROW
WHEN NEW.dlp_rule_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM dlp_rules rule
  WHERE rule.tenant_id=NEW.tenant_id AND rule.dlp_rule_id=NEW.dlp_rule_id
    AND rule.rule_type=NEW.rule_type
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_rule_owner_mismatch'); END;
CREATE TRIGGER trg_output_safety_scans_state_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN NEW.scan_status!='pending'
BEGIN SELECT RAISE(ABORT,'output_safety_scan_initial_state_invalid'); END;
CREATE TRIGGER trg_output_safety_scans_transition_prd071
BEFORE UPDATE ON output_safety_scans FOR EACH ROW
WHEN NOT (
  (NEW.rowid IS OLD.rowid
    AND NEW.output_safety_scan_id IS OLD.output_safety_scan_id
    AND NEW.tenant_id IS OLD.tenant_id AND NEW.request_id IS OLD.request_id
    AND NEW.trace_id IS OLD.trace_id
    AND NEW.stream_session_id IS OLD.stream_session_id
    AND NEW.scan_mode IS OLD.scan_mode AND NEW.scan_status IS OLD.scan_status
    AND NEW.decision IS OLD.decision AND NEW.action IS OLD.action
    AND NEW.dlp_rule_id IS OLD.dlp_rule_id AND NEW.rule_type IS OLD.rule_type
    AND NEW.match_count IS OLD.match_count
    AND NEW.ruleset_fingerprint IS OLD.ruleset_fingerprint
    AND NEW.detector_version IS OLD.detector_version
    AND NEW.interval_tokens IS OLD.interval_tokens
    AND NEW.window_tokens IS OLD.window_tokens
    AND NEW.checkpoint_count IS OLD.checkpoint_count
    AND NEW.observed_output_token_count IS OLD.observed_output_token_count
    AND NEW.scanned_token_count IS OLD.scanned_token_count
    AND NEW.emitted_token_count IS OLD.emitted_token_count
    AND NEW.emitted_sse_event_count IS OLD.emitted_sse_event_count
    AND NEW.emitted_bytes IS OLD.emitted_bytes
    AND NEW.exposure_state IS OLD.exposure_state
    AND NEW.billing_disposition IS OLD.billing_disposition
    AND NEW.failure_reason_code IS OLD.failure_reason_code
    AND NEW.started_at IS OLD.started_at AND NEW.completed_at IS OLD.completed_at)
  OR (OLD.scan_status='pending' AND NEW.scan_status IN (
    'not_configured','completed','unavailable','not_applicable'
  ))
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_transition_invalid'); END;
CREATE INDEX idx_output_safety_events_tenant_time_prd071
  ON output_safety_events(tenant_id,occurred_at DESC,event_id DESC);
CREATE TRIGGER trg_output_safety_events_conflict_insert_prd071
BEFORE INSERT ON output_safety_events FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM output_safety_events current
  WHERE current.event_id=NEW.event_id
     OR (current.tenant_id=NEW.tenant_id
       AND current.output_safety_scan_id=NEW.output_safety_scan_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'output_safety_event_immutable'); END;
CREATE TRIGGER trg_output_safety_events_delete_prd071
BEFORE DELETE ON output_safety_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_event_immutable'); END;
CREATE TRIGGER trg_output_safety_events_owner_insert_prd071
BEFORE INSERT ON output_safety_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM output_safety_scans scan
  WHERE scan.tenant_id=NEW.tenant_id AND scan.request_id=NEW.request_id
    AND scan.output_safety_scan_id=NEW.output_safety_scan_id
    AND scan.trace_id=NEW.trace_id AND scan.scan_status='completed'
    AND scan.decision='blocked' AND scan.scan_mode=NEW.scan_mode
    AND scan.exposure_state=NEW.exposure_state
    AND scan.emitted_token_count=NEW.emitted_token_count
    AND scan.checkpoint_count=NEW.checkpoint_count
    AND scan.dlp_rule_id=NEW.dlp_rule_id
    AND scan.detector_version=NEW.detector_version
    AND scan.billing_disposition=NEW.billing_disposition
    AND scan.completed_at=NEW.occurred_at
)
BEGIN SELECT RAISE(ABORT,'output_safety_event_owner_mismatch'); END;
CREATE TRIGGER trg_output_safety_events_update_prd071
BEFORE UPDATE ON output_safety_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_event_immutable'); END;
CREATE TRIGGER trg_output_safety_scans_cache_candidate_prd076
BEFORE UPDATE ON output_safety_scans FOR EACH ROW
WHEN NEW.billing_disposition='not_billable_response_cache_candidate'
  AND NOT EXISTS(
    SELECT 1 FROM response_cache_request_facts fact
    WHERE fact.tenant_id=NEW.tenant_id AND fact.request_id=NEW.request_id
      AND fact.trace_id=NEW.trace_id AND fact.status='hit_candidate'
      AND NEW.scan_mode='non_stream_full' AND NEW.scan_status='completed'
      AND NEW.decision='blocked' AND NEW.exposure_state='none'
      AND NEW.emitted_token_count=0 AND NEW.emitted_sse_event_count=0
      AND NEW.emitted_bytes=0
  )
BEGIN SELECT RAISE(ABORT,'output_safety_cache_candidate_owner_mismatch'); END;
CREATE TRIGGER trg_output_safety_events_cache_candidate_prd076
BEFORE INSERT ON output_safety_events FOR EACH ROW
WHEN NEW.billing_disposition='not_billable_response_cache_candidate'
  AND NOT EXISTS(
    SELECT 1 FROM response_cache_request_facts fact
    WHERE fact.tenant_id=NEW.tenant_id AND fact.request_id=NEW.request_id
      AND fact.trace_id=NEW.trace_id AND fact.status='hit_candidate'
      AND NEW.scan_mode='non_stream_full' AND NEW.exposure_state='none'
      AND NEW.emitted_token_count=0
  )
BEGIN SELECT RAISE(ABORT,'output_safety_cache_candidate_owner_mismatch'); END;

-- PRD-076 Audit preservation rebuild.
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS audit_logs_prd076_new;
CREATE TABLE audit_logs_prd076_new (
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
      'response_cache.invalidated'
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
      'response_cache_policy','response_cache_invalidation'
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
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO audit_logs_prd076_new(rowid,audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action, object_type, object_id, object_name, before_summary_json, after_summary_json, changed_fields_json, client_ip, user_agent, request_id, trace_id, created_at)
SELECT rowid,audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action, object_type, object_id, object_name, before_summary_json, after_summary_json, changed_fields_json, client_ip, user_agent, request_id, trace_id, created_at FROM audit_logs;
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd076_audit_copy_guard;
CREATE TEMP TABLE prd076_audit_copy_guard(issue_count INTEGER NOT NULL
  CONSTRAINT prd076_audit_copy_guard_ok CHECK(issue_count=0));
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd076_audit_copy_guard(issue_count)
SELECT ABS((SELECT COUNT(*) FROM audit_logs)
  -(SELECT COUNT(*) FROM audit_logs_prd076_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd076_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs_prd076_new
      EXCEPT SELECT rowid,* FROM audit_logs));
DROP TABLE prd076_audit_copy_guard;
DROP TABLE audit_logs;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE audit_logs_prd076_new RENAME TO audit_logs;
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
CREATE UNIQUE INDEX idx_audit_logs_policy_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN ('policy_template.applied','policy_template.rolled_back');
CREATE UNIQUE INDEX idx_audit_logs_prompt_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN (
    'prompt_template.created','prompt_template.updated',
    'prompt_template.version_published'
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
CREATE UNIQUE INDEX idx_audit_logs_response_cache_owner_prd076
  ON audit_logs(tenant_id,action,object_id,request_id)
  WHERE action IN (
    'response_cache.policy_created','response_cache.policy_updated',
    'response_cache.invalidated'
  );
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

-- 持久 DDL 全部完成后做 exact 数量、租户投影、依赖与结构健康门禁。任何失败都会回滚
-- RBAC、缓存表和四次保真重建，不能留下可被应用误识别的半套 0069。
DROP TABLE IF EXISTS temp.prd076_up_final_guard;
CREATE TEMP TABLE prd076_up_final_guard(
  issue_count INTEGER NOT NULL
    CONSTRAINT prd076_up_final_guard_ok CHECK(issue_count=0)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd076_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=69 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates
               WHERE catalog_revision!=12)=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=185
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*69 FROM tenants) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions
               WHERE catalog_revision!=12 OR status!='active')=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT tenant.tenant_id,template.permission_code
      FROM tenants tenant CROSS JOIN rbac_permission_templates template
      EXCEPT SELECT tenant_id,code FROM permissions
    ) AND NOT EXISTS(
      SELECT tenant_id,code FROM permissions
      EXCEPT
      SELECT tenant.tenant_id,template.permission_code
      FROM tenants tenant CROSS JOIN rbac_permission_templates template
    ) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'response_cache_key_state','response_cache_policies',
      'response_cache_policy_events','response_cache_entries',
      'response_cache_entry_tags','response_cache_tag_generations',
      'response_cache_key_generations','response_cache_fill_leases',
      'response_cache_fill_tags','response_cache_request_facts',
      'response_cache_hits','response_cache_invalidations',
      'response_cache_events','response_cache_event_outbox'
    ))=14 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM response_cache_key_state
      WHERE key_state_id=1 AND key_schema_version=1
        AND active_hmac_key_version=1 AND active_encryption_key_version=1
        AND generation=1)=1 THEN 0 ELSE 1 END
  + CASE WHEN instr(
      (SELECT sql FROM sqlite_schema
       WHERE type='table' AND name='usage_ledger'),
      "'response_cache'"
    )>0 THEN 0 ELSE 1 END
  + CASE WHEN instr(
      (SELECT sql FROM sqlite_schema
       WHERE type='table' AND name='finops_ledger_entries'),
      "price_snapshot_source IN ('finops_unpriced', 'response_cache_reference')"
    )>0 THEN 0 ELSE 1 END
  + CASE WHEN instr(
      (SELECT sql FROM sqlite_schema
       WHERE type='trigger' AND name='trg_finops_ledger_owner_insert_prd051'),
      "snapshot.source='response_cache_reference'"
    )>0 THEN 0 ELSE 1 END
  + CASE WHEN instr(
      (SELECT sql FROM sqlite_schema
       WHERE type='table' AND name='output_safety_scans'),
      "'not_billable_response_cache_candidate'"
    )>0 THEN 0 ELSE 1 END
  + CASE WHEN instr(
      (SELECT sql FROM sqlite_schema
       WHERE type='table' AND name='audit_logs'),
      "'response_cache.policy_created'"
    )>0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT tenant_id,response_cache_event_id FROM response_cache_events
      EXCEPT
      SELECT tenant_id,response_cache_event_id FROM response_cache_event_outbox
    ) AND NOT EXISTS(
      SELECT tenant_id,response_cache_event_id FROM response_cache_event_outbox
      EXCEPT SELECT tenant_id,response_cache_event_id FROM response_cache_events
    ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd076_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
