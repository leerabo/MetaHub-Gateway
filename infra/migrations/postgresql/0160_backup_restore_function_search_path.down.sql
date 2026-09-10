DO $migration$
DECLARE helper RECORD; previous_path TEXT := current_setting('search_path');
BEGIN
  IF EXISTS(SELECT 1 FROM public.backup_sets) THEN
    RAISE EXCEPTION 'backup_facts_prevent_restore_helper_down';
  END IF;
  FOR helper IN SELECT * FROM metahub_bootstrap.restore_function_settings LOOP
    IF helper.original_search_path IS NULL THEN
      EXECUTE format('ALTER FUNCTION %s RESET search_path',helper.function_signature);
    ELSE
      PERFORM pg_catalog.set_config('search_path',helper.original_search_path,true);
      EXECUTE format('ALTER FUNCTION %s SET search_path FROM CURRENT',helper.function_signature);
    END IF;
  END LOOP;
  PERFORM pg_catalog.set_config('search_path',previous_path,true);
END $migration$;
DROP TABLE metahub_bootstrap.restore_function_settings;
