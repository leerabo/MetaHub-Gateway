DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM platform_job_external_waits)
    OR EXISTS(SELECT 1 FROM platform_job_resumption_leases) THEN
    RAISE EXCEPTION 'platform_external_facts_prevent_down';
  END IF;
END $$;
DROP TRIGGER trg_platform_job_external_terminal_guard ON platform_jobs;
DROP FUNCTION platform_external_terminal_guard();
DROP TABLE platform_job_resumption_leases;
DROP FUNCTION platform_resumption_guard();
DROP TABLE platform_job_external_waits;
DROP FUNCTION platform_external_guard();
