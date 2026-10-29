-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE platform_external_down_guard(n INTEGER CHECK(n=0));
INSERT INTO platform_external_down_guard SELECT COUNT(*) FROM platform_job_external_waits;
INSERT INTO platform_external_down_guard SELECT COUNT(*) FROM platform_job_resumption_leases;
DROP TABLE platform_external_down_guard;
DROP TRIGGER trg_platform_job_external_terminal_guard;
DROP TABLE platform_job_resumption_leases;
DROP TABLE platform_job_external_waits;
COMMIT;
PRAGMA foreign_keys = ON;
