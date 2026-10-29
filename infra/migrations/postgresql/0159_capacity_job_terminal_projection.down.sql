DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM platform_jobs WHERE type_code='capacity.run') THEN
    RAISE EXCEPTION 'capacity_facts_prevent_down';
  END IF;
END $$;
DROP TRIGGER trg_capacity_job_terminal_projection ON platform_jobs;
DROP FUNCTION capacity_job_terminal_projection();
