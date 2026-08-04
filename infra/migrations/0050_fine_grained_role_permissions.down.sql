-- 回滚职责：仅在没有 PRD-059 新角色使用、矩阵变更、权限审计、订阅或投递事实时，
-- 把 0050 恢复为完整 0049 合同。默认矩阵投影可以删除，任何真实业务事实都必须先保留并升级处理。
-- 安全边界：回滚不得静默删除用户授权、AuditLog、Webhook 或 PRD-058 稳定导出证据。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd059_down_guard (
  business_fact_count INTEGER NOT NULL,
  unknown_dependency_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd059_down_guard
BEFORE INSERT ON prd059_down_guard FOR EACH ROW
WHEN NEW.business_fact_count!=0 OR NEW.unknown_dependency_count!=0
BEGIN SELECT RAISE(ABORT,'prd059_down_blocked'); END;

-- 所有业务事实和非默认矩阵都在首项持久 DDL 前统计；失败会保持 0050 schema 与数据原样不动。
INSERT INTO prd059_down_guard(business_fact_count,unknown_dependency_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0050' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM users
    WHERE role IN ('finance_admin','security_admin','ops_admin'))
  + (SELECT COUNT(*) FROM role_permission_events)
  + (SELECT COUNT(*) FROM audit_logs
      WHERE actor_role IN ('finance_admin','security_admin','ops_admin')
         OR action='role.permission_changed' OR object_type='role_permission')
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE (json_valid(endpoint.event_types_json)
          AND EXISTS (
            SELECT 1 FROM json_each(endpoint.event_types_json) event_type
            WHERE event_type.value='role.permission_changed'
          ))
        OR lower(endpoint.event_types_json) LIKE '%role.permission_changed%')
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE event_type='role.permission_changed')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      JOIN webhook_deliveries delivery
        ON delivery.tenant_id=attempt.tenant_id
       AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
      WHERE delivery.event_type='role.permission_changed')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
      WHERE event_type='role.permission_changed')
  + (SELECT COUNT(*) FROM tenants tenant
      WHERE (SELECT COUNT(*) FROM roles role
             WHERE role.tenant_id=tenant.tenant_id)!=6
         OR (SELECT COUNT(*) FROM permissions permission
             WHERE permission.tenant_id=tenant.tenant_id)!=48
         OR (SELECT COUNT(*) FROM role_permissions relation
             WHERE relation.tenant_id=tenant.tenant_id)!=125)
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
     JOIN rbac_role_permission_templates template ON template.role_code=role.code
     JOIN permissions permission
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
     WHERE template.permission_code IS NULL),
  -- 后续迁移或手工对象若依赖即将移除/重建的合同，必须先由其所有者回滚，不能猜测重写。
  (SELECT COUNT(*) FROM schema_migrations
    WHERE status='applied' AND version>'0050')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.sql IS NOT NULL
        AND (
          lower(object.sql) LIKE '%role_permissions%'
          OR lower(object.sql) LIKE '%role_permission_events%'
          OR lower(object.sql) LIKE '%rbac_permission_templates%'
          OR lower(object.sql) LIKE '%rbac_role_permission_templates%'
          OR lower(object.sql) LIKE '%references permissions%'
          OR lower(object.sql) LIKE '%role.permission_changed%'
          OR lower(object.sql) LIKE '%finance_admin%'
          OR lower(object.sql) LIKE '%security_admin%'
          OR lower(object.sql) LIKE '%ops_admin%'
          OR lower(object.sql) LIKE '%audit_logs%'
          OR lower(object.sql) LIKE '% roles%'
          OR lower(object.sql) LIKE '%"roles"%'
          OR lower(object.sql) LIKE '%[roles]%'
          OR lower(object.sql) LIKE '%`roles`%'
          OR lower(object.sql) LIKE '%'||char(10)||'roles%'
        )
        AND object.name NOT IN (
          'roles','permissions','role_permissions','role_permission_events',
          'rbac_permission_templates','rbac_role_permission_templates','audit_logs',
          'audit_log_export_sequences','audit_exports',
          'idx_roles_tenant_id','idx_permissions_tenant_category_prd059',
          'idx_role_permissions_tenant_role_prd059',
          'idx_role_permissions_tenant_permission_prd059',
          'idx_role_permission_events_tenant_time_prd059',
          'idx_role_permission_events_tenant_status_prd059',
          'idx_audit_logs_tenant_time','idx_audit_logs_tenant_actor_time',
          'idx_audit_logs_tenant_object_time','idx_audit_logs_tenant_action_time',
          'idx_audit_logs_tenant_request','idx_audit_logs_dry_run_request_unique',
          'idx_audit_logs_model_quality_request_unique',
          'idx_audit_logs_finops_adjustment_unique',
          'idx_audit_logs_role_permission_request_unique',
          'trg_rbac_permission_templates_insert_prd059',
          'trg_rbac_permission_templates_update_prd059',
          'trg_rbac_permission_templates_delete_prd059',
          'trg_rbac_role_templates_insert_prd059',
          'trg_rbac_role_templates_update_prd059',
          'trg_rbac_role_templates_delete_prd059',
          'trg_roles_owner_insert_prd059','trg_roles_matrix_update_prd059',
          'trg_roles_delete_prd059','trg_roles_rowid_insert_prd059',
          'trg_permissions_owner_insert_prd059','trg_permissions_update_prd059',
          'trg_permissions_delete_prd059','trg_permissions_rowid_insert_prd059',
          'trg_role_permissions_owner_insert_prd059',
          'trg_role_permissions_update_prd059',
          'trg_role_permissions_delete_prd059',
          'trg_role_permissions_rowid_insert_prd059',
          'trg_users_role_owner_insert_prd059','trg_users_role_owner_update_prd059',
          'trg_role_permission_events_owner_insert_prd059',
          'trg_role_permission_events_state_update_prd059',
          'trg_role_permission_events_delete_prd059',
          'trg_role_permission_events_rowid_insert_prd059',
          'trg_tenants_rbac_initialize_prd059',
          'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
          'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
          'trg_audit_logs_prd050_rowid_insert',
          'trg_audit_logs_export_sequence_insert_prd058',
          'trg_audit_log_export_sequences_owner_insert_prd058',
          'trg_audit_exports_owner_insert_prd058'
        ));
DROP TRIGGER trg_prd059_down_guard;
DROP TABLE prd059_down_guard;

-- TEMP 快照只服务本事务保真复核；角色只快照 0049 可表达的三类，新增默认角色经 guard 后可安全移除。
CREATE TEMP TABLE prd059_down_roles_before AS
SELECT rowid AS source_rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
FROM roles WHERE code IN ('admin','developer','viewer');
CREATE TEMP TABLE prd059_down_audit_before AS
SELECT rowid AS source_rowid,* FROM audit_logs;
CREATE TEMP TABLE prd059_down_sequences_before AS
SELECT rowid AS source_rowid,* FROM audit_log_export_sequences;
CREATE TEMP TABLE prd059_down_exports_before AS
SELECT rowid AS source_rowid,* FROM audit_exports;
CREATE TEMP TABLE prd059_down_export_events_before AS
SELECT rowid AS source_rowid,* FROM audit_export_events;
CREATE TEMP TABLE prd059_down_sqlite_sequence_before AS
SELECT name,seq FROM sqlite_sequence WHERE name='audit_log_export_sequences';

-- 先关闭创建新租户与用户角色的 0050 入口，再按事件、关联、权限的子到父顺序删除纯默认投影。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_users_role_owner_insert_prd059;
DROP TRIGGER trg_users_role_owner_update_prd059;

DROP TRIGGER trg_role_permission_events_owner_insert_prd059;
DROP TRIGGER trg_role_permission_events_state_update_prd059;
DROP TRIGGER trg_role_permission_events_delete_prd059;
DROP TRIGGER trg_role_permission_events_rowid_insert_prd059;
DROP TABLE role_permission_events;

DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_update_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_role_permissions_rowid_insert_prd059;
DROP TABLE role_permissions;

DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
DROP INDEX idx_users_tenant_user_prd059;

-- roles 不能原地收窄 CHECK；显式 rowid/逐列复制只保留 0049 三角色合同。
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_roles_delete_prd059;
DROP TRIGGER trg_roles_rowid_insert_prd059;
-- AuditLog owner 守卫读取 roles；必须先移除，避免换表期间形成悬空 schema 依赖。
DROP TRIGGER trg_audit_logs_owner_insert;
CREATE TABLE roles_prd059_down (
  role_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL CHECK (code IN ('admin', 'developer', 'viewer')),
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id, code)
);
-- 仅复制 0049 可表达的三角色并保留 rowid；前置 guard 已拒绝任何新角色用户或事件事实。
INSERT INTO roles_prd059_down(
  rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
)
SELECT rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
FROM roles WHERE code IN ('admin','developer','viewer');

CREATE TEMP TABLE prd059_down_role_copy_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd059_down_role_copy_guard
BEFORE INSERT ON prd059_down_role_copy_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd059_down_role_copy_failed'); END;
-- 双向差集与每租户三角色计数证明降级角色表没有历史漂移或目录缺项。
INSERT INTO prd059_down_role_copy_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM (
    SELECT * FROM prd059_down_roles_before
    EXCEPT
    SELECT rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
    FROM roles_prd059_down
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
    FROM roles_prd059_down
    EXCEPT SELECT * FROM prd059_down_roles_before
  ))
  + (SELECT COUNT(*) FROM tenants tenant
      WHERE (SELECT COUNT(*) FROM roles_prd059_down role
             WHERE role.tenant_id=tenant.tenant_id)!=3);
DROP TRIGGER trg_prd059_down_role_copy_guard;
DROP TABLE prd059_down_role_copy_guard;
DROP TABLE roles;
-- 角色保真 guard 通过后才恢复 0049 表名，避免 AuditLog owner trigger 指向临时父表。
ALTER TABLE roles_prd059_down RENAME TO roles;
CREATE INDEX idx_roles_tenant_id ON roles(tenant_id);

-- AuditLog 同样通过换表收窄六角色、动作和对象枚举；guard 已证明没有 0050 独有事实。
CREATE TABLE audit_logs_prd059_down (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role IN ('admin', 'developer', 'viewer')),
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
      'audit.exported'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'webhook_dead_letter',
      'usage_export_job', 'policy', 'policy_snapshot', 'policy_explanation',
      'routing_policy', 'model_quality_score', 'cache_affinity',
      'finops_adjustment', 'cost_anomaly', 'provider_error_map', 'audit_export'
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
-- AuditLog 显式复制 rowid 与全部列，NULL、JSON 和稳定序列映射不得重新生成。
INSERT INTO audit_logs_prd059_down(
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

CREATE TEMP TABLE prd059_down_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd059_down_audit_copy_guard
BEFORE INSERT ON prd059_down_audit_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd059_down_audit_copy_failed'); END;
-- 行数、NULL 口径和双向 rowid 全列差集共同阻断任何 AuditLog 降级漂移。
INSERT INTO prd059_down_audit_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd059_down),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd059_down WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd059_down WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd059_down WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd059_down
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd059_down
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd059_down_audit_copy_guard;
DROP TABLE prd059_down_audit_copy_guard;

-- 再移除 audit_logs 其余自身触发器和两个 PRD-058 入向 owner 守卫；新父表就位后按 0049 原定义恢复。
DROP TRIGGER trg_audit_logs_immutable_update;
DROP TRIGGER trg_audit_logs_immutable_delete;
DROP TRIGGER trg_audit_logs_prd050_conflict_insert;
DROP TRIGGER trg_audit_logs_prd050_rowid_insert;
DROP TRIGGER trg_audit_logs_export_sequence_insert_prd058;
DROP TRIGGER trg_audit_log_export_sequences_owner_insert_prd058;
DROP TRIGGER trg_audit_exports_owner_insert_prd058;
DROP TABLE audit_logs;
-- AuditLog 保真 guard 通过后恢复 0049 父表名，再按原定义重建全部索引与 owner/immutable trigger。
ALTER TABLE audit_logs_prd059_down RENAME TO audit_logs;

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

-- 这两个触发器仍归 PRD-058 所有；原样恢复可继续校验导出水位和快照的同租户父事实。
CREATE TRIGGER trg_audit_log_export_sequences_owner_insert_prd058
BEFORE INSERT ON audit_log_export_sequences FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
)
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_owner_mismatch'); END;

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

-- 恢复 0049 的 AuditLog owner、不可变、冲突、rowid 与稳定序列触发器。
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

-- 产品模板只在全部租户投影删除后移除；运行期不可变触发器必须先显式解除。
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TABLE rbac_role_permission_templates;
DROP TABLE rbac_permission_templates;

-- 提交前同时验证 0049 schema、三角色目录、历史 AuditLog 及三个不可变导出对象逐列保真。
CREATE TEMP TABLE prd059_down_final_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd059_down_final_guard
BEFORE INSERT ON prd059_down_final_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd059_down_final_schema_invalid'); END;
-- 最终 guard 证明 0050 对象已全部移除且 0049 父子事实、索引和触发器完整恢复。
INSERT INTO prd059_down_final_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'roles','audit_logs','audit_log_export_sequences','audit_exports','audit_export_events'
      ))=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'permissions','role_permissions','role_permission_events',
        'rbac_permission_templates','rbac_role_permission_templates'
      ))=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('roles'))=8 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name='uq_users_tenant_user_prd044')=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name='idx_users_tenant_user_prd059')=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM users
      WHERE role NOT IN ('admin','developer','viewer'))
  + (SELECT COUNT(*) FROM roles
      WHERE code NOT IN ('admin','developer','viewer'))
  + (SELECT COUNT(*) FROM tenants tenant
      WHERE (SELECT COUNT(*) FROM roles role
             WHERE role.tenant_id=tenant.tenant_id)!=3)
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
        'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
        'trg_audit_logs_prd050_rowid_insert',
        'trg_audit_logs_export_sequence_insert_prd058',
        'trg_audit_log_export_sequences_owner_insert_prd058',
        'trg_audit_exports_owner_insert_prd058'
      ))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_down_roles_before
      EXCEPT
      SELECT rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
      FROM roles
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,role_id,tenant_id,code,name,description,status,created_at,updated_at
      FROM roles
      EXCEPT SELECT * FROM prd059_down_roles_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_down_audit_before
      EXCEPT SELECT rowid,* FROM audit_logs
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT * FROM prd059_down_audit_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_down_sequences_before
      EXCEPT SELECT rowid,* FROM audit_log_export_sequences
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_log_export_sequences
      EXCEPT SELECT * FROM prd059_down_sequences_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_down_exports_before
      EXCEPT SELECT rowid,* FROM audit_exports
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_exports
      EXCEPT SELECT * FROM prd059_down_exports_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_down_export_events_before
      EXCEPT SELECT rowid,* FROM audit_export_events
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_export_events
      EXCEPT SELECT * FROM prd059_down_export_events_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd059_down_sqlite_sequence_before
      EXCEPT SELECT name,seq FROM sqlite_sequence
      WHERE name='audit_log_export_sequences'
    ))
  + (SELECT COUNT(*) FROM (
      SELECT name,seq FROM sqlite_sequence
      WHERE name='audit_log_export_sequences'
      EXCEPT SELECT * FROM prd059_down_sqlite_sequence_before
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check);
DROP TRIGGER trg_prd059_down_final_guard;
DROP TABLE prd059_down_final_guard;

DROP TABLE prd059_down_roles_before;
DROP TABLE prd059_down_audit_before;
DROP TABLE prd059_down_sequences_before;
DROP TABLE prd059_down_exports_before;
DROP TABLE prd059_down_export_events_before;
DROP TABLE prd059_down_sqlite_sequence_before;

COMMIT;
PRAGMA foreign_keys = ON;
