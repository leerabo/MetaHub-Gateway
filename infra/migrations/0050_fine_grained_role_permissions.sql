-- 迁移职责：为 PRD-059 建立六角色、48 个稳定权限点和租户内表驱动矩阵，
-- 同时保真扩展 AuditLog 的角色、动作与对象枚举。
-- 安全边界：产品模板是无 tenant_id 的发布元数据；角色、权限、关联和事件事实均以
-- tenant_id 参与复合关系。任何表都不保存 Token、Provider Key、Prompt、Response、
-- Authorization、Cookie、Webhook 正文或 DLP pattern。
--
-- 0049 已有两个入向复合外键指向 audit_logs。SQLite 在 foreign_keys=ON 时不能直接
-- 换父表，因此必须在显式事务外关闭 FK，事务末尾用 foreign_key_check 复验后再恢复。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 0050 只允许从完整 0049 栈顶升级。未知依赖若随换表被破坏，必须在首项持久 DDL 前失败。
CREATE TEMP TABLE prd059_up_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd059_up_guard
BEFORE INSERT ON prd059_up_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd059_prerequisite_failed'); END;
-- 在任何持久 DDL 前核验 0049 栈顶、旧三角色和 PRD-058 父子事实，失败必须原子终止。
INSERT INTO prd059_up_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0049' AND status='applied')=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE status='applied' AND version>'0049')=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','users','roles','audit_logs','audit_log_export_sequences',
        'audit_exports','audit_export_events'
      ))=7 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM users
      WHERE role NOT IN ('admin','developer','viewer'))
  + (SELECT COUNT(*) FROM roles
      WHERE code NOT IN ('admin','developer','viewer'))
  + ABS((SELECT COUNT(*) FROM audit_logs)
      -(SELECT COUNT(*) FROM audit_log_export_sequences))
  + (SELECT COUNT(*) FROM audit_logs audit
      LEFT JOIN audit_log_export_sequences sequence
        ON sequence.tenant_id=audit.tenant_id
       AND sequence.audit_log_id=audit.audit_log_id
      WHERE sequence.audit_log_id IS NULL)
  + (SELECT COUNT(*) FROM audit_log_export_sequences sequence
      LEFT JOIN audit_logs audit
        ON audit.tenant_id=sequence.tenant_id
       AND audit.audit_log_id=sequence.audit_log_id
      WHERE audit.audit_log_id IS NULL)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='view'
        AND lower(COALESCE(object.sql,'')) LIKE '%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='trigger' AND object.tbl_name!='audit_logs'
        AND lower(COALESCE(object.sql,'')) LIKE '%audit_logs%'
        AND object.name NOT IN (
          'trg_audit_exports_owner_insert_prd058',
          'trg_audit_log_export_sequences_owner_insert_prd058'
        ))
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='table' AND object.name NOT IN (
          'audit_logs','audit_log_export_sequences','audit_exports'
        )
        AND lower(COALESCE(object.sql,'')) LIKE '%references%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='index' AND object.tbl_name='audit_logs'
        AND object.sql IS NOT NULL
        AND object.name NOT IN (
          'idx_audit_logs_tenant_time','idx_audit_logs_tenant_actor_time',
          'idx_audit_logs_tenant_object_time','idx_audit_logs_tenant_action_time',
          'idx_audit_logs_tenant_request','idx_audit_logs_dry_run_request_unique',
          'idx_audit_logs_model_quality_request_unique',
          'idx_audit_logs_finops_adjustment_unique'
        ))
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='trigger' AND object.tbl_name='audit_logs'
        AND object.name NOT IN (
          'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
          'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
          'trg_audit_logs_prd050_rowid_insert',
          'trg_audit_logs_export_sequence_insert_prd058'
        ))
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='view'
        AND lower(COALESCE(object.sql,'')) LIKE '% roles%')
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.type='table' AND object.name!='roles'
        AND lower(COALESCE(object.sql,'')) LIKE '%references%roles%')
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.tbl_name='roles' AND object.name NOT IN (
        'roles','idx_roles_tenant_id','sqlite_autoindex_roles_1',
        'sqlite_autoindex_roles_2'
      ));
DROP TRIGGER trg_prd059_up_guard;
DROP TABLE prd059_up_guard;

-- TEMP 快照只服务本事务的逐列保真守卫，不成为产品表或长期业务证据。
CREATE TEMP TABLE prd059_roles_before AS
SELECT rowid AS source_rowid,* FROM roles;
CREATE TEMP TABLE prd059_audit_before AS
SELECT rowid AS source_rowid,* FROM audit_logs;
CREATE TEMP TABLE prd059_sequences_before AS
SELECT rowid AS source_rowid,* FROM audit_log_export_sequences;
CREATE TEMP TABLE prd059_exports_before AS
SELECT rowid AS source_rowid,* FROM audit_exports;
CREATE TEMP TABLE prd059_export_events_before AS
SELECT rowid AS source_rowid,* FROM audit_export_events;
CREATE TEMP TABLE prd059_sqlite_sequence_before AS
SELECT name,seq FROM sqlite_sequence
WHERE name='audit_log_export_sequences';

-- 六角色默认值只在迁移事务内复用；持久 tenant trigger 使用同一组冻结字面量。
CREATE TEMP TABLE prd059_role_defaults (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  matrix_sha256 TEXT NOT NULL,
  immutable INTEGER NOT NULL
);
-- 固定六角色名称、职责、默认矩阵哈希和 admin 不可变标志，供存量与新租户使用同一事实。
INSERT INTO prd059_role_defaults(code,name,description,matrix_sha256,immutable) VALUES
  ('admin','Admin','租户最高管理员，拥有完整权限且矩阵不可修改。','0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2',1),
  ('developer','Developer','负责模型接入、Key、调试和请求排障，不读取正式账单。','9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e',0),
  ('viewer','Viewer','仅查看基础概览和公开控制台信息。','b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a',0),
  ('finance_admin','FinanceAdmin','负责用量、账本、预算、价格和成本异常。','181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be',0),
  ('security_admin','SecurityAdmin','负责审计、DLP、策略、错误归一和安全事件。','ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c',0),
  ('ops_admin','OpsAdmin','负责 Provider、环境、项目、Key、模型和路由运维。','b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9',0);

-- 产品权限目录不属于任一租户；严格枚举、长度和 catalog revision 防止模糊 code。
CREATE TABLE rbac_permission_templates (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=1)
);

-- 权限目录一次性写入 48 个规范 code；后续不可变 trigger 禁止运行期篡改发布元数据。
INSERT INTO rbac_permission_templates(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('console.basic.read','console','基础控制台','访问概览、API 文档、系统状态和迁移兼容页面。','read',1),
  ('dashboard.read','console','读取概览','读取当前租户的聚合运行概览，不包含正式账单明细。','read',1),
  ('tenant.settings.read','tenant','读取租户设置','读取当前租户的脱敏基础配置。','read',1),
  ('user.manage','tenant','管理用户','创建租户用户并分配固定角色。','critical',1),
  ('rbac.matrix.manage','tenant','管理权限矩阵','读取并调整固定角色的可授权权限集合。','critical',1),
  ('environment.read','environment','读取环境','读取当前租户的环境边界和状态。','read',1),
  ('environment.write','environment','管理环境','创建、启停和维护当前租户环境。','write',1),
  ('project.read','project','读取项目','读取项目、应用及其当前租户归属。','read',1),
  ('project.write','project','管理项目','创建和维护项目、应用及资源绑定。','write',1),
  ('virtual_key.read','key','读取 Virtual Key','读取脱敏 Virtual Key 元数据。','read',1),
  ('virtual_key.write','key','管理 Virtual Key','创建、更新和撤销当前租户 Virtual Key。','critical',1),
  ('provider.read','provider','读取 Provider','读取 Provider 与 KeyPool 的脱敏配置。','read',1),
  ('provider.write','provider','管理 Provider','创建、更新和测试 Provider 与 KeyPool。','critical',1),
  ('provider.health.read','provider','读取 Provider 健康','读取上游健康状态和归一化错误摘要。','read',1),
  ('provider.health.probe','provider','探测 Provider','主动触发当前租户 Provider 健康探测。','write',1),
  ('provider_error_map.read','provider','读取错误映射','读取 Provider 错误归一规则。','read',1),
  ('provider_error_map.write','provider','管理错误映射','创建和更新 Provider 错误归一规则。','critical',1),
  ('model.read','model','读取模型','读取当前租户模型目录。','read',1),
  ('model.write','model','管理模型','创建和更新当前租户模型目录。','write',1),
  ('model_price.read','model','读取价格','读取模型价格时间线和版本。','read',1),
  ('model_price.write','model','管理价格','创建不可变模型价格计划。','critical',1),
  ('routing.read','routing','读取路由','读取别名、回退、路由和缓存亲和配置。','read',1),
  ('routing.write','routing','管理路由','创建和更新别名、回退及路由策略。','critical',1),
  ('model_quality.read','routing','读取模型质量','读取模型质量、成功率和延迟聚合。','read',1),
  ('model_quality.write','routing','管理模型质量','维护模型人工质量评分。','write',1),
  ('policy.read','security','读取策略','读取治理策略和版本摘要。','read',1),
  ('policy.write','security','管理策略','创建和更新治理策略。','critical',1),
  ('policy.simulate','security','模拟策略','对脱敏输入执行策略 Dry-Run。','write',1),
  ('policy.publish','security','发布策略','发布或回滚策略快照。','critical',1),
  ('dlp.read','security','读取 DLP','读取 DLP 规则的脱敏元数据。','read',1),
  ('dlp.write','security','管理 DLP','创建和更新 DLP 规则。','critical',1),
  ('request_log.read','observability','读取调用日志','读取当前租户调用日志、Trace 和保存筛选。','read',1),
  ('request_explanation.read','observability','读取请求解释','读取鉴权、路由、成本和安全解释事实。','read',1),
  ('feedback.use','observability','使用反馈','提交请求反馈并读取租户反馈汇总。','write',1),
  ('billing.usage.read','billing','读取用量','读取全租户用量、Token 和成本明细。','read',1),
  ('billing.usage.export','billing','导出用量','导出当前租户用量和成本账单。','critical',1),
  ('billing.finops.read','billing','读取正式账本','读取 append-only 正式账本和调整记录。','read',1),
  ('billing.finops.write','billing','调整正式账本','创建财务调整和冲正。','critical',1),
  ('billing.budget.read','billing','读取预算','读取预算规则和执行状态。','read',1),
  ('billing.budget.write','billing','管理预算','创建和更新预算规则。','critical',1),
  ('billing.anomaly.read','billing','读取成本异常','读取成本异常和检测器状态。','read',1),
  ('billing.anomaly.write','billing','处置成本异常','确认、忽略异常并维护临时限流。','critical',1),
  ('audit.read','audit','读取审计','读取当前租户不可变管理审计事实。','read',1),
  ('audit.export','audit','导出审计','导出不可变审计快照并生成复核哈希。','critical',1),
  ('webhook.manage','integration','管理 Webhook','管理 Endpoint、死信、重放和归档。','critical',1),
  ('developer_portal.read','developer','读取开发者门户','读取快速开始、SDK 示例和迁移指南。','read',1),
  ('debug_console.use','developer','使用 API 调试台','使用内置 OpenAI-compatible API 调试台。','write',1),
  ('system.config.read','system','读取系统配置','读取脱敏运行配置和系统状态。','read',1);

-- 角色模板同时表示租户可授予上限和 v1 默认集合，default_granted 固定为 1。
CREATE TABLE rbac_role_permission_templates (
  role_code TEXT NOT NULL CHECK (
    role_code IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  permission_code TEXT NOT NULL,
  default_granted INTEGER NOT NULL CHECK (default_granted=1),
  PRIMARY KEY (role_code,permission_code),
  FOREIGN KEY (permission_code) REFERENCES rbac_permission_templates(permission_code)
);
-- admin 上限等于完整目录并保持不可变，确保租户始终有恢复权限矩阵的入口。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
SELECT 'admin',permission_code,1 FROM rbac_permission_templates;
-- Developer 只获得接入、调试、日志和开发门户权限，明确排除全部 billing 权限。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
SELECT 'developer',permission_code,1 FROM rbac_permission_templates
WHERE permission_code IN (
  'console.basic.read','dashboard.read','debug_console.use','developer_portal.read',
  'environment.read','feedback.use','model.read','model_price.read','model_quality.read',
  'project.read','provider.health.probe','provider.health.read','provider_error_map.read',
  'request_explanation.read','request_log.read','routing.read','virtual_key.read',
  'virtual_key.write','webhook.manage'
);
-- Viewer 仅保留基础控制台与聚合概览，不可读取任何治理明细或写接口。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
SELECT 'viewer',permission_code,1 FROM rbac_permission_templates
WHERE permission_code IN ('console.basic.read','dashboard.read');
-- FinanceAdmin 仅获得账单、预算、价格和成本异常权限，明确排除 Provider 管理。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
SELECT 'finance_admin',permission_code,1 FROM rbac_permission_templates
WHERE permission_code IN (
  'billing.anomaly.read','billing.anomaly.write','billing.budget.read','billing.budget.write',
  'billing.finops.read','billing.finops.write','billing.usage.export','billing.usage.read',
  'console.basic.read','dashboard.read','model.read','model_price.read','model_price.write'
);
-- SecurityAdmin 仅获得审计、DLP、策略和安全治理权限，明确排除价格写入。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
SELECT 'security_admin',permission_code,1 FROM rbac_permission_templates
WHERE permission_code IN (
  'audit.export','audit.read','billing.anomaly.read','billing.anomaly.write',
  'console.basic.read','dashboard.read','dlp.read','dlp.write','model.read','model_price.read',
  'policy.publish','policy.read','policy.simulate','policy.write','provider_error_map.read',
  'provider_error_map.write','request_explanation.read','request_log.read','webhook.manage'
);
-- OpsAdmin 获得接入与路由运维权限，但不读取 Usage、FinOps 或账单导出。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
SELECT 'ops_admin',permission_code,1 FROM rbac_permission_templates
WHERE permission_code IN (
  'console.basic.read','dashboard.read','environment.read','environment.write','model.read',
  'model.write','model_price.read','model_quality.read','model_quality.write','project.read',
  'project.write','provider.health.probe','provider.health.read','provider.read','provider.write',
  'provider_error_map.read','request_explanation.read','request_log.read','routing.read',
  'routing.write','system.config.read','virtual_key.read','virtual_key.write','webhook.manage'
);

-- 模板只能由后续迁移先显式移除 trigger 后演进，运行期任何 DML 都失败关闭。
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

-- SQLite 不能原地扩展 code CHECK；旧角色逐 rowid 复制，新目录只补缺失角色。
CREATE TABLE roles_prd059_new (
  role_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL CHECK (
    code IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('active','disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  permission_revision INTEGER NOT NULL DEFAULT 1 CHECK (
    typeof(permission_revision)='integer' AND permission_revision>=1
  ),
  matrix_sha256 TEXT NOT NULL CHECK (
    length(matrix_sha256)=64 AND matrix_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  is_permission_immutable INTEGER NOT NULL CHECK (
    is_permission_immutable IN (0,1)
    AND is_permission_immutable=CASE WHEN code='admin' THEN 1 ELSE 0 END
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,role_id)
);
-- 存量三角色显式保留 rowid 与全部旧列，并附加 revision/hash/immutable 新字段。
INSERT INTO roles_prd059_new(
  rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at,
  permission_revision,matrix_sha256,is_permission_immutable
)
SELECT
  old.source_rowid,old.role_id,old.tenant_id,old.code,old.name,old.description,
  old.status,old.created_at,old.updated_at,1,defaults.matrix_sha256,defaults.immutable
FROM prd059_roles_before old
JOIN prd059_role_defaults defaults ON defaults.code=old.code;
-- 每个存量 tenant 补齐缺失的三个新角色，NOT EXISTS 保证重复执行不会产生同租户同 code。
INSERT INTO roles_prd059_new(
  role_id,tenant_id,code,name,description,status,created_at,updated_at,
  permission_revision,matrix_sha256,is_permission_immutable
)
SELECT
  'role_'||lower(hex(randomblob(16))),tenant.tenant_id,defaults.code,
  defaults.name,defaults.description,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  1,defaults.matrix_sha256,defaults.immutable
FROM tenants tenant CROSS JOIN prd059_role_defaults defaults
WHERE NOT EXISTS (
  SELECT 1 FROM roles_prd059_new role
  WHERE role.tenant_id=tenant.tenant_id AND role.code=defaults.code
);

CREATE TEMP TABLE prd059_role_copy_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd059_role_copy_guard
BEFORE INSERT ON prd059_role_copy_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd059_role_copy_failed'); END;
-- 双向 EXCEPT 与每租户六角色计数共同证明换表既不丢历史，也不漏新目录。
INSERT INTO prd059_role_copy_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM (
    SELECT source_rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
    FROM prd059_roles_before
    EXCEPT
    SELECT rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
    FROM roles_prd059_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT role.rowid,role.role_id,role.tenant_id,role.code,role.name,role.description,
           role.status,role.created_at,role.updated_at
    FROM roles_prd059_new role
    JOIN prd059_roles_before old ON old.role_id=role.role_id
    EXCEPT
    SELECT source_rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
    FROM prd059_roles_before
  ))
  + (SELECT COUNT(*) FROM tenants tenant
      WHERE (SELECT COUNT(*) FROM roles_prd059_new role
             WHERE role.tenant_id=tenant.tenant_id)!=6);
DROP TRIGGER trg_prd059_role_copy_guard;
DROP TABLE prd059_role_copy_guard;
DROP TABLE roles;
-- copy guard 通过后才切换父表名称，避免入向 owner 关系看到半完成角色目录。
ALTER TABLE roles_prd059_new RENAME TO roles;
CREATE INDEX idx_roles_tenant_id ON roles(tenant_id);

-- 固定角色目录不开放 CRUD；非 admin 只允许 revision+hash 的单步 CAS 变更。
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2'
    WHEN 'developer' THEN '9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e'
    WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
    WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
    WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
    WHEN 'ops_admin' THEN 'b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9'
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
CREATE TRIGGER trg_roles_delete_prd059
BEFORE DELETE ON roles FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_rowid_insert_prd059
AFTER INSERT ON roles FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;

-- 租户 Permission 是模板的不可变投影，复合唯一键供 RolePermission tenant owner 使用。
CREATE TABLE permissions (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=1),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
CREATE INDEX idx_permissions_tenant_category_prd059
  ON permissions(tenant_id,category,code);
-- 为每个存量 tenant 投影完整权限目录；复合唯一键阻止跨租户 code 或 ID 混用。
INSERT INTO permissions(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT
  'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,
  template.catalog_revision,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant CROSS JOIN rbac_permission_templates template;

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

-- RolePermission 一行表示 granted；双复合 FK 使全局 ID 也不能绕过 tenant owner。
CREATE TABLE role_permissions (
  role_permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  role_id TEXT NOT NULL,
  permission_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id,role_id) REFERENCES roles(tenant_id,role_id),
  FOREIGN KEY (tenant_id,permission_id) REFERENCES permissions(tenant_id,permission_id),
  UNIQUE (tenant_id,role_id,permission_id)
);
CREATE INDEX idx_role_permissions_tenant_role_prd059
  ON role_permissions(tenant_id,role_id,permission_id);
CREATE INDEX idx_role_permissions_tenant_permission_prd059
  ON role_permissions(tenant_id,permission_id,role_id);
-- 根据角色产品上限生成 v1 默认 granted 关系，所有关联同时绑定 role/permission tenant。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT
  'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,permission.permission_id,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM roles role
JOIN rbac_role_permission_templates template ON template.role_code=role.code
JOIN permissions permission
  ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code;

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
CREATE TRIGGER trg_role_permissions_update_prd059
BEFORE UPDATE ON role_permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id AND role.code='admin'
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_role_permissions_rowid_insert_prd059
AFTER INSERT ON role_permissions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

-- users 仍复用历史 role 文本列；数据库 trigger 将其提升为 tenant-qualified active role 关系。
CREATE TRIGGER trg_users_role_owner_insert_prd059
BEFORE INSERT ON users FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.code=NEW.role AND role.status='active'
)
BEGIN SELECT RAISE(ABORT,'rbac_user_role_owner_mismatch'); END;
CREATE TRIGGER trg_users_role_owner_update_prd059
BEFORE UPDATE OF tenant_id,role,status ON users FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.code=NEW.role AND role.status='active'
)
BEGIN SELECT RAISE(ABORT,'rbac_user_role_owner_mismatch'); END;

-- AuditLog 必须扩展六角色及 role.permission_changed，同时保留 0049 全部 rowid、NULL 和子表事实。
CREATE TABLE audit_logs_prd059_new (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role IN ('admin', 'developer', 'viewer', 'finance_admin', 'security_admin', 'ops_admin')),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created', 'provider.updated',
      'provider_key.created', 'provider_key.updated',
      'virtual_key.created', 'virtual_key.updated',
      'budget_rule.created', 'budget_rule.updated',
      'dlp_rule.created', 'dlp_rule.updated',
      'webhook_delivery.replayed', 'webhook_endpoint.secret_rotated',
      'webhook_dead_letter.archived',
      'usage_export.requested', 'usage_export.downloaded',
      'policy.created', 'policy.updated',
      'policy_snapshot.published', 'policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created', 'routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created', 'cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created', 'error_map.updated',
      'audit.exported',
      'role.permission_changed'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'webhook_dead_letter',
      'usage_export_job', 'policy', 'policy_snapshot', 'policy_explanation',
      'routing_policy', 'model_quality_score', 'cache_affinity',
      'finops_adjustment', 'cost_anomaly', 'provider_error_map', 'audit_export', 'role_permission'
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
  UNIQUE (tenant_id, audit_log_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- 显式复制 rowid 和每一列；新枚举不能重排历史事实或改变 NULL 语义。
INSERT INTO audit_logs_prd059_new(
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

CREATE TEMP TABLE prd059_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd059_audit_copy_guard
BEFORE INSERT ON prd059_audit_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd059_audit_copy_failed'); END;
-- 删除旧表前核对行数、NULL 分布和含 rowid 双向差集，禁止静默损坏历史证据。
INSERT INTO prd059_audit_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd059_new),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd059_new WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd059_new WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd059_new WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd059_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd059_new
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd059_audit_copy_guard;
DROP TABLE prd059_audit_copy_guard;

-- PRD-058 的两个 owner 守卫挂在导出子表上，但 SQL 会读取 audit_logs。
-- SQLite 在 DROP 父表时会重新校验这些入向触发器，因此必须先临时移除；
-- 新 audit_logs 原子切换完成后会按 0049 的原定义完整恢复，避免缩小既有安全边界。
DROP TRIGGER trg_audit_log_export_sequences_owner_insert_prd058;
DROP TRIGGER trg_audit_exports_owner_insert_prd058;
DROP TABLE audit_logs;
-- 复制守卫通过后才切换正式表名，事务外读者不会看到半迁移结构。
ALTER TABLE audit_logs_prd059_new RENAME TO audit_logs;

-- 恢复全部既有 tenant-first 索引，并增加复合候选键供同租户 FK 使用。
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id, created_at DESC, audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id, actor_user_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id, object_type, object_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id, action, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id, request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id, object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';
-- 恢复 PRD-058 的序列 owner 守卫，确保每条稳定导出水位仍归属于同租户 AuditLog。
CREATE TRIGGER trg_audit_log_export_sequences_owner_insert_prd058
BEFORE INSERT ON audit_log_export_sequences FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
)
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_owner_mismatch'); END;

-- 恢复 PRD-058 的导出 owner 守卫；定义与 0049 保持一致，继续同时校验 actor、
-- 快照边界、过滤器白名单和安全事件摘要，禁止借迁移绕过租户及证据约束。
CREATE TRIGGER trg_audit_exports_owner_insert_prd058
BEFORE INSERT ON audit_exports FOR EACH ROW
WHEN NOT (
  NOT EXISTS (SELECT 1 FROM audit_logs audit WHERE audit.audit_log_id=NEW.audit_log_id)
  AND EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.requested_by
  )
  AND (
    (NEW.snapshot_sequence=0
      AND NEW.snapshot_audit_log_id IS NULL AND NEW.snapshot_created_at IS NULL)
    OR
    (NEW.snapshot_sequence>0
      AND NEW.snapshot_audit_log_id IS NOT NULL AND NEW.snapshot_created_at IS NOT NULL
      AND EXISTS (
        SELECT 1
        FROM audit_log_export_sequences sequence
        JOIN audit_logs audit
          ON audit.tenant_id=sequence.tenant_id
         AND audit.audit_log_id=sequence.audit_log_id
        WHERE sequence.tenant_id=NEW.tenant_id
          AND sequence.audit_log_sequence=NEW.snapshot_sequence
          AND sequence.audit_log_id=NEW.snapshot_audit_log_id
          AND audit.created_at=NEW.snapshot_created_at
      ))
  )
  AND json_valid(NEW.filters_json)
  AND CASE WHEN json_valid(NEW.filters_json) THEN json_type(NEW.filters_json) END='object'
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filters_json) THEN NEW.filters_json ELSE '{}' END
    ) filter
    WHERE filter.key NOT IN (
      'created_from','created_to','actor_user_id','action','object_type','object_id','request_id'
    ) OR filter.type NOT IN ('text','null')
  )
  AND json_valid(NEW.event_payload_json)
  AND CASE WHEN json_valid(NEW.event_payload_json)
    THEN json_type(NEW.event_payload_json) END='object'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
  ))=8
  AND (SELECT COUNT(DISTINCT key) FROM json_each(
    CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
  ))=8
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
    ) field
    WHERE field.key NOT IN (
      'audit_export_id','status','format','schema_version','row_count',
      'content_size_bytes','content_sha256','filter_fingerprint'
    )
  )
  AND json_extract(NEW.event_payload_json,'$.audit_export_id')=NEW.audit_export_id
  AND json_extract(NEW.event_payload_json,'$.status')=NEW.status
  AND json_extract(NEW.event_payload_json,'$.format')=NEW.format
  AND json_type(NEW.event_payload_json,'$.schema_version')='integer'
  AND json_extract(NEW.event_payload_json,'$.schema_version')=NEW.schema_version
  AND json_type(NEW.event_payload_json,'$.row_count')='integer'
  AND json_extract(NEW.event_payload_json,'$.row_count')=NEW.row_count
  AND json_type(NEW.event_payload_json,'$.content_size_bytes')='integer'
  AND json_extract(NEW.event_payload_json,'$.content_size_bytes')=NEW.content_size_bytes
  AND json_extract(NEW.event_payload_json,'$.content_sha256')=NEW.content_sha256
  AND json_extract(NEW.event_payload_json,'$.filter_fingerprint')=NEW.filter_fingerprint
)
BEGIN SELECT RAISE(ABORT,'audit_export_owner_mismatch'); END;

-- SQLite 对同一事件的 BEFORE INSERT 触发器按最近创建者优先执行。0050 重建 owner 后
-- 必须再按 0049 顺序重建冲突守卫，确保重复 ID 始终先收敛为 immutable，而不是误报 owner。
DROP TRIGGER trg_audit_exports_conflict_insert_prd058;
CREATE TRIGGER trg_audit_exports_conflict_insert_prd058
BEFORE INSERT ON audit_exports FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_exports current
  WHERE current.audit_export_id=NEW.audit_export_id
     OR current.audit_log_id=NEW.audit_log_id
     OR (current.tenant_id=NEW.tenant_id AND current.requested_by=NEW.requested_by
         AND current.request_id=NEW.request_id)
     OR (current.tenant_id=NEW.tenant_id AND current.event_id=NEW.event_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'audit_export_immutable'); END;

-- 角色权限 source event 与矩阵、AuditLog 同事务写入；这里只保存权限 code 和哈希，
-- 不保存 Token、Provider Key、Prompt、Response、Cookie 或 Webhook 签名。
CREATE UNIQUE INDEX idx_users_tenant_user_prd059 ON users(tenant_id,user_id);

CREATE TABLE role_permission_events (
  event_id TEXT PRIMARY KEY CHECK (
    length(event_id)=38 AND substr(event_id,1,6)='rpevt_'
    AND substr(event_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  role_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  before_revision INTEGER NOT NULL CHECK (
    typeof(before_revision)='integer' AND before_revision>=1
  ),
  after_revision INTEGER NOT NULL CHECK (
    typeof(after_revision)='integer' AND after_revision=before_revision+1
  ),
  before_sha256 TEXT NOT NULL CHECK (
    length(before_sha256)=64 AND before_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  after_sha256 TEXT NOT NULL CHECK (
    length(after_sha256)=64 AND after_sha256 NOT GLOB '*[^0-9a-f]*'
    AND after_sha256!=before_sha256
  ),
  added_permissions_json TEXT NOT NULL CHECK (
    json_valid(added_permissions_json)
    AND json_type(added_permissions_json)='array'
    AND length(CAST(added_permissions_json AS BLOB))<=4096
  ),
  removed_permissions_json TEXT NOT NULL CHECK (
    json_valid(removed_permissions_json)
    AND json_type(removed_permissions_json)='array'
    AND length(CAST(removed_permissions_json AS BLOB))<=4096
  ),
  audit_log_id TEXT NOT NULL UNIQUE,
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 160
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=8192
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
  UNIQUE (tenant_id,event_id),
  UNIQUE (tenant_id,actor_user_id,request_id),
  FOREIGN KEY (tenant_id,role_id) REFERENCES roles(tenant_id,role_id),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,audit_log_id) REFERENCES audit_logs(tenant_id,audit_log_id)
);
CREATE INDEX idx_role_permission_events_tenant_time_prd059
  ON role_permission_events(tenant_id,created_at DESC,event_id DESC);
CREATE INDEX idx_role_permission_events_tenant_status_prd059
  ON role_permission_events(tenant_id,delivery_status,updated_at,event_id);

-- 插入时逐项反向绑定当前 role、actor、审计摘要和严格九字段 payload。
CREATE TRIGGER trg_role_permission_events_owner_insert_prd059
BEFORE INSERT ON role_permission_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.code!='admin' AND role.status='active'
    AND role.permission_revision=NEW.after_revision
    AND role.matrix_sha256=NEW.after_sha256
)
OR NOT EXISTS (
  SELECT 1 FROM users actor
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
    AND actor.status='active' AND actor.role=NEW.actor_role
)
OR NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
    AND audit.actor_user_id=NEW.actor_user_id AND audit.actor_role=NEW.actor_role
    AND audit.action='role.permission_changed'
    AND audit.object_type='role_permission' AND audit.object_id=NEW.role_id
    AND audit.request_id=NEW.request_id AND audit.trace_id=NEW.trace_id
    AND audit.created_at=NEW.created_at
    AND json_extract(audit.before_summary_json,'$.permission_revision')=NEW.before_revision
    AND json_extract(audit.before_summary_json,'$.matrix_sha256')=NEW.before_sha256
    AND json_extract(audit.after_summary_json,'$.permission_revision')=NEW.after_revision
    AND json_extract(audit.after_summary_json,'$.matrix_sha256')=NEW.after_sha256
    AND json_extract(audit.after_summary_json,'$.permission_count')=(
      SELECT COUNT(*) FROM role_permissions relation
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
    )
    AND json_extract(audit.before_summary_json,'$.permission_count')=(
      SELECT COUNT(*) FROM role_permissions relation
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
    )-(SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
     +(SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))
)
OR NEW.delivery_status!='pending' OR NEW.last_error_code IS NOT NULL
OR NEW.dispatched_at IS NOT NULL OR NEW.updated_at!=NEW.created_at
OR (SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
  +(SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))=0
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) item
  WHERE item.type!='text'
    OR NOT EXISTS (
      SELECT 1
      FROM rbac_role_permission_templates role_template
      JOIN roles role ON role.code=role_template.role_code
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
        AND role_template.permission_code=item.value
    )
    OR NOT EXISTS (
      SELECT 1
      FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
        AND permission.code=item.value
    )
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.removed_permissions_json) item
  WHERE item.type!='text'
    OR NOT EXISTS (
      SELECT 1
      FROM rbac_role_permission_templates role_template
      JOIN roles role ON role.code=role_template.role_code
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
        AND role_template.permission_code=item.value
    )
    OR EXISTS (
      SELECT 1
      FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
        AND permission.code=item.value
    )
)
OR (SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.added_permissions_json))
OR (SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.removed_permissions_json))
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) added
  JOIN json_each(NEW.removed_permissions_json) removed ON removed.value=added.value
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) item
  JOIN json_each(NEW.added_permissions_json) next ON next.key=item.key+1
  WHERE CAST(item.value AS TEXT)>=CAST(next.value AS TEXT)
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.removed_permissions_json) item
  JOIN json_each(NEW.removed_permissions_json) next ON next.key=item.key+1
  WHERE CAST(item.value AS TEXT)>=CAST(next.value AS TEXT)
)
OR (SELECT COUNT(*) FROM json_each(NEW.payload_json))!=9
OR EXISTS (
  SELECT 1 FROM json_each(NEW.payload_json) field
  WHERE field.key NOT IN (
    'schema_version','role_id','role_code','before_revision','after_revision',
    'before_sha256','after_sha256','added_permissions','removed_permissions'
  )
)
OR json_extract(NEW.payload_json,'$.schema_version')!=1
OR json_extract(NEW.payload_json,'$.role_id')!=NEW.role_id
OR json_extract(NEW.payload_json,'$.before_revision')!=NEW.before_revision
OR json_extract(NEW.payload_json,'$.after_revision')!=NEW.after_revision
OR json_extract(NEW.payload_json,'$.before_sha256')!=NEW.before_sha256
OR json_extract(NEW.payload_json,'$.after_sha256')!=NEW.after_sha256
OR json(json_extract(NEW.payload_json,'$.added_permissions'))
  !=json(NEW.added_permissions_json)
OR json(json_extract(NEW.payload_json,'$.removed_permissions'))
  !=json(NEW.removed_permissions_json)
OR NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.code=json_extract(NEW.payload_json,'$.role_code')
)
OR EXISTS (
  SELECT 1 FROM role_permission_events current
  WHERE current.event_id=NEW.event_id
     OR (current.tenant_id=NEW.tenant_id AND current.actor_user_id=NEW.actor_user_id
         AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_event_invalid'); END;

-- source event 只允许一次 pending -> dispatched/failed 终态回写，其余字段不可修改。
CREATE TRIGGER trg_role_permission_events_state_update_prd059
BEFORE UPDATE ON role_permission_events FOR EACH ROW
WHEN NOT (
  OLD.delivery_status='pending'
  AND NEW.delivery_status IN ('dispatched','failed')
  AND NEW.event_id=OLD.event_id AND NEW.tenant_id=OLD.tenant_id
  AND NEW.role_id=OLD.role_id AND NEW.actor_user_id=OLD.actor_user_id
  AND NEW.actor_role=OLD.actor_role AND NEW.request_id=OLD.request_id
  AND NEW.before_revision=OLD.before_revision AND NEW.after_revision=OLD.after_revision
  AND NEW.before_sha256=OLD.before_sha256 AND NEW.after_sha256=OLD.after_sha256
  AND NEW.added_permissions_json=OLD.added_permissions_json
  AND NEW.removed_permissions_json=OLD.removed_permissions_json
  AND NEW.audit_log_id=OLD.audit_log_id AND NEW.trace_id=OLD.trace_id
  AND NEW.payload_json=OLD.payload_json AND NEW.created_at=OLD.created_at
  AND NEW.dispatched_at=NEW.updated_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    (NEW.delivery_status='dispatched' AND NEW.last_error_code IS NULL)
    OR
    (NEW.delivery_status='failed'
      AND NEW.last_error_code='webhook.delivery_unavailable')
  )
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_event_immutable'); END;
CREATE TRIGGER trg_role_permission_events_delete_prd059
BEFORE DELETE ON role_permission_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_event_immutable'); END;
CREATE TRIGGER trg_role_permission_events_rowid_insert_prd059
AFTER INSERT ON role_permission_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_event_immutable'); END;
-- Audit owner trigger 保留 0049 全部分支，并验证角色权限审计的同租户角色终态。
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
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

-- 每条新 AuditLog 在同一事务获得唯一稳定水位；映射失败必须回滚原审计写入。
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 自动映射与原 AuditLog 共用事务，序列写入失败会撤销审计事实。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;


-- 新租户必须在 tenant INSERT 同一事务内生成完整六角色、48 权限和 125 个默认关联；
-- 任一步约束失败都会回滚租户，避免出现能够登录但无法正确授权的半初始化状态。
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新 tenant 与六角色在同一事务生成，任一目录约束失败必须连同 tenant INSERT 回滚。
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
      WHEN 'admin' THEN '0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2'
      WHEN 'developer' THEN '9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e'
      WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
      WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
      WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
      WHEN 'ops_admin' THEN 'b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (
    SELECT DISTINCT role_code FROM rbac_role_permission_templates
  ) catalog;

  -- 新 tenant 权限目录逐字段复制不可变模板，禁止 seed 或客户端覆盖 catalog 元数据。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT
    'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 新 tenant 默认授权只来自产品上限模板，并通过双复合 FK 固定 owner。
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
-- 提交前复核目录、租户投影、默认关系、历史审计与 0049 子表逐列保真。
CREATE TEMP TABLE prd059_final_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd059_final_guard
BEFORE INSERT ON prd059_final_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd059_final_schema_invalid'); END;
-- 提交前一次性验证目录规模、tenant 投影、触发器和 PRD-058 历史保真，失败则回滚整个 0050。
INSERT INTO prd059_final_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=48 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=125 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'roles','permissions','role_permissions','role_permission_events',
        'rbac_permission_templates','rbac_role_permission_templates','audit_logs',
        'audit_log_export_sequences','audit_exports','audit_export_events'
      ))=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_tenants_rbac_initialize_prd059',
        'trg_users_role_owner_insert_prd059','trg_users_role_owner_update_prd059',
        'trg_role_permission_events_owner_insert_prd059',
        'trg_role_permission_events_state_update_prd059',
        'trg_role_permission_events_delete_prd059',
        'trg_role_permission_events_rowid_insert_prd059',
        'trg_audit_logs_owner_insert','trg_audit_logs_export_sequence_insert_prd058'
      ))=9 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM tenants tenant
      WHERE (SELECT COUNT(*) FROM roles role
             WHERE role.tenant_id=tenant.tenant_id)!=6)
  + (SELECT COUNT(*) FROM tenants tenant
      WHERE (SELECT COUNT(*) FROM permissions permission
             WHERE permission.tenant_id=tenant.tenant_id)!=48)
  + (SELECT COUNT(*) FROM tenants tenant
      WHERE (SELECT COUNT(*) FROM role_permissions relation
             WHERE relation.tenant_id=tenant.tenant_id)!=125)
  + (SELECT COUNT(*) FROM permissions permission
      LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
       AND template.category=permission.category
       AND template.name=permission.name
       AND template.description=permission.description
       AND template.risk_level=permission.risk_level
       AND template.catalog_revision=permission.catalog_revision
      WHERE template.permission_code IS NULL OR permission.status!='active')
  + (SELECT COUNT(*)
     FROM roles role
     LEFT JOIN rbac_role_permission_templates template ON template.role_code=role.code
     LEFT JOIN permissions permission
       ON permission.tenant_id=role.tenant_id
      AND permission.code=template.permission_code
     LEFT JOIN role_permissions relation
       ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND relation.permission_id=permission.permission_id
     WHERE relation.role_permission_id IS NULL)
  + (SELECT COUNT(*)
     FROM role_permissions relation
     JOIN roles role
       ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     LEFT JOIN rbac_role_permission_templates template
       ON template.role_code=role.code AND template.permission_code=permission.code
     WHERE template.permission_code IS NULL)
  + (SELECT COUNT(*) FROM roles role
      WHERE role.permission_revision!=1
         OR role.is_permission_immutable!=CASE WHEN role.code='admin' THEN 1 ELSE 0 END
         OR role.matrix_sha256!=CASE role.code
           WHEN 'admin' THEN '0a7305c09f739695c6255e3a21363c15bade0285074269ce26a98fccc4f44ee2'
           WHEN 'developer' THEN '9a8afa161074ae48a071fc33d828f243f51f5f48c14e3961c63e7af38b12778e'
           WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
           WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
           WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
           WHEN 'ops_admin' THEN 'b5004351a1be7316b065e017b483c528af97653b12349ecdfc23ec8761bdf3b9'
         END)
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_audit_before
      EXCEPT SELECT rowid,* FROM audit_logs
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT * FROM prd059_audit_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_sequences_before
      EXCEPT SELECT rowid,* FROM audit_log_export_sequences
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_log_export_sequences
      EXCEPT SELECT * FROM prd059_sequences_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_exports_before
      EXCEPT SELECT rowid,* FROM audit_exports
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_exports
      EXCEPT SELECT * FROM prd059_exports_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_export_events_before
      EXCEPT SELECT rowid,* FROM audit_export_events
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_export_events
      EXCEPT SELECT * FROM prd059_export_events_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_sqlite_sequence_before
      EXCEPT SELECT name,seq FROM sqlite_sequence
      WHERE name='audit_log_export_sequences'
    ))
  + (SELECT COUNT(*) FROM (
      SELECT name,seq FROM sqlite_sequence
      WHERE name='audit_log_export_sequences'
      EXCEPT SELECT * FROM prd059_sqlite_sequence_before
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check);
DROP TRIGGER trg_prd059_final_guard;
DROP TABLE prd059_final_guard;

DROP TABLE prd059_roles_before;
DROP TABLE prd059_audit_before;
DROP TABLE prd059_sequences_before;
DROP TABLE prd059_exports_before;
DROP TABLE prd059_export_events_before;
DROP TABLE prd059_sqlite_sequence_before;
DROP TABLE prd059_role_defaults;

COMMIT;
PRAGMA foreign_keys = ON;
