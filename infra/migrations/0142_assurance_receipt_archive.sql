PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE assurance_archive_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO assurance_archive_up_guard
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
  ORDER BY version DESC LIMIT 1)='0139' THEN 0 ELSE 1 END;
DROP TABLE assurance_archive_up_guard;

CREATE TABLE assurance_receipt_archives(
  security_assessment_id TEXT NOT NULL PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=1),
  envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json) AND json_type(envelope_json)='object'
    AND length(CAST(envelope_json AS BLOB)) BETWEEN 1 AND 262144),
  envelope_sha256 TEXT NOT NULL UNIQUE CHECK(length(envelope_sha256)=64
    AND envelope_sha256=metahub_sha256(envelope_json)),
  request_json TEXT NOT NULL CHECK(json_valid(request_json) AND json_type(request_json)='object'
    AND length(CAST(request_json AS BLOB)) BETWEEN 1 AND 65536),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256=metahub_sha256(request_json)),
  trust_key_json TEXT NOT NULL CHECK(json_valid(trust_key_json) AND json_type(trust_key_json)='object'
    AND length(CAST(trust_key_json AS BLOB)) BETWEEN 1 AND 4096),
  trust_key_sha256 TEXT NOT NULL CHECK(length(trust_key_sha256)=64
    AND trust_key_sha256=metahub_sha256(trust_key_json)),
  verifier_version INTEGER NOT NULL CHECK(verifier_version=1),
  verified_at TEXT NOT NULL CHECK(unixepoch(verified_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,security_assessment_id)
    REFERENCES security_assessments(workspace_id,tenant_id,security_assessment_id),
  CHECK(COALESCE(json_extract(request_json,'$.security_assessment_id'),'')=security_assessment_id),
  CHECK(COALESCE(json_extract(envelope_json,'$.receipt.request_sha256'),'')=request_sha256),
  CHECK(COALESCE(json_extract(envelope_json,'$.key_id'),'')!=''
    AND COALESCE(json_extract(envelope_json,'$.key_id'),'')=
      COALESCE(json_extract(trust_key_json,'$.key_id'),''))
);
CREATE TRIGGER trg_assurance_archive_insert_prd137 BEFORE INSERT ON assurance_receipt_archives
FOR EACH ROW WHEN NOT EXISTS(
  SELECT 1 FROM security_assessments assessment
  JOIN assurance_runner_leases lease ON lease.security_assessment_id=assessment.security_assessment_id
  WHERE assessment.security_assessment_id=NEW.security_assessment_id AND assessment.status='running'
    AND assessment.workspace_id=NEW.workspace_id AND assessment.tenant_id=NEW.tenant_id
    AND lease.fencing_generation=NEW.fencing_generation
    AND lease.lease_token_sha256=COALESCE(json_extract(NEW.request_json,'$.lease_challenge_sha256'),''))
BEGIN SELECT RAISE(ABORT,'security_assurance_archive_binding_invalid'); END;
CREATE TRIGGER trg_assurance_archive_update_prd137 BEFORE UPDATE ON assurance_receipt_archives
BEGIN SELECT RAISE(ABORT,'security_assurance_archive_immutable'); END;
CREATE TRIGGER trg_assurance_archive_delete_prd137 BEFORE DELETE ON assurance_receipt_archives
BEGIN SELECT RAISE(ABORT,'security_assurance_archive_immutable'); END;
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT table_record.name,'archive_then_delete','security',metahub_sha256(table_record.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT name||char(31)||metahub_sha256(sql) AS record FROM sqlite_schema
    WHERE type='trigger' AND tbl_name=table_record.name ORDER BY name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema table_record WHERE type='table' AND name='assurance_receipt_archives';
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT name,tbl_name,sql,metahub_sha256(sql) FROM sqlite_schema
WHERE type='trigger' AND tbl_name='assurance_receipt_archives' ORDER BY name;
DROP TRIGGER trg_tenant_deletion_manifest_prd137_insert;
INSERT INTO tenant_deletion_manifest_extensions_prd137(source_table,tenant_key_column,source_prd,created_at)
VALUES('assurance_receipt_archives','tenant_id','PRD-137',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_tenant_deletion_manifest_prd137_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd137
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;
