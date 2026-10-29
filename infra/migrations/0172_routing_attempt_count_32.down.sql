-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
PRAGMA writable_schema = ON;

UPDATE sqlite_master
SET sql = replace(sql, 'routing_attempt_count BETWEEN 0 AND 32',
                  'routing_attempt_count BETWEEN 0 AND 2')
WHERE type='table' AND name='request_logs';

UPDATE sqlite_master
SET sql = replace(sql, 'routing_attempt_count BETWEEN 1 AND 32',
                  'routing_attempt_count IN (1,2)')
WHERE type='trigger' AND sql LIKE '%routing_attempt_count BETWEEN 1 AND 32%';

PRAGMA writable_schema = OFF;
PRAGMA integrity_check;
PRAGMA foreign_key_check;
COMMIT;
PRAGMA foreign_keys = ON;
