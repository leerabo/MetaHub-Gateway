CREATE TABLE platform_job_external_waits(
  platform_job_id TEXT PRIMARY KEY REFERENCES platform_jobs(platform_job_id),
  platform_job_attempt_id TEXT NOT NULL UNIQUE REFERENCES platform_job_attempts(platform_job_attempt_id),
  job_generation BIGINT NOT NULL CHECK(job_generation>=1),
  status TEXT NOT NULL CHECK(status IN('waiting','leased','completed')),
  resume_generation BIGINT NOT NULL CHECK(resume_generation>=0),
  continuation_envelope TEXT NOT NULL CHECK(length(continuation_envelope) BETWEEN 40 AND 131072),
  continuation_sha256 TEXT NOT NULL CHECK(continuation_sha256 ~ '^[0-9a-f]{64}$'),
  acknowledged INTEGER NOT NULL CHECK(acknowledged IN(0,1)),
  observation_count BIGINT NOT NULL CHECK(observation_count>=0),
  failure_count BIGINT NOT NULL CHECK(failure_count>=0),
  safe_outcome_code TEXT CHECK(safe_outcome_code IS NULL OR safe_outcome_code ~ '^[a-z0-9_.-]{3,96}$'),
  deadline_at TEXT NOT NULL CHECK(unixepoch(deadline_at) IS NOT NULL),
  next_poll_at TEXT NOT NULL CHECK(unixepoch(next_poll_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  CHECK((status='completed')=(completed_at IS NOT NULL)),
  CHECK(julianday(deadline_at)>julianday(created_at))
);
CREATE INDEX idx_platform_external_due ON platform_job_external_waits(status,next_poll_at);
CREATE FUNCTION platform_external_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'platform_external_fact_immutable'; END IF;
  IF TG_OP='INSERT' THEN
    IF NEW.status!='waiting' OR NEW.resume_generation!=0 OR NEW.acknowledged!=0
      OR NEW.observation_count!=0 OR NEW.failure_count!=0 OR NOT EXISTS(
        SELECT 1 FROM platform_jobs j JOIN platform_job_attempts a ON a.platform_job_id=j.platform_job_id
        WHERE j.platform_job_id=NEW.platform_job_id AND j.generation=NEW.job_generation
          AND j.status='running' AND a.platform_job_attempt_id=NEW.platform_job_attempt_id
          AND a.generation=NEW.job_generation AND a.status='running') THEN
      RAISE EXCEPTION 'platform_external_owner_invalid';
    END IF;
  ELSE
    IF NEW.platform_job_id!=OLD.platform_job_id OR NEW.platform_job_attempt_id!=OLD.platform_job_attempt_id
      OR NEW.job_generation!=OLD.job_generation OR NEW.continuation_envelope!=OLD.continuation_envelope
      OR NEW.continuation_sha256!=OLD.continuation_sha256 OR NEW.deadline_at!=OLD.deadline_at
      OR NEW.created_at!=OLD.created_at OR OLD.status='completed'
      OR NEW.acknowledged<OLD.acknowledged OR NEW.observation_count<OLD.observation_count
      OR NEW.failure_count<OLD.failure_count OR NEW.resume_generation<OLD.resume_generation
      OR NEW.resume_generation>OLD.resume_generation+1
      OR (NEW.resume_generation!=OLD.resume_generation AND NEW.status!='leased') THEN
      RAISE EXCEPTION 'platform_external_owner_immutable';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_platform_external_guard BEFORE INSERT OR UPDATE OR DELETE ON platform_job_external_waits
FOR EACH ROW EXECUTE FUNCTION platform_external_guard();

CREATE TABLE platform_job_resumption_leases(
  resumption_lease_id TEXT PRIMARY KEY CHECK(resumption_lease_id ~ '^pjr_[0-9a-f]{32}$'),
  platform_job_id TEXT NOT NULL REFERENCES platform_job_external_waits(platform_job_id),
  resume_generation BIGINT NOT NULL CHECK(resume_generation>=1),
  worker_code TEXT NOT NULL CHECK(length(worker_code) BETWEEN 3 AND 96),
  lease_token_hmac TEXT NOT NULL CHECK(lease_token_hmac ~ '^[0-9a-f]{64}$'),
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
CREATE FUNCTION platform_resumption_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'platform_resumption_immutable'; END IF;
  IF NEW.resumption_lease_id!=OLD.resumption_lease_id OR NEW.platform_job_id!=OLD.platform_job_id
    OR NEW.resume_generation!=OLD.resume_generation OR NEW.worker_code!=OLD.worker_code
    OR NEW.lease_token_hmac!=OLD.lease_token_hmac OR NEW.acquired_at!=OLD.acquired_at
    OR NEW.expires_at!=OLD.expires_at OR OLD.status!='active' OR NEW.status='active' THEN
    RAISE EXCEPTION 'platform_resumption_immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_platform_resumption_guard BEFORE UPDATE OR DELETE ON platform_job_resumption_leases
FOR EACH ROW EXECUTE FUNCTION platform_resumption_guard();
CREATE FUNCTION platform_external_terminal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status NOT IN('running','cancel_requested') AND EXISTS(
    SELECT 1 FROM platform_job_external_waits w WHERE w.platform_job_id=OLD.platform_job_id AND w.status!='completed') THEN
    RAISE EXCEPTION 'platform_external_stop_unconfirmed';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_platform_job_external_terminal_guard BEFORE UPDATE ON platform_jobs
FOR EACH ROW EXECUTE FUNCTION platform_external_terminal_guard();
