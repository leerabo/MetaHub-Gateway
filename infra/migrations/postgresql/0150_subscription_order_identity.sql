LOCK TABLE subscription_order_records,tenant_deletion_table_manifest,tenant_deletion_trigger_manifest IN ACCESS EXCLUSIVE MODE;
CREATE TEMPORARY TABLE billing_expected_shape (order_record_id text, workspace_id text NOT NULL, tenant_id text NOT NULL, subscription_id text NOT NULL, plan_version_id text NOT NULL, order_kind text NOT NULL, status text NOT NULL, currency text NOT NULL, amount_minor bigint NOT NULL, idempotency_sha256 text NOT NULL, request_sha256 text NOT NULL, created_by_membership_id text NOT NULL, created_at text NOT NULL, PRIMARY KEY (order_record_id));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_1 CHECK (length(order_record_id) = 36 AND substr(order_record_id, 1, 4) = 'sor_');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_2 CHECK (order_kind IN ('create', 'upgrade', 'downgrade'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_3 CHECK (status IN ('not_required', 'pending_payment', 'scheduled'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_4 CHECK (currency IN ('CNY', 'USD'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_5 CHECK (amount_minor BETWEEN 0 AND 1000000000000);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_6 CHECK (length(idempotency_sha256) = 64);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_7 CHECK (length(request_sha256) = 64);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_8 CHECK (unixepoch(created_at) IS NOT NULL);
CREATE UNIQUE INDEX billing_expected_index_0 ON billing_expected_shape (workspace_id, idempotency_sha256);
CREATE UNIQUE INDEX billing_expected_index_1 ON billing_expected_shape (order_record_id);
DO $shape$ BEGIN
  IF EXISTS(
    WITH columns AS (
      SELECT attrelid,row_number() OVER(PARTITION BY attrelid ORDER BY attnum) AS ordinal,
        attname,atttypid,atttypmod,attnotnull,attidentity,attgenerated,attcollation,
        pg_get_expr(value.adbin,value.adrelid) AS default_sql
      FROM pg_attribute attribute LEFT JOIN pg_attrdef value
        ON value.adrelid=attribute.attrelid AND value.adnum=attribute.attnum
      WHERE attrelid IN('subscription_order_records'::regclass,'billing_expected_shape'::regclass)
        AND attnum>0 AND NOT attisdropped
    ), actual AS (SELECT * FROM columns WHERE attrelid='subscription_order_records'::regclass),
    expected AS (SELECT * FROM columns WHERE attrelid='billing_expected_shape'::regclass)
    SELECT 1 FROM actual FULL JOIN expected USING(ordinal)
    WHERE ROW(actual.attname,actual.atttypid,actual.atttypmod,actual.attnotnull,
              actual.attidentity,actual.attgenerated,actual.attcollation,actual.default_sql)
      IS DISTINCT FROM ROW(expected.attname,expected.atttypid,expected.atttypmod,expected.attnotnull,
              expected.attidentity,expected.attgenerated,expected.attcollation,expected.default_sql)
  ) OR EXISTS(
    WITH actual AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='subscription_order_records'::regclass AND contype<>'f'),
    expected AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='billing_expected_shape'::regclass AND contype<>'f')
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) OR EXISTS(
    WITH expected(parent,child_columns,parent_columns,match,upd,del,deferred,initially_deferred,validated) AS (VALUES ('workspace_memberships',ARRAY['created_by_membership_id']::text[],ARRAY['workspace_membership_id']::text[],'s','a','a',false,false,true),('saas_plan_versions',ARRAY['plan_version_id']::text[],ARRAY['plan_version_id']::text[],'s','a','a',false,false,true),('workspace_subscriptions',ARRAY['workspace_id','tenant_id','subscription_id']::text[],ARRAY['workspace_id','tenant_id','subscription_id']::text[],'s','a','a',false,false,true)),
    actual AS (
      SELECT parent.relname::text,
        ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=key.num ORDER BY key.ordinal),
        ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=key.num ORDER BY key.ordinal),
        c.confmatchtype::text,c.confupdtype::text,c.confdeltype::text,c.condeferrable,c.condeferred,c.convalidated
      FROM pg_constraint c JOIN pg_class parent ON parent.oid=c.confrelid
      WHERE c.conrelid='subscription_order_records'::regclass AND c.contype='f'
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
        AND i.indrelid IN('subscription_order_records'::regclass,'billing_expected_shape'::regclass)
    ), actual AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='subscription_order_records'::regclass),
    expected AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='billing_expected_shape'::regclass)
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_table_drift'; END IF;
END; $shape$;
DROP TABLE billing_expected_shape;
DO $triggers$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha256) AS (SELECT NULL::text,NULL::text,NULL::int,NULL::text WHERE false), actual AS (
      SELECT t.tgname,t.tgenabled,t.tgtype,t.tgnargs,t.tgqual,t.tgattr,t.tgconstraint,
        p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='subscription_order_records'::regclass AND NOT t.tgisinternal
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
  IF (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='subscription_order_records')<>1
    OR EXISTS(SELECT 1 FROM tenant_deletion_table_manifest m CROSS JOIN (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='subscription_order_records'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='subscription_order_records'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='subscription_order_records'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE subscription_order_records ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual
      WHERE m.source_table='subscription_order_records' AND (
        m.table_sql_sha256 NOT IN('afc8e54669b323baf5a90f35c1f77739ec0389d861efe65b509c01b8d62c4190',actual.table_sha)
        OR m.column_signature_sha256 NOT IN('d5b0092a2fe43a999e2e742ec252142068f52ef1cd4c059266cc76c47c0dff25',actual.columns_sha)
        OR m.trigger_set_sha256 IS DISTINCT FROM (SELECT metahub_sha256(coalesce(
          string_agg(trigger_name||chr(31)||trigger_sha256,chr(30) ORDER BY trigger_name),''))
          FROM tenant_deletion_trigger_manifest WHERE source_table='subscription_order_records')))
    OR EXISTS(
      WITH legacy(name,sha256) AS (SELECT NULL::text,NULL::text WHERE false), actual AS (
        SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
        FROM pg_trigger WHERE tgrelid='subscription_order_records'::regclass AND NOT tgisinternal
      ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='subscription_order_records')
      SELECT 1 FROM actual FULL JOIN recorded ON tgname=trigger_name LEFT JOIN legacy ON name=trigger_name
      WHERE tgname IS NULL OR trigger_name IS NULL OR metahub_sha256(trigger_sql)<>trigger_sha256
        OR (trigger_sql IS DISTINCT FROM sql AND trigger_sha256 IS DISTINCT FROM sha256)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_manifest_drift'; END IF;
END; $manifest$;
DO $history$ BEGIN
  IF EXISTS(SELECT 1 FROM subscription_order_records GROUP BY subscription_id,created_at HAVING COUNT(*)>1)
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_order_history_ambiguous'; END IF;
END; $history$;
ALTER TABLE subscription_order_records ADD COLUMN rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE;
CREATE OR REPLACE FUNCTION fn_native_subscription_order_identity() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
  IF NEW.rowid IS DISTINCT FROM OLD.rowid THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_subscription_order_identity_immutable';
  END IF;
  RETURN NEW;
END;
$body$;
CREATE TRIGGER trg_native_subscription_order_identity BEFORE UPDATE ON subscription_order_records FOR EACH ROW EXECUTE FUNCTION fn_native_subscription_order_identity();
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT tgname,'subscription_order_records',pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid),
  metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid))
FROM pg_trigger WHERE tgrelid='subscription_order_records'::regclass AND tgname IN('trg_native_subscription_order_identity');
UPDATE tenant_deletion_trigger_manifest m SET
  trigger_sql=pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid))
FROM pg_trigger t WHERE t.tgrelid='subscription_order_records'::regclass AND m.source_table='subscription_order_records' AND m.trigger_name=t.tgname;
UPDATE tenant_deletion_table_manifest m SET table_sql_sha256=actual.table_sha,
  column_signature_sha256=actual.columns_sha,trigger_set_sha256=actual.triggers_sha
FROM (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='subscription_order_records'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='subscription_order_records'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='subscription_order_records'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE subscription_order_records ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual WHERE m.source_table='subscription_order_records';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
CREATE TEMPORARY TABLE billing_expected_shape (order_record_id text, workspace_id text NOT NULL, tenant_id text NOT NULL, subscription_id text NOT NULL, plan_version_id text NOT NULL, order_kind text NOT NULL, status text NOT NULL, currency text NOT NULL, amount_minor bigint NOT NULL, idempotency_sha256 text NOT NULL, request_sha256 text NOT NULL, created_by_membership_id text NOT NULL, created_at text NOT NULL, PRIMARY KEY (order_record_id));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_1 CHECK (length(order_record_id) = 36 AND substr(order_record_id, 1, 4) = 'sor_');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_2 CHECK (order_kind IN ('create', 'upgrade', 'downgrade'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_3 CHECK (status IN ('not_required', 'pending_payment', 'scheduled'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_4 CHECK (currency IN ('CNY', 'USD'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_5 CHECK (amount_minor BETWEEN 0 AND 1000000000000);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_6 CHECK (length(idempotency_sha256) = 64);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_7 CHECK (length(request_sha256) = 64);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_subscription_order_records_8 CHECK (unixepoch(created_at) IS NOT NULL);
CREATE UNIQUE INDEX billing_expected_index_0 ON billing_expected_shape (workspace_id, idempotency_sha256);
CREATE UNIQUE INDEX billing_expected_index_1 ON billing_expected_shape (order_record_id);
ALTER TABLE billing_expected_shape ADD COLUMN rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE;
DO $shape$ BEGIN
  IF EXISTS(
    WITH columns AS (
      SELECT attrelid,row_number() OVER(PARTITION BY attrelid ORDER BY attnum) AS ordinal,
        attname,atttypid,atttypmod,attnotnull,attidentity,attgenerated,attcollation,
        pg_get_expr(value.adbin,value.adrelid) AS default_sql
      FROM pg_attribute attribute LEFT JOIN pg_attrdef value
        ON value.adrelid=attribute.attrelid AND value.adnum=attribute.attnum
      WHERE attrelid IN('subscription_order_records'::regclass,'billing_expected_shape'::regclass)
        AND attnum>0 AND NOT attisdropped
    ), actual AS (SELECT * FROM columns WHERE attrelid='subscription_order_records'::regclass),
    expected AS (SELECT * FROM columns WHERE attrelid='billing_expected_shape'::regclass)
    SELECT 1 FROM actual FULL JOIN expected USING(ordinal)
    WHERE ROW(actual.attname,actual.atttypid,actual.atttypmod,actual.attnotnull,
              actual.attidentity,actual.attgenerated,actual.attcollation,actual.default_sql)
      IS DISTINCT FROM ROW(expected.attname,expected.atttypid,expected.atttypmod,expected.attnotnull,
              expected.attidentity,expected.attgenerated,expected.attcollation,expected.default_sql)
  ) OR EXISTS(
    WITH actual AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='subscription_order_records'::regclass AND contype<>'f'),
    expected AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='billing_expected_shape'::regclass AND contype<>'f')
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) OR EXISTS(
    WITH expected(parent,child_columns,parent_columns,match,upd,del,deferred,initially_deferred,validated) AS (VALUES ('workspace_memberships',ARRAY['created_by_membership_id']::text[],ARRAY['workspace_membership_id']::text[],'s','a','a',false,false,true),('saas_plan_versions',ARRAY['plan_version_id']::text[],ARRAY['plan_version_id']::text[],'s','a','a',false,false,true),('workspace_subscriptions',ARRAY['workspace_id','tenant_id','subscription_id']::text[],ARRAY['workspace_id','tenant_id','subscription_id']::text[],'s','a','a',false,false,true)),
    actual AS (
      SELECT parent.relname::text,
        ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=key.num ORDER BY key.ordinal),
        ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=key.num ORDER BY key.ordinal),
        c.confmatchtype::text,c.confupdtype::text,c.confdeltype::text,c.condeferrable,c.condeferred,c.convalidated
      FROM pg_constraint c JOIN pg_class parent ON parent.oid=c.confrelid
      WHERE c.conrelid='subscription_order_records'::regclass AND c.contype='f'
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
        AND i.indrelid IN('subscription_order_records'::regclass,'billing_expected_shape'::regclass)
    ), actual AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='subscription_order_records'::regclass),
    expected AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='billing_expected_shape'::regclass)
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_table_drift'; END IF;
END; $shape$;
DROP TABLE billing_expected_shape;
DO $triggers$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha256) AS (VALUES ('trg_native_subscription_order_identity','fn_native_subscription_order_identity',19,'09dd1d324a9e11c9a29ea86cb93bb15c38264414d484248bf252bed04d423ed0')), actual AS (
      SELECT t.tgname,t.tgenabled,t.tgtype,t.tgnargs,t.tgqual,t.tgattr,t.tgconstraint,
        p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='subscription_order_records'::regclass AND NOT t.tgisinternal
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
  IF (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='subscription_order_records')<>1
    OR EXISTS(SELECT 1 FROM tenant_deletion_table_manifest m CROSS JOIN (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='subscription_order_records'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='subscription_order_records'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='subscription_order_records'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE subscription_order_records ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual
      WHERE m.source_table='subscription_order_records' AND (
        m.table_sql_sha256 NOT IN(actual.table_sha)
        OR m.column_signature_sha256 NOT IN(actual.columns_sha)
        OR m.trigger_set_sha256 IS DISTINCT FROM (SELECT metahub_sha256(coalesce(
          string_agg(trigger_name||chr(31)||trigger_sha256,chr(30) ORDER BY trigger_name),''))
          FROM tenant_deletion_trigger_manifest WHERE source_table='subscription_order_records')))
    OR EXISTS(
      WITH legacy(name,sha256) AS (SELECT NULL::text,NULL::text WHERE false), actual AS (
        SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
        FROM pg_trigger WHERE tgrelid='subscription_order_records'::regclass AND NOT tgisinternal
      ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='subscription_order_records')
      SELECT 1 FROM actual FULL JOIN recorded ON tgname=trigger_name LEFT JOIN legacy ON name=trigger_name
      WHERE tgname IS NULL OR trigger_name IS NULL OR metahub_sha256(trigger_sql)<>trigger_sha256
        OR (trigger_sql IS DISTINCT FROM sql AND trigger_sha256 IS DISTINCT FROM sha256)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_manifest_drift'; END IF;
END; $manifest$;
DO $identity$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_sequence s JOIN pg_class r ON r.oid=s.seqrelid
    JOIN pg_depend d ON d.objid=r.oid AND d.classid='pg_class'::regclass AND d.deptype='i'
    JOIN pg_attribute a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
    WHERE a.attrelid='subscription_order_records'::regclass AND a.attname='rowid' AND a.attidentity='a'
      AND r.relnamespace=current_schema()::regnamespace AND s.seqincrement=1 AND s.seqmin=1
      AND s.seqmax=9223372036854775807 AND s.seqcache=1 AND NOT s.seqcycle)
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_order_identity_drift'; END IF;
END; $identity$;
