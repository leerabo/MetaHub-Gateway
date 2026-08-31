-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd139_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd139_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0130' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'partners','partner_customers','partner_commission_cursors','commission_records',
      'partner_portal_operations','partner_customer_outbox','platform_operators',
      'workspace_tenant_bindings','global_accounts','account_identity_links',
      'tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=12 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd139')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd139_up_guard;

CREATE TABLE platform_channel_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.channel.manage','platform.channel.lead.register',
    'platform.channel.attribution.request','platform.channel.attribution.approve',
    'platform.channel.statement.read')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE TABLE platform_channel_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_channel_permission_extensions(permission_code)
);
INSERT INTO platform_channel_permission_extensions VALUES
  ('platform.channel.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.channel.lead.register',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.channel.attribution.request',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.channel.attribution.approve',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.channel.statement.read',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
INSERT INTO platform_channel_role_permissions VALUES
  ('platform_owner','platform.channel.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.channel.lead.register',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.channel.attribution.request',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.channel.attribution.approve',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.channel.statement.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_channel','platform.channel.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_channel','platform.channel.lead.register',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_channel','platform.channel.attribution.request',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_channel','platform.channel.statement.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_billing','platform.channel.statement.read',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_platform_channel_permissions_insert_prd139
BEFORE INSERT ON platform_channel_permission_extensions
BEGIN SELECT RAISE(ABORT,'channel_permission_immutable'); END;
CREATE TRIGGER trg_platform_channel_permissions_update_prd139
BEFORE UPDATE ON platform_channel_permission_extensions
BEGIN SELECT RAISE(ABORT,'channel_permission_immutable'); END;
CREATE TRIGGER trg_platform_channel_permissions_delete_prd139
BEFORE DELETE ON platform_channel_permission_extensions
BEGIN SELECT RAISE(ABORT,'channel_permission_immutable'); END;
CREATE TRIGGER trg_platform_channel_roles_insert_prd139
BEFORE INSERT ON platform_channel_role_permissions
BEGIN SELECT RAISE(ABORT,'channel_permission_immutable'); END;
CREATE TRIGGER trg_platform_channel_roles_update_prd139
BEFORE UPDATE ON platform_channel_role_permissions
BEGIN SELECT RAISE(ABORT,'channel_permission_immutable'); END;
CREATE TRIGGER trg_platform_channel_roles_delete_prd139
BEFORE DELETE ON platform_channel_role_permissions
BEGIN SELECT RAISE(ABORT,'channel_permission_immutable'); END;

CREATE TABLE channel_partners(
  channel_partner_id TEXT PRIMARY KEY CHECK(length(channel_partner_id)=36
    AND substr(channel_partner_id,1,4)='par_' AND substr(channel_partner_id,5) NOT GLOB '*[^0-9a-f]*'),
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  partner_code TEXT NOT NULL UNIQUE CHECK(length(partner_code) BETWEEN 1 AND 80
    AND partner_code=lower(partner_code) AND partner_code NOT GLOB '*[^a-z0-9._-]*'),
  display_name TEXT NOT NULL CHECK(length(CAST(display_name AS BLOB)) BETWEEN 1 AND 120),
  tier_code TEXT NOT NULL CHECK(tier_code IN('registered','silver','gold','strategic')),
  region_scope TEXT NOT NULL CHECK(region_scope IN('global','cn','apac','emea','americas')),
  contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64
    AND contract_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','suspended','retired')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT,
  updated_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(owner_workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(channel_partner_id,owner_workspace_id,tenant_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at))
);
CREATE INDEX idx_channel_partners_directory_prd139
  ON channel_partners(status,tier_code,region_scope,partner_code);
CREATE TRIGGER trg_channel_partner_owner_update_prd139
BEFORE UPDATE ON channel_partners FOR EACH ROW
WHEN NEW.channel_partner_id!=OLD.channel_partner_id
  OR NEW.owner_workspace_id!=OLD.owner_workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.partner_code!=OLD.partner_code OR NEW.contract_sha256!=OLD.contract_sha256
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'channel_partner_owner_immutable'); END;
CREATE TRIGGER trg_channel_partner_delete_prd139 BEFORE DELETE ON channel_partners
BEGIN SELECT RAISE(ABORT,'channel_partner_immutable'); END;

CREATE TABLE channel_memberships(
  channel_membership_id TEXT PRIMARY KEY CHECK(length(channel_membership_id)=36
    AND substr(channel_membership_id,1,4)='chm_' AND substr(channel_membership_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  role_code TEXT NOT NULL CHECK(role_code IN('partner_owner','partner_operations','partner_finance')),
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  invited_by_operator_id TEXT,
  revoked_by_operator_id TEXT,
  invitation_sha256 TEXT NOT NULL CHECK(length(invitation_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(invited_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(revoked_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(channel_partner_id,account_id),
  CHECK((status='active' AND revoked_by_operator_id IS NULL AND revoked_at IS NULL)
    OR(status='revoked' AND revoked_at IS NOT NULL))
);
CREATE INDEX idx_channel_membership_account_prd139
  ON channel_memberships(account_id,status,channel_partner_id);
CREATE TRIGGER trg_channel_membership_owner_insert_prd139
BEFORE INSERT ON channel_memberships FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM channel_partners partner
  WHERE partner.channel_partner_id=NEW.channel_partner_id
    AND partner.owner_workspace_id=NEW.owner_workspace_id AND partner.tenant_id=NEW.tenant_id)
BEGIN SELECT RAISE(ABORT,'channel_membership_owner_invalid'); END;
CREATE TRIGGER trg_channel_membership_update_prd139
BEFORE UPDATE ON channel_memberships FOR EACH ROW
WHEN NEW.channel_membership_id!=OLD.channel_membership_id
  OR NEW.channel_partner_id!=OLD.channel_partner_id
  OR NEW.owner_workspace_id!=OLD.owner_workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.invitation_sha256!=OLD.invitation_sha256
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NOT((OLD.status='active' AND NEW.status IN('active','revoked'))
    OR(OLD.status='revoked' AND NEW.status IN('active','revoked')))
BEGIN SELECT RAISE(ABORT,'channel_membership_transition_invalid'); END;
CREATE TRIGGER trg_channel_membership_delete_prd139 BEFORE DELETE ON channel_memberships
BEGIN SELECT RAISE(ABORT,'channel_membership_immutable'); END;

CREATE TABLE channel_referral_codes(
  channel_referral_code_id TEXT PRIMARY KEY CHECK(length(channel_referral_code_id)=36
    AND substr(channel_referral_code_id,1,4)='crc_' AND substr(channel_referral_code_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  lookup_sha256 TEXT NOT NULL UNIQUE CHECK(length(lookup_sha256)=64),
  masked_suffix TEXT NOT NULL CHECK(length(masked_suffix) BETWEEN 4 AND 8),
  status TEXT NOT NULL CHECK(status IN('active','revoked','expired')),
  usage_limit INTEGER CHECK(usage_limit IS NULL OR usage_limit>=1),
  usage_count INTEGER NOT NULL CHECK(usage_count>=0),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(usage_limit IS NULL OR usage_count<=usage_limit)
);
CREATE UNIQUE INDEX uq_channel_referral_active_prd139
  ON channel_referral_codes(channel_partner_id) WHERE status='active';

CREATE TABLE channel_registered_leads(
  channel_lead_id TEXT PRIMARY KEY CHECK(length(channel_lead_id)=36
    AND substr(channel_lead_id,1,4)='cld_' AND substr(channel_lead_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  lead_code TEXT NOT NULL CHECK(length(lead_code) BETWEEN 1 AND 80),
  referral_code_id TEXT,
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 16),
  stage TEXT NOT NULL CHECK(stage IN('legacy_unresolved','registered','qualified','converted','closed')),
  contact_ciphertext TEXT,
  contact_lookup_sha256 TEXT CHECK(contact_lookup_sha256 IS NULL OR length(contact_lookup_sha256)=64),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  candidate_workspace_id TEXT,
  candidate_tenant_id TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  FOREIGN KEY(referral_code_id) REFERENCES channel_referral_codes(channel_referral_code_id),
  FOREIGN KEY(candidate_workspace_id,candidate_tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(channel_partner_id,lead_code),
  UNIQUE(channel_partner_id,source_sha256),
  CHECK((candidate_workspace_id IS NULL)=(candidate_tenant_id IS NULL))
);
CREATE INDEX idx_channel_leads_directory_prd139
  ON channel_registered_leads(channel_partner_id,stage,created_at,channel_lead_id);

CREATE TABLE channel_commission_rule_snapshots(
  channel_commission_rule_snapshot_id TEXT PRIMARY KEY CHECK(
    length(channel_commission_rule_snapshot_id)=36
    AND substr(channel_commission_rule_snapshot_id,1,4)='ccr_'
    AND substr(channel_commission_rule_snapshot_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256)=64),
  tier_code TEXT NOT NULL CHECK(tier_code IN('registered','silver','gold','strategic')),
  region_scope TEXT NOT NULL CHECK(region_scope IN('global','cn','apac','emea','americas')),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  commission_bps INTEGER NOT NULL CHECK(commission_bps BETWEEN 0 AND 10000),
  version INTEGER NOT NULL CHECK(version>=1),
  effective_from TEXT NOT NULL CHECK(unixepoch(effective_from) IS NOT NULL),
  effective_until TEXT CHECK(effective_until IS NULL OR unixepoch(effective_until) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  UNIQUE(channel_partner_id,currency,version),
  CHECK(effective_until IS NULL OR unixepoch(effective_until)>unixepoch(effective_from))
);
CREATE TRIGGER trg_channel_rule_update_prd139 BEFORE UPDATE ON channel_commission_rule_snapshots
BEGIN SELECT RAISE(ABORT,'channel_rule_immutable'); END;
CREATE TRIGGER trg_channel_rule_delete_prd139 BEFORE DELETE ON channel_commission_rule_snapshots
BEGIN SELECT RAISE(ABORT,'channel_rule_immutable'); END;

CREATE TABLE workspace_attributions(
  workspace_attribution_id TEXT PRIMARY KEY CHECK(length(workspace_attribution_id)=36
    AND substr(workspace_attribution_id,1,4)='wat_' AND substr(workspace_attribution_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  target_workspace_id TEXT NOT NULL,
  target_tenant_id TEXT NOT NULL,
  channel_lead_id TEXT,
  channel_commission_rule_snapshot_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending_approval','active','superseded','rejected','cancelled','expired')),
  starts_at TEXT NOT NULL CHECK(unixepoch(starts_at) IS NOT NULL),
  ends_at TEXT CHECK(ends_at IS NULL OR unixepoch(ends_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  FOREIGN KEY(target_workspace_id,target_tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(channel_lead_id) REFERENCES channel_registered_leads(channel_lead_id),
  FOREIGN KEY(channel_commission_rule_snapshot_id)
    REFERENCES channel_commission_rule_snapshots(channel_commission_rule_snapshot_id),
  UNIQUE(workspace_attribution_id,channel_partner_id,owner_workspace_id,tenant_id),
  CHECK(ends_at IS NULL OR unixepoch(ends_at)>unixepoch(starts_at))
);
CREATE UNIQUE INDEX uq_workspace_active_attribution_prd139
  ON workspace_attributions(target_workspace_id,target_tenant_id) WHERE status='active';
CREATE INDEX idx_workspace_attribution_partner_prd139
  ON workspace_attributions(channel_partner_id,status,target_workspace_id);
CREATE TRIGGER trg_workspace_attribution_owner_insert_prd139
BEFORE INSERT ON workspace_attributions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM channel_partners partner
      WHERE partner.channel_partner_id=NEW.channel_partner_id
        AND partner.owner_workspace_id=NEW.owner_workspace_id AND partner.tenant_id=NEW.tenant_id)
  OR NOT EXISTS(SELECT 1 FROM channel_commission_rule_snapshots rule
      WHERE rule.channel_commission_rule_snapshot_id=NEW.channel_commission_rule_snapshot_id
        AND rule.channel_partner_id=NEW.channel_partner_id
        AND rule.owner_workspace_id=NEW.owner_workspace_id AND rule.tenant_id=NEW.tenant_id)
BEGIN SELECT RAISE(ABORT,'workspace_attribution_owner_invalid'); END;

CREATE TABLE attribution_changes(
  attribution_change_id TEXT PRIMARY KEY CHECK(length(attribution_change_id)=36
    AND substr(attribution_change_id,1,4)='atc_' AND substr(attribution_change_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_attribution_id TEXT NOT NULL,
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  target_workspace_id TEXT NOT NULL,
  target_tenant_id TEXT NOT NULL,
  before_partner_id TEXT,
  before_attribution_revision INTEGER CHECK(
    before_attribution_revision IS NULL OR before_attribution_revision>=1),
  after_partner_id TEXT NOT NULL,
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80),
  requested_by_operator_id TEXT NOT NULL,
  requester_permission_revision INTEGER NOT NULL CHECK(requester_permission_revision>=1),
  required_approvals INTEGER NOT NULL CHECK(required_approvals IN(1,2)),
  approval_generation INTEGER NOT NULL CHECK(approval_generation>=1),
  approval_count INTEGER NOT NULL CHECK(approval_count BETWEEN 0 AND required_approvals),
  status TEXT NOT NULL CHECK(status IN('pending','approved','rejected','cancelled','expired')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_attribution_id,channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES workspace_attributions(workspace_attribution_id,channel_partner_id,owner_workspace_id,tenant_id),
  FOREIGN KEY(target_workspace_id,target_tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id)
  ,CHECK((before_partner_id IS NULL)=(before_attribution_revision IS NULL))
);
CREATE TABLE attribution_change_approvals(
  attribution_change_approval_id TEXT PRIMARY KEY CHECK(length(attribution_change_approval_id)=36
    AND substr(attribution_change_approval_id,1,4)='aca_'
    AND substr(attribution_change_approval_id,5) NOT GLOB '*[^0-9a-f]*'),
  attribution_change_id TEXT NOT NULL,
  approval_generation INTEGER NOT NULL CHECK(approval_generation>=1),
  approver_operator_id TEXT NOT NULL,
  approver_permission_revision INTEGER NOT NULL CHECK(approver_permission_revision>=1),
  decision TEXT NOT NULL CHECK(decision IN('approved','rejected')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(attribution_change_id) REFERENCES attribution_changes(attribution_change_id),
  FOREIGN KEY(approver_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(attribution_change_id,approval_generation,approver_operator_id)
);
CREATE TRIGGER trg_attribution_approval_update_prd139 BEFORE UPDATE ON attribution_change_approvals
BEGIN SELECT RAISE(ABORT,'attribution_approval_immutable'); END;
CREATE TRIGGER trg_attribution_approval_delete_prd139 BEFORE DELETE ON attribution_change_approvals
BEGIN SELECT RAISE(ABORT,'attribution_approval_immutable'); END;

CREATE TABLE channel_statements(
  channel_statement_id TEXT PRIMARY KEY CHECK(length(channel_statement_id)=36
    AND substr(channel_statement_id,1,4)='cst_' AND substr(channel_statement_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  period_start TEXT NOT NULL CHECK(unixepoch(period_start) IS NOT NULL),
  period_end TEXT NOT NULL CHECK(unixepoch(period_end) IS NOT NULL),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  total_amount_micros INTEGER NOT NULL CHECK(total_amount_micros>=0),
  total_commission_micros INTEGER NOT NULL CHECK(total_commission_micros>=0),
  line_count INTEGER NOT NULL CHECK(line_count>=0),
  reconciliation_sha256 TEXT NOT NULL CHECK(length(reconciliation_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('imported','open','closed')),
  close_revision INTEGER NOT NULL CHECK(close_revision>=1),
  closed_at TEXT CHECK(closed_at IS NULL OR unixepoch(closed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  UNIQUE(channel_partner_id,period_start,period_end,currency),
  CHECK(unixepoch(period_end)>unixepoch(period_start)),
  CHECK((status='open' AND closed_at IS NULL) OR(status IN('imported','closed') AND closed_at IS NOT NULL))
);
CREATE TABLE channel_statement_lines(
  channel_statement_line_id TEXT PRIMARY KEY CHECK(length(channel_statement_line_id)=36
    AND substr(channel_statement_line_id,1,4)='csl_' AND substr(channel_statement_line_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_statement_id TEXT NOT NULL,
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  target_workspace_id TEXT,
  target_tenant_id TEXT,
  channel_commission_rule_snapshot_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN('ledger','legacy_commission')),
  source_reference_id TEXT NOT NULL,
  amount_micros INTEGER NOT NULL CHECK(amount_micros>=0),
  commission_amount_micros INTEGER NOT NULL CHECK(commission_amount_micros>=0),
  token_count INTEGER NOT NULL CHECK(token_count>=0),
  estimated INTEGER NOT NULL CHECK(estimated IN(0,1)),
  source_created_at TEXT NOT NULL CHECK(unixepoch(source_created_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(channel_statement_id) REFERENCES channel_statements(channel_statement_id),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  FOREIGN KEY(target_workspace_id,target_tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(channel_commission_rule_snapshot_id)
    REFERENCES channel_commission_rule_snapshots(channel_commission_rule_snapshot_id),
  UNIQUE(source_kind,source_reference_id),
  CHECK((target_workspace_id IS NULL)=(target_tenant_id IS NULL))
);
CREATE INDEX idx_channel_statement_period_prd139
  ON channel_statements(channel_partner_id,period_start DESC,currency);
CREATE TRIGGER trg_channel_statement_update_prd139
BEFORE UPDATE ON channel_statements FOR EACH ROW
WHEN OLD.status IN('imported','closed') OR NEW.close_revision!=OLD.close_revision+1
BEGIN SELECT RAISE(ABORT,'channel_statement_immutable'); END;
CREATE TRIGGER trg_channel_statement_delete_prd139 BEFORE DELETE ON channel_statements
BEGIN SELECT RAISE(ABORT,'channel_statement_immutable'); END;
CREATE TRIGGER trg_channel_statement_line_update_prd139 BEFORE UPDATE ON channel_statement_lines
BEGIN SELECT RAISE(ABORT,'channel_statement_line_immutable'); END;
CREATE TRIGGER trg_channel_statement_line_delete_prd139 BEFORE DELETE ON channel_statement_lines
BEGIN SELECT RAISE(ABORT,'channel_statement_line_immutable'); END;

CREATE TABLE channel_legacy_object_mappings(
  channel_legacy_object_mapping_id TEXT PRIMARY KEY CHECK(length(channel_legacy_object_mapping_id)=36
    AND substr(channel_legacy_object_mapping_id,1,4)='clm_'
    AND substr(channel_legacy_object_mapping_id,5) NOT GLOB '*[^0-9a-f]*'),
  source_table TEXT NOT NULL CHECK(source_table IN('partners','partner_customers',
    'partner_commission_cursors','commission_records','partner_portal_operations',
    'partner_customer_outbox')),
  source_id TEXT NOT NULL,
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK(target_kind IN('partner','lead','cursor','statement_line','audit','outbox')),
  target_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64),
  migration_generation INTEGER NOT NULL CHECK(migration_generation=1),
  result TEXT NOT NULL CHECK(result IN('migrated','sent_tombstone')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  UNIQUE(source_table,source_id),
  UNIQUE(target_kind,target_id,source_table)
);
CREATE TRIGGER trg_channel_mapping_update_prd139 BEFORE UPDATE ON channel_legacy_object_mappings
BEGIN SELECT RAISE(ABORT,'channel_mapping_immutable'); END;
CREATE TRIGGER trg_channel_mapping_delete_prd139 BEFORE DELETE ON channel_legacy_object_mappings
BEGIN SELECT RAISE(ABORT,'channel_mapping_immutable'); END;

CREATE TABLE channel_migration_cursors(
  source_table TEXT PRIMARY KEY CHECK(source_table IN('partners','partner_customers',
    'partner_commission_cursors','commission_records','partner_portal_operations',
    'partner_customer_outbox')),
  source_row_count INTEGER NOT NULL CHECK(source_row_count>=0),
  mapped_row_count INTEGER NOT NULL CHECK(mapped_row_count>=0),
  source_canonical_sha256 TEXT NOT NULL CHECK(length(source_canonical_sha256)=64),
  maximum_watermark INTEGER NOT NULL CHECK(maximum_watermark>=0),
  pending_outbox_count INTEGER NOT NULL CHECK(pending_outbox_count>=0),
  migration_generation INTEGER NOT NULL CHECK(migration_generation=1),
  status TEXT NOT NULL CHECK(status='cutover'),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK(source_row_count=mapped_row_count)
);
CREATE TRIGGER trg_channel_migration_cursor_update_prd139 BEFORE UPDATE ON channel_migration_cursors
BEGIN SELECT RAISE(ABORT,'channel_migration_cursor_immutable'); END;
CREATE TRIGGER trg_channel_migration_cursor_delete_prd139 BEFORE DELETE ON channel_migration_cursors
BEGIN SELECT RAISE(ABORT,'channel_migration_cursor_immutable'); END;

CREATE TABLE channel_action_commands(
  channel_action_command_id TEXT PRIMARY KEY CHECK(length(channel_action_command_id)=36
    AND substr(channel_action_command_id,1,4)='cac_'
    AND substr(channel_action_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  operator_id TEXT NOT NULL,
  channel_partner_id TEXT,
  owner_workspace_id TEXT,
  tenant_id TEXT,
  action_code TEXT NOT NULL CHECK(action_code IN('partner.create','partner.update',
    'membership.upsert','membership.revoke','referral.rotate','lead.register',
    'attribution.request','attribution.approve')),
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL,
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  UNIQUE(operator_id,action_code,idempotency_key_sha256),
  CHECK((channel_partner_id IS NULL)=(owner_workspace_id IS NULL)
    AND(owner_workspace_id IS NULL)=(tenant_id IS NULL))
);
CREATE TRIGGER trg_channel_action_update_prd139 BEFORE UPDATE ON channel_action_commands
BEGIN SELECT RAISE(ABORT,'channel_action_immutable'); END;
CREATE TRIGGER trg_channel_action_delete_prd139 BEFORE DELETE ON channel_action_commands
BEGIN SELECT RAISE(ABORT,'channel_action_immutable'); END;

CREATE TABLE channel_events(
  channel_event_id TEXT PRIMARY KEY CHECK(length(channel_event_id)=36
    AND substr(channel_event_id,1,4)='cev_' AND substr(channel_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN('channel.partner_changed','channel.lead_registered',
    'workspace.attribution_changed','channel.statement_closed')),
  object_id TEXT NOT NULL,
  object_revision INTEGER NOT NULL CHECK(object_revision>=1),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id),
  UNIQUE(event_type,object_id,object_revision)
);
CREATE TABLE channel_event_outbox(
  channel_event_outbox_id TEXT PRIMARY KEY CHECK(length(channel_event_outbox_id)=36
    AND substr(channel_event_outbox_id,1,4)='ceo_'
    AND substr(channel_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  channel_event_id TEXT NOT NULL UNIQUE,
  channel_partner_id TEXT NOT NULL,
  owner_workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','leased','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  next_attempt_at TEXT,
  delivered_at TEXT,
  last_error_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(channel_event_id) REFERENCES channel_events(channel_event_id),
  FOREIGN KEY(channel_partner_id,owner_workspace_id,tenant_id)
    REFERENCES channel_partners(channel_partner_id,owner_workspace_id,tenant_id)
);
CREATE INDEX idx_channel_outbox_due_prd139
  ON channel_event_outbox(status,next_attempt_at,created_at);
CREATE TRIGGER trg_channel_event_update_prd139 BEFORE UPDATE ON channel_events
BEGIN SELECT RAISE(ABORT,'channel_event_immutable'); END;
CREATE TRIGGER trg_channel_event_delete_prd139 BEFORE DELETE ON channel_events
BEGIN SELECT RAISE(ABORT,'channel_event_immutable'); END;
CREATE TRIGGER trg_channel_outbox_delete_prd139 BEFORE DELETE ON channel_event_outbox
BEGIN SELECT RAISE(ABORT,'channel_outbox_immutable'); END;

CREATE TABLE channel_scheduler_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  legacy_cutover_watermark_at TEXT NOT NULL CHECK(unixepoch(legacy_cutover_watermark_at) IS NOT NULL),
  statement_watermark_at TEXT NOT NULL CHECK(unixepoch(statement_watermark_at) IS NOT NULL),
  outbox_watermark_at TEXT NOT NULL CHECK(unixepoch(outbox_watermark_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  bounded_batch_size INTEGER NOT NULL CHECK(bounded_batch_size BETWEEN 1 AND 100),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((lease_owner_id IS NULL AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND length(lease_token_sha256)=64 AND lease_expires_at IS NOT NULL))
);
INSERT INTO channel_scheduler_state VALUES(1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),'1970-01-01T00:00:00.000Z',
  '1970-01-01T00:00:00.000Z',NULL,NULL,NULL,0,50,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_channel_scheduler_update_prd139
BEFORE UPDATE ON channel_scheduler_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'channel_scheduler_fenced'); END;
CREATE TRIGGER trg_channel_scheduler_delete_prd139 BEFORE DELETE ON channel_scheduler_state
BEGIN SELECT RAISE(ABORT,'channel_scheduler_required'); END;

-- The legacy backfill is deliberately owner-qualified. A missing or ambiguous binding/account
-- leaves a row-count mismatch in the final guard and aborts the entire migration.
INSERT INTO channel_partners(
  channel_partner_id,owner_workspace_id,tenant_id,partner_code,display_name,tier_code,
  region_scope,contract_sha256,status,revision,created_by_operator_id,updated_by_operator_id,
  created_at,updated_at)
SELECT legacy.partner_id,binding.workspace_id,legacy.tenant_id,legacy.partner_code,
  legacy.display_name,'registered','global',legacy.payload_sha256,'active',legacy.revision,
  NULL,NULL,legacy.created_at,legacy.updated_at
FROM partners legacy
JOIN workspace_tenant_bindings binding ON binding.tenant_id=legacy.tenant_id
  AND binding.status='active'
JOIN account_identity_links identity ON identity.legacy_tenant_id=legacy.tenant_id
  AND identity.legacy_user_id=legacy.created_by_user_id AND identity.status='active';

INSERT INTO channel_memberships(
  channel_membership_id,channel_partner_id,owner_workspace_id,tenant_id,account_id,
  role_code,status,revision,invited_by_operator_id,revoked_by_operator_id,
  invitation_sha256,created_at,updated_at,revoked_at)
SELECT 'chm_'||substr(metahub_sha256('legacy-membership:'||legacy.partner_id||':'||identity.account_id),1,32),
  legacy.partner_id,binding.workspace_id,legacy.tenant_id,identity.account_id,
  'partner_owner','active',1,NULL,NULL,
  metahub_sha256('legacy-invitation:'||legacy.partner_id||':'||identity.account_id),
  legacy.created_at,legacy.updated_at,NULL
FROM partners legacy
JOIN workspace_tenant_bindings binding ON binding.tenant_id=legacy.tenant_id
  AND binding.status='active'
JOIN account_identity_links identity ON identity.legacy_tenant_id=legacy.tenant_id
  AND identity.legacy_user_id=legacy.created_by_user_id AND identity.status='active';

INSERT INTO channel_commission_rule_snapshots(
  channel_commission_rule_snapshot_id,channel_partner_id,owner_workspace_id,tenant_id,
  contract_sha256,tier_code,region_scope,currency,commission_bps,version,
  effective_from,effective_until,created_at)
SELECT 'ccr_'||substr(metahub_sha256('legacy-rule:'||partner.channel_partner_id||':'||currency.code),1,32),
  partner.channel_partner_id,partner.owner_workspace_id,partner.tenant_id,partner.contract_sha256,
  partner.tier_code,partner.region_scope,currency.code,1000,1,partner.created_at,NULL,partner.created_at
FROM channel_partners partner CROSS JOIN(
  SELECT 'CNY' code UNION ALL SELECT 'USD') currency;

INSERT INTO channel_registered_leads(
  channel_lead_id,channel_partner_id,owner_workspace_id,tenant_id,lead_code,
  referral_code_id,region_code,stage,contact_ciphertext,contact_lookup_sha256,
  source_sha256,candidate_workspace_id,candidate_tenant_id,revision,
  created_by_operator_id,created_at,updated_at)
SELECT 'cld_'||substr(metahub_sha256('legacy-lead:'||customer.partner_customer_id),1,32),
  customer.partner_id,partner.owner_workspace_id,customer.tenant_id,customer.customer_code,
  NULL,'global','legacy_unresolved',NULL,NULL,customer.payload_sha256,NULL,NULL,
  customer.revision,NULL,customer.created_at,customer.updated_at
FROM partner_customers customer
JOIN channel_partners partner ON partner.channel_partner_id=customer.partner_id
  AND partner.tenant_id=customer.tenant_id;

INSERT INTO channel_statements(
  channel_statement_id,channel_partner_id,owner_workspace_id,tenant_id,period_start,
  period_end,currency,total_amount_micros,total_commission_micros,line_count,
  reconciliation_sha256,status,close_revision,closed_at,created_at,updated_at)
SELECT 'cst_'||substr(metahub_sha256('legacy-statement:'||record.partner_id||':'
    ||substr(record.source_created_at,1,7)||':'||record.currency),1,32),
  record.partner_id,partner.owner_workspace_id,record.tenant_id,
  substr(record.source_created_at,1,7)||'-01T00:00:00.000Z',
  strftime('%Y-%m-%dT%H:%M:%fZ',substr(record.source_created_at,1,7)||'-01','+1 month'),
  record.currency,sum(record.amount_micros),sum(record.commission_amount_micros),count(*),
  metahub_sha256(group_concat(record.commission_record_id||':'||record.commission_amount_micros,'|')),
  'imported',1,max(record.created_at),min(record.created_at),max(record.created_at)
FROM commission_records record
JOIN channel_partners partner ON partner.channel_partner_id=record.partner_id
  AND partner.tenant_id=record.tenant_id
GROUP BY record.partner_id,record.tenant_id,substr(record.source_created_at,1,7),record.currency;

INSERT INTO channel_statement_lines(
  channel_statement_line_id,channel_statement_id,channel_partner_id,owner_workspace_id,
  tenant_id,target_workspace_id,target_tenant_id,channel_commission_rule_snapshot_id,
  source_kind,source_reference_id,amount_micros,commission_amount_micros,token_count,
  estimated,source_created_at,created_at)
SELECT 'csl_'||substr(metahub_sha256('legacy-line:'||record.commission_record_id),1,32),
  'cst_'||substr(metahub_sha256('legacy-statement:'||record.partner_id||':'
    ||substr(record.source_created_at,1,7)||':'||record.currency),1,32),
  record.partner_id,partner.owner_workspace_id,record.tenant_id,NULL,NULL,
  'ccr_'||substr(metahub_sha256('legacy-rule:'||record.partner_id||':'||record.currency),1,32),
  'legacy_commission',record.commission_record_id,record.amount_micros,
  record.commission_amount_micros,record.total_tokens,record.estimated,
  record.source_created_at,record.created_at
FROM commission_records record
JOIN channel_partners partner ON partner.channel_partner_id=record.partner_id
  AND partner.tenant_id=record.tenant_id;

INSERT INTO channel_legacy_object_mappings
SELECT 'clm_'||substr(metahub_sha256('partners:'||partner.partner_id),1,32),
  'partners',partner.partner_id,partner.partner_id,target.owner_workspace_id,partner.tenant_id,
  'partner',partner.partner_id,metahub_sha256(partner.partner_id||':'||partner.payload_sha256),
  metahub_sha256(target.channel_partner_id||':'||target.contract_sha256),1,'migrated',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM partners partner JOIN channel_partners target ON target.channel_partner_id=partner.partner_id;
INSERT INTO channel_legacy_object_mappings
SELECT 'clm_'||substr(metahub_sha256('partner_customers:'||customer.partner_customer_id),1,32),
  'partner_customers',customer.partner_customer_id,customer.partner_id,partner.owner_workspace_id,
  customer.tenant_id,'lead','cld_'||substr(metahub_sha256('legacy-lead:'||customer.partner_customer_id),1,32),
  metahub_sha256(customer.partner_customer_id||':'||customer.payload_sha256),
  metahub_sha256('legacy-lead:'||customer.partner_customer_id),1,'migrated',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM partner_customers customer JOIN channel_partners partner
  ON partner.channel_partner_id=customer.partner_id AND partner.tenant_id=customer.tenant_id;
INSERT INTO channel_legacy_object_mappings
SELECT 'clm_'||substr(metahub_sha256('partner_commission_cursors:'||cursor.tenant_id||':'||cursor.partner_customer_id),1,32),
  'partner_commission_cursors',cursor.tenant_id||':'||cursor.partner_customer_id,
  cursor.partner_id,partner.owner_workspace_id,cursor.tenant_id,'cursor',cursor.partner_customer_id,
  metahub_sha256(cursor.partner_customer_id||':'||cursor.status||':'||cursor.generation),
  metahub_sha256(cursor.partner_customer_id||':'||cursor.last_scanned_ledger_sequence),1,'migrated',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM partner_commission_cursors cursor JOIN channel_partners partner
  ON partner.channel_partner_id=cursor.partner_id AND partner.tenant_id=cursor.tenant_id;
INSERT INTO channel_legacy_object_mappings
SELECT 'clm_'||substr(metahub_sha256('commission_records:'||record.commission_record_id),1,32),
  'commission_records',record.commission_record_id,record.partner_id,partner.owner_workspace_id,
  record.tenant_id,'statement_line','csl_'||substr(metahub_sha256('legacy-line:'||record.commission_record_id),1,32),
  metahub_sha256(record.commission_record_id||':'||record.commission_amount_micros),
  metahub_sha256('legacy-line:'||record.commission_record_id),1,'migrated',
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM commission_records record JOIN channel_partners partner
  ON partner.channel_partner_id=record.partner_id AND partner.tenant_id=record.tenant_id;
INSERT INTO channel_legacy_object_mappings
SELECT 'clm_'||substr(metahub_sha256('partner_portal_operations:'||operation.partner_operation_id),1,32),
  'partner_portal_operations',operation.partner_operation_id,operation.partner_id,
  partner.owner_workspace_id,operation.tenant_id,'audit',operation.partner_operation_id,
  operation.summary_sha256,metahub_sha256(operation.action||':'||operation.object_id||':'||operation.summary_sha256),
  1,'migrated',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM partner_portal_operations operation JOIN channel_partners partner
  ON partner.channel_partner_id=operation.partner_id AND partner.tenant_id=operation.tenant_id;
INSERT INTO channel_legacy_object_mappings
SELECT 'clm_'||substr(metahub_sha256('partner_customer_outbox:'||outbox.partner_customer_outbox_id),1,32),
  'partner_customer_outbox',outbox.partner_customer_outbox_id,outbox.partner_id,
  partner.owner_workspace_id,outbox.tenant_id,'outbox',outbox.webhook_event_id,
  outbox.payload_sha256,metahub_sha256(outbox.webhook_event_id||':'||outbox.payload_sha256||':'||outbox.status),
  1,CASE WHEN outbox.status='sent' THEN 'sent_tombstone' ELSE 'migrated' END,
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM partner_customer_outbox outbox JOIN channel_partners partner
  ON partner.channel_partner_id=outbox.partner_id AND partner.tenant_id=outbox.tenant_id;

INSERT INTO channel_migration_cursors VALUES
  ('partners',(SELECT count(*) FROM partners),(SELECT count(*) FROM channel_legacy_object_mappings WHERE source_table='partners'),
    metahub_sha256(coalesce((SELECT group_concat(partner_id||':'||payload_sha256,'|') FROM partners),'')),0,0,1,'cutover',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('partner_customers',(SELECT count(*) FROM partner_customers),(SELECT count(*) FROM channel_legacy_object_mappings WHERE source_table='partner_customers'),
    metahub_sha256(coalesce((SELECT group_concat(partner_customer_id||':'||payload_sha256,'|') FROM partner_customers),'')),0,0,1,'cutover',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('partner_commission_cursors',(SELECT count(*) FROM partner_commission_cursors),(SELECT count(*) FROM channel_legacy_object_mappings WHERE source_table='partner_commission_cursors'),
    metahub_sha256(coalesce((SELECT group_concat(partner_customer_id||':'||status||':'||generation,'|') FROM partner_commission_cursors),'')),
    coalesce((SELECT max(last_scanned_ledger_sequence) FROM partner_commission_cursors),0),0,1,'cutover',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('commission_records',(SELECT count(*) FROM commission_records),(SELECT count(*) FROM channel_legacy_object_mappings WHERE source_table='commission_records'),
    metahub_sha256(coalesce((SELECT group_concat(commission_record_id||':'||commission_amount_micros,'|') FROM commission_records),'')),
    coalesce((SELECT max(commission_record_sequence) FROM commission_records),0),0,1,'cutover',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('partner_portal_operations',(SELECT count(*) FROM partner_portal_operations),(SELECT count(*) FROM channel_legacy_object_mappings WHERE source_table='partner_portal_operations'),
    metahub_sha256(coalesce((SELECT group_concat(partner_operation_id||':'||summary_sha256,'|') FROM partner_portal_operations),'')),0,0,1,'cutover',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('partner_customer_outbox',(SELECT count(*) FROM partner_customer_outbox),(SELECT count(*) FROM channel_legacy_object_mappings WHERE source_table='partner_customer_outbox'),
    metahub_sha256(coalesce((SELECT group_concat(partner_customer_outbox_id||':'||payload_sha256||':'||status,'|') FROM partner_customer_outbox),'')),
    coalesce((SELECT max(generation) FROM partner_customer_outbox),0),
    (SELECT count(*) FROM partner_customer_outbox WHERE status IN('pending','leased','failed')),1,'cutover',strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TRIGGER trg_legacy_partners_write_prd139 BEFORE INSERT ON partners
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partners_update_prd139 BEFORE UPDATE ON partners
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partners_delete_prd139 BEFORE DELETE ON partners
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_customers_write_prd139 BEFORE INSERT ON partner_customers
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_customers_update_prd139 BEFORE UPDATE ON partner_customers
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_customers_delete_prd139 BEFORE DELETE ON partner_customers
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_cursors_write_prd139 BEFORE INSERT ON partner_commission_cursors
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_cursors_update_prd139 BEFORE UPDATE ON partner_commission_cursors
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_cursors_delete_prd139 BEFORE DELETE ON partner_commission_cursors
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_commissions_write_prd139 BEFORE INSERT ON commission_records
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_commissions_update_prd139 BEFORE UPDATE ON commission_records
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_commissions_delete_prd139 BEFORE DELETE ON commission_records
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_operations_write_prd139 BEFORE INSERT ON partner_portal_operations
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_operations_update_prd139 BEFORE UPDATE ON partner_portal_operations
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_operations_delete_prd139 BEFORE DELETE ON partner_portal_operations
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_outbox_write_prd139 BEFORE INSERT ON partner_customer_outbox
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_outbox_update_prd139 BEFORE UPDATE ON partner_customer_outbox
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_outbox_delete_prd139 BEFORE DELETE ON partner_customer_outbox
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd139(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-139'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd139_manifest_sources(
  source_table TEXT PRIMARY KEY,disposition TEXT NOT NULL,retention_class TEXT NOT NULL);
INSERT INTO prd139_manifest_sources VALUES
  ('channel_partners','archive_then_delete','operational'),
  ('channel_memberships','archive_then_delete','operational'),
  ('channel_referral_codes','archive_then_delete','operational'),
  ('channel_registered_leads','archive_then_delete','operational'),
  ('channel_commission_rule_snapshots','archive_then_delete','billing'),
  ('workspace_attributions','archive_then_delete','billing'),
  ('attribution_changes','archive_then_delete','audit'),
  ('channel_statements','archive_then_delete','billing'),
  ('channel_statement_lines','archive_then_delete','billing'),
  ('channel_legacy_object_mappings','archive_then_delete','audit'),
  ('channel_action_commands','archive_then_delete','audit'),
  ('channel_events','archive_then_delete','audit'),
  ('channel_event_outbox','archive_then_delete','audit');
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
FROM sqlite_schema table_record JOIN prd139_manifest_sources source
  ON source.source_table=table_record.name WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd139
SELECT source_table,'tenant_id','PRD-139',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd139_manifest_sources ORDER BY source_table;
DROP TABLE prd139_manifest_sources;
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
CREATE TRIGGER trg_tenant_deletion_manifest_prd139_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd139
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd139_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd139
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd139_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd139
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd139_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd139_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_channel_permission_extensions)=5
      AND (SELECT COUNT(*) FROM platform_channel_role_permissions)=10
      AND (SELECT COUNT(*) FROM channel_migration_cursors)=6
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd139)=13
      AND (SELECT COUNT(*) FROM channel_partners)=(SELECT COUNT(*) FROM partners)
      AND (SELECT COUNT(*) FROM channel_memberships)=(SELECT COUNT(*) FROM partners)
      AND (SELECT COUNT(*) FROM channel_registered_leads)=(SELECT COUNT(*) FROM partner_customers)
      AND (SELECT COUNT(*) FROM channel_statement_lines)=(SELECT COUNT(*) FROM commission_records)
      AND (SELECT COUNT(*) FROM channel_legacy_object_mappings)=
        ((SELECT COUNT(*) FROM partners)+(SELECT COUNT(*) FROM partner_customers)
        +(SELECT COUNT(*) FROM partner_commission_cursors)+(SELECT COUNT(*) FROM commission_records)
        +(SELECT COUNT(*) FROM partner_portal_operations)+(SELECT COUNT(*) FROM partner_customer_outbox))
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd139_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
