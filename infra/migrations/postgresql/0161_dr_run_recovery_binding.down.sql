DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM dr_run_recovery_bindings) THEN
    RAISE EXCEPTION 'dr_recovery_facts_prevent_down';
  END IF;
END $$;
DROP TRIGGER trg_dr_run_binding_guard ON disaster_recovery_runs;
DROP FUNCTION dr_run_binding_guard();
DROP TABLE dr_run_recovery_bindings;
DROP FUNCTION dr_recovery_binding_guard();
