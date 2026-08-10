-- PRD-075 回滚迁移：仅在实验业务事实和活动租约清空后恢复 RBAC revision 10。
-- PRD-075 rollback: preserve every experiment, audit, request, RBAC, and Webhook fact.
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 冻结 revision 11 的完整权限目录；down 前按原值复核，down 后投影为 revision 10。
DROP TABLE IF EXISTS temp.prd075_expected_permissions;
CREATE TEMP TABLE prd075_expected_permissions(
  permission_code TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  catalog_revision INTEGER NOT NULL
) WITHOUT ROWID;
-- 写入冻结目录，不从待验证数据库反向生成期望值。
INSERT INTO prd075_expected_permissions(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('console.basic.read','console','基础控制台','访问概览、API 文档、系统状态和迁移兼容页面。','read',11),
  ('dashboard.read','console','读取概览','读取当前租户的聚合运行概览，不包含正式账单明细。','read',11),
  ('tenant.settings.read','tenant','读取租户设置','读取当前租户的脱敏基础配置。','read',11),
  ('user.manage','tenant','管理用户','创建租户用户并分配固定角色。','critical',11),
  ('rbac.matrix.manage','tenant','管理权限矩阵','读取并调整固定角色的可授权权限集合。','critical',11),
  ('environment.read','environment','读取环境','读取当前租户的环境边界和状态。','read',11),
  ('environment.write','environment','管理环境','创建、启停和维护当前租户环境。','write',11),
  ('project.read','project','读取项目','读取项目、应用及其当前租户归属。','read',11),
  ('project.write','project','管理项目','创建和维护项目、应用及资源绑定。','write',11),
  ('virtual_key.read','key','读取 Virtual Key','读取脱敏 Virtual Key 元数据。','read',11),
  ('virtual_key.write','key','管理 Virtual Key','创建、更新和撤销当前租户 Virtual Key。','critical',11),
  ('provider.read','provider','读取 Provider','读取 Provider 与 KeyPool 的脱敏配置。','read',11),
  ('provider.write','provider','管理 Provider','创建、更新和测试 Provider 与 KeyPool。','critical',11),
  ('provider.health.read','provider','读取 Provider 健康','读取上游健康状态和归一化错误摘要。','read',11),
  ('provider.health.probe','provider','探测 Provider','主动触发当前租户 Provider 健康探测。','write',11),
  ('provider_error_map.read','provider','读取错误映射','读取 Provider 错误归一规则。','read',11),
  ('provider_error_map.write','provider','管理错误映射','创建和更新 Provider 错误归一规则。','critical',11),
  ('model.read','model','读取模型','读取当前租户模型目录。','read',11),
  ('model.write','model','管理模型','创建和更新当前租户模型目录。','write',11),
  ('model_price.read','model','读取价格','读取模型价格时间线和版本。','read',11),
  ('model_price.write','model','管理价格','创建不可变模型价格计划。','critical',11),
  ('routing.read','routing','读取路由','读取别名、回退、路由和缓存亲和配置。','read',11),
  ('routing.write','routing','管理路由','创建和更新别名、回退及路由策略。','critical',11),
  ('config.transfer.read','migration','导出配置','读取并导出当前租户的严格白名单配置。','read',11),
  ('config.transfer.write','migration','导入配置','以 create-only 方式批量导入配置并生成全新 Virtual Key。','critical',11),
  ('model_quality.read','routing','读取模型质量','读取模型质量、成功率和延迟聚合。','read',11),
  ('model_quality.write','routing','管理模型质量','维护模型人工质量评分。','write',11),
  ('policy.read','security','读取策略','读取治理策略和版本摘要。','read',11),
  ('policy.write','security','管理策略','创建和更新治理策略。','critical',11),
  ('policy.simulate','security','模拟策略','对脱敏输入执行策略 Dry-Run。','write',11),
  ('policy.publish','security','发布策略','发布或回滚策略快照。','critical',11),
  ('dlp.read','security','读取 DLP','读取 DLP 规则的脱敏元数据。','read',11),
  ('dlp.write','security','管理 DLP','创建和更新 DLP 规则。','critical',11),
  ('request_log.read','observability','读取调用日志','读取当前租户调用日志、Trace 和保存筛选。','read',11),
  ('request_explanation.read','observability','读取请求解释','读取鉴权、路由、成本和安全解释事实。','read',11),
  ('feedback.use','observability','使用反馈','提交请求反馈并读取租户反馈汇总。','write',11),
  ('billing.usage.read','billing','读取用量','读取全租户用量、Token 和成本明细。','read',11),
  ('billing.usage.export','billing','导出用量','导出当前租户用量和成本账单。','critical',11),
  ('billing.finops.read','billing','读取正式账本','读取 append-only 正式账本和调整记录。','read',11),
  ('billing.finops.write','billing','调整正式账本','创建财务调整和冲正。','critical',11),
  ('billing.budget.read','billing','读取预算','读取预算规则和执行状态。','read',11),
  ('billing.budget.write','billing','管理预算','创建和更新预算规则。','critical',11),
  ('billing.anomaly.read','billing','读取成本异常','读取成本异常和检测器状态。','read',11),
  ('billing.anomaly.write','billing','处置成本异常','确认、忽略异常并维护临时限流。','critical',11),
  ('audit.read','audit','读取审计','读取当前租户不可变管理审计事实。','read',11),
  ('audit.export','audit','导出审计','导出不可变审计快照并生成复核哈希。','critical',11),
  ('webhook.manage','integration','管理 Webhook','管理 Endpoint、死信、重放和归档。','critical',11),
  ('developer_portal.read','developer','读取开发者门户','读取快速开始、SDK 示例和迁移指南。','read',11),
  ('debug_console.use','developer','使用 API 调试台','使用内置 OpenAI-compatible API 调试台。','write',11),
  ('chat.use','chat','使用企业 Chat','使用受 Gateway 全链路治理且不长期保存正文的企业 Chat。','write',11),
  ('prompt_template.read','prompt_template','读取 Prompt 模板','读取企业 Prompt 模板元数据、版本历史和授权后的模板定义。','read',11),
  ('prompt_template.write','prompt_template','管理 Prompt 模板','创建、修改、启停企业 Prompt 模板并发布不可变版本。','critical',11),
  ('prompt_template.use','prompt_template','使用 Prompt 模板','在企业 Chat 中选择并调用当前已发布的 Prompt 模板版本。','write',11),
  ('policy_template.read','security','读取策略模板','读取当前租户内置策略模板、适配状态和安全影响预览。','read',11),
  ('policy_template.apply','security','应用策略模板','应用服务端冻结的策略模板并原子发布策略快照。','critical',11),
  ('intent_risk.classify','security','分类敏感意图','对一次性文本执行本地敏感意图分类，不保存输入正文或命中片段。','write',11),
  ('session_risk.read','security','读取会话风险','读取当前租户高危会话、冻结状态和不可变处置摘要。','read',11),
  ('session_risk.manage','security','处置会话风险','冻结或释放已识别会话及其关联 Virtual Key 风险覆盖层。','critical',11),
  ('file.read','file','读取文件','读取当前租户文件处理状态、抽取文本和安全摘要。','read',11),
  ('file.write','file','上传文件','上传并解析当前租户的受支持文件。','write',11),
  ('file.security.scan','file','扫描文件 Prompt Injection','对当前租户文件执行隐藏 Prompt Injection 检测并写入不可变安全报告。','write',11),
  ('batch.read','batch','读取批量任务','读取当前租户批量任务、任务状态、成本摘要和授权后的结果。','read',11),
  ('batch.write','batch','管理批量任务','创建、取消和执行当前租户受治理的异步批量任务。','write',11),
  ('experiment.read','experiment','读取模型实验','读取当前租户模型 A/B 实验的安全配置、进度和权威结果。','read',11),
  ('experiment.write','experiment','创建模型实验','创建影响当前租户在线 Alias 流量的不可变模型 A/B 实验。','critical',11),
  ('system.config.read','system','读取系统配置','读取脱敏运行配置和系统状态。','read',11);

-- 冻结六个固定角色的 179 条 revision-11 默认关系，连同 default_granted 一并校验。
DROP TABLE IF EXISTS temp.prd075_expected_role_permissions;
CREATE TEMP TABLE prd075_expected_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  default_granted INTEGER NOT NULL,
  PRIMARY KEY(role_code,permission_code)
) WITHOUT ROWID;
-- 写入冻结角色关系，不以数量或待验证关系表作为期望值。
INSERT INTO prd075_expected_role_permissions(role_code,permission_code,default_granted)
VALUES
  ('admin','audit.export',1),('admin','audit.read',1),('admin','batch.read',1),('admin','batch.write',1),
  ('admin','billing.anomaly.read',1),('admin','billing.anomaly.write',1),('admin','billing.budget.read',1),('admin','billing.budget.write',1),
  ('admin','billing.finops.read',1),('admin','billing.finops.write',1),('admin','billing.usage.export',1),('admin','billing.usage.read',1),
  ('admin','chat.use',1),('admin','config.transfer.read',1),('admin','config.transfer.write',1),('admin','console.basic.read',1),
  ('admin','dashboard.read',1),('admin','debug_console.use',1),('admin','developer_portal.read',1),('admin','dlp.read',1),
  ('admin','dlp.write',1),('admin','environment.read',1),('admin','environment.write',1),('admin','experiment.read',1),
  ('admin','experiment.write',1),('admin','feedback.use',1),('admin','file.read',1),('admin','file.security.scan',1),
  ('admin','file.write',1),('admin','intent_risk.classify',1),('admin','model.read',1),('admin','model.write',1),
  ('admin','model_price.read',1),('admin','model_price.write',1),('admin','model_quality.read',1),('admin','model_quality.write',1),
  ('admin','policy.publish',1),('admin','policy.read',1),('admin','policy.simulate',1),('admin','policy.write',1),
  ('admin','policy_template.apply',1),('admin','policy_template.read',1),('admin','project.read',1),('admin','project.write',1),
  ('admin','prompt_template.read',1),('admin','prompt_template.use',1),('admin','prompt_template.write',1),('admin','provider.health.probe',1),
  ('admin','provider.health.read',1),('admin','provider.read',1),('admin','provider.write',1),('admin','provider_error_map.read',1),
  ('admin','provider_error_map.write',1),('admin','rbac.matrix.manage',1),('admin','request_explanation.read',1),('admin','request_log.read',1),
  ('admin','routing.read',1),('admin','routing.write',1),('admin','session_risk.manage',1),('admin','session_risk.read',1),
  ('admin','system.config.read',1),('admin','tenant.settings.read',1),('admin','user.manage',1),('admin','virtual_key.read',1),
  ('admin','virtual_key.write',1),('admin','webhook.manage',1),('developer','batch.read',1),('developer','batch.write',1),
  ('developer','chat.use',1),('developer','config.transfer.read',1),('developer','config.transfer.write',1),('developer','console.basic.read',1),
  ('developer','dashboard.read',1),('developer','debug_console.use',1),('developer','developer_portal.read',1),('developer','environment.read',1),
  ('developer','experiment.read',1),('developer','experiment.write',1),('developer','feedback.use',1),('developer','file.read',1),
  ('developer','file.security.scan',1),('developer','file.write',1),('developer','model.read',1),('developer','model_price.read',1),
  ('developer','model_quality.read',1),('developer','project.read',1),('developer','prompt_template.read',1),('developer','prompt_template.use',1),
  ('developer','prompt_template.write',1),('developer','provider.health.probe',1),('developer','provider.health.read',1),('developer','provider_error_map.read',1),
  ('developer','request_explanation.read',1),('developer','request_log.read',1),('developer','routing.read',1),('developer','virtual_key.read',1),
  ('developer','virtual_key.write',1),('developer','webhook.manage',1),('viewer','chat.use',1),('viewer','console.basic.read',1),
  ('viewer','dashboard.read',1),('viewer','file.read',1),('viewer','file.security.scan',1),('viewer','file.write',1),
  ('viewer','prompt_template.use',1),('finance_admin','billing.anomaly.read',1),('finance_admin','billing.anomaly.write',1),('finance_admin','billing.budget.read',1),
  ('finance_admin','billing.budget.write',1),('finance_admin','billing.finops.read',1),('finance_admin','billing.finops.write',1),('finance_admin','billing.usage.export',1),
  ('finance_admin','billing.usage.read',1),('finance_admin','console.basic.read',1),('finance_admin','dashboard.read',1),('finance_admin','model.read',1),
  ('finance_admin','model_price.read',1),('finance_admin','model_price.write',1),('finance_admin','policy_template.apply',1),('finance_admin','policy_template.read',1),
  ('security_admin','audit.export',1),('security_admin','audit.read',1),('security_admin','billing.anomaly.read',1),('security_admin','billing.anomaly.write',1),
  ('security_admin','console.basic.read',1),('security_admin','dashboard.read',1),('security_admin','dlp.read',1),('security_admin','dlp.write',1),
  ('security_admin','file.read',1),('security_admin','file.security.scan',1),('security_admin','intent_risk.classify',1),('security_admin','model.read',1),
  ('security_admin','model_price.read',1),('security_admin','policy.publish',1),('security_admin','policy.read',1),('security_admin','policy.simulate',1),
  ('security_admin','policy.write',1),('security_admin','policy_template.apply',1),('security_admin','policy_template.read',1),('security_admin','provider_error_map.read',1),
  ('security_admin','provider_error_map.write',1),('security_admin','request_explanation.read',1),('security_admin','request_log.read',1),('security_admin','session_risk.manage',1),
  ('security_admin','session_risk.read',1),('security_admin','webhook.manage',1),('ops_admin','batch.read',1),('ops_admin','batch.write',1),
  ('ops_admin','config.transfer.read',1),('ops_admin','config.transfer.write',1),('ops_admin','console.basic.read',1),('ops_admin','dashboard.read',1),
  ('ops_admin','environment.read',1),('ops_admin','environment.write',1),('ops_admin','experiment.read',1),('ops_admin','experiment.write',1),
  ('ops_admin','file.read',1),('ops_admin','model.read',1),('ops_admin','model.write',1),('ops_admin','model_price.read',1),
  ('ops_admin','model_quality.read',1),('ops_admin','model_quality.write',1),('ops_admin','policy_template.apply',1),('ops_admin','policy_template.read',1),
  ('ops_admin','project.read',1),('ops_admin','project.write',1),('ops_admin','provider.health.probe',1),('ops_admin','provider.health.read',1),
  ('ops_admin','provider.read',1),('ops_admin','provider.write',1),('ops_admin','provider_error_map.read',1),('ops_admin','request_explanation.read',1),
  ('ops_admin','request_log.read',1),('ops_admin','routing.read',1),('ops_admin','routing.write',1),('ops_admin','system.config.read',1),
  ('ops_admin','virtual_key.read',1),('ops_admin','virtual_key.write',1),('ops_admin','webhook.manage',1);

-- 清理临时迁移守卫 temp.prd075_down_guard；该对象不承载租户业务数据。
DROP TABLE IF EXISTS temp.prd075_down_guard;
CREATE TEMP TABLE prd075_down_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd075_down_guard_ok CHECK(issue_count=0)
);
-- 在回滚事务中处理 prd075_down_guard 的守卫或 revision 事实；异常会使整次回滚失败。
INSERT INTO prd075_down_guard(issue_count)
SELECT (SELECT COUNT(*) FROM gateway_request_scope_claims)
  + (SELECT COUNT(*) FROM model_experiments)
  + (SELECT COUNT(*) FROM experiment_assignments)
  + (SELECT COUNT(*) FROM experiment_request_executions)
  + (SELECT COUNT(*) FROM experiment_request_outcomes)
  + (SELECT COUNT(*) FROM experiment_usage_settlements)
  + (SELECT COUNT(*) FROM experiment_feedback_attributions)
  + (SELECT COUNT(*) FROM experiment_results)
  + (SELECT COUNT(*) FROM experiment_events)
  + (SELECT COUNT(*) FROM experiment_event_outbox)
  + (SELECT COUNT(*) FROM audit_logs
      WHERE action LIKE 'experiment.%' OR object_type='experiment'
        OR (length(object_id)=36 AND substr(object_id,1,4)='exp_'))
  + (SELECT COUNT(*) FROM role_permission_events event
      WHERE EXISTS(SELECT 1 FROM json_each(event.added_permissions_json)
        WHERE value IN ('experiment.read','experiment.write'))
         OR EXISTS(SELECT 1 FROM json_each(event.removed_permissions_json)
        WHERE value IN ('experiment.read','experiment.write')))
  + (SELECT COUNT(*) FROM request_logs
      WHERE error_code IN (
        'gateway.experiment_unavailable','gateway.experiment_variant_unavailable',
        'gateway.experiment_execution_timeout'))
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
      WHERE json_valid(endpoint.event_types_json)
        AND EXISTS(SELECT 1 FROM json_each(endpoint.event_types_json)
          WHERE value='experiment.completed'))
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE event_type='experiment.completed')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
      WHERE EXISTS(SELECT 1 FROM webhook_deliveries delivery
        WHERE delivery.webhook_delivery_id=attempt.webhook_delivery_id
          AND delivery.event_type='experiment.completed'))
  + (SELECT COUNT(*) FROM webhook_dead_letter_events dead
      WHERE EXISTS(SELECT 1 FROM webhook_deliveries delivery
        WHERE delivery.webhook_delivery_id=dead.webhook_delivery_id
          AND delivery.event_type='experiment.completed'))
  + (SELECT COUNT(*) FROM experiment_scheduler_tenants scheduler
      WHERE scheduler.lease_token IS NOT NULL
        AND unixepoch(scheduler.lease_expires_at)>unixepoch('now'))
  + CASE WHEN NOT EXISTS(
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM prd075_expected_permissions
      EXCEPT
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM rbac_permission_templates
    ) AND NOT EXISTS(
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM rbac_permission_templates
      EXCEPT
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM prd075_expected_permissions
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT role_code,permission_code,default_granted
      FROM prd075_expected_role_permissions
      EXCEPT
      SELECT role_code,permission_code,default_granted
      FROM rbac_role_permission_templates
    ) AND NOT EXISTS(
      SELECT role_code,permission_code,default_granted
      FROM rbac_role_permission_templates
      EXCEPT
      SELECT role_code,permission_code,default_granted
      FROM prd075_expected_role_permissions
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT tenant.tenant_id,expected.permission_code,expected.category,expected.name,
        expected.description,expected.risk_level,expected.catalog_revision,'active'
      FROM tenants tenant CROSS JOIN prd075_expected_permissions expected
      EXCEPT
      SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
      FROM permissions
    ) AND NOT EXISTS(
      SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
      FROM permissions
      EXCEPT
      SELECT tenant.tenant_id,expected.permission_code,expected.category,expected.name,
        expected.description,expected.risk_level,expected.catalog_revision,'active'
      FROM tenants tenant CROSS JOIN prd075_expected_permissions expected
    ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd075_down_guard;

-- Only roles that still exactly match the revision-11 canonical matrix may be contracted.
CREATE TEMP TABLE prd075_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148'
    WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
    WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
  END
  AND NOT EXISTS(
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS(
      SELECT 1 FROM role_permissions relation
      JOIN permissions permission ON permission.tenant_id=relation.tenant_id
        AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code
    )
  )
  AND NOT EXISTS(
    SELECT 1 FROM role_permissions relation
    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS(SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code)
  );

-- 清理临时迁移守卫 temp.prd075_down_role_guard；该对象不承载租户业务数据。
DROP TABLE IF EXISTS temp.prd075_down_role_guard;
CREATE TEMP TABLE prd075_down_role_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd075_down_role_guard_ok CHECK(issue_count=0)
);
-- 在回滚事务中处理 prd075_down_role_guard 的守卫或 revision 事实；异常会使整次回滚失败。
INSERT INTO prd075_down_role_guard(issue_count)
SELECT COUNT(*)
FROM role_permissions relation
JOIN permissions permission ON permission.tenant_id=relation.tenant_id
  AND permission.permission_id=relation.permission_id
JOIN roles role ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
WHERE permission.code IN ('experiment.read','experiment.write')
  AND NOT EXISTS(SELECT 1 FROM prd075_roles_to_contract canonical
    WHERE canonical.tenant_id=role.tenant_id AND canonical.role_id=role.role_id);
DROP TABLE prd075_down_role_guard;

-- Existing-table triggers are removed before the new owner graph is contracted.
DROP TRIGGER trg_request_logs_experiment_update_prd075;
DROP TRIGGER trg_request_logs_experiment_insert_prd075;
DROP TRIGGER trg_stream_sessions_experiment_update_prd075;
DROP TRIGGER trg_stream_sessions_experiment_insert_prd075;
DROP TRIGGER trg_routing_attempts_experiment_update_prd075;
DROP TRIGGER trg_routing_attempts_experiment_insert_prd075;
DROP TRIGGER trg_routing_decisions_experiment_delete_prd075;
DROP TRIGGER trg_routing_decisions_experiment_update_prd075;
DROP TRIGGER trg_request_feedbacks_experiment_attribution_prd075;
DROP TRIGGER trg_finops_experiment_settlement_prd075;
DROP TRIGGER trg_virtual_keys_scope_delete_prd075;
DROP TRIGGER trg_virtual_keys_scope_update_prd075;
DROP TRIGGER trg_gateway_claims_scope_delete_prd075;
DROP TRIGGER trg_gateway_claims_scope_update_prd075;
DROP TRIGGER trg_aliases_experiment_scope_delete_prd075;
DROP TRIGGER trg_aliases_experiment_scope_update_prd075;
DROP TRIGGER trg_environments_experiment_scope_update_prd075;
DROP TRIGGER trg_projects_experiment_scope_update_prd075;
DROP TRIGGER trg_providers_experiment_arm_delete_prd075;
DROP TRIGGER trg_providers_experiment_arm_update_prd075;
DROP TRIGGER trg_models_experiment_arm_delete_prd075;
DROP TRIGGER trg_models_experiment_arm_update_prd075;

DROP TRIGGER trg_experiment_scheduler_update_prd075;
DROP TRIGGER trg_experiment_scheduler_owner_insert_prd075;
DROP TRIGGER trg_experiment_outbox_rowid_prd075;
DROP TRIGGER trg_experiment_outbox_delete_prd075;
DROP TRIGGER trg_experiment_outbox_update_prd075;
DROP TRIGGER trg_experiment_outbox_owner_insert_prd075;
DROP TRIGGER trg_experiment_events_create_outbox_prd075;
DROP TRIGGER trg_experiment_events_rowid_prd075;
DROP TRIGGER trg_experiment_events_delete_prd075;
DROP TRIGGER trg_experiment_events_update_prd075;
DROP TRIGGER trg_experiment_events_owner_insert_prd075;
DROP TRIGGER trg_experiment_results_rowid_prd075;
DROP TRIGGER trg_experiment_results_delete_prd075;
DROP TRIGGER trg_experiment_results_update_prd075;
DROP TRIGGER trg_experiment_results_owner_insert_prd075;
DROP TRIGGER trg_experiment_feedback_facts_prd075;
DROP TRIGGER trg_experiment_feedback_rowid_prd075;
DROP TRIGGER trg_experiment_feedback_delete_prd075;
DROP TRIGGER trg_experiment_feedback_update_prd075;
DROP TRIGGER trg_experiment_feedback_owner_insert_prd075;
DROP TRIGGER trg_experiment_settlements_facts_prd075;
DROP TRIGGER trg_experiment_settlements_rowid_prd075;
DROP TRIGGER trg_experiment_settlements_delete_prd075;
DROP TRIGGER trg_experiment_settlements_update_prd075;
DROP TRIGGER trg_experiment_settlements_owner_insert_prd075;
DROP TRIGGER trg_experiment_outcomes_facts_prd075;
DROP TRIGGER trg_experiment_outcomes_rowid_prd075;
DROP TRIGGER trg_experiment_outcomes_delete_prd075;
DROP TRIGGER trg_experiment_outcomes_update_prd075;
DROP TRIGGER trg_experiment_outcomes_owner_insert_prd075;
DROP TRIGGER trg_experiment_executions_rowid_prd075;
DROP TRIGGER trg_experiment_executions_delete_prd075;
DROP TRIGGER trg_experiment_executions_update_prd075;
DROP TRIGGER trg_experiment_executions_owner_insert_prd075;
DROP TRIGGER trg_experiment_assignments_count_prd075;
DROP TRIGGER trg_experiment_assignments_rowid_prd075;
DROP TRIGGER trg_experiment_assignments_delete_prd075;
DROP TRIGGER trg_experiment_assignments_update_prd075;
DROP TRIGGER trg_experiment_assignments_owner_insert_prd075;
DROP TRIGGER trg_gateway_scope_claims_rowid_prd075;
DROP TRIGGER trg_gateway_scope_claims_delete_prd075;
DROP TRIGGER trg_gateway_scope_claims_update_prd075;
DROP TRIGGER trg_gateway_scope_claims_owner_insert_prd075;
DROP TRIGGER trg_model_experiments_feedback_fence_facts_prd075;
DROP TRIGGER trg_model_experiments_rowid_prd075;
DROP TRIGGER trg_model_experiments_delete_prd075;
DROP TRIGGER trg_model_experiments_finalization_update_prd075;
DROP TRIGGER trg_model_experiments_state_update_prd075;
DROP TRIGGER trg_model_experiments_immutable_update_prd075;
DROP TRIGGER trg_model_experiments_owner_insert_prd075;

-- 在回滚事务中处理 experiment_scheduler_tenants 的守卫或 revision 事实；异常会使整次回滚失败。
DELETE FROM experiment_scheduler_tenants;
DROP TABLE experiment_event_outbox;
DROP TABLE experiment_events;
DROP TABLE experiment_results;
DROP TABLE experiment_feedback_attributions;
DROP TABLE experiment_usage_settlements;
DROP TABLE experiment_request_outcomes;
DROP TABLE experiment_request_executions;
DROP TABLE experiment_assignments;
DROP TABLE model_experiments;
DROP TABLE gateway_request_scope_claims;
DROP TABLE experiment_scheduler_tenants;

-- AuditLog down 仅在实验审计事实为零后恢复 0067 的冻结枚举和全部旧 owner 约束。
-- 17 列、rowid、NULL 分布、索引与触发器均逐值恢复，历史审计事实不得漂移。
DROP TABLE IF EXISTS audit_logs_prd075_old;
CREATE TABLE audit_logs_prd075_old (
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
      'session_risk.freeze_session','session_risk.freeze_virtual_key',
      'session_risk.dismiss','session_risk.release_session',
      'session_risk.release_virtual_key'
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
      'policy_template_application','session_risk'
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
INSERT INTO audit_logs_prd075_old(
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
CREATE TEMP TABLE prd075_down_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 复制后逐值核对行数与 17 列内容，任何差异都在删除旧审计表前失败关闭。
INSERT INTO prd075_down_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd075_old))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd075_old WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd075_old WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd075_old WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd075_old
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd075_old
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd075_down_audit_copy_guard;

-- legacy_alter_table 保持四个已核验的外部 owner trigger 原样指向正式表名；旧表本地
-- trigger/index 随 DROP 移除，重命名后按冻结定义逐项恢复。
DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后替换正式审计表，确保历史治理证据完整保真。
ALTER TABLE audit_logs_prd075_old RENAME TO audit_logs;
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

-- tenant owner 候选键在 Audit 换表后必须显式恢复，供 application 的 deferred 环边使用。
CREATE UNIQUE INDEX uq_audit_logs_owner_prd068
  ON audit_logs(tenant_id,audit_log_id);
CREATE UNIQUE INDEX idx_audit_logs_policy_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN ('policy_template.applied','policy_template.rolled_back');

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

-- Template Audit 必须反向绑定同 tenant application、actor、request/trace 与预生成 fact ID。
-- 摘要只允许稳定治理字段，明确排除绑定值、价格、预算余额、URL、Key 和请求正文。
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

-- 每个管理员处置 source action 只允许一条同 score/request 审计；重复写入不能伪造第二份事实。

CREATE UNIQUE INDEX idx_audit_logs_session_risk_action_unique_prd070
  ON audit_logs(tenant_id,object_id,request_id)
  WHERE action IN (
    'session_risk.freeze_session','session_risk.freeze_virtual_key',
    'session_risk.dismiss','session_risk.release_session',
    'session_risk.release_virtual_key'
  );

-- SessionRisk Audit 必须反向绑定同租户 action、score、event、actor、request/trace 和逐字段摘要。
-- object_name 只保存 scope_type；API HMAC、幂等键、Prompt、Response 和命中片段均不得进入审计。
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

-- 在回滚事务中处理 role_permissions 的守卫或 revision 事实；异常会使整次回滚失败。
DELETE FROM role_permissions
WHERE EXISTS(SELECT 1 FROM prd075_roles_to_contract role
  WHERE role.tenant_id=role_permissions.tenant_id
    AND role.role_id=role_permissions.role_id)
  AND permission_id IN(SELECT permission_id FROM permissions
    WHERE tenant_id=role_permissions.tenant_id
      AND code IN ('experiment.read','experiment.write'));
-- 在回滚事务中处理 roles 的守卫或 revision 事实；异常会使整次回滚失败。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN 'ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262'
      WHEN 'developer' THEN '89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30'
      WHEN 'ops_admin' THEN '72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6'
    END
WHERE role_id IN(SELECT role_id FROM prd075_roles_to_contract);

-- 在回滚事务中处理 rbac_role_permission_templates 的守卫或 revision 事实；异常会使整次回滚失败。
DELETE FROM rbac_role_permission_templates
WHERE role_code IN ('admin','developer','ops_admin')
  AND permission_code IN ('experiment.read','experiment.write');

CREATE TABLE rbac_permission_templates_prd075_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=10)
);
-- 在回滚事务中处理 rbac_permission_templates_prd075_old 的守卫或 revision 事实；异常会使整次回滚失败。
INSERT INTO rbac_permission_templates_prd075_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,10
FROM rbac_permission_templates
WHERE permission_code NOT IN ('experiment.read','experiment.write');
DROP TABLE rbac_permission_templates;
-- 切换 rbac_permission_templates_prd075_old 的迁移期表结构；前后均保持租户归属、外键和状态约束。
ALTER TABLE rbac_permission_templates_prd075_old RENAME TO rbac_permission_templates;

CREATE TABLE permissions_prd075_old(
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
-- 在回滚事务中处理 permissions_prd075_old 的守卫或 revision 事实；异常会使整次回滚失败。
INSERT INTO permissions_prd075_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       10,status,created_at,updated_at
FROM permissions WHERE code NOT IN ('experiment.read','experiment.write');
DROP TABLE permissions;
-- 切换 permissions_prd075_old 的迁移期表结构；前后均保持租户归属、外键和状态约束。
ALTER TABLE permissions_prd075_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

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
WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level AND template.catalog_revision=NEW.catalog_revision)
  OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS(SELECT 1 FROM permissions current WHERE current.permission_id=NEW.permission_id
    OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059 AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0
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
WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name
  OR NEW.description!=OLD.description OR NEW.status!=OLD.status
  OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
    AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id AND role.status='active'
    AND permission.permission_id=NEW.permission_id AND permission.status='active')
  OR EXISTS(SELECT 1 FROM role_permissions current
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
  -- 在回滚事务中处理 roles 的守卫或 revision 事实；异常会使整次回滚失败。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  ) SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
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
  -- 在回滚事务中处理 permissions 的守卫或 revision 事实；异常会使整次回滚失败。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  ) SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    10,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  -- 在回滚事务中处理 role_permissions 的守卫或 revision 事实；异常会使整次回滚失败。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  ) SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code WHERE role.tenant_id=NEW.tenant_id;
END;

DROP TABLE prd075_roles_to_contract;
-- 清理临时迁移守卫 temp.prd075_down_final_guard；该对象不承载租户业务数据。
DROP TABLE IF EXISTS temp.prd075_down_final_guard;
CREATE TEMP TABLE prd075_down_final_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd075_down_final_guard_ok CHECK(issue_count=0)
);
-- 在回滚事务中处理 prd075_down_final_guard 的守卫或 revision 事实；异常会使整次回滚失败。
INSERT INTO prd075_down_final_guard(issue_count)
SELECT CASE WHEN NOT EXISTS(
      SELECT permission_code,category,name,description,risk_level,10
      FROM prd075_expected_permissions
      WHERE permission_code NOT IN ('experiment.read','experiment.write')
      EXCEPT
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM rbac_permission_templates
    ) AND NOT EXISTS(
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM rbac_permission_templates
      EXCEPT
      SELECT permission_code,category,name,description,risk_level,10
      FROM prd075_expected_permissions
      WHERE permission_code NOT IN ('experiment.read','experiment.write')
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT role_code,permission_code,default_granted
      FROM prd075_expected_role_permissions
      WHERE permission_code NOT IN ('experiment.read','experiment.write')
      EXCEPT
      SELECT role_code,permission_code,default_granted
      FROM rbac_role_permission_templates
    ) AND NOT EXISTS(
      SELECT role_code,permission_code,default_granted
      FROM rbac_role_permission_templates
      EXCEPT
      SELECT role_code,permission_code,default_granted
      FROM prd075_expected_role_permissions
      WHERE permission_code NOT IN ('experiment.read','experiment.write')
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT tenant.tenant_id,expected.permission_code,expected.category,expected.name,
        expected.description,expected.risk_level,10,'active'
      FROM tenants tenant CROSS JOIN prd075_expected_permissions expected
      WHERE expected.permission_code NOT IN ('experiment.read','experiment.write')
      EXCEPT
      SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
      FROM permissions
    ) AND NOT EXISTS(
      SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
      FROM permissions
      EXCEPT
      SELECT tenant.tenant_id,expected.permission_code,expected.category,expected.name,
        expected.description,expected.risk_level,10,'active'
      FROM tenants tenant CROSS JOIN prd075_expected_permissions expected
      WHERE expected.permission_code NOT IN ('experiment.read','experiment.write')
    ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'model_experiments','gateway_request_scope_claims','experiment_assignments',
      'experiment_request_executions','experiment_request_outcomes',
      'experiment_usage_settlements','experiment_feedback_attributions',
      'experiment_results','experiment_events','experiment_event_outbox',
      'experiment_scheduler_tenants'))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd075')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd075_down_final_guard;
DROP TABLE prd075_expected_role_permissions;
DROP TABLE prd075_expected_permissions;
COMMIT;
PRAGMA foreign_keys = ON;
PRAGMA legacy_alter_table = OFF;
