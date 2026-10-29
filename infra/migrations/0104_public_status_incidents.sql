-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd112_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd112_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0103' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','workspace_tenant_bindings',
      'notification_outbox','platform_alerts','disaster_recovery_runs'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd112_up_guard;

CREATE TABLE platform_status_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.status.read','platform.status.write','platform.status.security_approve')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-112'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=14)
);
CREATE TABLE platform_status_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_status_permission_extensions(permission_code)
);
INSERT INTO platform_status_permission_extensions VALUES
  ('platform.status.read','PRD-112',14),
  ('platform.status.write','PRD-112',14),
  ('platform.status.security_approve','PRD-112',14);
INSERT INTO platform_status_role_permissions VALUES
  ('platform_owner','platform.status.read'),
  ('platform_owner','platform.status.write'),
  ('platform_owner','platform.status.security_approve'),
  ('platform_ops','platform.status.read'),
  ('platform_ops','platform.status.write'),
  ('platform_security','platform.status.read'),
  ('platform_security','platform.status.security_approve'),
  ('platform_communications','platform.status.read'),
  ('platform_communications','platform.status.write');
CREATE TRIGGER trg_platform_status_permissions_insert_prd112
BEFORE INSERT ON platform_status_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_status_permission_immutable'); END;
CREATE TRIGGER trg_platform_status_permissions_update_prd112
BEFORE UPDATE ON platform_status_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_status_permission_immutable'); END;
CREATE TRIGGER trg_platform_status_permissions_delete_prd112
BEFORE DELETE ON platform_status_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_status_permission_immutable'); END;
CREATE TRIGGER trg_platform_status_roles_insert_prd112
BEFORE INSERT ON platform_status_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_status_permission_immutable'); END;
CREATE TRIGGER trg_platform_status_roles_update_prd112
BEFORE UPDATE ON platform_status_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_status_permission_immutable'); END;
CREATE TRIGGER trg_platform_status_roles_delete_prd112
BEFORE DELETE ON platform_status_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_status_permission_immutable'); END;

CREATE TABLE status_components(
  status_component_id TEXT PRIMARY KEY CHECK(length(status_component_id)=36
    AND substr(status_component_id,1,4)='stc_'),
  public_code TEXT NOT NULL UNIQUE CHECK(length(public_code) BETWEEN 2 AND 48
    AND public_code=lower(public_code) AND public_code NOT GLOB '*[^a-z0-9-]*'),
  public_name TEXT NOT NULL CHECK(length(public_name) BETWEEN 2 AND 80),
  component_group TEXT NOT NULL CHECK(component_group IN(
    'gateway','control','identity','billing','integrations')),
  current_state TEXT NOT NULL CHECK(current_state IN(
    'operational','degraded_performance','partial_outage','major_outage','maintenance')),
  display_order INTEGER NOT NULL UNIQUE CHECK(display_order BETWEEN 1 AND 1000),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO status_components VALUES
  ('stc_00000000000000000000000000000001','api-gateway','API Gateway','gateway',
   'operational',10,1,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z'),
  ('stc_00000000000000000000000000000002','control-console','Control Console','control',
   'operational',20,1,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z'),
  ('stc_00000000000000000000000000000003','identity','Identity','identity',
   'operational',30,1,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z'),
  ('stc_00000000000000000000000000000004','billing','Billing','billing',
   'operational',40,1,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z'),
  ('stc_00000000000000000000000000000005','integrations','Integrations','integrations',
   'operational',50,1,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z');
CREATE TRIGGER trg_status_components_update_prd112
BEFORE UPDATE ON status_components FOR EACH ROW
WHEN NEW.status_component_id!=OLD.status_component_id OR NEW.public_code!=OLD.public_code
  OR NEW.public_name!=OLD.public_name OR NEW.component_group!=OLD.component_group
  OR NEW.display_order!=OLD.display_order OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'status_component_transition_invalid'); END;
CREATE TRIGGER trg_status_components_delete_prd112 BEFORE DELETE ON status_components
BEGIN SELECT RAISE(ABORT,'status_component_immutable'); END;

CREATE TABLE incidents(
  incident_id TEXT PRIMARY KEY CHECK(length(incident_id)=36
    AND substr(incident_id,1,4)='inc_'),
  public_reference TEXT NOT NULL UNIQUE CHECK(length(public_reference) BETWEEN 8 AND 32
    AND public_reference NOT GLOB '*[^A-Z0-9-]*'),
  title TEXT NOT NULL CHECK(length(title) BETWEEN 5 AND 160
    AND instr(title,'<')=0 AND instr(title,'>')=0),
  classification TEXT NOT NULL CHECK(classification IN('reliability','security')),
  visibility TEXT NOT NULL CHECK(visibility IN('public','workspace')),
  severity TEXT NOT NULL CHECK(severity IN('minor','major','critical')),
  status TEXT NOT NULL CHECK(status IN(
    'draft','pending_security_review','published','monitoring','resolved',
    'postmortem_published')),
  acknowledged_at TEXT NOT NULL CHECK(unixepoch(acknowledged_at) IS NOT NULL),
  first_update_at TEXT CHECK(first_update_at IS NULL OR unixepoch(first_update_at) IS NOT NULL),
  created_by_operator_id TEXT NOT NULL,
  security_approved_by_operator_id TEXT,
  security_approved_at TEXT CHECK(security_approved_at IS NULL
    OR unixepoch(security_approved_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  resolved_at TEXT CHECK(resolved_at IS NULL OR unixepoch(resolved_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(security_approved_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((classification='security' AND status IN('draft','pending_security_review')
      AND security_approved_by_operator_id IS NULL AND security_approved_at IS NULL)
    OR(classification='security' AND status NOT IN('draft','pending_security_review')
      AND security_approved_by_operator_id IS NOT NULL
      AND security_approved_by_operator_id!=created_by_operator_id
      AND security_approved_at IS NOT NULL)
    OR(classification='reliability' AND security_approved_by_operator_id IS NULL
      AND security_approved_at IS NULL)),
  CHECK((status IN('draft','pending_security_review') AND first_update_at IS NULL)
    OR(status NOT IN('draft','pending_security_review') AND first_update_at IS NOT NULL)),
  CHECK((status IN('resolved','postmortem_published') AND resolved_at IS NOT NULL)
    OR(status NOT IN('resolved','postmortem_published') AND resolved_at IS NULL))
);
CREATE INDEX idx_incidents_recent_prd112 ON incidents(updated_at DESC,incident_id DESC);
CREATE TRIGGER trg_incidents_update_prd112
BEFORE UPDATE ON incidents FOR EACH ROW
WHEN NEW.incident_id!=OLD.incident_id OR NEW.public_reference!=OLD.public_reference
  OR NEW.title!=OLD.title OR NEW.classification!=OLD.classification
  OR NEW.visibility!=OLD.visibility OR NEW.severity!=OLD.severity
  OR NEW.acknowledged_at!=OLD.acknowledged_at
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status='postmortem_published'
  OR NOT(NEW.status=OLD.status
    OR(OLD.status='draft' AND NEW.status IN('pending_security_review','published'))
    OR(OLD.status='pending_security_review' AND NEW.status='published')
    OR(OLD.status='published' AND NEW.status IN('monitoring','resolved'))
    OR(OLD.status='monitoring' AND NEW.status='resolved')
    OR(OLD.status='resolved' AND NEW.status='postmortem_published'))
BEGIN SELECT RAISE(ABORT,'incident_transition_invalid'); END;
CREATE TRIGGER trg_incidents_delete_prd112 BEFORE DELETE ON incidents
BEGIN SELECT RAISE(ABORT,'incident_immutable'); END;

CREATE TABLE incident_workspace_scopes(
  incident_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  scope_kind TEXT NOT NULL CHECK(scope_kind IN('owner','private_projection')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(incident_id,workspace_id),
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_incident_workspace_owner_prd112
  ON incident_workspace_scopes(workspace_id,tenant_id,incident_id);
CREATE TRIGGER trg_incident_scopes_update_prd112 BEFORE UPDATE ON incident_workspace_scopes
BEGIN SELECT RAISE(ABORT,'incident_scope_immutable'); END;
CREATE TRIGGER trg_incident_scopes_delete_prd112 BEFORE DELETE ON incident_workspace_scopes
BEGIN SELECT RAISE(ABORT,'incident_scope_immutable'); END;

CREATE TABLE incident_updates(
  incident_update_id TEXT PRIMARY KEY CHECK(length(incident_update_id)=36
    AND substr(incident_update_id,1,4)='iup_'),
  incident_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('published','monitoring','resolved')),
  public_message TEXT NOT NULL CHECK(length(public_message) BETWEEN 10 AND 2000
    AND instr(public_message,'<')=0 AND instr(public_message,'>')=0),
  affected_components_json TEXT NOT NULL CHECK(json_valid(affected_components_json)
    AND json_type(affected_components_json)='array'),
  affected_regions_json TEXT NOT NULL CHECK(json_valid(affected_regions_json)
    AND json_type(affected_regions_json)='array'),
  operator_id TEXT NOT NULL,
  published_at TEXT NOT NULL CHECK(unixepoch(published_at) IS NOT NULL),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(incident_id,payload_sha256)
);
CREATE INDEX idx_incident_updates_timeline_prd112
  ON incident_updates(incident_id,published_at,incident_update_id);
CREATE TRIGGER trg_incident_updates_update_prd112 BEFORE UPDATE ON incident_updates
BEGIN SELECT RAISE(ABORT,'incident_update_immutable'); END;
CREATE TRIGGER trg_incident_updates_delete_prd112 BEFORE DELETE ON incident_updates
BEGIN SELECT RAISE(ABORT,'incident_update_immutable'); END;

CREATE TABLE maintenance_windows(
  maintenance_window_id TEXT PRIMARY KEY CHECK(length(maintenance_window_id)=36
    AND substr(maintenance_window_id,1,4)='mtn_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  public_reference TEXT NOT NULL UNIQUE CHECK(length(public_reference) BETWEEN 8 AND 32),
  title TEXT NOT NULL CHECK(length(title) BETWEEN 5 AND 160
    AND instr(title,'<')=0 AND instr(title,'>')=0),
  public_message TEXT NOT NULL CHECK(length(public_message) BETWEEN 10 AND 2000
    AND instr(public_message,'<')=0 AND instr(public_message,'>')=0),
  visibility TEXT NOT NULL CHECK(visibility IN('public','workspace')),
  status TEXT NOT NULL CHECK(status IN('planned','in_progress','completed','cancelled')),
  affected_components_json TEXT NOT NULL CHECK(json_valid(affected_components_json)
    AND json_type(affected_components_json)='array'),
  affected_regions_json TEXT NOT NULL CHECK(json_valid(affected_regions_json)
    AND json_type(affected_regions_json)='array'),
  starts_at TEXT NOT NULL CHECK(unixepoch(starts_at) IS NOT NULL),
  ends_at TEXT NOT NULL CHECK(unixepoch(ends_at) IS NOT NULL),
  created_by_operator_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(unixepoch(ends_at)>unixepoch(starts_at))
);
CREATE INDEX idx_maintenance_owner_prd112
  ON maintenance_windows(workspace_id,tenant_id,starts_at,maintenance_window_id);
CREATE TRIGGER trg_maintenance_update_prd112
BEFORE UPDATE ON maintenance_windows FOR EACH ROW
WHEN NEW.maintenance_window_id!=OLD.maintenance_window_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.public_reference!=OLD.public_reference OR NEW.title!=OLD.title
  OR NEW.public_message!=OLD.public_message OR NEW.visibility!=OLD.visibility
  OR NEW.affected_components_json!=OLD.affected_components_json
  OR NEW.affected_regions_json!=OLD.affected_regions_json
  OR NEW.starts_at!=OLD.starts_at OR NEW.ends_at!=OLD.ends_at
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('completed','cancelled')
  OR NOT(NEW.status=OLD.status
    OR(OLD.status='planned' AND NEW.status IN('in_progress','cancelled'))
    OR(OLD.status='in_progress' AND NEW.status='completed'))
BEGIN SELECT RAISE(ABORT,'maintenance_transition_invalid'); END;
CREATE TRIGGER trg_maintenance_delete_prd112 BEFORE DELETE ON maintenance_windows
BEGIN SELECT RAISE(ABORT,'maintenance_immutable'); END;

CREATE TABLE status_subscriptions(
  status_subscription_id TEXT PRIMARY KEY CHECK(length(status_subscription_id)=36
    AND substr(status_subscription_id,1,4)='sts_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  channel TEXT NOT NULL CHECK(channel IN('email')),
  contact_ciphertext TEXT NOT NULL CHECK(length(contact_ciphertext) BETWEEN 40 AND 4096),
  contact_blind_index TEXT NOT NULL CHECK(length(contact_blind_index)=64),
  scope_json TEXT NOT NULL CHECK(json_valid(scope_json) AND json_type(scope_json)='object'),
  verification_token_hash TEXT CHECK(verification_token_hash IS NULL
    OR length(verification_token_hash)=64),
  unsubscribe_token_hash TEXT CHECK(unsubscribe_token_hash IS NULL
    OR length(unsubscribe_token_hash)=64),
  status TEXT NOT NULL CHECK(status IN('pending','active','unsubscribed','expired')),
  verification_expires_at TEXT CHECK(verification_expires_at IS NULL
    OR unixepoch(verification_expires_at) IS NOT NULL),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  unsubscribed_at TEXT CHECK(unsubscribed_at IS NULL OR unixepoch(unsubscribed_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,contact_blind_index,channel),
  CHECK((status='pending' AND verification_token_hash IS NOT NULL
      AND unsubscribe_token_hash IS NULL AND verification_expires_at IS NOT NULL
      AND verified_at IS NULL AND unsubscribed_at IS NULL)
    OR(status='active' AND verification_token_hash IS NULL
      AND unsubscribe_token_hash IS NOT NULL AND verification_expires_at IS NULL
      AND verified_at IS NOT NULL AND unsubscribed_at IS NULL)
    OR(status='unsubscribed' AND verification_token_hash IS NULL
      AND unsubscribe_token_hash IS NULL AND verification_expires_at IS NULL
      AND verified_at IS NOT NULL AND unsubscribed_at IS NOT NULL)
    OR(status='expired' AND verification_token_hash IS NULL
      AND unsubscribe_token_hash IS NULL AND verification_expires_at IS NULL))
);
CREATE INDEX idx_status_subscriptions_owner_prd112
  ON status_subscriptions(workspace_id,tenant_id,status,status_subscription_id);
CREATE TRIGGER trg_status_subscriptions_update_prd112
BEFORE UPDATE ON status_subscriptions FOR EACH ROW
WHEN NEW.status_subscription_id!=OLD.status_subscription_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.channel!=OLD.channel
  OR NEW.contact_ciphertext!=OLD.contact_ciphertext
  OR NEW.contact_blind_index!=OLD.contact_blind_index OR NEW.scope_json!=OLD.scope_json
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('unsubscribed','expired')
  OR NOT((OLD.status='pending' AND NEW.status IN('active','expired'))
    OR(OLD.status='active' AND NEW.status='unsubscribed'))
BEGIN SELECT RAISE(ABORT,'status_subscription_transition_invalid'); END;
CREATE TRIGGER trg_status_subscriptions_delete_prd112 BEFORE DELETE ON status_subscriptions
BEGIN SELECT RAISE(ABORT,'status_subscription_immutable'); END;

CREATE TABLE postmortems(
  postmortem_id TEXT PRIMARY KEY CHECK(length(postmortem_id)=36
    AND substr(postmortem_id,1,4)='ptm_'),
  incident_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  public_summary TEXT NOT NULL CHECK(length(public_summary) BETWEEN 20 AND 4000
    AND instr(public_summary,'<')=0 AND instr(public_summary,'>')=0),
  timeline_json TEXT NOT NULL CHECK(json_valid(timeline_json)
    AND json_type(timeline_json)='array'),
  cause_category TEXT NOT NULL CHECK(cause_category IN(
    'capacity','dependency','deployment','configuration','software','network','other')),
  corrective_actions_json TEXT NOT NULL CHECK(json_valid(corrective_actions_json)
    AND json_type(corrective_actions_json)='array'),
  status TEXT NOT NULL CHECK(status IN('draft','published')),
  published_by_operator_id TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  published_at TEXT CHECK(published_at IS NULL OR unixepoch(published_at) IS NOT NULL),
  FOREIGN KEY(incident_id) REFERENCES incidents(incident_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(published_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='draft' AND published_by_operator_id IS NULL AND published_at IS NULL)
    OR(status='published' AND published_by_operator_id IS NOT NULL
      AND published_at IS NOT NULL))
);
CREATE TRIGGER trg_postmortems_update_prd112 BEFORE UPDATE ON postmortems
BEGIN SELECT RAISE(ABORT,'postmortem_immutable'); END;
CREATE TRIGGER trg_postmortems_delete_prd112 BEFORE DELETE ON postmortems
BEGIN SELECT RAISE(ABORT,'postmortem_immutable'); END;

CREATE TABLE status_publication_bundles(
  status_publication_bundle_id TEXT PRIMARY KEY CHECK(length(status_publication_bundle_id)=36
    AND substr(status_publication_bundle_id,1,4)='spb_'),
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  canonical_sha256 TEXT NOT NULL CHECK(length(canonical_sha256)=64
    AND canonical_sha256 NOT GLOB '*[^0-9a-f]*'),
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm IN(
    'ed25519','rsassa_pss_sha256')),
  signature_value TEXT NOT NULL CHECK(length(signature_value) BETWEEN 40 AND 2048),
  signing_key_version TEXT NOT NULL CHECK(length(signing_key_version) BETWEEN 3 AND 128),
  object_version TEXT NOT NULL UNIQUE CHECK(length(object_version) BETWEEN 16 AND 160
    AND object_version NOT GLOB '*[^A-Za-z0-9._-]*'),
  status TEXT NOT NULL CHECK(status IN('published','superseded')),
  publisher_kind TEXT NOT NULL CHECK(publisher_kind IN('system','operator')),
  created_by_operator_id TEXT,
  published_at TEXT NOT NULL CHECK(unixepoch(published_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((publisher_kind='system' AND created_by_operator_id IS NULL)
    OR(publisher_kind='operator' AND created_by_operator_id IS NOT NULL)),
  CHECK(unixepoch(expires_at)>unixepoch(published_at))
);
CREATE UNIQUE INDEX uq_status_bundle_head_prd112 ON status_publication_bundles(status)
  WHERE status='published';
CREATE TRIGGER trg_status_bundles_update_prd112
BEFORE UPDATE ON status_publication_bundles FOR EACH ROW
WHEN NEW.status_publication_bundle_id!=OLD.status_publication_bundle_id
  OR NEW.generation!=OLD.generation OR NEW.canonical_sha256!=OLD.canonical_sha256
  OR NEW.signature_algorithm!=OLD.signature_algorithm
  OR NEW.signature_value!=OLD.signature_value
  OR NEW.signing_key_version!=OLD.signing_key_version
  OR NEW.object_version!=OLD.object_version
  OR NEW.publisher_kind!=OLD.publisher_kind
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.published_at!=OLD.published_at OR NEW.expires_at!=OLD.expires_at
  OR OLD.status!='published' OR NEW.status!='superseded'
BEGIN SELECT RAISE(ABORT,'status_bundle_transition_invalid'); END;
CREATE TRIGGER trg_status_bundles_delete_prd112 BEFORE DELETE ON status_publication_bundles
BEGIN SELECT RAISE(ABORT,'status_bundle_immutable'); END;

CREATE TABLE status_commands(
  status_command_id TEXT PRIMARY KEY CHECK(length(status_command_id)=36
    AND substr(status_command_id,1,4)='scm_'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'incident.create','incident.approve','incident.update','maintenance.create',
    'postmortem.publish')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_type TEXT NOT NULL CHECK(result_object_type IN(
    'incident','incident_update','maintenance_window','postmortem')),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE TRIGGER trg_status_commands_update_prd112 BEFORE UPDATE ON status_commands
BEGIN SELECT RAISE(ABORT,'status_command_immutable'); END;
CREATE TRIGGER trg_status_commands_delete_prd112 BEFORE DELETE ON status_commands
BEGIN SELECT RAISE(ABORT,'status_command_immutable'); END;

CREATE TABLE status_event_outbox(
  status_event_id TEXT PRIMARY KEY CHECK(length(status_event_id)=36
    AND substr(status_event_id,1,4)='sev_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'incident.created','incident.updated','incident.resolved','maintenance.scheduled')),
  source_object_type TEXT NOT NULL CHECK(source_object_type IN(
    'incident','incident_update','maintenance_window')),
  source_object_id TEXT NOT NULL CHECK(length(source_object_id)=36),
  visibility TEXT NOT NULL CHECK(visibility IN('public','workspace')),
  workspace_id TEXT,
  tenant_id TEXT,
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((visibility='public' AND workspace_id IS NULL AND tenant_id IS NULL)
    OR(visibility='workspace' AND workspace_id IS NOT NULL AND tenant_id IS NOT NULL))
);
CREATE TRIGGER trg_status_events_update_prd112 BEFORE UPDATE ON status_event_outbox
BEGIN SELECT RAISE(ABORT,'status_event_immutable'); END;
CREATE TRIGGER trg_status_events_delete_prd112 BEFORE DELETE ON status_event_outbox
BEGIN SELECT RAISE(ABORT,'status_event_immutable'); END;

CREATE TEMP TABLE prd112_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd112_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM status_components)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_status_permission_extensions)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_status_role_permissions)=9 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'status_components','incidents','incident_workspace_scopes','incident_updates',
      'maintenance_windows','status_subscriptions','postmortems',
      'status_publication_bundles','status_commands','status_event_outbox'))=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd112_up_final_guard;
COMMIT;
