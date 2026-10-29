-- metahub: stream-retry-attempt-sequences-v1
-- The routing plan is bounded at 32 total models (one initial plus up to 31 fallbacks).
-- Historical SQLite tables encoded the original two-attempt contract in CHECK clauses.

BEGIN IMMEDIATE;
PRAGMA writable_schema = ON;

UPDATE sqlite_master
SET sql = replace(sql, 'attempt_sequence IN (1, 2)', 'attempt_sequence BETWEEN 1 AND 32')
WHERE type='table'
  AND name IN ('routing_attempts','cache_affinity_selections');

UPDATE sqlite_master
SET sql = replace(sql, 'attempt_sequence IN(1,2)', 'attempt_sequence BETWEEN 1 AND 32')
WHERE type='table' AND name='local_model_call_events';

-- Stream cache-affinity selections may bind to every handshake attempt. The first
-- attempt still has the starting-session owner check; later attempts use their own
-- starting StreamSession and are validated by the same graph predicate.
UPDATE sqlite_master
SET sql = replace(sql, 'NEW.attempt_sequence!=1', 'NEW.attempt_sequence<1')
WHERE type='trigger' AND name='trg_cache_affinity_selections_prd050_graph_insert';

PRAGMA writable_schema = OFF;
PRAGMA integrity_check;
COMMIT;
