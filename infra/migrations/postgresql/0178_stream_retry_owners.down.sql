-- metahub:expected-version-rollback-v1
BEGIN;

CREATE UNIQUE INDEX "idx_stream_sessions_tenant_request"
  ON "stream_sessions"("tenant_id","request_id");
CREATE UNIQUE INDEX "uq_output_safety_scans_tenant_id_request_id"
  ON "output_safety_scans"("tenant_id","request_id");

CREATE OR REPLACE FUNCTION "fn_trg_output_safety_scans_conflict_insert_prd071"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM "output_safety_scans" current
    WHERE current."output_safety_scan_id"=NEW."output_safety_scan_id"
       OR (current."tenant_id"=NEW."tenant_id" AND current."request_id"=NEW."request_id")
  ) THEN
    RAISE EXCEPTION 'output_safety_scan_immutable';
  END IF;
  RETURN NEW;
END;
$$;

COMMIT;
