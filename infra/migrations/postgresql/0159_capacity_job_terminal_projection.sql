CREATE FUNCTION capacity_job_terminal_projection() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.type_code!='capacity.run' OR NEW.status NOT IN('cancelled','failed','dead_lettered')
    OR NEW.status=OLD.status THEN RETURN NEW; END IF;
  UPDATE capacity_forecasts SET status='failed',revision=revision+1,
    completed_at=NEW.completed_at,
    updated_at=CASE WHEN NEW.updated_at>updated_at THEN NEW.updated_at
      ELSE to_char((updated_at::timestamptz+interval '1 millisecond') AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END
    WHERE platform_job_id=NEW.platform_job_id AND status IN('queued','running');
  UPDATE load_test_runs SET status=CASE WHEN NEW.status='cancelled' THEN 'stopped' ELSE 'failed' END,
    safe_outcome_code=COALESCE(NEW.safe_outcome_code,'platform_job.execution_failed'),
    revision=revision+1,completed_at=NEW.completed_at,
    updated_at=CASE WHEN NEW.updated_at>updated_at THEN NEW.updated_at
      ELSE to_char((updated_at::timestamptz+interval '1 millisecond') AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END
    WHERE platform_job_id=NEW.platform_job_id AND status IN('queued','running');
  UPDATE chaos_experiments SET status=CASE WHEN NEW.status='cancelled' THEN 'killed' ELSE 'failed' END,
    stop_reason_code=CASE WHEN NEW.status='cancelled' THEN 'operator_kill' ELSE 'runner_unavailable' END,
    objective_met=0,
    result_sha256='1d64ed9fb0e143e342c87969615fc077d50f71c15680a8cf254b7a8a672b6e31',
    revision=revision+1,completed_at=NEW.completed_at,
    updated_at=CASE WHEN NEW.updated_at>updated_at THEN NEW.updated_at
      ELSE to_char((updated_at::timestamptz+interval '1 millisecond') AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END
    WHERE platform_job_id=NEW.platform_job_id AND status IN('queued','running');
  RETURN NEW;
END $$;
CREATE TRIGGER trg_capacity_job_terminal_projection AFTER UPDATE OF status ON platform_jobs
  FOR EACH ROW EXECUTE FUNCTION capacity_job_terminal_projection();
