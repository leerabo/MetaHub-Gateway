BEGIN IMMEDIATE;

-- Root cancellation can happen before the capacity handler ever runs.
CREATE TRIGGER trg_capacity_job_terminal_projection
AFTER UPDATE OF status ON platform_jobs
WHEN NEW.type_code='capacity.run' AND NEW.status IN('cancelled','failed','dead_lettered')
  AND NEW.status!=OLD.status
BEGIN
  UPDATE capacity_forecasts SET status='failed',revision=revision+1,
    completed_at=NEW.completed_at,
    updated_at=CASE WHEN NEW.updated_at>updated_at THEN NEW.updated_at
      ELSE strftime('%Y-%m-%dT%H:%M:%fZ',updated_at,'+1 second') END
    WHERE platform_job_id=NEW.platform_job_id AND status IN('queued','running');
  UPDATE load_test_runs SET status=CASE WHEN NEW.status='cancelled' THEN 'stopped' ELSE 'failed' END,
    safe_outcome_code=COALESCE(NEW.safe_outcome_code,'platform_job.execution_failed'),
    revision=revision+1,completed_at=NEW.completed_at,
    updated_at=CASE WHEN NEW.updated_at>updated_at THEN NEW.updated_at
      ELSE strftime('%Y-%m-%dT%H:%M:%fZ',updated_at,'+1 second') END
    WHERE platform_job_id=NEW.platform_job_id AND status IN('queued','running');
  UPDATE chaos_experiments SET status=CASE WHEN NEW.status='cancelled' THEN 'killed' ELSE 'failed' END,
    stop_reason_code=CASE WHEN NEW.status='cancelled' THEN 'operator_kill' ELSE 'runner_unavailable' END,
    objective_met=0,
    result_sha256='1d64ed9fb0e143e342c87969615fc077d50f71c15680a8cf254b7a8a672b6e31',
    revision=revision+1,completed_at=NEW.completed_at,
    updated_at=CASE WHEN NEW.updated_at>updated_at THEN NEW.updated_at
      ELSE strftime('%Y-%m-%dT%H:%M:%fZ',updated_at,'+1 second') END
    WHERE platform_job_id=NEW.platform_job_id AND status IN('queued','running');
END;

COMMIT;
