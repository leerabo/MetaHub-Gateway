PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd097_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd097_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0088' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspaces','workspace_tenant_bindings','workspace_memberships','workspace_sessions',
      'workspace_environment_origins','environments','providers','provider_credentials',
      'models','model_aliases','projects','applications','virtual_keys','request_logs',
      'usage_ledger'))=15 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd097')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd097_up_guard;

-- The extra unique owner key lets immutable onboarding records prove the full actor binding
-- without tying history to a mutable Membership revision.
CREATE UNIQUE INDEX uq_workspace_memberships_onboarding_owner_prd097
  ON workspace_memberships(
    workspace_id,tenant_id,workspace_membership_id,account_id
  );

CREATE TABLE onboarding_progress(
  onboarding_progress_id TEXT PRIMARY KEY CHECK(length(onboarding_progress_id)=36
    AND substr(onboarding_progress_id,1,4)='onp_'
    AND substr(onboarding_progress_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  workspace_kind TEXT NOT NULL CHECK(workspace_kind IN('personal','organization')),
  initialization_source TEXT NOT NULL CHECK(initialization_source IN(
    'migration_backfill','workspace_created')),
  generation INTEGER NOT NULL CHECK(generation>=0),
  completed_steps INTEGER NOT NULL CHECK(completed_steps BETWEEN 0 AND 8),
  status TEXT NOT NULL CHECK(status IN('incomplete','active')),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64
    AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  evaluated_at TEXT NOT NULL CHECK(unixepoch(evaluated_at) IS NOT NULL),
  activated_at TEXT CHECK(activated_at IS NULL OR unixepoch(activated_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,tenant_id),
  CHECK((status='active' AND completed_steps=8 AND activated_at IS NOT NULL)
    OR(status='incomplete' AND completed_steps<8))
);
CREATE INDEX idx_onboarding_progress_status_prd097
  ON onboarding_progress(status,updated_at DESC,workspace_id);

CREATE TABLE onboarding_step_facts(
  onboarding_step_fact_id TEXT PRIMARY KEY CHECK(length(onboarding_step_fact_id)=36
    AND substr(onboarding_step_fact_id,1,4)='ons_'
    AND substr(onboarding_step_fact_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  step_id TEXT NOT NULL CHECK(step_id IN(
    'workspace_profile','environment','provider','model','model_alias',
    'project_application','virtual_key','first_call')),
  status TEXT NOT NULL CHECK(status IN(
    'current','blocked','complete','deferred','needs_attention')),
  source TEXT NOT NULL CHECK(source IN(
    'system_created','user_confirmed','verified','legacy_seed','none')),
  evidence_kind TEXT NOT NULL CHECK(evidence_kind IN(
    'profile_confirmation','environment_confirmation','provider_probe','model_binding',
    'alias_binding','project_application_binding','virtual_key_binding','gateway_usage')),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64
    AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_generation INTEGER NOT NULL CHECK(source_generation>=0),
  first_satisfied_at TEXT CHECK(
    first_satisfied_at IS NULL OR unixepoch(first_satisfied_at) IS NOT NULL),
  last_satisfied_at TEXT CHECK(
    last_satisfied_at IS NULL OR unixepoch(last_satisfied_at) IS NOT NULL),
  last_checked_at TEXT NOT NULL CHECK(unixepoch(last_checked_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES onboarding_progress(workspace_id,tenant_id),
  UNIQUE(workspace_id,step_id),
  CHECK((status='complete' AND first_satisfied_at IS NOT NULL
      AND last_satisfied_at IS NOT NULL)
    OR status!='complete')
);
CREATE INDEX idx_onboarding_step_facts_workspace_prd097
  ON onboarding_step_facts(workspace_id,step_id,status);

CREATE TABLE onboarding_acknowledgements(
  onboarding_acknowledgement_id TEXT PRIMARY KEY CHECK(
    length(onboarding_acknowledgement_id)=36
    AND substr(onboarding_acknowledgement_id,1,4)='ona_'
    AND substr(onboarding_acknowledgement_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_membership_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  step_id TEXT NOT NULL CHECK(step_id IN(
    'workspace_profile','environment','provider','model','model_alias',
    'project_application','virtual_key','first_call')),
  action TEXT NOT NULL CHECK(action IN('confirm','defer','reopen')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'user_confirmed','do_later','configuration_changed','retry_required')),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,actor_membership_id,actor_account_id)
    REFERENCES workspace_memberships(
      workspace_id,tenant_id,workspace_membership_id,account_id),
  UNIQUE(actor_membership_id,idempotency_key_hmac)
);
CREATE INDEX idx_onboarding_ack_step_prd097
  ON onboarding_acknowledgements(workspace_id,step_id,created_at DESC,
    onboarding_acknowledgement_id DESC);

CREATE TABLE activation_checkpoints(
  activation_checkpoint_id TEXT PRIMARY KEY CHECK(length(activation_checkpoint_id)=36
    AND substr(activation_checkpoint_id,1,4)='onc_'
    AND substr(activation_checkpoint_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_membership_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  completed_bitset INTEGER NOT NULL CHECK(completed_bitset BETWEEN 0 AND 255),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64
    AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  result TEXT NOT NULL CHECK(result IN(
    'incomplete','activated','active','activation_lost')),
  trigger_source TEXT NOT NULL CHECK(trigger_source IN('manual_recheck','step_action')),
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,actor_membership_id,actor_account_id)
    REFERENCES workspace_memberships(
      workspace_id,tenant_id,workspace_membership_id,account_id),
  UNIQUE(actor_membership_id,idempotency_key_hmac)
);
CREATE INDEX idx_activation_checkpoints_workspace_prd097
  ON activation_checkpoints(workspace_id,generation DESC,activation_checkpoint_id DESC);

CREATE TABLE onboarding_events(
  onboarding_event_id TEXT PRIMARY KEY CHECK(length(onboarding_event_id)=36
    AND substr(onboarding_event_id,1,4)='one_'
    AND substr(onboarding_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  activation_checkpoint_id TEXT NOT NULL,
  step_id TEXT CHECK(step_id IS NULL OR step_id IN(
    'workspace_profile','environment','provider','model','model_alias',
    'project_application','virtual_key','first_call')),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'onboarding.step_completed','onboarding.step_reopened',
    'workspace.activated','workspace.activation_lost')),
  source TEXT NOT NULL CHECK(source IN(
    'system_created','user_confirmed','verified','legacy_seed','none')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64
    AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(activation_checkpoint_id)
    REFERENCES activation_checkpoints(activation_checkpoint_id),
  CHECK((event_type LIKE 'onboarding.step_%' AND step_id IS NOT NULL)
    OR(event_type LIKE 'workspace.%' AND step_id IS NULL)),
  UNIQUE(activation_checkpoint_id,event_type,step_id)
);
CREATE INDEX idx_onboarding_events_workspace_prd097
  ON onboarding_events(workspace_id,occurred_at DESC,onboarding_event_id DESC);

CREATE TABLE onboarding_event_outbox(
  onboarding_event_outbox_id TEXT PRIMARY KEY CHECK(length(onboarding_event_outbox_id)=36
    AND substr(onboarding_event_outbox_id,1,4)='ono_'
    AND substr(onboarding_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  onboarding_event_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(onboarding_event_id) REFERENCES onboarding_events(onboarding_event_id),
  CHECK((status='pending' AND attempt_count=0 AND next_attempt_at IS NOT NULL
      AND delivered_at IS NULL)
    OR(status='processing' AND attempt_count BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND delivered_at IS NULL)
    OR(status='delivered' AND delivered_at IS NOT NULL AND next_attempt_at IS NULL)
    OR(status='failed' AND attempt_count=10 AND delivered_at IS NULL
      AND next_attempt_at IS NULL))
);
CREATE INDEX idx_onboarding_event_outbox_claim_prd097
  ON onboarding_event_outbox(status,next_attempt_at,created_at);

INSERT INTO onboarding_progress(
  onboarding_progress_id,workspace_id,tenant_id,workspace_kind,initialization_source,
  generation,completed_steps,status,projection_sha256,evaluated_at,activated_at,
  revision,created_at,updated_at)
SELECT 'onp_'||substr(metahub_sha256(binding.workspace_id||char(31)||'progress'),1,32),
  binding.workspace_id,binding.tenant_id,workspace.kind,'migration_backfill',0,0,'incomplete',
  metahub_sha256('onboarding-v1'||char(31)||workspace.kind||char(31)||'00000000'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),NULL,1,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM workspace_tenant_bindings binding
JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id;

INSERT INTO onboarding_step_facts(
  onboarding_step_fact_id,workspace_id,tenant_id,step_id,status,source,evidence_kind,
  evidence_sha256,source_generation,first_satisfied_at,last_satisfied_at,last_checked_at,
  revision,created_at,updated_at)
SELECT 'ons_'||substr(metahub_sha256(binding.workspace_id||char(31)||step.step_id),1,32),
  binding.workspace_id,binding.tenant_id,step.step_id,
  CASE WHEN step.step_order=1 THEN 'current' ELSE 'blocked' END,
  CASE
    WHEN step.step_id='workspace_profile' THEN 'system_created'
    WHEN step.step_id='environment' AND EXISTS(
      SELECT 1 FROM workspace_environment_origins origin
      JOIN environments environment
        ON environment.tenant_id=origin.tenant_id
       AND environment.environment_id=origin.environment_id
      WHERE origin.workspace_id=binding.workspace_id AND environment.code='dev'
        AND origin.origin='legacy_existing') THEN 'legacy_seed'
    WHEN step.step_id='environment' AND EXISTS(
      SELECT 1 FROM workspace_environment_origins origin
      JOIN environments environment
        ON environment.tenant_id=origin.tenant_id
       AND environment.environment_id=origin.environment_id
      WHERE origin.workspace_id=binding.workspace_id AND environment.code='dev')
      THEN 'system_created'
    WHEN step.step_id='provider' AND EXISTS(
      SELECT 1 FROM providers provider
      LEFT JOIN provider_credentials credential
        ON credential.tenant_id=provider.tenant_id
       AND credential.provider_id=provider.provider_id
      WHERE provider.tenant_id=binding.tenant_id
        AND (provider.created_by IS NULL
          OR credential.encrypted_key LIKE 'local-placeholder:%')) THEN 'legacy_seed'
    WHEN step.step_id='model' AND EXISTS(
      SELECT 1 FROM models model WHERE model.tenant_id=binding.tenant_id
        AND model.created_by IS NULL) THEN 'legacy_seed'
    WHEN step.step_id='model_alias' AND EXISTS(
      SELECT 1 FROM model_aliases alias_row WHERE alias_row.tenant_id=binding.tenant_id
        AND alias_row.created_by IS NULL) THEN 'legacy_seed'
    WHEN step.step_id='project_application' AND EXISTS(
      SELECT 1 FROM projects project WHERE project.tenant_id=binding.tenant_id
        AND project.created_by IS NULL) THEN 'legacy_seed'
    WHEN step.step_id='virtual_key' AND EXISTS(
      SELECT 1 FROM virtual_keys virtual_key WHERE virtual_key.tenant_id=binding.tenant_id
        AND virtual_key.created_by IS NULL) THEN 'legacy_seed'
    WHEN step.step_id='first_call' AND EXISTS(
      SELECT 1 FROM request_logs request_log
      WHERE request_log.tenant_id=binding.tenant_id AND request_log.status='success')
      THEN 'legacy_seed'
    ELSE 'none'
  END,
  step.evidence_kind,
  metahub_sha256('onboarding-v1'||char(31)||step.step_id||char(31)||'incomplete'),
  0,NULL,NULL,strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM workspace_tenant_bindings binding
CROSS JOIN (
  SELECT 1 step_order,'workspace_profile' step_id,'profile_confirmation' evidence_kind
  UNION ALL SELECT 2,'environment','environment_confirmation'
  UNION ALL SELECT 3,'provider','provider_probe'
  UNION ALL SELECT 4,'model','model_binding'
  UNION ALL SELECT 5,'model_alias','alias_binding'
  UNION ALL SELECT 6,'project_application','project_application_binding'
  UNION ALL SELECT 7,'virtual_key','virtual_key_binding'
  UNION ALL SELECT 8,'first_call','gateway_usage'
) step;

CREATE TRIGGER trg_onboarding_progress_transition_prd097
BEFORE UPDATE ON onboarding_progress FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.workspace_kind!=OLD.workspace_kind
  OR NEW.initialization_source!=OLD.initialization_source
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_progress_transition_invalid'); END;
CREATE TRIGGER trg_onboarding_progress_delete_prd097
BEFORE DELETE ON onboarding_progress FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_progress_delete_forbidden'); END;

CREATE TRIGGER trg_onboarding_step_facts_transition_prd097
BEFORE UPDATE ON onboarding_step_facts FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.step_id!=OLD.step_id OR NEW.evidence_kind!=OLD.evidence_kind
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_step_transition_invalid'); END;
CREATE TRIGGER trg_onboarding_step_facts_delete_prd097
BEFORE DELETE ON onboarding_step_facts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_step_delete_forbidden'); END;

CREATE TRIGGER trg_onboarding_ack_update_prd097
BEFORE UPDATE ON onboarding_acknowledgements FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_ack_immutable'); END;
CREATE TRIGGER trg_onboarding_ack_delete_prd097
BEFORE DELETE ON onboarding_acknowledgements FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_ack_immutable'); END;
CREATE TRIGGER trg_activation_checkpoints_update_prd097
BEFORE UPDATE ON activation_checkpoints FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.activation_checkpoint_immutable'); END;
CREATE TRIGGER trg_activation_checkpoints_delete_prd097
BEFORE DELETE ON activation_checkpoints FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.activation_checkpoint_immutable'); END;
CREATE TRIGGER trg_onboarding_events_update_prd097
BEFORE UPDATE ON onboarding_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_event_immutable'); END;
CREATE TRIGGER trg_onboarding_events_delete_prd097
BEFORE DELETE ON onboarding_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_event_immutable'); END;
CREATE TRIGGER trg_onboarding_outbox_delete_prd097
BEFORE DELETE ON onboarding_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_outbox_delete_forbidden'); END;

CREATE TRIGGER trg_onboarding_event_outbox_prd097
AFTER INSERT ON onboarding_events FOR EACH ROW
BEGIN
  INSERT INTO onboarding_event_outbox(
    onboarding_event_outbox_id,onboarding_event_id,event_type,status,attempt_count,
    next_attempt_at,delivered_at,created_at,updated_at)
  VALUES('ono_'||lower(hex(randomblob(16))),NEW.onboarding_event_id,NEW.event_type,
    'pending',0,NEW.occurred_at,NULL,NEW.occurred_at,NEW.occurred_at);
END;

CREATE TRIGGER trg_workspace_binding_onboarding_initialize_prd097
AFTER INSERT ON workspace_tenant_bindings FOR EACH ROW
BEGIN
  INSERT INTO onboarding_progress(
    onboarding_progress_id,workspace_id,tenant_id,workspace_kind,initialization_source,
    generation,completed_steps,status,projection_sha256,evaluated_at,activated_at,
    revision,created_at,updated_at)
  SELECT 'onp_'||substr(metahub_sha256(NEW.workspace_id||char(31)||'progress'),1,32),
    NEW.workspace_id,NEW.tenant_id,workspace.kind,'workspace_created',0,0,'incomplete',
    metahub_sha256('onboarding-v1'||char(31)||workspace.kind||char(31)||'00000000'),
    NEW.created_at,NULL,1,NEW.created_at,NEW.created_at
  FROM workspaces workspace WHERE workspace.workspace_id=NEW.workspace_id;

  INSERT INTO onboarding_step_facts(
    onboarding_step_fact_id,workspace_id,tenant_id,step_id,status,source,evidence_kind,
    evidence_sha256,source_generation,first_satisfied_at,last_satisfied_at,last_checked_at,
    revision,created_at,updated_at)
  SELECT 'ons_'||substr(metahub_sha256(NEW.workspace_id||char(31)||step.step_id),1,32),
    NEW.workspace_id,NEW.tenant_id,step.step_id,
    CASE WHEN step.step_order=1 THEN 'current' ELSE 'blocked' END,
    CASE WHEN step.step_id='workspace_profile' THEN 'system_created' ELSE 'none' END,
    step.evidence_kind,
    metahub_sha256('onboarding-v1'||char(31)||step.step_id||char(31)||'incomplete'),
    0,NULL,NULL,NEW.created_at,1,NEW.created_at,NEW.created_at
  FROM (
    SELECT 1 step_order,'workspace_profile' step_id,'profile_confirmation' evidence_kind
    UNION ALL SELECT 2,'environment','environment_confirmation'
    UNION ALL SELECT 3,'provider','provider_probe'
    UNION ALL SELECT 4,'model','model_binding'
    UNION ALL SELECT 5,'model_alias','alias_binding'
    UNION ALL SELECT 6,'project_application','project_application_binding'
    UNION ALL SELECT 7,'virtual_key','virtual_key_binding'
    UNION ALL SELECT 8,'first_call','gateway_usage'
  ) step;
END;

CREATE TEMP TABLE prd097_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd097_up_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM workspace_tenant_bindings)*8
    -(SELECT COUNT(*) FROM onboarding_step_facts)
  + (SELECT COUNT(*) FROM onboarding_progress WHERE completed_steps!=0
      OR status!='incomplete' OR generation!=0 OR revision!=1)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd097_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
