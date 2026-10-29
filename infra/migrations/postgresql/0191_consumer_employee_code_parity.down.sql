-- Retain directory data and reject removing populated employee codes.
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM workspace_consumers WHERE employee_code!='') THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='employee_code_rollback_blocked';
  END IF;
END $$;
DROP INDEX uq_workspace_consumers_employee_code;
ALTER TABLE workspace_consumers DROP COLUMN employee_code;
