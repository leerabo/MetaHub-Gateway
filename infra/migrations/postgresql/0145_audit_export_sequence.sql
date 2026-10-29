-- Preserve immutable audit mappings and serialize native export watermark allocation.
LOCK TABLE audit_log_export_sequences,audit_logs,tenant_deletion_trigger_manifest,
  tenant_deletion_table_manifest IN ACCESS EXCLUSIVE MODE;
DO $guard$
BEGIN
  IF EXISTS(
    WITH expected(trigger_name,function_name,type_bits,legacy_sha256,body_sha256) AS (VALUES ('trg_audit_log_export_sequences_conflict_insert_prd058','fn_trg_audit_log_export_sequences_conflict_insert_prd058',7,'20536a93c22829b5a6ed0ea9dff808f324662a6aaffba312f7db9bd96d55aa4d','4b86c2a8f6385f80d75925b220eb258f858c8617433fb2051eba7dfa5ff65624'),
('trg_audit_log_export_sequences_rowid_insert_prd058','fn_trg_audit_log_export_sequences_rowid_insert_prd058',5,'1c0ae11d56ae1e85ccea987125329b5a859c3e10c7d52edd1693524cc2f38cdf','8cdfcdfb08cc8304ef7d4211a4f1a84d6e515ad1cede1e7b531960e6267d77b1'),
('trg_audit_log_export_sequences_update_prd058','fn_trg_audit_log_export_sequences_update_prd058',19,'97b68c33446d9e2b1da4c921db4966d8391971093f7753e41fc8a6e5b2ac6957','1282950811b5014b6a423fddad9f54d4b933a24afa819ce0a3d23df2f032076c'),
('trg_audit_log_export_sequences_delete_prd058','fn_trg_audit_log_export_sequences_delete_prd058',11,'bc8a11ff088763f5f6fc1e4649c7e20f3a121842ce1059a7ad271a51279bf179','2ada6bbec4abf832f289da103f7276f2d0f5cf009986ff0d5cbe83158ffc2b71'),
('trg_audit_log_export_sequences_owner_insert_prd058','fn_trg_audit_log_export_sequences_owner_insert_prd058',7,'f5a1976a12c11e2fa8045e3492019942cff60d7f71e2a8a1470d3fe2f4ad98aa','bc1d107005071cc5cc4f3283651d0b4bfd91dc26c2d28007e150e68d66d7cfcc')),
    actual AS (
      SELECT trigger.tgname AS trigger_name,trigger.tgenabled,trigger.tgtype,trigger.tgnargs,
        trigger.tgqual,trigger.tgattr,trigger.tgconstraint,routine.proname,routine.prosrc,
        routine.prosecdef,routine.pronamespace,routine.prorettype,routine.prolang,routine.proconfig,
        routine.proisstrict,routine.proleakproof,routine.provolatile,routine.proparallel,
        pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid) AS sql
      FROM pg_trigger trigger JOIN pg_class relation ON relation.oid=trigger.tgrelid
      JOIN pg_proc routine ON routine.oid=trigger.tgfoid
      WHERE relation.relnamespace=current_schema()::regnamespace AND NOT trigger.tgisinternal
        AND relation.relname='audit_log_export_sequences'
    ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='audit_log_export_sequences')
    SELECT 1 FROM actual FULL JOIN recorded USING(trigger_name) FULL JOIN expected USING(trigger_name)
    WHERE actual.trigger_name IS NULL OR recorded.trigger_name IS NULL OR expected.trigger_name IS NULL
      OR actual.tgenabled<>'O' OR actual.tgtype<>expected.type_bits OR actual.tgnargs<>0
      OR actual.tgqual IS NOT NULL OR actual.tgattr::text<>'' OR actual.tgconstraint<>0
      OR actual.prosecdef OR actual.pronamespace<>current_schema()::regnamespace
      OR actual.prorettype<>'trigger'::regtype OR actual.prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
      OR actual.proconfig IS NOT NULL OR actual.proisstrict OR actual.proleakproof
      OR actual.provolatile<>'v' OR actual.proparallel<>'u' OR actual.proname<>expected.function_name
      OR metahub_sha256(actual.prosrc)<>expected.body_sha256
      OR ((actual.sql IS DISTINCT FROM recorded.trigger_sql
           OR metahub_sha256(actual.sql) IS DISTINCT FROM recorded.trigger_sha256)
        AND (metahub_sha256(recorded.trigger_sql) IS DISTINCT FROM expected.legacy_sha256
             OR recorded.trigger_sha256 IS DISTINCT FROM expected.legacy_sha256))
  ) OR (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='audit_log_export_sequences')<>1
    OR (SELECT trigger_set_sha256 FROM tenant_deletion_table_manifest WHERE source_table='audit_log_export_sequences')
       IS DISTINCT FROM (SELECT metahub_sha256(string_agg(trigger_name||chr(31)||trigger_sha256,
          chr(30) ORDER BY trigger_name)) FROM tenant_deletion_trigger_manifest WHERE source_table='audit_log_export_sequences')
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_audit_sequence_manifest_drift';
  END IF;
  IF EXISTS(SELECT 1 FROM pg_attribute attribute
    WHERE attribute.attrelid='audit_log_export_sequences'::regclass AND attribute.attname='audit_log_sequence'
      AND (attribute.atthasdef OR attribute.attidentity<>'' OR attribute.atttypid<>'bigint'::regtype))
    OR EXISTS(SELECT 1 FROM audit_log_export_sequences WHERE audit_log_sequence<=0)
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_audit_sequence_schema_drift';
  END IF;
END;
$guard$;
CREATE SEQUENCE metahub_audit_log_export_sequence AS BIGINT MINVALUE 1 NO CYCLE CACHE 1 OWNED BY audit_log_export_sequences.audit_log_sequence;
SELECT setval('metahub_audit_log_export_sequence',GREATEST(COALESCE(MAX(audit_log_sequence),0),1),COALESCE(MAX(audit_log_sequence),0)>0) FROM audit_log_export_sequences;
CREATE OR REPLACE FUNCTION "fn_trg_audit_log_export_sequences_conflict_insert_prd058"() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  IF NEW.audit_log_sequence IS NOT NULL OR EXISTS (
    SELECT 1 FROM audit_log_export_sequences current WHERE current.audit_log_id=NEW.audit_log_id
  ) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='audit_log_export_sequence_immutable';
  END IF;
  PERFORM pg_advisory_xact_lock(58049,1);
  NEW.audit_log_sequence := nextval(format('%I.metahub_audit_log_export_sequence',TG_TABLE_SCHEMA)::regclass);
  RETURN NEW;
END;
$trigger$;
CREATE OR REPLACE FUNCTION "fn_trg_audit_log_export_sequences_rowid_insert_prd058"() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  IF NEW.audit_log_sequence IS NULL OR NEW.audit_log_sequence<=0 THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='audit_log_export_sequence_immutable';
  END IF;
  RETURN NEW;
END;
$trigger$;

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
UPDATE tenant_deletion_trigger_manifest manifest
SET trigger_sql=pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid),
    trigger_sha256=metahub_sha256(pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid))
FROM pg_trigger trigger JOIN pg_class relation ON relation.oid=trigger.tgrelid
WHERE trigger.tgname=manifest.trigger_name AND relation.relname=manifest.source_table
  AND relation.relnamespace=current_schema()::regnamespace AND NOT trigger.tgisinternal
  AND manifest.source_table='audit_log_export_sequences';
UPDATE tenant_deletion_table_manifest SET trigger_set_sha256=(
  SELECT metahub_sha256(string_agg(trigger_name||chr(31)||trigger_sha256,chr(30) ORDER BY trigger_name))
  FROM tenant_deletion_trigger_manifest WHERE source_table='audit_log_export_sequences') WHERE source_table='audit_log_export_sequences';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
LOCK TABLE audit_log_export_sequences,audit_logs,tenant_deletion_trigger_manifest,
  tenant_deletion_table_manifest IN ACCESS EXCLUSIVE MODE;
DO $guard$
BEGIN
  IF EXISTS(
    WITH expected(trigger_name,function_name,type_bits,legacy_sha256,body_sha256) AS (VALUES ('trg_audit_log_export_sequences_conflict_insert_prd058','fn_trg_audit_log_export_sequences_conflict_insert_prd058',7,'20536a93c22829b5a6ed0ea9dff808f324662a6aaffba312f7db9bd96d55aa4d','5e066b293f5f5eddbe034453322bc05c0baa899e0283419e4fd97bbbdd3a4690'),
('trg_audit_log_export_sequences_rowid_insert_prd058','fn_trg_audit_log_export_sequences_rowid_insert_prd058',5,'1c0ae11d56ae1e85ccea987125329b5a859c3e10c7d52edd1693524cc2f38cdf','b00f5f5e744f4331ff12281a8f0644379eae4b64fcd211c7fe920e3026f2566f'),
('trg_audit_log_export_sequences_update_prd058','fn_trg_audit_log_export_sequences_update_prd058',19,'97b68c33446d9e2b1da4c921db4966d8391971093f7753e41fc8a6e5b2ac6957','1282950811b5014b6a423fddad9f54d4b933a24afa819ce0a3d23df2f032076c'),
('trg_audit_log_export_sequences_delete_prd058','fn_trg_audit_log_export_sequences_delete_prd058',11,'bc8a11ff088763f5f6fc1e4649c7e20f3a121842ce1059a7ad271a51279bf179','2ada6bbec4abf832f289da103f7276f2d0f5cf009986ff0d5cbe83158ffc2b71'),
('trg_audit_log_export_sequences_owner_insert_prd058','fn_trg_audit_log_export_sequences_owner_insert_prd058',7,'f5a1976a12c11e2fa8045e3492019942cff60d7f71e2a8a1470d3fe2f4ad98aa','bc1d107005071cc5cc4f3283651d0b4bfd91dc26c2d28007e150e68d66d7cfcc')),
    actual AS (
      SELECT trigger.tgname AS trigger_name,trigger.tgenabled,trigger.tgtype,trigger.tgnargs,
        trigger.tgqual,trigger.tgattr,trigger.tgconstraint,routine.proname,routine.prosrc,
        routine.prosecdef,routine.pronamespace,routine.prorettype,routine.prolang,routine.proconfig,
        routine.proisstrict,routine.proleakproof,routine.provolatile,routine.proparallel,
        pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid) AS sql
      FROM pg_trigger trigger JOIN pg_class relation ON relation.oid=trigger.tgrelid
      JOIN pg_proc routine ON routine.oid=trigger.tgfoid
      WHERE relation.relnamespace=current_schema()::regnamespace AND NOT trigger.tgisinternal
        AND relation.relname='audit_log_export_sequences'
    ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='audit_log_export_sequences')
    SELECT 1 FROM actual FULL JOIN recorded USING(trigger_name) FULL JOIN expected USING(trigger_name)
    WHERE actual.trigger_name IS NULL OR recorded.trigger_name IS NULL OR expected.trigger_name IS NULL
      OR actual.tgenabled<>'O' OR actual.tgtype<>expected.type_bits OR actual.tgnargs<>0
      OR actual.tgqual IS NOT NULL OR actual.tgattr::text<>'' OR actual.tgconstraint<>0
      OR actual.prosecdef OR actual.pronamespace<>current_schema()::regnamespace
      OR actual.prorettype<>'trigger'::regtype OR actual.prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
      OR actual.proconfig IS NOT NULL OR actual.proisstrict OR actual.proleakproof
      OR actual.provolatile<>'v' OR actual.proparallel<>'u' OR actual.proname<>expected.function_name
      OR metahub_sha256(actual.prosrc)<>expected.body_sha256
      OR ((actual.sql IS DISTINCT FROM recorded.trigger_sql
           OR metahub_sha256(actual.sql) IS DISTINCT FROM recorded.trigger_sha256)
        AND (metahub_sha256(recorded.trigger_sql) IS DISTINCT FROM expected.legacy_sha256
             OR recorded.trigger_sha256 IS DISTINCT FROM expected.legacy_sha256))
  ) OR (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='audit_log_export_sequences')<>1
    OR (SELECT trigger_set_sha256 FROM tenant_deletion_table_manifest WHERE source_table='audit_log_export_sequences')
       IS DISTINCT FROM (SELECT metahub_sha256(string_agg(trigger_name||chr(31)||trigger_sha256,
          chr(30) ORDER BY trigger_name)) FROM tenant_deletion_trigger_manifest WHERE source_table='audit_log_export_sequences')
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_audit_sequence_manifest_drift';
  END IF;
  IF EXISTS(SELECT 1 FROM pg_attribute attribute
    WHERE attribute.attrelid='audit_log_export_sequences'::regclass AND attribute.attname='audit_log_sequence'
      AND (attribute.atthasdef OR attribute.attidentity<>'' OR attribute.atttypid<>'bigint'::regtype))
    OR EXISTS(SELECT 1 FROM audit_log_export_sequences WHERE audit_log_sequence<=0)
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_audit_sequence_schema_drift';
  END IF;
END;
$guard$;
DO $sequence$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_sequence sequence
    JOIN pg_class relation ON relation.oid=sequence.seqrelid
    JOIN pg_depend dependency ON dependency.objid=relation.oid
      AND dependency.classid='pg_class'::regclass AND dependency.deptype='a'
    JOIN pg_attribute attribute ON attribute.attrelid=dependency.refobjid
      AND attribute.attnum=dependency.refobjsubid
    WHERE relation.relnamespace=current_schema()::regnamespace AND relation.relname='metahub_audit_log_export_sequence'
      AND attribute.attrelid='audit_log_export_sequences'::regclass AND attribute.attname='audit_log_sequence'
      AND sequence.seqincrement=1 AND sequence.seqmin=1 AND sequence.seqmax=9223372036854775807
      AND sequence.seqcache=1 AND NOT sequence.seqcycle)
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_audit_sequence_allocator_drift'; END IF;
END;
$sequence$;
