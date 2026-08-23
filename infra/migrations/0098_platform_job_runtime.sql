-- Build the platform-only durable Scheduler/Worker runtime without moving Workspace batch facts.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd106_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd106_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0097' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','platform_operator_role_bindings',
      'platform_audit_logs'))=4 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd106')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd106_up_guard;

CREATE TABLE platform_job_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.job.read','platform.job.manage','platform.job.type_manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-106'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=8)
);
CREATE TABLE platform_job_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_job_permission_extensions(permission_code)
);
INSERT INTO platform_job_permission_extensions VALUES
  ('platform.job.read','PRD-106',8),
  ('platform.job.manage','PRD-106',8),
  ('platform.job.type_manage','PRD-106',8);
INSERT INTO platform_job_role_permissions VALUES
  ('platform_owner','platform.job.read'),
  ('platform_owner','platform.job.manage'),
  ('platform_owner','platform.job.type_manage'),
  ('platform_ops','platform.job.read'),
  ('platform_ops','platform.job.manage'),
  ('platform_ops','platform.job.type_manage');
CREATE TRIGGER trg_platform_job_permissions_insert_prd106
BEFORE INSERT ON platform_job_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_job_permission_immutable'); END;
CREATE TRIGGER trg_platform_job_permissions_update_prd106
BEFORE UPDATE ON platform_job_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_job_permission_immutable'); END;
CREATE TRIGGER trg_platform_job_permissions_delete_prd106
BEFORE DELETE ON platform_job_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_job_permission_immutable'); END;
CREATE TRIGGER trg_platform_job_role_permissions_insert_prd106
BEFORE INSERT ON platform_job_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_job_permission_immutable'); END;
CREATE TRIGGER trg_platform_job_role_permissions_update_prd106
BEFORE UPDATE ON platform_job_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_job_permission_immutable'); END;
CREATE TRIGGER trg_platform_job_role_permissions_delete_prd106
BEFORE DELETE ON platform_job_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_job_permission_immutable'); END;

CREATE TABLE platform_job_types(
  type_code TEXT PRIMARY KEY CHECK(length(type_code) BETWEEN 3 AND 80
    AND type_code=lower(type_code) AND type_code NOT GLOB '*[^a-z0-9_.-]*'),
  contract_version INTEGER NOT NULL CHECK(contract_version=1),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64
    AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
  queue_code TEXT NOT NULL CHECK(queue_code IN('critical','standard','bulk')),
  resource_class TEXT NOT NULL CHECK(resource_class IN('control','io','compute')),
  max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 20),
  timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 86400),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO platform_job_types VALUES
  ('platform.runtime.probe',1,'ef2a29612c6c3018d752debdef38e6614b38369a29f17862b13f9b2d17f34008','standard','control',3,30,'active',1,datetime('now'),datetime('now')),
  ('feature.rollout.step',1,'9437dec9f27297cac3c0772037b078a7fe9b48afdfe04ab6ddd0310bd14f8356','critical','control',5,300,'disabled',1,datetime('now'),datetime('now')),
  ('config.snapshot.rebuild',1,'1ebb88b44fe9e58b6470dee24db85aa53d07381fb420094703780e5a97b4ee8b','critical','compute',5,3600,'disabled',1,datetime('now'),datetime('now')),
  ('observability.aggregate',1,'d4643cf6e892d761d51c131d9a2018587bca4a1e2255a2f9ebea92684a3b51f2','standard','compute',5,900,'disabled',1,datetime('now'),datetime('now')),
  ('risk.restriction.expire',1,'29bcccfb0c115578dfd084b11efdcdd7ba1e2e4d6e85690f68ac4c9ea5d17f23','critical','control',5,120,'disabled',1,datetime('now'),datetime('now')),
  ('dr.run',1,'417f9430600f82f96835c80f594e132de6aab5fe86ff0c6159a9464b84cc6edd','critical','io',3,86400,'disabled',1,datetime('now'),datetime('now')),
  ('status.publish',1,'ef3f4ac800546f9efb896e044c32c292c7190d5cd65a20a6bfa0a467f11d76a2','critical','io',5,300,'disabled',1,datetime('now'),datetime('now')),
  ('capacity.run',1,'3bf48ae980924f284b3f41baccf181b5aee7337852172e4c10627b46d9cf6ace','bulk','compute',3,86400,'disabled',1,datetime('now'),datetime('now'));
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;
CREATE TRIGGER trg_platform_job_types_delete_prd106 BEFORE DELETE ON platform_job_types
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TABLE platform_work_queues(
  platform_work_queue_id TEXT PRIMARY KEY CHECK(length(platform_work_queue_id)=36
    AND substr(platform_work_queue_id,1,4)='pjq_'),
  queue_code TEXT NOT NULL UNIQUE CHECK(queue_code IN('critical','standard','bulk')),
  priority_weight INTEGER NOT NULL CHECK(priority_weight BETWEEN 1 AND 100),
  max_concurrency INTEGER NOT NULL CHECK(max_concurrency BETWEEN 1 AND 1000),
  status TEXT NOT NULL CHECK(status='active'),
  revision INTEGER NOT NULL CHECK(revision=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
INSERT INTO platform_work_queues VALUES
  ('pjq_00000000000000000000000000000001','critical',100,8,'active',1,datetime('now')),
  ('pjq_00000000000000000000000000000002','standard',50,8,'active',1,datetime('now')),
  ('pjq_00000000000000000000000000000003','bulk',10,2,'active',1,datetime('now'));
CREATE TRIGGER trg_platform_work_queues_update_prd106 BEFORE UPDATE ON platform_work_queues
BEGIN SELECT RAISE(ABORT,'platform_work_queue_immutable'); END;
CREATE TRIGGER trg_platform_work_queues_delete_prd106 BEFORE DELETE ON platform_work_queues
BEGIN SELECT RAISE(ABORT,'platform_work_queue_immutable'); END;

CREATE TABLE platform_jobs(
  platform_job_id TEXT PRIMARY KEY CHECK(length(platform_job_id)=37
    AND substr(platform_job_id,1,5)='pjob_'),
  type_code TEXT NOT NULL,
  queue_code TEXT NOT NULL,
  priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
  status TEXT NOT NULL CHECK(status IN(
    'queued','leased','running','cancel_requested','succeeded','failed','cancelled','dead_lettered')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  generation INTEGER NOT NULL CHECK(generation>=0),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 20),
  input_envelope TEXT NOT NULL CHECK(length(input_envelope) BETWEEN 40 AND 131072),
  input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64
    AND input_sha256 NOT GLOB '*[^0-9a-f]*'),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_code TEXT NOT NULL CHECK(length(source_code) BETWEEN 3 AND 80),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  safe_outcome_code TEXT CHECK(safe_outcome_code IS NULL
    OR (length(safe_outcome_code) BETWEEN 3 AND 96 AND safe_outcome_code NOT GLOB '*[^a-z0-9_.-]*')),
  scheduled_at TEXT NOT NULL CHECK(unixepoch(scheduled_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(type_code) REFERENCES platform_job_types(type_code),
  FOREIGN KEY(queue_code) REFERENCES platform_work_queues(queue_code),
  UNIQUE(type_code,idempotency_sha256),
  CHECK((status IN('succeeded','failed','cancelled','dead_lettered') AND completed_at IS NOT NULL)
    OR(status NOT IN('succeeded','failed','cancelled','dead_lettered') AND completed_at IS NULL))
);
CREATE INDEX idx_platform_jobs_claim_prd106
  ON platform_jobs(status,scheduled_at,priority DESC,created_at,platform_job_id);
CREATE INDEX idx_platform_jobs_queue_prd106
  ON platform_jobs(queue_code,status,created_at DESC);
CREATE TRIGGER trg_platform_jobs_owner_update_prd106
BEFORE UPDATE ON platform_jobs FOR EACH ROW
WHEN NEW.platform_job_id!=OLD.platform_job_id OR NEW.type_code!=OLD.type_code
  OR NEW.queue_code!=OLD.queue_code OR NEW.priority!=OLD.priority
  OR NEW.max_attempts!=OLD.max_attempts OR NEW.input_envelope!=OLD.input_envelope
  OR NEW.input_sha256!=OLD.input_sha256 OR NEW.idempotency_sha256!=OLD.idempotency_sha256
  OR NEW.source_code!=OLD.source_code OR NEW.request_id!=OLD.request_id
  OR NEW.trace_id!=OLD.trace_id OR NEW.created_at!=OLD.created_at
  OR NEW.generation<OLD.generation OR NEW.attempt_count<OLD.attempt_count
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_owner_immutable'); END;
CREATE TRIGGER trg_platform_jobs_delete_prd106 BEFORE DELETE ON platform_jobs
BEGIN SELECT RAISE(ABORT,'platform_job_immutable'); END;

CREATE TABLE platform_job_attempts(
  platform_job_attempt_id TEXT PRIMARY KEY CHECK(length(platform_job_attempt_id)=36
    AND substr(platform_job_attempt_id,1,4)='pja_'),
  platform_job_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 20),
  generation INTEGER NOT NULL CHECK(generation>=1),
  worker_code TEXT NOT NULL CHECK(length(worker_code) BETWEEN 3 AND 96),
  status TEXT NOT NULL CHECK(status IN('leased','running','succeeded','failed','cancelled','stale')),
  safe_outcome_code TEXT CHECK(safe_outcome_code IS NULL
    OR (length(safe_outcome_code) BETWEEN 3 AND 96 AND safe_outcome_code NOT GLOB '*[^a-z0-9_.-]*')),
  leased_at TEXT NOT NULL CHECK(unixepoch(leased_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  UNIQUE(platform_job_id,attempt_number),
  UNIQUE(platform_job_id,generation),
  CHECK((status IN('succeeded','failed','cancelled','stale') AND completed_at IS NOT NULL)
    OR(status IN('leased','running') AND completed_at IS NULL))
);
CREATE INDEX idx_platform_job_attempts_job_prd106
  ON platform_job_attempts(platform_job_id,attempt_number DESC);
CREATE TRIGGER trg_platform_job_attempts_update_prd106
BEFORE UPDATE ON platform_job_attempts FOR EACH ROW
WHEN NEW.platform_job_attempt_id!=OLD.platform_job_attempt_id
  OR NEW.platform_job_id!=OLD.platform_job_id OR NEW.attempt_number!=OLD.attempt_number
  OR NEW.generation!=OLD.generation OR NEW.worker_code!=OLD.worker_code
  OR NEW.leased_at!=OLD.leased_at OR OLD.status IN('succeeded','failed','cancelled','stale')
BEGIN SELECT RAISE(ABORT,'platform_job_attempt_immutable'); END;
CREATE TRIGGER trg_platform_job_attempts_delete_prd106 BEFORE DELETE ON platform_job_attempts
BEGIN SELECT RAISE(ABORT,'platform_job_attempt_immutable'); END;

CREATE TABLE platform_scheduler_leases(
  platform_scheduler_lease_id TEXT PRIMARY KEY CHECK(length(platform_scheduler_lease_id)=36
    AND substr(platform_scheduler_lease_id,1,4)='pjl_'),
  platform_job_id TEXT NOT NULL,
  platform_job_attempt_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  worker_code TEXT NOT NULL CHECK(length(worker_code) BETWEEN 3 AND 96),
  lease_token_hmac TEXT NOT NULL CHECK(length(lease_token_hmac)=64
    AND lease_token_hmac NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','released','expired')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  acquired_at TEXT NOT NULL CHECK(unixepoch(acquired_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(platform_job_attempt_id) REFERENCES platform_job_attempts(platform_job_attempt_id),
  UNIQUE(platform_job_id,generation),
  CHECK((status='active' AND released_at IS NULL)
    OR(status IN('released','expired') AND released_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_platform_scheduler_active_job_prd106
  ON platform_scheduler_leases(platform_job_id) WHERE status='active';
CREATE INDEX idx_platform_scheduler_expiry_prd106
  ON platform_scheduler_leases(status,expires_at,platform_job_id);
CREATE TRIGGER trg_platform_scheduler_leases_update_prd106
BEFORE UPDATE ON platform_scheduler_leases FOR EACH ROW
WHEN NEW.platform_scheduler_lease_id!=OLD.platform_scheduler_lease_id
  OR NEW.platform_job_id!=OLD.platform_job_id
  OR NEW.platform_job_attempt_id!=OLD.platform_job_attempt_id
  OR NEW.generation!=OLD.generation OR NEW.worker_code!=OLD.worker_code
  OR NEW.lease_token_hmac!=OLD.lease_token_hmac OR NEW.acquired_at!=OLD.acquired_at
  OR OLD.status!='active'
  OR (NEW.status='active' AND NEW.released_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'platform_scheduler_lease_immutable'); END;
CREATE TRIGGER trg_platform_scheduler_leases_delete_prd106 BEFORE DELETE ON platform_scheduler_leases
BEGIN SELECT RAISE(ABORT,'platform_scheduler_lease_immutable'); END;

CREATE TABLE platform_job_checkpoints(
  platform_job_checkpoint_id TEXT PRIMARY KEY CHECK(length(platform_job_checkpoint_id)=36
    AND substr(platform_job_checkpoint_id,1,4)='pjc_'),
  platform_job_id TEXT NOT NULL,
  platform_job_attempt_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  sequence_number INTEGER NOT NULL CHECK(sequence_number>=1),
  checkpoint_envelope TEXT NOT NULL CHECK(length(checkpoint_envelope) BETWEEN 40 AND 131072),
  checkpoint_sha256 TEXT NOT NULL CHECK(length(checkpoint_sha256)=64
    AND checkpoint_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(platform_job_attempt_id) REFERENCES platform_job_attempts(platform_job_attempt_id),
  UNIQUE(platform_job_id,generation,sequence_number)
);
CREATE INDEX idx_platform_job_checkpoints_head_prd106
  ON platform_job_checkpoints(platform_job_id,generation,sequence_number DESC);
CREATE TRIGGER trg_platform_job_checkpoints_update_prd106 BEFORE UPDATE ON platform_job_checkpoints
BEGIN SELECT RAISE(ABORT,'platform_job_checkpoint_immutable'); END;
CREATE TRIGGER trg_platform_job_checkpoints_delete_prd106 BEFORE DELETE ON platform_job_checkpoints
BEGIN SELECT RAISE(ABORT,'platform_job_checkpoint_immutable'); END;

CREATE TABLE platform_job_effects(
  platform_job_effect_id TEXT PRIMARY KEY CHECK(length(platform_job_effect_id)=36
    AND substr(platform_job_effect_id,1,4)='pje_'),
  platform_job_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  effect_key TEXT NOT NULL CHECK(length(effect_key) BETWEEN 3 AND 96
    AND effect_key NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  effect_sha256 TEXT NOT NULL CHECK(length(effect_sha256)=64
    AND effect_sha256 NOT GLOB '*[^0-9a-f]*'),
  committed_at TEXT NOT NULL CHECK(unixepoch(committed_at) IS NOT NULL),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  UNIQUE(platform_job_id,effect_key)
);
CREATE TRIGGER trg_platform_job_effects_update_prd106 BEFORE UPDATE ON platform_job_effects
BEGIN SELECT RAISE(ABORT,'platform_job_effect_immutable'); END;
CREATE TRIGGER trg_platform_job_effects_delete_prd106 BEFORE DELETE ON platform_job_effects
BEGIN SELECT RAISE(ABORT,'platform_job_effect_immutable'); END;

CREATE TABLE platform_dead_letter_jobs(
  platform_dead_letter_job_id TEXT PRIMARY KEY CHECK(length(platform_dead_letter_job_id)=36
    AND substr(platform_dead_letter_job_id,1,4)='pjd_'),
  platform_job_id TEXT NOT NULL,
  final_attempt_number INTEGER NOT NULL CHECK(final_attempt_number>=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  safe_reason_code TEXT NOT NULL CHECK(length(safe_reason_code) BETWEEN 3 AND 96
    AND safe_reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  UNIQUE(platform_job_id,generation)
);
CREATE TRIGGER trg_platform_dead_letter_update_prd106 BEFORE UPDATE ON platform_dead_letter_jobs
BEGIN SELECT RAISE(ABORT,'platform_dead_letter_immutable'); END;
CREATE TRIGGER trg_platform_dead_letter_delete_prd106 BEFORE DELETE ON platform_dead_letter_jobs
BEGIN SELECT RAISE(ABORT,'platform_dead_letter_immutable'); END;

CREATE TABLE platform_job_event_outbox(
  platform_job_event_outbox_id TEXT PRIMARY KEY CHECK(length(platform_job_event_outbox_id)=36
    AND substr(platform_job_event_outbox_id,1,4)='pjo_'),
  platform_job_id TEXT NOT NULL,
  platform_job_attempt_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'platform_job.started','platform_job.failed','platform_job.completed','platform_job.dead_lettered')),
  type_code TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=0),
  safe_outcome_code TEXT CHECK(safe_outcome_code IS NULL
    OR (length(safe_outcome_code) BETWEEN 3 AND 96 AND safe_outcome_code NOT GLOB '*[^a-z0-9_.-]*')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(platform_job_attempt_id) REFERENCES platform_job_attempts(platform_job_attempt_id)
);
CREATE INDEX idx_platform_job_event_outbox_prd106
  ON platform_job_event_outbox(status,next_attempt_at,created_at);
CREATE TRIGGER trg_platform_job_event_owner_prd106
BEFORE UPDATE ON platform_job_event_outbox FOR EACH ROW
WHEN NEW.platform_job_event_outbox_id!=OLD.platform_job_event_outbox_id
  OR NEW.platform_job_id!=OLD.platform_job_id
  OR coalesce(NEW.platform_job_attempt_id,'')!=coalesce(OLD.platform_job_attempt_id,'')
  OR NEW.event_type!=OLD.event_type OR NEW.type_code!=OLD.type_code
  OR NEW.generation!=OLD.generation
  OR coalesce(NEW.safe_outcome_code,'')!=coalesce(OLD.safe_outcome_code,'')
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.created_at!=OLD.created_at OR NEW.attempt_count<OLD.attempt_count
BEGIN SELECT RAISE(ABORT,'platform_job_event_immutable'); END;
CREATE TRIGGER trg_platform_job_event_delete_prd106 BEFORE DELETE ON platform_job_event_outbox
BEGIN SELECT RAISE(ABORT,'platform_job_event_immutable'); END;

CREATE TABLE platform_job_commands(
  platform_job_command_id TEXT PRIMARY KEY CHECK(length(platform_job_command_id)=36
    AND substr(platform_job_command_id,1,4)='pjm_'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN('job.retry','job.cancel','type.activate','type.disable')),
  platform_job_id TEXT,
  type_code TEXT,
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_status TEXT NOT NULL CHECK(length(result_status) BETWEEN 3 AND 32),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(type_code) REFERENCES platform_job_types(type_code),
  UNIQUE(operator_id,idempotency_sha256),
  CHECK((platform_job_id IS NOT NULL AND type_code IS NULL AND action_code IN('job.retry','job.cancel'))
    OR(platform_job_id IS NULL AND type_code IS NOT NULL AND action_code IN('type.activate','type.disable')))
);
CREATE TRIGGER trg_platform_job_commands_update_prd106 BEFORE UPDATE ON platform_job_commands
BEGIN SELECT RAISE(ABORT,'platform_job_command_immutable'); END;
CREATE TRIGGER trg_platform_job_commands_delete_prd106 BEFORE DELETE ON platform_job_commands
BEGIN SELECT RAISE(ABORT,'platform_job_command_immutable'); END;

CREATE TEMP TABLE prd106_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd106_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_job_permission_extensions)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_role_permissions)=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_types)=8 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_work_queues)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_jobs','platform_job_attempts','platform_scheduler_leases',
      'platform_job_checkpoints','platform_job_effects','platform_dead_letter_jobs',
      'platform_job_event_outbox','platform_job_commands'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd106_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
