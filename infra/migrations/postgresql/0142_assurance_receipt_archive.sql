CREATE TABLE assurance_receipt_archives(
  security_assessment_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  fencing_generation BIGINT NOT NULL CHECK(fencing_generation>=1),
  envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json) AND json_type(envelope_json)='object'
    AND octet_length(envelope_json) BETWEEN 1 AND 262144),
  envelope_sha256 TEXT NOT NULL UNIQUE CHECK(length(envelope_sha256)=64
    AND envelope_sha256=metahub_sha256(envelope_json)),
  request_json TEXT NOT NULL CHECK(json_valid(request_json) AND json_type(request_json)='object'
    AND octet_length(request_json) BETWEEN 1 AND 65536),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256=metahub_sha256(request_json)),
  trust_key_json TEXT NOT NULL CHECK(json_valid(trust_key_json) AND json_type(trust_key_json)='object'
    AND octet_length(trust_key_json) BETWEEN 1 AND 4096),
  trust_key_sha256 TEXT NOT NULL CHECK(length(trust_key_sha256)=64
    AND trust_key_sha256=metahub_sha256(trust_key_json)),
  verifier_version BIGINT NOT NULL CHECK(verifier_version=1),
  verified_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY(workspace_id,tenant_id,security_assessment_id)
    REFERENCES security_assessments(workspace_id,tenant_id,security_assessment_id),
  CHECK(COALESCE(json_extract(request_json,'$.security_assessment_id'),'')=security_assessment_id),
  CHECK(COALESCE(json_extract(envelope_json,'$.receipt.request_sha256'),'')=request_sha256),
  CHECK(COALESCE(json_extract(envelope_json,'$.key_id'),'')!=''
    AND COALESCE(json_extract(envelope_json,'$.key_id'),'')=
      COALESCE(json_extract(trust_key_json,'$.key_id'),''))
);
CREATE FUNCTION fn_assurance_archive_insert_prd137() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(
    SELECT 1 FROM security_assessments assessment
    JOIN assurance_runner_leases lease ON lease.security_assessment_id=assessment.security_assessment_id
    WHERE assessment.security_assessment_id=NEW.security_assessment_id AND assessment.status='running'
      AND assessment.workspace_id=NEW.workspace_id AND assessment.tenant_id=NEW.tenant_id
      AND lease.fencing_generation=NEW.fencing_generation
      AND lease.lease_token_sha256=COALESCE(json_extract(NEW.request_json,'$.lease_challenge_sha256'),''))
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='security_assurance_archive_binding_invalid';
  END IF;
  RETURN NEW;
END;
$$;
CREATE FUNCTION fn_assurance_archive_immutable_prd137() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='security_assurance_archive_immutable';
END;
$$;
CREATE TRIGGER trg_assurance_archive_insert_prd137 BEFORE INSERT ON assurance_receipt_archives
FOR EACH ROW EXECUTE FUNCTION fn_assurance_archive_insert_prd137();
CREATE TRIGGER trg_assurance_archive_update_prd137 BEFORE UPDATE ON assurance_receipt_archives
FOR EACH ROW EXECUTE FUNCTION fn_assurance_archive_immutable_prd137();
CREATE TRIGGER trg_assurance_archive_delete_prd137 BEFORE DELETE ON assurance_receipt_archives
FOR EACH ROW EXECUTE FUNCTION fn_assurance_archive_immutable_prd137();
CREATE TRIGGER trg_assurance_archive_truncate_prd137 BEFORE TRUNCATE ON assurance_receipt_archives
FOR EACH STATEMENT EXECUTE FUNCTION fn_assurance_archive_immutable_prd137();

-- Native catalog definitions include trigger function bodies, not frozen SQLite SQL.
WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute
  LEFT JOIN pg_attrdef default_value ON default_value.adrelid=attribute.attrelid
    AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='assurance_receipt_archives'::regclass
    AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='assurance_receipt_archives'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='assurance_receipt_archives'::regclass AND NOT tgisinternal
)
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT 'assurance_receipt_archives','archive_then_delete','security',
  metahub_sha256('CREATE TABLE assurance_receipt_archives ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
  metahub_sha256((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30) ORDER BY tgname) FROM triggers)),
  1,CURRENT_TIMESTAMP;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT tgname,'assurance_receipt_archives',
  pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid),
  metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid))
FROM pg_trigger WHERE tgrelid='assurance_receipt_archives'::regclass AND NOT tgisinternal;
DROP TRIGGER trg_tenant_deletion_manifest_prd137_insert ON tenant_deletion_manifest_extensions_prd137;
INSERT INTO tenant_deletion_manifest_extensions_prd137(source_table,tenant_key_column,source_prd,created_at)
VALUES('assurance_receipt_archives','tenant_id','PRD-137',CURRENT_TIMESTAMP);
CREATE TRIGGER trg_tenant_deletion_manifest_prd137_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd137
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_manifest_prd137_insert();
