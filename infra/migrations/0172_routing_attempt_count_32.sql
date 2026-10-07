-- metahub: routing-attempt-count-32-v1
-- Request logs and feedback/output-safety graph checks must accept one initial
-- attempt plus up to 31 ordered fallback attempts.

BEGIN IMMEDIATE;
PRAGMA writable_schema = ON;

UPDATE sqlite_master
SET sql = replace(sql, 'routing_attempt_count BETWEEN 0 AND 2',
                  'routing_attempt_count BETWEEN 0 AND 32')
WHERE type='table' AND name='request_logs';

UPDATE sqlite_master
SET sql = replace(sql, 'routing_attempt_count IN (1,2)',
                  'routing_attempt_count BETWEEN 1 AND 32')
WHERE type='trigger' AND sql LIKE '%routing_attempt_count IN (1,2)%';

UPDATE sqlite_master
SET sql = replace(sql, 'routing_attempt_count IN (1, 2)',
                  'routing_attempt_count BETWEEN 1 AND 32')
WHERE type='trigger' AND sql LIKE '%routing_attempt_count IN (1, 2)%';

PRAGMA writable_schema = OFF;
PRAGMA integrity_check;
COMMIT;
