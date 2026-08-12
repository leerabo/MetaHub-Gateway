-- PRD-077 正向迁移：租户隔离的可选 只读管理助手、结算事实与恢复型 outbox。
-- 迁移只保存不可逆摘要、密文和安全财务证据；Prompt、Response、Provider Key、Token
-- 与 Credential 明文不得进入任何表、trigger 错误或验收证据。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 冻结 revision-13 的完整权限目录；preflight 排除本迁移新增权限并投影为 revision 12，
-- final guard 则按 revision 13 全量复核。期望值只来自 checksum-locked 迁移源码，
-- 不从待验证数据库反向生成。
DROP TABLE IF EXISTS temp.prd077_expected_permissions;
CREATE TEMP TABLE prd077_expected_permissions(
  permission_code TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=13)
) WITHOUT ROWID;
-- 写入冻结的 revision-13 权限事实，供迁移前后目录完整性校验使用。
INSERT INTO prd077_expected_permissions(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('console.basic.read','console','基础控制台','访问概览、API 文档、系统状态和迁移兼容页面。','read',13),
  ('dashboard.read','console','读取概览','读取当前租户的聚合运行概览，不包含正式账单明细。','read',13),
  ('tenant.settings.read','tenant','读取租户设置','读取当前租户的脱敏基础配置。','read',13),
  ('user.manage','tenant','管理用户','创建租户用户并分配固定角色。','critical',13),
  ('rbac.matrix.manage','tenant','管理权限矩阵','读取并调整固定角色的可授权权限集合。','critical',13),
  ('environment.read','environment','读取环境','读取当前租户的环境边界和状态。','read',13),
  ('environment.write','environment','管理环境','创建、启停和维护当前租户环境。','write',13),
  ('project.read','project','读取项目','读取项目、应用及其当前租户归属。','read',13),
  ('project.write','project','管理项目','创建和维护项目、应用及资源绑定。','write',13),
  ('virtual_key.read','key','读取 Virtual Key','读取脱敏 Virtual Key 元数据。','read',13),
  ('virtual_key.write','key','管理 Virtual Key','创建、更新和撤销当前租户 Virtual Key。','critical',13),
  ('provider.read','provider','读取 Provider','读取 Provider 与 KeyPool 的脱敏配置。','read',13),
  ('provider.write','provider','管理 Provider','创建、更新和测试 Provider 与 KeyPool。','critical',13),
  ('provider.health.read','provider','读取 Provider 健康','读取上游健康状态和归一化错误摘要。','read',13),
  ('provider.health.probe','provider','探测 Provider','主动触发当前租户 Provider 健康探测。','write',13),
  ('provider_error_map.read','provider','读取错误映射','读取 Provider 错误归一规则。','read',13),
  ('provider_error_map.write','provider','管理错误映射','创建和更新 Provider 错误归一规则。','critical',13),
  ('model.read','model','读取模型','读取当前租户模型目录。','read',13),
  ('model.write','model','管理模型','创建和更新当前租户模型目录。','write',13),
  ('model_price.read','model','读取价格','读取模型价格时间线和版本。','read',13),
  ('model_price.write','model','管理价格','创建不可变模型价格计划。','critical',13),
  ('routing.read','routing','读取路由','读取别名、回退、路由和缓存亲和配置。','read',13),
  ('routing.write','routing','管理路由','创建和更新别名、回退及路由策略。','critical',13),
  ('config.transfer.read','migration','导出配置','读取并导出当前租户的严格白名单配置。','read',13),
  ('config.transfer.write','migration','导入配置','以 create-only 方式批量导入配置并生成全新 Virtual Key。','critical',13),
  ('model_quality.read','routing','读取模型质量','读取模型质量、成功率和延迟聚合。','read',13),
  ('model_quality.write','routing','管理模型质量','维护模型人工质量评分。','write',13),
  ('policy.read','security','读取策略','读取治理策略和版本摘要。','read',13),
  ('policy.write','security','管理策略','创建和更新治理策略。','critical',13),
  ('policy.simulate','security','模拟策略','对脱敏输入执行策略 Dry-Run。','write',13),
  ('policy.publish','security','发布策略','发布或回滚策略快照。','critical',13),
  ('dlp.read','security','读取 DLP','读取 DLP 规则的脱敏元数据。','read',13),
  ('dlp.write','security','管理 DLP','创建和更新 DLP 规则。','critical',13),
  ('request_log.read','observability','读取调用日志','读取当前租户调用日志、Trace 和保存筛选。','read',13),
  ('request_explanation.read','observability','读取请求解释','读取鉴权、路由、成本和安全解释事实。','read',13),
  ('feedback.use','observability','使用反馈','提交请求反馈并读取租户反馈汇总。','write',13),
  ('billing.usage.read','billing','读取用量','读取全租户用量、Token 和成本明细。','read',13),
  ('billing.usage.export','billing','导出用量','导出当前租户用量和成本账单。','critical',13),
  ('billing.finops.read','billing','读取正式账本','读取 append-only 正式账本和调整记录。','read',13),
  ('billing.finops.write','billing','调整正式账本','创建财务调整和冲正。','critical',13),
  ('billing.budget.read','billing','读取预算','读取预算规则和执行状态。','read',13),
  ('billing.budget.write','billing','管理预算','创建和更新预算规则。','critical',13),
  ('billing.anomaly.read','billing','读取成本异常','读取成本异常和检测器状态。','read',13),
  ('billing.anomaly.write','billing','处置成本异常','确认、忽略异常并维护临时限流。','critical',13),
  ('audit.read','audit','读取审计','读取当前租户不可变管理审计事实。','read',13),
  ('audit.export','audit','导出审计','导出不可变审计快照并生成复核哈希。','critical',13),
  ('webhook.manage','integration','管理 Webhook','管理 Endpoint、死信、重放和归档。','critical',13),
  ('developer_portal.read','developer','读取开发者门户','读取快速开始、SDK 示例和迁移指南。','read',13),
  ('debug_console.use','developer','使用 API 调试台','使用内置 OpenAI-compatible API 调试台。','write',13),
  ('chat.use','chat','使用企业 Chat','使用受 Gateway 全链路治理且不长期保存正文的企业 Chat。','write',13),
  ('prompt_template.read','prompt_template','读取 Prompt 模板','读取企业 Prompt 模板元数据、版本历史和授权后的模板定义。','read',13),
  ('prompt_template.write','prompt_template','管理 Prompt 模板','创建、修改、启停企业 Prompt 模板并发布不可变版本。','critical',13),
  ('prompt_template.use','prompt_template','使用 Prompt 模板','在企业 Chat 中选择并调用当前已发布的 Prompt 模板版本。','write',13),
  ('policy_template.read','security','读取策略模板','读取当前租户内置策略模板、适配状态和安全影响预览。','read',13),
  ('policy_template.apply','security','应用策略模板','应用服务端冻结的策略模板并原子发布策略快照。','critical',13),
  ('intent_risk.classify','security','分类敏感意图','对一次性文本执行本地敏感意图分类，不保存输入正文或命中片段。','write',13),
  ('session_risk.read','security','读取会话风险','读取当前租户高危会话、冻结状态和不可变处置摘要。','read',13),
  ('session_risk.manage','security','处置会话风险','冻结或释放已识别会话及其关联 Virtual Key 风险覆盖层。','critical',13),
  ('file.read','file','读取文件','读取当前租户文件处理状态、抽取文本和安全摘要。','read',13),
  ('file.write','file','上传文件','上传并解析当前租户的受支持文件。','write',13),
  ('file.security.scan','file','扫描文件 Prompt Injection','对当前租户文件执行隐藏 Prompt Injection 检测并写入不可变安全报告。','write',13),
  ('batch.read','batch','读取批量任务','读取当前租户批量任务、任务状态、成本摘要和授权后的结果。','read',13),
  ('batch.write','batch','管理批量任务','创建、取消和执行当前租户受治理的异步批量任务。','write',13),
  ('experiment.read','experiment','读取模型实验','读取当前租户模型 A/B 实验的安全配置、进度和权威结果。','read',13),
  ('experiment.write','experiment','创建模型实验','创建影响当前租户在线 Alias 流量的不可变模型 A/B 实验。','critical',13),
  ('system.config.read','system','读取系统配置','读取脱敏运行配置和系统状态。','read',13),
  ('response_cache.read','response_cache','读取响应缓存策略',
   '读取当前租户 Response Cache 的安全策略、状态和节省金额摘要。','read',13),
  ('response_cache.write','response_cache','管理响应缓存策略',
   '创建或修改当前租户 Response Cache 的显式启用、TTL、收费和标签策略。','critical',13),
  ('response_cache.invalidate','response_cache','失效响应缓存',
   '按当前租户的受控选择器推进围栏并主动失效 Response Cache。','critical',13),
  ('admin_assistant.query','admin_assistant','使用只读管理助手',
   '查询当前租户的用量、错误、成本或策略安全摘要，不授予任何修改能力。','read',13);

-- 冻结六个固定角色的 188 条 revision-13 默认关系，连同 default_granted 一并校验。
DROP TABLE IF EXISTS temp.prd077_expected_role_permissions;
CREATE TEMP TABLE prd077_expected_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  default_granted INTEGER NOT NULL,
  PRIMARY KEY(role_code,permission_code)
) WITHOUT ROWID;
-- 写入六个固定角色的授权期望，防止迁移过程静默扩大或缩减默认权限。
INSERT INTO prd077_expected_role_permissions(
  role_code,permission_code,default_granted
) VALUES
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
  ('ops_admin','virtual_key.read',1),('ops_admin','virtual_key.write',1),('ops_admin','webhook.manage',1),
  ('admin','response_cache.read',1),('admin','response_cache.write',1),
  ('admin','response_cache.invalidate',1),('finance_admin','response_cache.read',1),
  ('finance_admin','response_cache.write',1),('finance_admin','response_cache.invalidate',1),
  ('admin','admin_assistant.query',1),('finance_admin','admin_assistant.query',1),
  ('security_admin','admin_assistant.query',1);

-- 冻结 0070 将移除并重建的 15 个 revision-12 RBAC trigger 原文；同名空实现、
-- owner 表漂移或 guard body 漂移都不得穿透 preflight。
-- 每次执行先清理本连接残留的临时期望表，避免复用连接时读取旧触发器基线。
DROP TABLE IF EXISTS temp.prd077_expected_rbac_triggers;
CREATE TEMP TABLE prd077_expected_rbac_triggers(
  trigger_name TEXT PRIMARY KEY,
  table_name TEXT NOT NULL,
  raw_sql TEXT NOT NULL
) WITHOUT ROWID;
-- 写入受校验的触发器原文，确保重建前的所有权表和防篡改逻辑均未漂移。
INSERT INTO prd077_expected_rbac_triggers(trigger_name,table_name,raw_sql)
VALUES
  ('trg_permissions_delete_prd059','permissions','CREATE TRIGGER trg_permissions_delete_prd059
BEFORE DELETE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trg_permissions_owner_insert_prd059','permissions','CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level
    AND template.catalog_revision=NEW.catalog_revision
) OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!=''active''
  OR EXISTS(SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trg_permissions_rowid_insert_prd059','permissions','CREATE TRIGGER trg_permissions_rowid_insert_prd059
AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trg_permissions_update_prd059','permissions','CREATE TRIGGER trg_permissions_update_prd059
BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trg_rbac_permission_templates_delete_prd059','rbac_permission_templates','CREATE TRIGGER trg_rbac_permission_templates_delete_prd059
BEFORE DELETE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trg_rbac_permission_templates_insert_prd059','rbac_permission_templates','CREATE TRIGGER trg_rbac_permission_templates_insert_prd059
BEFORE INSERT ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trg_rbac_permission_templates_update_prd059','rbac_permission_templates','CREATE TRIGGER trg_rbac_permission_templates_update_prd059
BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trg_rbac_role_templates_delete_prd059','rbac_role_permission_templates','CREATE TRIGGER trg_rbac_role_templates_delete_prd059
BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trg_rbac_role_templates_insert_prd059','rbac_role_permission_templates','CREATE TRIGGER trg_rbac_role_templates_insert_prd059
BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trg_rbac_role_templates_update_prd059','rbac_role_permission_templates','CREATE TRIGGER trg_rbac_role_templates_update_prd059
BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trg_role_permissions_delete_prd059','role_permissions','CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM roles role
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id
    AND role.code=''admin'')
BEGIN SELECT RAISE(ABORT,''rbac_role_permission_immutable''); END'),
  ('trg_role_permissions_owner_insert_prd059','role_permissions','CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template
    ON template.role_code=role.code
   AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.status=''active'' AND permission.permission_id=NEW.permission_id
    AND permission.status=''active''
) OR EXISTS(SELECT 1 FROM role_permissions current
  WHERE current.role_permission_id=NEW.role_permission_id
     OR (current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
         AND current.permission_id=NEW.permission_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,''rbac_role_permission_owner_mismatch''); END'),
  ('trg_roles_matrix_update_prd059','roles','CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code=''admin''
  OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code
  OR NEW.name!=OLD.name OR NEW.description!=OLD.description
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB ''*[^0-9a-f]*''
BEGIN SELECT RAISE(ABORT,''rbac_role_immutable''); END'),
  ('trg_roles_owner_insert_prd059','roles','CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN(
    ''admin'',''developer'',''viewer'',''finance_admin'',''security_admin'',''ops_admin''
  )
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN ''admin'' THEN ''cda333482b406b02032f50eba4ee765658b584152f4d9e31f71005b101cfdd01''
    WHEN ''developer'' THEN ''71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd''
    WHEN ''viewer'' THEN ''33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9''
    WHEN ''finance_admin'' THEN ''2348a956d71e8b5cf0f897700c8e91e62dca91070c58010c79330ae60b36fa76''
    WHEN ''security_admin'' THEN ''28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6''
    WHEN ''ops_admin'' THEN ''0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a''
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code=''admin'' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current
    WHERE current.role_id=NEW.role_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,''rbac_role_immutable''); END'),
  ('trg_tenants_rbac_initialize_prd059','tenants','CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT ''role_''||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN ''admin'' THEN ''Admin''
      WHEN ''developer'' THEN ''Developer'' WHEN ''viewer'' THEN ''Viewer''
      WHEN ''finance_admin'' THEN ''FinanceAdmin''
      WHEN ''security_admin'' THEN ''SecurityAdmin''
      WHEN ''ops_admin'' THEN ''OpsAdmin'' END,
    CASE catalog.role_code
      WHEN ''admin'' THEN ''租户最高管理员，拥有完整权限且矩阵不可修改。''
      WHEN ''developer'' THEN ''负责模型接入、Key、调试和请求排障，不读取正式账单。''
      WHEN ''viewer'' THEN ''仅查看基础概览和公开控制台信息。''
      WHEN ''finance_admin'' THEN ''负责用量、账本、预算、价格、成本异常和响应缓存。''
      WHEN ''security_admin'' THEN ''负责审计、DLP、策略、错误归一和安全事件。''
      WHEN ''ops_admin'' THEN ''负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。''
    END,
    ''active'',strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),
    strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),1,
    CASE catalog.role_code
      WHEN ''admin'' THEN ''cda333482b406b02032f50eba4ee765658b584152f4d9e31f71005b101cfdd01''
      WHEN ''developer'' THEN ''71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd''
      WHEN ''viewer'' THEN ''33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9''
      WHEN ''finance_admin'' THEN ''2348a956d71e8b5cf0f897700c8e91e62dca91070c58010c79330ae60b36fa76''
      WHEN ''security_admin'' THEN ''28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6''
      WHEN ''ops_admin'' THEN ''0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a''
    END,
    CASE WHEN catalog.role_code=''admin'' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT ''perm_''||lower(hex(randomblob(16))),NEW.tenant_id,
    template.permission_code,template.category,template.name,
    template.description,template.risk_level,12,''active'',
    strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),strftime(''%Y-%m-%dT%H:%M:%SZ'',''now'')
  FROM rbac_permission_templates template;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  )
  SELECT ''rperm_''||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),
    strftime(''%Y-%m-%dT%H:%M:%SZ'',''now'')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END');

-- revision_mask: 1=0069/revision-12，2=0070/revision-13，3=两端 canonical SQL 相同。
-- 摘要来自 checksum-locked fresh 数据库，不从待验证 schema 反向生成期望值。
DROP TABLE IF EXISTS temp.prd077_expected_schema;
CREATE TEMP TABLE prd077_expected_schema(
  revision_mask INTEGER NOT NULL CHECK(revision_mask IN(1,2,3)),
  schema_type TEXT NOT NULL CHECK(schema_type IN('table','index','trigger')),
  object_name TEXT NOT NULL,
  table_name TEXT NOT NULL,
  sql_sha256 TEXT CHECK(
    sql_sha256 IS NULL OR (
      length(sql_sha256)=64 AND sql_sha256 NOT GLOB '*[^0-9a-f]*'
    )
  ),
  PRIMARY KEY(revision_mask,schema_type,object_name)
) WITHOUT ROWID;
-- 写入各版本应存在的模式对象与摘要，供 up、down 和重放路径精确核验。
INSERT INTO prd077_expected_schema(
  revision_mask,schema_type,object_name,table_name,sql_sha256
) VALUES
  (2,'index','idx_assistant_audits_due_prd077','assistant_audits','5e2577e707030d27b9a2b0e71881b35bc1b048f802f9b14bf042ce6251e88149'),
  (2,'index','idx_assistant_audits_tenant_actor_time_prd077','assistant_audits','65e7e30f509ad72fa3cd02668ed0bff1b44224aa9a52e1780e649792a9f12b21'),
  (2,'index','idx_assistant_queries_tenant_actor_time_prd077','assistant_queries','b131d202348aa28c6221c4b2170eb9e00ce8c0ebfc40fc8a35632d89ef0362ef'),
  (2,'index','idx_assistant_queries_tenant_target_prd077','assistant_queries','554a688567a71a6bd3e8632e334773cda5002c0b7a5dc5cbd39b2c215deb4735'),
  (2,'index','idx_assistant_queries_tenant_time_prd077','assistant_queries','9fd71a7354e5f87bbf669e9907e47dcd6871d66ae1b83de10a59f7d53c54e852'),
  (2,'index','idx_audit_logs_admin_assistant_owner_prd077','audit_logs','8ab14faff625639d4980b742b888e3f47df79938eb8b1f07c57bce150b05e715'),
  (3,'index','idx_audit_logs_config_transfer_request_unique','audit_logs','92a2fcfed91edae03f9d6e9dcb263d304c0da684becaadd7b829c742b75d5451'),
  (3,'index','idx_audit_logs_dry_run_request_unique','audit_logs','0585c1afb60479918e86288bde474ffec0433c834040ac2b5293055947307e25'),
  (3,'index','idx_audit_logs_experiment_owner_unique','audit_logs','7e93da47997561facce71d4d441c431244a918537390eb067cb6f039a06aad09'),
  (3,'index','idx_audit_logs_finops_adjustment_unique','audit_logs','8f89585a3aeeb22d9ba86abf1a4fa20c57ff3c49a1b56383d627a74451e0e353'),
  (3,'index','idx_audit_logs_invitation_request_unique','audit_logs','75b71fe84e76980e300bb9798c40b00bd386630ad1d0deb16308a9212c2cd44b'),
  (3,'index','idx_audit_logs_model_quality_request_unique','audit_logs','2c7ab03278c0f6cb6fce1672b02ee76ec03fabe7a2a94722e01fc9ea7e0d14b6'),
  (3,'index','idx_audit_logs_policy_template_request_unique','audit_logs','24dbe88fecc29ed04ac46844b4917303d2299cfc3eee884f620def9b6642a43d'),
  (3,'index','idx_audit_logs_prompt_template_request_unique','audit_logs','774cca7cbb4f4c26c144580906c8bbf66b0da89fddcb7eeb6976204865d42724'),
  (3,'index','idx_audit_logs_response_cache_owner_prd076','audit_logs','c1580b9c9273ad5110d1fc09fcc0c47ae9e72c79353078044cec09dd6f49298b'),
  (3,'index','idx_audit_logs_role_permission_request_unique','audit_logs','aeeb3d8c4c54e5e2b8807163bb6272ea751a55265642182b4a6e917a3f5b0ce5'),
  (3,'index','idx_audit_logs_session_risk_action_unique_prd070','audit_logs','55b3eef1c1ccfbc7dfdb4cb15edb5622b04a189e0887346b59467ae676afbb35'),
  (3,'index','idx_audit_logs_tenant_action_time','audit_logs','2847c2ea0720eb48c9b0ac7f4c2cbe776be03886f5a5435a0c587eef3e88ef73'),
  (3,'index','idx_audit_logs_tenant_actor_time','audit_logs','c7a278f98822fa5ad34a15cc5056be11c6d1dc5109c9f6795914c0057902fe08'),
  (3,'index','idx_audit_logs_tenant_object_time','audit_logs','ed9215afcb076c58ccdabb396f27ef1175b544a87df55eb64ad9728e83c13103'),
  (3,'index','idx_audit_logs_tenant_request','audit_logs','8933d7d29db9b2149a627af78379b414840e8d8832c19d1ceaa31bb1bcb6e3c5'),
  (3,'index','idx_audit_logs_tenant_time','audit_logs','62594b2975d2c03b753e2d9efcb4e6ab4438571dd649f5737fae8cabb39f0a33'),
  (3,'index','idx_permissions_tenant_category_prd059','permissions','40c2b9f1bb6c4963956be433dddab0f2207afa48d64c7c2f2b2e2ff6ca9f4275'),
  (2,'index','sqlite_autoindex_assistant_audits_1','assistant_audits',NULL),
  (2,'index','sqlite_autoindex_assistant_audits_2','assistant_audits',NULL),
  (2,'index','sqlite_autoindex_assistant_audits_3','assistant_audits',NULL),
  (2,'index','sqlite_autoindex_assistant_audits_4','assistant_audits',NULL),
  (2,'index','sqlite_autoindex_assistant_audits_5','assistant_audits',NULL),
  (2,'index','sqlite_autoindex_assistant_audits_6','assistant_audits',NULL),
  (2,'index','sqlite_autoindex_assistant_audits_7','assistant_audits',NULL),
  (2,'index','sqlite_autoindex_assistant_queries_1','assistant_queries',NULL),
  (2,'index','sqlite_autoindex_assistant_queries_2','assistant_queries',NULL),
  (2,'index','sqlite_autoindex_assistant_queries_3','assistant_queries',NULL),
  (3,'index','sqlite_autoindex_audit_logs_1','audit_logs',NULL),
  (3,'index','sqlite_autoindex_audit_logs_2','audit_logs',NULL),
  (3,'index','sqlite_autoindex_permissions_1','permissions',NULL),
  (3,'index','sqlite_autoindex_permissions_2','permissions',NULL),
  (3,'index','sqlite_autoindex_permissions_3','permissions',NULL),
  (3,'index','sqlite_autoindex_rbac_permission_templates_1','rbac_permission_templates',NULL),
  (3,'index','sqlite_autoindex_rbac_role_permission_templates_1','rbac_role_permission_templates',NULL),
  (3,'index','uq_audit_logs_owner_prd068','audit_logs','fb3843d60143581573f1c321bcf4d0e05c4f8448d2b4e153b5e134007426bbac'),
  (2,'table','assistant_audits','assistant_audits','4b613323eebca2989637c98c1777b0839144cacdc10b0983ab91c18d29fd2141'),
  (2,'table','assistant_queries','assistant_queries','b2881b5ab9a208cea6c4d0ed9af3aa4f15f97fde2a43654672a1e6463541e264'),
  (1,'table','audit_logs','audit_logs','6a7304756bd0ffdfcc946f4d34c63aeb9f2f59bb0c44f11b2f339982329ff34c'),
  (2,'table','audit_logs','audit_logs','4323575690701b597552e314accdbb152a3850308cd5cfcf72557c4beed92a1d'),
  (1,'table','permissions','permissions','c260d5475e882bd15bf1d892b5b22beaccd494cfe438818b7a8b888d08ee7d8f'),
  (2,'table','permissions','permissions','981e79266de3e2ea8b7fa9fc90bebed934c3b0738d28100200cfaf8025c72b1e'),
  (1,'table','rbac_permission_templates','rbac_permission_templates','26f0b19059e5daf31922d6aa724a604d49173af12252fd531222b60fa48dc7ce'),
  (2,'table','rbac_permission_templates','rbac_permission_templates','e034010108f61eb24a3130a8beb3a09276d364114f4694a1f1f8414b93329a54'),
  (3,'table','rbac_role_permission_templates','rbac_role_permission_templates','11c5d3f0a00c2c12805a805fb34be46e35089d916b7392f16d5c9480a81ebcc6'),
  (2,'trigger','trg_assistant_audits_delete_prd077','assistant_audits','d3464874722e890c1070e3bdbb819c1335efe4bdf012388475114040f566e1d8'),
  (2,'trigger','trg_assistant_audits_owner_insert_prd077','assistant_audits','c66c15f04e66bd097694262f8f8a45351e80ee81883000eb460b03c076f92ec6'),
  (2,'trigger','trg_assistant_audits_rowid_insert_prd077','assistant_audits','2c9eee8cd9a81a188af95076537d58f636635691751a6a1f8bd9501582ba35ce'),
  (2,'trigger','trg_assistant_audits_update_prd077','assistant_audits','95e8187b75d5b0166a6650aba91df78bd9e70f372287363adaac1cfa6ba61517'),
  (2,'trigger','trg_assistant_queries_delete_prd077','assistant_queries','63a629134ca0cb2ac8d24b4fcdf62863e090b272af6e4ca8f49527a53733b411'),
  (2,'trigger','trg_assistant_queries_owner_insert_prd077','assistant_queries','67d180365fbe4e486b13b47a5f347944498704ce51105ab87b155475114fbaae'),
  (2,'trigger','trg_assistant_queries_rowid_insert_prd077','assistant_queries','3f73af253c811fb83dc0c8480db86def5aa64b54d55859a0d9d9531f033a5aee'),
  (2,'trigger','trg_assistant_queries_update_prd077','assistant_queries','ed42722d503d02bce87314195a0ed16058d4b128e65093a7e0f824652de9fafd'),
  (2,'trigger','trg_audit_logs_admin_assistant_owner_insert_prd077','audit_logs','20108804785c590287059a56d47369c9dd3db03eefda32c2457c266b52f4ab1c'),
  (3,'trigger','trg_audit_logs_export_sequence_insert_prd058','audit_logs','ed54d3990d7e4a70f1ff88fe9b922a5413b2db53e67d3814d18dd108d57bed62'),
  (3,'trigger','trg_audit_logs_immutable_delete','audit_logs','d3315515e128df9429cdd13ebb5f1abc22ffdc31e6c5b178e1b687f5a323a156'),
  (3,'trigger','trg_audit_logs_immutable_update','audit_logs','9ffc759c0c4f364e472084b54ca15b19dcc148f4c002225997ac5941efbd83b4'),
  (3,'trigger','trg_audit_logs_owner_insert','audit_logs','54c33c11b615c321581986478b505ed237a38bcb3e266bda29f35c5cb9812d0e'),
  (3,'trigger','trg_audit_logs_policy_template_owner_insert_prd068','audit_logs','8146509b99183a1ec2fd76097328a815d81149b8334e54bdda26c053345c5e35'),
  (3,'trigger','trg_audit_logs_prd050_conflict_insert','audit_logs','0279769ebe6c15f822939567bea763bd6272054bf1c55433b9ed19172b128229'),
  (3,'trigger','trg_audit_logs_prd050_rowid_insert','audit_logs','c5019343535f043eabb2330c5551563bc16968c35576a2f2726d4c1bf18182f3'),
  (3,'trigger','trg_audit_logs_response_cache_owner_insert_prd076','audit_logs','785139d8bf8758007699f5159aeeb31633d119f7b28750b10b2452e8f8ebb5d0'),
  (3,'trigger','trg_audit_logs_session_risk_owner_insert_prd070','audit_logs','d82f771edb42a99281e9ad85a4481cd271aeda1a444d0fe32d7bbbc325ba6c06'),
  (3,'trigger','trg_permissions_delete_prd059','permissions','7a18aba7d62bfae1e4231c3acf73ae8be3b5e28687f28a72e803e16d059354b7'),
  (3,'trigger','trg_permissions_owner_insert_prd059','permissions','c3ebb14a1bd6d7595fc05ca1d0282444c49e63fa30c73f3b10721a74e8f05cae'),
  (3,'trigger','trg_permissions_rowid_insert_prd059','permissions','d2f2d5c3d2acc68c347ba3a5877ac964a91495789f5b5ee6d99ab4a05ec9fb42'),
  (3,'trigger','trg_permissions_update_prd059','permissions','e70b9b3dbeef0a5e00f9f657244566bd3f7a09f6e738da6dd4e7e675d53da450'),
  (3,'trigger','trg_rbac_permission_templates_delete_prd059','rbac_permission_templates','09c58f75002fd2dc89c04672d2b469507376ba4e1399927d649d5483965424d3'),
  (3,'trigger','trg_rbac_permission_templates_insert_prd059','rbac_permission_templates','38102842bbc400731dc6b6e4a9c4398b3a45df352c4fecb5e54cfc4f1103c2ea'),
  (3,'trigger','trg_rbac_permission_templates_update_prd059','rbac_permission_templates','3f346678d45c17a39510339c18906659db97c2a913e75dc2ef7c358681fc068d'),
  (3,'trigger','trg_rbac_role_templates_delete_prd059','rbac_role_permission_templates','333b9f531685c0e4ea16784fcf9f0d4bed4d8f2b933b9fd8ce226dec60581d43'),
  (3,'trigger','trg_rbac_role_templates_insert_prd059','rbac_role_permission_templates','df247f1932d4a2dc6aa0dce82e071bfcbd3137bff366a18ba10350a55a8f3f2b'),
  (3,'trigger','trg_rbac_role_templates_update_prd059','rbac_role_permission_templates','dcd36e60d0d7da76364273fbf91249393dcaf4e1d09732da724403dad8a7ba7d'),
  (3,'trigger','trg_role_permissions_delete_prd059','role_permissions','445679ca275b25010c8174e05e45bb7bdff38e32f5e80ec494030b2e57518e3c'),
  (3,'trigger','trg_role_permissions_owner_insert_prd059','role_permissions','a2c9a344510165be31da5d003743f1ed6b0bd5629491a634b99e35091cb9d34f'),
  (3,'trigger','trg_roles_matrix_update_prd059','roles','96b37be8e83b22f59def5f7e38195fdf18285064f1805472ece7f3170aa3adac'),
  (1,'trigger','trg_roles_owner_insert_prd059','roles','09ac9085dbdb7c3aec6ac8783802fcb01a4ad3528907e4a4719e939c6e73c44b'),
  (2,'trigger','trg_roles_owner_insert_prd059','roles','cbfb7785701b1af88d83836bff6170c271ea9f898e2263e7810ea1ed18221492'),
  (1,'trigger','trg_tenants_rbac_initialize_prd059','tenants','2bf7615e035b6f4d5b2519a93f0ddeac59aa421ae6dab51382868541ab3ad927'),
  (2,'trigger','trg_tenants_rbac_initialize_prd059','tenants','9b10bb76497d60ba13bda10e1adcb0b1060c5cbbe3a4a105f349ce4749394cd4');

DROP VIEW IF EXISTS temp.prd077_actual_schema;
CREATE TEMP VIEW prd077_actual_schema AS
SELECT type AS schema_type,name AS object_name,tbl_name AS table_name,
       metahub_sha256(sql) AS sql_sha256
FROM main.sqlite_schema
WHERE name='audit_logs' OR tbl_name='audit_logs'
   OR name IN(
     'rbac_permission_templates','rbac_role_permission_templates','permissions',
     'assistant_queries','assistant_audits'
   )
   OR tbl_name IN(
     'rbac_permission_templates','rbac_role_permission_templates','permissions',
     'assistant_queries','assistant_audits'
   )
   OR name IN(
     'trg_tenants_rbac_initialize_prd059','trg_roles_owner_insert_prd059',
     'trg_roles_matrix_update_prd059','trg_role_permissions_owner_insert_prd059',
     'trg_role_permissions_delete_prd059',
     'trg_rbac_permission_templates_insert_prd059',
     'trg_rbac_permission_templates_update_prd059',
     'trg_rbac_permission_templates_delete_prd059',
     'trg_rbac_role_templates_insert_prd059',
     'trg_rbac_role_templates_update_prd059',
     'trg_rbac_role_templates_delete_prd059','trg_permissions_owner_insert_prd059',
     'trg_permissions_update_prd059','trg_permissions_delete_prd059',
     'trg_permissions_rowid_insert_prd059'
   );

-- 首个持久 DDL 前冻结 revision-12 基线。目录或租户投影漂移时必须原子失败，不能在
-- 损坏矩阵上猜测补齐 只读管理助手 权限。
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd077_up_guard;
CREATE TEMP TABLE prd077_up_guard(
  issue_count INTEGER NOT NULL
    CONSTRAINT prd077_up_guard_ok CHECK(issue_count=0)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd077_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations WHERE status='applied')=69
      AND (SELECT COUNT(*) FROM schema_migrations
        WHERE version='0069' AND status='applied')=1
      AND (SELECT MAX(version) FROM schema_migrations WHERE status='applied')='0069'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT permission_code,category,name,description,risk_level,12
      FROM prd077_expected_permissions
      WHERE permission_code!='admin_assistant.query'
      EXCEPT
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM rbac_permission_templates
    ) AND NOT EXISTS(
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM rbac_permission_templates
      EXCEPT
      SELECT permission_code,category,name,description,risk_level,12
      FROM prd077_expected_permissions
      WHERE permission_code!='admin_assistant.query'
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT role_code,permission_code,default_granted
      FROM prd077_expected_role_permissions
      WHERE permission_code!='admin_assistant.query'
      EXCEPT
      SELECT role_code,permission_code,default_granted
      FROM rbac_role_permission_templates
    ) AND NOT EXISTS(
      SELECT role_code,permission_code,default_granted
      FROM rbac_role_permission_templates
      EXCEPT
      SELECT role_code,permission_code,default_granted
      FROM prd077_expected_role_permissions
      WHERE permission_code!='admin_assistant.query'
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT tenant.tenant_id,expected.permission_code,expected.category,expected.name,
        expected.description,expected.risk_level,12,'active'
      FROM tenants tenant CROSS JOIN prd077_expected_permissions expected
      WHERE expected.permission_code!='admin_assistant.query'
      EXCEPT
      SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
      FROM permissions
    ) AND NOT EXISTS(
      SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
      FROM permissions
      EXCEPT
      SELECT tenant.tenant_id,expected.permission_code,expected.category,expected.name,
        expected.description,expected.risk_level,12,'active'
      FROM tenants tenant CROSS JOIN prd077_expected_permissions expected
      WHERE expected.permission_code!='admin_assistant.query'
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT trigger_name,table_name,raw_sql
      FROM prd077_expected_rbac_triggers
      EXCEPT
      SELECT name,tbl_name,sql FROM sqlite_schema
      WHERE type='trigger'
        AND name IN (SELECT trigger_name FROM prd077_expected_rbac_triggers)
    ) AND NOT EXISTS(
      SELECT name,tbl_name,sql FROM sqlite_schema
      WHERE type='trigger'
        AND name IN (SELECT trigger_name FROM prd077_expected_rbac_triggers)
      EXCEPT
      SELECT trigger_name,table_name,raw_sql
      FROM prd077_expected_rbac_triggers
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT schema_type,object_name,table_name,sql_sha256
      FROM prd077_expected_schema WHERE revision_mask IN(1,3)
      EXCEPT
      SELECT schema_type,object_name,table_name,sql_sha256
      FROM prd077_actual_schema
    ) AND NOT EXISTS(
      SELECT schema_type,object_name,table_name,sql_sha256
      FROM prd077_actual_schema
      EXCEPT
      SELECT schema_type,object_name,table_name,sql_sha256
      FROM prd077_expected_schema WHERE revision_mask IN(1,3)
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM roles role
      WHERE role.code='admin'
        AND role.matrix_sha256!='cda333482b406b02032f50eba4ee765658b584152f4d9e31f71005b101cfdd01'
    ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'rbac_permission_templates_prd077_new','permissions_prd077_new',
      'usage_ledger_prd077_new','finops_ledger_entries_prd077_old',
      'audit_logs_prd077_new',
      'output_safety_scans_prd077_new','output_safety_events_prd077_new'
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd077_up_guard;

-- 只扩展仍保持 revision-12 默认矩阵的角色。finance_admin 的定制矩阵不被迁移覆盖；
-- admin 为不可变完整矩阵，基线漂移已由 preflight 阻断。
CREATE TEMP TABLE prd077_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','finance_admin','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'cda333482b406b02032f50eba4ee765658b584152f4d9e31f71005b101cfdd01'
    WHEN 'finance_admin' THEN '2348a956d71e8b5cf0f897700c8e91e62dca91070c58010c79330ae60b36fa76'
    WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
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

CREATE TABLE rbac_permission_templates_prd077_new(
  permission_code TEXT PRIMARY KEY CHECK(
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=13)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO rbac_permission_templates_prd077_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,13
FROM rbac_permission_templates;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO rbac_permission_templates_prd077_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('admin_assistant.query','admin_assistant','使用只读管理助手',
   '查询当前租户的用量、错误、成本或策略安全摘要，不授予任何修改能力。','read',13);
DROP TABLE rbac_permission_templates;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE rbac_permission_templates_prd077_new RENAME TO rbac_permission_templates;

-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','admin_assistant.query',1),
  ('finance_admin','admin_assistant.query',1),
  ('security_admin','admin_assistant.query',1);

CREATE TABLE permissions_prd077_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=13),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO permissions_prd077_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       13,status,created_at,updated_at
FROM permissions;
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO permissions_prd077_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       13,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code IN(
    'admin_assistant.query'
  );
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE permissions_prd077_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059
  ON permissions(tenant_id,category,code);

-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd077_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE permission.code IN(
  'admin_assistant.query'
);
-- 正向迁移更新冻结的版本或权限事实，并由终态检查确认没有越权扩散。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d'
      WHEN 'finance_admin' THEN '63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8'
      WHEN 'security_admin' THEN '38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656'
    END
WHERE role_id IN(SELECT role_id FROM prd077_roles_to_expand);

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
    WHEN 'admin' THEN '0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d'
    WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8'
    WHEN 'security_admin' THEN '38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656'
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
      WHEN 'admin' THEN '0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d'
      WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8'
      WHEN 'security_admin' THEN '38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656'
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
    template.description,template.risk_level,13,'active',
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

DROP TABLE prd077_roles_to_expand;

-- PRD-077 AuditLog preservation rebuild; only the action/object allowlist changes.
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS audit_logs_prd077_new;
CREATE TABLE audit_logs_prd077_new (
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
      'admin_assistant.queried'
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
      'assistant_query'
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
INSERT INTO audit_logs_prd077_new(rowid,audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action, object_type, object_id, object_name, before_summary_json, after_summary_json, changed_fields_json, client_ip, user_agent, request_id, trace_id, created_at)
SELECT rowid,audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action, object_type, object_id, object_name, before_summary_json, after_summary_json, changed_fields_json, client_ip, user_agent, request_id, trace_id, created_at FROM audit_logs;
-- 正向迁移只移除已验证的临时或旧结构，业务事实与依赖不得被静默丢弃。
DROP TABLE IF EXISTS temp.prd077_audit_copy_guard;
CREATE TEMP TABLE prd077_audit_copy_guard(issue_count INTEGER NOT NULL
  CONSTRAINT prd077_audit_copy_guard_ok CHECK(issue_count=0));
-- 正向迁移使用显式列写入迁移事实，避免列顺序或默认值漂移造成静默错位。
INSERT INTO prd077_audit_copy_guard(issue_count)
SELECT ABS((SELECT COUNT(*) FROM audit_logs)
  -(SELECT COUNT(*) FROM audit_logs_prd077_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd077_new))
  + (SELECT COUNT(*) FROM (SELECT rowid,* FROM audit_logs_prd077_new
      EXCEPT SELECT rowid,* FROM audit_logs));
DROP TABLE prd077_audit_copy_guard;
DROP TABLE audit_logs;
-- 正向迁移仅在显式复制与依赖校验完成后切换表名，禁止部分替换。
ALTER TABLE audit_logs_prd077_new RENAME TO audit_logs;
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

-- AssistantQuery 只保存有界枚举、计数和链路身份；问题、回答、裸 hash、Prompt、
-- Response、Provider Key 与 Token 均没有持久化列。
CREATE TABLE assistant_queries(
  assistant_query_id TEXT PRIMARY KEY CHECK(
    length(assistant_query_id)=36
    AND substr(assistant_query_id,1,4)='asq_'
    AND substr(assistant_query_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_session_id TEXT NOT NULL,
  query_type TEXT NOT NULL CHECK(
    query_type IN(
      'usage_statistics','error_explanation','cost_advice',
      'policy_explanation','unknown'
    )
  ),
  target_request_id TEXT CHECK(
    target_request_id IS NULL OR (
      length(target_request_id) BETWEEN 5 AND 160
      AND substr(target_request_id,1,4)='req_'
      AND instr(target_request_id,char(0))=0
    )
  ),
  window_started_at TEXT CHECK(
    window_started_at IS NULL OR unixepoch(window_started_at) IS NOT NULL
  ),
  window_ended_at TEXT CHECK(
    window_ended_at IS NULL OR unixepoch(window_ended_at) IS NOT NULL
  ),
  outcome TEXT NOT NULL CHECK(
    outcome IN(
      'answered','read_only_blocked','unsupported','ambiguous',
      'permission_denied','rate_limited','failed'
    )
  ),
  failure_code TEXT,
  claim_count INTEGER NOT NULL CHECK(
    typeof(claim_count)='integer' AND claim_count BETWEEN 0 AND 1000
  ),
  source_count INTEGER NOT NULL CHECK(
    typeof(source_count)='integer' AND source_count BETWEEN 0 AND 100
  ),
  redaction_count INTEGER NOT NULL CHECK(
    typeof(redaction_count)='integer' AND redaction_count BETWEEN 0 AND 100
  ),
  query_duration_ms INTEGER NOT NULL CHECK(
    typeof(query_duration_ms)='integer' AND query_duration_ms BETWEEN 0 AND 30000
  ),
  operation_request_id TEXT NOT NULL CHECK(
    length(operation_request_id) BETWEEN 5 AND 160
    AND substr(operation_request_id,1,4)='req_'
    AND instr(operation_request_id,char(0))=0
  ),
  operation_trace_id TEXT NOT NULL CHECK(
    length(operation_trace_id) BETWEEN 2 AND 160
    AND instr(operation_trace_id,char(0))=0
  ),
  execution_mode TEXT NOT NULL CHECK(execution_mode='deterministic_read_model_v1'),
  model_call_count INTEGER NOT NULL CHECK(
    typeof(model_call_count)='integer' AND model_call_count=0
  ),
  model_input_tokens INTEGER NOT NULL CHECK(
    typeof(model_input_tokens)='integer' AND model_input_tokens=0
  ),
  model_output_tokens INTEGER NOT NULL CHECK(
    typeof(model_output_tokens)='integer' AND model_output_tokens=0
  ),
  model_cost_cny_micros INTEGER NOT NULL CHECK(
    typeof(model_cost_cny_micros)='integer' AND model_cost_cny_micros=0
  ),
  model_cost_status TEXT NOT NULL CHECK(
    model_cost_status='not_applicable_local'
  ),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(tenant_id,assistant_query_id),
  UNIQUE(
    tenant_id,assistant_query_id,actor_user_id,actor_session_id
  ),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(actor_user_id) REFERENCES users(user_id),
  FOREIGN KEY(actor_session_id) REFERENCES sessions(session_id),
  CHECK(
    (window_started_at IS NULL AND window_ended_at IS NULL)
    OR (
      window_started_at IS NOT NULL AND window_ended_at IS NOT NULL
      AND unixepoch(window_started_at)<unixepoch(window_ended_at)
      AND unixepoch(window_ended_at)-unixepoch(window_started_at)
          BETWEEN 1 AND 2592000
    )
  ),
  CHECK(
    target_request_id IS NULL
    OR query_type IN('error_explanation','policy_explanation')
  ),
  CHECK(
    (outcome='answered' AND query_type!='unknown' AND failure_code IS NULL
      AND claim_count>=1 AND source_count>=1
      AND redaction_count=CASE query_type
        WHEN 'error_explanation' THEN 3 ELSE 2 END)
    OR
    (outcome!='answered' AND claim_count=0 AND source_count=0
      AND redaction_count=0 AND failure_code=CASE outcome
        WHEN 'read_only_blocked' THEN 'admin_assistant.read_only_violation'
        WHEN 'unsupported' THEN 'admin_assistant.query_unsupported'
        WHEN 'ambiguous' THEN 'admin_assistant.query_ambiguous'
        WHEN 'permission_denied' THEN 'auth.permission_denied'
        WHEN 'rate_limited' THEN 'admin_assistant.rate_limited'
        WHEN 'failed' THEN failure_code
      END
      AND (
        outcome!='failed'
        OR failure_code IN(
          'admin_assistant.request_invalid',
          'admin_assistant.source_not_found',
          'admin_assistant.source_unavailable',
          'admin_assistant.answer_invalid'
        )
      )
    )
  )
);
CREATE INDEX idx_assistant_queries_tenant_time_prd077
  ON assistant_queries(tenant_id,created_at DESC,assistant_query_id DESC);
CREATE INDEX idx_assistant_queries_tenant_actor_time_prd077
  ON assistant_queries(
    tenant_id,actor_user_id,created_at DESC,assistant_query_id DESC
  );
CREATE INDEX idx_assistant_queries_tenant_target_prd077
  ON assistant_queries(tenant_id,target_request_id)
  WHERE target_request_id IS NOT NULL;
CREATE TRIGGER trg_assistant_queries_owner_insert_prd077
BEFORE INSERT ON assistant_queries FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM users actor
  JOIN sessions session
    ON session.tenant_id=actor.tenant_id
   AND session.user_id=actor.user_id
  WHERE actor.tenant_id=NEW.tenant_id
    AND actor.user_id=NEW.actor_user_id
    AND actor.status='active'
    AND session.session_id=NEW.actor_session_id
    AND session.status='active'
)
OR EXISTS(
  SELECT 1 FROM assistant_queries current
  WHERE current.assistant_query_id=NEW.assistant_query_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'assistant_query_owner_mismatch'); END;
CREATE TRIGGER trg_assistant_queries_update_prd077
BEFORE UPDATE ON assistant_queries FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'assistant_query_immutable'); END;
CREATE TRIGGER trg_assistant_queries_delete_prd077
BEFORE DELETE ON assistant_queries FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'assistant_query_immutable'); END;
CREATE TRIGGER trg_assistant_queries_rowid_insert_prd077
AFTER INSERT ON assistant_queries FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'assistant_query_immutable'); END;

-- AssistantAudit 与 Query 一对一，并用延迟 AuditLog 外键允许同一事务先写专用审计、
-- 再写通用审计；source 与 permission 数组必须是排序、去重的 canonical JSON。
CREATE TABLE assistant_audits(
  assistant_audit_id TEXT PRIMARY KEY CHECK(
    length(assistant_audit_id)=36
    AND substr(assistant_audit_id,1,4)='asa_'
    AND substr(assistant_audit_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  assistant_event_id TEXT NOT NULL UNIQUE CHECK(
    length(assistant_event_id)=38
    AND substr(assistant_event_id,1,6)='asevt_'
    AND substr(assistant_event_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  assistant_query_id TEXT NOT NULL UNIQUE,
  actor_user_id TEXT NOT NULL,
  actor_session_id TEXT NOT NULL,
  audit_log_id TEXT NOT NULL UNIQUE CHECK(
    length(audit_log_id)=36
    AND substr(audit_log_id,1,4)='aud_'
    AND substr(audit_log_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  required_permissions_json TEXT NOT NULL CHECK(
    json_valid(required_permissions_json)
    AND json_type(required_permissions_json)='array'
    AND json(required_permissions_json)=required_permissions_json
  ),
  source_types_json TEXT NOT NULL CHECK(
    json_valid(source_types_json)
    AND json_type(source_types_json)='array'
    AND json(source_types_json)=source_types_json
  ),
  permission_revision INTEGER NOT NULL CHECK(
    typeof(permission_revision)='integer' AND permission_revision>=1
  ),
  authorization_fingerprint TEXT NOT NULL CHECK(
    length(authorization_fingerprint)=64
    AND authorization_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  answer_schema_version TEXT NOT NULL CHECK(
    answer_schema_version='admin-assistant-answer-v1'
  ),
  outcome TEXT NOT NULL CHECK(
    outcome IN(
      'answered','read_only_blocked','unsupported','ambiguous',
      'permission_denied','rate_limited','failed'
    )
  ),
  claim_count INTEGER NOT NULL CHECK(
    typeof(claim_count)='integer' AND claim_count BETWEEN 0 AND 1000
  ),
  source_count INTEGER NOT NULL CHECK(
    typeof(source_count)='integer' AND source_count BETWEEN 0 AND 100
  ),
  redaction_count INTEGER NOT NULL CHECK(
    typeof(redaction_count)='integer' AND redaction_count BETWEEN 0 AND 100
  ),
  query_duration_ms INTEGER NOT NULL CHECK(
    typeof(query_duration_ms)='integer' AND query_duration_ms BETWEEN 0 AND 30000
  ),
  operation_request_id TEXT NOT NULL,
  operation_trace_id TEXT NOT NULL,
  execution_mode TEXT NOT NULL CHECK(execution_mode='deterministic_read_model_v1'),
  model_call_count INTEGER NOT NULL CHECK(
    typeof(model_call_count)='integer' AND model_call_count=0
  ),
  model_input_tokens INTEGER NOT NULL CHECK(
    typeof(model_input_tokens)='integer' AND model_input_tokens=0
  ),
  model_output_tokens INTEGER NOT NULL CHECK(
    typeof(model_output_tokens)='integer' AND model_output_tokens=0
  ),
  model_cost_cny_micros INTEGER NOT NULL CHECK(
    typeof(model_cost_cny_micros)='integer' AND model_cost_cny_micros=0
  ),
  model_cost_status TEXT NOT NULL CHECK(
    model_cost_status='not_applicable_local'
  ),
  event_dispatch_status TEXT NOT NULL CHECK(
    event_dispatch_status IN('pending','processing','dispatched','failed')
  ),
  event_dispatch_attempts INTEGER NOT NULL CHECK(
    typeof(event_dispatch_attempts)='integer'
    AND event_dispatch_attempts BETWEEN 0 AND 10
  ),
  event_next_attempt_at TEXT CHECK(
    event_next_attempt_at IS NULL OR (
      length(event_next_attempt_at)=20
      AND strftime('%Y-%m-%dT%H:%M:%SZ',event_next_attempt_at)
          IS event_next_attempt_at
    )
  ),
  event_claim_token TEXT CHECK(
    event_claim_token IS NULL OR (
      length(event_claim_token)=64
      AND event_claim_token NOT GLOB '*[^0-9a-f]*'
    )
  ),
  event_lease_expires_at TEXT CHECK(
    event_lease_expires_at IS NULL
    OR (
      length(event_lease_expires_at)=20
      AND strftime('%Y-%m-%dT%H:%M:%SZ',event_lease_expires_at)
          IS event_lease_expires_at
    )
  ),
  event_lease_owner_id TEXT CHECK(
    event_lease_owner_id IS NULL OR (
      length(event_lease_owner_id) BETWEEN 1 AND 128
      AND instr(event_lease_owner_id,char(0))=0
    )
  ),
  event_last_error_code TEXT CHECK(
    event_last_error_code IS NULL
    OR event_last_error_code='admin_assistant.event_unavailable'
  ),
  event_dispatched_at TEXT CHECK(
    event_dispatched_at IS NULL OR (
      length(event_dispatched_at)=20
      AND strftime('%Y-%m-%dT%H:%M:%SZ',event_dispatched_at)
          IS event_dispatched_at
    )
  ),
  created_at TEXT NOT NULL CHECK(
    length(created_at)=20
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at) IS created_at
  ),
  updated_at TEXT NOT NULL CHECK(
    length(updated_at)=20
    AND strftime('%Y-%m-%dT%H:%M:%SZ',updated_at) IS updated_at
    AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  UNIQUE(tenant_id,assistant_audit_id),
  UNIQUE(tenant_id,assistant_event_id),
  UNIQUE(tenant_id,audit_log_id),
  FOREIGN KEY(
    tenant_id,assistant_query_id,actor_user_id,actor_session_id
  ) REFERENCES assistant_queries(
    tenant_id,assistant_query_id,actor_user_id,actor_session_id
  ),
  FOREIGN KEY(tenant_id,audit_log_id)
    REFERENCES audit_logs(tenant_id,audit_log_id)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK(
    (event_dispatch_status='pending'
      AND event_dispatch_attempts BETWEEN 0 AND 9
      AND event_next_attempt_at IS NOT NULL
      AND event_claim_token IS NULL AND event_lease_expires_at IS NULL
      AND event_lease_owner_id IS NULL
      AND (
        (event_dispatch_attempts=0 AND event_last_error_code IS NULL)
        OR
        (event_dispatch_attempts>=1
          AND event_last_error_code='admin_assistant.event_unavailable')
      )
      AND event_dispatched_at IS NULL)
    OR
    (event_dispatch_status='processing'
      AND event_dispatch_attempts BETWEEN 1 AND 10
      AND event_next_attempt_at IS NULL
      AND event_claim_token IS NOT NULL AND event_lease_expires_at IS NOT NULL
      AND event_lease_owner_id IS NOT NULL AND event_last_error_code IS NULL
      AND event_dispatched_at IS NULL)
    OR
    (event_dispatch_status='failed'
      AND event_dispatch_attempts=10
      AND event_next_attempt_at IS NULL
      AND event_claim_token IS NULL AND event_lease_expires_at IS NULL
      AND event_lease_owner_id IS NULL
      AND event_last_error_code='admin_assistant.event_unavailable'
      AND event_dispatched_at IS NULL)
    OR
    (event_dispatch_status='dispatched'
      AND event_dispatch_attempts BETWEEN 1 AND 10
      AND event_next_attempt_at IS NULL
      AND event_claim_token IS NULL AND event_lease_expires_at IS NULL
      AND event_lease_owner_id IS NULL AND event_last_error_code IS NULL
      AND event_dispatched_at IS NOT NULL)
  )
);
CREATE INDEX idx_assistant_audits_due_prd077
  ON assistant_audits(
    event_dispatch_status,event_next_attempt_at,event_lease_expires_at,
    tenant_id,assistant_event_id
  );
CREATE INDEX idx_assistant_audits_tenant_actor_time_prd077
  ON assistant_audits(
    tenant_id,actor_user_id,created_at DESC,assistant_audit_id DESC
  );
CREATE TRIGGER trg_assistant_audits_owner_insert_prd077
BEFORE INSERT ON assistant_audits FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM assistant_queries query
  WHERE query.tenant_id=NEW.tenant_id
    AND query.assistant_query_id=NEW.assistant_query_id
    AND query.actor_user_id=NEW.actor_user_id
    AND query.actor_session_id=NEW.actor_session_id
    AND query.outcome=NEW.outcome
    AND query.claim_count=NEW.claim_count
    AND query.source_count=NEW.source_count
    AND query.redaction_count=NEW.redaction_count
    AND query.query_duration_ms=NEW.query_duration_ms
    AND query.operation_request_id=NEW.operation_request_id
    AND query.operation_trace_id=NEW.operation_trace_id
    AND query.execution_mode=NEW.execution_mode
    AND query.model_call_count=NEW.model_call_count
    AND query.model_input_tokens=NEW.model_input_tokens
    AND query.model_output_tokens=NEW.model_output_tokens
    AND query.model_cost_cny_micros=NEW.model_cost_cny_micros
    AND query.model_cost_status=NEW.model_cost_status
    AND query.created_at=NEW.created_at
    AND NEW.required_permissions_json=CASE query.query_type
      WHEN 'usage_statistics' THEN
        '["admin_assistant.query","billing.usage.read","dashboard.read"]'
      WHEN 'error_explanation' THEN
        '["admin_assistant.query","request_log.read"]'
      WHEN 'cost_advice' THEN
        '["admin_assistant.query","billing.anomaly.read","billing.budget.read",' ||
        '"billing.finops.read","billing.usage.read","dashboard.read","model_price.read"]'
      WHEN 'policy_explanation' THEN CASE
        WHEN query.target_request_id IS NULL
          THEN '["admin_assistant.query","policy.read"]'
        ELSE
          '["admin_assistant.query","policy.read","request_explanation.read"]'
        END
      WHEN 'unknown' THEN '["admin_assistant.query"]'
    END
    AND (
      (query.outcome!='answered' AND NEW.source_types_json='[]')
      OR
      (
        query.outcome='answered'
        AND json_array_length(NEW.source_types_json)=NEW.source_count
        AND NOT EXISTS(
          SELECT 1 FROM json_each(NEW.source_types_json) source
          WHERE source.type!='text'
            OR source.value NOT IN(
              'overview','request_logs','request_explanations','usage',
              'finops_ledger','budgets','cost_anomalies','model_prices',
              'policies','policy_snapshots'
            )
            OR (
              query.query_type='usage_statistics'
              AND source.value NOT IN('overview','usage')
            )
            OR (
              query.query_type='error_explanation'
              AND source.value!='request_logs'
            )
            OR (
              query.query_type='cost_advice'
              AND source.value NOT IN(
                'overview','usage','finops_ledger','budgets',
                'cost_anomalies','model_prices'
              )
            )
            OR (
              query.query_type='policy_explanation'
              AND source.value NOT IN(
                'policies','policy_snapshots','request_explanations'
              )
            )
        )
      )
    )
)
OR EXISTS(
  SELECT 1 FROM json_each(NEW.required_permissions_json) left_value
  JOIN json_each(NEW.required_permissions_json) right_value
    ON right_value.key>left_value.key
   AND right_value.value<=left_value.value
)
OR EXISTS(
  SELECT 1 FROM json_each(NEW.source_types_json) left_value
  JOIN json_each(NEW.source_types_json) right_value
    ON right_value.key>left_value.key
   AND right_value.value<=left_value.value
)
OR EXISTS(
  SELECT 1 FROM assistant_audits current
  WHERE current.assistant_audit_id=NEW.assistant_audit_id
    OR current.assistant_event_id=NEW.assistant_event_id
    OR current.assistant_query_id=NEW.assistant_query_id
    OR current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'assistant_audit_owner_mismatch'); END;
CREATE TRIGGER trg_assistant_audits_update_prd077
BEFORE UPDATE ON assistant_audits FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.assistant_audit_id!=OLD.assistant_audit_id
  OR NEW.assistant_event_id!=OLD.assistant_event_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.assistant_query_id!=OLD.assistant_query_id
  OR NEW.actor_user_id!=OLD.actor_user_id
  OR NEW.actor_session_id!=OLD.actor_session_id
  OR NEW.audit_log_id!=OLD.audit_log_id
  OR NEW.required_permissions_json!=OLD.required_permissions_json
  OR NEW.source_types_json!=OLD.source_types_json
  OR NEW.permission_revision!=OLD.permission_revision
  OR NEW.authorization_fingerprint!=OLD.authorization_fingerprint
  OR NEW.answer_schema_version!=OLD.answer_schema_version
  OR NEW.outcome!=OLD.outcome
  OR NEW.claim_count!=OLD.claim_count
  OR NEW.source_count!=OLD.source_count
  OR NEW.redaction_count!=OLD.redaction_count
  OR NEW.query_duration_ms!=OLD.query_duration_ms
  OR NEW.operation_request_id!=OLD.operation_request_id
  OR NEW.operation_trace_id!=OLD.operation_trace_id
  OR NEW.execution_mode!=OLD.execution_mode
  OR NEW.model_call_count!=OLD.model_call_count
  OR NEW.model_input_tokens!=OLD.model_input_tokens
  OR NEW.model_output_tokens!=OLD.model_output_tokens
  OR NEW.model_cost_cny_micros!=OLD.model_cost_cny_micros
  OR NEW.model_cost_status!=OLD.model_cost_status
  OR NEW.created_at!=OLD.created_at
  OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
  OR NOT(
    (
      OLD.event_dispatch_status='pending'
      AND NEW.event_dispatch_status='processing'
      AND OLD.event_dispatch_attempts<10
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND unixepoch(OLD.event_next_attempt_at)<=unixepoch(NEW.updated_at)
      AND unixepoch(NEW.event_lease_expires_at)>unixepoch(NEW.updated_at)
    )
    OR
    (
      OLD.event_dispatch_status='processing'
      AND NEW.event_dispatch_status='processing'
      AND OLD.event_dispatch_attempts<10
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND NEW.event_claim_token!=OLD.event_claim_token
      AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at)
      AND unixepoch(NEW.event_lease_expires_at)>unixepoch(NEW.updated_at)
    )
    OR
    (
      OLD.event_dispatch_status='processing'
      AND NEW.event_dispatch_status='processing'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND NEW.event_next_attempt_at IS OLD.event_next_attempt_at
      AND NEW.event_claim_token=OLD.event_claim_token
      AND NEW.event_lease_owner_id=OLD.event_lease_owner_id
      AND NEW.event_last_error_code IS OLD.event_last_error_code
      AND NEW.event_dispatched_at IS OLD.event_dispatched_at
      AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at)
      AND unixepoch(OLD.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.event_lease_expires_at)>unixepoch(OLD.event_lease_expires_at)
    )
    OR
    (
      OLD.event_dispatch_status='processing'
      AND NEW.event_dispatch_status IN('pending','dispatched','failed')
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND unixepoch(OLD.event_lease_expires_at)>unixepoch(NEW.updated_at)
    )
    OR
    (
      OLD.event_dispatch_status='processing'
      AND OLD.event_dispatch_attempts=10
      AND NEW.event_dispatch_status='failed'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at)
    )
    OR
    (
      OLD.event_dispatch_status IN('pending','processing')
      AND NEW.event_dispatch_status='failed'
      AND NEW.event_dispatch_attempts=10
      AND (
        strftime('%Y-%m-%dT%H:%M:%SZ',OLD.updated_at) IS NOT OLD.updated_at
        OR (
          OLD.event_next_attempt_at IS NOT NULL
          AND strftime('%Y-%m-%dT%H:%M:%SZ',OLD.event_next_attempt_at)
              IS NOT OLD.event_next_attempt_at
        )
        OR (
          OLD.event_lease_expires_at IS NOT NULL
          AND strftime('%Y-%m-%dT%H:%M:%SZ',OLD.event_lease_expires_at)
              IS NOT OLD.event_lease_expires_at
        )
      )
    )
  )
BEGIN SELECT RAISE(ABORT,'assistant_audit_transition_invalid'); END;
CREATE TRIGGER trg_assistant_audits_delete_prd077
BEFORE DELETE ON assistant_audits FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'assistant_audit_immutable'); END;
CREATE TRIGGER trg_assistant_audits_rowid_insert_prd077
AFTER INSERT ON assistant_audits FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'assistant_audit_immutable'); END;



CREATE UNIQUE INDEX idx_audit_logs_admin_assistant_owner_prd077
  ON audit_logs(tenant_id,object_id)
  WHERE action='admin_assistant.queried';
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

-- 最终门禁同时固定 revision-13 投影、空 Query/Audit 一对一图、AuditLog allowlist
-- 以及所有 FK/SQLite 完整性；任何偏差都回滚整个 0070。
DROP TABLE IF EXISTS temp.prd077_up_final_guard;
CREATE TEMP TABLE prd077_up_final_guard(
  issue_count INTEGER NOT NULL
    CONSTRAINT prd077_up_final_guard_ok CHECK(issue_count=0)
);
-- 汇总 revision-13 投影、空助手事实图、审计约束、外键和完整性，任一偏差回滚整个 0070。
INSERT INTO prd077_up_final_guard(issue_count)
SELECT
  CASE WHEN NOT EXISTS(
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM prd077_expected_permissions
      EXCEPT
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM rbac_permission_templates
    ) AND NOT EXISTS(
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM rbac_permission_templates
      EXCEPT
      SELECT permission_code,category,name,description,risk_level,catalog_revision
      FROM prd077_expected_permissions
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT role_code,permission_code,default_granted
      FROM prd077_expected_role_permissions
      EXCEPT
      SELECT role_code,permission_code,default_granted
      FROM rbac_role_permission_templates
    ) AND NOT EXISTS(
      SELECT role_code,permission_code,default_granted
      FROM rbac_role_permission_templates
      EXCEPT
      SELECT role_code,permission_code,default_granted
      FROM prd077_expected_role_permissions
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT tenant.tenant_id,expected.permission_code,expected.category,expected.name,
        expected.description,expected.risk_level,expected.catalog_revision,'active'
      FROM tenants tenant CROSS JOIN prd077_expected_permissions expected
      EXCEPT
      SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
      FROM permissions
    ) AND NOT EXISTS(
      SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
      FROM permissions
      EXCEPT
      SELECT tenant.tenant_id,expected.permission_code,expected.category,expected.name,
        expected.description,expected.risk_level,expected.catalog_revision,'active'
      FROM tenants tenant CROSS JOIN prd077_expected_permissions expected
    ) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT schema_type,object_name,table_name,sql_sha256
      FROM prd077_expected_schema WHERE revision_mask IN(2,3)
      EXCEPT
      SELECT schema_type,object_name,table_name,sql_sha256
      FROM prd077_actual_schema
    ) AND NOT EXISTS(
      SELECT schema_type,object_name,table_name,sql_sha256
      FROM prd077_actual_schema
      EXCEPT
      SELECT schema_type,object_name,table_name,sql_sha256
      FROM prd077_expected_schema WHERE revision_mask IN(2,3)
    ) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table'
      AND name IN('assistant_queries','assistant_audits'))=2 THEN 0 ELSE 1 END
  + CASE WHEN instr(
      (SELECT sql FROM sqlite_schema
       WHERE type='table' AND name='audit_logs'),
      "'admin_assistant.queried'"
    )>0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE name IN(
        'trg_assistant_queries_owner_insert_prd077',
        'trg_assistant_queries_update_prd077',
        'trg_assistant_queries_delete_prd077',
        'trg_assistant_queries_rowid_insert_prd077',
        'trg_assistant_audits_owner_insert_prd077',
        'trg_assistant_audits_update_prd077',
        'trg_assistant_audits_delete_prd077',
        'trg_assistant_audits_rowid_insert_prd077',
        'trg_audit_logs_admin_assistant_owner_insert_prd077'
      ))=9 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT tenant_id,assistant_query_id FROM assistant_queries
      EXCEPT SELECT tenant_id,assistant_query_id FROM assistant_audits
    ) AND NOT EXISTS(
      SELECT tenant_id,assistant_query_id FROM assistant_audits
      EXCEPT SELECT tenant_id,assistant_query_id FROM assistant_queries
    ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd077_up_final_guard;
DROP VIEW prd077_actual_schema;
DROP TABLE prd077_expected_schema;
DROP TABLE prd077_expected_rbac_triggers;
DROP TABLE prd077_expected_role_permissions;
DROP TABLE prd077_expected_permissions;
COMMIT;
PRAGMA foreign_keys = ON;
