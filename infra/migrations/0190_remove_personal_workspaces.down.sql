-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
-- Personal workspace data is intentionally purged and cannot be reconstructed.
CREATE TEMP TABLE personal_removal_rollback_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO personal_removal_rollback_guard VALUES(1);
DROP TABLE personal_removal_rollback_guard;
COMMIT;
PRAGMA foreign_keys = ON;
