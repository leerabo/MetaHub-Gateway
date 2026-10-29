-- Retain the root attempt while external work is durably observed and stopped.
BEGIN IMMEDIATE;
CREATE TABLE platform_job_external_waits(
  platform_job_id TEXT PRIMARY KEY REFERENCES platform_jobs(platform_job_id),
  platform_job_attempt_id TEXT NOT NULL UNIQUE REFERENCES platform_job_attempts(platform_job_attempt_id),
  job_generation INTEGER NOT NULL CHECK(job_generation>=1),
  status TEXT NOT NULL CHECK(status IN('waiting','leased','completed')),
  resume_generation INTEGER NOT NULL CHECK(resume_generation>=0),
  continuation_envelope TEXT NOT NULL CHECK(length(continuation_envelope) BETWEEN 40 AND 131072),
  continuation_sha256 TEXT NOT NULL CHECK(length(continuation_sha256)=64
    AND continuation_sha256 NOT GLOB '*[^0-9a-f]*'),
  acknowledged INTEGER NOT NULL CHECK(acknowledged IN(0,1)),
  observation_count INTEGER NOT NULL CHECK(observation_count>=0),
  failure_count INTEGER NOT NULL CHECK(failure_count>=0),
  safe_outcome_code TEXT CHECK(safe_outcome_code IS NULL OR
    (length(safe_outcome_code) BETWEEN 3 AND 96 AND safe_outcome_code NOT GLOB '*[^a-z0-9_.-]*')),
  deadline_at TEXT NOT NULL CHECK(unixepoch(deadline_at) IS NOT NULL),
  next_poll_at TEXT NOT NULL CHECK(unixepoch(next_poll_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  CHECK((status='completed')=(completed_at IS NOT NULL)),
  CHECK(julianday(deadline_at)>julianday(created_at))
);
CREATE INDEX idx_platform_external_due ON platform_job_external_waits(status,next_poll_at);
CREATE TRIGGER trg_platform_external_insert BEFORE INSERT ON platform_job_external_waits
WHEN NEW.status!='waiting' OR NEW.resume_generation!=0 OR NEW.acknowledged!=0
  OR NEW.observation_count!=0 OR NEW.failure_count!=0 OR NOT EXISTS(
    SELECT 1 FROM platform_jobs j JOIN platform_job_attempts a ON a.platform_job_id=j.platform_job_id
    WHERE j.platform_job_id=NEW.platform_job_id AND j.generation=NEW.job_generation
      AND j.status='running' AND a.platform_job_attempt_id=NEW.platform_job_attempt_id
      AND a.generation=NEW.job_generation AND a.status='running')
BEGIN SELECT RAISE(ABORT,'platform_external_owner_invalid'); END;
CREATE TRIGGER trg_platform_external_update BEFORE UPDATE ON platform_job_external_waits
WHEN NEW.platform_job_id!=OLD.platform_job_id OR NEW.platform_job_attempt_id!=OLD.platform_job_attempt_id
  OR NEW.job_generation!=OLD.job_generation OR NEW.continuation_envelope!=OLD.continuation_envelope
  OR NEW.continuation_sha256!=OLD.continuation_sha256 OR NEW.deadline_at!=OLD.deadline_at
  OR NEW.created_at!=OLD.created_at OR OLD.status='completed'
  OR NEW.acknowledged<OLD.acknowledged OR NEW.observation_count<OLD.observation_count
  OR NEW.failure_count<OLD.failure_count OR NEW.resume_generation<OLD.resume_generation
  OR NEW.resume_generation>OLD.resume_generation+1
  OR (NEW.resume_generation!=OLD.resume_generation AND NEW.status!='leased')
BEGIN SELECT RAISE(ABORT,'platform_external_owner_immutable'); END;
CREATE TRIGGER trg_platform_external_delete BEFORE DELETE ON platform_job_external_waits
BEGIN SELECT RAISE(ABORT,'platform_external_fact_immutable'); END;

CREATE TABLE platform_job_resumption_leases(
  resumption_lease_id TEXT PRIMARY KEY CHECK(length(resumption_lease_id)=36 AND substr(resumption_lease_id,1,4)='pjr_'),
  platform_job_id TEXT NOT NULL REFERENCES platform_job_external_waits(platform_job_id),
  resume_generation INTEGER NOT NULL CHECK(resume_generation>=1),
  worker_code TEXT NOT NULL CHECK(length(worker_code) BETWEEN 3 AND 96),
  lease_token_hmac TEXT NOT NULL CHECK(length(lease_token_hmac)=64 AND lease_token_hmac NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','released','expired')),
  acquired_at TEXT NOT NULL CHECK(unixepoch(acquired_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  UNIQUE(platform_job_id,resume_generation),
  CHECK((status='active')=(released_at IS NULL)),
  CHECK(julianday(expires_at)>julianday(acquired_at))
);
CREATE UNIQUE INDEX uq_platform_resumption_active ON platform_job_resumption_leases(platform_job_id)
  WHERE status='active';
CREATE INDEX idx_platform_resumption_expiry ON platform_job_resumption_leases(status,expires_at);
CREATE TRIGGER trg_platform_resumption_update BEFORE UPDATE ON platform_job_resumption_leases
WHEN NEW.resumption_lease_id!=OLD.resumption_lease_id OR NEW.platform_job_id!=OLD.platform_job_id
  OR NEW.resume_generation!=OLD.resume_generation OR NEW.worker_code!=OLD.worker_code
  OR NEW.lease_token_hmac!=OLD.lease_token_hmac OR NEW.acquired_at!=OLD.acquired_at
  OR NEW.expires_at!=OLD.expires_at OR OLD.status!='active' OR NEW.status='active'
BEGIN SELECT RAISE(ABORT,'platform_resumption_immutable'); END;
CREATE TRIGGER trg_platform_resumption_delete BEFORE DELETE ON platform_job_resumption_leases
BEGIN SELECT RAISE(ABORT,'platform_resumption_immutable'); END;
CREATE TRIGGER trg_platform_job_external_terminal_guard BEFORE UPDATE ON platform_jobs
WHEN NEW.status NOT IN('running','cancel_requested') AND EXISTS(
  SELECT 1 FROM platform_job_external_waits w WHERE w.platform_job_id=OLD.platform_job_id AND w.status!='completed')
BEGIN SELECT RAISE(ABORT,'platform_external_stop_unconfirmed'); END;
COMMIT;
