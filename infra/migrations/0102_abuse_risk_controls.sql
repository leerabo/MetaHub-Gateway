-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd110_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd110_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0101' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','global_accounts','platform_roles','platform_operators',
      'platform_job_types','platform_jobs'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd110_up_guard;

CREATE TABLE platform_risk_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.risk.read','platform.risk.decide','platform.risk.support_read')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-110'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=12)
);
CREATE TABLE platform_risk_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_risk_permission_extensions(permission_code)
);
INSERT INTO platform_risk_permission_extensions VALUES
  ('platform.risk.read','PRD-110',12),
  ('platform.risk.decide','PRD-110',12),
  ('platform.risk.support_read','PRD-110',12);
INSERT INTO platform_risk_role_permissions VALUES
  ('platform_owner','platform.risk.read'),
  ('platform_owner','platform.risk.decide'),
  ('platform_security','platform.risk.read'),
  ('platform_security','platform.risk.decide'),
  ('platform_support','platform.risk.support_read');
CREATE TRIGGER trg_platform_risk_permissions_insert_prd110
BEFORE INSERT ON platform_risk_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_risk_permission_immutable'); END;
CREATE TRIGGER trg_platform_risk_permissions_update_prd110
BEFORE UPDATE ON platform_risk_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_risk_permission_immutable'); END;
CREATE TRIGGER trg_platform_risk_permissions_delete_prd110
BEFORE DELETE ON platform_risk_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_risk_permission_immutable'); END;
CREATE TRIGGER trg_platform_risk_roles_insert_prd110
BEFORE INSERT ON platform_risk_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_risk_permission_immutable'); END;
CREATE TRIGGER trg_platform_risk_roles_update_prd110
BEFORE UPDATE ON platform_risk_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_risk_permission_immutable'); END;
CREATE TRIGGER trg_platform_risk_roles_delete_prd110
BEFORE DELETE ON platform_risk_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_risk_permission_immutable'); END;

CREATE TABLE abuse_counters(
  abuse_counter_id TEXT PRIMARY KEY CHECK(length(abuse_counter_id)=36
    AND substr(abuse_counter_id,1,4)='abc_'),
  counter_domain TEXT NOT NULL CHECK(counter_domain IN(
    'registration','login','login_recovery','resource_creation','gateway_api')),
  subject_kind TEXT NOT NULL CHECK(subject_kind IN(
    'ip_prefix','device','account','workspace','virtual_key')),
  subject_hmac TEXT NOT NULL CHECK(length(subject_hmac)=64
    AND subject_hmac NOT GLOB '*[^0-9a-f]*'),
  window_started_at TEXT NOT NULL CHECK(unixepoch(window_started_at) IS NOT NULL),
  window_seconds INTEGER NOT NULL CHECK(window_seconds BETWEEN 1 AND 86400),
  current_count INTEGER NOT NULL CHECK(current_count>=0),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(counter_domain,subject_kind,subject_hmac,window_started_at),
  CHECK(unixepoch(expires_at)>unixepoch(window_started_at))
);
CREATE INDEX idx_abuse_counters_expiry_prd110 ON abuse_counters(expires_at,abuse_counter_id);
CREATE TRIGGER trg_abuse_counters_update_prd110
BEFORE UPDATE ON abuse_counters FOR EACH ROW
WHEN NEW.abuse_counter_id!=OLD.abuse_counter_id
  OR NEW.counter_domain!=OLD.counter_domain OR NEW.subject_kind!=OLD.subject_kind
  OR NEW.subject_hmac!=OLD.subject_hmac OR NEW.window_started_at!=OLD.window_started_at
  OR NEW.window_seconds!=OLD.window_seconds OR NEW.current_count<OLD.current_count
  OR NEW.expires_at!=OLD.expires_at OR NEW.revision!=OLD.revision+1
  OR NEW.created_at!=OLD.created_at OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'abuse_counter_transition_invalid'); END;
CREATE TRIGGER trg_abuse_counters_delete_prd110 BEFORE DELETE ON abuse_counters
BEGIN SELECT RAISE(ABORT,'abuse_counter_immutable'); END;

CREATE TABLE abuse_counter_facts(
  abuse_counter_fact_id TEXT PRIMARY KEY CHECK(length(abuse_counter_fact_id)=36
    AND substr(abuse_counter_fact_id,1,4)='abf_'),
  abuse_counter_id TEXT NOT NULL,
  count_after INTEGER NOT NULL CHECK(count_after>=1),
  source_code TEXT NOT NULL CHECK(source_code IN('redis','database_rebuild','test_injected')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(abuse_counter_id) REFERENCES abuse_counters(abuse_counter_id),
  UNIQUE(abuse_counter_id,count_after)
);
CREATE TRIGGER trg_abuse_counter_facts_update_prd110 BEFORE UPDATE ON abuse_counter_facts
BEGIN SELECT RAISE(ABORT,'abuse_counter_fact_immutable'); END;
CREATE TRIGGER trg_abuse_counter_facts_delete_prd110 BEFORE DELETE ON abuse_counter_facts
BEGIN SELECT RAISE(ABORT,'abuse_counter_fact_immutable'); END;

CREATE TABLE risk_cases(
  risk_case_id TEXT PRIMARY KEY CHECK(length(risk_case_id)=36
    AND substr(risk_case_id,1,4)='rca_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  case_kind TEXT NOT NULL CHECK(case_kind IN(
    'credential_stuffing','resource_abuse','api_abuse','account_takeover')),
  safe_reason_family TEXT NOT NULL CHECK(safe_reason_family IN(
    'unusual_volume','repeated_failures','automation_detected','resource_spike','manual_review')),
  score_band TEXT NOT NULL CHECK(score_band IN('low','medium','high','critical')),
  status TEXT NOT NULL CHECK(status IN(
    'open','under_review','restricted','cleared','closed')),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  opened_at TEXT NOT NULL CHECK(unixepoch(opened_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  closed_at TEXT CHECK(closed_at IS NULL OR unixepoch(closed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(risk_case_id,workspace_id,tenant_id),
  CHECK((status IN('cleared','closed') AND closed_at IS NOT NULL)
    OR(status NOT IN('cleared','closed') AND closed_at IS NULL))
);
CREATE INDEX idx_risk_cases_status_prd110
  ON risk_cases(status,updated_at DESC,risk_case_id DESC);
CREATE TRIGGER trg_risk_cases_update_prd110
BEFORE UPDATE ON risk_cases FOR EACH ROW
WHEN NEW.risk_case_id!=OLD.risk_case_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.case_kind!=OLD.case_kind
  OR NEW.safe_reason_family!=OLD.safe_reason_family OR NEW.score_band!=OLD.score_band
  OR NEW.source_watermark<OLD.source_watermark OR NEW.revision!=OLD.revision+1
  OR NEW.opened_at!=OLD.opened_at OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='open' AND NEW.status IN('under_review','restricted','cleared','closed'))
    OR(OLD.status='under_review' AND NEW.status IN('restricted','cleared','closed'))
    OR(OLD.status='restricted' AND NEW.status IN('cleared','closed'))
    OR(OLD.status='cleared' AND NEW.status='closed'))
BEGIN SELECT RAISE(ABORT,'risk_case_transition_invalid'); END;
CREATE TRIGGER trg_risk_cases_delete_prd110 BEFORE DELETE ON risk_cases
BEGIN SELECT RAISE(ABORT,'risk_case_immutable'); END;

CREATE TABLE risk_case_transitions(
  risk_case_transition_id TEXT PRIMARY KEY CHECK(length(risk_case_transition_id)=36
    AND substr(risk_case_transition_id,1,4)='rct_'),
  risk_case_id TEXT NOT NULL,
  from_status TEXT CHECK(from_status IS NULL OR from_status IN(
    'open','under_review','restricted','cleared','closed')),
  to_status TEXT NOT NULL CHECK(to_status IN(
    'open','under_review','restricted','cleared','closed')),
  case_revision INTEGER NOT NULL CHECK(case_revision>=1),
  safe_reason_family TEXT NOT NULL CHECK(length(safe_reason_family) BETWEEN 3 AND 64),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('automatic','platform','appeal_job')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 3 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(risk_case_id) REFERENCES risk_cases(risk_case_id),
  UNIQUE(risk_case_id,case_revision)
);
CREATE TRIGGER trg_risk_case_transitions_update_prd110 BEFORE UPDATE ON risk_case_transitions
BEGIN SELECT RAISE(ABORT,'risk_case_transition_immutable'); END;
CREATE TRIGGER trg_risk_case_transitions_delete_prd110 BEFORE DELETE ON risk_case_transitions
BEGIN SELECT RAISE(ABORT,'risk_case_transition_immutable'); END;

CREATE TABLE risk_signals(
  risk_signal_id TEXT PRIMARY KEY CHECK(length(risk_signal_id)=36
    AND substr(risk_signal_id,1,4)='rsg_'),
  risk_case_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  signal_type TEXT NOT NULL CHECK(signal_type IN(
    'request_velocity','failed_authentication','distributed_source','resource_velocity','credential_reuse')),
  severity TEXT NOT NULL CHECK(severity IN('info','warning','critical')),
  time_bucket TEXT NOT NULL CHECK(unixepoch(time_bucket) IS NOT NULL),
  subject_hmac TEXT NOT NULL CHECK(length(subject_hmac)=64),
  ip_prefix_hmac TEXT CHECK(ip_prefix_hmac IS NULL OR length(ip_prefix_hmac)=64),
  device_pseudonym TEXT CHECK(device_pseudonym IS NULL OR length(device_pseudonym)=64),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  policy_version INTEGER NOT NULL CHECK(policy_version>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(risk_case_id,workspace_id,tenant_id)
    REFERENCES risk_cases(risk_case_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_risk_signals_case_prd110 ON risk_signals(risk_case_id,created_at);
CREATE TRIGGER trg_risk_signals_update_prd110 BEFORE UPDATE ON risk_signals
BEGIN SELECT RAISE(ABORT,'risk_signal_immutable'); END;
CREATE TRIGGER trg_risk_signals_delete_prd110 BEFORE DELETE ON risk_signals
BEGIN SELECT RAISE(ABORT,'risk_signal_immutable'); END;

CREATE TABLE risk_decisions(
  risk_decision_id TEXT PRIMARY KEY CHECK(length(risk_decision_id)=36
    AND substr(risk_decision_id,1,4)='rdc_'),
  risk_case_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN(
    'allow','challenge','required_review','restricted','cleared')),
  safe_reason_family TEXT NOT NULL CHECK(length(safe_reason_family) BETWEEN 3 AND 64),
  policy_version INTEGER NOT NULL CHECK(policy_version>=1),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  source_code TEXT NOT NULL CHECK(source_code IN('automatic','platform','appeal_job')),
  actor_operator_id TEXT,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(risk_case_id,workspace_id,tenant_id)
    REFERENCES risk_cases(risk_case_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((source_code='platform' AND actor_operator_id IS NOT NULL)
    OR(source_code!='platform' AND actor_operator_id IS NULL))
);
CREATE TRIGGER trg_risk_decisions_update_prd110 BEFORE UPDATE ON risk_decisions
BEGIN SELECT RAISE(ABORT,'risk_decision_immutable'); END;
CREATE TRIGGER trg_risk_decisions_delete_prd110 BEFORE DELETE ON risk_decisions
BEGIN SELECT RAISE(ABORT,'risk_decision_immutable'); END;

CREATE TABLE risk_restrictions(
  risk_restriction_id TEXT PRIMARY KEY CHECK(length(risk_restriction_id)=36
    AND substr(risk_restriction_id,1,4)='rst_'),
  risk_case_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_kind TEXT NOT NULL CHECK(subject_kind IN('account','virtual_key','workspace')),
  subject_account_id TEXT,
  subject_hmac TEXT NOT NULL CHECK(length(subject_hmac)=64),
  restriction_kind TEXT NOT NULL CHECK(restriction_kind IN(
    'api_calls','resource_creation','workspace_access')),
  status TEXT NOT NULL CHECK(status IN('active','cleared','expired')),
  starts_at TEXT NOT NULL CHECK(unixepoch(starts_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  cleared_at TEXT CHECK(cleared_at IS NULL OR unixepoch(cleared_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(risk_case_id,workspace_id,tenant_id)
    REFERENCES risk_cases(risk_case_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(subject_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(risk_case_id,restriction_kind),
  CHECK(unixepoch(expires_at)>unixepoch(starts_at)),
  CHECK((subject_kind='account' AND subject_account_id IS NOT NULL)
    OR(subject_kind!='account' AND subject_account_id IS NULL)),
  CHECK((status='active' AND cleared_at IS NULL)
    OR(status!='active' AND cleared_at IS NOT NULL))
);
CREATE INDEX idx_risk_restrictions_active_prd110
  ON risk_restrictions(status,expires_at,risk_restriction_id);
CREATE TRIGGER trg_risk_restrictions_update_prd110
BEFORE UPDATE ON risk_restrictions FOR EACH ROW
WHEN NEW.risk_restriction_id!=OLD.risk_restriction_id OR NEW.risk_case_id!=OLD.risk_case_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.subject_kind!=OLD.subject_kind OR NEW.subject_account_id IS NOT OLD.subject_account_id
  OR NEW.subject_hmac!=OLD.subject_hmac OR NEW.restriction_kind!=OLD.restriction_kind
  OR NEW.starts_at!=OLD.starts_at OR NEW.expires_at!=OLD.expires_at
  OR NEW.revision!=OLD.revision+1 OR NEW.created_at!=OLD.created_at
  OR NEW.updated_at<=OLD.updated_at OR OLD.status!='active'
  OR NEW.status NOT IN('cleared','expired')
BEGIN SELECT RAISE(ABORT,'risk_restriction_transition_invalid'); END;
CREATE TRIGGER trg_risk_restrictions_delete_prd110 BEFORE DELETE ON risk_restrictions
BEGIN SELECT RAISE(ABORT,'risk_restriction_immutable'); END;

CREATE TABLE risk_challenges(
  risk_challenge_id TEXT PRIMARY KEY CHECK(length(risk_challenge_id)=36
    AND substr(risk_challenge_id,1,4)='rch_'),
  risk_case_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  challenge_kind TEXT NOT NULL CHECK(challenge_kind IN('email_otp','totp','proof_of_control')),
  token_hmac TEXT NOT NULL CHECK(length(token_hmac)=64),
  answer_hmac TEXT NOT NULL CHECK(length(answer_hmac)=64),
  intent_sha256 TEXT NOT NULL CHECK(length(intent_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending','consumed','failed','expired')),
  attempts INTEGER NOT NULL CHECK(attempts BETWEEN 0 AND 5),
  max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 5),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(risk_case_id,workspace_id,tenant_id)
    REFERENCES risk_cases(risk_case_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(token_hmac),
  CHECK(attempts<=max_attempts),
  CHECK((status='consumed' AND consumed_at IS NOT NULL)
    OR(status!='consumed' AND consumed_at IS NULL))
);
CREATE TRIGGER trg_risk_challenges_update_prd110
BEFORE UPDATE ON risk_challenges FOR EACH ROW
WHEN NEW.risk_challenge_id!=OLD.risk_challenge_id OR NEW.risk_case_id!=OLD.risk_case_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.challenge_kind!=OLD.challenge_kind
  OR NEW.token_hmac!=OLD.token_hmac OR NEW.answer_hmac!=OLD.answer_hmac
  OR NEW.intent_sha256!=OLD.intent_sha256 OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.expires_at!=OLD.expires_at OR NEW.attempts NOT IN(OLD.attempts,OLD.attempts+1)
  OR NEW.revision!=OLD.revision+1 OR NEW.created_at!=OLD.created_at
  OR NEW.updated_at<=OLD.updated_at OR OLD.status!='pending'
  OR NEW.status NOT IN('pending','consumed','failed','expired')
BEGIN SELECT RAISE(ABORT,'risk_challenge_transition_invalid'); END;
CREATE TRIGGER trg_risk_challenges_delete_prd110 BEFORE DELETE ON risk_challenges
BEGIN SELECT RAISE(ABORT,'risk_challenge_immutable'); END;

CREATE TABLE risk_challenge_attempts(
  risk_challenge_attempt_id TEXT PRIMARY KEY CHECK(length(risk_challenge_attempt_id)=36
    AND substr(risk_challenge_attempt_id,1,4)='rat_'),
  risk_challenge_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 5),
  outcome TEXT NOT NULL CHECK(outcome IN('accepted','rejected','expired')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(risk_challenge_id) REFERENCES risk_challenges(risk_challenge_id),
  UNIQUE(risk_challenge_id,attempt_number)
);
CREATE TRIGGER trg_risk_challenge_attempts_update_prd110 BEFORE UPDATE ON risk_challenge_attempts
BEGIN SELECT RAISE(ABORT,'risk_challenge_attempt_immutable'); END;
CREATE TRIGGER trg_risk_challenge_attempts_delete_prd110 BEFORE DELETE ON risk_challenge_attempts
BEGIN SELECT RAISE(ABORT,'risk_challenge_attempt_immutable'); END;

CREATE TABLE risk_challenge_commands(
  risk_challenge_command_id TEXT PRIMARY KEY CHECK(length(risk_challenge_command_id)=36
    AND substr(risk_challenge_command_id,1,4)='rcc_'),
  risk_challenge_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_status TEXT NOT NULL CHECK(result_status='consumed'),
  result_revision INTEGER NOT NULL CHECK(result_revision>=2),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(risk_challenge_id) REFERENCES risk_challenges(risk_challenge_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,idempotency_sha256)
);
CREATE TRIGGER trg_risk_challenge_commands_update_prd110 BEFORE UPDATE ON risk_challenge_commands
BEGIN SELECT RAISE(ABORT,'risk_challenge_command_immutable'); END;
CREATE TRIGGER trg_risk_challenge_commands_delete_prd110 BEFORE DELETE ON risk_challenge_commands
BEGIN SELECT RAISE(ABORT,'risk_challenge_command_immutable'); END;

CREATE TABLE risk_appeals(
  risk_appeal_id TEXT PRIMARY KEY CHECK(length(risk_appeal_id)=36
    AND substr(risk_appeal_id,1,4)='rap_'),
  risk_case_id TEXT NOT NULL,
  risk_restriction_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN(
    'not_my_activity','expected_usage','restriction_error','other')),
  detail_ciphertext TEXT,
  detail_sha256 TEXT CHECK(detail_sha256 IS NULL OR length(detail_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending','approved','rejected')),
  resolution_reason TEXT CHECK(resolution_reason IS NULL OR resolution_reason IN(
    'verified_false_positive','restriction_confirmed','insufficient_evidence')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  resolved_at TEXT CHECK(resolved_at IS NULL OR unixepoch(resolved_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(risk_case_id,workspace_id,tenant_id)
    REFERENCES risk_cases(risk_case_id,workspace_id,tenant_id),
  FOREIGN KEY(risk_restriction_id) REFERENCES risk_restrictions(risk_restriction_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(account_id,idempotency_sha256),
  CHECK((detail_ciphertext IS NULL)=(detail_sha256 IS NULL)),
  CHECK((status='pending' AND resolution_reason IS NULL AND resolved_at IS NULL)
    OR(status!='pending' AND resolution_reason IS NOT NULL AND resolved_at IS NOT NULL))
);
CREATE INDEX idx_risk_appeals_case_prd110 ON risk_appeals(risk_case_id,created_at);
CREATE TRIGGER trg_risk_appeals_update_prd110
BEFORE UPDATE ON risk_appeals FOR EACH ROW
WHEN NEW.risk_appeal_id!=OLD.risk_appeal_id OR NEW.risk_case_id!=OLD.risk_case_id
  OR NEW.risk_restriction_id!=OLD.risk_restriction_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.category!=OLD.category
  OR NEW.detail_ciphertext IS NOT OLD.detail_ciphertext OR NEW.detail_sha256 IS NOT OLD.detail_sha256
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256 OR NEW.request_sha256!=OLD.request_sha256
  OR OLD.status!='pending' OR NEW.status NOT IN('approved','rejected')
  OR NEW.revision!=OLD.revision+1 OR NEW.created_at!=OLD.created_at
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'risk_appeal_transition_invalid'); END;
CREATE TRIGGER trg_risk_appeals_delete_prd110 BEFORE DELETE ON risk_appeals
BEGIN SELECT RAISE(ABORT,'risk_appeal_immutable'); END;

CREATE TABLE risk_commands(
  risk_command_id TEXT PRIMARY KEY CHECK(length(risk_command_id)=36
    AND substr(risk_command_id,1,4)='rcm_'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code='risk.case.decision'),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_case_id TEXT NOT NULL,
  result_decision_id TEXT NOT NULL,
  result_revision INTEGER NOT NULL CHECK(result_revision>=2),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(result_case_id) REFERENCES risk_cases(risk_case_id),
  FOREIGN KEY(result_decision_id) REFERENCES risk_decisions(risk_decision_id),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE TRIGGER trg_risk_commands_update_prd110 BEFORE UPDATE ON risk_commands
BEGIN SELECT RAISE(ABORT,'risk_command_immutable'); END;
CREATE TRIGGER trg_risk_commands_delete_prd110 BEFORE DELETE ON risk_commands
BEGIN SELECT RAISE(ABORT,'risk_command_immutable'); END;

CREATE TABLE risk_event_outbox(
  risk_event_outbox_id TEXT PRIMARY KEY CHECK(length(risk_event_outbox_id)=36
    AND substr(risk_event_outbox_id,1,4)='reo_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'risk.challenge_required','risk.workspace_restricted','risk.appeal_resolved')),
  risk_case_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  safe_reason_family TEXT NOT NULL CHECK(length(safe_reason_family) BETWEEN 3 AND 64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(risk_case_id,workspace_id,tenant_id)
    REFERENCES risk_cases(risk_case_id,workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TRIGGER trg_risk_event_outbox_update_prd110 BEFORE UPDATE ON risk_event_outbox
BEGIN SELECT RAISE(ABORT,'risk_event_immutable'); END;
CREATE TRIGGER trg_risk_event_outbox_delete_prd110 BEFORE DELETE ON risk_event_outbox
BEGIN SELECT RAISE(ABORT,'risk_event_immutable'); END;

CREATE TABLE risk_degraded_counters(
  risk_degraded_counter_id TEXT PRIMARY KEY CHECK(risk_degraded_counter_id=
    'rdg_00000000000000000000000000000001'),
  privilege_fail_closed_count INTEGER NOT NULL CHECK(privilege_fail_closed_count>=0),
  gateway_fail_open_count INTEGER NOT NULL CHECK(gateway_fail_open_count>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO risk_degraded_counters VALUES
  ('rdg_00000000000000000000000000000001',0,0,1,datetime('now'));
CREATE TRIGGER trg_risk_degraded_counters_update_prd110
BEFORE UPDATE ON risk_degraded_counters FOR EACH ROW
WHEN NEW.risk_degraded_counter_id!=OLD.risk_degraded_counter_id
  OR NEW.privilege_fail_closed_count<OLD.privilege_fail_closed_count
  OR NEW.gateway_fail_open_count<OLD.gateway_fail_open_count
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'risk_degraded_counter_invalid'); END;
CREATE TRIGGER trg_risk_degraded_counters_delete_prd110 BEFORE DELETE ON risk_degraded_counters
BEGIN SELECT RAISE(ABORT,'risk_degraded_counter_immutable'); END;

UPDATE platform_job_types
SET status='active',revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='risk.restriction.expire' AND status='disabled' AND revision=1;

CREATE TEMP TABLE prd110_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd110_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_risk_permission_extensions)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_risk_role_permissions)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='risk.restriction.expire')='active:2' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT privilege_fail_closed_count||':'||gateway_fail_open_count||':'||revision
      FROM risk_degraded_counters)='0:0:1' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'abuse_counters','abuse_counter_facts','risk_cases','risk_case_transitions',
      'risk_signals','risk_decisions','risk_restrictions','risk_challenges',
      'risk_challenge_attempts','risk_challenge_commands','risk_appeals','risk_commands',
      'risk_event_outbox','risk_degraded_counters'))=14 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd110_up_final_guard;
COMMIT;
