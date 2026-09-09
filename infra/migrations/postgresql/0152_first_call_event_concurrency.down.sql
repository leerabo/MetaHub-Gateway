LOCK TABLE request_logs,product_events,tenant_deletion_table_manifest,tenant_deletion_trigger_manifest IN ACCESS EXCLUSIVE MODE;
DO $guard$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha,columns) AS (VALUES ('trg_request_logs_product_event_insert_prd085','fn_trg_request_logs_product_event_insert_prd085',5,'3ae3e11e8d4e04c811a1d52ddf60b91d5b956cae9e2f4f5fca0a7178ec89df64',ARRAY[]::text[]),('trg_request_logs_product_event_update_prd085','fn_trg_request_logs_product_event_update_prd085',17,'8e020cbd0c696d922a885a04e3e9f390d9d7bf7a174fa578371f087b0fb23876',ARRAY['status']::text[])), actual AS (
      SELECT t.*,p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='request_logs'::regclass AND t.tgname IN(
        'trg_request_logs_product_event_insert_prd085','trg_request_logs_product_event_update_prd085')
    ) SELECT 1 FROM expected FULL JOIN actual ON name=tgname
    WHERE name IS NULL OR tgname IS NULL OR proname<>function OR tgenabled<>'O' OR tgtype<>bits
      OR tgnargs<>0 OR tgqual IS NOT NULL OR tgconstraint<>0 OR tgisinternal
      OR columns IS DISTINCT FROM ARRAY(SELECT a.attname::text
        FROM unnest(tgattr) WITH ORDINALITY key(num,ordinal)
        JOIN pg_attribute a ON a.attrelid=tgrelid AND a.attnum=key.num ORDER BY key.ordinal)
      OR metahub_sha256(prosrc)<>sha OR prosecdef OR proconfig IS NOT NULL OR proisstrict
      OR proleakproof OR provolatile<>'v' OR proparallel<>'u'
      OR pronamespace<>current_schema()::regnamespace OR prorettype<>'trigger'::regtype
      OR prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_first_call_trigger_drift'; END IF;
END; $guard$;
CREATE TEMP TABLE native_first_call_expected(tenant_id TEXT,event_type TEXT);
CREATE UNIQUE INDEX native_first_call_expected_index ON native_first_call_expected(tenant_id,event_type)
WHERE event_type IN('tenant.registered','gateway.first_call_succeeded');
DO $index$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_index actual CROSS JOIN pg_index expected
      WHERE actual.indexrelid=to_regclass('uq_product_events_once_prd085')
        AND actual.indrelid='product_events'::regclass
        AND expected.indexrelid='native_first_call_expected_index'::regclass
        AND actual.indisunique AND actual.indisvalid AND actual.indisready AND actual.indimmediate
        AND NOT actual.indnullsnotdistinct AND actual.indnkeyatts=2 AND actual.indnatts=2
        AND actual.indclass=expected.indclass AND actual.indcollation=expected.indcollation
        AND actual.indoption=expected.indoption AND actual.indexprs IS NULL
        AND pg_get_expr(actual.indpred,actual.indrelid)=pg_get_expr(expected.indpred,expected.indrelid)
        AND ARRAY(SELECT a.attname::text FROM unnest(actual.indkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=actual.indrelid AND a.attnum=key.num ORDER BY key.ordinal)
          =ARRAY['tenant_id','event_type']::text[])
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_first_call_index_drift'; END IF;
END; $index$;
DROP TABLE native_first_call_expected;
DO $manifest$ BEGIN
  IF (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='request_logs')<>1
    OR EXISTS(SELECT 1 FROM tenant_deletion_table_manifest m CROSS JOIN (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='request_logs'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='request_logs'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='request_logs'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE request_logs ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual
      WHERE m.source_table='request_logs' AND (
        m.table_sql_sha256 NOT IN('53802051abe1d88b648dcefe34b2a7bcc7cc3bcf9b3a89b4d11c4e4dab7037aa',actual.table_sha)
        OR m.column_signature_sha256 NOT IN('a34659d32a7c0b3a44b0cb1a110b84edd820744a5fb6ea624a3155c4f119f242',actual.columns_sha)
        OR m.trigger_set_sha256 IS DISTINCT FROM (
          SELECT metahub_sha256(string_agg(trigger_name||chr(31)||trigger_sha256,chr(30)
            ORDER BY trigger_name)) FROM tenant_deletion_trigger_manifest WHERE source_table='request_logs')))
    OR EXISTS(
      WITH legacy(name,sha) AS (VALUES ('trg_request_log_final_attempt_insert','14047bd515938a7f743bce780c0c8e1ce33ecefdb14bee64adbabd3744b2c930'),('trg_request_log_final_attempt_update','6637f8d529a7a51bd5a6f29b7a5ad3bb3bad32397e7701245037c2a11007f6f0'),('trg_request_logs_claim_insert','3aba596ba818e0f27fb633eb5c459256c9a979c33ff3cba3e2aae2bcacb68aaa'),('trg_request_logs_claim_update','b07c4629f54155ccfdb6cafb07edc442a1e6b84623ae80fc26053b268cb1a836'),('trg_request_logs_snapshot_insert','82734e05c5d00c8399720e7386f36e56bc12fc30519e239ebe2ecf8c1c1836d5'),('trg_request_logs_snapshot_update','10e155fbc181bb4e4f392e05f6cc56c7931d6c373cf6fe46be8b96724abec929'),('trg_request_log_feedback_parent_delete_prd049','add7c163177169fa4d2935727ecf24c1f30de78b49e4c538dfc133538e67fc91'),('trg_request_log_feedback_parent_conflict_update_prd049','1ad5f0c1aa3f141779d5b23f5933422917d4d494de945b31f6aa1600e1d18efb'),('trg_request_logs_intent_insert_prd069','eb12afe28605bc74eb6933d286cf59ec60df13442b0ebea6c1a96bbc40163a5f'),('trg_request_logs_intent_update_prd069','45abc803b1e64bb971b419758b42b1b43b216b6b45526a774a31d3b1ed1b5fca'),('trg_request_log_stream_insert','4d8d9fb1e02407127c6295a027c14ed7fe77e2c5fcac631ac69e72a7ae69b8e5'),('trg_request_log_stream_update','3e621921d062b84c43229844682f481e7b431e8463162f3d7f2e308bf6d225cb'),('trg_request_log_feedback_parent_update_prd049','74075d2218f3ea6f49fea7abc4891a27d19ad2c754bf6eb8770143e623517ca3'),('trg_request_log_feedback_parent_conflict_insert_prd049','63734e728696ee4ba24d3e63f7a4235c7cf6db821f13a34c6eb1ec96c263a49f'),('trg_request_logs_experiment_insert_prd075','8268134dd1454e08b57d15611e2e91260c41055e5a516a89919c2dc5624c25be'),('trg_request_logs_experiment_update_prd075','2ef2ea01256bc3dd87d0a4da39256ffdfdaeb7520fc6563cc81cfa86978029c1')), actual AS (
        SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
        FROM pg_trigger WHERE tgrelid='request_logs'::regclass AND NOT tgisinternal
      ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='request_logs')
      SELECT 1 FROM actual FULL JOIN recorded ON tgname=trigger_name LEFT JOIN legacy ON name=trigger_name
      WHERE tgname IS NULL OR trigger_name IS NULL
        OR trigger_sha256 IS DISTINCT FROM metahub_sha256(trigger_sql)
        OR (trigger_sql IS DISTINCT FROM sql AND trigger_sha256 IS DISTINCT FROM sha)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_first_call_manifest_drift'; END IF;
END; $manifest$;
CREATE OR REPLACE FUNCTION fn_trg_request_logs_product_event_insert_prd085() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
  IF NEW.status='success' THEN
    INSERT INTO product_events( product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id, operation_request_id,operation_trace_id,capture_mode,status, occurred_at,created_at,updated_at) SELECT 'pev_'||substr(metahub_sha256((jsonb_build_array( NEW.tenant_id,'gateway.first_call_succeeded','request_log',NEW.request_id)::TEXT)),1,32), NEW.tenant_id,'gateway.first_call_succeeded',NULL,'request_log',NEW.request_id, NEW.request_id,NEW.trace_id,'live','recorded',NEW.created_at, strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE NOT EXISTS(SELECT 1 FROM product_events current WHERE current.tenant_id=NEW.tenant_id AND current.event_type='gateway.first_call_succeeded');
  END IF;
  RETURN NEW;
END;
$body$;
CREATE OR REPLACE FUNCTION fn_trg_request_logs_product_event_update_prd085() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
  IF OLD.status<>'success' AND NEW.status='success' THEN
    INSERT INTO product_events( product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id, operation_request_id,operation_trace_id,capture_mode,status, occurred_at,created_at,updated_at) SELECT 'pev_'||substr(metahub_sha256((jsonb_build_array( NEW.tenant_id,'gateway.first_call_succeeded','request_log',NEW.request_id)::TEXT)),1,32), NEW.tenant_id,'gateway.first_call_succeeded',NULL,'request_log',NEW.request_id, NEW.request_id,NEW.trace_id,'live','recorded',COALESCE(NEW.updated_at,NEW.created_at), strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE NOT EXISTS(SELECT 1 FROM product_events current WHERE current.tenant_id=NEW.tenant_id AND current.event_type='gateway.first_call_succeeded');
  END IF;
  RETURN NEW;
END;
$body$;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
UPDATE tenant_deletion_trigger_manifest m SET
  trigger_sql=pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid))
FROM pg_trigger t WHERE t.tgrelid='request_logs'::regclass AND m.source_table='request_logs'
  AND m.trigger_name=t.tgname AND m.trigger_name IN(
    'trg_request_logs_product_event_insert_prd085','trg_request_logs_product_event_update_prd085');
UPDATE tenant_deletion_table_manifest SET trigger_set_sha256=(
  SELECT metahub_sha256(string_agg(trigger_name||chr(31)||trigger_sha256,chr(30) ORDER BY trigger_name))
  FROM tenant_deletion_trigger_manifest WHERE source_table='request_logs') WHERE source_table='request_logs';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
DO $guard$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha,columns) AS (VALUES ('trg_request_logs_product_event_insert_prd085','fn_trg_request_logs_product_event_insert_prd085',5,'d602ea4e0655d39943dbc07c171ee7d47a9a222ac8c9d5556cafeb21475fe5dd',ARRAY[]::text[]),('trg_request_logs_product_event_update_prd085','fn_trg_request_logs_product_event_update_prd085',17,'9e24500eabb6da68fd1f0d6d1ca2cfcae3152754fc3db8e8a5d5a815477594f0',ARRAY['status']::text[])), actual AS (
      SELECT t.*,p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='request_logs'::regclass AND t.tgname IN(
        'trg_request_logs_product_event_insert_prd085','trg_request_logs_product_event_update_prd085')
    ) SELECT 1 FROM expected FULL JOIN actual ON name=tgname
    WHERE name IS NULL OR tgname IS NULL OR proname<>function OR tgenabled<>'O' OR tgtype<>bits
      OR tgnargs<>0 OR tgqual IS NOT NULL OR tgconstraint<>0 OR tgisinternal
      OR columns IS DISTINCT FROM ARRAY(SELECT a.attname::text
        FROM unnest(tgattr) WITH ORDINALITY key(num,ordinal)
        JOIN pg_attribute a ON a.attrelid=tgrelid AND a.attnum=key.num ORDER BY key.ordinal)
      OR metahub_sha256(prosrc)<>sha OR prosecdef OR proconfig IS NOT NULL OR proisstrict
      OR proleakproof OR provolatile<>'v' OR proparallel<>'u'
      OR pronamespace<>current_schema()::regnamespace OR prorettype<>'trigger'::regtype
      OR prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_first_call_trigger_drift'; END IF;
END; $guard$;
DO $manifest$ BEGIN
  IF (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='request_logs')<>1
    OR EXISTS(SELECT 1 FROM tenant_deletion_table_manifest m CROSS JOIN (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='request_logs'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='request_logs'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='request_logs'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE request_logs ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual
      WHERE m.source_table='request_logs' AND (
        m.table_sql_sha256 NOT IN('53802051abe1d88b648dcefe34b2a7bcc7cc3bcf9b3a89b4d11c4e4dab7037aa',actual.table_sha)
        OR m.column_signature_sha256 NOT IN('a34659d32a7c0b3a44b0cb1a110b84edd820744a5fb6ea624a3155c4f119f242',actual.columns_sha)
        OR m.trigger_set_sha256 IS DISTINCT FROM (
          SELECT metahub_sha256(string_agg(trigger_name||chr(31)||trigger_sha256,chr(30)
            ORDER BY trigger_name)) FROM tenant_deletion_trigger_manifest WHERE source_table='request_logs')))
    OR EXISTS(
      WITH legacy(name,sha) AS (VALUES ('trg_request_log_final_attempt_insert','14047bd515938a7f743bce780c0c8e1ce33ecefdb14bee64adbabd3744b2c930'),('trg_request_log_final_attempt_update','6637f8d529a7a51bd5a6f29b7a5ad3bb3bad32397e7701245037c2a11007f6f0'),('trg_request_logs_claim_insert','3aba596ba818e0f27fb633eb5c459256c9a979c33ff3cba3e2aae2bcacb68aaa'),('trg_request_logs_claim_update','b07c4629f54155ccfdb6cafb07edc442a1e6b84623ae80fc26053b268cb1a836'),('trg_request_logs_snapshot_insert','82734e05c5d00c8399720e7386f36e56bc12fc30519e239ebe2ecf8c1c1836d5'),('trg_request_logs_snapshot_update','10e155fbc181bb4e4f392e05f6cc56c7931d6c373cf6fe46be8b96724abec929'),('trg_request_log_feedback_parent_delete_prd049','add7c163177169fa4d2935727ecf24c1f30de78b49e4c538dfc133538e67fc91'),('trg_request_log_feedback_parent_conflict_update_prd049','1ad5f0c1aa3f141779d5b23f5933422917d4d494de945b31f6aa1600e1d18efb'),('trg_request_logs_intent_insert_prd069','eb12afe28605bc74eb6933d286cf59ec60df13442b0ebea6c1a96bbc40163a5f'),('trg_request_logs_intent_update_prd069','45abc803b1e64bb971b419758b42b1b43b216b6b45526a774a31d3b1ed1b5fca'),('trg_request_log_stream_insert','4d8d9fb1e02407127c6295a027c14ed7fe77e2c5fcac631ac69e72a7ae69b8e5'),('trg_request_log_stream_update','3e621921d062b84c43229844682f481e7b431e8463162f3d7f2e308bf6d225cb'),('trg_request_log_feedback_parent_update_prd049','74075d2218f3ea6f49fea7abc4891a27d19ad2c754bf6eb8770143e623517ca3'),('trg_request_log_feedback_parent_conflict_insert_prd049','63734e728696ee4ba24d3e63f7a4235c7cf6db821f13a34c6eb1ec96c263a49f'),('trg_request_logs_experiment_insert_prd075','8268134dd1454e08b57d15611e2e91260c41055e5a516a89919c2dc5624c25be'),('trg_request_logs_experiment_update_prd075','2ef2ea01256bc3dd87d0a4da39256ffdfdaeb7520fc6563cc81cfa86978029c1'),('trg_request_logs_product_event_insert_prd085','de2e6157b432ccca4b95b07ee951234f2a9bd3f7808ba1cc633513df51b6e187'),('trg_request_logs_product_event_update_prd085','86606b52e17e1ee52dbf96be46b79ea05543a4b578944afc8caa6436e7b3e667')), actual AS (
        SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
        FROM pg_trigger WHERE tgrelid='request_logs'::regclass AND NOT tgisinternal
      ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='request_logs')
      SELECT 1 FROM actual FULL JOIN recorded ON tgname=trigger_name LEFT JOIN legacy ON name=trigger_name
      WHERE tgname IS NULL OR trigger_name IS NULL
        OR trigger_sha256 IS DISTINCT FROM metahub_sha256(trigger_sql)
        OR (trigger_sql IS DISTINCT FROM sql AND trigger_sha256 IS DISTINCT FROM sha)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_first_call_manifest_drift'; END IF;
END; $manifest$;
