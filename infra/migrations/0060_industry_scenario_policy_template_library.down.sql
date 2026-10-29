-- PRD-068 回滚：只允许从无 application、mask、Template Audit/Webhook/RBAC 治理事实的
-- canonical revision 5 状态恢复 0059。任何不可伪造业务证据都必须在首项持久 DDL 前失败关闭。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 先冻结可安全收缩的 canonical 角色。Admin 的 revision 1..5 来自产品迁移历史；
-- Finance/Security/Ops 只接受新 tenant 的 1 或本次升级后的 2，避免收缩自定义后回退矩阵。
DROP TABLE IF EXISTS temp.prd068_roles_to_contract;
CREATE TEMP TABLE prd068_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','finance_admin','security_admin','ops_admin')
  AND (
    (role.code='admin' AND role.permission_revision BETWEEN 1 AND 5)
    OR (role.code IN ('finance_admin','security_admin')
        AND role.permission_revision BETWEEN 1 AND 2)
    OR (role.code='ops_admin' AND role.permission_revision BETWEEN 1 AND 3)
  )
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
    WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
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

-- 业务事实门禁使用 TEMP trigger 提供稳定错误原因；任何计数非零都在持久对象变化前原子中止。
DROP TABLE IF EXISTS temp.prd068_down_business_guard;
DROP TRIGGER IF EXISTS temp.trg_prd068_down_business_guard;
CREATE TEMP TABLE prd068_down_business_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd068_down_business_guard
BEFORE INSERT ON prd068_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd068_down_business_facts_present'); END;
-- 将 application、Audit/Webhook、权限和 mask 证据折叠为单一稳定业务门禁计数。
INSERT INTO prd068_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM policy_template_applications)
  + (SELECT COUNT(*) FROM policy_template_application_items)
  + (SELECT COUNT(*) FROM policy_template_application_dependencies)
  + (SELECT COUNT(*) FROM policy_template_events)
  + (SELECT COUNT(*) FROM audit_logs
     WHERE action IN ('policy_template.applied','policy_template.rolled_back')
        OR object_type='policy_template_application')
  -- 用户主动订阅、投递、尝试或死信都是外部治理事实；up 本身不会改写 endpoint JSON。
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE (
       json_valid(endpoint.event_types_json)
       AND EXISTS (
         SELECT 1 FROM json_each(endpoint.event_types_json) item
         WHERE item.value='policy_template.applied'
       )
     ) OR lower(endpoint.event_types_json) LIKE '%policy_template.applied%')
  + (SELECT COUNT(*) FROM webhook_deliveries
     WHERE event_type='policy_template.applied')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='policy_template.applied')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
     WHERE event_type='policy_template.applied')
  -- 显式权限变更历史不能靠删除关系掩盖；JSON 损坏时保守文本命中仍失败关闭。
  + (SELECT COUNT(*) FROM role_permission_events event
     WHERE EXISTS (
       SELECT 1 FROM json_each(event.added_permissions_json) item
       WHERE item.value IN ('policy_template.read','policy_template.apply')
     ) OR EXISTS (
       SELECT 1 FROM json_each(event.removed_permissions_json) item
       WHERE item.value IN ('policy_template.read','policy_template.apply')
     ) OR lower(event.added_permissions_json) LIKE '%policy_template.%'
       OR lower(event.removed_permissions_json) LIKE '%policy_template.%')
  + (SELECT COUNT(*)
     FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code LIKE 'policy_template.%'
       AND NOT EXISTS (
         SELECT 1 FROM prd068_roles_to_contract source
         WHERE source.tenant_id=relation.tenant_id
           AND source.role_id=relation.role_id
       ))
  -- RequestLog 的命中 ID 即使 applied=0 也证明 mask 曾参与决策，不能在 down 中静默抹除。
  + (SELECT COUNT(*) FROM request_logs
     WHERE policy_mask_applied!=0 OR policy_mask_policy_id IS NOT NULL)
  + (SELECT COUNT(*)
     FROM policies policy
     JOIN policy_actions action
       ON action.tenant_id=policy.tenant_id AND action.policy_id=policy.policy_id
     WHERE policy.status='active' AND action.action_type='mask')
  + (SELECT COUNT(*)
     FROM policy_snapshot_items item
     JOIN policy_snapshots snapshot
       ON snapshot.tenant_id=item.tenant_id
      AND snapshot.policy_snapshot_id=item.policy_snapshot_id
     WHERE snapshot.state='published' AND item.action_type='mask');
DROP TRIGGER trg_prd068_down_business_guard;
DROP TABLE prd068_down_business_guard;

-- 结构门禁冻结 0060 栈顶、RBAC/Audit/RequestLog 终态以及所有未知后续引用。
DROP TABLE IF EXISTS temp.prd068_down_guard;
CREATE TEMP TABLE prd068_down_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd068_down_preflight_ok CHECK (issue_count=0)
);
-- 结构检查在首项持久 DDL 前执行，未知后续依赖和损坏 owner 图一律失败关闭。
INSERT INTO prd068_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0060' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
     WHERE status='applied' AND version>'0060')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=56
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=5)=56
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=149
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*56 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=5)
      =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM roles role
     WHERE role.code='admin' AND NOT EXISTS (
       SELECT 1 FROM prd068_roles_to_contract source
       WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
     ))
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=52
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='index' AND tbl_name='request_logs'
             AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='trigger' AND tbl_name='request_logs')=12
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('audit_logs'))=17
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='index' AND tbl_name='audit_logs'
             AND name NOT LIKE 'sqlite_autoindex%')=14
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='trigger' AND tbl_name='audit_logs')=7
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN (
      'policy_templates','policy_template_applications',
      'policy_template_application_items','policy_template_application_dependencies',
      'policy_template_events'
    ))=5 THEN 0 ELSE 1 END
  -- 自有对象统一使用 prd068 后缀；任何其他 view/trigger/index 对新表或新列的引用均视为后续依赖。
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (
         instr(lower(object.sql),'policy_templates')>0
         OR instr(lower(object.sql),'policy_template_applications')>0
         OR instr(lower(object.sql),'policy_template_application_items')>0
         OR instr(lower(object.sql),'policy_template_application_dependencies')>0
         OR instr(lower(object.sql),'policy_template_events')>0
         OR instr(lower(object.sql),'policy_mask_applied')>0
         OR instr(lower(object.sql),'policy_mask_policy_id')>0
       )
       AND object.name NOT IN (
         'policy_templates',
         'policy_template_applications','policy_template_application_items',
         'policy_template_application_dependencies','policy_template_events',
         'request_logs','audit_logs'
       )
       AND object.name NOT LIKE '%prd068')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd068_down_guard;

-- 先移除附着在既有外部表上的保护 trigger；域表自己的 trigger 会随 DROP TABLE 原子移除。
DROP TRIGGER trg_tenants_policy_templates_initialize_prd068;
DROP TRIGGER trg_policy_template_generated_policy_update_prd068;
DROP TRIGGER trg_policy_template_generated_policy_delete_prd068;
DROP TRIGGER trg_policy_template_generated_rule_insert_prd068;
DROP TRIGGER trg_policy_template_generated_rule_update_prd068;
DROP TRIGGER trg_policy_template_generated_rule_delete_prd068;
DROP TRIGGER trg_policy_template_generated_action_insert_prd068;
DROP TRIGGER trg_policy_template_generated_action_update_prd068;
DROP TRIGGER trg_policy_template_generated_action_delete_prd068;
DROP TRIGGER trg_policy_template_project_update_prd068;
DROP TRIGGER trg_policy_template_project_delete_prd068;
DROP TRIGGER trg_policy_template_environment_update_prd068;
DROP TRIGGER trg_policy_template_environment_delete_prd068;
DROP TRIGGER trg_policy_template_model_alias_insert_prd068;
DROP TRIGGER trg_policy_template_model_alias_update_prd068;
DROP TRIGGER trg_policy_template_model_alias_delete_prd068;
DROP TRIGGER trg_policy_template_model_update_prd068;
DROP TRIGGER trg_policy_template_model_delete_prd068;
DROP TRIGGER trg_policy_template_provider_update_prd068;
DROP TRIGGER trg_policy_template_provider_delete_prd068;

-- 业务门禁已证明四张事实表为空；按子到父顺序删除，最后移除只读 tenant seed。
DROP TABLE policy_template_events;
DROP TABLE policy_template_application_dependencies;
DROP TABLE policy_template_application_items;
DROP TABLE policy_template_applications;
DROP TABLE policy_templates;
DROP INDEX uq_policy_events_owner_prd068;
DROP INDEX uq_policy_snapshot_events_owner_prd068;

-- 0060 Audit action/object 收缩前已证明不存在 Template Audit；恢复 0059 的完整 17 列枚举，
-- 并以 rowid、NULL 分布和双向 EXCEPT 保留全部历史治理事实。
CREATE TABLE audit_logs_prd068_old (
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
-- Audit 逐列复制包含历史 rowid 与所有 nullable 字段，不重算任何治理摘要。
INSERT INTO audit_logs_prd068_old(rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at)
SELECT rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at FROM audit_logs;
CREATE TEMP TABLE prd068_down_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 删除新表前执行行数、NULL 分布与双向差集校验，避免同主键不同内容被掩盖。
INSERT INTO prd068_down_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd068_old))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd068_old WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd068_old WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd068_old WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd068_old
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd068_old
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd068_down_audit_copy_guard;
DROP TABLE audit_logs;
-- 只有保真门禁通过后才恢复正式 Audit 表名及 0059 的完整索引/触发器。
ALTER TABLE audit_logs_prd068_old RENAME TO audit_logs;
CREATE UNIQUE INDEX idx_audit_logs_config_transfer_request_unique
  ON audit_logs(tenant_id,actor_user_id,action,request_id)
  WHERE action IN ('config.imported','config.exported');
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id,object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_invitation_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action IN ('user.invited','invitation.accepted');
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_prompt_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN (
    'prompt_template.created','prompt_template.updated',
    'prompt_template.version_published'
  );
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';
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
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

-- mask 证据门禁已证明新增两列全为默认空值；按 0059 的 50 列、rowid、24 索引和
-- 12 个本地 trigger 换表恢复，避免 SQLite DROP COLUMN 隐式丢失前序约束。
CREATE TABLE request_logs_prd068_old (
  request_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  provider_id TEXT,
  model_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
  error_code TEXT,
  latency_ms INTEGER,
  created_at TEXT NOT NULL,
  endpoint TEXT,
  request_model TEXT,
  resolved_model TEXT,
  provider_code TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
  estimated_cost_amount NUMERIC NOT NULL DEFAULT 0 CHECK (estimated_cost_amount >= 0),
  currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency IN ('CNY', 'USD')),
  price_schema_version TEXT,
  updated_at TEXT,
  budget_status TEXT,
  budget_rule_id TEXT,
  budget_scope_type TEXT,
  budget_scope_id TEXT,
  budget_action TEXT,
  budget_reason_code TEXT,
  budget_current_cost_cny NUMERIC,
  budget_limit_cny NUMERIC,
  dlp_status TEXT,
  dlp_rule_id TEXT,
  dlp_action TEXT,
  dlp_match_count INTEGER NOT NULL DEFAULT 0,
  dlp_reason_code TEXT,
  project_id TEXT,
  application_id TEXT,
  environment_id TEXT REFERENCES environments(environment_id),
  environment_code TEXT,
  fallback_triggered INTEGER NOT NULL DEFAULT 0 CHECK (fallback_triggered IN (0, 1)),
  routing_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (routing_attempt_count BETWEEN 0 AND 2),
  final_routing_attempt_id TEXT,
  stream_session_id TEXT REFERENCES stream_sessions(stream_session_id),
  stream_status TEXT CHECK (
    stream_status IS NULL
    OR stream_status IN ('completed', 'timed_out', 'client_disconnected', 'upstream_failed')
  ),
  snapshot_version INTEGER CHECK (
    snapshot_version IS NULL
    OR (typeof(snapshot_version) = 'integer' AND snapshot_version >= 1)
  ),
  policy_snapshot_status TEXT NOT NULL DEFAULT 'disabled' CHECK (
    policy_snapshot_status IN (
      'disabled', 'not_published', 'current', 'lkg',
      'emergency_lkg', 'expired', 'unavailable'
    )
  ),
  policy_decision TEXT CHECK (
    policy_decision IS NULL OR policy_decision IN ('allow', 'deny', 'route_to')
  ),
  policy_reason_code TEXT CHECK (
    policy_reason_code IS NULL OR length(policy_reason_code) BETWEEN 1 AND 128
  ),
  policy_id TEXT CHECK (policy_id IS NULL OR length(policy_id) BETWEEN 5 AND 128),
  policy_action_type TEXT CHECK (
    policy_action_type IS NULL
    OR policy_action_type IN ('allow', 'deny', 'mask', 'log_only', 'route_to')
  ),
  policy_target_model_id TEXT CHECK (
    policy_target_model_id IS NULL OR length(policy_target_model_id) BETWEEN 1 AND 128
  ),
  PRIMARY KEY (tenant_id, request_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  CHECK (
    (policy_action_type = 'route_to' AND policy_target_model_id IS NOT NULL)
    OR (policy_action_type IS NULL)
    OR (policy_action_type != 'route_to' AND policy_target_model_id IS NULL)
  )
);
-- RequestLog 只投影 0059 的 50 列，新增 mask 字段已由业务门禁证明为空。
INSERT INTO request_logs_prd068_old(rowid,request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,error_code,latency_ms,created_at,endpoint,request_model,resolved_model,provider_code,input_tokens,output_tokens,total_tokens,estimated_cost_amount,currency,price_schema_version,updated_at,budget_status,budget_rule_id,budget_scope_type,budget_scope_id,budget_action,budget_reason_code,budget_current_cost_cny,budget_limit_cny,dlp_status,dlp_rule_id,dlp_action,dlp_match_count,dlp_reason_code,project_id,application_id,environment_id,environment_code,fallback_triggered,routing_attempt_count,final_routing_attempt_id,stream_session_id,stream_status,snapshot_version,policy_snapshot_status,policy_decision,policy_reason_code,policy_id,policy_action_type,policy_target_model_id)
SELECT rowid,request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,error_code,latency_ms,created_at,endpoint,request_model,resolved_model,provider_code,input_tokens,output_tokens,total_tokens,estimated_cost_amount,currency,price_schema_version,updated_at,budget_status,budget_rule_id,budget_scope_type,budget_scope_id,budget_action,budget_reason_code,budget_current_cost_cny,budget_limit_cny,dlp_status,dlp_rule_id,dlp_action,dlp_match_count,dlp_reason_code,project_id,application_id,environment_id,environment_code,fallback_triggered,routing_attempt_count,final_routing_attempt_id,stream_session_id,stream_status,snapshot_version,policy_snapshot_status,policy_decision,policy_reason_code,policy_id,policy_action_type,policy_target_model_id FROM request_logs;
CREATE TEMP TABLE prd068_down_request_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 显式比较 rowid 与全部 50 列的双向差集，不能只依赖行数或 request_id。
INSERT INTO prd068_down_request_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM request_logs)-(SELECT COUNT(*) FROM request_logs_prd068_old))
  + CASE WHEN (SELECT COUNT(*) FROM request_logs WHERE error_code IS NULL)
      =(SELECT COUNT(*) FROM request_logs_prd068_old WHERE error_code IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM request_logs WHERE policy_id IS NULL)
      =(SELECT COUNT(*) FROM request_logs_prd068_old WHERE policy_id IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,error_code,latency_ms,created_at,endpoint,request_model,resolved_model,provider_code,input_tokens,output_tokens,total_tokens,estimated_cost_amount,currency,price_schema_version,updated_at,budget_status,budget_rule_id,budget_scope_type,budget_scope_id,budget_action,budget_reason_code,budget_current_cost_cny,budget_limit_cny,dlp_status,dlp_rule_id,dlp_action,dlp_match_count,dlp_reason_code,project_id,application_id,environment_id,environment_code,fallback_triggered,routing_attempt_count,final_routing_attempt_id,stream_session_id,stream_status,snapshot_version,policy_snapshot_status,policy_decision,policy_reason_code,policy_id,policy_action_type,policy_target_model_id FROM request_logs
      EXCEPT SELECT rowid,request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,error_code,latency_ms,created_at,endpoint,request_model,resolved_model,provider_code,input_tokens,output_tokens,total_tokens,estimated_cost_amount,currency,price_schema_version,updated_at,budget_status,budget_rule_id,budget_scope_type,budget_scope_id,budget_action,budget_reason_code,budget_current_cost_cny,budget_limit_cny,dlp_status,dlp_rule_id,dlp_action,dlp_match_count,dlp_reason_code,project_id,application_id,environment_id,environment_code,fallback_triggered,routing_attempt_count,final_routing_attempt_id,stream_session_id,stream_status,snapshot_version,policy_snapshot_status,policy_decision,policy_reason_code,policy_id,policy_action_type,policy_target_model_id FROM request_logs_prd068_old
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,error_code,latency_ms,created_at,endpoint,request_model,resolved_model,provider_code,input_tokens,output_tokens,total_tokens,estimated_cost_amount,currency,price_schema_version,updated_at,budget_status,budget_rule_id,budget_scope_type,budget_scope_id,budget_action,budget_reason_code,budget_current_cost_cny,budget_limit_cny,dlp_status,dlp_rule_id,dlp_action,dlp_match_count,dlp_reason_code,project_id,application_id,environment_id,environment_code,fallback_triggered,routing_attempt_count,final_routing_attempt_id,stream_session_id,stream_status,snapshot_version,policy_snapshot_status,policy_decision,policy_reason_code,policy_id,policy_action_type,policy_target_model_id FROM request_logs_prd068_old
      EXCEPT SELECT rowid,request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,status,error_code,latency_ms,created_at,endpoint,request_model,resolved_model,provider_code,input_tokens,output_tokens,total_tokens,estimated_cost_amount,currency,price_schema_version,updated_at,budget_status,budget_rule_id,budget_scope_type,budget_scope_id,budget_action,budget_reason_code,budget_current_cost_cny,budget_limit_cny,dlp_status,dlp_rule_id,dlp_action,dlp_match_count,dlp_reason_code,project_id,application_id,environment_id,environment_code,fallback_triggered,routing_attempt_count,final_routing_attempt_id,stream_session_id,stream_status,snapshot_version,policy_snapshot_status,policy_decision,policy_reason_code,policy_id,policy_action_type,policy_target_model_id FROM request_logs
    ));
DROP TABLE prd068_down_request_copy_guard;
DROP TABLE request_logs;
-- 保真校验成功后恢复正式表名，再原样重建 24 个索引和 12 个本地 trigger。
ALTER TABLE request_logs_prd068_old RENAME TO request_logs;
CREATE INDEX idx_request_logs_application_time
  ON request_logs(tenant_id, application_id, created_at DESC);
CREATE INDEX idx_request_logs_cost_time_prd055
  ON request_logs(tenant_id, estimated_cost_amount, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_created_at ON request_logs(tenant_id, created_at DESC);
CREATE INDEX idx_request_logs_dashboard_key_time
  ON request_logs(tenant_id, virtual_key_id, created_at DESC);
CREATE INDEX idx_request_logs_dashboard_latency
  ON request_logs(tenant_id, created_at, latency_ms);
CREATE INDEX idx_request_logs_dashboard_model_time
  ON request_logs(tenant_id, resolved_model, created_at DESC);
CREATE INDEX idx_request_logs_environment_time
  ON request_logs(tenant_id, environment_id, created_at DESC);
CREATE INDEX idx_request_logs_error_code ON request_logs(error_code);
CREATE INDEX idx_request_logs_latency_time_prd055
  ON request_logs(tenant_id, latency_ms, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_project_time
  ON request_logs(tenant_id, project_id, created_at DESC);
CREATE INDEX idx_request_logs_provider_code ON request_logs(tenant_id, provider_code);
CREATE INDEX idx_request_logs_provider_time_prd055
  ON request_logs(tenant_id, provider_code, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_request_model ON request_logs(tenant_id, request_model);
CREATE INDEX idx_request_logs_resolved_model ON request_logs(tenant_id, resolved_model);
CREATE INDEX idx_request_logs_snapshot_version
  ON request_logs(tenant_id, snapshot_version, created_at DESC, request_id DESC)
  WHERE snapshot_version IS NOT NULL;
CREATE INDEX idx_request_logs_status_time
  ON request_logs(tenant_id, status, created_at DESC);
CREATE INDEX idx_request_logs_status_time_prd055
  ON request_logs(tenant_id, status, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_stream_session
  ON request_logs(tenant_id, stream_session_id)
  WHERE stream_session_id IS NOT NULL;
CREATE INDEX idx_request_logs_tenant_id ON request_logs(tenant_id);
CREATE INDEX idx_request_logs_terminal_stage_time_prd055
  ON request_logs(
    tenant_id,
    CASE
      WHEN status='success' THEN 'completed'
      WHEN error_code IN ('gateway.auth_missing','gateway.auth_invalid') THEN 'authentication'
      WHEN error_code IN ('gateway.rate_limit_exceeded','gateway.rate_limit_unavailable') THEN 'rate_limit'
      WHEN error_code IN ('gateway.budget_exhausted','gateway.budget_unavailable') THEN 'budget'
      WHEN error_code IN ('gateway.dlp_blocked','gateway.dlp_unavailable') THEN 'dlp'
      WHEN error_code LIKE 'gateway.policy_%' THEN 'policy'
      WHEN error_code IN ('gateway.cache_affinity_unavailable') THEN 'keypool'
      WHEN error_code LIKE 'gateway.upstream_%'
        OR error_code IN (
          'gateway.execution_interrupted','gateway.stream_not_supported',
          'gateway.stream_session_unavailable','gateway.client_disconnected',
          'gateway.stream_processing_unavailable'
        ) THEN 'provider'
      WHEN error_code IN (
        'gateway.model_missing','gateway.messages_missing','gateway.input_missing',
        'gateway.model_not_found','gateway.model_alias_disabled','gateway.model_disabled',
        'gateway.provider_not_found','gateway.provider_unhealthy','gateway.no_route_available',
        'gateway.routing_policy_invalid','gateway.routing_snapshot_unavailable',
        'gateway.routing_snapshot_invalid','gateway.routing_decision_unavailable',
        'gateway.routing_attempt_unavailable','gateway.fallback_unavailable'
      ) THEN 'routing'
      WHEN error_code='gateway.request_log_unavailable' THEN 'logs_and_webhooks'
      ELSE 'unknown'
    END,
    created_at DESC,
    request_id DESC
  );
CREATE INDEX idx_request_logs_tokens_time_prd055
  ON request_logs(tenant_id, total_tokens, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_trace_id ON request_logs(trace_id);
CREATE INDEX idx_request_logs_trace_time_prd055
  ON request_logs(tenant_id, trace_id, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_virtual_key_time_prd055
  ON request_logs(tenant_id, virtual_key_id, created_at DESC, request_id DESC);
CREATE TRIGGER trg_request_log_feedback_parent_conflict_insert_prd049
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM request_logs current
  JOIN request_feedbacks feedback
    ON feedback.tenant_id=current.tenant_id
   AND feedback.source_request_id=current.request_id
  WHERE current.tenant_id=NEW.tenant_id
    AND current.request_id=NEW.request_id
    AND (
      current.virtual_key_id IS NOT NEW.virtual_key_id
      OR current.status IS NOT NEW.status
      OR current.model_id IS NOT NEW.model_id
      OR current.provider_id IS NOT NEW.provider_id
      OR current.project_id IS NOT NEW.project_id
      OR current.application_id IS NOT NEW.application_id
      OR current.environment_id IS NOT NEW.environment_id
      OR current.stream_session_id IS NOT NEW.stream_session_id
      OR current.stream_status IS NOT NEW.stream_status
      OR current.final_routing_attempt_id IS NOT NEW.final_routing_attempt_id
      OR current.routing_attempt_count IS NOT NEW.routing_attempt_count
    )
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;
CREATE TRIGGER trg_request_log_feedback_parent_conflict_update_prd049
BEFORE UPDATE OF tenant_id, request_id ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM request_logs target
  JOIN request_feedbacks feedback
    ON feedback.tenant_id=target.tenant_id
   AND feedback.source_request_id=target.request_id
  WHERE target.tenant_id=NEW.tenant_id
    AND target.request_id=NEW.request_id
    AND target.rowid != OLD.rowid
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;
CREATE TRIGGER trg_request_log_feedback_parent_delete_prd049
BEFORE DELETE ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_feedbacks feedback
  WHERE feedback.tenant_id=OLD.tenant_id AND feedback.source_request_id=OLD.request_id
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;
CREATE TRIGGER trg_request_log_feedback_parent_update_prd049
BEFORE UPDATE OF
  tenant_id, request_id, virtual_key_id, status, model_id, provider_id,
  project_id, application_id, environment_id, stream_session_id, stream_status,
  final_routing_attempt_id, routing_attempt_count
ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_feedbacks feedback
  WHERE feedback.tenant_id=OLD.tenant_id AND feedback.source_request_id=OLD.request_id
)
AND (
  NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.virtual_key_id IS NOT OLD.virtual_key_id
  OR NEW.status IS NOT OLD.status
  OR NEW.model_id IS NOT OLD.model_id
  OR NEW.provider_id IS NOT OLD.provider_id
  OR NEW.project_id IS NOT OLD.project_id
  OR NEW.application_id IS NOT OLD.application_id
  OR NEW.environment_id IS NOT OLD.environment_id
  OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.stream_status IS NOT OLD.stream_status
  OR NEW.final_routing_attempt_id IS NOT OLD.final_routing_attempt_id
  OR NEW.routing_attempt_count IS NOT OLD.routing_attempt_count
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;
CREATE TRIGGER trg_request_log_final_attempt_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN NEW.final_routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.final_routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
  ) THEN RAISE(ABORT, 'request_log_final_attempt_mismatch') END;
END;
CREATE TRIGGER trg_request_log_final_attempt_update
BEFORE UPDATE OF final_routing_attempt_id, tenant_id, request_id ON request_logs
FOR EACH ROW
WHEN NEW.final_routing_attempt_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM routing_attempts ra
    WHERE ra.routing_attempt_id=NEW.final_routing_attempt_id
      AND ra.tenant_id=NEW.tenant_id
      AND ra.request_id=NEW.request_id
  ) THEN RAISE(ABORT, 'request_log_final_attempt_mismatch') END;
END;
CREATE TRIGGER trg_request_log_stream_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.stream_session_id IS NULL)<>(NEW.stream_status IS NULL)
    THEN RAISE(ABORT, 'request_log_stream_fields_incomplete') END;
  SELECT CASE WHEN NEW.stream_session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM stream_sessions ss
    WHERE ss.stream_session_id=NEW.stream_session_id
      AND ss.tenant_id=NEW.tenant_id
      AND ss.request_id=NEW.request_id
      AND ss.status=NEW.stream_status
  ) THEN RAISE(ABORT, 'request_log_stream_session_mismatch') END;
  SELECT CASE WHEN NEW.stream_status IS NOT NULL AND NOT (
    (NEW.stream_status='completed' AND NEW.status='success')
    OR (NEW.stream_status<>'completed' AND NEW.status='failed')
  ) THEN RAISE(ABORT, 'request_log_stream_status_mismatch') END;
END;
CREATE TRIGGER trg_request_log_stream_update
BEFORE UPDATE OF tenant_id, request_id, status, stream_session_id, stream_status ON request_logs
FOR EACH ROW
BEGIN
  SELECT CASE WHEN (NEW.stream_session_id IS NULL)<>(NEW.stream_status IS NULL)
    THEN RAISE(ABORT, 'request_log_stream_fields_incomplete') END;
  SELECT CASE WHEN NEW.stream_session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM stream_sessions ss
    WHERE ss.stream_session_id=NEW.stream_session_id
      AND ss.tenant_id=NEW.tenant_id
      AND ss.request_id=NEW.request_id
      AND ss.status=NEW.stream_status
  ) THEN RAISE(ABORT, 'request_log_stream_session_mismatch') END;
  SELECT CASE WHEN NEW.stream_status IS NOT NULL AND NOT (
    (NEW.stream_status='completed' AND NEW.status='success')
    OR (NEW.stream_status<>'completed' AND NEW.status='failed')
  ) THEN RAISE(ABORT, 'request_log_stream_status_mismatch') END;
END;
CREATE TRIGGER trg_request_logs_claim_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM gateway_request_claims claim
  WHERE claim.tenant_id = NEW.tenant_id
    AND claim.request_id = NEW.request_id
    AND claim.trace_id = NEW.trace_id
    AND claim.virtual_key_id IS NEW.virtual_key_id
    AND claim.snapshot_version IS NEW.snapshot_version
    AND claim.policy_snapshot_status = NEW.policy_snapshot_status
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_claim_mismatch');
END;
CREATE TRIGGER trg_request_logs_claim_update
BEFORE UPDATE OF tenant_id, request_id, trace_id, virtual_key_id,
  snapshot_version, policy_snapshot_status
ON request_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM gateway_request_claims claim
  WHERE claim.tenant_id = NEW.tenant_id
    AND claim.request_id = NEW.request_id
    AND claim.trace_id = NEW.trace_id
    AND claim.virtual_key_id IS NEW.virtual_key_id
    AND claim.snapshot_version IS NEW.snapshot_version
    AND claim.policy_snapshot_status = NEW.policy_snapshot_status
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_claim_mismatch');
END;
CREATE TRIGGER trg_request_logs_snapshot_insert
BEFORE INSERT ON request_logs
FOR EACH ROW
WHEN NEW.snapshot_version IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.snapshot_version = NEW.snapshot_version
    AND snapshot.state = 'published'
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_snapshot_mismatch');
END;
CREATE TRIGGER trg_request_logs_snapshot_update
BEFORE UPDATE OF tenant_id, snapshot_version ON request_logs
FOR EACH ROW
WHEN NEW.snapshot_version IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = NEW.tenant_id
    AND snapshot.snapshot_version = NEW.snapshot_version
    AND snapshot.state = 'published'
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_snapshot_mismatch');
END;

-- RBAC 收缩期间短暂移除不可变与 owner trigger；删除集合已由首项门禁证明只属于
-- canonical 产品矩阵，任何租户自定义授权或权限历史都不会被静默抹除。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;

-- 先删除两项新权限的 tenant 关系，避免随后删除 Permission 投影时留下悬空复合外键。
DELETE FROM role_permissions
WHERE permission_id IN (
  SELECT permission_id FROM permissions WHERE code LIKE 'policy_template.%'
);
-- 再删除八条产品角色模板关系，使默认矩阵精确恢复 revision 4。
DELETE FROM rbac_role_permission_templates
WHERE permission_code LIKE 'policy_template.%';

-- 产品 Permission 模板恢复 revision 4 的 54 项；旧 rowid 与中文展示元数据逐值保留。
CREATE TABLE rbac_permission_templates_prd068_old (
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
-- 旧 54 项模板保留 rowid 与展示元数据，只收缩新权限并回退统一 revision。
INSERT INTO rbac_permission_templates_prd068_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,4
FROM rbac_permission_templates
WHERE permission_code NOT LIKE 'policy_template.%';
DROP TABLE rbac_permission_templates;
-- 产品目录完整复制后恢复正式表名，再重新启用不可变发布约束。
ALTER TABLE rbac_permission_templates_prd068_old RENAME TO rbac_permission_templates;

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

-- 租户 Permission 投影同步恢复 revision 4；除两项新权限外的 ID、rowid 和时间全部保真。
CREATE TABLE permissions_prd068_old (
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
-- 每个 tenant 的旧 Permission 保留稳定 ID、rowid 与时间，不复制两项新权限。
INSERT INTO permissions_prd068_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       4,status,created_at,updated_at
FROM permissions
WHERE code NOT LIKE 'policy_template.%';
DROP TABLE permissions;
-- 租户 Permission 全量保真后恢复正式表名，并重建 0059 owner trigger。
ALTER TABLE permissions_prd068_old RENAME TO permissions;
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

-- 只收缩冻结集合：升级角色 revision 精确减一，新 tenant 的 revision=1 保持 1。
UPDATE roles
SET permission_revision=CASE
      WHEN permission_revision>1 THEN permission_revision-1 ELSE 1 END,
    matrix_sha256=CASE code
      WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
      WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
      WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
      WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
    END
WHERE role_id IN (SELECT role_id FROM prd068_roles_to_contract);

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
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id
    AND role.code='admin'
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

-- 降级后新 tenant 继续按 0059 revision 4 初始化，不能残留已删除的模板权限或 hash。
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 降级后的新 tenant 先创建 revision 4 六角色，hash 不得引用已删除权限。
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

  -- 再按 revision 4 产品目录生成当前租户独立的 Permission 权限实体投影。
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

  -- 最后建立同 tenant 默认关系，确保角色初始化不存在跨租户拼接窗口。
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

-- 提交前逐项证明 0059 的 RBAC、Audit、RequestLog 与 schema 对象已经精确恢复。
DROP TABLE IF EXISTS temp.prd068_down_final_guard;
CREATE TEMP TABLE prd068_down_final_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd068_down_final_state_ok CHECK (issue_count=0)
);
-- 提交前证明 RBAC、Audit、RequestLog 和全部 PRD-068 对象均已精确恢复或移除。
INSERT INTO prd068_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=54
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=4)=54
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code LIKE 'policy_template.%')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=141
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code LIKE 'policy_template.%')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*54 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=4)
      =(SELECT COUNT(*) FROM permissions)
      AND (SELECT COUNT(*) FROM permissions WHERE code LIKE 'policy_template.%')=0
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd068_roles_to_contract source
     JOIN roles role
       ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=CASE
             WHEN source.permission_revision>1
             THEN source.permission_revision-1 ELSE 1 END
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
          WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
          WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
          WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
        END)
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=50
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='index' AND tbl_name='request_logs'
             AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='trigger' AND tbl_name='request_logs')=12
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('audit_logs'))=17
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='index' AND tbl_name='audit_logs'
             AND name NOT LIKE 'sqlite_autoindex%')=12
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='trigger' AND tbl_name='audit_logs')=6
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema
     WHERE name LIKE '%prd068'
        OR name IN (
          'policy_templates','policy_template_applications',
          'policy_template_application_items','policy_template_application_dependencies',
          'policy_template_events','idx_audit_logs_policy_template_request_unique'
        ))
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs'),'policy_template.applied')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs')
               WHERE name IN ('policy_mask_applied','policy_mask_policy_id'))=0
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd068_down_final_guard;
DROP TABLE prd068_roles_to_contract;
COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
