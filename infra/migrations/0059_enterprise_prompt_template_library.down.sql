-- PRD-067 回滚：只允许从无模板业务事实的 canonical revision 4 空状态恢复 0058。
-- Template、Version、Invocation、Operation、Audit、Webhook 与显式权限事件均是治理证据；
-- 任一存在时必须在首项持久 DDL 前失败关闭，且已有 PRD-066 Chat 事实不得删除或改写。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 业务事实使用稳定原因与结构漂移分开；TEMP trigger 在任何持久对象变化前原子拒绝不可降级状态。
DROP TABLE IF EXISTS temp.prd067_down_business_guard;
DROP TRIGGER IF EXISTS temp.trg_prd067_down_business_guard;
CREATE TEMP TABLE prd067_down_business_guard (
  invalid_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd067_down_business_guard
BEFORE INSERT ON prd067_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd067_down_business_facts_present'); END;

-- 汇总七表、模板 Audit/Webhook 和显式权限变更；任何一项都禁止删除不可伪造的治理事实。
INSERT INTO prd067_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM prompt_templates)
  + (SELECT COUNT(*) FROM prompt_template_versions)
  + (SELECT COUNT(*) FROM prompt_template_version_projects)
  + (SELECT COUNT(*) FROM prompt_template_version_models)
  + (SELECT COUNT(*) FROM chat_prompt_template_invocations)
  + (SELECT COUNT(*) FROM prompt_template_events)
  + (SELECT COUNT(*) FROM prompt_template_operations)
  + (SELECT COUNT(*) FROM audit_logs
     WHERE action IN (
       'prompt_template.created','prompt_template.updated',
       'prompt_template.version_published'
     ) OR object_type IN ('prompt_template','prompt_template_version'))
  -- JSON 精确命中与保守文本命中同时覆盖，损坏订阅不能绕过 down guard。
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE (
       json_valid(endpoint.event_types_json)
       AND EXISTS (
         SELECT 1 FROM json_each(endpoint.event_types_json) item
         WHERE item.value='prompt_template.published'
       )
     ) OR lower(endpoint.event_types_json) LIKE '%prompt_template.published%')
  + (SELECT COUNT(*) FROM webhook_deliveries
     WHERE event_type='prompt_template.published')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='prompt_template.published')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
     WHERE event_type='prompt_template.published')
  + (SELECT COUNT(*) FROM role_permission_events event
     WHERE EXISTS (
       SELECT 1 FROM json_each(event.added_permissions_json) item
       WHERE item.value IN (
         'prompt_template.read','prompt_template.write','prompt_template.use'
       )
     ) OR EXISTS (
       SELECT 1 FROM json_each(event.removed_permissions_json) item
       WHERE item.value IN (
         'prompt_template.read','prompt_template.write','prompt_template.use'
       )
     ) OR lower(event.added_permissions_json) LIKE '%prompt_template.%'
       OR lower(event.removed_permissions_json) LIKE '%prompt_template.%')
  -- 只允许完整 revision 4 产品矩阵中的 Admin/Developer/Viewer 持权；其他关系是显式业务配置。
  + (SELECT COUNT(*)
     FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     JOIN roles role
       ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
     WHERE permission.code LIKE 'prompt_template.%' AND (
       role.code NOT IN ('admin','developer','viewer')
       OR role.matrix_sha256!=CASE role.code
         WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
         WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
         WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
       END
       OR (role.code IN ('admin','developer')
           AND role.permission_revision NOT BETWEEN 1 AND 4)
       OR (role.code='viewer' AND role.permission_revision NOT BETWEEN 1 AND 3)
       OR EXISTS (
         SELECT 1 FROM rbac_role_permission_templates template
         WHERE template.role_code=role.code AND NOT EXISTS (
           SELECT 1 FROM role_permissions expected_relation
           JOIN permissions expected_permission
             ON expected_permission.tenant_id=expected_relation.tenant_id
            AND expected_permission.permission_id=expected_relation.permission_id
           WHERE expected_relation.tenant_id=role.tenant_id
             AND expected_relation.role_id=role.role_id
             AND expected_permission.code=template.permission_code
         )
       )
       OR EXISTS (
         SELECT 1 FROM role_permissions actual_relation
         JOIN permissions actual_permission
           ON actual_permission.tenant_id=actual_relation.tenant_id
          AND actual_permission.permission_id=actual_relation.permission_id
         WHERE actual_relation.tenant_id=role.tenant_id
           AND actual_relation.role_id=role.role_id
           AND NOT EXISTS (
             SELECT 1 FROM rbac_role_permission_templates template
             WHERE template.role_code=role.code
               AND template.permission_code=actual_permission.code
           )
       )
     ));
DROP TRIGGER trg_prd067_down_business_guard;
DROP TABLE prd067_down_business_guard;

-- 冻结可安全收缩的 canonical 角色；revision=1 是 0059 后新租户，其余历史值精确减一但不低于 1。
DROP TABLE IF EXISTS temp.prd067_roles_to_contract;
CREATE TEMP TABLE prd067_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','viewer')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320'
    WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
    WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
  END
  AND (
    (role.code IN ('admin','developer') AND role.permission_revision BETWEEN 1 AND 4)
    OR (role.code='viewer' AND role.permission_revision BETWEEN 1 AND 3)
  )
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

-- 结构门禁验证 0059 栈顶、canonical RBAC/Audit、完整域对象和未知后续依赖。
DROP TABLE IF EXISTS temp.prd067_down_guard;
CREATE TEMP TABLE prd067_down_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd067_down_preflight_ok CHECK (issue_count=0)
);
-- 首项 DML 原子触发全部回滚前置条件，任何业务事实或后续依赖都会在 DDL 前失败。
INSERT INTO prd067_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0059' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
     WHERE status='applied' AND version>'0059')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN (
      'prompt_templates','prompt_template_versions',
      'prompt_template_version_projects','prompt_template_version_models',
      'chat_prompt_template_invocations','prompt_template_events',
      'prompt_template_operations','chat_sessions','chat_messages',
      'chat_execution_grants','audit_logs'
    ))=11 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=54
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=4)=54
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code LIKE 'prompt_template.%')=3
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=141
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code LIKE 'prompt_template.%')=7
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*54 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=4)
      =(SELECT COUNT(*) FROM permissions)
      AND (SELECT COUNT(*) FROM permissions WHERE code LIKE 'prompt_template.%')
      =(SELECT COUNT(*)*3 FROM tenants)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM roles role
     WHERE role.code='admin' AND NOT EXISTS (
       SELECT 1 FROM prd067_roles_to_contract source
       WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
     ))
  + (SELECT COUNT(*) FROM roles role
     WHERE role.matrix_sha256 IN (
       'e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320',
       '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c',
       '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
     ) AND NOT EXISTS (
       SELECT 1 FROM prd067_roles_to_contract source
       WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
     ))
  + (SELECT COUNT(*) FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code LIKE 'prompt_template.%'
       AND NOT EXISTS (
         SELECT 1 FROM prd067_roles_to_contract source
         WHERE source.tenant_id=relation.tenant_id AND source.role_id=relation.role_id
       ))
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_prompt_template%_prd067')=18
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_chat_prompt_invocations%_prd067')=3
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN (
      'uq_chat_sessions_template_owner_prd067',
      'uq_chat_messages_template_message_prd067',
      'uq_chat_messages_template_owner_request_prd067',
      'idx_audit_logs_prompt_template_request_unique'
    ))=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND tbl_name='audit_logs'
        AND name NOT LIKE 'sqlite_autoindex%')=12 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs'),'prompt_template.version_published')>0
    THEN 0 ELSE 1 END
  -- 只有本迁移冻结的对象可引用模板七表；未知 view/trigger/index 表示后续代码已依赖，必须拒绝。
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (
         instr(lower(object.sql),'prompt_templates')>0
         OR instr(lower(object.sql),'prompt_template_versions')>0
         OR instr(lower(object.sql),'prompt_template_version_projects')>0
         OR instr(lower(object.sql),'prompt_template_version_models')>0
         OR instr(lower(object.sql),'chat_prompt_template_invocations')>0
         OR instr(lower(object.sql),'prompt_template_events')>0
         OR instr(lower(object.sql),'prompt_template_operations')>0
       )
       AND object.name NOT IN (
         'prompt_templates','prompt_template_versions',
         'prompt_template_version_projects','prompt_template_version_models',
         'chat_prompt_template_invocations','prompt_template_events',
         'prompt_template_operations',
         'idx_prompt_templates_tenant_status_prd067',
         'idx_prompt_template_versions_tenant_template_prd067',
         'idx_prompt_template_version_projects_owner_prd067',
         'idx_prompt_template_version_models_owner_prd067',
         'idx_chat_prompt_invocations_tenant_template_prd067',
         'idx_chat_prompt_invocations_tenant_request_prd067',
         'idx_prompt_template_events_tenant_status_prd067',
         'idx_prompt_template_operations_tenant_template_prd067',
         'trg_prompt_templates_owner_insert_prd067',
         'trg_prompt_templates_state_update_prd067',
         'trg_prompt_templates_delete_prd067',
         'trg_prompt_template_versions_owner_insert_prd067',
         'trg_prompt_template_versions_seal_update_prd067',
         'trg_prompt_template_versions_delete_prd067',
         'trg_prompt_template_version_projects_owner_insert_prd067',
         'trg_prompt_template_version_projects_update_prd067',
         'trg_prompt_template_version_projects_delete_prd067',
         'trg_prompt_template_version_models_owner_insert_prd067',
         'trg_prompt_template_version_models_update_prd067',
         'trg_prompt_template_version_models_delete_prd067',
         'trg_chat_prompt_invocations_owner_insert_prd067',
         'trg_chat_prompt_invocations_state_update_prd067',
         'trg_chat_prompt_invocations_delete_prd067',
         'trg_prompt_template_events_owner_insert_prd067',
         'trg_prompt_template_events_state_update_prd067',
         'trg_prompt_template_events_delete_prd067',
         'trg_prompt_template_operations_owner_insert_prd067',
         'trg_prompt_template_operations_update_prd067',
         'trg_prompt_template_operations_delete_prd067',
         'trg_audit_logs_owner_insert'
       ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd067_down_guard;

-- 七表已由业务门禁证明为空，按子表到父表删除；PRD-066 Chat 表和其全部事实保持原样。
DROP TABLE prompt_template_operations;
DROP TABLE prompt_template_events;
DROP TABLE chat_prompt_template_invocations;
DROP TABLE prompt_template_version_models;
DROP TABLE prompt_template_version_projects;
DROP TABLE prompt_template_versions;
DROP TABLE prompt_templates;

-- Invocation 的三组候选父键仅由 0059 增加，删除不会重写 ChatSession/Message 行。
DROP INDEX uq_chat_messages_template_owner_request_prd067;
DROP INDEX uq_chat_messages_template_message_prd067;
DROP INDEX uq_chat_sessions_template_owner_prd067;

-- AuditLog 恢复 0054@0058 的完整枚举；17 列、历史 rowid、NULL 与 11 个显式索引逐值保真。
-- 只有业务门禁已证明不存在模板 Audit 时才收缩枚举，Invitation/Config Transfer 仍完整保留。
CREATE TABLE audit_logs_prd067_old (
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
      'config.imported','config.exported'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider','provider_key','virtual_key','budget_rule','dlp_rule',
      'webhook_delivery','webhook_endpoint','webhook_dead_letter',
      'usage_export_job','policy','policy_snapshot','policy_explanation',
      'routing_policy','model_quality_score','cache_affinity',
      'finops_adjustment','cost_anomaly','provider_error_map','audit_export',
      'role_permission','invitation','config_transfer'
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
INSERT INTO audit_logs_prd067_old(
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
CREATE TEMP TABLE prd067_down_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 复制后逐值核对行数与 17 列内容，任何差异都在删除旧审计表前失败关闭。
INSERT INTO prd067_down_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd067_old))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd067_old WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd067_old WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd067_old WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd067_old
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd067_old
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd067_down_audit_copy_guard;

-- legacy_alter_table 保持四个已核验的外部 owner trigger 原样指向正式表名；旧表本地
-- trigger/index 随 DROP 移除，重命名后按冻结定义逐项恢复。
DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后替换正式审计表，确保历史治理证据完整保真。
ALTER TABLE audit_logs_prd067_old RENAME TO audit_logs;
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

-- RBAC 收缩期间短暂移除不可变/owner trigger；所有删除集合已经被前置门禁冻结。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;

-- 先移除三项模板关系，避免删除 tenant Permission 投影时遗留复合外键引用。
DELETE FROM role_permissions
WHERE permission_id IN (
  SELECT permission_id FROM permissions WHERE code LIKE 'prompt_template.%'
);
-- 再移除七条产品角色模板关系，使默认矩阵恢复到 revision 3。
DELETE FROM rbac_role_permission_templates
WHERE permission_code LIKE 'prompt_template.%';

-- 产品模板恢复 revision 3 的 51 项，旧 rowid 和展示元数据全部保真。
CREATE TABLE rbac_permission_templates_prd067_old (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=3)
);
-- 复制既有模板的 rowid 与展示元数据，仅收缩三项模板权限并回退目录 revision。
INSERT INTO rbac_permission_templates_prd067_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,3
FROM rbac_permission_templates
WHERE permission_code NOT LIKE 'prompt_template.%';
DROP TABLE rbac_permission_templates;
-- 原子替换产品权限目录，让原表名重新指向已验证的 revision 3 结构。
ALTER TABLE rbac_permission_templates_prd067_old RENAME TO rbac_permission_templates;

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

-- 租户 Permission 删除三项模板投影并恢复 revision 3；其余稳定 ID、rowid 和时间不变。
CREATE TABLE permissions_prd067_old (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=3),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 逐租户复制稳定权限标识与 rowid，只删除本迁移新增的三项模板投影。
INSERT INTO permissions_prd067_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       3,status,created_at,updated_at
FROM permissions
WHERE code NOT LIKE 'prompt_template.%';
DROP TABLE permissions;
-- 原子恢复租户权限表名称，再重建 revision 3 的索引与不可变约束触发器。
ALTER TABLE permissions_prd067_old RENAME TO permissions;
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

-- 只收缩前置集合中的 canonical 角色；新租户 revision=1 保持 1，升级角色回退一次。
UPDATE roles
SET permission_revision=CASE
      WHEN permission_revision>1 THEN permission_revision-1 ELSE 1 END,
    matrix_sha256=CASE code
      WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
      WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
      WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
    END
WHERE role_id IN (SELECT role_id FROM prd067_roles_to_contract);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
    WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
    WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
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

-- 降级后新 tenant 仍按 revision 3 产品目录初始化六角色和 51 项 tenant 投影。
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 先创建六个默认角色，后续权限关系才能通过同租户复合外键与模板约束。
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
      WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
      WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
      WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
      WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
      WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
      WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 再按 revision 3 产品目录生成租户权限投影，禁止沿用已删除的模板权限。
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

  -- 最后依据产品角色模板建立关系，确保每条授权都由同租户角色和权限共同拥有。
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

-- 提交前证明 revision 3、0054@0058 Audit、PRD-066 Chat 与完整性均已精确恢复。
DROP TABLE IF EXISTS temp.prd067_down_final_guard;
CREATE TEMP TABLE prd067_down_final_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd067_down_final_state_ok CHECK (issue_count=0)
);
-- 最终 DML 在提交前复核 RBAC/Audit/Chat 保真与数据库完整性，禁止留下部分降级状态。
INSERT INTO prd067_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=51
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=3)=51
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code LIKE 'prompt_template.%')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=134
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code LIKE 'prompt_template.%')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*51 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=3)
      =(SELECT COUNT(*) FROM permissions)
      AND (SELECT COUNT(*) FROM permissions WHERE code LIKE 'prompt_template.%')=0
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd067_roles_to_contract source
     JOIN roles role
       ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=CASE
             WHEN source.permission_revision>1
             THEN source.permission_revision-1 ELSE 1 END
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
          WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
          WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
        END)
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'prompt_templates','prompt_template_versions',
      'prompt_template_version_projects','prompt_template_version_models',
      'chat_prompt_template_invocations','prompt_template_events',
      'prompt_template_operations','uq_chat_sessions_template_owner_prd067',
      'uq_chat_messages_template_message_prd067',
      'uq_chat_messages_template_owner_request_prd067',
      'idx_audit_logs_prompt_template_request_unique'
    ) OR name LIKE 'trg_prompt_template%_prd067'
      OR name LIKE 'trg_chat_prompt_invocations%_prd067')
  -- 0058 的 Chat 表、13 个显式索引和 9 个 trigger 必须仍在，既有业务行允许并必须保留。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'chat_sessions','chat_messages','chat_execution_grants'
      ))=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN (
      'uq_sessions_tenant_session_prd066',
      'uq_chat_sessions_tenant_session_prd066',
      'idx_chat_sessions_tenant_owner_status_prd066',
      'idx_chat_sessions_tenant_expiry_prd066',
      'uq_chat_messages_tenant_message_prd066',
      'uq_chat_messages_tenant_session_client_prd066',
      'uq_chat_messages_tenant_gateway_request_prd066',
      'uq_chat_messages_tenant_session_inflight_prd066',
      'idx_chat_messages_tenant_session_sequence_prd066',
      'uq_chat_execution_grants_hash_prd066',
      'uq_chat_execution_grants_tenant_message_prd066',
      'idx_chat_execution_grants_tenant_session_status_prd066',
      'idx_chat_execution_grants_tenant_expiry_prd066'
    ))=13 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_chat_%_prd066')=9
    THEN 0 ELSE 1 END
  -- Audit 恢复 17 列、11 个显式索引和 10 个历史 trigger，模板枚举必须完全消失。
  + CASE WHEN (
      SELECT group_concat(name,',') FROM (
        SELECT name FROM pragma_table_info('audit_logs') ORDER BY cid
      )
    )='audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND tbl_name='audit_logs'
        AND name NOT LIKE 'sqlite_autoindex%')=11 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='audit_logs'),'prompt_template')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name IN (
      'trg_audit_logs_owner_insert','trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete','trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert','trg_audit_logs_export_sequence_insert_prd058',
      'trg_audit_exports_owner_insert_prd058',
      'trg_audit_log_export_sequences_owner_insert_prd058',
      'trg_invitation_events_owner_insert_prd060',
      'trg_role_permission_events_owner_insert_prd059'
    ))=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_log_export_sequences)
      =(SELECT COUNT(*) FROM audit_logs) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd067_down_final_guard;
DROP TABLE prd067_roles_to_contract;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
