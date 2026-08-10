-- PRD-075 正向迁移：建立租户隔离的实验、分配、结果、调度和完成事件数据模型。
-- PRD-075: tenant-isolated online model A/B testing and quality evaluation.
-- Only bounded identifiers, counters, fingerprints, and safe summaries are persisted here.
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 冻结 revision 11 的完整权限目录；up 前投影为 revision 10，up 后按原值复核。
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

-- 冻结 checksum-locked fresh 0067 的 15 个 RBAC trigger 原始定义，拒绝同名空实现穿透。
DROP TABLE IF EXISTS temp.prd075_expected_rbac_triggers;
CREATE TEMP TABLE prd075_expected_rbac_triggers(
  schema_type TEXT NOT NULL,
  trigger_name TEXT PRIMARY KEY,
  table_name TEXT NOT NULL,
  raw_sql TEXT NOT NULL
) WITHOUT ROWID;
-- 原始 SQL 直接来自 0067 迁移结果，不从当前待迁移数据库反向构造期望值。
INSERT INTO prd075_expected_rbac_triggers(
  schema_type,trigger_name,table_name,raw_sql
) VALUES
  ('trigger','trg_permissions_delete_prd059','permissions','CREATE TRIGGER trg_permissions_delete_prd059
BEFORE DELETE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trigger','trg_permissions_owner_insert_prd059','permissions','CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level AND template.catalog_revision=NEW.catalog_revision
) OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!=''active''
  OR EXISTS(SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trigger','trg_permissions_rowid_insert_prd059','permissions','CREATE TRIGGER trg_permissions_rowid_insert_prd059
AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trigger','trg_permissions_update_prd059','permissions','CREATE TRIGGER trg_permissions_update_prd059
BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trigger','trg_rbac_permission_templates_delete_prd059','rbac_permission_templates','CREATE TRIGGER trg_rbac_permission_templates_delete_prd059
BEFORE DELETE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trigger','trg_rbac_permission_templates_insert_prd059','rbac_permission_templates','CREATE TRIGGER trg_rbac_permission_templates_insert_prd059
BEFORE INSERT ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trigger','trg_rbac_permission_templates_update_prd059','rbac_permission_templates','CREATE TRIGGER trg_rbac_permission_templates_update_prd059
BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trigger','trg_rbac_role_templates_delete_prd059','rbac_role_permission_templates','CREATE TRIGGER trg_rbac_role_templates_delete_prd059
BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trigger','trg_rbac_role_templates_insert_prd059','rbac_role_permission_templates','CREATE TRIGGER trg_rbac_role_templates_insert_prd059
BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trigger','trg_rbac_role_templates_update_prd059','rbac_role_permission_templates','CREATE TRIGGER trg_rbac_role_templates_update_prd059
BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_template_immutable''); END'),
  ('trigger','trg_role_permissions_delete_prd059','role_permissions','CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM roles role WHERE role.tenant_id=OLD.tenant_id
  AND role.role_id=OLD.role_id AND role.code=''admin'')
BEGIN SELECT RAISE(ABORT,''rbac_role_permission_immutable''); END'),
  ('trigger','trg_role_permissions_owner_insert_prd059','role_permissions','CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template
    ON template.role_code=role.code AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id AND role.status=''active''
    AND permission.permission_id=NEW.permission_id AND permission.status=''active''
) OR EXISTS(SELECT 1 FROM role_permissions current
  WHERE current.role_permission_id=NEW.role_permission_id
     OR (current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
         AND current.permission_id=NEW.permission_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,''rbac_role_permission_owner_mismatch''); END'),
  ('trigger','trg_roles_matrix_update_prd059','roles','CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code=''admin''
  OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB ''*[^0-9a-f]*''
BEGIN SELECT RAISE(ABORT,''rbac_role_immutable''); END'),
  ('trigger','trg_roles_owner_insert_prd059','roles','CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN (''admin'',''developer'',''viewer'',''finance_admin'',''security_admin'',''ops_admin'')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN ''admin'' THEN ''ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262''
    WHEN ''developer'' THEN ''89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30''
    WHEN ''viewer'' THEN ''33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9''
    WHEN ''finance_admin'' THEN ''96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114''
    WHEN ''security_admin'' THEN ''28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6''
    WHEN ''ops_admin'' THEN ''72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6''
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code=''admin'' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current WHERE current.role_id=NEW.role_id
    OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,''rbac_role_immutable''); END'),
  ('trigger','trg_tenants_rbac_initialize_prd059','tenants','CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  '||'INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT ''role_''||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN ''admin'' THEN ''Admin'' WHEN ''developer'' THEN ''Developer''
      WHEN ''viewer'' THEN ''Viewer'' WHEN ''finance_admin'' THEN ''FinanceAdmin''
      WHEN ''security_admin'' THEN ''SecurityAdmin'' WHEN ''ops_admin'' THEN ''OpsAdmin'' END,
    CASE catalog.role_code WHEN ''admin'' THEN ''租户最高管理员，拥有完整权限且矩阵不可修改。''
      WHEN ''developer'' THEN ''负责模型接入、Key、调试和请求排障，不读取正式账单。''
      WHEN ''viewer'' THEN ''仅查看基础概览和公开控制台信息。''
      WHEN ''finance_admin'' THEN ''负责用量、账本、预算、价格和成本异常。''
      WHEN ''security_admin'' THEN ''负责审计、DLP、策略、错误归一和安全事件。''
      WHEN ''ops_admin'' THEN ''负责 Provider、环境、项目、Key、模型、路由和批量任务运维。'' END,
    ''active'',strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),1,
    CASE catalog.role_code
      WHEN ''admin'' THEN ''ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262''
      WHEN ''developer'' THEN ''89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30''
      WHEN ''viewer'' THEN ''33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9''
      WHEN ''finance_admin'' THEN ''96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114''
      WHEN ''security_admin'' THEN ''28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6''
      WHEN ''ops_admin'' THEN ''72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6'' END,
    CASE WHEN catalog.role_code=''admin'' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  '||'INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  ) SELECT ''perm_''||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    10,''active'',strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),strftime(''%Y-%m-%dT%H:%M:%SZ'',''now'')
  FROM rbac_permission_templates template;
  '||'INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  ) SELECT ''rperm_''||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),
    strftime(''%Y-%m-%dT%H:%M:%SZ'',''now'')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission
    ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END');

-- B canonical 只覆盖 down 为中文门禁保留的 7 条注释或排版差异，其余 8 条严格复用 A。
DROP TABLE IF EXISTS temp.prd075_expected_rbac_trigger_down_overrides;
CREATE TEMP TABLE prd075_expected_rbac_trigger_down_overrides(
  schema_type TEXT NOT NULL,
  trigger_name TEXT PRIMARY KEY,
  table_name TEXT NOT NULL,
  raw_sql TEXT NOT NULL
) WITHOUT ROWID;
-- 这些原始 SQL 冻结自 0068 down 结果，不接受按 trigger 自由混搭 A/B。
INSERT INTO prd075_expected_rbac_trigger_down_overrides(
  schema_type,trigger_name,table_name,raw_sql
) VALUES
  ('trigger','trg_permissions_delete_prd059','permissions','CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trigger','trg_permissions_owner_insert_prd059','permissions','CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level AND template.catalog_revision=NEW.catalog_revision)
  OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!=''active''
  OR EXISTS(SELECT 1 FROM permissions current WHERE current.permission_id=NEW.permission_id
    OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trigger','trg_permissions_rowid_insert_prd059','permissions','CREATE TRIGGER trg_permissions_rowid_insert_prd059 AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trigger','trg_permissions_update_prd059','permissions','CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,''rbac_permission_immutable''); END'),
  ('trigger','trg_role_permissions_owner_insert_prd059','role_permissions','CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
    AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id AND role.status=''active''
    AND permission.permission_id=NEW.permission_id AND permission.status=''active'')
  OR EXISTS(SELECT 1 FROM role_permissions current
    WHERE current.role_permission_id=NEW.role_permission_id
      OR (current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
        AND current.permission_id=NEW.permission_id)
      OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,''rbac_role_permission_owner_mismatch''); END'),
  ('trigger','trg_roles_matrix_update_prd059','roles','CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code=''admin'' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name
  OR NEW.description!=OLD.description OR NEW.status!=OLD.status
  OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB ''*[^0-9a-f]*''
BEGIN SELECT RAISE(ABORT,''rbac_role_immutable''); END'),
  ('trigger','trg_tenants_rbac_initialize_prd059','tenants','CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 在回滚事务中处理 roles 的守卫或 revision 事实；异常会使整次回滚失败。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  ) SELECT ''role_''||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN ''admin'' THEN ''Admin'' WHEN ''developer'' THEN ''Developer''
      WHEN ''viewer'' THEN ''Viewer'' WHEN ''finance_admin'' THEN ''FinanceAdmin''
      WHEN ''security_admin'' THEN ''SecurityAdmin'' WHEN ''ops_admin'' THEN ''OpsAdmin'' END,
    CASE catalog.role_code WHEN ''admin'' THEN ''租户最高管理员，拥有完整权限且矩阵不可修改。''
      WHEN ''developer'' THEN ''负责模型接入、Key、调试和请求排障，不读取正式账单。''
      WHEN ''viewer'' THEN ''仅查看基础概览和公开控制台信息。''
      WHEN ''finance_admin'' THEN ''负责用量、账本、预算、价格和成本异常。''
      WHEN ''security_admin'' THEN ''负责审计、DLP、策略、错误归一和安全事件。''
      WHEN ''ops_admin'' THEN ''负责 Provider、环境、项目、Key、模型、路由和批量任务运维。'' END,
    ''active'',strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),1,
    CASE catalog.role_code
      WHEN ''admin'' THEN ''ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262''
      WHEN ''developer'' THEN ''89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30''
      WHEN ''viewer'' THEN ''33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9''
      WHEN ''finance_admin'' THEN ''96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114''
      WHEN ''security_admin'' THEN ''28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6''
      WHEN ''ops_admin'' THEN ''72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6'' END,
    CASE WHEN catalog.role_code=''admin'' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 在回滚事务中处理 permissions 的守卫或 revision 事实；异常会使整次回滚失败。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  ) SELECT ''perm_''||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    10,''active'',strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),strftime(''%Y-%m-%dT%H:%M:%SZ'',''now'')
  FROM rbac_permission_templates template;
  -- 在回滚事务中处理 role_permissions 的守卫或 revision 事实；异常会使整次回滚失败。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  ) SELECT ''rperm_''||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime(''%Y-%m-%dT%H:%M:%SZ'',''now''),
    strftime(''%Y-%m-%dT%H:%M:%SZ'',''now'')
  FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code WHERE role.tenant_id=NEW.tenant_id;
END');

-- 清理临时迁移守卫 temp.prd075_up_guard；该对象不承载租户业务数据。
DROP TABLE IF EXISTS temp.prd075_up_guard;
CREATE TEMP TABLE prd075_up_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd075_up_guard_ok CHECK(issue_count=0)
);
-- 迁移 prd075_up_guard 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
INSERT INTO prd075_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations WHERE status='applied')=67
      AND (SELECT COUNT(*) FROM schema_migrations
        WHERE version='0067' AND status='applied')=1
      AND (SELECT MAX(version) FROM schema_migrations WHERE status='applied')='0067'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
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
  + CASE WHEN (NOT EXISTS(
      SELECT schema_type,trigger_name,table_name,raw_sql
      FROM prd075_expected_rbac_triggers
      EXCEPT
      SELECT type,name,tbl_name,sql
      FROM sqlite_schema
      WHERE type='trigger'
        AND name IN (SELECT trigger_name FROM prd075_expected_rbac_triggers)
    ) AND NOT EXISTS(
      SELECT type,name,tbl_name,sql
      FROM sqlite_schema
      WHERE type='trigger'
        AND name IN (SELECT trigger_name FROM prd075_expected_rbac_triggers)
      EXCEPT
      SELECT schema_type,trigger_name,table_name,raw_sql
      FROM prd075_expected_rbac_triggers
    )) OR (NOT EXISTS(
      SELECT base.schema_type,base.trigger_name,base.table_name,
        COALESCE(down.raw_sql,base.raw_sql)
      FROM prd075_expected_rbac_triggers base
      LEFT JOIN prd075_expected_rbac_trigger_down_overrides down
        ON down.trigger_name=base.trigger_name
      EXCEPT
      SELECT type,name,tbl_name,sql
      FROM sqlite_schema
      WHERE type='trigger'
        AND name IN (SELECT trigger_name FROM prd075_expected_rbac_triggers)
    ) AND NOT EXISTS(
      SELECT type,name,tbl_name,sql
      FROM sqlite_schema
      WHERE type='trigger'
        AND name IN (SELECT trigger_name FROM prd075_expected_rbac_triggers)
      EXCEPT
      SELECT base.schema_type,base.trigger_name,base.table_name,
        COALESCE(down.raw_sql,base.raw_sql)
      FROM prd075_expected_rbac_triggers base
      LEFT JOIN prd075_expected_rbac_trigger_down_overrides down
        ON down.trigger_name=base.trigger_name
    )) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN (
      'model_experiments','gateway_request_scope_claims','experiment_assignments',
      'experiment_request_executions','experiment_request_outcomes',
      'experiment_usage_settlements','experiment_feedback_attributions',
      'experiment_results','experiment_events','experiment_event_outbox',
      'experiment_scheduler_tenants','rbac_permission_templates_prd075_new',
      'permissions_prd075_new'))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd075_up_guard;
-- 首个持久 DDL 前的 trigger 校验已完成，及时清理只读 TEMP canonical。
DROP TABLE prd075_expected_rbac_trigger_down_overrides;
DROP TABLE prd075_expected_rbac_triggers;

-- Only exact canonical revision-10 matrices receive the two new default grants.
CREATE TEMP TABLE prd075_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262'
    WHEN 'developer' THEN '89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30'
    WHEN 'ops_admin' THEN '72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6'
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

CREATE TABLE rbac_permission_templates_prd075_new(
  permission_code TEXT PRIMARY KEY CHECK(
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=11)
);
-- 迁移 rbac_permission_templates_prd075_new 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
INSERT INTO rbac_permission_templates_prd075_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,11
FROM rbac_permission_templates;
-- 迁移 rbac_permission_templates_prd075_new 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
INSERT INTO rbac_permission_templates_prd075_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('experiment.read','experiment','读取模型实验',
   '读取当前租户模型 A/B 实验的安全配置、进度和权威结果。','read',11),
  ('experiment.write','experiment','创建模型实验',
   '创建影响当前租户在线 Alias 流量的不可变模型 A/B 实验。','critical',11);
DROP TABLE rbac_permission_templates;
-- 切换 rbac_permission_templates_prd075_new 的迁移期表结构；前后均保持租户归属、外键和状态约束。
ALTER TABLE rbac_permission_templates_prd075_new RENAME TO rbac_permission_templates;

-- 迁移 rbac_role_permission_templates 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','experiment.read',1),('admin','experiment.write',1),
  ('developer','experiment.read',1),('developer','experiment.write',1),
  ('ops_admin','experiment.read',1),('ops_admin','experiment.write',1);

CREATE TABLE permissions_prd075_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=11),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 迁移 permissions_prd075_new 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
INSERT INTO permissions_prd075_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       11,status,created_at,updated_at
FROM permissions;
-- 迁移 permissions_prd075_new 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
INSERT INTO permissions_prd075_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
       template.category,template.name,template.description,template.risk_level,
       11,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
JOIN rbac_permission_templates template
  ON template.permission_code IN ('experiment.read','experiment.write');
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;
DROP TABLE permissions;
-- 切换 permissions_prd075_new 的迁移期表结构；前后均保持租户归属、外键和状态约束。
ALTER TABLE permissions_prd075_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

-- 迁移 role_permissions 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
       permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd075_roles_to_expand role
JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE permission.code IN ('experiment.read','experiment.write');
-- 迁移 roles 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148'
      WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
      WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
    END
WHERE role_id IN (SELECT role_id FROM prd075_roles_to_expand);

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
    WHEN 'admin' THEN '33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148'
    WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
    WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a'
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
  -- 迁移 roles 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
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
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。' END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148'
      WHEN 'developer' THEN '71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN '28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6'
      WHEN 'ops_admin' THEN '0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 迁移 permissions 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  ) SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    11,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  -- 迁移 role_permissions 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
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

DROP TABLE prd075_roles_to_expand;

CREATE TABLE model_experiments(
  experiment_id TEXT PRIMARY KEY CHECK(
    length(experiment_id)=36 AND substr(experiment_id,1,4)='exp_'
    AND substr(experiment_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK(
    length(name) BETWEEN 1 AND 120 AND name=trim(name)
    AND instr(name,char(0))=0
  ),
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  model_a_id TEXT NOT NULL,
  model_a_provider_id TEXT NOT NULL,
  model_a_provider_code TEXT NOT NULL CHECK(
    length(model_a_provider_code) BETWEEN 1 AND 128
    AND model_a_provider_code=lower(model_a_provider_code)
    AND model_a_provider_code NOT GLOB '*[^a-z0-9_.-]*'
  ),
  model_a_route_config_fingerprint TEXT NOT NULL CHECK(
    length(model_a_route_config_fingerprint)=64
    AND model_a_route_config_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  model_b_id TEXT NOT NULL,
  model_b_provider_id TEXT NOT NULL,
  model_b_provider_code TEXT NOT NULL CHECK(
    length(model_b_provider_code) BETWEEN 1 AND 128
    AND model_b_provider_code=lower(model_b_provider_code)
    AND model_b_provider_code NOT GLOB '*[^a-z0-9_.-]*'
  ),
  model_b_route_config_fingerprint TEXT NOT NULL CHECK(
    length(model_b_route_config_fingerprint)=64
    AND model_b_route_config_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  target_sample_count INTEGER NOT NULL CHECK(
    typeof(target_sample_count)='integer' AND target_sample_count BETWEEN 2 AND 100000
  ),
  traffic_sample_bps INTEGER NOT NULL CHECK(
    typeof(traffic_sample_bps)='integer' AND traffic_sample_bps BETWEEN 1 AND 10000
  ),
  model_a_allocation_bps INTEGER NOT NULL CHECK(
    typeof(model_a_allocation_bps)='integer' AND model_a_allocation_bps BETWEEN 1 AND 9999
  ),
  model_b_allocation_bps INTEGER NOT NULL CHECK(
    typeof(model_b_allocation_bps)='integer' AND model_b_allocation_bps BETWEEN 1 AND 9999
    AND model_a_allocation_bps+model_b_allocation_bps=10000
  ),
  model_a_target_count INTEGER NOT NULL CHECK(
    typeof(model_a_target_count)='integer' AND model_a_target_count>=1
  ),
  model_b_target_count INTEGER NOT NULL CHECK(
    typeof(model_b_target_count)='integer' AND model_b_target_count>=1
    AND model_a_target_count+model_b_target_count=target_sample_count
  ),
  sampling_duration_seconds INTEGER NOT NULL CHECK(
    typeof(sampling_duration_seconds)='integer'
    AND sampling_duration_seconds BETWEEN 60 AND 604800
  ),
  feedback_collection_enabled INTEGER NOT NULL CHECK(
    typeof(feedback_collection_enabled)='integer'
    AND feedback_collection_enabled IN (0,1)
  ),
  feedback_window_seconds INTEGER NOT NULL CHECK(
    typeof(feedback_window_seconds)='integer'
    AND ((feedback_collection_enabled=1 AND feedback_window_seconds BETWEEN 60 AND 604800)
      OR (feedback_collection_enabled=0 AND feedback_window_seconds=0))
  ),
  allocation_algorithm_version INTEGER NOT NULL CHECK(
    typeof(allocation_algorithm_version)='integer' AND allocation_algorithm_version=1
  ),
  sampling_key_fingerprint TEXT NOT NULL CHECK(
    length(sampling_key_fingerprint)=64
    AND sampling_key_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK(status IN ('active','collecting_feedback','completed')),
  assigned_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(assigned_count)='integer' AND assigned_count BETWEEN 0 AND target_sample_count
  ),
  model_a_assigned_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(model_a_assigned_count)='integer'
    AND model_a_assigned_count BETWEEN 0 AND model_a_target_count
  ),
  model_b_assigned_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(model_b_assigned_count)='integer'
    AND model_b_assigned_count BETWEEN 0 AND model_b_target_count
    AND assigned_count=model_a_assigned_count+model_b_assigned_count
  ),
  facts_version INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(facts_version)='integer' AND facts_version BETWEEN 0 AND 9223372036854775807
  ),
  feedback_fence_closed_at TEXT CHECK(
    feedback_fence_closed_at IS NULL OR unixepoch(feedback_fence_closed_at) IS NOT NULL
  ),
  finalization_generation INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(finalization_generation)='integer'
    AND finalization_generation BETWEEN 0 AND 9223372036854775807
  ),
  finalization_owner_id TEXT CHECK(
    finalization_owner_id IS NULL OR (
      length(finalization_owner_id) BETWEEN 1 AND 128
      AND finalization_owner_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
    )
  ),
  finalization_lease_token TEXT CHECK(
    finalization_lease_token IS NULL OR (
      length(finalization_lease_token) BETWEEN 16 AND 128
      AND finalization_lease_token NOT GLOB '*[^A-Za-z0-9_.:-]*'
    )
  ),
  finalization_lease_expires_at TEXT CHECK(
    finalization_lease_expires_at IS NULL
    OR unixepoch(finalization_lease_expires_at) IS NOT NULL
  ),
  created_by TEXT NOT NULL,
  idempotency_key_hash TEXT NOT NULL CHECK(
    length(idempotency_key_hash)=64 AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'
  ),
  request_fingerprint TEXT NOT NULL CHECK(
    length(request_fingerprint)=64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  operation_request_id TEXT NOT NULL CHECK(length(operation_request_id) BETWEEN 1 AND 128),
  operation_trace_id TEXT NOT NULL CHECK(length(operation_trace_id) BETWEEN 1 AND 160),
  execution_max_seconds INTEGER NOT NULL CHECK(
    typeof(execution_max_seconds)='integer' AND execution_max_seconds BETWEEN 60 AND 86400
  ),
  settlement_grace_seconds INTEGER NOT NULL CHECK(
    typeof(settlement_grace_seconds)='integer' AND settlement_grace_seconds BETWEEN 60 AND 86400
  ),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  sampling_deadline_at TEXT NOT NULL CHECK(
    unixepoch(sampling_deadline_at)>unixepoch(started_at)
  ),
  sampling_ended_at TEXT CHECK(
    sampling_ended_at IS NULL OR unixepoch(sampling_ended_at)>=unixepoch(started_at)
  ),
  feedback_deadline_at TEXT CHECK(
    feedback_deadline_at IS NULL OR unixepoch(feedback_deadline_at)>=unixepoch(sampling_ended_at)
  ),
  settlement_deadline_at TEXT CHECK(
    settlement_deadline_at IS NULL
    OR unixepoch(settlement_deadline_at)>=unixepoch(feedback_deadline_at)
  ),
  completion_reason TEXT CHECK(
    completion_reason IS NULL OR completion_reason IN ('target_reached','sampling_deadline_reached')
  ),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(
    unixepoch(updated_at) IS NOT NULL AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  FOREIGN KEY(tenant_id,environment_id) REFERENCES environments(tenant_id,environment_id),
  FOREIGN KEY(tenant_id,model_alias_id) REFERENCES model_aliases(tenant_id,model_alias_id),
  FOREIGN KEY(tenant_id,model_a_id) REFERENCES models(tenant_id,model_id),
  FOREIGN KEY(tenant_id,model_b_id) REFERENCES models(tenant_id,model_id),
  FOREIGN KEY(tenant_id,model_a_provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(tenant_id,model_b_provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(tenant_id,created_by) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,experiment_id),
  UNIQUE(tenant_id,created_by,idempotency_key_hash),
  CHECK(model_a_id!=model_b_id),
  CHECK(
    (status='active' AND sampling_ended_at IS NULL AND feedback_deadline_at IS NULL
      AND settlement_deadline_at IS NULL AND feedback_fence_closed_at IS NULL
      AND completion_reason IS NULL AND completed_at IS NULL)
    OR (status='collecting_feedback' AND sampling_ended_at IS NOT NULL
      AND feedback_deadline_at IS NOT NULL AND settlement_deadline_at IS NOT NULL
      AND completion_reason IS NULL AND completed_at IS NULL)
    OR (status='completed' AND sampling_ended_at IS NOT NULL
      AND feedback_deadline_at IS NOT NULL AND settlement_deadline_at IS NOT NULL
      AND feedback_fence_closed_at IS NOT NULL AND completion_reason IS NOT NULL
      AND completed_at IS NOT NULL)
  ),
  CHECK(
    (finalization_owner_id IS NULL AND finalization_lease_token IS NULL
      AND finalization_lease_expires_at IS NULL)
    OR (finalization_generation>=1 AND finalization_owner_id IS NOT NULL
      AND finalization_lease_token IS NOT NULL AND finalization_lease_expires_at IS NOT NULL)
  )
);
CREATE UNIQUE INDEX uq_model_experiments_active_scope_prd075
  ON model_experiments(tenant_id,project_id,environment_id,model_alias_id)
  WHERE status='active';
CREATE INDEX idx_model_experiments_tenant_status_time_prd075
  ON model_experiments(tenant_id,status,created_at DESC,experiment_id DESC);
CREATE INDEX idx_model_experiments_scheduler_due_prd075
  ON model_experiments(status,sampling_deadline_at,feedback_deadline_at,settlement_deadline_at)
  WHERE status!='completed';

CREATE TABLE gateway_request_scope_claims(
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  virtual_key_id TEXT,
  project_id TEXT,
  application_id TEXT,
  environment_id TEXT,
  environment_code TEXT CHECK(
    environment_code IS NULL OR environment_code IN ('dev','test','prod')
  ),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(tenant_id,request_id),
  FOREIGN KEY(tenant_id,request_id)
    REFERENCES gateway_request_claims(tenant_id,request_id),
  FOREIGN KEY(tenant_id,virtual_key_id)
    REFERENCES virtual_keys(tenant_id,virtual_key_id),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  FOREIGN KEY(tenant_id,application_id) REFERENCES applications(tenant_id,application_id),
  FOREIGN KEY(tenant_id,environment_id) REFERENCES environments(tenant_id,environment_id),
  UNIQUE(tenant_id,request_id,trace_id,virtual_key_id),
  CHECK((environment_id IS NULL)=(environment_code IS NULL)),
  CHECK(application_id IS NULL OR project_id IS NOT NULL),
  CHECK(virtual_key_id IS NOT NULL OR (
    project_id IS NULL AND application_id IS NULL
    AND environment_id IS NULL AND environment_code IS NULL
  ))
);
CREATE INDEX idx_gateway_scope_claims_tenant_time_prd075
  ON gateway_request_scope_claims(tenant_id,created_at,request_id);
CREATE INDEX idx_gateway_scope_claims_scope_prd075
  ON gateway_request_scope_claims(tenant_id,project_id,environment_id,request_id);

-- Assignment is declared before its two children; both reverse edges are deferred closure FKs.
CREATE TABLE experiment_assignments(
  assignment_id TEXT PRIMARY KEY CHECK(
    length(assignment_id)=37 AND substr(assignment_id,1,5)='exas_'
    AND substr(assignment_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  virtual_key_id TEXT NOT NULL,
  routing_decision_id TEXT NOT NULL,
  execution_id TEXT NOT NULL CHECK(
    length(execution_id)=37 AND substr(execution_id,1,5)='exrn_'
    AND substr(execution_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  usage_settlement_id TEXT NOT NULL CHECK(
    length(usage_settlement_id)=37 AND substr(usage_settlement_id,1,5)='exst_'
    AND substr(usage_settlement_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  assignment_sequence INTEGER NOT NULL CHECK(
    typeof(assignment_sequence)='integer' AND assignment_sequence BETWEEN 1 AND 100000
  ),
  variant TEXT NOT NULL CHECK(variant IN ('a','b')),
  model_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_code TEXT NOT NULL CHECK(
    length(provider_code) BETWEEN 1 AND 128 AND provider_code=lower(provider_code)
    AND provider_code NOT GLOB '*[^a-z0-9_.-]*'
  ),
  sample_bucket INTEGER NOT NULL CHECK(
    typeof(sample_bucket)='integer' AND sample_bucket BETWEEN 0 AND 9999
  ),
  variant_bucket INTEGER NOT NULL CHECK(
    typeof(variant_bucket)='integer' AND variant_bucket BETWEEN 0 AND 9999
  ),
  assignment_reason TEXT NOT NULL CHECK(
    assignment_reason IN ('hmac_preferred','floor_forced','ceiling_forced')
  ),
  algorithm_version INTEGER NOT NULL CHECK(
    typeof(algorithm_version)='integer' AND algorithm_version=1
  ),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,experiment_id)
    REFERENCES model_experiments(tenant_id,experiment_id),
  FOREIGN KEY(tenant_id,request_id)
    REFERENCES gateway_request_scope_claims(tenant_id,request_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(tenant_id,routing_decision_id)
    REFERENCES routing_decisions(tenant_id,routing_decision_id),
  FOREIGN KEY(tenant_id,model_id) REFERENCES models(tenant_id,model_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(tenant_id,assignment_id,execution_id)
    REFERENCES experiment_request_executions(tenant_id,assignment_id,execution_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(tenant_id,assignment_id,usage_settlement_id)
    REFERENCES experiment_usage_settlements(tenant_id,assignment_id,usage_settlement_id)
    DEFERRABLE INITIALLY DEFERRED,
  UNIQUE(tenant_id,assignment_id),
  UNIQUE(tenant_id,request_id),
  UNIQUE(tenant_id,experiment_id,assignment_sequence),
  UNIQUE(tenant_id,routing_decision_id)
);
CREATE INDEX idx_experiment_assignments_experiment_variant_prd075
  ON experiment_assignments(tenant_id,experiment_id,variant,assignment_sequence);
CREATE INDEX idx_experiment_assignments_request_prd075
  ON experiment_assignments(tenant_id,request_id,assignment_id);

CREATE TABLE experiment_request_executions(
  execution_id TEXT PRIMARY KEY CHECK(
    length(execution_id)=37 AND substr(execution_id,1,5)='exrn_'
    AND substr(execution_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  status TEXT NOT NULL CHECK(status IN ('assigned','provider_calling','terminal')),
  generation INTEGER NOT NULL CHECK(
    typeof(generation)='integer' AND generation BETWEEN 1 AND 9223372036854775807
  ),
  lease_owner_id TEXT NOT NULL CHECK(
    length(lease_owner_id) BETWEEN 1 AND 128
    AND lease_owner_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  lease_token TEXT NOT NULL CHECK(
    length(lease_token) BETWEEN 16 AND 128
    AND lease_token NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  lease_expires_at TEXT NOT NULL CHECK(unixepoch(lease_expires_at) IS NOT NULL),
  absolute_deadline_at TEXT NOT NULL CHECK(unixepoch(absolute_deadline_at) IS NOT NULL),
  send_fence_at TEXT CHECK(send_fence_at IS NULL OR unixepoch(send_fence_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(
    unixepoch(updated_at) IS NOT NULL AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  FOREIGN KEY(tenant_id,assignment_id)
    REFERENCES experiment_assignments(tenant_id,assignment_id),
  UNIQUE(tenant_id,execution_id),
  UNIQUE(tenant_id,assignment_id),
  UNIQUE(tenant_id,assignment_id,execution_id),
  CHECK(unixepoch(lease_expires_at)<=unixepoch(absolute_deadline_at)),
  CHECK((status='assigned' AND send_fence_at IS NULL)
    OR (status IN ('provider_calling','terminal') AND send_fence_at IS NOT NULL)
    OR (status='terminal' AND send_fence_at IS NULL))
);
CREATE INDEX idx_experiment_executions_lease_prd075
  ON experiment_request_executions(status,lease_expires_at,absolute_deadline_at,execution_id)
  WHERE status!='terminal';
CREATE INDEX idx_experiment_executions_request_prd075
  ON experiment_request_executions(tenant_id,request_id,execution_id);

CREATE TABLE experiment_request_outcomes(
  outcome_id TEXT PRIMARY KEY CHECK(
    length(outcome_id)=37 AND substr(outcome_id,1,5)='exoc_'
    AND substr(outcome_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  execution_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  variant TEXT NOT NULL CHECK(variant IN ('a','b')),
  model_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  gateway_status TEXT NOT NULL CHECK(gateway_status IN ('success','failed','unresolved')),
  exposure_status TEXT NOT NULL CHECK(exposure_status IN ('not_exposed','exposed','unknown')),
  outcome_reason TEXT NOT NULL CHECK(
    outcome_reason IN (
      'completed','gateway_failed','variant_unavailable','execution_timeout',
      'cancelled','abandoned_before_provider','provider_outcome_unknown','recovered_terminal'
    )
  ),
  error_code TEXT CHECK(
    error_code IS NULL OR (length(error_code) BETWEEN 3 AND 128
      AND error_code NOT GLOB '*[^a-z0-9._]*')
  ),
  routing_attempt_id TEXT,
  stream_session_id TEXT,
  quality_eligible INTEGER NOT NULL CHECK(
    typeof(quality_eligible)='integer' AND quality_eligible IN (0,1)
  ),
  quality_succeeded INTEGER CHECK(
    quality_succeeded IS NULL OR (
      typeof(quality_succeeded)='integer' AND quality_succeeded IN (0,1)
    )
  ),
  latency_ms INTEGER CHECK(
    latency_ms IS NULL OR (typeof(latency_ms)='integer' AND latency_ms>=0)
  ),
  recovery_generation INTEGER NOT NULL CHECK(
    typeof(recovery_generation)='integer'
    AND recovery_generation BETWEEN 1 AND 9223372036854775807
  ),
  facts_at TEXT NOT NULL CHECK(unixepoch(facts_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,assignment_id)
    REFERENCES experiment_assignments(tenant_id,assignment_id),
  FOREIGN KEY(tenant_id,execution_id)
    REFERENCES experiment_request_executions(tenant_id,execution_id),
  FOREIGN KEY(tenant_id,routing_attempt_id)
    REFERENCES routing_attempts(tenant_id,routing_attempt_id),
  FOREIGN KEY(tenant_id,stream_session_id)
    REFERENCES stream_sessions(tenant_id,stream_session_id),
  UNIQUE(tenant_id,outcome_id),
  UNIQUE(tenant_id,assignment_id),
  UNIQUE(tenant_id,execution_id),
  CHECK(NOT (routing_attempt_id IS NOT NULL AND stream_session_id IS NOT NULL)),
  CHECK((quality_eligible=0 AND quality_succeeded IS NULL AND latency_ms IS NULL)
    OR (quality_eligible=1 AND exposure_status='exposed'
      AND quality_succeeded IS NOT NULL AND latency_ms IS NOT NULL)),
  CHECK((gateway_status='success' AND error_code IS NULL)
    OR (gateway_status!='success' AND error_code IS NOT NULL)),
  CHECK(
    (outcome_reason='completed'
      AND gateway_status='success' AND exposure_status='exposed'
      AND (routing_attempt_id IS NOT NULL OR stream_session_id IS NOT NULL))
    OR (outcome_reason='gateway_failed' AND gateway_status='failed')
    OR (outcome_reason='variant_unavailable'
      AND gateway_status='failed' AND exposure_status='not_exposed'
      AND error_code='gateway.experiment_variant_unavailable'
      AND routing_attempt_id IS NULL AND stream_session_id IS NULL
      AND quality_eligible=0)
    OR (outcome_reason='execution_timeout'
      AND gateway_status='failed'
      AND error_code='gateway.experiment_execution_timeout'
      AND quality_eligible=0)
    OR (outcome_reason='cancelled'
      AND gateway_status='failed' AND quality_eligible=0)
    OR (outcome_reason='abandoned_before_provider'
      AND gateway_status='unresolved' AND exposure_status='not_exposed'
      AND error_code='gateway.experiment_unavailable'
      AND routing_attempt_id IS NULL AND stream_session_id IS NULL
      AND quality_eligible=0)
    OR (outcome_reason='provider_outcome_unknown'
      AND gateway_status='unresolved' AND exposure_status='unknown'
      AND error_code='gateway.experiment_unavailable'
      AND routing_attempt_id IS NULL AND stream_session_id IS NULL
      AND quality_eligible=0)
    OR (outcome_reason='recovered_terminal'
      AND ((gateway_status='success' AND exposure_status='exposed')
        OR (gateway_status='failed' AND exposure_status IN ('not_exposed','exposed')))
      AND (routing_attempt_id IS NOT NULL OR stream_session_id IS NOT NULL)
      AND (exposure_status='exposed' OR quality_eligible=0))
  )
);
CREATE INDEX idx_experiment_outcomes_experiment_variant_prd075
  ON experiment_request_outcomes(tenant_id,experiment_id,variant,gateway_status,exposure_status);

CREATE TABLE experiment_usage_settlements(
  usage_settlement_id TEXT PRIMARY KEY CHECK(
    length(usage_settlement_id)=37 AND substr(usage_settlement_id,1,5)='exst_'
    AND substr(usage_settlement_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  variant TEXT NOT NULL CHECK(variant IN ('a','b')),
  model_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','recorded','not_exposed','unavailable')),
  usage_ledger_id TEXT,
  finops_ledger_entry_id TEXT,
  gross_cny_micros INTEGER CHECK(
    gross_cny_micros IS NULL OR (typeof(gross_cny_micros)='integer' AND gross_cny_micros>=0)
  ),
  normalization_status TEXT NOT NULL CHECK(
    normalization_status IN ('pending','normalized','unavailable','not_applicable')
  ),
  settlement_reason TEXT NOT NULL CHECK(
    settlement_reason IN (
      'awaiting_usage','usage_charge_recorded','not_exposed','variant_unavailable',
      'usage_unavailable','hard_deadline'
    )
  ),
  settlement_version INTEGER NOT NULL CHECK(
    typeof(settlement_version)='integer' AND settlement_version=1
  ),
  settled_at TEXT CHECK(settled_at IS NULL OR unixepoch(settled_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(
    unixepoch(updated_at) IS NOT NULL AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  FOREIGN KEY(tenant_id,assignment_id)
    REFERENCES experiment_assignments(tenant_id,assignment_id),
  FOREIGN KEY(tenant_id,usage_ledger_id) REFERENCES usage_ledger(tenant_id,usage_ledger_id),
  FOREIGN KEY(tenant_id,finops_ledger_entry_id)
    REFERENCES finops_ledger_entries(tenant_id,finops_ledger_entry_id),
  UNIQUE(tenant_id,usage_settlement_id),
  UNIQUE(tenant_id,assignment_id),
  UNIQUE(tenant_id,assignment_id,usage_settlement_id),
  CHECK(
    (status='pending' AND usage_ledger_id IS NULL AND finops_ledger_entry_id IS NULL
      AND gross_cny_micros IS NULL AND normalization_status='pending'
      AND settlement_reason='awaiting_usage' AND settled_at IS NULL)
    OR (status='recorded' AND finops_ledger_entry_id IS NOT NULL
      AND normalization_status IN ('normalized','unavailable')
      AND settlement_reason='usage_charge_recorded' AND settled_at IS NOT NULL
      AND ((normalization_status='normalized' AND gross_cny_micros IS NOT NULL)
        OR (normalization_status='unavailable' AND gross_cny_micros IS NULL)))
    OR (status='not_exposed' AND usage_ledger_id IS NULL AND finops_ledger_entry_id IS NULL
      AND gross_cny_micros IS NULL AND normalization_status='not_applicable'
      AND settlement_reason IN ('not_exposed','variant_unavailable') AND settled_at IS NOT NULL)
    OR (status='unavailable' AND finops_ledger_entry_id IS NULL
      AND gross_cny_micros IS NULL AND normalization_status='unavailable'
      AND settlement_reason IN ('usage_unavailable','hard_deadline') AND settled_at IS NOT NULL)
  )
);
CREATE INDEX idx_experiment_settlements_pending_prd075
  ON experiment_usage_settlements(status,created_at,usage_settlement_id)
  WHERE status='pending';
CREATE INDEX idx_experiment_settlements_experiment_variant_prd075
  ON experiment_usage_settlements(tenant_id,experiment_id,variant,status);

CREATE TABLE experiment_feedback_attributions(
  feedback_attribution_id TEXT PRIMARY KEY CHECK(
    length(feedback_attribution_id)=37 AND substr(feedback_attribution_id,1,5)='exfa_'
    AND substr(feedback_attribution_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  request_feedback_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK(length(source_request_id) BETWEEN 1 AND 128),
  variant TEXT NOT NULL CHECK(variant IN ('a','b')),
  rating INTEGER NOT NULL CHECK(typeof(rating)='integer' AND rating BETWEEN 1 AND 5),
  accepted INTEGER NOT NULL CHECK(typeof(accepted)='integer' AND accepted IN (0,1)),
  attributed_at TEXT NOT NULL CHECK(unixepoch(attributed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,assignment_id)
    REFERENCES experiment_assignments(tenant_id,assignment_id),
  FOREIGN KEY(tenant_id,request_feedback_id)
    REFERENCES request_feedbacks(tenant_id,request_feedback_id),
  UNIQUE(tenant_id,feedback_attribution_id),
  UNIQUE(tenant_id,assignment_id),
  UNIQUE(tenant_id,request_feedback_id)
);
CREATE INDEX idx_experiment_feedback_experiment_variant_prd075
  ON experiment_feedback_attributions(tenant_id,experiment_id,variant,attributed_at);

CREATE TABLE experiment_results(
  experiment_result_id TEXT PRIMARY KEY CHECK(
    length(experiment_result_id)=37 AND substr(experiment_result_id,1,5)='exrs_'
    AND substr(experiment_result_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  variant TEXT NOT NULL CHECK(variant IN ('a','b')),
  model_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_code TEXT NOT NULL,
  route_config_fingerprint TEXT NOT NULL CHECK(
    length(route_config_fingerprint)=64
    AND route_config_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  assigned_count INTEGER NOT NULL CHECK(typeof(assigned_count)='integer' AND assigned_count>=0),
  gateway_success_count INTEGER NOT NULL CHECK(typeof(gateway_success_count)='integer' AND gateway_success_count>=0),
  gateway_failure_count INTEGER NOT NULL CHECK(typeof(gateway_failure_count)='integer' AND gateway_failure_count>=0),
  unresolved_count INTEGER NOT NULL CHECK(typeof(unresolved_count)='integer' AND unresolved_count>=0),
  exposed_count INTEGER NOT NULL CHECK(typeof(exposed_count)='integer' AND exposed_count>=0),
  not_exposed_count INTEGER NOT NULL CHECK(typeof(not_exposed_count)='integer' AND not_exposed_count>=0),
  exposure_unknown_count INTEGER NOT NULL CHECK(typeof(exposure_unknown_count)='integer' AND exposure_unknown_count>=0),
  quality_success_count INTEGER NOT NULL CHECK(typeof(quality_success_count)='integer' AND quality_success_count>=0),
  quality_failure_count INTEGER NOT NULL CHECK(typeof(quality_failure_count)='integer' AND quality_failure_count>=0),
  gateway_success_rate_bps INTEGER CHECK(gateway_success_rate_bps IS NULL OR (
    typeof(gateway_success_rate_bps)='integer' AND gateway_success_rate_bps BETWEEN 0 AND 10000)),
  quality_success_rate_bps INTEGER CHECK(quality_success_rate_bps IS NULL OR (
    typeof(quality_success_rate_bps)='integer' AND quality_success_rate_bps BETWEEN 0 AND 10000)),
  latency_sample_count INTEGER NOT NULL CHECK(typeof(latency_sample_count)='integer' AND latency_sample_count>=0),
  total_latency_ms INTEGER NOT NULL CHECK(typeof(total_latency_ms)='integer' AND total_latency_ms>=0),
  average_latency_micros INTEGER CHECK(average_latency_micros IS NULL OR (
    typeof(average_latency_micros)='integer' AND average_latency_micros>=0)),
  p95_latency_ms INTEGER CHECK(p95_latency_ms IS NULL OR (
    typeof(p95_latency_ms)='integer' AND p95_latency_ms>=0)),
  settlement_recorded_count INTEGER NOT NULL CHECK(typeof(settlement_recorded_count)='integer' AND settlement_recorded_count>=0),
  settlement_not_exposed_count INTEGER NOT NULL CHECK(typeof(settlement_not_exposed_count)='integer' AND settlement_not_exposed_count>=0),
  settlement_unavailable_count INTEGER NOT NULL CHECK(typeof(settlement_unavailable_count)='integer' AND settlement_unavailable_count>=0),
  gross_normalized_cny_micros INTEGER NOT NULL CHECK(typeof(gross_normalized_cny_micros)='integer' AND gross_normalized_cny_micros>=0),
  cost_known_count INTEGER NOT NULL CHECK(typeof(cost_known_count)='integer' AND cost_known_count>=0),
  cost_unknown_count INTEGER NOT NULL CHECK(typeof(cost_unknown_count)='integer' AND cost_unknown_count>=0),
  average_cost_cny_micros INTEGER CHECK(average_cost_cny_micros IS NULL OR (
    typeof(average_cost_cny_micros)='integer' AND average_cost_cny_micros>=0)),
  feedback_enabled INTEGER NOT NULL CHECK(typeof(feedback_enabled)='integer' AND feedback_enabled IN (0,1)),
  feedback_count INTEGER NOT NULL CHECK(typeof(feedback_count)='integer' AND feedback_count>=0),
  rating_sum INTEGER NOT NULL CHECK(typeof(rating_sum)='integer' AND rating_sum>=0),
  average_rating_milli INTEGER NOT NULL CHECK(typeof(average_rating_milli)='integer' AND average_rating_milli BETWEEN 0 AND 5000),
  accepted_count INTEGER NOT NULL CHECK(typeof(accepted_count)='integer' AND accepted_count>=0),
  adoption_rate_bps INTEGER NOT NULL CHECK(typeof(adoption_rate_bps)='integer' AND adoption_rate_bps BETWEEN 0 AND 10000),
  statistics_version INTEGER NOT NULL CHECK(typeof(statistics_version)='integer' AND statistics_version=1),
  facts_version INTEGER NOT NULL CHECK(typeof(facts_version)='integer' AND facts_version>=0),
  facts_through_at TEXT NOT NULL CHECK(unixepoch(facts_through_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,experiment_id)
    REFERENCES model_experiments(tenant_id,experiment_id),
  UNIQUE(tenant_id,experiment_result_id),
  UNIQUE(tenant_id,experiment_id,variant),
  CHECK(gateway_success_count+gateway_failure_count+unresolved_count=assigned_count),
  CHECK(exposed_count+not_exposed_count+exposure_unknown_count=assigned_count),
  CHECK(quality_success_count+quality_failure_count=latency_sample_count),
  CHECK(settlement_recorded_count+settlement_not_exposed_count+settlement_unavailable_count=assigned_count),
  CHECK(cost_known_count+cost_unknown_count<=assigned_count),
  CHECK((latency_sample_count=0 AND average_latency_micros IS NULL AND p95_latency_ms IS NULL)
    OR (latency_sample_count>0 AND average_latency_micros IS NOT NULL AND p95_latency_ms IS NOT NULL)),
  CHECK((cost_known_count=0 AND average_cost_cny_micros IS NULL)
    OR (cost_known_count>0 AND average_cost_cny_micros IS NOT NULL)),
  CHECK((feedback_enabled=0 AND feedback_count=0 AND rating_sum=0
      AND average_rating_milli=0 AND accepted_count=0 AND adoption_rate_bps=0)
    OR feedback_enabled=1)
);
CREATE INDEX idx_experiment_results_experiment_prd075
  ON experiment_results(tenant_id,experiment_id,variant);

CREATE TABLE experiment_events(
  experiment_event_id TEXT PRIMARY KEY CHECK(
    length(experiment_event_id)=37 AND substr(experiment_event_id,1,5)='exev_'
    AND substr(experiment_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type='experiment.completed'),
  actor_type TEXT NOT NULL CHECK(actor_type='user'),
  actor_id TEXT NOT NULL,
  object_type TEXT NOT NULL CHECK(object_type='experiment'),
  object_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK(length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK(length(source_trace_id) BETWEEN 1 AND 160),
  payload_json TEXT NOT NULL CHECK(
    json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=8192
  ),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,experiment_id)
    REFERENCES model_experiments(tenant_id,experiment_id),
  FOREIGN KEY(tenant_id,actor_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,experiment_event_id),
  UNIQUE(tenant_id,experiment_id,event_type)
);
CREATE INDEX idx_experiment_events_tenant_time_prd075
  ON experiment_events(tenant_id,occurred_at,experiment_event_id);

CREATE TABLE experiment_event_outbox(
  experiment_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(experiment_event_outbox_id)=37
    AND substr(experiment_event_outbox_id,1,5)='exob_'
    AND substr(experiment_event_outbox_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  experiment_event_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  dispatch_status TEXT NOT NULL CHECK(
    dispatch_status IN ('pending','processing','dispatched','failed')
  ),
  dispatch_attempts INTEGER NOT NULL CHECK(
    typeof(dispatch_attempts)='integer' AND dispatch_attempts BETWEEN 0 AND 10
  ),
  lease_generation INTEGER NOT NULL CHECK(
    typeof(lease_generation)='integer' AND lease_generation BETWEEN 0 AND 9223372036854775807
  ),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  claim_token TEXT,
  lease_owner_id TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  delivery_snapshot_frozen_at TEXT CHECK(delivery_snapshot_frozen_at IS NULL OR unixepoch(delivery_snapshot_frozen_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN ('webhook.payload_invalid','webhook.delivery_unavailable')),
  dispatched_at TEXT CHECK(dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,experiment_event_id)
    REFERENCES experiment_events(tenant_id,experiment_event_id),
  UNIQUE(tenant_id,experiment_event_outbox_id),
  UNIQUE(tenant_id,experiment_event_id),
  CHECK(
    (dispatch_status='pending' AND dispatch_attempts BETWEEN 0 AND 9
      AND next_attempt_at IS NOT NULL AND claim_token IS NULL AND lease_owner_id IS NULL
      AND lease_expires_at IS NULL AND dispatched_at IS NULL)
    OR (dispatch_status='processing' AND dispatch_attempts BETWEEN 1 AND 10
      AND lease_generation>=1 AND next_attempt_at IS NULL AND claim_token IS NOT NULL
      AND length(claim_token) BETWEEN 16 AND 128 AND lease_owner_id IS NOT NULL
      AND length(lease_owner_id) BETWEEN 1 AND 128 AND lease_expires_at IS NOT NULL
      AND last_error_code IS NULL AND dispatched_at IS NULL)
    OR (dispatch_status='dispatched' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND claim_token IS NULL AND lease_owner_id IS NULL
      AND lease_expires_at IS NULL AND delivery_snapshot_frozen_at IS NOT NULL
      AND last_error_code IS NULL AND dispatched_at IS NOT NULL)
    OR (dispatch_status='failed' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND claim_token IS NULL AND lease_owner_id IS NULL
      AND lease_expires_at IS NULL AND last_error_code IS NOT NULL AND dispatched_at IS NULL)
  )
);
CREATE INDEX idx_experiment_outbox_due_prd075
  ON experiment_event_outbox(dispatch_status,next_attempt_at,lease_expires_at,
    created_at,experiment_event_outbox_id)
  WHERE dispatch_status IN ('pending','processing');

CREATE TABLE experiment_scheduler_tenants(
  tenant_id TEXT PRIMARY KEY,
  next_run_at TEXT NOT NULL CHECK(unixepoch(next_run_at) IS NOT NULL),
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(lease_generation)='integer' AND lease_generation BETWEEN 0 AND 9223372036854775807
  ),
  lease_token TEXT CHECK(
    lease_token IS NULL OR (length(lease_token) BETWEEN 16 AND 128
      AND lease_token NOT GLOB '*[^A-Za-z0-9_.:-]*')
  ),
  lease_owner_id TEXT CHECK(
    lease_owner_id IS NULL OR (length(lease_owner_id) BETWEEN 1 AND 128
      AND lease_owner_id NOT GLOB '*[^A-Za-z0-9_.:-]*')
  ),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  CHECK((lease_token IS NULL AND lease_owner_id IS NULL AND lease_expires_at IS NULL)
    OR (lease_generation>=1 AND lease_token IS NOT NULL
      AND lease_owner_id IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE INDEX idx_experiment_scheduler_due_prd075
  ON experiment_scheduler_tenants(next_run_at,lease_expires_at,tenant_id);

CREATE TRIGGER trg_model_experiments_owner_insert_prd075
BEFORE INSERT ON model_experiments FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants
    WHERE tenant_id=NEW.tenant_id AND status='active')
  OR NOT EXISTS(SELECT 1 FROM users
    WHERE tenant_id=NEW.tenant_id AND user_id=NEW.created_by AND status='active')
  OR NOT EXISTS(SELECT 1 FROM projects
    WHERE tenant_id=NEW.tenant_id AND project_id=NEW.project_id AND status='active')
  OR NOT EXISTS(SELECT 1 FROM environments
    WHERE tenant_id=NEW.tenant_id AND environment_id=NEW.environment_id AND status='active')
  OR NOT EXISTS(SELECT 1 FROM model_aliases
    WHERE tenant_id=NEW.tenant_id AND model_alias_id=NEW.model_alias_id
      AND environment_id=NEW.environment_id AND status='active')
  OR NOT EXISTS(SELECT 1 FROM models model JOIN providers provider
      ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
    WHERE model.tenant_id=NEW.tenant_id AND model.model_id=NEW.model_a_id
      AND model.provider_id=NEW.model_a_provider_id AND model.status='active'
      AND provider.status='active' AND provider.provider_code=NEW.model_a_provider_code
      AND json_valid(model.capabilities)
      AND json_extract(model.capabilities,'$.chat')=1)
  OR NOT EXISTS(SELECT 1 FROM models model JOIN providers provider
      ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
    WHERE model.tenant_id=NEW.tenant_id AND model.model_id=NEW.model_b_id
      AND model.provider_id=NEW.model_b_provider_id AND model.status='active'
      AND provider.status='active' AND provider.provider_code=NEW.model_b_provider_code
      AND json_valid(model.capabilities)
      AND json_extract(model.capabilities,'$.chat')=1)
  OR NEW.model_a_target_count!=(NEW.target_sample_count*NEW.model_a_allocation_bps+5000)/10000
  OR NEW.model_b_target_count!=NEW.target_sample_count-NEW.model_a_target_count
  OR NEW.status!='active' OR NEW.assigned_count!=0
  OR NEW.model_a_assigned_count!=0 OR NEW.model_b_assigned_count!=0
  OR NEW.facts_version!=0 OR NEW.finalization_generation!=0
  OR NEW.started_at!=NEW.created_at
  OR unixepoch(NEW.sampling_deadline_at)!=unixepoch(NEW.started_at)+NEW.sampling_duration_seconds
  OR EXISTS(SELECT 1 FROM model_experiments current
    WHERE current.experiment_id=NEW.experiment_id
       OR (current.tenant_id=NEW.tenant_id AND current.created_by=NEW.created_by
         AND current.idempotency_key_hash=NEW.idempotency_key_hash)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_owner_mismatch'); END;

CREATE TRIGGER trg_model_experiments_immutable_update_prd075
BEFORE UPDATE ON model_experiments FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.experiment_id!=OLD.experiment_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.name!=OLD.name
  OR NEW.project_id!=OLD.project_id OR NEW.environment_id!=OLD.environment_id
  OR NEW.model_alias_id!=OLD.model_alias_id
  OR NEW.model_a_id!=OLD.model_a_id OR NEW.model_a_provider_id!=OLD.model_a_provider_id
  OR NEW.model_a_provider_code!=OLD.model_a_provider_code
  OR NEW.model_a_route_config_fingerprint!=OLD.model_a_route_config_fingerprint
  OR NEW.model_b_id!=OLD.model_b_id OR NEW.model_b_provider_id!=OLD.model_b_provider_id
  OR NEW.model_b_provider_code!=OLD.model_b_provider_code
  OR NEW.model_b_route_config_fingerprint!=OLD.model_b_route_config_fingerprint
  OR NEW.target_sample_count!=OLD.target_sample_count
  OR NEW.traffic_sample_bps!=OLD.traffic_sample_bps
  OR NEW.model_a_allocation_bps!=OLD.model_a_allocation_bps
  OR NEW.model_b_allocation_bps!=OLD.model_b_allocation_bps
  OR NEW.model_a_target_count!=OLD.model_a_target_count
  OR NEW.model_b_target_count!=OLD.model_b_target_count
  OR NEW.sampling_duration_seconds!=OLD.sampling_duration_seconds
  OR NEW.feedback_collection_enabled!=OLD.feedback_collection_enabled
  OR NEW.feedback_window_seconds!=OLD.feedback_window_seconds
  OR NEW.allocation_algorithm_version!=OLD.allocation_algorithm_version
  OR NEW.sampling_key_fingerprint!=OLD.sampling_key_fingerprint
  OR NEW.created_by!=OLD.created_by OR NEW.idempotency_key_hash!=OLD.idempotency_key_hash
  OR NEW.request_fingerprint!=OLD.request_fingerprint
  OR NEW.operation_request_id!=OLD.operation_request_id
  OR NEW.operation_trace_id!=OLD.operation_trace_id
  OR NEW.execution_max_seconds!=OLD.execution_max_seconds
  OR NEW.settlement_grace_seconds!=OLD.settlement_grace_seconds
  OR NEW.started_at!=OLD.started_at OR NEW.sampling_deadline_at!=OLD.sampling_deadline_at
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'experiment_immutable'); END;

CREATE TRIGGER trg_model_experiments_state_update_prd075
BEFORE UPDATE ON model_experiments FOR EACH ROW
WHEN NEW.assigned_count<OLD.assigned_count
  OR NEW.model_a_assigned_count<OLD.model_a_assigned_count
  OR NEW.model_b_assigned_count<OLD.model_b_assigned_count
  OR NEW.assigned_count>OLD.assigned_count+1
  OR NEW.model_a_assigned_count>OLD.model_a_assigned_count+1
  OR NEW.model_b_assigned_count>OLD.model_b_assigned_count+1
  OR NEW.facts_version<OLD.facts_version OR NEW.facts_version>OLD.facts_version+1
  OR NEW.finalization_generation<OLD.finalization_generation
  OR (OLD.status='active' AND NEW.status NOT IN ('active','collecting_feedback'))
  OR (OLD.status='collecting_feedback' AND NEW.status NOT IN ('collecting_feedback','completed'))
  OR (OLD.status='completed' AND (
    NEW.status!='completed' OR NEW.facts_version!=OLD.facts_version
    OR NEW.assigned_count!=OLD.assigned_count
    OR NEW.model_a_assigned_count!=OLD.model_a_assigned_count
    OR NEW.model_b_assigned_count!=OLD.model_b_assigned_count
    OR NEW.feedback_fence_closed_at IS NOT OLD.feedback_fence_closed_at
    OR NEW.completed_at IS NOT OLD.completed_at))
  OR (NEW.assigned_count!=OLD.assigned_count
    AND NEW.facts_version!=OLD.facts_version+1)
  OR (NEW.feedback_fence_closed_at IS NOT OLD.feedback_fence_closed_at
    AND NOT (OLD.feedback_fence_closed_at IS NULL
      AND NEW.feedback_fence_closed_at IS NOT NULL
      AND NEW.facts_version=OLD.facts_version))
  OR (NEW.status='completed' AND (
    (SELECT COUNT(*) FROM experiment_results result
      WHERE result.tenant_id=NEW.tenant_id AND result.experiment_id=NEW.experiment_id)!=2
    OR NOT EXISTS(SELECT 1 FROM experiment_events event
      WHERE event.tenant_id=NEW.tenant_id AND event.experiment_id=NEW.experiment_id
        AND event.event_type='experiment.completed')))
BEGIN SELECT RAISE(ABORT,'experiment_state_invalid'); END;

CREATE TRIGGER trg_model_experiments_finalization_update_prd075
BEFORE UPDATE ON model_experiments FOR EACH ROW
WHEN NOT (
    (NEW.finalization_generation=OLD.finalization_generation
      AND NEW.finalization_owner_id IS OLD.finalization_owner_id
      AND NEW.finalization_lease_token IS OLD.finalization_lease_token
      AND NEW.finalization_lease_expires_at IS OLD.finalization_lease_expires_at
      AND (OLD.finalization_lease_token IS NULL OR (
        NEW.status=OLD.status
        AND NEW.assigned_count=OLD.assigned_count
        AND NEW.model_a_assigned_count=OLD.model_a_assigned_count
        AND NEW.model_b_assigned_count=OLD.model_b_assigned_count
        AND NEW.facts_version=OLD.facts_version
        AND NEW.feedback_fence_closed_at IS OLD.feedback_fence_closed_at
        AND NEW.sampling_ended_at IS OLD.sampling_ended_at
        AND NEW.feedback_deadline_at IS OLD.feedback_deadline_at
        AND NEW.settlement_deadline_at IS OLD.settlement_deadline_at
        AND NEW.completion_reason IS OLD.completion_reason
        AND NEW.completed_at IS OLD.completed_at
        AND NEW.updated_at=OLD.updated_at)))
    OR (
      OLD.status='collecting_feedback' AND NEW.status='collecting_feedback'
      AND NEW.feedback_fence_closed_at IS NOT NULL
      AND NEW.finalization_generation=OLD.finalization_generation+1
      AND (OLD.finalization_lease_token IS NULL
        OR unixepoch(OLD.finalization_lease_expires_at)<=unixepoch('now'))
      AND NEW.finalization_owner_id IS NOT NULL
      AND NEW.finalization_lease_token IS NOT NULL
      AND NEW.finalization_lease_expires_at IS NOT NULL
      AND unixepoch(NEW.finalization_lease_expires_at)>unixepoch('now')
      AND unixepoch(NEW.finalization_lease_expires_at)<=unixepoch('now')+300
      AND EXISTS(SELECT 1 FROM experiment_scheduler_tenants scheduler
        WHERE scheduler.tenant_id=NEW.tenant_id
          AND scheduler.lease_owner_id=NEW.finalization_owner_id
          AND scheduler.lease_token IS NOT NULL
          AND unixepoch(scheduler.lease_expires_at)>unixepoch('now')
          AND unixepoch(scheduler.lease_expires_at)
            >=unixepoch(NEW.finalization_lease_expires_at))
      AND (SELECT COUNT(*) FROM experiment_request_executions execution
        WHERE execution.tenant_id=NEW.tenant_id
          AND execution.experiment_id=NEW.experiment_id
          AND execution.status='terminal')=NEW.assigned_count
      AND (SELECT COUNT(*) FROM experiment_request_outcomes outcome
        WHERE outcome.tenant_id=NEW.tenant_id
          AND outcome.experiment_id=NEW.experiment_id)=NEW.assigned_count
      AND (SELECT COUNT(*) FROM experiment_usage_settlements settlement
        WHERE settlement.tenant_id=NEW.tenant_id
          AND settlement.experiment_id=NEW.experiment_id
          AND settlement.status!='pending')=NEW.assigned_count
    )
    OR (
      OLD.status='collecting_feedback' AND NEW.status='completed'
      AND NEW.finalization_generation=OLD.finalization_generation
      AND OLD.finalization_owner_id IS NOT NULL
      AND OLD.finalization_lease_token IS NOT NULL
      AND unixepoch(OLD.finalization_lease_expires_at)>unixepoch('now')
      AND NEW.finalization_owner_id IS NULL
      AND NEW.finalization_lease_token IS NULL
      AND NEW.finalization_lease_expires_at IS NULL
      AND EXISTS(SELECT 1 FROM experiment_scheduler_tenants scheduler
        WHERE scheduler.tenant_id=OLD.tenant_id
          AND scheduler.lease_owner_id=OLD.finalization_owner_id
          AND scheduler.lease_token IS NOT NULL
          AND unixepoch(scheduler.lease_expires_at)>unixepoch('now'))
    )
  )
BEGIN SELECT RAISE(ABORT,'experiment_finalization_state_invalid'); END;

CREATE TRIGGER trg_model_experiments_delete_prd075
BEFORE DELETE ON model_experiments FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_immutable'); END;
CREATE TRIGGER trg_model_experiments_rowid_prd075
AFTER INSERT ON model_experiments FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_immutable'); END;

CREATE TRIGGER trg_model_experiments_feedback_fence_facts_prd075
AFTER UPDATE OF feedback_fence_closed_at ON model_experiments FOR EACH ROW
WHEN OLD.feedback_fence_closed_at IS NULL AND NEW.feedback_fence_closed_at IS NOT NULL
BEGIN
  -- 迁移 model_experiments 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  UPDATE model_experiments
  SET facts_version=facts_version+1
  WHERE tenant_id=NEW.tenant_id AND experiment_id=NEW.experiment_id
    AND facts_version<9223372036854775807;
  SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'experiment_facts_version_invalid') END;
END;

CREATE TRIGGER trg_gateway_scope_claims_owner_insert_prd075
BEFORE INSERT ON gateway_request_scope_claims FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id AND claim.request_id=NEW.request_id
      AND claim.trace_id=NEW.trace_id AND claim.virtual_key_id IS NEW.virtual_key_id)
  OR (NEW.virtual_key_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM virtual_keys key
    WHERE key.tenant_id=NEW.tenant_id AND key.virtual_key_id=NEW.virtual_key_id
      AND key.project_id IS NEW.project_id AND key.application_id IS NEW.application_id
      AND key.environment IS NEW.environment_code
      AND (
        key.environment_id IS NEW.environment_id
        OR (
          -- 0068 前的兼容 Key 可能只保存环境 code；只接受同租户同 code 解析出的环境 ID。
          key.environment_id IS NULL AND NEW.environment_id IS NOT NULL
          AND EXISTS(SELECT 1 FROM environments legacy_environment
            WHERE legacy_environment.tenant_id=key.tenant_id
              AND legacy_environment.environment_id=NEW.environment_id
              AND legacy_environment.code=key.environment)
        )
      )))
  OR (NEW.application_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM applications application
    WHERE application.tenant_id=NEW.tenant_id
      AND application.application_id=NEW.application_id
      AND application.project_id=NEW.project_id))
  OR (NEW.environment_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM environments environment
    WHERE environment.tenant_id=NEW.tenant_id
      AND environment.environment_id=NEW.environment_id
      AND environment.code=NEW.environment_code))
  OR EXISTS(SELECT 1 FROM gateway_request_scope_claims current
    WHERE (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_scope_claim_owner_mismatch'); END;
CREATE TRIGGER trg_gateway_scope_claims_update_prd075
BEFORE UPDATE ON gateway_request_scope_claims FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_scope_claim_immutable'); END;
CREATE TRIGGER trg_gateway_scope_claims_delete_prd075
BEFORE DELETE ON gateway_request_scope_claims FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_scope_claim_immutable'); END;
CREATE TRIGGER trg_gateway_scope_claims_rowid_prd075
AFTER INSERT ON gateway_request_scope_claims FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_scope_claim_immutable'); END;

CREATE TRIGGER trg_experiment_assignments_owner_insert_prd075
BEFORE INSERT ON experiment_assignments FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM model_experiments experiment
    JOIN gateway_request_scope_claims scope
      ON scope.tenant_id=experiment.tenant_id
     AND scope.project_id=experiment.project_id
     AND scope.environment_id=experiment.environment_id
    JOIN gateway_request_claims claim
      ON claim.tenant_id=scope.tenant_id AND claim.request_id=scope.request_id
    JOIN routing_decisions decision
      ON decision.tenant_id=scope.tenant_id AND decision.request_id=scope.request_id
    WHERE experiment.tenant_id=NEW.tenant_id
      AND experiment.experiment_id=NEW.experiment_id AND experiment.status='active'
      AND experiment.assigned_count<experiment.target_sample_count
      AND NEW.assignment_sequence=experiment.assigned_count+1
      AND NEW.sample_bucket<experiment.traffic_sample_bps
      AND NEW.algorithm_version=experiment.allocation_algorithm_version
      AND scope.request_id=NEW.request_id AND scope.trace_id=NEW.trace_id
      AND scope.virtual_key_id=NEW.virtual_key_id
      AND claim.trace_id=NEW.trace_id AND claim.virtual_key_id=NEW.virtual_key_id
      AND decision.routing_decision_id=NEW.routing_decision_id
      AND decision.trace_id=NEW.trace_id AND decision.virtual_key_id=NEW.virtual_key_id
      AND decision.model_alias_id=experiment.model_alias_id
      AND decision.selected_model_id=NEW.model_id
      AND decision.provider_id=NEW.provider_id AND decision.provider_code=NEW.provider_code
      AND decision.decision_reason=CASE NEW.variant
        WHEN 'a' THEN 'experiment.variant_a' ELSE 'experiment.variant_b' END
      AND ((decision.decision_status='selected' AND decision.error_code IS NULL)
        OR (decision.decision_status='failed'
          AND decision.error_code='gateway.experiment_variant_unavailable'))
      AND json_valid(decision.candidate_summary)
      AND (SELECT COUNT(*) FROM json_each(decision.candidate_summary))=5
      AND NOT EXISTS(SELECT 1 FROM json_each(decision.candidate_summary) field
        WHERE field.key NOT IN ('experiment_id','assignment_id','variant',
          'assignment_reason','algorithm_version'))
      AND json_extract(decision.candidate_summary,'$.experiment_id')=NEW.experiment_id
      AND json_extract(decision.candidate_summary,'$.assignment_id')=NEW.assignment_id
      AND json_extract(decision.candidate_summary,'$.variant')=NEW.variant
      AND json_extract(decision.candidate_summary,'$.assignment_reason')=NEW.assignment_reason
      AND json_extract(decision.candidate_summary,'$.algorithm_version')=NEW.algorithm_version
      AND NEW.model_id=CASE NEW.variant
        WHEN 'a' THEN experiment.model_a_id ELSE experiment.model_b_id END
      AND NEW.provider_id=CASE NEW.variant
        WHEN 'a' THEN experiment.model_a_provider_id ELSE experiment.model_b_provider_id END
      AND NEW.provider_code=CASE NEW.variant
        WHEN 'a' THEN experiment.model_a_provider_code ELSE experiment.model_b_provider_code END
      AND (
        (NEW.assignment_reason='floor_forced' AND NEW.variant='a' AND (
          experiment.model_a_assigned_count<
            (NEW.assignment_sequence*experiment.model_a_target_count)/experiment.target_sample_count
          OR experiment.model_b_assigned_count>=experiment.model_b_target_count))
        OR (NEW.assignment_reason='ceiling_forced' AND NEW.variant='b' AND (
          experiment.model_a_assigned_count>=
            (NEW.assignment_sequence*experiment.model_a_target_count
              +experiment.target_sample_count-1)/experiment.target_sample_count
          OR experiment.model_a_assigned_count>=experiment.model_a_target_count))
        OR (NEW.assignment_reason='hmac_preferred'
          AND experiment.model_a_assigned_count>=
            (NEW.assignment_sequence*experiment.model_a_target_count)/experiment.target_sample_count
          AND experiment.model_a_assigned_count<
            (NEW.assignment_sequence*experiment.model_a_target_count
              +experiment.target_sample_count-1)/experiment.target_sample_count
          AND NEW.variant=CASE WHEN NEW.variant_bucket<experiment.model_a_allocation_bps
            THEN 'a' ELSE 'b' END)
      )
      AND experiment.model_a_assigned_count+CASE WHEN NEW.variant='a' THEN 1 ELSE 0 END
        BETWEEN (NEW.assignment_sequence*experiment.model_a_target_count)/experiment.target_sample_count
          AND (NEW.assignment_sequence*experiment.model_a_target_count
            +experiment.target_sample_count-1)/experiment.target_sample_count
  )
  OR EXISTS(SELECT 1 FROM experiment_assignments current
    WHERE current.assignment_id=NEW.assignment_id
       OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
       OR (current.tenant_id=NEW.tenant_id AND current.experiment_id=NEW.experiment_id
         AND current.assignment_sequence=NEW.assignment_sequence)
       OR (current.tenant_id=NEW.tenant_id
         AND current.routing_decision_id=NEW.routing_decision_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_assignment_owner_mismatch'); END;

CREATE TRIGGER trg_experiment_assignments_update_prd075
BEFORE UPDATE ON experiment_assignments FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_assignment_immutable'); END;
CREATE TRIGGER trg_experiment_assignments_delete_prd075
BEFORE DELETE ON experiment_assignments FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_assignment_immutable'); END;
CREATE TRIGGER trg_experiment_assignments_rowid_prd075
AFTER INSERT ON experiment_assignments FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_assignment_immutable'); END;

CREATE TRIGGER trg_experiment_assignments_count_prd075
AFTER INSERT ON experiment_assignments FOR EACH ROW
BEGIN
  -- 迁移 model_experiments 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  UPDATE model_experiments
  SET assigned_count=assigned_count+1,
      model_a_assigned_count=model_a_assigned_count+CASE WHEN NEW.variant='a' THEN 1 ELSE 0 END,
      model_b_assigned_count=model_b_assigned_count+CASE WHEN NEW.variant='b' THEN 1 ELSE 0 END,
      facts_version=facts_version+1,
      status=CASE WHEN assigned_count+1=target_sample_count
        THEN 'collecting_feedback' ELSE status END,
      sampling_ended_at=CASE WHEN assigned_count+1=target_sample_count
        THEN NEW.created_at ELSE sampling_ended_at END,
      feedback_deadline_at=CASE WHEN assigned_count+1=target_sample_count
        THEN strftime('%Y-%m-%dT%H:%M:%SZ',NEW.created_at,
          '+'||feedback_window_seconds||' seconds') ELSE feedback_deadline_at END,
      settlement_deadline_at=CASE WHEN assigned_count+1=target_sample_count THEN
        max(
          strftime('%Y-%m-%dT%H:%M:%SZ',NEW.created_at,
            '+'||feedback_window_seconds||' seconds'),
          strftime('%Y-%m-%dT%H:%M:%SZ',NEW.created_at,
            '+'||(execution_max_seconds+settlement_grace_seconds)||' seconds')
        ) ELSE settlement_deadline_at END,
      updated_at=NEW.created_at
  WHERE tenant_id=NEW.tenant_id AND experiment_id=NEW.experiment_id
    AND facts_version<9223372036854775807;
  SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'experiment_counter_overflow') END;
END;

CREATE TRIGGER trg_experiment_executions_owner_insert_prd075
BEFORE INSERT ON experiment_request_executions FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM experiment_assignments assignment
    JOIN model_experiments experiment
      ON experiment.tenant_id=assignment.tenant_id
     AND experiment.experiment_id=assignment.experiment_id
    WHERE assignment.tenant_id=NEW.tenant_id
      AND assignment.assignment_id=NEW.assignment_id
      AND assignment.execution_id=NEW.execution_id
      AND assignment.experiment_id=NEW.experiment_id
      AND assignment.request_id=NEW.request_id
      AND NEW.status='assigned' AND NEW.generation=1
      AND unixepoch(NEW.absolute_deadline_at)
        =unixepoch(assignment.created_at)+experiment.execution_max_seconds
      AND unixepoch(NEW.lease_expires_at)>unixepoch(assignment.created_at)
  )
  OR EXISTS(SELECT 1 FROM experiment_request_executions current
    WHERE current.execution_id=NEW.execution_id
       OR (current.tenant_id=NEW.tenant_id AND current.assignment_id=NEW.assignment_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_execution_owner_mismatch'); END;

CREATE TRIGGER trg_experiment_executions_update_prd075
BEFORE UPDATE ON experiment_request_executions FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.execution_id!=OLD.execution_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.experiment_id!=OLD.experiment_id
  OR NEW.assignment_id!=OLD.assignment_id OR NEW.request_id!=OLD.request_id
  OR NEW.absolute_deadline_at!=OLD.absolute_deadline_at OR NEW.created_at!=OLD.created_at
  OR NEW.generation<OLD.generation OR NEW.generation>OLD.generation+1
  OR (NEW.generation=OLD.generation AND (
    NEW.lease_owner_id!=OLD.lease_owner_id OR NEW.lease_token!=OLD.lease_token))
  OR (NEW.generation=OLD.generation+1
    AND unixepoch(OLD.lease_expires_at)>unixepoch('now'))
  OR (NEW.generation=OLD.generation+1 AND (
    NEW.status!=OLD.status OR NEW.send_fence_at IS NOT OLD.send_fence_at))
  OR (NEW.generation=OLD.generation AND OLD.status!='terminal'
    AND (unixepoch(OLD.lease_expires_at)<=unixepoch('now')
      OR unixepoch(OLD.absolute_deadline_at)<=unixepoch('now'))
    AND NOT (NEW.status='terminal' AND length(OLD.lease_owner_id)=50
      AND substr(OLD.lease_owner_id,1,18)='experiment-reaper-'
      AND substr(OLD.lease_owner_id,19) NOT GLOB '*[^0-9a-f]*'
      AND EXISTS(SELECT 1 FROM experiment_request_outcomes outcome
        WHERE outcome.tenant_id=NEW.tenant_id
          AND outcome.execution_id=NEW.execution_id
          AND outcome.recovery_generation=NEW.generation
          AND outcome.outcome_reason IN (
            'abandoned_before_provider','provider_outcome_unknown','recovered_terminal'
          )
          AND unixepoch(outcome.facts_at)=unixepoch(NEW.updated_at))))
  OR (OLD.status='assigned' AND NEW.status NOT IN ('assigned','provider_calling','terminal'))
  OR (OLD.status='provider_calling' AND NEW.status NOT IN ('provider_calling','terminal'))
  OR OLD.status='terminal'
  OR (OLD.send_fence_at IS NOT NULL AND NEW.send_fence_at IS NOT OLD.send_fence_at)
  OR (OLD.send_fence_at IS NULL AND NEW.send_fence_at IS NOT NULL
    AND NEW.status NOT IN ('provider_calling','terminal'))
  OR (NEW.status='terminal' AND NOT EXISTS(
    SELECT 1 FROM experiment_request_outcomes outcome
    WHERE outcome.tenant_id=NEW.tenant_id AND outcome.execution_id=NEW.execution_id
      AND outcome.assignment_id=NEW.assignment_id
      AND outcome.recovery_generation=NEW.generation
      AND unixepoch(outcome.facts_at)=unixepoch(NEW.updated_at)))
BEGIN SELECT RAISE(ABORT,'experiment_execution_state_invalid'); END;

CREATE TRIGGER trg_experiment_executions_delete_prd075
BEFORE DELETE ON experiment_request_executions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_execution_immutable'); END;
CREATE TRIGGER trg_experiment_executions_rowid_prd075
AFTER INSERT ON experiment_request_executions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_execution_immutable'); END;

CREATE TRIGGER trg_experiment_outcomes_owner_insert_prd075
BEFORE INSERT ON experiment_request_outcomes FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM experiment_assignments assignment
    JOIN experiment_request_executions execution
      ON execution.tenant_id=assignment.tenant_id
     AND execution.assignment_id=assignment.assignment_id
     AND execution.execution_id=assignment.execution_id
    WHERE assignment.tenant_id=NEW.tenant_id
      AND assignment.assignment_id=NEW.assignment_id
      AND assignment.experiment_id=NEW.experiment_id
      AND assignment.request_id=NEW.request_id
      AND assignment.variant=NEW.variant AND assignment.model_id=NEW.model_id
      AND assignment.provider_id=NEW.provider_id
      AND execution.execution_id=NEW.execution_id AND execution.status!='terminal'
      AND execution.generation=NEW.recovery_generation
      AND unixepoch(NEW.facts_at)=unixepoch(NEW.created_at)
      AND unixepoch(NEW.facts_at)>=unixepoch(execution.created_at)
      AND (
        (NEW.outcome_reason IN (
            'completed','gateway_failed','variant_unavailable',
            'execution_timeout','cancelled'
          )
          AND unixepoch(execution.lease_expires_at)>unixepoch('now')
          AND unixepoch(execution.absolute_deadline_at)>unixepoch('now')
          AND unixepoch(execution.lease_expires_at)>unixepoch(NEW.facts_at)
          AND unixepoch(execution.absolute_deadline_at)>unixepoch(NEW.facts_at)
          AND (
            (NEW.outcome_reason='variant_unavailable'
              AND execution.status='assigned' AND execution.send_fence_at IS NULL)
            OR (NEW.outcome_reason='completed'
              AND execution.status='provider_calling'
              AND execution.send_fence_at IS NOT NULL)
            OR (NEW.outcome_reason IN ('gateway_failed','execution_timeout','cancelled')
              AND (
                (execution.status='assigned' AND execution.send_fence_at IS NULL
                  AND NEW.exposure_status='not_exposed' AND NEW.quality_eligible=0)
                OR (execution.status='provider_calling'
                  AND execution.send_fence_at IS NOT NULL)
              ))
          ))
        OR ((unixepoch(execution.lease_expires_at)<=unixepoch(NEW.facts_at)
              OR unixepoch(execution.absolute_deadline_at)<=unixepoch(NEW.facts_at))
          AND (unixepoch(execution.lease_expires_at)<=unixepoch('now')
            OR unixepoch(execution.absolute_deadline_at)<=unixepoch('now'))
          AND (
            (NEW.outcome_reason='abandoned_before_provider'
              AND execution.status='assigned' AND execution.send_fence_at IS NULL)
            OR (NEW.outcome_reason IN ('provider_outcome_unknown','recovered_terminal')
              AND execution.status='provider_calling'
              AND execution.send_fence_at IS NOT NULL)
          ))
      )
      AND (NEW.routing_attempt_id IS NULL OR EXISTS(
        SELECT 1 FROM routing_attempts attempt
        WHERE attempt.tenant_id=NEW.tenant_id
          AND attempt.routing_attempt_id=NEW.routing_attempt_id
          AND attempt.request_id=NEW.request_id AND attempt.model_id=NEW.model_id
          AND attempt.provider_id=NEW.provider_id AND attempt.attempt_sequence=1
          AND attempt.attempt_role='primary' AND attempt.fallback_policy_id IS NULL
          AND attempt.status IN ('succeeded','failed') AND attempt.completed_at IS NOT NULL
          AND (NEW.gateway_status!='success' OR attempt.status='succeeded')))
      AND (NEW.stream_session_id IS NULL OR EXISTS(
        SELECT 1 FROM stream_sessions stream
        WHERE stream.tenant_id=NEW.tenant_id
          AND stream.stream_session_id=NEW.stream_session_id
          AND stream.request_id=NEW.request_id AND stream.model_id=NEW.model_id
          AND stream.provider_id=NEW.provider_id
          AND stream.status NOT IN ('starting','active') AND stream.completed_at IS NOT NULL
          AND (NEW.gateway_status!='success' OR stream.status='completed')))
      AND (NEW.outcome_reason!='recovered_terminal' OR EXISTS(
        SELECT 1 FROM request_logs request_log
        WHERE request_log.tenant_id=NEW.tenant_id
          AND request_log.request_id=NEW.request_id
          AND request_log.model_id=NEW.model_id
          AND request_log.provider_id=NEW.provider_id
          AND request_log.status=NEW.gateway_status))
      AND (NEW.outcome_reason!='recovered_terminal' OR NEW.gateway_status!='success'
        OR EXISTS(SELECT 1 FROM experiment_usage_settlements settlement
          WHERE settlement.tenant_id=NEW.tenant_id
            AND settlement.assignment_id=NEW.assignment_id
            AND settlement.status='recorded'))
  )
  OR EXISTS(SELECT 1 FROM experiment_request_outcomes current
    WHERE current.outcome_id=NEW.outcome_id
       OR (current.tenant_id=NEW.tenant_id AND current.assignment_id=NEW.assignment_id)
       OR (current.tenant_id=NEW.tenant_id AND current.execution_id=NEW.execution_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_outcome_owner_mismatch'); END;

CREATE TRIGGER trg_experiment_outcomes_update_prd075
BEFORE UPDATE ON experiment_request_outcomes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_outcome_immutable'); END;
CREATE TRIGGER trg_experiment_outcomes_delete_prd075
BEFORE DELETE ON experiment_request_outcomes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_outcome_immutable'); END;
CREATE TRIGGER trg_experiment_outcomes_rowid_prd075
AFTER INSERT ON experiment_request_outcomes FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_outcome_immutable'); END;
CREATE TRIGGER trg_experiment_outcomes_facts_prd075
AFTER INSERT ON experiment_request_outcomes FOR EACH ROW
BEGIN
  -- 迁移 model_experiments 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  UPDATE model_experiments SET facts_version=facts_version+1,updated_at=NEW.facts_at
  WHERE tenant_id=NEW.tenant_id AND experiment_id=NEW.experiment_id
    AND status!='completed' AND facts_version<9223372036854775807;
  SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'experiment_facts_version_invalid') END;
END;

CREATE TRIGGER trg_experiment_settlements_owner_insert_prd075
BEFORE INSERT ON experiment_usage_settlements FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=NEW.tenant_id
      AND assignment.assignment_id=NEW.assignment_id
      AND assignment.usage_settlement_id=NEW.usage_settlement_id
      AND assignment.experiment_id=NEW.experiment_id
      AND assignment.request_id=NEW.request_id AND assignment.variant=NEW.variant
      AND assignment.model_id=NEW.model_id AND assignment.provider_id=NEW.provider_id
      AND (NEW.status='pending'
        OR (NEW.status='not_exposed' AND NEW.settlement_reason='variant_unavailable'))
  )
  OR EXISTS(SELECT 1 FROM experiment_usage_settlements current
    WHERE current.usage_settlement_id=NEW.usage_settlement_id
       OR (current.tenant_id=NEW.tenant_id AND current.assignment_id=NEW.assignment_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_settlement_owner_mismatch'); END;

CREATE TRIGGER trg_experiment_settlements_update_prd075
BEFORE UPDATE ON experiment_usage_settlements FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.usage_settlement_id!=OLD.usage_settlement_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.experiment_id!=OLD.experiment_id
  OR NEW.assignment_id!=OLD.assignment_id OR NEW.request_id!=OLD.request_id
  OR NEW.variant!=OLD.variant OR NEW.model_id!=OLD.model_id OR NEW.provider_id!=OLD.provider_id
  OR NEW.settlement_version!=OLD.settlement_version OR NEW.created_at!=OLD.created_at
  OR OLD.status!='pending' OR NEW.status NOT IN ('recorded','not_exposed','unavailable')
  OR (NEW.status='recorded' AND NOT EXISTS(
    SELECT 1 FROM finops_ledger_entries entry
    WHERE entry.tenant_id=NEW.tenant_id
      AND entry.finops_ledger_entry_id=NEW.finops_ledger_entry_id
      AND entry.entry_type='usage_charge' AND entry.direction='debit'
      AND entry.quota_type='paid' AND entry.status='posted'
      AND entry.posting_source IN ('direct','spool_replay')
      AND entry.usage_ledger_id=NEW.usage_ledger_id
      AND entry.source_request_id=NEW.request_id
      AND entry.model_id=NEW.model_id AND entry.provider_id=NEW.provider_id
      AND NEW.normalization_status=CASE entry.cny_normalization_status
        WHEN 'normalized' THEN 'normalized' ELSE 'unavailable' END
      AND NEW.gross_cny_micros IS entry.normalized_cny_micros
      AND NEW.settled_at=entry.created_at AND NEW.updated_at=entry.created_at))
  OR (NEW.status='not_exposed' AND NOT EXISTS(
    SELECT 1 FROM experiment_request_outcomes outcome
    WHERE outcome.tenant_id=NEW.tenant_id
      AND outcome.assignment_id=NEW.assignment_id
      AND outcome.exposure_status='not_exposed'))
  OR EXISTS(SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=OLD.tenant_id AND experiment.experiment_id=OLD.experiment_id
      AND experiment.status='completed')
BEGIN SELECT RAISE(ABORT,'experiment_settlement_immutable'); END;
CREATE TRIGGER trg_experiment_settlements_delete_prd075
BEFORE DELETE ON experiment_usage_settlements FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_settlement_immutable'); END;
CREATE TRIGGER trg_experiment_settlements_rowid_prd075
AFTER INSERT ON experiment_usage_settlements FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_settlement_immutable'); END;
CREATE TRIGGER trg_experiment_settlements_facts_prd075
AFTER UPDATE OF status ON experiment_usage_settlements FOR EACH ROW
WHEN NEW.status!=OLD.status
BEGIN
  -- 迁移 model_experiments 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  UPDATE model_experiments SET facts_version=facts_version+1,updated_at=NEW.updated_at
  WHERE tenant_id=NEW.tenant_id AND experiment_id=NEW.experiment_id
    AND status!='completed' AND facts_version<9223372036854775807;
  SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'experiment_facts_version_invalid') END;
END;

CREATE TRIGGER trg_experiment_feedback_owner_insert_prd075
BEFORE INSERT ON experiment_feedback_attributions FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM experiment_assignments assignment
    JOIN request_feedbacks feedback
      ON feedback.tenant_id=assignment.tenant_id
     AND feedback.source_request_id=assignment.request_id
    JOIN model_experiments experiment
      ON experiment.tenant_id=assignment.tenant_id
     AND experiment.experiment_id=assignment.experiment_id
    WHERE assignment.tenant_id=NEW.tenant_id
      AND assignment.assignment_id=NEW.assignment_id
      AND assignment.experiment_id=NEW.experiment_id
      AND assignment.variant=NEW.variant
      AND assignment.request_id=NEW.source_request_id
      AND feedback.request_feedback_id=NEW.request_feedback_id
      AND feedback.model_id=assignment.model_id AND feedback.provider_id=assignment.provider_id
      AND feedback.rating=NEW.rating AND feedback.accepted=NEW.accepted
      AND experiment.feedback_collection_enabled=1
      AND experiment.feedback_fence_closed_at IS NULL
      AND experiment.status!='completed'
      AND (experiment.sampling_ended_at IS NULL
        OR unixepoch(NEW.attributed_at)<unixepoch(experiment.feedback_deadline_at))
  )
  OR EXISTS(SELECT 1 FROM experiment_feedback_attributions current
    WHERE current.feedback_attribution_id=NEW.feedback_attribution_id
       OR (current.tenant_id=NEW.tenant_id AND current.assignment_id=NEW.assignment_id)
       OR (current.tenant_id=NEW.tenant_id
         AND current.request_feedback_id=NEW.request_feedback_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_feedback_owner_mismatch'); END;

CREATE TRIGGER trg_experiment_feedback_update_prd075
BEFORE UPDATE ON experiment_feedback_attributions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_feedback_immutable'); END;
CREATE TRIGGER trg_experiment_feedback_delete_prd075
BEFORE DELETE ON experiment_feedback_attributions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_feedback_immutable'); END;
CREATE TRIGGER trg_experiment_feedback_rowid_prd075
AFTER INSERT ON experiment_feedback_attributions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_feedback_immutable'); END;
CREATE TRIGGER trg_experiment_feedback_facts_prd075
AFTER INSERT ON experiment_feedback_attributions FOR EACH ROW
BEGIN
  -- 迁移 model_experiments 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  UPDATE model_experiments SET facts_version=facts_version+1,updated_at=NEW.attributed_at
  WHERE tenant_id=NEW.tenant_id AND experiment_id=NEW.experiment_id
    AND status!='completed' AND facts_version<9223372036854775807;
  SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'experiment_facts_version_invalid') END;
END;

CREATE TRIGGER trg_request_feedbacks_experiment_attribution_prd075
AFTER INSERT ON request_feedbacks FOR EACH ROW
BEGIN
  -- 迁移 experiment_feedback_attributions 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  INSERT INTO experiment_feedback_attributions(
    feedback_attribution_id,tenant_id,experiment_id,assignment_id,
    request_feedback_id,source_request_id,variant,rating,accepted,attributed_at,created_at
  )
  SELECT 'exfa_'||lower(hex(randomblob(16))),assignment.tenant_id,
    assignment.experiment_id,assignment.assignment_id,NEW.request_feedback_id,
    NEW.source_request_id,assignment.variant,NEW.rating,NEW.accepted,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM experiment_assignments assignment
  JOIN model_experiments experiment
    ON experiment.tenant_id=assignment.tenant_id
   AND experiment.experiment_id=assignment.experiment_id
  WHERE assignment.tenant_id=NEW.tenant_id
    AND assignment.request_id=NEW.source_request_id
    AND assignment.model_id=NEW.model_id AND assignment.provider_id=NEW.provider_id
    AND experiment.feedback_collection_enabled=1
    AND experiment.feedback_fence_closed_at IS NULL
    AND experiment.status!='completed'
    AND (experiment.sampling_ended_at IS NULL
      OR unixepoch('now')<unixepoch(experiment.feedback_deadline_at));
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

CREATE TRIGGER trg_experiment_results_owner_insert_prd075
BEFORE INSERT ON experiment_results FOR EACH ROW
WHEN NOT EXISTS(
    WITH arm_facts AS (
      SELECT assignment.assignment_id,outcome.outcome_id,
             outcome.gateway_status,outcome.exposure_status,
             outcome.quality_eligible,outcome.quality_succeeded,outcome.latency_ms,
             settlement.status AS settlement_status,
             settlement.normalization_status,settlement.gross_cny_micros,
             attribution.rating,attribution.accepted
      FROM experiment_assignments assignment
      JOIN experiment_request_executions execution
        ON execution.tenant_id=assignment.tenant_id
       AND execution.assignment_id=assignment.assignment_id
       AND execution.execution_id=assignment.execution_id
      JOIN experiment_request_outcomes outcome
        ON outcome.tenant_id=assignment.tenant_id
       AND outcome.assignment_id=assignment.assignment_id
       AND outcome.execution_id=assignment.execution_id
      JOIN experiment_usage_settlements settlement
        ON settlement.tenant_id=assignment.tenant_id
       AND settlement.assignment_id=assignment.assignment_id
       AND settlement.usage_settlement_id=assignment.usage_settlement_id
      LEFT JOIN experiment_feedback_attributions attribution
        ON attribution.tenant_id=assignment.tenant_id
       AND attribution.assignment_id=assignment.assignment_id
      WHERE assignment.tenant_id=NEW.tenant_id
        AND assignment.experiment_id=NEW.experiment_id
        AND assignment.variant=NEW.variant
    ),
    latency_ranked AS (
      SELECT latency_ms,
             row_number() OVER (ORDER BY latency_ms,outcome_id) AS latency_rank,
             count(*) OVER () AS latency_count
      FROM arm_facts WHERE quality_eligible=1
    ),
    arm_totals AS (
      SELECT COUNT(*) AS assigned_count,
        COALESCE(SUM(gateway_status='success'),0) AS gateway_success_count,
        COALESCE(SUM(gateway_status='failed'),0) AS gateway_failure_count,
        COALESCE(SUM(gateway_status='unresolved'),0) AS unresolved_count,
        COALESCE(SUM(exposure_status='exposed'),0) AS exposed_count,
        COALESCE(SUM(exposure_status='not_exposed'),0) AS not_exposed_count,
        COALESCE(SUM(exposure_status='unknown'),0) AS exposure_unknown_count,
        COALESCE(SUM(quality_eligible=1 AND quality_succeeded=1),0)
          AS quality_success_count,
        COALESCE(SUM(quality_eligible=1 AND quality_succeeded=0),0)
          AS quality_failure_count,
        COALESCE(SUM(CASE WHEN quality_eligible=1 THEN latency_ms ELSE 0 END),0)
          AS total_latency_ms,
        COALESCE(SUM(settlement_status='recorded'),0) AS settlement_recorded_count,
        COALESCE(SUM(settlement_status='not_exposed'),0)
          AS settlement_not_exposed_count,
        COALESCE(SUM(settlement_status='unavailable'),0)
          AS settlement_unavailable_count,
        COALESCE(SUM(CASE
          WHEN settlement_status='recorded' AND normalization_status='normalized'
            THEN gross_cny_micros ELSE 0 END),0) AS gross_normalized_cny_micros,
        COALESCE(SUM(settlement_status='recorded'
          AND normalization_status='normalized'),0) AS cost_known_count,
        COALESCE(SUM(exposure_status='unknown' OR settlement_status='unavailable'
          OR (settlement_status='recorded' AND gross_cny_micros IS NULL)),0)
          AS cost_unknown_count,
        COALESCE(SUM(rating IS NOT NULL),0) AS feedback_count,
        COALESCE(SUM(CASE WHEN rating IS NOT NULL THEN rating ELSE 0 END),0)
          AS rating_sum,
        COALESCE(SUM(rating IS NOT NULL AND accepted=1),0) AS accepted_count
      FROM arm_facts
    ),
    expected AS (
      SELECT arm_totals.*,
        quality_success_count+quality_failure_count AS latency_sample_count,
        CASE WHEN gateway_success_count+gateway_failure_count=0 THEN NULL ELSE
          (2*gateway_success_count*10000
            +gateway_success_count+gateway_failure_count)
          /(2*(gateway_success_count+gateway_failure_count)) END
          AS gateway_success_rate_bps,
        CASE WHEN quality_success_count+quality_failure_count=0 THEN NULL ELSE
          (2*quality_success_count*10000
            +quality_success_count+quality_failure_count)
          /(2*(quality_success_count+quality_failure_count)) END
          AS quality_success_rate_bps,
        CASE WHEN quality_success_count+quality_failure_count=0 THEN NULL ELSE
          (total_latency_ms/(quality_success_count+quality_failure_count))*1000
          +(2*(total_latency_ms%(quality_success_count+quality_failure_count))*1000
              +quality_success_count+quality_failure_count)
            /(2*(quality_success_count+quality_failure_count)) END
          AS average_latency_micros,
        (SELECT latency_ms FROM latency_ranked
          WHERE latency_rank=(95*latency_count+99)/100) AS p95_latency_ms,
        CASE WHEN cost_known_count=0 THEN NULL ELSE
          gross_normalized_cny_micros/cost_known_count
          +(2*(gross_normalized_cny_micros%cost_known_count)+cost_known_count)
            /(2*cost_known_count) END AS average_cost_cny_micros,
        CASE WHEN feedback_count=0 THEN 0 ELSE
          (2*rating_sum*1000+feedback_count)/(2*feedback_count) END
          AS average_rating_milli,
        CASE WHEN feedback_count=0 THEN 0 ELSE
          (2*accepted_count*10000+feedback_count)/(2*feedback_count) END
          AS adoption_rate_bps
      FROM arm_totals
    )
    SELECT 1 FROM model_experiments experiment
    JOIN experiment_scheduler_tenants scheduler
      ON scheduler.tenant_id=experiment.tenant_id
     AND scheduler.lease_owner_id=experiment.finalization_owner_id
     AND scheduler.lease_token IS NOT NULL
     AND unixepoch(scheduler.lease_expires_at)>unixepoch('now')
     AND unixepoch(scheduler.lease_expires_at)
       >=unixepoch(experiment.finalization_lease_expires_at)
    CROSS JOIN expected
    WHERE experiment.tenant_id=NEW.tenant_id AND experiment.experiment_id=NEW.experiment_id
      AND experiment.status='collecting_feedback'
      AND experiment.feedback_fence_closed_at IS NOT NULL
      AND experiment.facts_version=NEW.facts_version
      AND experiment.finalization_generation>=1
      AND experiment.finalization_owner_id IS NOT NULL
      AND experiment.finalization_lease_token IS NOT NULL
      AND experiment.finalization_lease_expires_at IS NOT NULL
      AND unixepoch(experiment.finalization_lease_expires_at)>unixepoch('now')
      AND NEW.statistics_version=1
      AND NEW.model_id=CASE NEW.variant
        WHEN 'a' THEN experiment.model_a_id ELSE experiment.model_b_id END
      AND NEW.provider_id=CASE NEW.variant
        WHEN 'a' THEN experiment.model_a_provider_id ELSE experiment.model_b_provider_id END
      AND NEW.provider_code=CASE NEW.variant
        WHEN 'a' THEN experiment.model_a_provider_code ELSE experiment.model_b_provider_code END
      AND NEW.route_config_fingerprint=CASE NEW.variant
        WHEN 'a' THEN experiment.model_a_route_config_fingerprint
        ELSE experiment.model_b_route_config_fingerprint END
      AND NEW.assigned_count=CASE NEW.variant
        WHEN 'a' THEN experiment.model_a_assigned_count ELSE experiment.model_b_assigned_count END
      AND NEW.feedback_enabled=experiment.feedback_collection_enabled
      AND NEW.assigned_count=expected.assigned_count
      AND NEW.gateway_success_count=expected.gateway_success_count
      AND NEW.gateway_failure_count=expected.gateway_failure_count
      AND NEW.unresolved_count=expected.unresolved_count
      AND NEW.exposed_count=expected.exposed_count
      AND NEW.not_exposed_count=expected.not_exposed_count
      AND NEW.exposure_unknown_count=expected.exposure_unknown_count
      AND NEW.quality_success_count=expected.quality_success_count
      AND NEW.quality_failure_count=expected.quality_failure_count
      AND NEW.gateway_success_rate_bps IS expected.gateway_success_rate_bps
      AND NEW.quality_success_rate_bps IS expected.quality_success_rate_bps
      AND NEW.latency_sample_count=expected.latency_sample_count
      AND NEW.total_latency_ms=expected.total_latency_ms
      AND NEW.average_latency_micros IS expected.average_latency_micros
      AND NEW.p95_latency_ms IS expected.p95_latency_ms
      AND NEW.settlement_recorded_count=expected.settlement_recorded_count
      AND NEW.settlement_not_exposed_count=expected.settlement_not_exposed_count
      AND NEW.settlement_unavailable_count=expected.settlement_unavailable_count
      AND NEW.gross_normalized_cny_micros=expected.gross_normalized_cny_micros
      AND NEW.cost_known_count=expected.cost_known_count
      AND NEW.cost_unknown_count=expected.cost_unknown_count
      AND NEW.average_cost_cny_micros IS expected.average_cost_cny_micros
      AND NEW.feedback_count=expected.feedback_count
      AND NEW.rating_sum=expected.rating_sum
      AND NEW.average_rating_milli=expected.average_rating_milli
      AND NEW.accepted_count=expected.accepted_count
      AND NEW.adoption_rate_bps=expected.adoption_rate_bps
      AND (expected.latency_sample_count=0
        OR expected.total_latency_ms/expected.latency_sample_count<9223372036854775
        OR (expected.total_latency_ms/expected.latency_sample_count=9223372036854775
          AND (2*(expected.total_latency_ms%expected.latency_sample_count)*1000
                +expected.latency_sample_count)/(2*expected.latency_sample_count)<=807))
      AND NEW.facts_through_at=experiment.updated_at
      AND unixepoch(NEW.facts_through_at)>=unixepoch(experiment.feedback_fence_closed_at)
      AND unixepoch(NEW.facts_through_at)<=unixepoch(NEW.created_at)
      AND unixepoch(NEW.created_at)<unixepoch(experiment.finalization_lease_expires_at)
      AND NOT EXISTS(SELECT 1 FROM experiment_assignments assignment
        WHERE assignment.tenant_id=NEW.tenant_id
          AND assignment.experiment_id=NEW.experiment_id
          AND unixepoch(assignment.created_at)>unixepoch(NEW.facts_through_at))
      AND NOT EXISTS(SELECT 1 FROM experiment_request_executions execution
        WHERE execution.tenant_id=NEW.tenant_id
          AND execution.experiment_id=NEW.experiment_id
          AND unixepoch(execution.updated_at)>unixepoch(NEW.facts_through_at))
      AND NOT EXISTS(SELECT 1 FROM experiment_request_outcomes outcome
        WHERE outcome.tenant_id=NEW.tenant_id
          AND outcome.experiment_id=NEW.experiment_id
          AND unixepoch(outcome.facts_at)>unixepoch(NEW.facts_through_at))
      AND NOT EXISTS(SELECT 1 FROM experiment_usage_settlements settlement
        WHERE settlement.tenant_id=NEW.tenant_id
          AND settlement.experiment_id=NEW.experiment_id
          AND unixepoch(settlement.updated_at)>unixepoch(NEW.facts_through_at))
      AND NOT EXISTS(SELECT 1 FROM experiment_feedback_attributions attribution
        WHERE attribution.tenant_id=NEW.tenant_id
          AND attribution.experiment_id=NEW.experiment_id
          AND unixepoch(attribution.attributed_at)>unixepoch(NEW.facts_through_at))
      AND (SELECT COUNT(*) FROM experiment_request_executions execution
        WHERE execution.tenant_id=NEW.tenant_id
          AND execution.experiment_id=NEW.experiment_id
          AND execution.status='terminal')=experiment.assigned_count
      AND (SELECT COUNT(*) FROM experiment_request_outcomes outcome
        WHERE outcome.tenant_id=NEW.tenant_id
          AND outcome.experiment_id=NEW.experiment_id)=experiment.assigned_count
      AND (SELECT COUNT(*) FROM experiment_usage_settlements settlement
        WHERE settlement.tenant_id=NEW.tenant_id
          AND settlement.experiment_id=NEW.experiment_id
          AND settlement.status!='pending')=experiment.assigned_count
      AND NOT EXISTS(SELECT 1 FROM experiment_results current
        WHERE current.tenant_id=NEW.tenant_id
          AND current.experiment_id=NEW.experiment_id
          AND (current.facts_version!=NEW.facts_version
            OR current.facts_through_at!=NEW.facts_through_at
            OR current.created_at!=NEW.created_at))
  )
  OR EXISTS(SELECT 1 FROM experiment_results current
    WHERE current.experiment_result_id=NEW.experiment_result_id
       OR (current.tenant_id=NEW.tenant_id AND current.experiment_id=NEW.experiment_id
         AND current.variant=NEW.variant)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_result_owner_mismatch'); END;

CREATE TRIGGER trg_experiment_results_update_prd075
BEFORE UPDATE ON experiment_results FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_result_immutable'); END;
CREATE TRIGGER trg_experiment_results_delete_prd075
BEFORE DELETE ON experiment_results FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_result_immutable'); END;
CREATE TRIGGER trg_experiment_results_rowid_prd075
AFTER INSERT ON experiment_results FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_result_immutable'); END;

CREATE TRIGGER trg_experiment_events_owner_insert_prd075
BEFORE INSERT ON experiment_events FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=NEW.tenant_id AND experiment.experiment_id=NEW.experiment_id
      AND experiment.status='collecting_feedback'
      AND experiment.feedback_fence_closed_at IS NOT NULL
      AND experiment.finalization_owner_id IS NOT NULL
      AND experiment.finalization_lease_token IS NOT NULL
      AND unixepoch(experiment.finalization_lease_expires_at)>unixepoch('now')
      AND EXISTS(SELECT 1 FROM experiment_scheduler_tenants scheduler
        WHERE scheduler.tenant_id=experiment.tenant_id
          AND scheduler.lease_owner_id=experiment.finalization_owner_id
          AND scheduler.lease_token IS NOT NULL
          AND unixepoch(scheduler.lease_expires_at)>unixepoch('now')
          AND unixepoch(scheduler.lease_expires_at)
            >=unixepoch(experiment.finalization_lease_expires_at))
      AND NEW.actor_id=experiment.created_by AND NEW.object_id=experiment.experiment_id
      AND NEW.source_request_id=experiment.operation_request_id
      AND NEW.source_trace_id=experiment.operation_trace_id
      AND (SELECT COUNT(*) FROM experiment_results result
        WHERE result.tenant_id=experiment.tenant_id
          AND result.experiment_id=experiment.experiment_id)=2
      AND json_valid(NEW.payload_json)
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=19
      AND NOT EXISTS(SELECT 1 FROM json_each(NEW.payload_json) field
        WHERE field.key NOT IN (
          'schema_version','experiment_id','project_id','environment_id','model_alias_id',
          'model_a_id','model_b_id','status','completion_reason','assigned_count',
          'model_a_assigned_count','model_b_assigned_count','gateway_success_count',
          'gateway_failure_count','unresolved_count','cost_unknown_count','feedback_count',
          'facts_version','facts_through_at'))
      AND json_extract(NEW.payload_json,'$.schema_version')=1
      AND json_extract(NEW.payload_json,'$.experiment_id')=experiment.experiment_id
      AND json_extract(NEW.payload_json,'$.project_id')=experiment.project_id
      AND json_extract(NEW.payload_json,'$.environment_id')=experiment.environment_id
      AND json_extract(NEW.payload_json,'$.model_alias_id')=experiment.model_alias_id
      AND json_extract(NEW.payload_json,'$.model_a_id')=experiment.model_a_id
      AND json_extract(NEW.payload_json,'$.model_b_id')=experiment.model_b_id
      AND json_extract(NEW.payload_json,'$.status')='completed'
      AND json_extract(NEW.payload_json,'$.completion_reason')=CASE
        WHEN experiment.assigned_count=experiment.target_sample_count
          THEN 'target_reached' ELSE 'sampling_deadline_reached' END
      AND json_extract(NEW.payload_json,'$.assigned_count')=experiment.assigned_count
      AND json_extract(NEW.payload_json,'$.model_a_assigned_count')=experiment.model_a_assigned_count
      AND json_extract(NEW.payload_json,'$.model_b_assigned_count')=experiment.model_b_assigned_count
      AND json_extract(NEW.payload_json,'$.gateway_success_count')=(
        SELECT SUM(gateway_success_count) FROM experiment_results result
        WHERE result.tenant_id=experiment.tenant_id AND result.experiment_id=experiment.experiment_id)
      AND json_extract(NEW.payload_json,'$.gateway_failure_count')=(
        SELECT SUM(gateway_failure_count) FROM experiment_results result
        WHERE result.tenant_id=experiment.tenant_id AND result.experiment_id=experiment.experiment_id)
      AND json_extract(NEW.payload_json,'$.unresolved_count')=(
        SELECT SUM(unresolved_count) FROM experiment_results result
        WHERE result.tenant_id=experiment.tenant_id AND result.experiment_id=experiment.experiment_id)
      AND json_extract(NEW.payload_json,'$.cost_unknown_count')=(
        SELECT SUM(cost_unknown_count) FROM experiment_results result
        WHERE result.tenant_id=experiment.tenant_id AND result.experiment_id=experiment.experiment_id)
      AND json_extract(NEW.payload_json,'$.feedback_count')=(
        SELECT SUM(feedback_count) FROM experiment_results result
        WHERE result.tenant_id=experiment.tenant_id AND result.experiment_id=experiment.experiment_id)
      AND json_extract(NEW.payload_json,'$.facts_version')=experiment.facts_version
      AND json_extract(NEW.payload_json,'$.facts_through_at')=(
        SELECT MIN(facts_through_at) FROM experiment_results result
        WHERE result.tenant_id=experiment.tenant_id AND result.experiment_id=experiment.experiment_id)
  )
  OR EXISTS(SELECT 1 FROM experiment_events current
    WHERE current.experiment_event_id=NEW.experiment_event_id
       OR (current.tenant_id=NEW.tenant_id AND current.experiment_id=NEW.experiment_id
         AND current.event_type=NEW.event_type)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_event_owner_mismatch'); END;

CREATE TRIGGER trg_experiment_events_update_prd075
BEFORE UPDATE ON experiment_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_event_immutable'); END;
CREATE TRIGGER trg_experiment_events_delete_prd075
BEFORE DELETE ON experiment_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_event_immutable'); END;
CREATE TRIGGER trg_experiment_events_rowid_prd075
AFTER INSERT ON experiment_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_event_immutable'); END;

CREATE TRIGGER trg_experiment_events_create_outbox_prd075
AFTER INSERT ON experiment_events FOR EACH ROW
BEGIN
  -- 迁移 experiment_event_outbox 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
  INSERT INTO experiment_event_outbox(
    experiment_event_outbox_id,tenant_id,experiment_event_id,experiment_id,
    dispatch_status,dispatch_attempts,lease_generation,next_attempt_at,created_at,updated_at
  ) VALUES(
    'exob_'||substr(NEW.experiment_event_id,6),NEW.tenant_id,NEW.experiment_event_id,
    NEW.experiment_id,'pending',0,0,NEW.occurred_at,NEW.occurred_at,NEW.occurred_at
  );
END;

CREATE TRIGGER trg_experiment_outbox_owner_insert_prd075
BEFORE INSERT ON experiment_event_outbox FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM experiment_events event
    WHERE event.tenant_id=NEW.tenant_id
      AND event.experiment_event_id=NEW.experiment_event_id
      AND event.experiment_id=NEW.experiment_id)
  OR NEW.experiment_event_outbox_id!='exob_'||substr(NEW.experiment_event_id,6)
  OR NEW.dispatch_status!='pending' OR NEW.dispatch_attempts!=0 OR NEW.lease_generation!=0
  OR NEW.next_attempt_at!=NEW.created_at OR NEW.updated_at!=NEW.created_at
  OR EXISTS(SELECT 1 FROM experiment_event_outbox current
    WHERE current.experiment_event_outbox_id=NEW.experiment_event_outbox_id
       OR (current.tenant_id=NEW.tenant_id
         AND current.experiment_event_id=NEW.experiment_event_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_outbox_owner_mismatch'); END;

CREATE TRIGGER trg_experiment_outbox_update_prd075
BEFORE UPDATE ON experiment_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.experiment_event_outbox_id!=OLD.experiment_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.experiment_event_id!=OLD.experiment_event_id
  OR NEW.experiment_id!=OLD.experiment_id OR NEW.created_at!=OLD.created_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NEW.dispatch_attempts<OLD.dispatch_attempts
  OR NEW.dispatch_attempts>OLD.dispatch_attempts+1
  OR NEW.lease_generation<OLD.lease_generation
  OR NEW.lease_generation>OLD.lease_generation+1
  OR (OLD.dispatch_status='pending' AND NEW.dispatch_status NOT IN ('pending','processing'))
  OR (OLD.dispatch_status='processing' AND NEW.dispatch_status NOT IN ('processing','pending','dispatched','failed'))
  OR OLD.dispatch_status IN ('dispatched','failed')
  OR (NEW.dispatch_status='processing' AND (
    unixepoch(NEW.lease_expires_at)>unixepoch(NEW.updated_at)+90
    OR (OLD.dispatch_status!='processing' AND (
      NEW.dispatch_attempts!=OLD.dispatch_attempts+1
      OR NEW.lease_generation!=OLD.lease_generation+1))
    OR (OLD.dispatch_status='processing' AND NOT (
      (NEW.dispatch_attempts=OLD.dispatch_attempts
        AND NEW.lease_generation=OLD.lease_generation
        AND NEW.claim_token=OLD.claim_token AND NEW.lease_owner_id=OLD.lease_owner_id
        AND unixepoch(NEW.lease_expires_at)>=unixepoch(OLD.lease_expires_at))
      OR (unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)
        AND NEW.dispatch_attempts=OLD.dispatch_attempts+1
        AND NEW.lease_generation=OLD.lease_generation+1
        AND NEW.claim_token!=OLD.claim_token)
    ))
  ))
BEGIN SELECT RAISE(ABORT,'experiment_outbox_state_invalid'); END;
CREATE TRIGGER trg_experiment_outbox_delete_prd075
BEFORE DELETE ON experiment_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_outbox_immutable'); END;
CREATE TRIGGER trg_experiment_outbox_rowid_prd075
AFTER INSERT ON experiment_event_outbox FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'experiment_outbox_immutable'); END;

CREATE TRIGGER trg_experiment_scheduler_owner_insert_prd075
BEFORE INSERT ON experiment_scheduler_tenants FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.lease_generation!=0 OR NEW.lease_token IS NOT NULL
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
  OR NEW.next_run_at!=NEW.created_at OR NEW.updated_at!=NEW.created_at
  OR EXISTS(SELECT 1 FROM experiment_scheduler_tenants current
    WHERE current.tenant_id=NEW.tenant_id OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'experiment_scheduler_owner_mismatch'); END;
CREATE TRIGGER trg_experiment_scheduler_update_prd075
BEFORE UPDATE ON experiment_scheduler_tenants FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at
  OR NOT (
    (NEW.lease_generation=OLD.lease_generation
      AND NEW.lease_token IS OLD.lease_token
      AND NEW.lease_owner_id IS OLD.lease_owner_id
      AND NEW.lease_expires_at IS OLD.lease_expires_at)
    OR (NEW.lease_generation=OLD.lease_generation
      AND OLD.lease_token IS NOT NULL AND OLD.lease_owner_id IS NOT NULL
      AND OLD.lease_expires_at IS NOT NULL
      AND NEW.lease_token IS NULL AND NEW.lease_owner_id IS NULL
      AND NEW.lease_expires_at IS NULL)
    OR (NEW.lease_generation=OLD.lease_generation+1
      AND (OLD.lease_token IS NULL
        OR unixepoch(OLD.lease_expires_at)<=unixepoch('now'))
      AND NEW.lease_token IS NOT NULL AND NEW.lease_owner_id IS NOT NULL
      AND unixepoch(NEW.lease_expires_at)>unixepoch('now')
      AND unixepoch(NEW.lease_expires_at)<=unixepoch('now')+300)
  )
BEGIN SELECT RAISE(ABORT,'experiment_scheduler_state_invalid'); END;

-- Existing mutable parents may not rewrite a frozen request scope or active arm configuration.
CREATE TRIGGER trg_gateway_claims_scope_update_prd075
BEFORE UPDATE ON gateway_request_claims FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM gateway_request_scope_claims scope
  WHERE scope.tenant_id=OLD.tenant_id AND scope.request_id=OLD.request_id)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;
CREATE TRIGGER trg_gateway_claims_scope_delete_prd075
BEFORE DELETE ON gateway_request_claims FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM gateway_request_scope_claims scope
  WHERE scope.tenant_id=OLD.tenant_id AND scope.request_id=OLD.request_id)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;

CREATE TRIGGER trg_virtual_keys_scope_update_prd075
BEFORE UPDATE ON virtual_keys FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM gateway_request_scope_claims scope
    WHERE scope.tenant_id=OLD.tenant_id AND scope.virtual_key_id=OLD.virtual_key_id)
  AND (NEW.rowid!=OLD.rowid OR NEW.virtual_key_id!=OLD.virtual_key_id
    OR NEW.tenant_id!=OLD.tenant_id OR NEW.project_id IS NOT OLD.project_id
    OR NEW.application_id IS NOT OLD.application_id
    OR NEW.environment_id IS NOT OLD.environment_id OR NEW.environment!=OLD.environment)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;
CREATE TRIGGER trg_virtual_keys_scope_delete_prd075
BEFORE DELETE ON virtual_keys FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM gateway_request_scope_claims scope
  WHERE scope.tenant_id=OLD.tenant_id AND scope.virtual_key_id=OLD.virtual_key_id)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;

CREATE TRIGGER trg_models_experiment_arm_update_prd075
BEFORE UPDATE ON models FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
      AND OLD.model_id IN (experiment.model_a_id,experiment.model_b_id))
  AND (NEW.rowid!=OLD.rowid OR NEW.model_id!=OLD.model_id OR NEW.tenant_id!=OLD.tenant_id
    OR NEW.provider_id!=OLD.provider_id OR NEW.upstream_model_name!=OLD.upstream_model_name
    OR NEW.capabilities!=OLD.capabilities OR NEW.context_window!=OLD.context_window
    OR NEW.status!=OLD.status OR NEW.provider_model_id IS NOT OLD.provider_model_id)
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;
CREATE TRIGGER trg_models_experiment_arm_delete_prd075
BEFORE DELETE ON models FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
  WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
    AND OLD.model_id IN (experiment.model_a_id,experiment.model_b_id))
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;

CREATE TRIGGER trg_providers_experiment_arm_update_prd075
BEFORE UPDATE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
      AND OLD.provider_id IN (experiment.model_a_provider_id,experiment.model_b_provider_id))
  AND (NEW.rowid!=OLD.rowid OR NEW.provider_id!=OLD.provider_id OR NEW.tenant_id!=OLD.tenant_id
    OR NEW.provider_code!=OLD.provider_code OR NEW.status!=OLD.status
    OR NEW.provider_type!=OLD.provider_type OR NEW.base_url!=OLD.base_url
    OR NEW.auth_type!=OLD.auth_type)
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;
CREATE TRIGGER trg_providers_experiment_arm_delete_prd075
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
  WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
    AND OLD.provider_id IN (experiment.model_a_provider_id,experiment.model_b_provider_id))
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;

CREATE TRIGGER trg_aliases_experiment_scope_update_prd075
BEFORE UPDATE ON model_aliases FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
      AND experiment.model_alias_id=OLD.model_alias_id)
  AND (NEW.rowid!=OLD.rowid OR NEW.model_alias_id!=OLD.model_alias_id
    OR NEW.tenant_id!=OLD.tenant_id OR NEW.environment_id IS NOT OLD.environment_id
    OR NEW.status!=OLD.status OR NEW.alias!=OLD.alias)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;
CREATE TRIGGER trg_aliases_experiment_scope_delete_prd075
BEFORE DELETE ON model_aliases FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
  WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
    AND experiment.model_alias_id=OLD.model_alias_id)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;

CREATE TRIGGER trg_projects_experiment_scope_update_prd075
BEFORE UPDATE ON projects FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
      AND experiment.project_id=OLD.project_id)
  AND (NEW.rowid!=OLD.rowid OR NEW.project_id!=OLD.project_id
    OR NEW.tenant_id!=OLD.tenant_id OR NEW.status!=OLD.status)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;
CREATE TRIGGER trg_environments_experiment_scope_update_prd075
BEFORE UPDATE ON environments FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
      AND experiment.environment_id=OLD.environment_id)
  AND (NEW.rowid!=OLD.rowid OR NEW.environment_id!=OLD.environment_id
    OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.status!=OLD.status)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;

CREATE TRIGGER trg_routing_decisions_experiment_update_prd075
BEFORE UPDATE ON routing_decisions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
  WHERE assignment.tenant_id=OLD.tenant_id
    AND assignment.routing_decision_id=OLD.routing_decision_id)
BEGIN SELECT RAISE(ABORT,'experiment_routing_decision_immutable'); END;
CREATE TRIGGER trg_routing_decisions_experiment_delete_prd075
BEFORE DELETE ON routing_decisions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
  WHERE assignment.tenant_id=OLD.tenant_id
    AND assignment.routing_decision_id=OLD.routing_decision_id)
BEGIN SELECT RAISE(ABORT,'experiment_routing_decision_immutable'); END;

CREATE TRIGGER trg_routing_attempts_experiment_insert_prd075
BEFORE INSERT ON routing_attempts FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id)
  AND NOT EXISTS(SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id
      AND assignment.trace_id=NEW.trace_id AND assignment.model_id=NEW.model_id
      AND assignment.provider_id=NEW.provider_id AND assignment.provider_code=NEW.provider_code
      AND NEW.attempt_sequence=1 AND NEW.attempt_role='primary'
      AND NEW.trigger_type='initial' AND NEW.fallback_policy_id IS NULL
      AND NEW.fallback_policy_version IS NULL)
BEGIN SELECT RAISE(ABORT,'experiment_attempt_owner_mismatch'); END;
CREATE TRIGGER trg_routing_attempts_experiment_update_prd075
BEFORE UPDATE ON routing_attempts FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=OLD.tenant_id AND assignment.request_id=OLD.request_id)
  AND NOT EXISTS(SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id
      AND assignment.trace_id=NEW.trace_id AND assignment.model_id=NEW.model_id
      AND assignment.provider_id=NEW.provider_id AND assignment.provider_code=NEW.provider_code
      AND NEW.attempt_sequence=1 AND NEW.attempt_role='primary'
      AND NEW.trigger_type='initial' AND NEW.fallback_policy_id IS NULL
      AND NEW.fallback_policy_version IS NULL)
BEGIN SELECT RAISE(ABORT,'experiment_attempt_owner_mismatch'); END;

CREATE TRIGGER trg_stream_sessions_experiment_insert_prd075
BEFORE INSERT ON stream_sessions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id)
  AND NOT EXISTS(SELECT 1 FROM experiment_assignments assignment
    JOIN gateway_request_scope_claims scope
      ON scope.tenant_id=assignment.tenant_id AND scope.request_id=assignment.request_id
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id
      AND assignment.trace_id=NEW.trace_id AND assignment.virtual_key_id=NEW.virtual_key_id
      AND assignment.model_id=NEW.model_id AND assignment.provider_id=NEW.provider_id
      AND assignment.provider_code=NEW.provider_code
      AND scope.project_id IS NEW.project_id AND scope.application_id IS NEW.application_id
      AND scope.environment_id IS NEW.environment_id
      AND scope.environment_code IS NEW.environment_code)
BEGIN SELECT RAISE(ABORT,'experiment_stream_owner_mismatch'); END;
CREATE TRIGGER trg_stream_sessions_experiment_update_prd075
BEFORE UPDATE ON stream_sessions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=OLD.tenant_id AND assignment.request_id=OLD.request_id)
  AND NOT EXISTS(SELECT 1 FROM experiment_assignments assignment
    JOIN gateway_request_scope_claims scope
      ON scope.tenant_id=assignment.tenant_id AND scope.request_id=assignment.request_id
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id
      AND assignment.trace_id=NEW.trace_id AND assignment.virtual_key_id=NEW.virtual_key_id
      AND assignment.model_id=NEW.model_id AND assignment.provider_id=NEW.provider_id
      AND assignment.provider_code=NEW.provider_code
      AND scope.project_id IS NEW.project_id AND scope.application_id IS NEW.application_id
      AND scope.environment_id IS NEW.environment_id
      AND scope.environment_code IS NEW.environment_code)
BEGIN SELECT RAISE(ABORT,'experiment_stream_owner_mismatch'); END;

CREATE TRIGGER trg_request_logs_experiment_insert_prd075
BEFORE INSERT ON request_logs FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id)
  AND NOT EXISTS(SELECT 1 FROM experiment_assignments assignment
    JOIN gateway_request_scope_claims scope
      ON scope.tenant_id=assignment.tenant_id AND scope.request_id=assignment.request_id
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id
      AND assignment.trace_id=NEW.trace_id AND assignment.virtual_key_id=NEW.virtual_key_id
      AND (NEW.model_id IS NULL OR NEW.model_id=assignment.model_id)
      AND (NEW.provider_id IS NULL OR NEW.provider_id=assignment.provider_id)
      AND (NEW.provider_code IS NULL OR NEW.provider_code=assignment.provider_code)
      AND (NEW.project_id IS NULL OR NEW.project_id IS scope.project_id)
      AND (NEW.application_id IS NULL OR NEW.application_id IS scope.application_id)
      AND (NEW.environment_id IS NULL OR NEW.environment_id IS scope.environment_id)
      AND (NEW.environment_code IS NULL OR NEW.environment_code IS scope.environment_code))
BEGIN SELECT RAISE(ABORT,'experiment_request_log_owner_mismatch'); END;
CREATE TRIGGER trg_request_logs_experiment_update_prd075
BEFORE UPDATE ON request_logs FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
    WHERE assignment.tenant_id=OLD.tenant_id AND assignment.request_id=OLD.request_id)
  AND NOT EXISTS(SELECT 1 FROM experiment_assignments assignment
    JOIN gateway_request_scope_claims scope
      ON scope.tenant_id=assignment.tenant_id AND scope.request_id=assignment.request_id
    WHERE assignment.tenant_id=NEW.tenant_id AND assignment.request_id=NEW.request_id
      AND assignment.trace_id=NEW.trace_id AND assignment.virtual_key_id=NEW.virtual_key_id
      AND (NEW.model_id IS NULL OR NEW.model_id=assignment.model_id)
      AND (NEW.provider_id IS NULL OR NEW.provider_id=assignment.provider_id)
      AND (NEW.provider_code IS NULL OR NEW.provider_code=assignment.provider_code)
      AND (NEW.project_id IS NULL OR NEW.project_id IS scope.project_id)
      AND (NEW.application_id IS NULL OR NEW.application_id IS scope.application_id)
      AND (NEW.environment_id IS NULL OR NEW.environment_id IS scope.environment_id)
      AND (NEW.environment_code IS NULL OR NEW.environment_code IS scope.environment_code))
BEGIN SELECT RAISE(ABORT,'experiment_request_log_owner_mismatch'); END;

-- AuditLog 沿用 0067 的 17 列、rowid、索引和全部旧 owner 约束，只扩展实验创建动作。
-- 换表前后的双向差集和 NULL 分布继续逐值验证，历史审计事实不得漂移。
DROP TABLE IF EXISTS audit_logs_prd075_new;
CREATE TABLE audit_logs_prd075_new (
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
      'policy_template_application','session_risk','experiment'
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
INSERT INTO audit_logs_prd075_new(
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
CREATE TEMP TABLE prd075_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 复制后逐值核对行数与 17 列内容，任何差异都在删除旧审计表前失败关闭。
INSERT INTO prd075_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd075_new))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd075_new WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd075_new WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd075_new WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd075_new
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd075_new
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd075_audit_copy_guard;

-- legacy_alter_table 保持四个已核验的外部 owner trigger 原样指向正式表名；旧表本地
-- trigger/index 随 DROP 移除，重命名后按冻结定义逐项恢复。
DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后替换正式审计表，确保历史治理证据完整保真。
ALTER TABLE audit_logs_prd075_new RENAME TO audit_logs;
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

CREATE UNIQUE INDEX idx_audit_logs_experiment_owner_unique
  ON audit_logs(tenant_id,object_id) WHERE action='experiment.created';

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
    OR (
      NEW.action='experiment.created' AND current.action=NEW.action
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
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

-- 清理临时迁移守卫 temp.prd075_up_final_guard；该对象不承载租户业务数据。
DROP TABLE IF EXISTS temp.prd075_up_final_guard;
CREATE TEMP TABLE prd075_up_final_guard(
  issue_count INTEGER NOT NULL CONSTRAINT prd075_up_final_guard_ok CHECK(issue_count=0)
);
-- 迁移 prd075_up_final_guard 的模板、租户投影或守卫事实；写入集合由事务和精确条件限定。
INSERT INTO prd075_up_final_guard(issue_count)
SELECT CASE WHEN NOT EXISTS(
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
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN (
      'model_experiments','gateway_request_scope_claims','experiment_assignments',
      'experiment_request_executions','experiment_request_outcomes',
      'experiment_usage_settlements','experiment_feedback_attributions',
      'experiment_results','experiment_events','experiment_event_outbox',
      'experiment_scheduler_tenants'))=11 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name LIKE '%prd075')=17 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE '%prd075')=72 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name='experiment_assignments'
        AND lower(replace(replace(sql,char(10),''),' ','')) LIKE
          '%referencesexperiment_request_executions(tenant_id,assignment_id,execution_id)deferrableinitiallydeferred%'
        AND lower(replace(replace(sql,char(10),''),' ','')) LIKE
          '%referencesexperiment_usage_settlements(tenant_id,assignment_id,usage_settlement_id)deferrableinitiallydeferred%')=1
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd075_up_final_guard;
DROP TABLE prd075_expected_role_permissions;
DROP TABLE prd075_expected_permissions;
COMMIT;
PRAGMA foreign_keys = ON;
PRAGMA legacy_alter_table = OFF;
