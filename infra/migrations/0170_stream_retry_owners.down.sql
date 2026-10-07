-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE UNIQUE INDEX idx_stream_sessions_tenant_request
  ON stream_sessions(tenant_id,request_id);

DROP TRIGGER IF EXISTS trg_output_safety_scans_conflict_insert_prd071;
CREATE TRIGGER trg_output_safety_scans_conflict_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM output_safety_scans current
  WHERE current.output_safety_scan_id=NEW.output_safety_scan_id
     OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;
