-- metahub: stream-retry-owners-v1
-- Permit more than one provider attempt to share a request id while keeping
-- each stream/output-safety owner unique within its tenant.

BEGIN;

DROP INDEX IF EXISTS "idx_stream_sessions_tenant_request";
DROP INDEX IF EXISTS "uq_output_safety_scans_tenant_id_request_id";

CREATE OR REPLACE FUNCTION "fn_trg_output_safety_scans_conflict_insert_prd071"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM "output_safety_scans" current
    WHERE current."output_safety_scan_id"=NEW."output_safety_scan_id"
  ) THEN
    RAISE EXCEPTION 'output_safety_scan_immutable';
  END IF;
  RETURN NEW;
END;
$$;

COMMIT;
