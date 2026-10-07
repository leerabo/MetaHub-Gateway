-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
PRAGMA writable_schema = ON;

UPDATE sqlite_master
SET sql = replace(sql, 'attempt_sequence BETWEEN 1 AND 32', 'attempt_sequence IN (1, 2)')
WHERE type='table'
  AND name IN ('routing_attempts','cache_affinity_selections');

UPDATE sqlite_master
SET sql = replace(sql, 'attempt_sequence BETWEEN 1 AND 32', 'attempt_sequence IN(1,2)')
WHERE type='table' AND name='local_model_call_events';

UPDATE sqlite_master
SET sql = replace(sql, 'NEW.attempt_sequence<1', 'NEW.attempt_sequence!=1')
WHERE type='trigger' AND name='trg_cache_affinity_selections_prd050_graph_insert';

PRAGMA writable_schema = OFF;
PRAGMA integrity_check;
COMMIT;
PRAGMA foreign_keys = ON;
