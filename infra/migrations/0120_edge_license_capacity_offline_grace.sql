-- 建立私有 Gateway License、签名 entitlement、容量摘要和离线宽限事实。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd128_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 只允许从完整 0119 基线扩展，并确认商业、设备与删除清单依赖仍存在。
INSERT INTO prd128_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0119' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','edge_device_identities','workspace_subscriptions',
      'entitlement_snapshots','subscription_orders','platform_operators',
      'tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd128')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd128_up_guard;

-- Workspace License 权限分离安全摘要、在线刷新和 owner-only 离线导入。
CREATE TABLE workspace_edge_license_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.license.read','edge.license.refresh','edge.license.import')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-128'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=38)
);
CREATE TRIGGER trg_workspace_edge_license_permissions_insert_prd128
BEFORE INSERT ON workspace_edge_license_permission_extensions FOR EACH ROW
WHEN NEW.catalog_revision!=38 OR NEW.source_prd!='PRD-128'
BEGIN SELECT RAISE(ABORT,'edge_license_permission_catalog_invalid'); END;
CREATE TRIGGER trg_workspace_edge_license_permissions_update_prd128
BEFORE UPDATE ON workspace_edge_license_permission_extensions
BEGIN SELECT RAISE(ABORT,'edge_license_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_license_permissions_delete_prd128
BEFORE DELETE ON workspace_edge_license_permission_extensions
BEGIN SELECT RAISE(ABORT,'edge_license_permission_immutable'); END;
CREATE TABLE workspace_edge_license_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_edge_license_permission_extensions(permission_code)
);
CREATE TRIGGER trg_workspace_edge_license_roles_insert_prd128
BEFORE INSERT ON workspace_edge_license_role_permissions FOR EACH ROW
WHEN NOT(
  (NEW.role_code='owner' AND NEW.permission_code IN(
    'edge.license.read','edge.license.refresh','edge.license.import'))
  OR(NEW.role_code='admin' AND NEW.permission_code IN(
    'edge.license.read','edge.license.refresh'))
  OR(NEW.role_code IN('finance_admin','auditor') AND NEW.permission_code='edge.license.read'))
BEGIN SELECT RAISE(ABORT,'edge_license_role_permission_invalid'); END;
CREATE TRIGGER trg_workspace_edge_license_roles_update_prd128
BEFORE UPDATE ON workspace_edge_license_role_permissions
BEGIN SELECT RAISE(ABORT,'edge_license_role_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_license_roles_delete_prd128
BEFORE DELETE ON workspace_edge_license_role_permissions
BEGIN SELECT RAISE(ABORT,'edge_license_role_permission_immutable'); END;
INSERT INTO workspace_edge_license_permission_extensions VALUES
  ('edge.license.read','PRD-128',38),
  ('edge.license.refresh','PRD-128',38),
  ('edge.license.import','PRD-128',38);
INSERT INTO workspace_edge_license_role_permissions VALUES
  ('owner','edge.license.read'),('owner','edge.license.refresh'),
  ('owner','edge.license.import'),('admin','edge.license.read'),
  ('admin','edge.license.refresh'),('finance_admin','edge.license.read'),
  ('auditor','edge.license.read');

-- 平台签发和吊销只属于 billing/security 职责，不授予 support 商业写权限。
CREATE TABLE platform_edge_license_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.edge_license.read','platform.edge_license.issue',
    'platform.edge_license.revoke')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-128'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=38)
);
CREATE TRIGGER trg_platform_edge_license_permissions_insert_prd128
BEFORE INSERT ON platform_edge_license_permission_extensions FOR EACH ROW
WHEN NEW.catalog_revision!=38 OR NEW.source_prd!='PRD-128'
BEGIN SELECT RAISE(ABORT,'platform_edge_license_permission_catalog_invalid'); END;
CREATE TRIGGER trg_platform_edge_license_permissions_update_prd128
BEFORE UPDATE ON platform_edge_license_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_license_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_license_permissions_delete_prd128
BEFORE DELETE ON platform_edge_license_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_license_permission_immutable'); END;
CREATE TABLE platform_edge_license_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security','platform_billing',
    'platform_verification','platform_compliance','platform_privacy',
    'platform_support','release_manager','platform_provider_engineer','platform_channel',
    'platform_customer_success','platform_growth')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_edge_license_permission_extensions(permission_code)
);
CREATE TRIGGER trg_platform_edge_license_roles_insert_prd128
BEFORE INSERT ON platform_edge_license_role_permissions FOR EACH ROW
WHEN NOT(
  (NEW.role_code='platform_owner')
  OR(NEW.role_code='platform_billing')
  OR(NEW.role_code='platform_security' AND NEW.permission_code IN(
    'platform.edge_license.read','platform.edge_license.revoke'))
  OR(NEW.role_code='platform_ops'
    AND NEW.permission_code='platform.edge_license.read'))
BEGIN SELECT RAISE(ABORT,'platform_edge_license_role_permission_invalid'); END;
CREATE TRIGGER trg_platform_edge_license_roles_update_prd128
BEFORE UPDATE ON platform_edge_license_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_license_role_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_license_roles_delete_prd128
BEFORE DELETE ON platform_edge_license_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_license_role_permission_immutable'); END;
INSERT INTO platform_edge_license_permission_extensions VALUES
  ('platform.edge_license.read','PRD-128',38),
  ('platform.edge_license.issue','PRD-128',38),
  ('platform.edge_license.revoke','PRD-128',38);
INSERT INTO platform_edge_license_role_permissions VALUES
  ('platform_owner','platform.edge_license.read'),
  ('platform_owner','platform.edge_license.issue'),
  ('platform_owner','platform.edge_license.revoke'),
  ('platform_billing','platform.edge_license.read'),
  ('platform_billing','platform.edge_license.issue'),
  ('platform_billing','platform.edge_license.revoke'),
  ('platform_security','platform.edge_license.read'),
  ('platform_security','platform.edge_license.revoke'),
  ('platform_ops','platform.edge_license.read');

-- License head 只指向不可变 entitlement；generation、refresh 和 revocation 水位单调增长。
CREATE TABLE edge_licenses(
  edge_license_id TEXT PRIMARY KEY CHECK(length(edge_license_id)=36
    AND substr(edge_license_id,1,4)='lic_'
    AND substr(edge_license_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('provisioning','active','revoked')),
  current_entitlement_id TEXT,
  current_generation INTEGER NOT NULL CHECK(current_generation>=0),
  refresh_counter INTEGER NOT NULL CHECK(refresh_counter>=0),
  revocation_generation INTEGER NOT NULL CHECK(revocation_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  issued_by_operator_id TEXT NOT NULL,
  revoked_by_operator_id TEXT,
  revoked_reason_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(issued_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(revoked_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(current_entitlement_id) REFERENCES edge_signed_entitlements(entitlement_id)
    DEFERRABLE INITIALLY DEFERRED,
  UNIQUE(workspace_id,tenant_id,edge_license_id),
  CHECK((status='provisioning' AND current_entitlement_id IS NULL AND current_generation=0)
    OR(status IN('active','revoked') AND current_entitlement_id IS NOT NULL
      AND current_generation>=1)),
  CHECK((status!='revoked' AND revoked_by_operator_id IS NULL
      AND revoked_reason_code IS NULL AND revoked_at IS NULL)
    OR(status='revoked' AND revoked_by_operator_id IS NOT NULL
      AND revoked_reason_code IS NOT NULL AND revoked_at IS NOT NULL))
);
CREATE TRIGGER trg_edge_licenses_update_prd128
BEFORE UPDATE ON edge_licenses FOR EACH ROW
WHEN NEW.edge_license_id!=OLD.edge_license_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.issued_by_operator_id!=OLD.issued_by_operator_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NEW.current_generation<OLD.current_generation
  OR NEW.refresh_counter<OLD.refresh_counter
  OR NEW.revocation_generation<OLD.revocation_generation OR OLD.status='revoked'
BEGIN SELECT RAISE(ABORT,'edge_license_transition_invalid'); END;
CREATE TRIGGER trg_edge_licenses_delete_prd128 BEFORE DELETE ON edge_licenses
BEGIN SELECT RAISE(ABORT,'edge_license_immutable'); END;

-- 每个 SignedEntitlement 绑定商业来源、设备代次、容量、时间窗和独立签名用途域。
CREATE TABLE edge_signed_entitlements(
  entitlement_id TEXT PRIMARY KEY CHECK(length(entitlement_id)=36
    AND substr(entitlement_id,1,4)='let_'
    AND substr(entitlement_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_license_id TEXT NOT NULL,
  source_entitlement_snapshot_id TEXT NOT NULL,
  source_order_id TEXT,
  commercial_source_revision INTEGER NOT NULL CHECK(commercial_source_revision>=1),
  device_generation INTEGER NOT NULL CHECK(device_generation>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  refresh_counter INTEGER NOT NULL CHECK(refresh_counter>=generation),
  revocation_generation INTEGER NOT NULL CHECK(revocation_generation>=0),
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  node_limit INTEGER NOT NULL CHECK(node_limit BETWEEN 1 AND 100000),
  concurrent_request_limit INTEGER NOT NULL CHECK(
    concurrent_request_limit BETWEEN 1 AND 10000000),
  feature_codes_json TEXT NOT NULL CHECK(json_valid(feature_codes_json)
    AND json_type(feature_codes_json)='array' AND length(feature_codes_json)<=2048),
  envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json)
    AND json_type(envelope_json)='object' AND length(envelope_json)<=32768),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256)=64
    AND envelope_sha256 NOT GLOB '*[^0-9a-f]*'),
  signature_key_id TEXT NOT NULL CHECK(length(signature_key_id) BETWEEN 3 AND 80),
  signing_public_key_b64 TEXT NOT NULL CHECK(length(signing_public_key_b64)=43),
  signature_value TEXT NOT NULL CHECK(length(signature_value)=86),
  signature_purpose TEXT NOT NULL CHECK(
    signature_purpose='metahub.edge-license.entitlement/v1'),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  not_before TEXT NOT NULL CHECK(unixepoch(not_before) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  grace_ends_at TEXT NOT NULL CHECK(unixepoch(grace_ends_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_license_id)
    REFERENCES edge_licenses(workspace_id,tenant_id,edge_license_id),
  FOREIGN KEY(source_entitlement_snapshot_id)
    REFERENCES entitlement_snapshots(entitlement_snapshot_id),
  FOREIGN KEY(source_order_id) REFERENCES subscription_orders(order_id),
  UNIQUE(edge_license_id,generation),
  UNIQUE(edge_deployment_id,generation),
  CHECK(unixepoch(expires_at)>unixepoch(not_before)),
  CHECK(unixepoch(grace_ends_at)-unixepoch(expires_at) BETWEEN 604800 AND 2592000)
);
CREATE INDEX idx_edge_signed_entitlements_owner_prd128
  ON edge_signed_entitlements(workspace_id,tenant_id,edge_deployment_id,generation DESC);
CREATE TRIGGER trg_edge_signed_entitlements_update_prd128
BEFORE UPDATE ON edge_signed_entitlements
BEGIN SELECT RAISE(ABORT,'edge_signed_entitlement_immutable'); END;
CREATE TRIGGER trg_edge_signed_entitlements_delete_prd128
BEFORE DELETE ON edge_signed_entitlements
BEGIN SELECT RAISE(ABORT,'edge_signed_entitlement_immutable'); END;

-- CapacityGrant 以整数维度展开签名容量，禁止浮点和未知功能码。
CREATE TABLE edge_capacity_grants(
  capacity_grant_id TEXT PRIMARY KEY CHECK(length(capacity_grant_id)=36
    AND substr(capacity_grant_id,1,4)='cpg_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  entitlement_id TEXT NOT NULL,
  dimension TEXT NOT NULL CHECK(dimension IN('node','concurrent_request','feature')),
  integer_limit INTEGER CHECK(integer_limit IS NULL OR integer_limit BETWEEN 1 AND 10000000),
  feature_code TEXT CHECK(feature_code IS NULL OR feature_code IN(
    'advanced_routing','audit_export','diagnostic_support',
    'policy_enforcement','telemetry_export')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(entitlement_id) REFERENCES edge_signed_entitlements(entitlement_id),
  UNIQUE(entitlement_id,dimension,feature_code),
  CHECK((dimension='feature' AND feature_code IS NOT NULL AND integer_limit IS NULL)
    OR(dimension IN('node','concurrent_request') AND feature_code IS NULL
      AND integer_limit IS NOT NULL))
);
CREATE UNIQUE INDEX idx_edge_capacity_grants_numeric_prd128
  ON edge_capacity_grants(entitlement_id,dimension)
  WHERE dimension IN('node','concurrent_request');
CREATE UNIQUE INDEX idx_edge_capacity_grants_feature_prd128
  ON edge_capacity_grants(entitlement_id,feature_code)
  WHERE dimension='feature';
CREATE TRIGGER trg_edge_capacity_grants_update_prd128 BEFORE UPDATE ON edge_capacity_grants
BEGIN SELECT RAISE(ABORT,'edge_capacity_grant_immutable'); END;
CREATE TRIGGER trg_edge_capacity_grants_delete_prd128 BEFORE DELETE ON edge_capacity_grants
BEGIN SELECT RAISE(ABORT,'edge_capacity_grant_immutable'); END;

-- Workspace 刷新请求只排队商业重签，不返回或接受客户自报容量。
CREATE TABLE edge_license_refresh_requests(
  refresh_request_id TEXT PRIMARY KEY CHECK(length(refresh_request_id)=36
    AND substr(refresh_request_id,1,4)='lrr_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_license_id TEXT NOT NULL,
  expected_license_revision INTEGER NOT NULL CHECK(expected_license_revision>=1),
  status TEXT NOT NULL CHECK(status IN('requested','fulfilled','failed')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  requested_by_account_id TEXT NOT NULL,
  fulfilled_entitlement_id TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_license_id)
    REFERENCES edge_licenses(workspace_id,tenant_id,edge_license_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(fulfilled_entitlement_id)
    REFERENCES edge_signed_entitlements(entitlement_id)
);
CREATE TRIGGER trg_edge_license_refresh_update_prd128
BEFORE UPDATE ON edge_license_refresh_requests FOR EACH ROW
WHEN NEW.refresh_request_id!=OLD.refresh_request_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.edge_license_id!=OLD.edge_license_id
  OR NEW.expected_license_revision!=OLD.expected_license_revision
  OR NEW.reason_code!=OLD.reason_code
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.requested_at!=OLD.requested_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('fulfilled','failed')
BEGIN SELECT RAISE(ABORT,'edge_license_refresh_transition_invalid'); END;
CREATE TRIGGER trg_edge_license_refresh_delete_prd128
BEFORE DELETE ON edge_license_refresh_requests
BEGIN SELECT RAISE(ABORT,'edge_license_refresh_immutable'); END;

-- Agent ACK 只回传安全状态、水位与整数用量，不回传本地请求或配置正文。
CREATE TABLE edge_license_acknowledgements(
  license_ack_id TEXT PRIMARY KEY CHECK(length(license_ack_id)=36
    AND substr(license_ack_id,1,4)='lak_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  entitlement_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  disposition TEXT NOT NULL CHECK(disposition IN('applied','rejected')),
  decision_status TEXT NOT NULL CHECK(decision_status IN(
    'current','grace','restricted','revoked','not_yet_valid','clock_rollback')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80),
  trusted_wall_time TEXT NOT NULL CHECK(unixepoch(trusted_wall_time) IS NOT NULL),
  attempt_nonce_sha256 TEXT NOT NULL CHECK(length(attempt_nonce_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  acknowledged_at TEXT NOT NULL CHECK(unixepoch(acknowledged_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(entitlement_id) REFERENCES edge_signed_entitlements(entitlement_id),
  UNIQUE(edge_deployment_id,generation)
);
CREATE TRIGGER trg_edge_license_acks_update_prd128
BEFORE UPDATE ON edge_license_acknowledgements
BEGIN SELECT RAISE(ABORT,'edge_license_ack_immutable'); END;
CREATE TRIGGER trg_edge_license_acks_delete_prd128
BEFORE DELETE ON edge_license_acknowledgements
BEGIN SELECT RAISE(ABORT,'edge_license_ack_immutable'); END;

-- LicenseLease 只上传聚合水位用于审计，不参与 Edge 热路径准入。
CREATE TABLE edge_license_lease_reports(
  lease_report_id TEXT PRIMARY KEY CHECK(length(lease_report_id)=36
    AND substr(lease_report_id,1,4)='lrp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  entitlement_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  active_node_units INTEGER NOT NULL CHECK(active_node_units BETWEEN 0 AND 100000),
  active_request_units INTEGER NOT NULL CHECK(
    active_request_units BETWEEN 0 AND 10000000),
  denied_count INTEGER NOT NULL CHECK(denied_count BETWEEN 0 AND 1000000000),
  bucket_started_at TEXT NOT NULL CHECK(unixepoch(bucket_started_at) IS NOT NULL),
  bucket_ended_at TEXT NOT NULL CHECK(unixepoch(bucket_ended_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(entitlement_id) REFERENCES edge_signed_entitlements(entitlement_id),
  UNIQUE(edge_deployment_id,bucket_started_at),
  CHECK(unixepoch(bucket_ended_at)>unixepoch(bucket_started_at))
);
CREATE TRIGGER trg_edge_license_lease_reports_update_prd128
BEFORE UPDATE ON edge_license_lease_reports
BEGIN SELECT RAISE(ABORT,'edge_license_lease_report_immutable'); END;
CREATE TRIGGER trg_edge_license_lease_reports_delete_prd128
BEFORE DELETE ON edge_license_lease_reports
BEGIN SELECT RAISE(ABORT,'edge_license_lease_report_immutable'); END;

-- LicenseAudit 与 outbox 只保存固定事件和摘要，不保存账单 PII 或签名私钥。
CREATE TABLE edge_license_audits(
  license_audit_id TEXT PRIMARY KEY CHECK(length(license_audit_id)=36
    AND substr(license_audit_id,1,4)='lau_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_license_id TEXT NOT NULL,
  entitlement_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'license.issued','license.refreshed','license.revoked','license.expiring',
    'license.grace_started','license.capacity_exceeded')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace','agent','system')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_license_id)
    REFERENCES edge_licenses(workspace_id,tenant_id,edge_license_id),
  FOREIGN KEY(entitlement_id) REFERENCES edge_signed_entitlements(entitlement_id),
  UNIQUE(workspace_id,tenant_id,license_audit_id)
);
CREATE TRIGGER trg_edge_license_audits_update_prd128 BEFORE UPDATE ON edge_license_audits
BEGIN SELECT RAISE(ABORT,'edge_license_audit_immutable'); END;
CREATE TRIGGER trg_edge_license_audits_delete_prd128 BEFORE DELETE ON edge_license_audits
BEGIN SELECT RAISE(ABORT,'edge_license_audit_immutable'); END;
CREATE TABLE edge_license_outbox(
  license_outbox_id TEXT PRIMARY KEY CHECK(length(license_outbox_id)=36
    AND substr(license_outbox_id,1,4)='lou_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  license_audit_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','billing','security')),
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,license_audit_id)
    REFERENCES edge_license_audits(workspace_id,tenant_id,license_audit_id),
  UNIQUE(license_audit_id,delivery_domain)
);
CREATE TRIGGER trg_edge_license_outbox_update_prd128
BEFORE UPDATE ON edge_license_outbox FOR EACH ROW
WHEN NEW.license_outbox_id!=OLD.license_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.license_audit_id!=OLD.license_audit_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count!=OLD.attempt_count+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status='delivered'
BEGIN SELECT RAISE(ABORT,'edge_license_outbox_transition_invalid'); END;
CREATE TRIGGER trg_edge_license_outbox_delete_prd128 BEFORE DELETE ON edge_license_outbox
BEGIN SELECT RAISE(ABORT,'edge_license_outbox_immutable'); END;

-- 幂等命令表按主体域和动作隔离，禁止同 key 改写商业意图。
CREATE TABLE edge_license_action_commands(
  license_action_command_id TEXT PRIMARY KEY CHECK(length(license_action_command_id)=36
    AND substr(license_action_command_id,1,4)='lac_'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace','agent')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'edge.license.issue','edge.license.refresh','edge.license.revoke',
    'edge.license.ack','edge.license.lease_report')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id) BETWEEN 4 AND 40),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(actor_domain,actor_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_edge_license_actions_update_prd128
BEFORE UPDATE ON edge_license_action_commands
BEGIN SELECT RAISE(ABORT,'edge_license_action_immutable'); END;
CREATE TRIGGER trg_edge_license_actions_delete_prd128
BEFORE DELETE ON edge_license_action_commands
BEGIN SELECT RAISE(ABORT,'edge_license_action_immutable'); END;

-- 新 tenant 表全部登记删除清单并冻结目录 hash。
CREATE TABLE tenant_deletion_manifest_extensions_prd128(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-128'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd128_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd128_manifest_sources VALUES
  ('edge_licenses','archive_then_delete','billing'),
  ('edge_signed_entitlements','archive_then_delete','billing'),
  ('edge_capacity_grants','archive_then_delete','billing'),
  ('edge_license_refresh_requests','archive_then_delete','audit'),
  ('edge_license_acknowledgements','archive_then_delete','audit'),
  ('edge_license_lease_reports','archive_then_delete','audit'),
  ('edge_license_audits','archive_then_delete','audit'),
  ('edge_license_outbox','delete','operational');
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT table_record.name,source.disposition,source.retention_class,
  metahub_sha256(table_record.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
    FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
      AND trigger_record.tbl_name=table_record.name ORDER BY trigger_record.name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema table_record
JOIN prd128_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd128(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-128',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd128_manifest_sources ORDER BY source_table;
DROP TABLE prd128_manifest_sources;
DELETE FROM tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd128_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd128
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd128_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd128
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd128_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd128
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd128_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd128_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_license_permission_extensions)=3
      AND (SELECT COUNT(*) FROM workspace_edge_license_role_permissions)=7
      AND (SELECT COUNT(*) FROM platform_edge_license_permission_extensions)=3
      AND (SELECT COUNT(*) FROM platform_edge_license_role_permissions)=9
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd128)=8
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd128_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
