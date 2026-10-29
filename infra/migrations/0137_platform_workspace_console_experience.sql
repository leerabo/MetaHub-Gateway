-- PRD-145 separates Platform, Workspace, and GlobalAccount console experience facts.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd145_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd145_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0136' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'global_accounts','global_sessions','account_identity_links',
      'workspace_tenant_bindings','workspace_memberships','platform_operators',
      'platform_operator_role_bindings','user_preferences','user_preference_events',
      'assistant_queries','assistant_audits','approval_tasks','edge_deployments'))=13
    THEN 0 ELSE 1 END;
DROP TABLE prd145_up_guard;

CREATE TABLE platform_console_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.console.overview.read','platform.console.assistant.query',
    'platform.console.approval.read')),
  introduced_in_migration TEXT NOT NULL CHECK(introduced_in_migration='0137')
);
INSERT INTO platform_console_permission_extensions VALUES
  ('platform.console.overview.read','0137'),
  ('platform.console.assistant.query','0137'),
  ('platform.console.approval.read','0137');

CREATE TABLE platform_console_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security','platform_billing',
    'platform_support','platform_compliance','release_manager')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES platform_console_permission_extensions(permission_code)
);
INSERT INTO platform_console_role_permissions VALUES
  ('platform_owner','platform.console.overview.read'),
  ('platform_owner','platform.console.assistant.query'),
  ('platform_owner','platform.console.approval.read'),
  ('platform_ops','platform.console.overview.read'),
  ('platform_ops','platform.console.assistant.query'),
  ('platform_security','platform.console.overview.read'),
  ('platform_security','platform.console.assistant.query'),
  ('platform_security','platform.console.approval.read'),
  ('platform_billing','platform.console.overview.read'),
  ('platform_billing','platform.console.assistant.query'),
  ('platform_billing','platform.console.approval.read'),
  ('platform_support','platform.console.overview.read'),
  ('platform_compliance','platform.console.approval.read'),
  ('release_manager','platform.console.approval.read');

CREATE TABLE workspace_console_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'workspace.console.overview.read','workspace.console.assistant.query',
    'workspace.console.approval.read','workspace.deployment_diagnostic.read',
    'workspace.deployment_diagnostic.audit_read')),
  introduced_in_migration TEXT NOT NULL CHECK(introduced_in_migration='0137')
);
INSERT INTO workspace_console_permission_extensions VALUES
  ('workspace.console.overview.read','0137'),
  ('workspace.console.assistant.query','0137'),
  ('workspace.console.approval.read','0137'),
  ('workspace.deployment_diagnostic.read','0137'),
  ('workspace.deployment_diagnostic.audit_read','0137');

CREATE TABLE workspace_console_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin',
    'auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_console_permission_extensions(permission_code)
);
INSERT INTO workspace_console_role_permissions VALUES
  ('owner','workspace.console.overview.read'),
  ('owner','workspace.console.assistant.query'),
  ('owner','workspace.console.approval.read'),
  ('owner','workspace.deployment_diagnostic.read'),
  ('admin','workspace.console.overview.read'),
  ('admin','workspace.console.assistant.query'),
  ('admin','workspace.console.approval.read'),
  ('admin','workspace.deployment_diagnostic.read'),
  ('developer','workspace.console.overview.read'),
  ('developer','workspace.console.assistant.query'),
  ('developer','workspace.deployment_diagnostic.read'),
  ('security_admin','workspace.console.overview.read'),
  ('security_admin','workspace.console.assistant.query'),
  ('security_admin','workspace.console.approval.read'),
  ('finance_admin','workspace.console.overview.read'),
  ('finance_admin','workspace.console.assistant.query'),
  ('finance_admin','workspace.console.approval.read'),
  ('legal_admin','workspace.console.overview.read'),
  ('legal_admin','workspace.console.assistant.query'),
  ('legal_admin','workspace.console.approval.read'),
  ('auditor','workspace.console.overview.read'),
  ('auditor','workspace.console.assistant.query'),
  ('auditor','workspace.deployment_diagnostic.audit_read'),
  ('viewer','workspace.console.overview.read');

CREATE TRIGGER trg_platform_console_permission_insert_prd145
BEFORE INSERT ON platform_console_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_console_permissions_immutable'); END;
CREATE TRIGGER trg_platform_console_permission_update_prd145
BEFORE UPDATE ON platform_console_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_console_permissions_immutable'); END;
CREATE TRIGGER trg_platform_console_permission_delete_prd145
BEFORE DELETE ON platform_console_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_console_permissions_immutable'); END;
CREATE TRIGGER trg_platform_console_role_insert_prd145
BEFORE INSERT ON platform_console_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_console_roles_immutable'); END;
CREATE TRIGGER trg_platform_console_role_update_prd145
BEFORE UPDATE ON platform_console_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_console_roles_immutable'); END;
CREATE TRIGGER trg_platform_console_role_delete_prd145
BEFORE DELETE ON platform_console_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_console_roles_immutable'); END;
CREATE TRIGGER trg_workspace_console_permission_insert_prd145
BEFORE INSERT ON workspace_console_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_console_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_console_permission_update_prd145
BEFORE UPDATE ON workspace_console_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_console_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_console_permission_delete_prd145
BEFORE DELETE ON workspace_console_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_console_permissions_immutable'); END;
CREATE TRIGGER trg_workspace_console_role_insert_prd145
BEFORE INSERT ON workspace_console_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_console_roles_immutable'); END;
CREATE TRIGGER trg_workspace_console_role_update_prd145
BEFORE UPDATE ON workspace_console_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_console_roles_immutable'); END;
CREATE TRIGGER trg_workspace_console_role_delete_prd145
BEFORE DELETE ON workspace_console_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_console_roles_immutable'); END;

CREATE TABLE platform_dashboard_projection_versions(
  platform_dashboard_projection_id TEXT PRIMARY KEY CHECK(
    length(platform_dashboard_projection_id)=36
    AND substr(platform_dashboard_projection_id,1,4)='pdp_'
    AND substr(platform_dashboard_projection_id,5) NOT GLOB '*[^0-9a-f]*'),
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  schema_version TEXT NOT NULL CHECK(schema_version='platform-dashboard-v1'),
  status TEXT NOT NULL CHECK(status IN('ready','partial','stale')),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64
    AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(platform_dashboard_projection_id,generation)
);
CREATE TABLE platform_dashboard_projection_heads(
  head_key TEXT PRIMARY KEY CHECK(head_key='platform'),
  platform_dashboard_projection_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(platform_dashboard_projection_id,generation)
    REFERENCES platform_dashboard_projection_versions(
      platform_dashboard_projection_id,generation)
);
CREATE TABLE platform_dashboard_projection_sources(
  platform_dashboard_projection_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  source_code TEXT NOT NULL CHECK(source_code IN(
    'status','observability','jobs','capacity','channel','customer_success',
    'growth','branding','provider_engineering','commercial_accounting')),
  metric_code TEXT NOT NULL CHECK(length(metric_code) BETWEEN 1 AND 80
    AND metric_code=lower(metric_code)
    AND metric_code NOT GLOB '*[^a-z0-9_.-]*'),
  metric_value INTEGER CHECK(metric_value IS NULL OR metric_value>=0),
  state_code TEXT NOT NULL CHECK(length(state_code) BETWEEN 1 AND 80
    AND state_code=lower(state_code)
    AND state_code NOT GLOB '*[^a-z0-9_.-]*'),
  action_code TEXT CHECK(action_code IS NULL OR(length(action_code) BETWEEN 1 AND 80
    AND action_code=lower(action_code)
    AND action_code NOT GLOB '*[^a-z0-9_.-]*')),
  freshness TEXT NOT NULL CHECK(freshness IN('fresh','stale','unavailable')),
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  reason_code TEXT CHECK(reason_code IS NULL OR(length(reason_code) BETWEEN 1 AND 80
    AND reason_code=lower(reason_code)
    AND reason_code NOT GLOB '*[^a-z0-9_.-]*')),
  PRIMARY KEY(platform_dashboard_projection_id,source_code,metric_code),
  FOREIGN KEY(platform_dashboard_projection_id,generation)
    REFERENCES platform_dashboard_projection_versions(
      platform_dashboard_projection_id,generation)
);
CREATE INDEX idx_platform_dashboard_sources_prd145
  ON platform_dashboard_projection_sources(
    platform_dashboard_projection_id,source_code,freshness,metric_code);

CREATE TABLE workspace_dashboard_projection_versions(
  workspace_dashboard_projection_id TEXT PRIMARY KEY CHECK(
    length(workspace_dashboard_projection_id)=36
    AND substr(workspace_dashboard_projection_id,1,4)='wdp_'
    AND substr(workspace_dashboard_projection_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  schema_version TEXT NOT NULL CHECK(schema_version='workspace-dashboard-v1'),
  status TEXT NOT NULL CHECK(status IN('ready','partial','stale')),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64
    AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,generation),
  UNIQUE(workspace_id,tenant_id,workspace_dashboard_projection_id,generation),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TABLE workspace_dashboard_projection_heads(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_dashboard_projection_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,workspace_dashboard_projection_id,generation)
    REFERENCES workspace_dashboard_projection_versions(
      workspace_id,tenant_id,workspace_dashboard_projection_id,generation)
);
CREATE TABLE workspace_dashboard_projection_sources(
  workspace_dashboard_projection_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  source_code TEXT NOT NULL CHECK(source_code IN(
    'role_tasks','gateway_health','provider_health','governance','approvals',
    'usage_cost','notifications','deployment')),
  metric_code TEXT NOT NULL CHECK(length(metric_code) BETWEEN 1 AND 80
    AND metric_code=lower(metric_code)
    AND metric_code NOT GLOB '*[^a-z0-9_.-]*'),
  metric_value INTEGER CHECK(metric_value IS NULL OR metric_value>=0),
  state_code TEXT NOT NULL CHECK(length(state_code) BETWEEN 1 AND 80
    AND state_code=lower(state_code)
    AND state_code NOT GLOB '*[^a-z0-9_.-]*'),
  action_code TEXT CHECK(action_code IS NULL OR(length(action_code) BETWEEN 1 AND 80
    AND action_code=lower(action_code)
    AND action_code NOT GLOB '*[^a-z0-9_.-]*')),
  freshness TEXT NOT NULL CHECK(freshness IN('fresh','stale','unavailable')),
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  reason_code TEXT CHECK(reason_code IS NULL OR(length(reason_code) BETWEEN 1 AND 80
    AND reason_code=lower(reason_code)
    AND reason_code NOT GLOB '*[^a-z0-9_.-]*')),
  PRIMARY KEY(workspace_dashboard_projection_id,source_code,metric_code),
  FOREIGN KEY(workspace_id,tenant_id,workspace_dashboard_projection_id,generation)
    REFERENCES workspace_dashboard_projection_versions(
      workspace_id,tenant_id,workspace_dashboard_projection_id,generation)
);
CREATE INDEX idx_workspace_dashboard_sources_prd145
  ON workspace_dashboard_projection_sources(
    workspace_id,tenant_id,workspace_dashboard_projection_id,source_code,freshness);

CREATE TABLE platform_domain_approval_queue_items(
  platform_approval_projection_id TEXT PRIMARY KEY CHECK(
    length(platform_approval_projection_id)=36
    AND substr(platform_approval_projection_id,1,4)='pdq_'
    AND substr(platform_approval_projection_id,5) NOT GLOB '*[^0-9a-f]*'),
  source_domain TEXT NOT NULL CHECK(source_domain IN(
    'channel','customer_success','growth','branding','provider_engineering',
    'commercial_accounting','security','release')),
  source_type TEXT NOT NULL CHECK(length(source_type) BETWEEN 1 AND 80
    AND source_type=lower(source_type)
    AND source_type NOT GLOB '*[^a-z0-9_.-]*'),
  source_id_digest TEXT NOT NULL CHECK(length(source_id_digest)=64
    AND source_id_digest NOT GLOB '*[^0-9a-f]*'),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  status TEXT NOT NULL CHECK(status IN('pending','approved','rejected','expired')),
  risk_band TEXT NOT NULL CHECK(risk_band IN('standard','high','critical')),
  applicant_domain TEXT NOT NULL CHECK(applicant_domain IN('platform','service')),
  applicant_pseudonym TEXT NOT NULL CHECK(length(applicant_pseudonym)=64
    AND applicant_pseudonym NOT GLOB '*[^0-9a-f]*'),
  required_permission_code TEXT NOT NULL CHECK(length(required_permission_code) BETWEEN 3 AND 120),
  summary_code TEXT NOT NULL CHECK(length(summary_code) BETWEEN 1 AND 80
    AND summary_code=lower(summary_code)
    AND summary_code NOT GLOB '*[^a-z0-9_.-]*'),
  decision_contract_code TEXT NOT NULL CHECK(length(decision_contract_code) BETWEEN 1 AND 80
    AND decision_contract_code=lower(decision_contract_code)
    AND decision_contract_code NOT GLOB '*[^a-z0-9_.-]*'),
  projection_generation INTEGER NOT NULL CHECK(projection_generation>=1),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64
    AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  deadline_at TEXT NOT NULL CHECK(unixepoch(deadline_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(source_domain,source_type,source_id_digest,source_revision)
);
CREATE INDEX idx_platform_approval_queue_prd145
  ON platform_domain_approval_queue_items(
    status,source_domain,risk_band,deadline_at,platform_approval_projection_id);

CREATE TABLE platform_domain_approval_source_cursors(
  source_domain TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  generation INTEGER NOT NULL CHECK(generation>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(source_domain,source_type)
);
CREATE TABLE platform_domain_approval_rejections(
  rejection_id TEXT PRIMARY KEY CHECK(length(rejection_id)=36
    AND substr(rejection_id,1,4)='par_'
    AND substr(rejection_id,5) NOT GLOB '*[^0-9a-f]*'),
  source_domain TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_id_digest TEXT NOT NULL CHECK(length(source_id_digest)=64),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace','service')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'owner_unmapped','actor_domain_mismatch','applicant_decider_overlap',
    'source_revision_regressed','source_schema_invalid')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(source_domain,source_type,source_id_digest,source_revision)
);

CREATE TABLE workspace_domain_approval_queue_items(
  workspace_approval_projection_id TEXT PRIMARY KEY CHECK(
    length(workspace_approval_projection_id)=36
    AND substr(workspace_approval_projection_id,1,4)='wdq_'
    AND substr(workspace_approval_projection_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_domain TEXT NOT NULL CHECK(source_domain IN(
    'budget','session_risk','billing','privacy','compliance','security')),
  source_type TEXT NOT NULL CHECK(length(source_type) BETWEEN 1 AND 80
    AND source_type=lower(source_type)
    AND source_type NOT GLOB '*[^a-z0-9_.-]*'),
  source_id_digest TEXT NOT NULL CHECK(length(source_id_digest)=64
    AND source_id_digest NOT GLOB '*[^0-9a-f]*'),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  status TEXT NOT NULL CHECK(status IN('pending','approved','rejected','expired')),
  risk_band TEXT NOT NULL CHECK(risk_band IN('standard','high','critical')),
  applicant_domain TEXT NOT NULL CHECK(applicant_domain IN('workspace','service')),
  applicant_pseudonym TEXT NOT NULL CHECK(length(applicant_pseudonym)=64
    AND applicant_pseudonym NOT GLOB '*[^0-9a-f]*'),
  required_permission_code TEXT NOT NULL CHECK(length(required_permission_code) BETWEEN 3 AND 120),
  summary_code TEXT NOT NULL CHECK(length(summary_code) BETWEEN 1 AND 80
    AND summary_code=lower(summary_code)
    AND summary_code NOT GLOB '*[^a-z0-9_.-]*'),
  decision_contract_code TEXT NOT NULL CHECK(length(decision_contract_code) BETWEEN 1 AND 80
    AND decision_contract_code=lower(decision_contract_code)
    AND decision_contract_code NOT GLOB '*[^a-z0-9_.-]*'),
  projection_generation INTEGER NOT NULL CHECK(projection_generation>=1),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64
    AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  deadline_at TEXT NOT NULL CHECK(unixepoch(deadline_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,tenant_id,source_domain,source_type,source_id_digest,source_revision)
);
CREATE INDEX idx_workspace_approval_queue_prd145
  ON workspace_domain_approval_queue_items(
    workspace_id,tenant_id,status,source_domain,risk_band,deadline_at,
    workspace_approval_projection_id);

CREATE TABLE workspace_domain_approval_source_cursors(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  source_domain TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  generation INTEGER NOT NULL CHECK(generation>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id,source_domain,source_type),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TABLE workspace_domain_approval_rejections(
  rejection_id TEXT PRIMARY KEY CHECK(length(rejection_id)=36
    AND substr(rejection_id,1,4)='war_'
    AND substr(rejection_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT,
  tenant_id TEXT,
  source_domain TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_id_digest TEXT NOT NULL CHECK(length(source_id_digest)=64),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace','service')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'owner_unmapped','actor_domain_mismatch','applicant_decider_overlap',
    'source_revision_regressed','source_schema_invalid')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  CHECK((reason_code='owner_unmapped' AND workspace_id IS NULL AND tenant_id IS NULL)
    OR(reason_code!='owner_unmapped' AND workspace_id IS NOT NULL AND tenant_id IS NOT NULL)),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(source_domain,source_type,source_id_digest,source_revision)
);

CREATE TABLE platform_assistant_query_projections(
  platform_assistant_query_projection_id TEXT PRIMARY KEY CHECK(
    length(platform_assistant_query_projection_id)=36
    AND substr(platform_assistant_query_projection_id,1,4)='paq_'
    AND substr(platform_assistant_query_projection_id,5) NOT GLOB '*[^0-9a-f]*'),
  operator_id TEXT NOT NULL,
  platform_session_id TEXT NOT NULL,
  operator_revision INTEGER NOT NULL CHECK(operator_revision>=1),
  query_type TEXT NOT NULL CHECK(query_type IN(
    'operations_summary','security_summary','billing_summary',
    'provider_summary','unknown')),
  outcome TEXT NOT NULL CHECK(outcome IN(
    'answered','partial','unsupported','permission_denied','rate_limited','failed')),
  source_types_json TEXT NOT NULL CHECK(json_valid(source_types_json)
    AND json_type(source_types_json)='array'
    AND json(source_types_json)=source_types_json),
  claim_count INTEGER NOT NULL CHECK(claim_count BETWEEN 0 AND 100),
  source_count INTEGER NOT NULL CHECK(source_count BETWEEN 0 AND 50),
  redaction_count INTEGER NOT NULL CHECK(redaction_count BETWEEN 0 AND 100),
  latency_bucket TEXT NOT NULL CHECK(latency_bucket IN(
    'lt_50ms','lt_150ms','lt_300ms','lt_1000ms','gte_1000ms')),
  permission_revision INTEGER NOT NULL CHECK(permission_revision>=1),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64
    AND permission_fingerprint NOT GLOB '*[^0-9a-f]*'),
  execution_mode TEXT NOT NULL CHECK(execution_mode='deterministic_read_model_v1'),
  failure_code TEXT CHECK(failure_code IS NULL OR(length(failure_code) BETWEEN 1 AND 80
    AND failure_code=lower(failure_code)
    AND failure_code NOT GLOB '*[^a-z0-9_.-]*')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  CHECK((outcome IN('answered','partial') AND failure_code IS NULL)
    OR(outcome NOT IN('answered','partial') AND failure_code IS NOT NULL))
);
CREATE INDEX idx_platform_assistant_queries_prd145
  ON platform_assistant_query_projections(
    operator_id,created_at DESC,platform_assistant_query_projection_id);

CREATE TABLE workspace_assistant_query_projections(
  workspace_assistant_query_projection_id TEXT PRIMARY KEY CHECK(
    length(workspace_assistant_query_projection_id)=36
    AND substr(workspace_assistant_query_projection_id,1,4)='waq_'
    AND substr(workspace_assistant_query_projection_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  workspace_session_id TEXT NOT NULL,
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  query_type TEXT NOT NULL CHECK(query_type IN(
    'operations_summary','security_summary','billing_summary',
    'provider_summary','unknown')),
  outcome TEXT NOT NULL CHECK(outcome IN(
    'answered','partial','unsupported','permission_denied','rate_limited','failed')),
  source_types_json TEXT NOT NULL CHECK(json_valid(source_types_json)
    AND json_type(source_types_json)='array'
    AND json(source_types_json)=source_types_json),
  claim_count INTEGER NOT NULL CHECK(claim_count BETWEEN 0 AND 100),
  source_count INTEGER NOT NULL CHECK(source_count BETWEEN 0 AND 50),
  redaction_count INTEGER NOT NULL CHECK(redaction_count BETWEEN 0 AND 100),
  latency_bucket TEXT NOT NULL CHECK(latency_bucket IN(
    'lt_50ms','lt_150ms','lt_300ms','lt_1000ms','gte_1000ms')),
  permission_revision INTEGER NOT NULL CHECK(permission_revision>=1),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64
    AND permission_fingerprint NOT GLOB '*[^0-9a-f]*'),
  execution_mode TEXT NOT NULL CHECK(execution_mode='deterministic_read_model_v1'),
  failure_code TEXT CHECK(failure_code IS NULL OR(length(failure_code) BETWEEN 1 AND 80
    AND failure_code=lower(failure_code)
    AND failure_code NOT GLOB '*[^a-z0-9_.-]*')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  CHECK((outcome IN('answered','partial') AND failure_code IS NULL)
    OR(outcome NOT IN('answered','partial') AND failure_code IS NOT NULL))
);
CREATE INDEX idx_workspace_assistant_queries_prd145
  ON workspace_assistant_query_projections(
    workspace_id,tenant_id,account_id,created_at DESC,
    workspace_assistant_query_projection_id);

CREATE TABLE account_preferences(
  account_preference_id TEXT PRIMARY KEY CHECK(length(account_preference_id)=36
    AND substr(account_preference_id,1,4)='acp_'
    AND substr(account_preference_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL UNIQUE,
  locale TEXT NOT NULL CHECK(locale IN('zh-CN','en-US')),
  status TEXT NOT NULL CHECK(status='active'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  source TEXT NOT NULL CHECK(source IN('legacy_backfill','identity')),
  confirmation_state TEXT NOT NULL CHECK(
    confirmation_state IN('confirmed','needs_confirmation')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id)
);
CREATE TABLE account_preference_commands(
  account_preference_command_id TEXT PRIMARY KEY CHECK(
    length(account_preference_command_id)=36
    AND substr(account_preference_command_id,1,4)='apc_'
    AND substr(account_preference_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  global_session_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action='change_locale'),
  idempotency_hmac TEXT NOT NULL CHECK(length(idempotency_hmac)=64
    AND idempotency_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  expected_revision INTEGER NOT NULL CHECK(expected_revision>=1),
  old_locale TEXT NOT NULL CHECK(old_locale IN('zh-CN','en-US')),
  new_locale TEXT NOT NULL CHECK(new_locale IN('zh-CN','en-US')),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  changed INTEGER NOT NULL CHECK(changed IN(0,1)),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,idempotency_hmac),
  CHECK((changed=0 AND old_locale=new_locale AND result_revision=expected_revision)
    OR(changed=1 AND old_locale!=new_locale AND result_revision=expected_revision+1))
);
CREATE TABLE account_preference_events(
  account_preference_event_id TEXT PRIMARY KEY CHECK(
    length(account_preference_event_id)=36
    AND substr(account_preference_event_id,1,4)='ape_'
    AND substr(account_preference_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  account_preference_command_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL CHECK(event_type='account_preference.changed'),
  preference_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=2),
  old_locale TEXT NOT NULL CHECK(old_locale IN('zh-CN','en-US')),
  new_locale TEXT NOT NULL CHECK(new_locale IN('zh-CN','en-US')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(account_preference_command_id)
    REFERENCES account_preference_commands(account_preference_command_id),
  FOREIGN KEY(preference_id) REFERENCES account_preferences(account_preference_id),
  UNIQUE(account_id,revision)
);
CREATE TABLE account_preference_event_outbox(
  account_preference_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(account_preference_event_outbox_id)=36
    AND substr(account_preference_event_outbox_id,1,4)='apo_'
    AND substr(account_preference_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_preference_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','dispatched','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
  lease_token_hmac TEXT CHECK(lease_token_hmac IS NULL OR length(lease_token_hmac)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  dispatched_at TEXT CHECK(dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(account_preference_event_id)
    REFERENCES account_preference_events(account_preference_event_id),
  CHECK((status='processing' AND lease_owner_id IS NOT NULL
      AND lease_token_hmac IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR(status!='processing' AND lease_owner_id IS NULL
      AND lease_token_hmac IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX idx_account_preference_outbox_prd145
  ON account_preference_event_outbox(status,next_attempt_at,created_at);

CREATE TABLE account_preference_source_mappings(
  account_preference_source_mapping_id TEXT PRIMARY KEY CHECK(
    length(account_preference_source_mapping_id)=36
    AND substr(account_preference_source_mapping_id,1,4)='apm_'
    AND substr(account_preference_source_mapping_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  legacy_tenant_id TEXT NOT NULL,
  legacy_user_id TEXT NOT NULL,
  legacy_preference_id TEXT NOT NULL UNIQUE,
  source_locale TEXT NOT NULL CHECK(source_locale IN('zh-CN','en-US')),
  source_updated_at TEXT NOT NULL CHECK(unixepoch(source_updated_at) IS NOT NULL),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  selection_state TEXT NOT NULL CHECK(selection_state IN('candidate','selected')),
  rule_version TEXT NOT NULL CHECK(rule_version='account-preference-backfill-v1'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(legacy_tenant_id,legacy_user_id)
    REFERENCES users(tenant_id,user_id),
  UNIQUE(account_id,legacy_tenant_id,legacy_user_id)
);

CREATE TRIGGER trg_global_account_preference_insert_prd145
AFTER INSERT ON global_accounts
BEGIN
  INSERT INTO account_preferences(
    account_preference_id,account_id,locale,status,revision,source,
    confirmation_state,created_at,updated_at)
  VALUES(
    'acp_'||substr(metahub_sha256('account-preference:'||NEW.account_id),1,32),
    NEW.account_id,'zh-CN','active',1,'identity','confirmed',
    strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;

INSERT INTO account_preferences(
  account_preference_id,account_id,locale,status,revision,source,
  confirmation_state,created_at,updated_at)
SELECT 'acp_'||substr(metahub_sha256('account-preference:'||account.account_id),1,32),
  account.account_id,
  coalesce(
    (SELECT event.new_locale
       FROM account_identity_links link
       JOIN user_preference_events event
         ON event.tenant_id=link.legacy_tenant_id
        AND event.user_id=link.legacy_user_id
      WHERE link.account_id=account.account_id AND link.status='active'
      ORDER BY event.created_at DESC,event.event_id DESC LIMIT 1),
    (SELECT preference.locale
       FROM account_identity_links link
       JOIN user_preferences preference
         ON preference.tenant_id=link.legacy_tenant_id
        AND preference.user_id=link.legacy_user_id
      WHERE link.account_id=account.account_id AND link.status='active'
      ORDER BY preference.updated_at DESC,preference.preference_id DESC LIMIT 1),
    'zh-CN'),
  'active',1,
  CASE WHEN EXISTS(
    SELECT 1 FROM account_identity_links link
    JOIN user_preferences preference
      ON preference.tenant_id=link.legacy_tenant_id
     AND preference.user_id=link.legacy_user_id
    WHERE link.account_id=account.account_id AND link.status='active')
    THEN 'legacy_backfill' ELSE 'identity' END,
  CASE WHEN NOT EXISTS(
      SELECT 1 FROM account_identity_links link
      JOIN user_preference_events event
        ON event.tenant_id=link.legacy_tenant_id
       AND event.user_id=link.legacy_user_id
      WHERE link.account_id=account.account_id AND link.status='active')
    AND 1 < (
      SELECT count(DISTINCT preference.locale)
      FROM account_identity_links link
      JOIN user_preferences preference
        ON preference.tenant_id=link.legacy_tenant_id
       AND preference.user_id=link.legacy_user_id
      WHERE link.account_id=account.account_id AND link.status='active'
        AND preference.updated_at=(
          SELECT max(latest.updated_at)
          FROM account_identity_links latest_link
          JOIN user_preferences latest
            ON latest.tenant_id=latest_link.legacy_tenant_id
           AND latest.user_id=latest_link.legacy_user_id
          WHERE latest_link.account_id=account.account_id
            AND latest_link.status='active'))
    THEN 'needs_confirmation' ELSE 'confirmed' END,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM global_accounts account;

INSERT INTO account_preference_source_mappings(
  account_preference_source_mapping_id,account_id,legacy_tenant_id,
  legacy_user_id,legacy_preference_id,source_locale,source_updated_at,
  source_sha256,selection_state,rule_version,created_at)
SELECT 'apm_'||substr(metahub_sha256(
    'account-preference-source:'||preference.preference_id),1,32),
  link.account_id,preference.tenant_id,preference.user_id,
  preference.preference_id,preference.locale,preference.updated_at,
  metahub_sha256(preference.tenant_id||':'||preference.user_id||':'||
    preference.preference_id||':'||preference.locale||':'||preference.updated_at),
  CASE WHEN preference.preference_id=(
    SELECT latest.preference_id
    FROM account_identity_links latest_link
    JOIN user_preferences latest
      ON latest.tenant_id=latest_link.legacy_tenant_id
     AND latest.user_id=latest_link.legacy_user_id
    WHERE latest_link.account_id=link.account_id AND latest_link.status='active'
    ORDER BY latest.updated_at DESC,latest.preference_id DESC LIMIT 1)
    THEN 'selected' ELSE 'candidate' END,
  'account-preference-backfill-v1',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM account_identity_links link
JOIN user_preferences preference
  ON preference.tenant_id=link.legacy_tenant_id
 AND preference.user_id=link.legacy_user_id
WHERE link.status='active';

CREATE TABLE workspace_deployment_diagnostic_versions(
  deployment_diagnostic_projection_id TEXT PRIMARY KEY CHECK(
    length(deployment_diagnostic_projection_id)=36
    AND substr(deployment_diagnostic_projection_id,1,4)='ddp_'
    AND substr(deployment_diagnostic_projection_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  projection_revision INTEGER NOT NULL CHECK(projection_revision>=1),
  deployment_kind TEXT NOT NULL CHECK(deployment_kind IN('connected','isolated')),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 40),
  release_band TEXT NOT NULL CHECK(release_band IN('current','behind','unknown')),
  compatibility_band TEXT NOT NULL CHECK(
    compatibility_band IN('compatible','attention','blocked','unknown')),
  desired_generation INTEGER NOT NULL CHECK(desired_generation>=0),
  observed_generation INTEGER NOT NULL CHECK(observed_generation>=0),
  health_state TEXT NOT NULL CHECK(
    health_state IN('healthy','degraded','offline','revoked','unknown')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 80
    AND reason_code=lower(reason_code)
    AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  action_code TEXT CHECK(action_code IS NULL OR(length(action_code) BETWEEN 1 AND 80
    AND action_code=lower(action_code)
    AND action_code NOT GLOB '*[^a-z0-9_.-]*')),
  evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64
    AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
  source_watermarks_json TEXT NOT NULL CHECK(json_valid(source_watermarks_json)
    AND json_type(source_watermarks_json)='object'
    AND json(source_watermarks_json)=source_watermarks_json),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,projection_revision),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,
    deployment_diagnostic_projection_id,projection_revision)
);
CREATE TABLE workspace_deployment_diagnostic_heads(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  deployment_diagnostic_projection_id TEXT NOT NULL UNIQUE,
  projection_revision INTEGER NOT NULL CHECK(projection_revision>=1),
  head_revision INTEGER NOT NULL CHECK(head_revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,
      deployment_diagnostic_projection_id,projection_revision)
    REFERENCES workspace_deployment_diagnostic_versions(
      workspace_id,tenant_id,edge_deployment_id,
      deployment_diagnostic_projection_id,projection_revision)
);
CREATE INDEX idx_workspace_deployment_diagnostic_prd145
  ON workspace_deployment_diagnostic_heads(workspace_id,tenant_id,head_revision);

CREATE TABLE platform_console_events(
  platform_console_event_id TEXT PRIMARY KEY CHECK(length(platform_console_event_id)=36
    AND substr(platform_console_event_id,1,4)='pce_'
    AND substr(platform_console_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'platform_dashboard.refreshed','approval.domain_rejected')),
  object_type TEXT NOT NULL CHECK(object_type IN('dashboard','approval_projection')),
  object_id TEXT NOT NULL CHECK(length(object_id) BETWEEN 1 AND 160),
  event_key_sha256 TEXT NOT NULL UNIQUE CHECK(length(event_key_sha256)=64
    AND event_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND json(payload_json)=payload_json
    AND length(payload_json)<=2048),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  CHECK((event_type='platform_dashboard.refreshed' AND object_type='dashboard')
    OR(event_type='approval.domain_rejected' AND object_type='approval_projection'))
);
CREATE TABLE platform_console_event_outbox(
  platform_console_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(platform_console_event_outbox_id)=36
    AND substr(platform_console_event_outbox_id,1,4)='pco_'
    AND substr(platform_console_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  platform_console_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','dispatched','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
  lease_token_hmac TEXT CHECK(lease_token_hmac IS NULL OR length(lease_token_hmac)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  dispatched_at TEXT CHECK(dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(platform_console_event_id)
    REFERENCES platform_console_events(platform_console_event_id),
  CHECK((status='processing' AND lease_owner_id IS NOT NULL
      AND lease_token_hmac IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR(status!='processing' AND lease_owner_id IS NULL
      AND lease_token_hmac IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX idx_platform_console_outbox_prd145
  ON platform_console_event_outbox(status,next_attempt_at,created_at);

CREATE TABLE workspace_console_events(
  workspace_console_event_id TEXT PRIMARY KEY CHECK(length(workspace_console_event_id)=36
    AND substr(workspace_console_event_id,1,4)='wce_'
    AND substr(workspace_console_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'workspace_dashboard.refreshed','approval.domain_rejected')),
  object_type TEXT NOT NULL CHECK(object_type IN('dashboard','approval_projection')),
  object_id TEXT NOT NULL CHECK(length(object_id) BETWEEN 1 AND 160),
  event_key_sha256 TEXT NOT NULL UNIQUE CHECK(length(event_key_sha256)=64
    AND event_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND json(payload_json)=payload_json
    AND length(payload_json)<=2048),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((event_type='workspace_dashboard.refreshed' AND object_type='dashboard')
    OR(event_type='approval.domain_rejected' AND object_type='approval_projection'))
);
CREATE TABLE workspace_console_event_outbox(
  workspace_console_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(workspace_console_event_outbox_id)=36
    AND substr(workspace_console_event_outbox_id,1,4)='wco_'
    AND substr(workspace_console_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_console_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','dispatched','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
  lease_token_hmac TEXT CHECK(lease_token_hmac IS NULL OR length(lease_token_hmac)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  dispatched_at TEXT CHECK(dispatched_at IS NULL OR unixepoch(dispatched_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_console_event_id)
    REFERENCES workspace_console_events(workspace_console_event_id),
  CHECK((status='processing' AND lease_owner_id IS NOT NULL
      AND lease_token_hmac IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR(status!='processing' AND lease_owner_id IS NULL
      AND lease_token_hmac IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX idx_workspace_console_outbox_prd145
  ON workspace_console_event_outbox(status,next_attempt_at,created_at);

CREATE TABLE console_experience_read_audits(
  console_experience_read_audit_id TEXT PRIMARY KEY CHECK(
    length(console_experience_read_audit_id)=36
    AND substr(console_experience_read_audit_id,1,4)='cra_'
    AND substr(console_experience_read_audit_id,5) NOT GLOB '*[^0-9a-f]*'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace','identity')),
  actor_pseudonym TEXT NOT NULL CHECK(length(actor_pseudonym)=64
    AND actor_pseudonym NOT GLOB '*[^0-9a-f]*'),
  owner_digest TEXT NOT NULL CHECK(length(owner_digest)=64
    AND owner_digest NOT GLOB '*[^0-9a-f]*'),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'platform.overview.read','workspace.overview.read',
    'platform.assistant.query','workspace.assistant.query',
    'platform.approvals.read','workspace.approvals.read',
    'identity.preferences.read','identity.preferences.change',
    'workspace.deployment_summary.read')),
  permission_revision INTEGER NOT NULL CHECK(permission_revision>=1),
  permission_fingerprint TEXT NOT NULL CHECK(length(permission_fingerprint)=64
    AND permission_fingerprint NOT GLOB '*[^0-9a-f]*'),
  projection_watermark TEXT NOT NULL CHECK(length(projection_watermark) BETWEEN 1 AND 160),
  outcome TEXT NOT NULL CHECK(outcome IN(
    'success','partial','stale','empty','permission_denied','unavailable','conflict')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE INDEX idx_console_read_audit_prd145
  ON console_experience_read_audits(
    actor_domain,action_code,created_at DESC,console_experience_read_audit_id);

CREATE TABLE console_experience_scheduler_state(
  worker_kind TEXT PRIMARY KEY CHECK(worker_kind IN(
    'platform_dashboard_refresh','workspace_dashboard_refresh',
    'platform_approval_projection','workspace_approval_projection',
    'workspace_deployment_diagnostic','platform_outbox','workspace_outbox',
    'account_preference_outbox','reconciliation','cleanup')),
  enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
  fence_generation INTEGER NOT NULL CHECK(fence_generation>=1),
  lease_owner_id TEXT,
  lease_token_hmac TEXT CHECK(lease_token_hmac IS NULL OR length(lease_token_hmac)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  cursor_value TEXT,
  last_error_code TEXT,
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((lease_owner_id IS NULL AND lease_token_hmac IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND lease_token_hmac IS NOT NULL
      AND lease_expires_at IS NOT NULL))
);
INSERT INTO console_experience_scheduler_state(
  worker_kind,enabled,fence_generation,lease_owner_id,lease_token_hmac,
  lease_expires_at,attempt_count,next_attempt_at,cursor_value,last_error_code,updated_at)
SELECT worker_kind,1,1,NULL,NULL,NULL,0,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),NULL,NULL,
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (
  SELECT 'platform_dashboard_refresh' worker_kind UNION ALL
  SELECT 'workspace_dashboard_refresh' UNION ALL
  SELECT 'platform_approval_projection' UNION ALL
  SELECT 'workspace_approval_projection' UNION ALL
  SELECT 'workspace_deployment_diagnostic' UNION ALL
  SELECT 'platform_outbox' UNION ALL
  SELECT 'workspace_outbox' UNION ALL
  SELECT 'account_preference_outbox' UNION ALL
  SELECT 'reconciliation' UNION ALL
  SELECT 'cleanup');

CREATE TABLE console_experience_migration_cursors(
  source_kind TEXT PRIMARY KEY CHECK(source_kind IN(
    'dashboard','assistant','approval','account_preference','diagnostic','cutover')),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  target_row_count INTEGER NOT NULL CHECK(target_row_count>=0),
  owner_coverage_count INTEGER NOT NULL CHECK(owner_coverage_count>=0),
  source_watermark TEXT NOT NULL CHECK(length(source_watermark) BETWEEN 1 AND 160),
  state TEXT NOT NULL CHECK(state IN('inventory','backfilled','blocked','cutover')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO console_experience_migration_cursors VALUES
  ('dashboard',0,metahub_sha256('dashboard:0'),0,0,'none','inventory',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('assistant',(SELECT COUNT(*) FROM assistant_queries),
    metahub_sha256('assistant:'||(SELECT COUNT(*) FROM assistant_queries)),0,0,
    coalesce((SELECT max(created_at) FROM assistant_queries),'none'),'inventory',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('approval',(SELECT COUNT(*) FROM approval_tasks),
    metahub_sha256('approval:'||(SELECT COUNT(*) FROM approval_tasks)),0,0,
    coalesce((SELECT max(updated_at) FROM approval_tasks),'none'),'inventory',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('account_preference',(SELECT COUNT(*) FROM user_preferences),
    metahub_sha256('account-preference:'||(SELECT COUNT(*) FROM user_preferences)),
    (SELECT COUNT(*) FROM account_preferences),
    (SELECT COUNT(DISTINCT account_id) FROM account_preferences),
    coalesce((SELECT max(updated_at) FROM user_preferences),'none'),'backfilled',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('diagnostic',(SELECT COUNT(*) FROM edge_deployments),
    metahub_sha256('diagnostic:'||(SELECT COUNT(*) FROM edge_deployments)),0,0,
    coalesce((SELECT max(updated_at) FROM edge_deployments),'none'),'inventory',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('cutover',0,metahub_sha256('cutover:0'),0,0,'none','inventory',1,
    strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE console_experience_reconciliations(
  reconciliation_id TEXT PRIMARY KEY CHECK(length(reconciliation_id)=36
    AND substr(reconciliation_id,1,4)='cer_'
    AND substr(reconciliation_id,5) NOT GLOB '*[^0-9a-f]*'),
  scope_code TEXT NOT NULL CHECK(scope_code IN(
    'platform','workspace','identity','all')),
  owner_digest TEXT NOT NULL CHECK(length(owner_digest)=64),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  target_row_count INTEGER NOT NULL CHECK(target_row_count>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64),
  outcome TEXT NOT NULL CHECK(outcome IN('matched','mismatch','blocked')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);

CREATE TRIGGER trg_platform_dashboard_version_update_prd145
BEFORE UPDATE ON platform_dashboard_projection_versions
BEGIN SELECT RAISE(ABORT,'platform_dashboard_version_immutable'); END;
CREATE TRIGGER trg_platform_dashboard_version_delete_prd145
BEFORE DELETE ON platform_dashboard_projection_versions
BEGIN SELECT RAISE(ABORT,'platform_dashboard_version_immutable'); END;
CREATE TRIGGER trg_platform_dashboard_source_update_prd145
BEFORE UPDATE ON platform_dashboard_projection_sources
BEGIN SELECT RAISE(ABORT,'platform_dashboard_source_immutable'); END;
CREATE TRIGGER trg_platform_dashboard_source_delete_prd145
BEFORE DELETE ON platform_dashboard_projection_sources
BEGIN SELECT RAISE(ABORT,'platform_dashboard_source_immutable'); END;
CREATE TRIGGER trg_workspace_dashboard_version_update_prd145
BEFORE UPDATE ON workspace_dashboard_projection_versions
BEGIN SELECT RAISE(ABORT,'workspace_dashboard_version_immutable'); END;
CREATE TRIGGER trg_workspace_dashboard_version_delete_prd145
BEFORE DELETE ON workspace_dashboard_projection_versions
BEGIN SELECT RAISE(ABORT,'workspace_dashboard_version_immutable'); END;
CREATE TRIGGER trg_workspace_dashboard_source_update_prd145
BEFORE UPDATE ON workspace_dashboard_projection_sources
BEGIN SELECT RAISE(ABORT,'workspace_dashboard_source_immutable'); END;
CREATE TRIGGER trg_workspace_dashboard_source_delete_prd145
BEFORE DELETE ON workspace_dashboard_projection_sources
BEGIN SELECT RAISE(ABORT,'workspace_dashboard_source_immutable'); END;
CREATE TRIGGER trg_platform_assistant_update_prd145
BEFORE UPDATE ON platform_assistant_query_projections
BEGIN SELECT RAISE(ABORT,'platform_assistant_projection_immutable'); END;
CREATE TRIGGER trg_platform_assistant_delete_prd145
BEFORE DELETE ON platform_assistant_query_projections
BEGIN SELECT RAISE(ABORT,'platform_assistant_projection_immutable'); END;
CREATE TRIGGER trg_workspace_assistant_update_prd145
BEFORE UPDATE ON workspace_assistant_query_projections
BEGIN SELECT RAISE(ABORT,'workspace_assistant_projection_immutable'); END;
CREATE TRIGGER trg_workspace_assistant_delete_prd145
BEFORE DELETE ON workspace_assistant_query_projections
BEGIN SELECT RAISE(ABORT,'workspace_assistant_projection_immutable'); END;
CREATE TRIGGER trg_account_preference_event_update_prd145
BEFORE UPDATE ON account_preference_events
BEGIN SELECT RAISE(ABORT,'account_preference_event_immutable'); END;
CREATE TRIGGER trg_account_preference_event_delete_prd145
BEFORE DELETE ON account_preference_events
BEGIN SELECT RAISE(ABORT,'account_preference_event_immutable'); END;
CREATE TRIGGER trg_platform_console_event_update_prd145
BEFORE UPDATE ON platform_console_events
BEGIN SELECT RAISE(ABORT,'platform_console_event_immutable'); END;
CREATE TRIGGER trg_platform_console_event_delete_prd145
BEFORE DELETE ON platform_console_events
BEGIN SELECT RAISE(ABORT,'platform_console_event_immutable'); END;
CREATE TRIGGER trg_workspace_console_event_update_prd145
BEFORE UPDATE ON workspace_console_events
BEGIN SELECT RAISE(ABORT,'workspace_console_event_immutable'); END;
CREATE TRIGGER trg_workspace_console_event_delete_prd145
BEFORE DELETE ON workspace_console_events
BEGIN SELECT RAISE(ABORT,'workspace_console_event_immutable'); END;
CREATE TRIGGER trg_console_read_audit_update_prd145
BEFORE UPDATE ON console_experience_read_audits
BEGIN SELECT RAISE(ABORT,'console_read_audit_immutable'); END;
CREATE TRIGGER trg_console_read_audit_delete_prd145
BEFORE DELETE ON console_experience_read_audits
BEGIN SELECT RAISE(ABORT,'console_read_audit_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;

CREATE TEMP TABLE prd145_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd145_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + CASE WHEN (SELECT COUNT(*) FROM platform_console_permission_extensions)=3
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_console_role_permissions)=14
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_console_permission_extensions)=5
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_console_role_permissions)=24
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM console_experience_scheduler_state)=10
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM console_experience_migration_cursors)=6
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM account_preference_source_mappings)=
      (SELECT COUNT(*) FROM user_preferences preference
       JOIN account_identity_links link
         ON link.legacy_tenant_id=preference.tenant_id
        AND link.legacy_user_id=preference.user_id
       WHERE link.status='active') THEN 0 ELSE 1 END;
DROP TABLE prd145_post_guard;
