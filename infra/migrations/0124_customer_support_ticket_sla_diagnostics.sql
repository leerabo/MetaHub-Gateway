-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd132_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd132_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0123' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','workspace_memberships','global_accounts','platform_operators',
      'uploaded_files','tenant_deletion_table_manifest'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd132')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd132_up_guard;

CREATE TABLE workspace_customer_support_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'support.ticket.read','support.ticket.create','support.ticket.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-132'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=44)
);
CREATE TABLE workspace_customer_support_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_customer_support_permission_extensions(permission_code)
);
INSERT INTO workspace_customer_support_permission_extensions VALUES
  ('support.ticket.read','PRD-132',44),
  ('support.ticket.create','PRD-132',44),
  ('support.ticket.manage','PRD-132',44);
INSERT INTO workspace_customer_support_role_permissions VALUES
  ('owner','support.ticket.read'),('owner','support.ticket.create'),
  ('owner','support.ticket.manage'),
  ('admin','support.ticket.read'),('admin','support.ticket.create'),
  ('admin','support.ticket.manage'),
  ('developer','support.ticket.read'),('developer','support.ticket.create'),
  ('security_admin','support.ticket.read'),('security_admin','support.ticket.create'),
  ('finance_admin','support.ticket.read'),('finance_admin','support.ticket.create'),
  ('legal_admin','support.ticket.read'),('legal_admin','support.ticket.create'),
  ('auditor','support.ticket.read'),('viewer','support.ticket.read');
CREATE TRIGGER trg_workspace_customer_support_permissions_insert_prd132
BEFORE INSERT ON workspace_customer_support_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_workspace_customer_support_permissions_update_prd132
BEFORE UPDATE ON workspace_customer_support_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_workspace_customer_support_permissions_delete_prd132
BEFORE DELETE ON workspace_customer_support_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_workspace_customer_support_roles_insert_prd132
BEFORE INSERT ON workspace_customer_support_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_workspace_customer_support_roles_update_prd132
BEFORE UPDATE ON workspace_customer_support_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_workspace_customer_support_roles_delete_prd132
BEFORE DELETE ON workspace_customer_support_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_customer_support_permission_immutable'); END;

CREATE TABLE platform_customer_support_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.support.ticket.read','platform.support.ticket.manage','platform.support.sla.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-132'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=44)
);
CREATE TABLE platform_customer_support_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('platform_owner','platform_support')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_customer_support_permission_extensions(permission_code)
);
INSERT INTO platform_customer_support_permission_extensions VALUES
  ('platform.support.ticket.read','PRD-132',44),
  ('platform.support.ticket.manage','PRD-132',44),
  ('platform.support.sla.manage','PRD-132',44);
INSERT INTO platform_customer_support_role_permissions VALUES
  ('platform_owner','platform.support.ticket.read'),
  ('platform_support','platform.support.ticket.read'),
  ('platform_support','platform.support.ticket.manage'),
  ('platform_support','platform.support.sla.manage');
CREATE TRIGGER trg_platform_customer_support_permissions_insert_prd132
BEFORE INSERT ON platform_customer_support_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_support_permissions_update_prd132
BEFORE UPDATE ON platform_customer_support_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_support_permissions_delete_prd132
BEFORE DELETE ON platform_customer_support_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_support_roles_insert_prd132
BEFORE INSERT ON platform_customer_support_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_support_roles_update_prd132
BEFORE UPDATE ON platform_customer_support_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_customer_support_permission_immutable'); END;
CREATE TRIGGER trg_platform_customer_support_roles_delete_prd132
BEFORE DELETE ON platform_customer_support_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_customer_support_permission_immutable'); END;

CREATE TABLE support_sla_policies(
  severity TEXT NOT NULL CHECK(severity IN('p1','p2','p3','p4')),
  policy_version INTEGER NOT NULL CHECK(policy_version=1),
  first_response_seconds INTEGER NOT NULL CHECK(first_response_seconds BETWEEN 60 AND 86400),
  resolution_seconds INTEGER NOT NULL CHECK(resolution_seconds BETWEEN 300 AND 604800),
  risk_lead_seconds INTEGER NOT NULL CHECK(risk_lead_seconds BETWEEN 60 AND 3600),
  pause_on_customer_wait INTEGER NOT NULL CHECK(pause_on_customer_wait IN(0,1)),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-132'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(severity,policy_version),
  CHECK(risk_lead_seconds<=first_response_seconds),
  CHECK(first_response_seconds<resolution_seconds)
);
INSERT INTO support_sla_policies VALUES
  ('p1',1,300,14400,300,1,'PRD-132',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('p2',1,3600,28800,900,1,'PRD-132',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('p3',1,14400,86400,1800,1,'PRD-132',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('p4',1,28800,259200,3600,1,'PRD-132',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_support_sla_policies_insert_prd132 BEFORE INSERT ON support_sla_policies
BEGIN SELECT RAISE(ABORT,'support_sla_policy_immutable'); END;
CREATE TRIGGER trg_support_sla_policies_update_prd132 BEFORE UPDATE ON support_sla_policies
BEGIN SELECT RAISE(ABORT,'support_sla_policy_immutable'); END;
CREATE TRIGGER trg_support_sla_policies_delete_prd132 BEFORE DELETE ON support_sla_policies
BEGIN SELECT RAISE(ABORT,'support_sla_policy_immutable'); END;

CREATE TABLE support_tickets(
  support_ticket_id TEXT PRIMARY KEY CHECK(length(support_ticket_id)=36
    AND substr(support_ticket_id,1,4)='tkt_'
    AND substr(support_ticket_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  reporter_account_id TEXT NOT NULL,
  reporter_membership_id TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN(
    'account','billing','integration','migration','reliability','security')),
  severity TEXT NOT NULL CHECK(severity IN('p1','p2','p3','p4')),
  status TEXT NOT NULL CHECK(status IN(
    'open','triaged','waiting_customer','waiting_platform','resolved','closed')),
  subject_ciphertext TEXT NOT NULL CHECK(length(subject_ciphertext) BETWEEN 64 AND 4096),
  subject_key_version INTEGER NOT NULL CHECK(subject_key_version=1),
  related_kind TEXT NOT NULL CHECK(related_kind IN(
    'none','request','incident','edge_deployment','migration')),
  related_reference_hmac TEXT CHECK(related_reference_hmac IS NULL OR(
    length(related_reference_hmac)=76 AND related_reference_hmac GLOB 'hmac-sha256:*'
    AND substr(related_reference_hmac,13) NOT GLOB '*[^0-9a-f]*')),
  platform_assignee_operator_id TEXT,
  sla_policy_version INTEGER NOT NULL CHECK(sla_policy_version=1),
  authorization_fingerprint_sha256 TEXT NOT NULL CHECK(
    length(authorization_fingerprint_sha256)=64
    AND authorization_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  last_customer_activity_at TEXT NOT NULL CHECK(unixepoch(last_customer_activity_at) IS NOT NULL),
  last_platform_activity_at TEXT CHECK(last_platform_activity_at IS NULL
    OR unixepoch(last_platform_activity_at) IS NOT NULL),
  resolved_at TEXT CHECK(resolved_at IS NULL OR unixepoch(resolved_at) IS NOT NULL),
  closed_at TEXT CHECK(closed_at IS NULL OR unixepoch(closed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(reporter_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(reporter_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(platform_assignee_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(severity,sla_policy_version)
    REFERENCES support_sla_policies(severity,policy_version),
  UNIQUE(workspace_id,tenant_id,support_ticket_id),
  CHECK((related_kind='none' AND related_reference_hmac IS NULL)
    OR(related_kind!='none' AND related_reference_hmac IS NOT NULL)),
  CHECK((status NOT IN('resolved','closed') AND resolved_at IS NULL AND closed_at IS NULL)
    OR(status='resolved' AND resolved_at IS NOT NULL AND closed_at IS NULL)
    OR(status='closed' AND resolved_at IS NOT NULL AND closed_at IS NOT NULL)),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at))
);
CREATE INDEX idx_support_tickets_workspace_prd132
  ON support_tickets(workspace_id,updated_at DESC,support_ticket_id DESC);
CREATE INDEX idx_support_tickets_platform_queue_prd132
  ON support_tickets(status,severity,updated_at,support_ticket_id);
CREATE TRIGGER trg_support_tickets_update_prd132 BEFORE UPDATE ON support_tickets FOR EACH ROW
WHEN NEW.support_ticket_id!=OLD.support_ticket_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.reporter_account_id!=OLD.reporter_account_id
  OR NEW.reporter_membership_id!=OLD.reporter_membership_id OR NEW.category!=OLD.category
  OR NEW.severity!=OLD.severity OR NEW.subject_ciphertext!=OLD.subject_ciphertext
  OR NEW.subject_key_version!=OLD.subject_key_version OR NEW.related_kind!=OLD.related_kind
  OR NEW.related_reference_hmac IS NOT OLD.related_reference_hmac
  OR NEW.sla_policy_version!=OLD.sla_policy_version
  OR NEW.authorization_fingerprint_sha256!=OLD.authorization_fingerprint_sha256
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT ((NEW.status=OLD.status)
    OR(OLD.status='open' AND NEW.status IN('triaged','waiting_customer','waiting_platform','resolved'))
    OR(OLD.status='triaged' AND NEW.status IN('waiting_customer','waiting_platform','resolved'))
    OR(OLD.status='waiting_customer' AND NEW.status IN('triaged','waiting_platform','resolved'))
    OR(OLD.status='waiting_platform' AND NEW.status IN('triaged','waiting_customer','resolved'))
    OR(OLD.status='resolved' AND NEW.status IN('open','closed')))
BEGIN SELECT RAISE(ABORT,'support_ticket_transition_invalid'); END;
CREATE TRIGGER trg_support_tickets_delete_prd132 BEFORE DELETE ON support_tickets
BEGIN SELECT RAISE(ABORT,'support_ticket_immutable'); END;

CREATE TABLE support_ticket_messages(
  ticket_message_id TEXT PRIMARY KEY CHECK(length(ticket_message_id)=36
    AND substr(ticket_message_id,1,4)='msg_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  support_ticket_id TEXT NOT NULL,
  author_domain TEXT NOT NULL CHECK(author_domain IN('customer','platform')),
  author_account_id TEXT,
  author_operator_id TEXT,
  visibility TEXT NOT NULL CHECK(visibility IN('customer','internal')),
  body_ciphertext TEXT NOT NULL CHECK(length(body_ciphertext) BETWEEN 64 AND 32768),
  body_key_version INTEGER NOT NULL CHECK(body_key_version=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,support_ticket_id)
    REFERENCES support_tickets(workspace_id,tenant_id,support_ticket_id),
  FOREIGN KEY(author_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(author_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(workspace_id,tenant_id,ticket_message_id),
  CHECK((author_domain='customer' AND author_account_id IS NOT NULL
      AND author_operator_id IS NULL AND visibility='customer')
    OR(author_domain='platform' AND author_account_id IS NULL
      AND author_operator_id IS NOT NULL)),
  CHECK(author_domain='platform' OR visibility!='internal')
);
CREATE INDEX idx_support_messages_ticket_prd132
  ON support_ticket_messages(support_ticket_id,created_at,ticket_message_id);
CREATE TRIGGER trg_support_messages_update_prd132 BEFORE UPDATE ON support_ticket_messages
BEGIN SELECT RAISE(ABORT,'support_message_immutable'); END;
CREATE TRIGGER trg_support_messages_delete_prd132 BEFORE DELETE ON support_ticket_messages
BEGIN SELECT RAISE(ABORT,'support_message_immutable'); END;

CREATE TABLE support_ticket_attachments(
  ticket_attachment_id TEXT PRIMARY KEY CHECK(length(ticket_attachment_id)=36
    AND substr(ticket_attachment_id,1,4)='att_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  support_ticket_id TEXT NOT NULL,
  uploaded_by_account_id TEXT NOT NULL,
  source_file_id TEXT NOT NULL,
  source_reference_hmac TEXT NOT NULL CHECK(length(source_reference_hmac)=76
    AND source_reference_hmac GLOB 'hmac-sha256:*'),
  source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint)=76
    AND source_fingerprint GLOB 'hmac-sha256:*'),
  intent_code TEXT NOT NULL CHECK(intent_code IN(
    'diagnostic_evidence','log_bundle','reproduction')),
  media_type TEXT NOT NULL CHECK(media_type IN(
    'text/plain','application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
  size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 52428800),
  status TEXT NOT NULL CHECK(status IN(
    'quarantined','available','rejected','scan_timeout')),
  scan_version TEXT NOT NULL CHECK(scan_version='ticket-attachment-scan-v1'),
  scan_outcome TEXT CHECK(scan_outcome IS NULL OR scan_outcome IN(
    'clean','malicious','structure_invalid','owner_changed','timeout','unavailable')),
  download_generation INTEGER NOT NULL CHECK(download_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  scan_timeout_at TEXT NOT NULL CHECK(unixepoch(scan_timeout_at) IS NOT NULL),
  scanned_at TEXT CHECK(scanned_at IS NULL OR unixepoch(scanned_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,support_ticket_id)
    REFERENCES support_tickets(workspace_id,tenant_id,support_ticket_id),
  FOREIGN KEY(uploaded_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(tenant_id,source_file_id) REFERENCES uploaded_files(tenant_id,file_id),
  UNIQUE(workspace_id,tenant_id,ticket_attachment_id),
  CHECK((status='quarantined' AND scan_outcome IS NULL AND scanned_at IS NULL
      AND download_generation=0)
    OR(status='available' AND scan_outcome='clean' AND scanned_at IS NOT NULL
      AND download_generation=1)
    OR(status='rejected' AND scan_outcome IN(
      'malicious','structure_invalid','owner_changed','unavailable')
      AND scanned_at IS NOT NULL AND download_generation=0)
    OR(status='scan_timeout' AND scan_outcome='timeout' AND scanned_at IS NOT NULL
      AND download_generation=0))
);
CREATE INDEX idx_support_attachments_ticket_prd132
  ON support_ticket_attachments(support_ticket_id,created_at,ticket_attachment_id);
CREATE INDEX idx_support_attachments_scan_prd132
  ON support_ticket_attachments(status,scan_timeout_at,ticket_attachment_id);
CREATE TRIGGER trg_support_attachments_update_prd132
BEFORE UPDATE ON support_ticket_attachments FOR EACH ROW
WHEN NEW.ticket_attachment_id!=OLD.ticket_attachment_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.support_ticket_id!=OLD.support_ticket_id
  OR NEW.uploaded_by_account_id!=OLD.uploaded_by_account_id
  OR NEW.source_file_id!=OLD.source_file_id
  OR NEW.source_reference_hmac!=OLD.source_reference_hmac
  OR NEW.source_fingerprint!=OLD.source_fingerprint OR NEW.intent_code!=OLD.intent_code
  OR NEW.media_type!=OLD.media_type OR NEW.size_bytes!=OLD.size_bytes
  OR NEW.scan_version!=OLD.scan_version OR NEW.scan_timeout_at!=OLD.scan_timeout_at
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR OLD.status!='quarantined' OR NEW.status NOT IN('available','rejected','scan_timeout')
BEGIN SELECT RAISE(ABORT,'support_attachment_transition_invalid'); END;
CREATE TRIGGER trg_support_attachments_delete_prd132 BEFORE DELETE ON support_ticket_attachments
BEGIN SELECT RAISE(ABORT,'support_attachment_immutable'); END;

CREATE TABLE support_attachment_scan_jobs(
  attachment_scan_job_id TEXT PRIMARY KEY CHECK(length(attachment_scan_job_id)=36
    AND substr(attachment_scan_job_id,1,4)='asj_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  ticket_attachment_id TEXT NOT NULL UNIQUE,
  expected_attachment_revision INTEGER NOT NULL CHECK(expected_attachment_revision=1),
  expected_source_fingerprint TEXT NOT NULL CHECK(length(expected_source_fingerprint)=76),
  status TEXT NOT NULL CHECK(status IN('pending','completed','failed','timed_out')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 5),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'support.attachment_unavailable','support.attachment_owner_changed')),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,ticket_attachment_id)
    REFERENCES support_ticket_attachments(workspace_id,tenant_id,ticket_attachment_id),
  CHECK((status='pending' AND completed_at IS NULL)
    OR(status!='pending' AND completed_at IS NOT NULL))
);
CREATE INDEX idx_support_attachment_jobs_due_prd132
  ON support_attachment_scan_jobs(status,next_attempt_at,attachment_scan_job_id);
CREATE TRIGGER trg_support_attachment_jobs_update_prd132
BEFORE UPDATE ON support_attachment_scan_jobs FOR EACH ROW
WHEN NEW.attachment_scan_job_id!=OLD.attachment_scan_job_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.ticket_attachment_id!=OLD.ticket_attachment_id
  OR NEW.expected_attachment_revision!=OLD.expected_attachment_revision
  OR NEW.expected_source_fingerprint!=OLD.expected_source_fingerprint
  OR NEW.created_at!=OLD.created_at OR NEW.attempt_count<OLD.attempt_count
  OR NEW.updated_at<=OLD.updated_at OR OLD.status!='pending'
BEGIN SELECT RAISE(ABORT,'support_attachment_job_transition_invalid'); END;
CREATE TRIGGER trg_support_attachment_jobs_delete_prd132 BEFORE DELETE ON support_attachment_scan_jobs
BEGIN SELECT RAISE(ABORT,'support_attachment_job_immutable'); END;

CREATE TABLE support_sla_clocks(
  sla_clock_id TEXT PRIMARY KEY CHECK(length(sla_clock_id)=36
    AND substr(sla_clock_id,1,4)='slc_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  support_ticket_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN('running','paused_customer','paused_policy','stopped')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  first_response_deadline_at TEXT NOT NULL CHECK(unixepoch(first_response_deadline_at) IS NOT NULL),
  resolution_deadline_at TEXT NOT NULL CHECK(unixepoch(resolution_deadline_at) IS NOT NULL),
  risk_at TEXT NOT NULL CHECK(unixepoch(risk_at) IS NOT NULL),
  first_responded_at TEXT CHECK(first_responded_at IS NULL OR unixepoch(first_responded_at) IS NOT NULL),
  paused_at TEXT CHECK(paused_at IS NULL OR unixepoch(paused_at) IS NOT NULL),
  stopped_at TEXT CHECK(stopped_at IS NULL OR unixepoch(stopped_at) IS NOT NULL),
  risk_generation INTEGER NOT NULL CHECK(risk_generation>=0),
  escalation_generation INTEGER NOT NULL CHECK(escalation_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,support_ticket_id)
    REFERENCES support_tickets(workspace_id,tenant_id,support_ticket_id),
  UNIQUE(workspace_id,tenant_id,sla_clock_id),
  CHECK((state='running' AND paused_at IS NULL AND stopped_at IS NULL)
    OR(state IN('paused_customer','paused_policy') AND paused_at IS NOT NULL AND stopped_at IS NULL)
    OR(state='stopped' AND paused_at IS NULL AND stopped_at IS NOT NULL)),
  CHECK(escalation_generation<=risk_generation)
);
CREATE INDEX idx_support_sla_due_prd132 ON support_sla_clocks(
  state,risk_at,resolution_deadline_at,sla_clock_id);
CREATE TRIGGER trg_support_sla_clocks_update_prd132
BEFORE UPDATE ON support_sla_clocks FOR EACH ROW
WHEN NEW.sla_clock_id!=OLD.sla_clock_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.support_ticket_id!=OLD.support_ticket_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.generation<OLD.generation OR NEW.risk_generation<OLD.risk_generation
  OR NEW.escalation_generation<OLD.escalation_generation
  OR NEW.updated_at<=OLD.updated_at
  OR NOT ((NEW.state=OLD.state)
    OR(OLD.state='running' AND NEW.state IN('paused_customer','paused_policy','stopped'))
    OR(OLD.state IN('paused_customer','paused_policy') AND NEW.state IN('running','stopped'))
    OR(OLD.state='stopped' AND NEW.state='running' AND NEW.generation=OLD.generation+1))
BEGIN SELECT RAISE(ABORT,'support_sla_clock_transition_invalid'); END;
CREATE TRIGGER trg_support_sla_clocks_delete_prd132 BEFORE DELETE ON support_sla_clocks
BEGIN SELECT RAISE(ABORT,'support_sla_clock_immutable'); END;

CREATE TABLE support_sla_clock_events(
  sla_clock_event_id TEXT PRIMARY KEY CHECK(length(sla_clock_event_id)=36
    AND substr(sla_clock_event_id,1,4)='sce_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  sla_clock_id TEXT NOT NULL,
  support_ticket_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'started','paused_customer','paused_policy','resumed','first_responded',
    'at_risk','escalated','stopped','reopened')),
  clock_generation INTEGER NOT NULL CHECK(clock_generation>=1),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,sla_clock_id)
    REFERENCES support_sla_clocks(workspace_id,tenant_id,sla_clock_id),
  FOREIGN KEY(workspace_id,tenant_id,support_ticket_id)
    REFERENCES support_tickets(workspace_id,tenant_id,support_ticket_id),
  UNIQUE(sla_clock_id,event_type,clock_generation,source_revision)
);
CREATE INDEX idx_support_clock_events_ticket_prd132
  ON support_sla_clock_events(support_ticket_id,occurred_at,sla_clock_event_id);
CREATE TRIGGER trg_support_clock_events_update_prd132 BEFORE UPDATE ON support_sla_clock_events
BEGIN SELECT RAISE(ABORT,'support_sla_clock_event_immutable'); END;
CREATE TRIGGER trg_support_clock_events_delete_prd132 BEFORE DELETE ON support_sla_clock_events
BEGIN SELECT RAISE(ABORT,'support_sla_clock_event_immutable'); END;

CREATE TABLE support_escalations(
  escalation_id TEXT PRIMARY KEY CHECK(length(escalation_id)=36
    AND substr(escalation_id,1,4)='esc_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  support_ticket_id TEXT NOT NULL,
  source_code TEXT NOT NULL CHECK(source_code IN(
    'customer_requested','sla_at_risk','sla_breached','platform_manual')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._-]*'),
  generation INTEGER NOT NULL CHECK(generation>=1),
  requested_by_domain TEXT NOT NULL CHECK(requested_by_domain IN('customer','platform','scheduler')),
  requested_by_account_id TEXT,
  requested_by_operator_id TEXT,
  status TEXT NOT NULL CHECK(status IN('open','acknowledged','closed')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  acknowledged_at TEXT CHECK(acknowledged_at IS NULL OR unixepoch(acknowledged_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,support_ticket_id)
    REFERENCES support_tickets(workspace_id,tenant_id,support_ticket_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(support_ticket_id,generation),
  CHECK((requested_by_domain='customer' AND requested_by_account_id IS NOT NULL
      AND requested_by_operator_id IS NULL)
    OR(requested_by_domain='platform' AND requested_by_account_id IS NULL
      AND requested_by_operator_id IS NOT NULL)
    OR(requested_by_domain='scheduler' AND requested_by_account_id IS NULL
      AND requested_by_operator_id IS NULL)),
  CHECK((status='open' AND acknowledged_at IS NULL)
    OR(status!='open' AND acknowledged_at IS NOT NULL))
);
CREATE INDEX idx_support_escalations_ticket_prd132
  ON support_escalations(support_ticket_id,created_at,escalation_id);
CREATE TRIGGER trg_support_escalations_update_prd132
BEFORE UPDATE ON support_escalations FOR EACH ROW
WHEN NEW.escalation_id!=OLD.escalation_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.support_ticket_id!=OLD.support_ticket_id
  OR NEW.source_code!=OLD.source_code OR NEW.reason_code!=OLD.reason_code
  OR NEW.generation!=OLD.generation OR NEW.requested_by_domain!=OLD.requested_by_domain
  OR NEW.requested_by_account_id IS NOT OLD.requested_by_account_id
  OR NEW.requested_by_operator_id IS NOT OLD.requested_by_operator_id
  OR NEW.created_at!=OLD.created_at OR OLD.status!='open'
  OR NEW.status NOT IN('acknowledged','closed')
BEGIN SELECT RAISE(ABORT,'support_escalation_transition_invalid'); END;
CREATE TRIGGER trg_support_escalations_delete_prd132 BEFORE DELETE ON support_escalations
BEGIN SELECT RAISE(ABORT,'support_escalation_immutable'); END;

CREATE TABLE support_satisfaction_surveys(
  satisfaction_survey_id TEXT PRIMARY KEY CHECK(length(satisfaction_survey_id)=36
    AND substr(satisfaction_survey_id,1,4)='ssv_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  support_ticket_id TEXT NOT NULL UNIQUE,
  submitted_by_account_id TEXT NOT NULL,
  rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
  comment_ciphertext TEXT,
  comment_key_version INTEGER CHECK(comment_key_version IS NULL OR comment_key_version=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,support_ticket_id)
    REFERENCES support_tickets(workspace_id,tenant_id,support_ticket_id),
  FOREIGN KEY(submitted_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,satisfaction_survey_id),
  CHECK((comment_ciphertext IS NULL AND comment_key_version IS NULL)
    OR(comment_ciphertext IS NOT NULL AND length(comment_ciphertext) BETWEEN 64 AND 16384
      AND comment_key_version=1))
);
CREATE TRIGGER trg_support_surveys_update_prd132 BEFORE UPDATE ON support_satisfaction_surveys
BEGIN SELECT RAISE(ABORT,'support_survey_immutable'); END;
CREATE TRIGGER trg_support_surveys_delete_prd132 BEFORE DELETE ON support_satisfaction_surveys
BEGIN SELECT RAISE(ABORT,'support_survey_immutable'); END;

CREATE TABLE support_ticket_events(
  support_ticket_event_id TEXT PRIMARY KEY CHECK(length(support_ticket_event_id)=36
    AND substr(support_ticket_event_id,1,4)='ste_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  support_ticket_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'support.ticket_created','support.sla_at_risk','support.ticket_resolved',
    'support.ticket_reopened','support.ticket_updated','support.message_created',
    'support.attachment_quarantined','support.attachment_available',
    'support.attachment_rejected','support.attachment_scan_timeout',
    'support.escalation_created','support.survey_submitted')),
  source_object_id TEXT NOT NULL CHECK(length(source_object_id)=36),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('customer','platform','scheduler')),
  safe_metadata_json TEXT NOT NULL CHECK(json_valid(safe_metadata_json)
    AND json_type(safe_metadata_json)='object' AND length(safe_metadata_json)<=1024),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,support_ticket_id)
    REFERENCES support_tickets(workspace_id,tenant_id,support_ticket_id),
  UNIQUE(support_ticket_id,event_type,source_object_id,source_revision),
  UNIQUE(workspace_id,tenant_id,support_ticket_event_id)
);
CREATE INDEX idx_support_events_ticket_prd132
  ON support_ticket_events(support_ticket_id,occurred_at,support_ticket_event_id);
CREATE TRIGGER trg_support_events_update_prd132 BEFORE UPDATE ON support_ticket_events
BEGIN SELECT RAISE(ABORT,'support_ticket_event_immutable'); END;
CREATE TRIGGER trg_support_events_delete_prd132 BEFORE DELETE ON support_ticket_events
BEGIN SELECT RAISE(ABORT,'support_ticket_event_immutable'); END;

CREATE TABLE support_event_outbox(
  support_event_outbox_id TEXT PRIMARY KEY CHECK(length(support_event_outbox_id)=36
    AND substr(support_event_outbox_id,1,4)='seo_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  support_ticket_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('notification','webhook')),
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'support.event_unavailable','webhook.delivery_unavailable')),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,support_ticket_event_id)
    REFERENCES support_ticket_events(workspace_id,tenant_id,support_ticket_event_id),
  UNIQUE(support_ticket_event_id,delivery_domain),
  CHECK((status='pending' AND delivered_at IS NULL)
    OR(status='delivered' AND delivered_at IS NOT NULL)
    OR(status='failed' AND delivered_at IS NULL))
);
CREATE INDEX idx_support_outbox_due_prd132
  ON support_event_outbox(status,next_attempt_at,support_event_outbox_id);
CREATE TRIGGER trg_support_outbox_update_prd132 BEFORE UPDATE ON support_event_outbox FOR EACH ROW
WHEN NEW.support_event_outbox_id!=OLD.support_event_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.support_ticket_event_id!=OLD.support_ticket_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR NEW.updated_at<=OLD.updated_at
  OR OLD.status='delivered'
BEGIN SELECT RAISE(ABORT,'support_outbox_transition_invalid'); END;
CREATE TRIGGER trg_support_outbox_delete_prd132 BEFORE DELETE ON support_event_outbox
BEGIN SELECT RAISE(ABORT,'support_outbox_immutable'); END;

CREATE TABLE support_action_commands(
  support_action_command_id TEXT PRIMARY KEY CHECK(length(support_action_command_id)=36
    AND substr(support_action_command_id,1,4)='sac_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('customer','platform')),
  actor_identity TEXT NOT NULL CHECK(length(actor_identity)=36
    AND substr(actor_identity,1,4) IN('gac_','pto_')),
  actor_account_id TEXT,
  actor_operator_id TEXT,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'support.ticket.create','support.ticket.patch','support.attachment.create',
    'support.escalation.create')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(actor_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((actor_domain='customer' AND actor_identity=actor_account_id
      AND actor_account_id IS NOT NULL AND actor_operator_id IS NULL)
    OR(actor_domain='platform' AND actor_identity=actor_operator_id
      AND actor_account_id IS NULL AND actor_operator_id IS NOT NULL)),
  UNIQUE(workspace_id,actor_domain,actor_identity,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_support_actions_update_prd132 BEFORE UPDATE ON support_action_commands
BEGIN SELECT RAISE(ABORT,'support_action_immutable'); END;
CREATE TRIGGER trg_support_actions_delete_prd132 BEFORE DELETE ON support_action_commands
BEGIN SELECT RAISE(ABORT,'support_action_immutable'); END;

CREATE TABLE support_scheduler_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  watermark_at TEXT NOT NULL CHECK(unixepoch(watermark_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR length(lease_token_sha256)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((lease_owner_id IS NULL AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 8 AND 128
      AND lease_token_sha256 IS NOT NULL AND lease_expires_at IS NOT NULL))
);
INSERT INTO support_scheduler_state VALUES(
  1,'1970-01-01T00:00:00.000Z',NULL,NULL,NULL,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_support_scheduler_state_update_prd132
BEFORE UPDATE ON support_scheduler_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'support_scheduler_fencing_invalid'); END;
CREATE TRIGGER trg_support_scheduler_state_delete_prd132 BEFORE DELETE ON support_scheduler_state
BEGIN SELECT RAISE(ABORT,'support_scheduler_state_required'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd132(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-132'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd132_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd132_manifest_sources VALUES
  ('support_tickets','archive_then_delete','audit'),
  ('support_ticket_messages','archive_then_delete','audit'),
  ('support_ticket_attachments','archive_then_delete','audit'),
  ('support_attachment_scan_jobs','delete','operational'),
  ('support_sla_clocks','archive_then_delete','audit'),
  ('support_sla_clock_events','archive_then_delete','audit'),
  ('support_escalations','archive_then_delete','audit'),
  ('support_satisfaction_surveys','archive_then_delete','audit'),
  ('support_ticket_events','archive_then_delete','audit'),
  ('support_event_outbox','delete','operational'),
  ('support_action_commands','archive_then_delete','audit');
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
JOIN prd132_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd132(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-132',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd132_manifest_sources ORDER BY source_table;
DROP TABLE prd132_manifest_sources;
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
CREATE TRIGGER trg_tenant_deletion_manifest_prd132_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd132
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd132_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd132
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd132_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd132
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd132_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd132_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_customer_support_permission_extensions)=3
      AND (SELECT COUNT(*) FROM workspace_customer_support_role_permissions)=16
      AND (SELECT COUNT(*) FROM platform_customer_support_permission_extensions)=3
      AND (SELECT COUNT(*) FROM platform_customer_support_role_permissions)=4
      AND (SELECT COUNT(*) FROM support_sla_policies)=4
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd132)=11
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd132_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
