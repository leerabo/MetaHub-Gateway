-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE dr_binding_down_guard(facts INTEGER NOT NULL CHECK(facts=0));
INSERT INTO dr_binding_down_guard SELECT COUNT(*) FROM dr_run_recovery_bindings;
DROP TABLE dr_binding_down_guard;
DROP TRIGGER trg_dr_run_binding_update;
DROP TRIGGER trg_dr_run_binding_insert;
DROP TABLE dr_run_recovery_bindings;
COMMIT;
PRAGMA foreign_keys = ON;
