LOCK TABLE usage_export_ledger_sequences,usage_ledger,platform_meter_checkpoints,usage_export_jobs,platform_meter_readings,tenant_deletion_table_manifest,tenant_deletion_trigger_manifest IN ACCESS EXCLUSIVE MODE;
CREATE TEMPORARY TABLE billing_expected_shape (usage_export_sequence bigint, tenant_id text NOT NULL, usage_ledger_id text NOT NULL, PRIMARY KEY (usage_export_sequence));
CREATE UNIQUE INDEX billing_expected_index_0 ON billing_expected_shape (usage_ledger_id);
DO $shape$ BEGIN
  IF EXISTS(
    WITH columns AS (
      SELECT attrelid,row_number() OVER(PARTITION BY attrelid ORDER BY attnum) AS ordinal,
        attname,atttypid,atttypmod,attnotnull,attidentity,attgenerated,attcollation,
        pg_get_expr(value.adbin,value.adrelid) AS default_sql
      FROM pg_attribute attribute LEFT JOIN pg_attrdef value
        ON value.adrelid=attribute.attrelid AND value.adnum=attribute.attnum
      WHERE attrelid IN('usage_export_ledger_sequences'::regclass,'billing_expected_shape'::regclass)
        AND attnum>0 AND NOT attisdropped
    ), actual AS (SELECT * FROM columns WHERE attrelid='usage_export_ledger_sequences'::regclass),
    expected AS (SELECT * FROM columns WHERE attrelid='billing_expected_shape'::regclass)
    SELECT 1 FROM actual FULL JOIN expected USING(ordinal)
    WHERE ROW(actual.attname,actual.atttypid,actual.atttypmod,actual.attnotnull,
              actual.attidentity,actual.attgenerated,actual.attcollation,actual.default_sql)
      IS DISTINCT FROM ROW(expected.attname,expected.atttypid,expected.atttypmod,expected.attnotnull,
              expected.attidentity,expected.attgenerated,expected.attcollation,expected.default_sql)
  ) OR EXISTS(
    WITH actual AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='usage_export_ledger_sequences'::regclass AND contype<>'f'),
    expected AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='billing_expected_shape'::regclass AND contype<>'f')
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) OR EXISTS(
    WITH expected(parent,child_columns,parent_columns,match,upd,del,deferred,initially_deferred,validated) AS (VALUES ('usage_ledger',ARRAY['usage_ledger_id']::text[],ARRAY['usage_ledger_id']::text[],'s','a','a',false,false,true),('tenants',ARRAY['tenant_id']::text[],ARRAY['tenant_id']::text[],'s','a','a',false,false,true)),
    actual AS (
      SELECT parent.relname::text,
        ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=key.num ORDER BY key.ordinal),
        ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=key.num ORDER BY key.ordinal),
        c.confmatchtype::text,c.confupdtype::text,c.confdeltype::text,c.condeferrable,c.condeferred,c.convalidated
      FROM pg_constraint c JOIN pg_class parent ON parent.oid=c.confrelid
      WHERE c.conrelid='usage_export_ledger_sequences'::regclass AND c.contype='f'
        AND parent.relnamespace=current_schema()::regnamespace
    ) (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
    UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
  ) OR EXISTS(
    WITH indexes AS (
      SELECT i.indrelid,i.indisvalid,i.indisready,i.indimmediate,i.indnkeyatts,
        ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY key(num,ordinal)
          LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=key.num ORDER BY key.ordinal) AS columns,
        pg_get_expr(i.indpred,i.indrelid) AS predicate,pg_get_expr(i.indexprs,i.indrelid) AS expression
      FROM pg_index i WHERE i.indisunique
        AND i.indrelid IN('usage_export_ledger_sequences'::regclass,'billing_expected_shape'::regclass)
    ), actual AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='usage_export_ledger_sequences'::regclass),
    expected AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='billing_expected_shape'::regclass)
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_table_drift'; END IF;
END; $shape$;
DROP TABLE billing_expected_shape;
DO $triggers$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha256) AS (VALUES ('trg_usage_export_ledger_sequences_owner_insert','fn_trg_usage_export_ledger_sequences_owner_insert',7,'86a2f9f029120befd5cd16e99f675d75f09d248e2671f42ae8cfe051bc37bfa3'),
('trg_usage_export_ledger_sequences_immutable_update','fn_trg_usage_export_ledger_sequences_immutable_update',19,'fa7863273d75814bd04cb643b268f517c162ac12a930fa652e1c0dbd875d92e7'),
('trg_usage_export_ledger_sequences_immutable_delete','fn_trg_usage_export_ledger_sequences_immutable_delete',11,'9c9b00dece0b2e57dfc1be790d66f125a42c0907138610859b7ccb51f3a18619')), actual AS (
      SELECT t.tgname,t.tgenabled,t.tgtype,t.tgnargs,t.tgqual,t.tgattr,t.tgconstraint,
        p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='usage_export_ledger_sequences'::regclass AND NOT t.tgisinternal
    ) SELECT 1 FROM expected FULL JOIN actual ON name=tgname
    WHERE name IS NULL OR tgname IS NULL OR proname<>function OR tgenabled<>'O' OR tgtype<>bits
      OR tgnargs<>0 OR tgqual IS NOT NULL OR tgattr::text<>'' OR tgconstraint<>0
      OR metahub_sha256(prosrc)<>sha256 OR prosecdef OR proconfig IS NOT NULL OR proisstrict
      OR proleakproof OR provolatile<>'v' OR proparallel<>'u'
      OR pronamespace<>current_schema()::regnamespace OR prorettype<>'trigger'::regtype
      OR prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_trigger_drift'; END IF;
END; $triggers$;
DO $manifest$ BEGIN
  IF (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='usage_export_ledger_sequences')<>1
    OR EXISTS(SELECT 1 FROM tenant_deletion_table_manifest m CROSS JOIN (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='usage_export_ledger_sequences'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='usage_export_ledger_sequences'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='usage_export_ledger_sequences'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE usage_export_ledger_sequences ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual
      WHERE m.source_table='usage_export_ledger_sequences' AND (
        m.table_sql_sha256 NOT IN('3ce3ac8a5cfecfca79658fb1c7e2c86bc82943cf356e88d20d6696e7c5ab665c',actual.table_sha)
        OR m.column_signature_sha256 NOT IN('113a41aedcdc3d43ac91694c068046d82e0abfc9f1f306e69e8eeb6df21f0cf4',actual.columns_sha)
        OR m.trigger_set_sha256 IS DISTINCT FROM (SELECT metahub_sha256(coalesce(
          string_agg(trigger_name||chr(31)||trigger_sha256,chr(30) ORDER BY trigger_name),''))
          FROM tenant_deletion_trigger_manifest WHERE source_table='usage_export_ledger_sequences')))
    OR EXISTS(
      WITH legacy(name,sha256) AS (VALUES ('trg_usage_export_ledger_sequences_owner_insert','589ebd4b1e03d6edc2740087cf70d5c558f994149e59dbf79bf692d5b67d62eb'),('trg_usage_export_ledger_sequences_immutable_update','5800f84ef95d68c5ddb7239a566916f17f88300a4618fd8b11a346965c81d41f'),('trg_usage_export_ledger_sequences_immutable_delete','ac81426b89aacf53fc48f89842005cd3b8abaa43c7f2d1339786e238c3fc0038')), actual AS (
        SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
        FROM pg_trigger WHERE tgrelid='usage_export_ledger_sequences'::regclass AND NOT tgisinternal
      ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='usage_export_ledger_sequences')
      SELECT 1 FROM actual FULL JOIN recorded ON tgname=trigger_name LEFT JOIN legacy ON name=trigger_name
      WHERE tgname IS NULL OR trigger_name IS NULL OR metahub_sha256(trigger_sql)<>trigger_sha256
        OR (trigger_sql IS DISTINCT FROM sql AND trigger_sha256 IS DISTINCT FROM sha256)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_manifest_drift'; END IF;
END; $manifest$;
DO $facts$ BEGIN
  IF EXISTS(SELECT 1 FROM usage_ledger u LEFT JOIN usage_export_ledger_sequences s USING(usage_ledger_id)
      WHERE s.usage_ledger_id IS NULL OR s.tenant_id<>u.tenant_id)
    OR EXISTS(SELECT 1 FROM usage_export_ledger_sequences WHERE usage_export_sequence<=0)
    OR COALESCE((SELECT MAX(source_sequence) FROM platform_meter_checkpoints),0)
      >COALESCE((SELECT MAX(usage_export_sequence) FROM usage_export_ledger_sequences),0)
    OR COALESCE((SELECT MAX(snapshot_ledger_sequence) FROM usage_export_jobs),0)
      >COALESCE((SELECT MAX(usage_export_sequence) FROM usage_export_ledger_sequences),0)
    OR COALESCE((SELECT MAX(source_sequence) FROM platform_meter_readings),0)
      >COALESCE((SELECT MAX(usage_export_sequence) FROM usage_export_ledger_sequences),0)
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_usage_sequence_history_invalid'; END IF;
END; $facts$;
CREATE TABLE metahub_usage_export_allocator(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  last_sequence BIGINT NOT NULL CHECK(last_sequence>=0));
INSERT INTO metahub_usage_export_allocator SELECT 1,COALESCE(MAX(usage_export_sequence),0) FROM usage_export_ledger_sequences;
CREATE OR REPLACE FUNCTION fn_native_usage_allocator_guard() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
  IF TG_OP<>'UPDATE' THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_usage_allocator_immutable';
  END IF;
  IF pg_trigger_depth()<2 OR NEW.singleton_id<>OLD.singleton_id
    OR NEW.last_sequence<>OLD.last_sequence+1 THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_usage_allocator_immutable';
  END IF;
  RETURN NEW;
END;
$body$;
CREATE TRIGGER trg_native_usage_allocator_guard BEFORE INSERT OR UPDATE OR DELETE ON metahub_usage_export_allocator FOR EACH ROW EXECUTE FUNCTION fn_native_usage_allocator_guard();
CREATE TRIGGER trg_native_usage_allocator_truncate BEFORE TRUNCATE ON metahub_usage_export_allocator FOR EACH STATEMENT EXECUTE FUNCTION fn_native_usage_allocator_guard();
CREATE OR REPLACE FUNCTION fn_trg_usage_export_ledger_sequences_owner_insert() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
  IF NEW.usage_export_sequence IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='usage_export_ledger_sequence_immutable';
  END IF;
  IF NOT EXISTS(SELECT 1 FROM usage_ledger ul
      WHERE ul.usage_ledger_id=NEW.usage_ledger_id AND ul.tenant_id=NEW.tenant_id)
    OR EXISTS(SELECT 1 FROM usage_export_ledger_sequences WHERE usage_ledger_id=NEW.usage_ledger_id)
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='usage_export_ledger_sequence_owner_mismatch';
  END IF;
  EXECUTE format('UPDATE %I.metahub_usage_export_allocator SET last_sequence=last_sequence+1 '
    'WHERE singleton_id=1 RETURNING last_sequence',TG_TABLE_SCHEMA) INTO NEW.usage_export_sequence;
  IF NEW.usage_export_sequence IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_usage_allocator_missing';
  END IF;
  RETURN NEW;
END;
$body$;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
UPDATE tenant_deletion_trigger_manifest m SET
  trigger_sql=pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid))
FROM pg_trigger t WHERE t.tgrelid='usage_export_ledger_sequences'::regclass AND m.source_table='usage_export_ledger_sequences' AND m.trigger_name=t.tgname;
UPDATE tenant_deletion_table_manifest m SET table_sql_sha256=actual.table_sha,
  column_signature_sha256=actual.columns_sha,trigger_set_sha256=actual.triggers_sha
FROM (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='usage_export_ledger_sequences'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='usage_export_ledger_sequences'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='usage_export_ledger_sequences'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE usage_export_ledger_sequences ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual WHERE m.source_table='usage_export_ledger_sequences';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
CREATE TEMPORARY TABLE billing_expected_shape (singleton_id integer PRIMARY KEY CHECK (singleton_id = 1), last_sequence bigint NOT NULL CHECK (last_sequence >= 0));
DO $shape$ BEGIN
  IF EXISTS(
    WITH columns AS (
      SELECT attrelid,row_number() OVER(PARTITION BY attrelid ORDER BY attnum) AS ordinal,
        attname,atttypid,atttypmod,attnotnull,attidentity,attgenerated,attcollation,
        pg_get_expr(value.adbin,value.adrelid) AS default_sql
      FROM pg_attribute attribute LEFT JOIN pg_attrdef value
        ON value.adrelid=attribute.attrelid AND value.adnum=attribute.attnum
      WHERE attrelid IN('metahub_usage_export_allocator'::regclass,'billing_expected_shape'::regclass)
        AND attnum>0 AND NOT attisdropped
    ), actual AS (SELECT * FROM columns WHERE attrelid='metahub_usage_export_allocator'::regclass),
    expected AS (SELECT * FROM columns WHERE attrelid='billing_expected_shape'::regclass)
    SELECT 1 FROM actual FULL JOIN expected USING(ordinal)
    WHERE ROW(actual.attname,actual.atttypid,actual.atttypmod,actual.attnotnull,
              actual.attidentity,actual.attgenerated,actual.attcollation,actual.default_sql)
      IS DISTINCT FROM ROW(expected.attname,expected.atttypid,expected.atttypmod,expected.attnotnull,
              expected.attidentity,expected.attgenerated,expected.attcollation,expected.default_sql)
  ) OR EXISTS(
    WITH actual AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='metahub_usage_export_allocator'::regclass AND contype<>'f'),
    expected AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='billing_expected_shape'::regclass AND contype<>'f')
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) OR EXISTS(
    WITH expected(parent,child_columns,parent_columns,match,upd,del,deferred,initially_deferred,validated) AS (SELECT NULL::text,NULL::text[],NULL::text[],NULL::text,NULL::text,NULL::text,false,false,true WHERE false),
    actual AS (
      SELECT parent.relname::text,
        ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=key.num ORDER BY key.ordinal),
        ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=key.num ORDER BY key.ordinal),
        c.confmatchtype::text,c.confupdtype::text,c.confdeltype::text,c.condeferrable,c.condeferred,c.convalidated
      FROM pg_constraint c JOIN pg_class parent ON parent.oid=c.confrelid
      WHERE c.conrelid='metahub_usage_export_allocator'::regclass AND c.contype='f'
        AND parent.relnamespace=current_schema()::regnamespace
    ) (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
    UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
  ) OR EXISTS(
    WITH indexes AS (
      SELECT i.indrelid,i.indisvalid,i.indisready,i.indimmediate,i.indnkeyatts,
        ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY key(num,ordinal)
          LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=key.num ORDER BY key.ordinal) AS columns,
        pg_get_expr(i.indpred,i.indrelid) AS predicate,pg_get_expr(i.indexprs,i.indrelid) AS expression
      FROM pg_index i WHERE i.indisunique
        AND i.indrelid IN('metahub_usage_export_allocator'::regclass,'billing_expected_shape'::regclass)
    ), actual AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='metahub_usage_export_allocator'::regclass),
    expected AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='billing_expected_shape'::regclass)
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_table_drift'; END IF;
END; $shape$;
DROP TABLE billing_expected_shape;
DO $triggers$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha256) AS (VALUES ('trg_native_usage_allocator_guard','fn_native_usage_allocator_guard',31,'b7cbed39ff396af8636db951ecf849ad9f5c5b4190adbc26bdf2d4734342f286'),
('trg_native_usage_allocator_truncate','fn_native_usage_allocator_guard',34,'b7cbed39ff396af8636db951ecf849ad9f5c5b4190adbc26bdf2d4734342f286')), actual AS (
      SELECT t.tgname,t.tgenabled,t.tgtype,t.tgnargs,t.tgqual,t.tgattr,t.tgconstraint,
        p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='metahub_usage_export_allocator'::regclass AND NOT t.tgisinternal
    ) SELECT 1 FROM expected FULL JOIN actual ON name=tgname
    WHERE name IS NULL OR tgname IS NULL OR proname<>function OR tgenabled<>'O' OR tgtype<>bits
      OR tgnargs<>0 OR tgqual IS NOT NULL OR tgattr::text<>'' OR tgconstraint<>0
      OR metahub_sha256(prosrc)<>sha256 OR prosecdef OR proconfig IS NOT NULL OR proisstrict
      OR proleakproof OR provolatile<>'v' OR proparallel<>'u'
      OR pronamespace<>current_schema()::regnamespace OR prorettype<>'trigger'::regtype
      OR prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_trigger_drift'; END IF;
END; $triggers$;
CREATE TEMPORARY TABLE billing_expected_shape (usage_export_sequence bigint, tenant_id text NOT NULL, usage_ledger_id text NOT NULL, PRIMARY KEY (usage_export_sequence));
CREATE UNIQUE INDEX billing_expected_index_0 ON billing_expected_shape (usage_ledger_id);
DO $shape$ BEGIN
  IF EXISTS(
    WITH columns AS (
      SELECT attrelid,row_number() OVER(PARTITION BY attrelid ORDER BY attnum) AS ordinal,
        attname,atttypid,atttypmod,attnotnull,attidentity,attgenerated,attcollation,
        pg_get_expr(value.adbin,value.adrelid) AS default_sql
      FROM pg_attribute attribute LEFT JOIN pg_attrdef value
        ON value.adrelid=attribute.attrelid AND value.adnum=attribute.attnum
      WHERE attrelid IN('usage_export_ledger_sequences'::regclass,'billing_expected_shape'::regclass)
        AND attnum>0 AND NOT attisdropped
    ), actual AS (SELECT * FROM columns WHERE attrelid='usage_export_ledger_sequences'::regclass),
    expected AS (SELECT * FROM columns WHERE attrelid='billing_expected_shape'::regclass)
    SELECT 1 FROM actual FULL JOIN expected USING(ordinal)
    WHERE ROW(actual.attname,actual.atttypid,actual.atttypmod,actual.attnotnull,
              actual.attidentity,actual.attgenerated,actual.attcollation,actual.default_sql)
      IS DISTINCT FROM ROW(expected.attname,expected.atttypid,expected.atttypmod,expected.attnotnull,
              expected.attidentity,expected.attgenerated,expected.attcollation,expected.default_sql)
  ) OR EXISTS(
    WITH actual AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='usage_export_ledger_sequences'::regclass AND contype<>'f'),
    expected AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='billing_expected_shape'::regclass AND contype<>'f')
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) OR EXISTS(
    WITH expected(parent,child_columns,parent_columns,match,upd,del,deferred,initially_deferred,validated) AS (VALUES ('usage_ledger',ARRAY['usage_ledger_id']::text[],ARRAY['usage_ledger_id']::text[],'s','a','a',false,false,true),('tenants',ARRAY['tenant_id']::text[],ARRAY['tenant_id']::text[],'s','a','a',false,false,true)),
    actual AS (
      SELECT parent.relname::text,
        ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=key.num ORDER BY key.ordinal),
        ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=key.num ORDER BY key.ordinal),
        c.confmatchtype::text,c.confupdtype::text,c.confdeltype::text,c.condeferrable,c.condeferred,c.convalidated
      FROM pg_constraint c JOIN pg_class parent ON parent.oid=c.confrelid
      WHERE c.conrelid='usage_export_ledger_sequences'::regclass AND c.contype='f'
        AND parent.relnamespace=current_schema()::regnamespace
    ) (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
    UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
  ) OR EXISTS(
    WITH indexes AS (
      SELECT i.indrelid,i.indisvalid,i.indisready,i.indimmediate,i.indnkeyatts,
        ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY key(num,ordinal)
          LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=key.num ORDER BY key.ordinal) AS columns,
        pg_get_expr(i.indpred,i.indrelid) AS predicate,pg_get_expr(i.indexprs,i.indrelid) AS expression
      FROM pg_index i WHERE i.indisunique
        AND i.indrelid IN('usage_export_ledger_sequences'::regclass,'billing_expected_shape'::regclass)
    ), actual AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='usage_export_ledger_sequences'::regclass),
    expected AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='billing_expected_shape'::regclass)
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_table_drift'; END IF;
END; $shape$;
DROP TABLE billing_expected_shape;
DO $triggers$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha256) AS (VALUES ('trg_usage_export_ledger_sequences_owner_insert','fn_trg_usage_export_ledger_sequences_owner_insert',7,'14a7ecae33f29fac841f066caa211f11a26d7bd5a103d40504b5db803674944b'),
('trg_usage_export_ledger_sequences_immutable_update','fn_trg_usage_export_ledger_sequences_immutable_update',19,'fa7863273d75814bd04cb643b268f517c162ac12a930fa652e1c0dbd875d92e7'),
('trg_usage_export_ledger_sequences_immutable_delete','fn_trg_usage_export_ledger_sequences_immutable_delete',11,'9c9b00dece0b2e57dfc1be790d66f125a42c0907138610859b7ccb51f3a18619')), actual AS (
      SELECT t.tgname,t.tgenabled,t.tgtype,t.tgnargs,t.tgqual,t.tgattr,t.tgconstraint,
        p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='usage_export_ledger_sequences'::regclass AND NOT t.tgisinternal
    ) SELECT 1 FROM expected FULL JOIN actual ON name=tgname
    WHERE name IS NULL OR tgname IS NULL OR proname<>function OR tgenabled<>'O' OR tgtype<>bits
      OR tgnargs<>0 OR tgqual IS NOT NULL OR tgattr::text<>'' OR tgconstraint<>0
      OR metahub_sha256(prosrc)<>sha256 OR prosecdef OR proconfig IS NOT NULL OR proisstrict
      OR proleakproof OR provolatile<>'v' OR proparallel<>'u'
      OR pronamespace<>current_schema()::regnamespace OR prorettype<>'trigger'::regtype
      OR prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_trigger_drift'; END IF;
END; $triggers$;
DO $manifest$ BEGIN
  IF (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='usage_export_ledger_sequences')<>1
    OR EXISTS(SELECT 1 FROM tenant_deletion_table_manifest m CROSS JOIN (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='usage_export_ledger_sequences'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='usage_export_ledger_sequences'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='usage_export_ledger_sequences'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE usage_export_ledger_sequences ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual
      WHERE m.source_table='usage_export_ledger_sequences' AND (
        m.table_sql_sha256 NOT IN(actual.table_sha)
        OR m.column_signature_sha256 NOT IN(actual.columns_sha)
        OR m.trigger_set_sha256 IS DISTINCT FROM (SELECT metahub_sha256(coalesce(
          string_agg(trigger_name||chr(31)||trigger_sha256,chr(30) ORDER BY trigger_name),''))
          FROM tenant_deletion_trigger_manifest WHERE source_table='usage_export_ledger_sequences')))
    OR EXISTS(
      WITH legacy(name,sha256) AS (SELECT NULL::text,NULL::text WHERE false), actual AS (
        SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
        FROM pg_trigger WHERE tgrelid='usage_export_ledger_sequences'::regclass AND NOT tgisinternal
      ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='usage_export_ledger_sequences')
      SELECT 1 FROM actual FULL JOIN recorded ON tgname=trigger_name LEFT JOIN legacy ON name=trigger_name
      WHERE tgname IS NULL OR trigger_name IS NULL OR metahub_sha256(trigger_sql)<>trigger_sha256
        OR (trigger_sql IS DISTINCT FROM sql AND trigger_sha256 IS DISTINCT FROM sha256)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_manifest_drift'; END IF;
END; $manifest$;
