PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd096_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd096_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0087' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspaces','workspace_tenant_bindings','workspace_memberships','workspace_sessions',
      'tenant_data_states','customer_health_scores','platform_operators',
      'platform_audit_logs'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd096')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd096_up_guard;

CREATE TABLE workspace_operational_states(
  workspace_operational_state_id TEXT PRIMARY KEY CHECK(
    length(workspace_operational_state_id)=36
    AND substr(workspace_operational_state_id,1,4)='wos_'
    AND substr(workspace_operational_state_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN(
    'active','suspended','deletion_pending','deleted','migration_conflict')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  risk_revision INTEGER NOT NULL CHECK(risk_revision>=1),
  source TEXT NOT NULL CHECK(source IN(
    'migration_backfill','workspace_creation','platform_suspend','platform_restore',
    'data_lifecycle')),
  suspended_reason_code TEXT,
  suspended_by_operator_id TEXT,
  suspended_at TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(suspended_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='suspended' AND source='platform_suspend'
      AND suspended_reason_code IS NOT NULL
      AND length(suspended_reason_code) BETWEEN 2 AND 80
      AND suspended_by_operator_id IS NOT NULL
      AND suspended_at IS NOT NULL AND unixepoch(suspended_at) IS NOT NULL)
    OR(status!='suspended' AND suspended_reason_code IS NULL
      AND suspended_by_operator_id IS NULL AND suspended_at IS NULL))
);
CREATE INDEX idx_workspace_operational_directory_prd096
  ON workspace_operational_states(updated_at DESC,workspace_id);
CREATE INDEX idx_workspace_operational_filter_prd096
  ON workspace_operational_states(status,updated_at DESC,workspace_id);

CREATE TABLE workspace_risk_tags(
  workspace_risk_tag_id TEXT PRIMARY KEY CHECK(length(workspace_risk_tag_id)=36
    AND substr(workspace_risk_tag_id,1,4)='wrt_'
    AND substr(workspace_risk_tag_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  tag_code TEXT NOT NULL CHECK(tag_code IN(
    'security_review','payment_attention','adoption_attention','compliance_hold')),
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT NOT NULL,
  updated_by_operator_id TEXT NOT NULL,
  revoked_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(updated_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(revoked_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(workspace_id,tag_code),
  CHECK((status='active' AND revoked_by_operator_id IS NULL AND revoked_at IS NULL)
    OR(status='revoked' AND revoked_by_operator_id IS NOT NULL AND revoked_at IS NOT NULL))
);
CREATE INDEX idx_workspace_risk_tags_active_prd096
  ON workspace_risk_tags(tag_code,workspace_id) WHERE status='active';

CREATE TABLE workspace_health_projections(
  workspace_health_projection_id TEXT PRIMARY KEY CHECK(
    length(workspace_health_projection_id)=36
    AND substr(workspace_health_projection_id,1,4)='whp_'
    AND substr(workspace_health_projection_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  source_score_id TEXT NOT NULL,
  source_sequence INTEGER NOT NULL CHECK(source_sequence>=1),
  score_band TEXT NOT NULL CHECK(score_band IN('healthy','watch','at_risk','critical')),
  risk_level TEXT NOT NULL CHECK(risk_level IN('healthy','watch','at_risk','critical')),
  risk_tags_json TEXT NOT NULL CHECK(json_valid(risk_tags_json)
    AND json_type(risk_tags_json)='array' AND length(CAST(risk_tags_json AS BLOB))<=512),
  source_projection_sha256 TEXT NOT NULL CHECK(length(source_projection_sha256)=64
    AND source_projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_evaluated_at TEXT NOT NULL CHECK(unixepoch(source_evaluated_at) IS NOT NULL),
  projected_at TEXT NOT NULL CHECK(unixepoch(projected_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,source_score_id)
    REFERENCES customer_health_scores(tenant_id,customer_health_score_id)
);
CREATE INDEX idx_workspace_health_filter_prd096
  ON workspace_health_projections(risk_level,source_evaluated_at,workspace_id);

CREATE TABLE platform_action_intents(
  platform_action_intent_id TEXT PRIMARY KEY CHECK(length(platform_action_intent_id)=36
    AND substr(platform_action_intent_id,1,4)='pai_'
    AND substr(platform_action_intent_id,5) NOT GLOB '*[^0-9a-f]*'),
  operator_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  action_type TEXT NOT NULL CHECK(action_type IN(
    'suspend','restore','risk_tag_add','risk_tag_remove','owner_notification')),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  expected_revision INTEGER NOT NULL CHECK(expected_revision>=1),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80),
  status TEXT NOT NULL CHECK(status='completed'),
  result_json TEXT NOT NULL CHECK(json_valid(result_json) AND json_type(result_json)='object'
    AND length(CAST(result_json AS BLOB))<=4096),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(operator_id,idempotency_key_hmac)
);
CREATE INDEX idx_platform_action_workspace_prd096
  ON platform_action_intents(workspace_id,created_at DESC,platform_action_intent_id DESC);

CREATE TABLE workspace_owner_notification_outbox(
  workspace_owner_notification_id TEXT PRIMARY KEY CHECK(
    length(workspace_owner_notification_id)=36
    AND substr(workspace_owner_notification_id,1,4)='wno_'
    AND substr(workspace_owner_notification_id,5) NOT GLOB '*[^0-9a-f]*'),
  platform_action_intent_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  owner_membership_id TEXT NOT NULL,
  owner_account_id TEXT NOT NULL,
  template_code TEXT NOT NULL CHECK(template_code IN(
    'workspace_operational_notice','workspace_risk_follow_up','workspace_service_update')),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=1024),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','sent','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR length(last_error_code) BETWEEN 2 AND 80),
  sent_at TEXT CHECK(sent_at IS NULL OR unixepoch(sent_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(platform_action_intent_id)
    REFERENCES platform_action_intents(platform_action_intent_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(owner_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(owner_account_id) REFERENCES global_accounts(account_id),
  CHECK(payload_sha256=metahub_sha256(payload_json)),
  CHECK((status='pending' AND attempt_count=0 AND next_attempt_at IS NOT NULL
      AND last_error_code IS NULL AND sent_at IS NULL)
    OR(status='sent' AND sent_at IS NOT NULL AND next_attempt_at IS NULL
      AND last_error_code IS NULL)
    OR(status='failed' AND attempt_count BETWEEN 1 AND 10
      AND last_error_code IS NOT NULL AND sent_at IS NULL))
);
CREATE INDEX idx_workspace_owner_notification_claim_prd096
  ON workspace_owner_notification_outbox(status,next_attempt_at,created_at);
CREATE INDEX idx_workspace_owner_notification_workspace_prd096
  ON workspace_owner_notification_outbox(workspace_id,created_at DESC);

CREATE TABLE workspace_operational_events(
  workspace_operational_event_id TEXT PRIMARY KEY CHECK(
    length(workspace_operational_event_id)=36
    AND substr(workspace_operational_event_id,1,4)='woe_'
    AND substr(workspace_operational_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_operator_id TEXT NOT NULL,
  action_intent_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'workspace.suspended','workspace.restored','workspace.risk_tag_added',
    'workspace.risk_tag_removed','workspace.owner_notification_requested')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80),
  operational_revision INTEGER NOT NULL CHECK(operational_revision>=1),
  risk_revision INTEGER NOT NULL CHECK(risk_revision>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(action_intent_id) REFERENCES platform_action_intents(platform_action_intent_id)
);
CREATE INDEX idx_workspace_operational_events_time_prd096
  ON workspace_operational_events(workspace_id,occurred_at DESC,
    workspace_operational_event_id DESC);

CREATE TABLE workspace_operational_event_outbox(
  workspace_operational_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(workspace_operational_event_outbox_id)=36
    AND substr(workspace_operational_event_outbox_id,1,4)='woo_'
    AND substr(workspace_operational_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_operational_event_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_operational_event_id)
    REFERENCES workspace_operational_events(workspace_operational_event_id),
  CHECK((status='pending' AND attempt_count=0 AND next_attempt_at IS NOT NULL
      AND delivered_at IS NULL)
    OR(status='processing' AND attempt_count BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND delivered_at IS NULL)
    OR(status='delivered' AND delivered_at IS NOT NULL AND next_attempt_at IS NULL)
    OR(status='failed' AND attempt_count=10 AND delivered_at IS NULL
      AND next_attempt_at IS NULL))
);
CREATE INDEX idx_workspace_operational_outbox_claim_prd096
  ON workspace_operational_event_outbox(status,next_attempt_at,created_at);

INSERT INTO workspace_operational_states(
  workspace_operational_state_id,workspace_id,tenant_id,status,revision,risk_revision,
  source,suspended_reason_code,suspended_by_operator_id,suspended_at,created_at,updated_at)
SELECT 'wos_'||lower(hex(randomblob(16))),workspace.workspace_id,binding.tenant_id,
  CASE
    WHEN data_state.lifecycle_status='deleted' AND tenant.status='disabled' THEN 'deleted'
    WHEN data_state.lifecycle_status='deletion_requested' THEN 'deletion_pending'
    WHEN workspace.status='active' AND binding.status='active' AND tenant.status='active'
      AND data_state.lifecycle_status='active' THEN 'active'
    ELSE 'migration_conflict'
  END,
  1,1,'migration_backfill',NULL,NULL,NULL,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM workspaces workspace
JOIN workspace_tenant_bindings binding ON binding.workspace_id=workspace.workspace_id
JOIN tenants tenant ON tenant.tenant_id=binding.tenant_id
JOIN tenant_data_states data_state ON data_state.tenant_id=binding.tenant_id;

INSERT INTO workspace_health_projections(
  workspace_health_projection_id,workspace_id,tenant_id,source_score_id,source_sequence,
  score_band,risk_level,risk_tags_json,source_projection_sha256,
  source_evaluated_at,projected_at)
SELECT 'whp_'||lower(hex(randomblob(16))),binding.workspace_id,score.tenant_id,
  score.customer_health_score_id,score.customer_health_score_sequence,
  score.risk_level,score.risk_level,
  json('['||rtrim(
    CASE WHEN score.usage_declining=1 THEN '"usage_declining",' ELSE '' END||
    CASE WHEN score.high_error_rate=1 THEN '"high_error_rate",' ELSE '' END||
    CASE WHEN score.cost_anomaly_open=1 THEN '"cost_anomaly_open",' ELSE '' END||
    CASE WHEN score.budget_missing=1 THEN '"budget_missing",' ELSE '' END||
    CASE WHEN score.team_inactive=1 THEN '"team_inactive",' ELSE '' END,',')||']'),
  score.health_projection_sha256,score.evaluated_at,strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM customer_health_scores score
JOIN workspace_tenant_bindings binding ON binding.tenant_id=score.tenant_id
WHERE NOT EXISTS(SELECT 1 FROM customer_health_scores newer
  WHERE newer.tenant_id=score.tenant_id
    AND newer.customer_health_score_sequence>score.customer_health_score_sequence);

CREATE TRIGGER trg_workspace_operational_transition_prd096
BEFORE UPDATE ON workspace_operational_states FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.workspace_operational_state_id!=OLD.workspace_operational_state_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at
  OR NOT((NEW.revision=OLD.revision+1 AND NEW.risk_revision=OLD.risk_revision)
    OR(NEW.revision=OLD.revision AND NEW.risk_revision=OLD.risk_revision+1))
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'workspace_operational_transition_invalid'); END;
CREATE TRIGGER trg_workspace_operational_delete_prd096
BEFORE DELETE ON workspace_operational_states FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_operational_immutable'); END;

-- The database guard keeps N-1 application code fail-closed while 0088 remains installed.
CREATE TRIGGER trg_workspace_session_operational_insert_prd096
BEFORE INSERT ON workspace_sessions FOR EACH ROW
WHEN NEW.status='active' AND NOT EXISTS(
  SELECT 1 FROM workspace_operational_states state
  WHERE state.workspace_id=NEW.workspace_id AND state.tenant_id=NEW.tenant_id
    AND state.status='active')
BEGIN SELECT RAISE(ABORT,'workspace_operational_inactive'); END;
CREATE TRIGGER trg_workspace_session_operational_update_prd096
BEFORE UPDATE ON workspace_sessions FOR EACH ROW
WHEN NEW.status='active' AND NOT EXISTS(
  SELECT 1 FROM workspace_operational_states state
  WHERE state.workspace_id=NEW.workspace_id AND state.tenant_id=NEW.tenant_id
    AND state.status='active')
BEGIN SELECT RAISE(ABORT,'workspace_operational_inactive'); END;

CREATE TRIGGER trg_workspace_risk_tags_owner_insert_prd096
BEFORE INSERT ON workspace_risk_tags FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM platform_operators operator
  WHERE operator.operator_id=NEW.created_by_operator_id AND operator.status='active')
  OR NEW.created_by_operator_id!=NEW.updated_by_operator_id
BEGIN SELECT RAISE(ABORT,'workspace_risk_actor_invalid'); END;
CREATE TRIGGER trg_workspace_risk_tags_transition_prd096
BEFORE UPDATE ON workspace_risk_tags FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.workspace_risk_tag_id!=OLD.workspace_risk_tag_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.tag_code!=OLD.tag_code OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.status=OLD.status OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'workspace_risk_transition_invalid'); END;
CREATE TRIGGER trg_workspace_risk_tags_delete_prd096
BEFORE DELETE ON workspace_risk_tags FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_risk_immutable'); END;

CREATE TRIGGER trg_workspace_health_transition_prd096
BEFORE UPDATE ON workspace_health_projections FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.workspace_health_projection_id!=OLD.workspace_health_projection_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.source_sequence<=OLD.source_sequence
  OR unixepoch(NEW.projected_at)<unixepoch(OLD.projected_at)
BEGIN SELECT RAISE(ABORT,'workspace_health_projection_invalid'); END;
CREATE TRIGGER trg_workspace_health_delete_prd096
BEFORE DELETE ON workspace_health_projections FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_health_projection_immutable'); END;

CREATE TRIGGER trg_platform_action_intents_update_prd096
BEFORE UPDATE ON platform_action_intents FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'platform_action_intent_immutable'); END;
CREATE TRIGGER trg_platform_action_intents_delete_prd096
BEFORE DELETE ON platform_action_intents FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'platform_action_intent_immutable'); END;

CREATE TRIGGER trg_workspace_notification_owner_prd096
BEFORE INSERT ON workspace_owner_notification_outbox FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM workspace_memberships member
  JOIN global_accounts account ON account.account_id=member.account_id
  WHERE member.workspace_membership_id=NEW.owner_membership_id
    AND member.workspace_id=NEW.workspace_id AND member.tenant_id=NEW.tenant_id
    AND member.account_id=NEW.owner_account_id AND member.role_code='owner'
    AND member.status='active' AND account.status='active' AND account.login_enabled=1)
BEGIN SELECT RAISE(ABORT,'workspace_notification_owner_invalid'); END;
CREATE TRIGGER trg_workspace_notification_transition_prd096
BEFORE UPDATE ON workspace_owner_notification_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.workspace_owner_notification_id!=OLD.workspace_owner_notification_id
  OR NEW.platform_action_intent_id!=OLD.platform_action_intent_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.owner_membership_id!=OLD.owner_membership_id
  OR NEW.owner_account_id!=OLD.owner_account_id OR NEW.template_code!=OLD.template_code
  OR NEW.payload_json!=OLD.payload_json OR NEW.payload_sha256!=OLD.payload_sha256
  OR NEW.created_at!=OLD.created_at OR NEW.attempt_count<OLD.attempt_count
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'workspace_notification_transition_invalid'); END;
CREATE TRIGGER trg_workspace_notification_delete_prd096
BEFORE DELETE ON workspace_owner_notification_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_notification_immutable'); END;

CREATE TRIGGER trg_workspace_operational_events_update_prd096
BEFORE UPDATE ON workspace_operational_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_operational_event_immutable'); END;
CREATE TRIGGER trg_workspace_operational_events_delete_prd096
BEFORE DELETE ON workspace_operational_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_operational_event_immutable'); END;
CREATE TRIGGER trg_workspace_operational_event_outbox_prd096
AFTER INSERT ON workspace_operational_events FOR EACH ROW
BEGIN
  INSERT INTO workspace_operational_event_outbox(
    workspace_operational_event_outbox_id,workspace_operational_event_id,event_type,
    status,attempt_count,next_attempt_at,delivered_at,created_at,updated_at)
  VALUES('woo_'||lower(hex(randomblob(16))),NEW.workspace_operational_event_id,
    NEW.event_type,'pending',0,NEW.occurred_at,NULL,NEW.occurred_at,NEW.occurred_at);
END;
CREATE TRIGGER trg_workspace_operational_outbox_transition_prd096
BEFORE UPDATE ON workspace_operational_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.workspace_operational_event_outbox_id!=OLD.workspace_operational_event_outbox_id
  OR NEW.workspace_operational_event_id!=OLD.workspace_operational_event_id
  OR NEW.event_type!=OLD.event_type OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'workspace_operational_outbox_transition_invalid'); END;
CREATE TRIGGER trg_workspace_operational_outbox_delete_prd096
BEFORE DELETE ON workspace_operational_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_operational_outbox_immutable'); END;

CREATE TRIGGER trg_workspace_binding_operational_initialize_prd096
AFTER INSERT ON workspace_tenant_bindings FOR EACH ROW
BEGIN
  INSERT INTO workspace_operational_states(
    workspace_operational_state_id,workspace_id,tenant_id,status,revision,risk_revision,
    source,suspended_reason_code,suspended_by_operator_id,suspended_at,created_at,updated_at)
  SELECT 'wos_'||lower(hex(randomblob(16))),NEW.workspace_id,NEW.tenant_id,
    CASE WHEN workspace.status='active' AND NEW.status='active' AND tenant.status='active'
      AND data_state.lifecycle_status='active' THEN 'active' ELSE 'migration_conflict' END,
    1,1,'workspace_creation',NULL,NULL,NULL,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM workspaces workspace
  JOIN tenants tenant ON tenant.tenant_id=NEW.tenant_id
  JOIN tenant_data_states data_state ON data_state.tenant_id=NEW.tenant_id
  WHERE workspace.workspace_id=NEW.workspace_id;
END;

CREATE TRIGGER trg_tenant_data_operational_transition_prd096
AFTER UPDATE OF lifecycle_status ON tenant_data_states FOR EACH ROW
WHEN NEW.lifecycle_status!=OLD.lifecycle_status
BEGIN
  UPDATE workspace_operational_states
  SET status=CASE WHEN NEW.lifecycle_status='deleted'
      AND (SELECT status FROM tenants WHERE tenant_id=NEW.tenant_id)='disabled'
      THEN 'deleted' ELSE 'deletion_pending' END,
    revision=revision+1,source='data_lifecycle',
    suspended_reason_code=NULL,suspended_by_operator_id=NULL,suspended_at=NULL,
    updated_at=NEW.updated_at
  WHERE tenant_id=NEW.tenant_id AND status!='deleted';
END;

CREATE TRIGGER trg_customer_health_workspace_projection_prd096
AFTER INSERT ON customer_health_scores FOR EACH ROW
BEGIN
  INSERT INTO workspace_health_projections(
    workspace_health_projection_id,workspace_id,tenant_id,source_score_id,source_sequence,
    score_band,risk_level,risk_tags_json,source_projection_sha256,
    source_evaluated_at,projected_at)
  SELECT 'whp_'||lower(hex(randomblob(16))),binding.workspace_id,NEW.tenant_id,
    NEW.customer_health_score_id,NEW.customer_health_score_sequence,
    NEW.risk_level,NEW.risk_level,
    json('['||rtrim(
      CASE WHEN NEW.usage_declining=1 THEN '"usage_declining",' ELSE '' END||
      CASE WHEN NEW.high_error_rate=1 THEN '"high_error_rate",' ELSE '' END||
      CASE WHEN NEW.cost_anomaly_open=1 THEN '"cost_anomaly_open",' ELSE '' END||
      CASE WHEN NEW.budget_missing=1 THEN '"budget_missing",' ELSE '' END||
      CASE WHEN NEW.team_inactive=1 THEN '"team_inactive",' ELSE '' END,',')||']'),
    NEW.health_projection_sha256,NEW.evaluated_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM workspace_tenant_bindings binding WHERE binding.tenant_id=NEW.tenant_id
  ON CONFLICT(workspace_id) DO UPDATE SET
    source_score_id=excluded.source_score_id,
    source_sequence=excluded.source_sequence,
    score_band=excluded.score_band,
    risk_level=excluded.risk_level,
    risk_tags_json=excluded.risk_tags_json,
    source_projection_sha256=excluded.source_projection_sha256,
    source_evaluated_at=excluded.source_evaluated_at,
    projected_at=excluded.projected_at
  WHERE excluded.source_sequence>workspace_health_projections.source_sequence;
END;

CREATE TEMP TABLE prd096_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd096_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_operational_states)=
    (SELECT COUNT(*) FROM workspace_tenant_bindings) THEN 0 ELSE 1 END
  + CASE WHEN EXISTS(SELECT 1 FROM workspace_operational_states state
      LEFT JOIN workspace_tenant_bindings binding
        ON binding.workspace_id=state.workspace_id AND binding.tenant_id=state.tenant_id
      WHERE binding.workspace_id IS NULL) THEN 1 ELSE 0 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd096_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
