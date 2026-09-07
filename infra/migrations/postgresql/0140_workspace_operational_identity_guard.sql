-- PostgreSQL physical tuple addresses are not stable logical row identities.
CREATE OR REPLACE FUNCTION fn_trg_workspace_operational_transition_prd096() RETURNS trigger
LANGUAGE plpgsql AS $trigger$
BEGIN
  IF NEW.workspace_operational_state_id IS DISTINCT FROM OLD.workspace_operational_state_id
    OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
    OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
    OR NEW.created_at IS DISTINCT FROM OLD.created_at
    OR NOT((NEW.revision=OLD.revision+1 AND NEW.risk_revision=OLD.risk_revision)
      OR(NEW.revision=OLD.revision AND NEW.risk_revision=OLD.risk_revision+1))
    OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_operational_transition_invalid';
  END IF;
  RETURN NEW;
END;
$trigger$;
