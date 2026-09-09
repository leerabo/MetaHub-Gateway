LOCK TABLE finops_ledger_entries,tenant_deletion_table_manifest,tenant_deletion_trigger_manifest IN ACCESS EXCLUSIVE MODE;
LOCK TABLE metahub_finops_ledger_allocator IN ACCESS EXCLUSIVE MODE;
CREATE TEMPORARY TABLE billing_expected_shape (singleton_id integer PRIMARY KEY CHECK (singleton_id = 1), last_sequence bigint NOT NULL CHECK (last_sequence >= 0));
DO $shape$ BEGIN
  IF EXISTS(
    WITH columns AS (
      SELECT attrelid,row_number() OVER(PARTITION BY attrelid ORDER BY attnum) AS ordinal,
        attname,atttypid,atttypmod,attnotnull,attidentity,attgenerated,attcollation,
        pg_get_expr(value.adbin,value.adrelid) AS default_sql
      FROM pg_attribute attribute LEFT JOIN pg_attrdef value
        ON value.adrelid=attribute.attrelid AND value.adnum=attribute.attnum
      WHERE attrelid IN('metahub_finops_ledger_allocator'::regclass,'billing_expected_shape'::regclass)
        AND attnum>0 AND NOT attisdropped
    ), actual AS (SELECT * FROM columns WHERE attrelid='metahub_finops_ledger_allocator'::regclass),
    expected AS (SELECT * FROM columns WHERE attrelid='billing_expected_shape'::regclass)
    SELECT 1 FROM actual FULL JOIN expected USING(ordinal)
    WHERE ROW(actual.attname,actual.atttypid,actual.atttypmod,actual.attnotnull,
              actual.attidentity,actual.attgenerated,actual.attcollation,actual.default_sql)
      IS DISTINCT FROM ROW(expected.attname,expected.atttypid,expected.atttypmod,expected.attnotnull,
              expected.attidentity,expected.attgenerated,expected.attcollation,expected.default_sql)
  ) OR EXISTS(
    WITH actual AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='metahub_finops_ledger_allocator'::regclass AND contype<>'f'),
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
      WHERE c.conrelid='metahub_finops_ledger_allocator'::regclass AND c.contype='f'
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
        AND i.indrelid IN('metahub_finops_ledger_allocator'::regclass,'billing_expected_shape'::regclass)
    ), actual AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='metahub_finops_ledger_allocator'::regclass),
    expected AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='billing_expected_shape'::regclass)
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_table_drift'; END IF;
END; $shape$;
DROP TABLE billing_expected_shape;
DO $triggers$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha256) AS (VALUES ('trg_native_finops_allocator_guard','fn_native_finops_allocator_guard',31,'eea0896eb054bf7f97a844026652490d41b5dbdad0e0c9889719739b800d17aa'),
('trg_native_finops_allocator_truncate','fn_native_finops_allocator_guard',34,'eea0896eb054bf7f97a844026652490d41b5dbdad0e0c9889719739b800d17aa')), actual AS (
      SELECT t.tgname,t.tgenabled,t.tgtype,t.tgnargs,t.tgqual,t.tgattr,t.tgconstraint,
        p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='metahub_finops_ledger_allocator'::regclass AND NOT t.tgisinternal
    ) SELECT 1 FROM expected FULL JOIN actual ON name=tgname
    WHERE name IS NULL OR tgname IS NULL OR proname<>function OR tgenabled<>'O' OR tgtype<>bits
      OR tgnargs<>0 OR tgqual IS NOT NULL OR tgattr::text<>'' OR tgconstraint<>0
      OR metahub_sha256(prosrc)<>sha256 OR prosecdef OR proconfig IS NOT NULL OR proisstrict
      OR proleakproof OR provolatile<>'v' OR proparallel<>'u'
      OR pronamespace<>current_schema()::regnamespace OR prorettype<>'trigger'::regtype
      OR prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_trigger_drift'; END IF;
END; $triggers$;
CREATE TEMPORARY TABLE billing_expected_shape (ledger_sequence bigint, finops_ledger_entry_id text NOT NULL, event_id text NOT NULL, tenant_id text NOT NULL, entry_type text NOT NULL, direction text NOT NULL, amount_micros bigint NOT NULL, currency text NOT NULL, normalized_cny_micros bigint, cny_normalization_status text NOT NULL, price_evidence_status text NOT NULL, price_snapshot_source text, posting_source text NOT NULL, quota_type text NOT NULL, usage_ledger_id text, finops_adjustment_id text, source_request_id text, source_trace_id text, operation_request_id text NOT NULL, operation_trace_id text NOT NULL, price_snapshot_id text, price_schema_version text, reversal_of_entry_id text, actor_user_id text, input_tokens bigint NOT NULL DEFAULT 0, output_tokens bigint NOT NULL DEFAULT 0, total_tokens bigint NOT NULL DEFAULT 0, cache_tokens bigint NOT NULL DEFAULT 0, model_id text, provider_id text, virtual_key_id text, environment_id text, project_id text, application_id text, estimated bigint NOT NULL DEFAULT 0, status text NOT NULL DEFAULT 'posted', created_at text NOT NULL, updated_at text NOT NULL, PRIMARY KEY (ledger_sequence));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_1 CHECK (ledger_sequence > 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_2 CHECK (length(finops_ledger_entry_id) = 36 AND substr(finops_ledger_entry_id, 1, 4) = 'fle_' AND substr(finops_ledger_entry_id, 5) OPERATOR(public.!@@) '*[^0-9a-f]*');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_3 CHECK (length(event_id) = 38 AND substr(event_id, 1, 6) = 'flevt_' AND substr(event_id, 7) OPERATOR(public.!@@) '*[^0-9a-f]*');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_4 CHECK (entry_type IN ('usage_charge', 'manual_debit', 'manual_credit', 'reversal', 'gift_credit', 'trial_credit'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_5 CHECK (direction IN ('debit', 'credit'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_6 CHECK (typeof(amount_micros) = 'integer' AND amount_micros BETWEEN 0 AND 1000000000000000);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_7 CHECK (currency IN ('CNY', 'USD'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_8 CHECK (normalized_cny_micros IS NULL OR (typeof(normalized_cny_micros) = 'integer' AND normalized_cny_micros BETWEEN 0 AND 1000000000000000));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_9 CHECK (cny_normalization_status IN ('normalized', 'unavailable'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_10 CHECK (price_evidence_status IN ('priced', 'unpriced', 'legacy_amount_only', 'not_applicable'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_11 CHECK (posting_source IN ('direct', 'spool_replay', 'legacy_backfill'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_12 CHECK (quota_type IN ('paid', 'free', 'trial', 'gift'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_13 CHECK (length(operation_request_id) BETWEEN 1 AND 128);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_14 CHECK (length(operation_trace_id) BETWEEN 1 AND 128);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_15 CHECK (typeof(input_tokens) = 'integer' AND input_tokens >= 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_16 CHECK (typeof(output_tokens) = 'integer' AND output_tokens >= 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_17 CHECK (typeof(total_tokens) = 'integer' AND total_tokens >= 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_18 CHECK (typeof(cache_tokens) = 'integer' AND cache_tokens >= 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_19 CHECK (estimated IN (0, 1));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_20 CHECK (status = 'posted');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_21 CHECK (created_at = updated_at);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_22 CHECK (entry_type = 'usage_charge' OR amount_micros > 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_23 CHECK ((entry_type IN ('usage_charge', 'manual_debit') AND direction = 'debit') OR (entry_type IN ('manual_credit', 'gift_credit', 'trial_credit') AND direction = 'credit') OR entry_type = 'reversal');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_24 CHECK ((currency = 'CNY' AND price_evidence_status = 'unpriced' AND price_snapshot_source = 'response_cache_reference' AND price_schema_version = 'unpriced-v1' AND amount_micros = 0 AND cny_normalization_status = 'unavailable' AND normalized_cny_micros IS NULL) OR (currency = 'CNY' AND NOT(price_evidence_status = 'unpriced' AND price_snapshot_source = 'response_cache_reference' AND price_schema_version = 'unpriced-v1') AND cny_normalization_status = 'normalized' AND normalized_cny_micros = amount_micros) OR (currency = 'USD' AND cny_normalization_status = 'unavailable' AND normalized_cny_micros IS NULL));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_25 CHECK ((entry_type = 'usage_charge' AND usage_ledger_id IS NOT NULL AND finops_adjustment_id IS NULL AND actor_user_id IS NULL AND source_request_id IS NOT NULL AND source_trace_id IS NOT NULL) OR (entry_type <> 'usage_charge' AND usage_ledger_id IS NULL AND finops_adjustment_id IS NOT NULL AND actor_user_id IS NOT NULL));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_26 CHECK ((entry_type = 'reversal' AND reversal_of_entry_id IS NOT NULL) OR (entry_type <> 'reversal' AND reversal_of_entry_id IS NULL));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_27 CHECK ((entry_type = 'usage_charge' AND quota_type = 'paid') OR (entry_type IN ('manual_debit', 'manual_credit') AND quota_type = 'paid') OR (entry_type = 'gift_credit' AND quota_type = 'gift') OR (entry_type = 'trial_credit' AND quota_type = 'trial') OR entry_type = 'reversal');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_28 CHECK ((price_evidence_status = 'not_applicable' AND price_snapshot_id IS NULL AND price_schema_version IS NULL AND price_snapshot_source IS NULL) OR (price_evidence_status = 'unpriced' AND price_snapshot_id IS NOT NULL AND price_schema_version = 'unpriced-v1' AND price_snapshot_source IN ('finops_unpriced', 'response_cache_reference')) OR (price_evidence_status = 'legacy_amount_only' AND price_snapshot_id IS NOT NULL AND price_schema_version = 'legacy-unversioned-v1' AND price_snapshot_source = 'finops_legacy_backfill') OR (price_evidence_status = 'priced' AND price_snapshot_id IS NOT NULL AND NULLIF(TRIM(BOTH FROM price_schema_version), '') IS NOT NULL AND NULLIF(TRIM(BOTH FROM price_snapshot_source), '') IS NOT NULL AND price_snapshot_source NOT IN ('finops_unpriced', 'finops_legacy_backfill') AND NOT(price_snapshot_source = 'response_cache_reference' AND price_schema_version = 'unpriced-v1')));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_29 CHECK (entry_type <> 'usage_charge' OR price_evidence_status <> 'not_applicable');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_30 CHECK (entry_type = 'usage_charge' OR entry_type = 'reversal' OR price_evidence_status = 'not_applicable');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_31 CHECK (entry_type = 'usage_charge' OR (input_tokens = 0 AND output_tokens = 0 AND total_tokens = 0 AND cache_tokens = 0 AND estimated = 0));
CREATE UNIQUE INDEX billing_expected_index_0 ON billing_expected_shape (tenant_id, source_request_id, finops_ledger_entry_id);
CREATE UNIQUE INDEX billing_expected_index_1 ON billing_expected_shape (tenant_id, finops_adjustment_id);
CREATE UNIQUE INDEX billing_expected_index_2 ON billing_expected_shape (tenant_id, usage_ledger_id);
CREATE UNIQUE INDEX billing_expected_index_3 ON billing_expected_shape (tenant_id, event_id);
CREATE UNIQUE INDEX billing_expected_index_4 ON billing_expected_shape (tenant_id, finops_ledger_entry_id);
CREATE UNIQUE INDEX billing_expected_index_5 ON billing_expected_shape (event_id);
CREATE UNIQUE INDEX billing_expected_index_6 ON billing_expected_shape (finops_ledger_entry_id);
DO $shape$ BEGIN
  IF EXISTS(
    WITH columns AS (
      SELECT attrelid,row_number() OVER(PARTITION BY attrelid ORDER BY attnum) AS ordinal,
        attname,atttypid,atttypmod,attnotnull,attidentity,attgenerated,attcollation,
        pg_get_expr(value.adbin,value.adrelid) AS default_sql
      FROM pg_attribute attribute LEFT JOIN pg_attrdef value
        ON value.adrelid=attribute.attrelid AND value.adnum=attribute.attnum
      WHERE attrelid IN('finops_ledger_entries'::regclass,'billing_expected_shape'::regclass)
        AND attnum>0 AND NOT attisdropped
    ), actual AS (SELECT * FROM columns WHERE attrelid='finops_ledger_entries'::regclass),
    expected AS (SELECT * FROM columns WHERE attrelid='billing_expected_shape'::regclass)
    SELECT 1 FROM actual FULL JOIN expected USING(ordinal)
    WHERE ROW(actual.attname,actual.atttypid,actual.atttypmod,actual.attnotnull,
              actual.attidentity,actual.attgenerated,actual.attcollation,actual.default_sql)
      IS DISTINCT FROM ROW(expected.attname,expected.atttypid,expected.atttypmod,expected.attnotnull,
              expected.attidentity,expected.attgenerated,expected.attcollation,expected.default_sql)
  ) OR EXISTS(
    WITH actual AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='finops_ledger_entries'::regclass AND contype<>'f'),
    expected AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='billing_expected_shape'::regclass AND contype<>'f')
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) OR EXISTS(
    WITH expected(parent,child_columns,parent_columns,match,upd,del,deferred,initially_deferred,validated) AS (VALUES ('applications',ARRAY['tenant_id','application_id']::text[],ARRAY['tenant_id','application_id']::text[],'s','a','a',false,false,true),('projects',ARRAY['tenant_id','project_id']::text[],ARRAY['tenant_id','project_id']::text[],'s','a','a',false,false,true),('environments',ARRAY['tenant_id','environment_id']::text[],ARRAY['tenant_id','environment_id']::text[],'s','a','a',false,false,true),('virtual_keys',ARRAY['tenant_id','virtual_key_id']::text[],ARRAY['tenant_id','virtual_key_id']::text[],'s','a','a',false,false,true),('providers',ARRAY['tenant_id','provider_id']::text[],ARRAY['tenant_id','provider_id']::text[],'s','a','a',false,false,true),('models',ARRAY['tenant_id','model_id']::text[],ARRAY['tenant_id','model_id']::text[],'s','a','a',false,false,true),('users',ARRAY['tenant_id','actor_user_id']::text[],ARRAY['tenant_id','user_id']::text[],'s','a','a',false,false,true),('finops_ledger_entries',ARRAY['tenant_id','reversal_of_entry_id']::text[],ARRAY['tenant_id','finops_ledger_entry_id']::text[],'s','a','a',false,false,true),('price_snapshots',ARRAY['tenant_id','price_snapshot_id']::text[],ARRAY['tenant_id','price_snapshot_id']::text[],'s','a','a',false,false,true),('request_logs',ARRAY['tenant_id','source_request_id']::text[],ARRAY['tenant_id','request_id']::text[],'s','a','a',false,false,true),('finops_adjustments',ARRAY['tenant_id','finops_adjustment_id']::text[],ARRAY['tenant_id','finops_adjustment_id']::text[],'s','a','a',false,false,true),('usage_ledger',ARRAY['tenant_id','usage_ledger_id']::text[],ARRAY['tenant_id','usage_ledger_id']::text[],'s','a','a',false,false,true),('tenants',ARRAY['tenant_id']::text[],ARRAY['tenant_id']::text[],'s','a','a',false,false,true)),
    actual AS (
      SELECT parent.relname::text,
        ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=key.num ORDER BY key.ordinal),
        ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=key.num ORDER BY key.ordinal),
        c.confmatchtype::text,c.confupdtype::text,c.confdeltype::text,c.condeferrable,c.condeferred,c.convalidated
      FROM pg_constraint c JOIN pg_class parent ON parent.oid=c.confrelid
      WHERE c.conrelid='finops_ledger_entries'::regclass AND c.contype='f'
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
        AND i.indrelid IN('finops_ledger_entries'::regclass,'billing_expected_shape'::regclass)
    ), actual AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='finops_ledger_entries'::regclass),
    expected AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='billing_expected_shape'::regclass)
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_table_drift'; END IF;
END; $shape$;
DROP TABLE billing_expected_shape;
DO $triggers$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha256) AS (VALUES ('trg_finops_ledger_owner_insert_prd051','fn_trg_finops_ledger_owner_insert_prd051',7,'7d6579606fb27b0dd8a049ea4908b0590171353f46cb54e4e50f722af0716b84'),
('trg_finops_ledger_conflict_insert_prd051','fn_trg_finops_ledger_conflict_insert_prd051',7,'111fa296a5965cb720db541d7c8e812fb17432cc191b7f2a78d067660ab6bd89'),
('trg_finops_ledger_immutable_update_prd051','fn_trg_finops_ledger_immutable_update_prd051',19,'7bf116e79507f4052fc71e4b5223d4041c055915421987bc5838836e56e14105'),
('trg_finops_ledger_immutable_delete_prd051','fn_trg_finops_ledger_immutable_delete_prd051',11,'75ebcdbb8fe4d6c94810f6b4c4a5bcd5449b3f0a4463c8bfc89768de46d28017'),
('trg_finops_experiment_settlement_prd075','fn_trg_finops_experiment_settlement_prd075',5,'ee7b3b4e9495ee89bd6fa1aa74b064382cb06e81abdf4aebcea719abae0f00eb'),
('trg_native_finops_sequence','fn_native_finops_sequence',7,'0f6e2881f8f25c4bb09776fcc896b56517b86c0999566b023fc1c96442176989')), actual AS (
      SELECT t.tgname,t.tgenabled,t.tgtype,t.tgnargs,t.tgqual,t.tgattr,t.tgconstraint,
        p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='finops_ledger_entries'::regclass AND NOT t.tgisinternal
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
  IF (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='finops_ledger_entries')<>1
    OR EXISTS(SELECT 1 FROM tenant_deletion_table_manifest m CROSS JOIN (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='finops_ledger_entries'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='finops_ledger_entries'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='finops_ledger_entries'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE finops_ledger_entries ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual
      WHERE m.source_table='finops_ledger_entries' AND (
        m.table_sql_sha256 NOT IN(actual.table_sha)
        OR m.column_signature_sha256 NOT IN(actual.columns_sha)
        OR m.trigger_set_sha256 IS DISTINCT FROM (SELECT metahub_sha256(coalesce(
          string_agg(trigger_name||chr(31)||trigger_sha256,chr(30) ORDER BY trigger_name),''))
          FROM tenant_deletion_trigger_manifest WHERE source_table='finops_ledger_entries')))
    OR EXISTS(
      WITH legacy(name,sha256) AS (SELECT NULL::text,NULL::text WHERE false), actual AS (
        SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
        FROM pg_trigger WHERE tgrelid='finops_ledger_entries'::regclass AND NOT tgisinternal
      ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='finops_ledger_entries')
      SELECT 1 FROM actual FULL JOIN recorded ON tgname=trigger_name LEFT JOIN legacy ON name=trigger_name
      WHERE tgname IS NULL OR trigger_name IS NULL OR metahub_sha256(trigger_sql)<>trigger_sha256
        OR (trigger_sql IS DISTINCT FROM sql AND trigger_sha256 IS DISTINCT FROM sha256)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_manifest_drift'; END IF;
END; $manifest$;
DO $facts$ BEGIN
  IF (SELECT COUNT(*) FROM metahub_finops_ledger_allocator)<>1 OR (SELECT last_sequence FROM metahub_finops_ledger_allocator)<>0
    OR EXISTS(SELECT 1 FROM finops_ledger_entries) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_finops_sequence_rollback_has_facts';
  END IF;
END; $facts$;
DROP TRIGGER trg_native_finops_sequence ON finops_ledger_entries;
DROP FUNCTION fn_native_finops_sequence();
DROP TABLE metahub_finops_ledger_allocator;
DROP FUNCTION fn_native_finops_allocator_guard();
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
ALTER TABLE tenant_deletion_trigger_manifest DISABLE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table='finops_ledger_entries' AND trigger_name IN('trg_native_finops_sequence');
ALTER TABLE tenant_deletion_trigger_manifest ENABLE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
UPDATE tenant_deletion_trigger_manifest m SET
  trigger_sql=pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(t.oid)||';'||chr(10)||pg_get_functiondef(t.tgfoid))
FROM pg_trigger t WHERE t.tgrelid='finops_ledger_entries'::regclass AND m.source_table='finops_ledger_entries' AND m.trigger_name=t.tgname;
UPDATE tenant_deletion_table_manifest m SET table_sql_sha256=actual.table_sha,
  column_signature_sha256=actual.columns_sha,trigger_set_sha256=actual.triggers_sha
FROM (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='finops_ledger_entries'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='finops_ledger_entries'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='finops_ledger_entries'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE finops_ledger_entries ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual WHERE m.source_table='finops_ledger_entries';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
CREATE TEMPORARY TABLE billing_expected_shape (ledger_sequence bigint, finops_ledger_entry_id text NOT NULL, event_id text NOT NULL, tenant_id text NOT NULL, entry_type text NOT NULL, direction text NOT NULL, amount_micros bigint NOT NULL, currency text NOT NULL, normalized_cny_micros bigint, cny_normalization_status text NOT NULL, price_evidence_status text NOT NULL, price_snapshot_source text, posting_source text NOT NULL, quota_type text NOT NULL, usage_ledger_id text, finops_adjustment_id text, source_request_id text, source_trace_id text, operation_request_id text NOT NULL, operation_trace_id text NOT NULL, price_snapshot_id text, price_schema_version text, reversal_of_entry_id text, actor_user_id text, input_tokens bigint NOT NULL DEFAULT 0, output_tokens bigint NOT NULL DEFAULT 0, total_tokens bigint NOT NULL DEFAULT 0, cache_tokens bigint NOT NULL DEFAULT 0, model_id text, provider_id text, virtual_key_id text, environment_id text, project_id text, application_id text, estimated bigint NOT NULL DEFAULT 0, status text NOT NULL DEFAULT 'posted', created_at text NOT NULL, updated_at text NOT NULL, PRIMARY KEY (ledger_sequence));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_1 CHECK (ledger_sequence > 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_2 CHECK (length(finops_ledger_entry_id) = 36 AND substr(finops_ledger_entry_id, 1, 4) = 'fle_' AND substr(finops_ledger_entry_id, 5) OPERATOR(public.!@@) '*[^0-9a-f]*');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_3 CHECK (length(event_id) = 38 AND substr(event_id, 1, 6) = 'flevt_' AND substr(event_id, 7) OPERATOR(public.!@@) '*[^0-9a-f]*');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_4 CHECK (entry_type IN ('usage_charge', 'manual_debit', 'manual_credit', 'reversal', 'gift_credit', 'trial_credit'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_5 CHECK (direction IN ('debit', 'credit'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_6 CHECK (typeof(amount_micros) = 'integer' AND amount_micros BETWEEN 0 AND 1000000000000000);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_7 CHECK (currency IN ('CNY', 'USD'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_8 CHECK (normalized_cny_micros IS NULL OR (typeof(normalized_cny_micros) = 'integer' AND normalized_cny_micros BETWEEN 0 AND 1000000000000000));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_9 CHECK (cny_normalization_status IN ('normalized', 'unavailable'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_10 CHECK (price_evidence_status IN ('priced', 'unpriced', 'legacy_amount_only', 'not_applicable'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_11 CHECK (posting_source IN ('direct', 'spool_replay', 'legacy_backfill'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_12 CHECK (quota_type IN ('paid', 'free', 'trial', 'gift'));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_13 CHECK (length(operation_request_id) BETWEEN 1 AND 128);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_14 CHECK (length(operation_trace_id) BETWEEN 1 AND 128);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_15 CHECK (typeof(input_tokens) = 'integer' AND input_tokens >= 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_16 CHECK (typeof(output_tokens) = 'integer' AND output_tokens >= 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_17 CHECK (typeof(total_tokens) = 'integer' AND total_tokens >= 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_18 CHECK (typeof(cache_tokens) = 'integer' AND cache_tokens >= 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_19 CHECK (estimated IN (0, 1));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_20 CHECK (status = 'posted');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_21 CHECK (created_at = updated_at);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_22 CHECK (entry_type = 'usage_charge' OR amount_micros > 0);
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_23 CHECK ((entry_type IN ('usage_charge', 'manual_debit') AND direction = 'debit') OR (entry_type IN ('manual_credit', 'gift_credit', 'trial_credit') AND direction = 'credit') OR entry_type = 'reversal');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_24 CHECK ((currency = 'CNY' AND price_evidence_status = 'unpriced' AND price_snapshot_source = 'response_cache_reference' AND price_schema_version = 'unpriced-v1' AND amount_micros = 0 AND cny_normalization_status = 'unavailable' AND normalized_cny_micros IS NULL) OR (currency = 'CNY' AND NOT(price_evidence_status = 'unpriced' AND price_snapshot_source = 'response_cache_reference' AND price_schema_version = 'unpriced-v1') AND cny_normalization_status = 'normalized' AND normalized_cny_micros = amount_micros) OR (currency = 'USD' AND cny_normalization_status = 'unavailable' AND normalized_cny_micros IS NULL));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_25 CHECK ((entry_type = 'usage_charge' AND usage_ledger_id IS NOT NULL AND finops_adjustment_id IS NULL AND actor_user_id IS NULL AND source_request_id IS NOT NULL AND source_trace_id IS NOT NULL) OR (entry_type <> 'usage_charge' AND usage_ledger_id IS NULL AND finops_adjustment_id IS NOT NULL AND actor_user_id IS NOT NULL));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_26 CHECK ((entry_type = 'reversal' AND reversal_of_entry_id IS NOT NULL) OR (entry_type <> 'reversal' AND reversal_of_entry_id IS NULL));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_27 CHECK ((entry_type = 'usage_charge' AND quota_type = 'paid') OR (entry_type IN ('manual_debit', 'manual_credit') AND quota_type = 'paid') OR (entry_type = 'gift_credit' AND quota_type = 'gift') OR (entry_type = 'trial_credit' AND quota_type = 'trial') OR entry_type = 'reversal');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_28 CHECK ((price_evidence_status = 'not_applicable' AND price_snapshot_id IS NULL AND price_schema_version IS NULL AND price_snapshot_source IS NULL) OR (price_evidence_status = 'unpriced' AND price_snapshot_id IS NOT NULL AND price_schema_version = 'unpriced-v1' AND price_snapshot_source IN ('finops_unpriced', 'response_cache_reference')) OR (price_evidence_status = 'legacy_amount_only' AND price_snapshot_id IS NOT NULL AND price_schema_version = 'legacy-unversioned-v1' AND price_snapshot_source = 'finops_legacy_backfill') OR (price_evidence_status = 'priced' AND price_snapshot_id IS NOT NULL AND NULLIF(TRIM(BOTH FROM price_schema_version), '') IS NOT NULL AND NULLIF(TRIM(BOTH FROM price_snapshot_source), '') IS NOT NULL AND price_snapshot_source NOT IN ('finops_unpriced', 'finops_legacy_backfill') AND NOT(price_snapshot_source = 'response_cache_reference' AND price_schema_version = 'unpriced-v1')));
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_29 CHECK (entry_type <> 'usage_charge' OR price_evidence_status <> 'not_applicable');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_30 CHECK (entry_type = 'usage_charge' OR entry_type = 'reversal' OR price_evidence_status = 'not_applicable');
ALTER TABLE billing_expected_shape ADD CONSTRAINT ck_finops_ledger_entries_31 CHECK (entry_type = 'usage_charge' OR (input_tokens = 0 AND output_tokens = 0 AND total_tokens = 0 AND cache_tokens = 0 AND estimated = 0));
CREATE UNIQUE INDEX billing_expected_index_0 ON billing_expected_shape (tenant_id, source_request_id, finops_ledger_entry_id);
CREATE UNIQUE INDEX billing_expected_index_1 ON billing_expected_shape (tenant_id, finops_adjustment_id);
CREATE UNIQUE INDEX billing_expected_index_2 ON billing_expected_shape (tenant_id, usage_ledger_id);
CREATE UNIQUE INDEX billing_expected_index_3 ON billing_expected_shape (tenant_id, event_id);
CREATE UNIQUE INDEX billing_expected_index_4 ON billing_expected_shape (tenant_id, finops_ledger_entry_id);
CREATE UNIQUE INDEX billing_expected_index_5 ON billing_expected_shape (event_id);
CREATE UNIQUE INDEX billing_expected_index_6 ON billing_expected_shape (finops_ledger_entry_id);
DO $shape$ BEGIN
  IF EXISTS(
    WITH columns AS (
      SELECT attrelid,row_number() OVER(PARTITION BY attrelid ORDER BY attnum) AS ordinal,
        attname,atttypid,atttypmod,attnotnull,attidentity,attgenerated,attcollation,
        pg_get_expr(value.adbin,value.adrelid) AS default_sql
      FROM pg_attribute attribute LEFT JOIN pg_attrdef value
        ON value.adrelid=attribute.attrelid AND value.adnum=attribute.attnum
      WHERE attrelid IN('finops_ledger_entries'::regclass,'billing_expected_shape'::regclass)
        AND attnum>0 AND NOT attisdropped
    ), actual AS (SELECT * FROM columns WHERE attrelid='finops_ledger_entries'::regclass),
    expected AS (SELECT * FROM columns WHERE attrelid='billing_expected_shape'::regclass)
    SELECT 1 FROM actual FULL JOIN expected USING(ordinal)
    WHERE ROW(actual.attname,actual.atttypid,actual.atttypmod,actual.attnotnull,
              actual.attidentity,actual.attgenerated,actual.attcollation,actual.default_sql)
      IS DISTINCT FROM ROW(expected.attname,expected.atttypid,expected.atttypmod,expected.attnotnull,
              expected.attidentity,expected.attgenerated,expected.attcollation,expected.default_sql)
  ) OR EXISTS(
    WITH actual AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='finops_ledger_entries'::regclass AND contype<>'f'),
    expected AS (SELECT pg_get_constraintdef(oid) AS sql,convalidated
        FROM pg_constraint WHERE conrelid='billing_expected_shape'::regclass AND contype<>'f')
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) OR EXISTS(
    WITH expected(parent,child_columns,parent_columns,match,upd,del,deferred,initially_deferred,validated) AS (VALUES ('applications',ARRAY['tenant_id','application_id']::text[],ARRAY['tenant_id','application_id']::text[],'s','a','a',false,false,true),('projects',ARRAY['tenant_id','project_id']::text[],ARRAY['tenant_id','project_id']::text[],'s','a','a',false,false,true),('environments',ARRAY['tenant_id','environment_id']::text[],ARRAY['tenant_id','environment_id']::text[],'s','a','a',false,false,true),('virtual_keys',ARRAY['tenant_id','virtual_key_id']::text[],ARRAY['tenant_id','virtual_key_id']::text[],'s','a','a',false,false,true),('providers',ARRAY['tenant_id','provider_id']::text[],ARRAY['tenant_id','provider_id']::text[],'s','a','a',false,false,true),('models',ARRAY['tenant_id','model_id']::text[],ARRAY['tenant_id','model_id']::text[],'s','a','a',false,false,true),('users',ARRAY['tenant_id','actor_user_id']::text[],ARRAY['tenant_id','user_id']::text[],'s','a','a',false,false,true),('finops_ledger_entries',ARRAY['tenant_id','reversal_of_entry_id']::text[],ARRAY['tenant_id','finops_ledger_entry_id']::text[],'s','a','a',false,false,true),('price_snapshots',ARRAY['tenant_id','price_snapshot_id']::text[],ARRAY['tenant_id','price_snapshot_id']::text[],'s','a','a',false,false,true),('request_logs',ARRAY['tenant_id','source_request_id']::text[],ARRAY['tenant_id','request_id']::text[],'s','a','a',false,false,true),('finops_adjustments',ARRAY['tenant_id','finops_adjustment_id']::text[],ARRAY['tenant_id','finops_adjustment_id']::text[],'s','a','a',false,false,true),('usage_ledger',ARRAY['tenant_id','usage_ledger_id']::text[],ARRAY['tenant_id','usage_ledger_id']::text[],'s','a','a',false,false,true),('tenants',ARRAY['tenant_id']::text[],ARRAY['tenant_id']::text[],'s','a','a',false,false,true)),
    actual AS (
      SELECT parent.relname::text,
        ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=key.num ORDER BY key.ordinal),
        ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY key(num,ordinal)
          JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=key.num ORDER BY key.ordinal),
        c.confmatchtype::text,c.confupdtype::text,c.confdeltype::text,c.condeferrable,c.condeferred,c.convalidated
      FROM pg_constraint c JOIN pg_class parent ON parent.oid=c.confrelid
      WHERE c.conrelid='finops_ledger_entries'::regclass AND c.contype='f'
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
        AND i.indrelid IN('finops_ledger_entries'::regclass,'billing_expected_shape'::regclass)
    ), actual AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='finops_ledger_entries'::regclass),
    expected AS (SELECT indisvalid,indisready,indimmediate,indnkeyatts,columns,predicate,expression
       FROM indexes WHERE indrelid='billing_expected_shape'::regclass)
    (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_table_drift'; END IF;
END; $shape$;
DROP TABLE billing_expected_shape;
DO $triggers$ BEGIN
  IF EXISTS(
    WITH expected(name,function,bits,sha256) AS (VALUES ('trg_finops_ledger_owner_insert_prd051','fn_trg_finops_ledger_owner_insert_prd051',7,'7d6579606fb27b0dd8a049ea4908b0590171353f46cb54e4e50f722af0716b84'),
('trg_finops_ledger_conflict_insert_prd051','fn_trg_finops_ledger_conflict_insert_prd051',7,'111fa296a5965cb720db541d7c8e812fb17432cc191b7f2a78d067660ab6bd89'),
('trg_finops_ledger_immutable_update_prd051','fn_trg_finops_ledger_immutable_update_prd051',19,'7bf116e79507f4052fc71e4b5223d4041c055915421987bc5838836e56e14105'),
('trg_finops_ledger_immutable_delete_prd051','fn_trg_finops_ledger_immutable_delete_prd051',11,'75ebcdbb8fe4d6c94810f6b4c4a5bcd5449b3f0a4463c8bfc89768de46d28017'),
('trg_finops_experiment_settlement_prd075','fn_trg_finops_experiment_settlement_prd075',5,'ee7b3b4e9495ee89bd6fa1aa74b064382cb06e81abdf4aebcea719abae0f00eb')), actual AS (
      SELECT t.tgname,t.tgenabled,t.tgtype,t.tgnargs,t.tgqual,t.tgattr,t.tgconstraint,
        p.proname,p.prosrc,p.prosecdef,p.proconfig,p.proisstrict,p.proleakproof,
        p.provolatile,p.proparallel,p.pronamespace,p.prorettype,p.prolang
      FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
      WHERE t.tgrelid='finops_ledger_entries'::regclass AND NOT t.tgisinternal
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
  IF (SELECT count(*) FROM tenant_deletion_table_manifest WHERE source_table='finops_ledger_entries')<>1
    OR EXISTS(SELECT 1 FROM tenant_deletion_table_manifest m CROSS JOIN (WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute LEFT JOIN pg_attrdef default_value
    ON default_value.adrelid=attribute.attrelid AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='finops_ledger_entries'::regclass AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='finops_ledger_entries'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='finops_ledger_entries'::regclass AND NOT tgisinternal
)
SELECT metahub_sha256('CREATE TABLE finops_ledger_entries ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')') AS table_sha,
  metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum)
    FROM columns)) AS columns_sha,
  metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),'')) AS triggers_sha
) actual
      WHERE m.source_table='finops_ledger_entries' AND (
        m.table_sql_sha256 NOT IN('988e0676e99eab46a990b6096f3a79283feb0109415586fc94e9c1c8610007f5',actual.table_sha)
        OR m.column_signature_sha256 NOT IN('9c5228ad27a6b32d0a973400b241f80816df9886765c25ac966ac5733f43c736',actual.columns_sha)
        OR m.trigger_set_sha256 IS DISTINCT FROM (SELECT metahub_sha256(coalesce(
          string_agg(trigger_name||chr(31)||trigger_sha256,chr(30) ORDER BY trigger_name),''))
          FROM tenant_deletion_trigger_manifest WHERE source_table='finops_ledger_entries')))
    OR EXISTS(
      WITH legacy(name,sha256) AS (VALUES ('trg_finops_ledger_owner_insert_prd051','da68bd0730d329d1374191a616c123e97f68c3b4fab68626599a93b1dcd0e46f'),('trg_finops_ledger_conflict_insert_prd051','089945211531457ae3b34f194d62a6f3d99966487fc1193c58ccdcad2f8b876f'),('trg_finops_ledger_immutable_update_prd051','20dea5b90bc529690316ef9c888b4edd8214d1fd21ac09d23889cf72c1ad43c0'),('trg_finops_ledger_immutable_delete_prd051','55f5f707204fa37019c884b7b68faff469e6c053b0ab44ee4af63ad904f9a7b3'),('trg_finops_experiment_settlement_prd075','825f63cdbd0f8522f373d7ba77e25a573005f52b05e7faf4e9fbe251a5082956')), actual AS (
        SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
        FROM pg_trigger WHERE tgrelid='finops_ledger_entries'::regclass AND NOT tgisinternal
      ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest WHERE source_table='finops_ledger_entries')
      SELECT 1 FROM actual FULL JOIN recorded ON tgname=trigger_name LEFT JOIN legacy ON name=trigger_name
      WHERE tgname IS NULL OR trigger_name IS NULL OR metahub_sha256(trigger_sql)<>trigger_sha256
        OR (trigger_sql IS DISTINCT FROM sql AND trigger_sha256 IS DISTINCT FROM sha256)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_billing_manifest_drift'; END IF;
END; $manifest$;
