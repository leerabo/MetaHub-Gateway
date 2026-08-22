-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd100_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd100_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0092' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM migration_leases)
  + (SELECT COUNT(*) FROM backup_sets)
  + (SELECT COUNT(*) FROM backup_restore_verifications)
  + (SELECT COUNT(*) FROM infrastructure_event_outbox)
  + CASE WHEN (SELECT COUNT(*) FROM infrastructure_resources)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_infrastructure_permission_extensions)=4
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_infrastructure_role_permissions)=9
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd100_down_guard;

DROP TABLE infrastructure_event_outbox;
DROP TRIGGER trg_backup_restore_verifications_delete_prd100;
DROP TRIGGER trg_backup_verification_owner_update_prd100;
DROP TABLE backup_restore_verifications;
DROP TRIGGER trg_backup_sets_delete_prd100;
DROP TRIGGER trg_backup_set_owner_update_prd100;
DROP TABLE backup_sets;
DROP TRIGGER trg_migration_leases_delete_prd100;
DROP TRIGGER trg_migration_lease_owner_update_prd100;
DROP TABLE migration_leases;
DROP TRIGGER trg_infrastructure_resources_delete_prd100;
DROP TRIGGER trg_infrastructure_resource_owner_update_prd100;
DROP TABLE infrastructure_resources;
DROP TRIGGER trg_platform_infrastructure_role_permissions_delete_prd100;
DROP TRIGGER trg_platform_infrastructure_role_permissions_update_prd100;
DROP TRIGGER trg_platform_infrastructure_role_permissions_insert_prd100;
DROP TRIGGER trg_platform_infrastructure_permissions_delete_prd100;
DROP TRIGGER trg_platform_infrastructure_permissions_update_prd100;
DROP TRIGGER trg_platform_infrastructure_permissions_insert_prd100;
DROP TABLE platform_infrastructure_role_permissions;
DROP TABLE platform_infrastructure_permission_extensions;

CREATE TEMP TABLE prd100_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd100_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd100_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
