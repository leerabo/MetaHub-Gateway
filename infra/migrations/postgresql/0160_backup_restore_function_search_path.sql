-- Keep CHECK/default helper calls valid under pg_restore's empty search_path.
CREATE TABLE metahub_bootstrap.restore_function_settings(
  function_signature TEXT PRIMARY KEY,
  original_search_path TEXT
);

DO $migration$
DECLARE helper RECORD; original_path TEXT;
BEGIN
  FOR helper IN
    SELECT p.oid,format('%I.%I(%s)',n.nspname,p.proname,
      pg_catalog.pg_get_function_identity_arguments(p.oid)) signature
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.prokind='f' AND p.proname=ANY(ARRAY[
      'metahub_utc_now','metahub_sha256','randomblob','hex','json_quote',
      'metahub_password_hash_algorithm','typeof','json_valid','metahub_json_value',
      'json_extract','json_type','json_array_length','metahub_json_scalar',
      'metahub_json_type','metahub_json_atom','json_each','json_tree',
      'metahub_json_group_array_sfunc','metahub_jsonb_text','metahub_sum_boolean_sfunc',
      'metahub_boolean_equal_integer','json_remove','metahub_json','metahub_glob','glob',
      'metahub_not_glob','metahub_timestamp','unixepoch','julianday','strftime','date',
      'datetime','instr','substr','chr','printf'
    ])
    ORDER BY p.oid::regprocedure::text
  LOOP
    SELECT substring(setting FROM length('search_path=')+1) INTO original_path
      FROM pg_catalog.pg_proc p,LATERAL unnest(p.proconfig) setting
      WHERE p.oid=helper.oid AND setting LIKE 'search_path=%';
    INSERT INTO metahub_bootstrap.restore_function_settings VALUES(helper.signature,original_path);
    EXECUTE format('ALTER FUNCTION %s SET search_path TO pg_catalog, public, pg_temp',helper.signature);
  END LOOP;
  IF (SELECT COUNT(*) FROM metahub_bootstrap.restore_function_settings)!=47 THEN
    RAISE EXCEPTION 'backup_restore_helper_catalog_mismatch';
  END IF;
END $migration$;
