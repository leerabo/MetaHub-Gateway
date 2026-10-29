-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd147_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd147_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0139' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM console_domain_migrations)
  + (SELECT COUNT(*) FROM console_domain_migration_heads)
  + (SELECT COUNT(*) FROM platform_console_domain_migrations)
  + (SELECT COUNT(*) FROM console_backfill_cursors)
  + (SELECT COUNT(*) FROM console_backfill_checkpoints)
  + (SELECT COUNT(*) FROM console_backfill_attempts)
  + (SELECT COUNT(*) FROM workspace_domain_reconciliations)
  + (SELECT COUNT(*) FROM workspace_domain_reconciliation_checks)
  + (SELECT COUNT(*) FROM platform_domain_reconciliations)
  + (SELECT COUNT(*) FROM platform_domain_reconciliation_checks)
  + (SELECT COUNT(*) FROM console_contract_releases)
  + (SELECT COUNT(*) FROM console_contract_release_items)
  + (SELECT COUNT(*) FROM console_contract_release_heads)
  + (SELECT COUNT(*) FROM workspace_legacy_route_tombstones)
  + (SELECT COUNT(*) FROM platform_legacy_route_retirements)
  + (SELECT COUNT(*) FROM platform_console_migration_batches)
  + (SELECT COUNT(*) FROM platform_console_migration_batch_items)
  + (SELECT COUNT(*) FROM platform_console_migration_commands)
  + (SELECT COUNT(*) FROM platform_console_migration_approvals)
  + (SELECT COUNT(*) FROM platform_console_migration_security_reviews)
  + (SELECT COUNT(*) FROM platform_console_migration_events)
  + (SELECT COUNT(*) FROM platform_console_migration_event_outbox)
  + (SELECT COUNT(*) FROM platform_console_migration_audits)
  + (SELECT COUNT(*) FROM scheduler_leases)
  + (SELECT COUNT(*) FROM scheduler_watermarks)
  + (SELECT COUNT(*) FROM runtime_surface_observations)
  + (SELECT COUNT(*) FROM platform_jobs WHERE type_code='console.domain.migrate')
  + CASE WHEN (SELECT COUNT(*) FROM platform_console_migration_permission_extensions)=5
      AND (SELECT COUNT(*) FROM platform_console_migration_role_permissions)=7 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM legacy_route_catalog WHERE status='planned')=46 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM scheduler_registry_entries
      WHERE enabled=1 AND seed_entry=1 AND revision=1)=37 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM scheduler_baseline_mappings)=41 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM scheduler_queue_contracts WHERE enabled=1 AND revision=1)=64 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM runtime_surface_manifest WHERE revision=1)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM scheduler_registry_heads
      WHERE head_key='production' AND generation=1 AND revision=1)=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM console_ingress_contract_heads
      WHERE head_key='production' AND route_deny_epoch=1 AND contract_generation=0
        AND legacy_write_fenced=0 AND status='expanded' AND revision=1)=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_types
      WHERE type_code='console.domain.migrate' AND contract_version=1
        AND artifact_sha256='3dc5425d407ced943e122242ff502637ef7aad664478b1f79cb297e4a5622e9a'
        AND queue_code='bulk' AND resource_class='io' AND max_attempts=10
        AND timeout_seconds=18000 AND status='active' AND revision=1)=1
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd147_down_guard;

DROP TRIGGER trg_console_backfill_checkpoint_delete_prd147;
DROP TRIGGER trg_console_backfill_checkpoint_update_prd147;
DROP TRIGGER trg_console_migration_audit_delete_prd147;
DROP TRIGGER trg_console_migration_audit_update_prd147;
DROP TRIGGER trg_console_migration_event_delete_prd147;
DROP TRIGGER trg_console_migration_event_update_prd147;
DROP TRIGGER trg_scheduler_watermark_advance_prd147;
DROP TRIGGER trg_scheduler_watermark_fence_prd147;
DROP TRIGGER trg_platform_route_retirement_state_prd147;
DROP TRIGGER trg_workspace_route_tombstone_state_prd147;
DROP TRIGGER trg_console_migration_state_prd147;
DROP TRIGGER trg_runtime_surface_delete_prd147;
DROP TRIGGER trg_runtime_surface_update_prd147;
DROP TRIGGER trg_runtime_surface_insert_prd147;
DROP TRIGGER trg_scheduler_queue_delete_prd147;
DROP TRIGGER trg_scheduler_queue_update_prd147;
DROP TRIGGER trg_scheduler_queue_insert_prd147;
DROP TRIGGER trg_scheduler_mapping_delete_prd147;
DROP TRIGGER trg_scheduler_mapping_update_prd147;
DROP TRIGGER trg_scheduler_mapping_insert_prd147;
DROP TRIGGER trg_scheduler_registry_delete_prd147;
DROP TRIGGER trg_scheduler_registry_update_prd147;
DROP TRIGGER trg_scheduler_registry_insert_prd147;
DROP TRIGGER trg_legacy_route_catalog_delete_prd147;
DROP TRIGGER trg_legacy_route_catalog_update_prd147;
DROP TRIGGER trg_legacy_route_catalog_insert_prd147;
DROP TRIGGER trg_platform_console_migration_role_delete_prd147;
DROP TRIGGER trg_platform_console_migration_role_update_prd147;
DROP TRIGGER trg_platform_console_migration_role_insert_prd147;
DROP TRIGGER trg_platform_console_migration_permission_delete_prd147;
DROP TRIGGER trg_platform_console_migration_permission_update_prd147;
DROP TRIGGER trg_platform_console_migration_permission_insert_prd147;

DROP TABLE console_ingress_contract_heads;
DROP TABLE runtime_surface_observations;
DROP TABLE runtime_surface_manifest;
DROP TABLE scheduler_watermarks;
DROP TABLE scheduler_leases;
DROP TABLE scheduler_queue_contracts;
DROP TABLE scheduler_baseline_mappings;
DROP TABLE scheduler_registry_heads;
DROP TABLE scheduler_registry_entries;
DROP TABLE platform_console_migration_audits;
DROP TABLE platform_console_migration_event_outbox;
DROP TABLE platform_console_migration_events;
DROP TABLE platform_console_migration_security_reviews;
DROP TABLE platform_console_migration_approvals;
DROP TABLE platform_console_migration_commands;
DROP TABLE platform_console_migration_batch_items;
DROP TABLE platform_console_migration_batches;
DROP TABLE platform_legacy_route_retirements;
DROP TABLE workspace_legacy_route_tombstones;
DROP TABLE legacy_route_catalog;
DROP TABLE console_contract_release_heads;
DROP TABLE console_contract_release_items;
DROP TABLE console_contract_releases;
DROP TABLE platform_domain_reconciliation_checks;
DROP TABLE platform_domain_reconciliations;
DROP TABLE workspace_domain_reconciliation_checks;
DROP TABLE workspace_domain_reconciliations;
DROP TABLE console_backfill_attempts;
DROP TABLE console_backfill_checkpoints;
DROP TABLE console_backfill_cursors;
DROP TABLE platform_console_domain_migrations;
DROP TABLE console_domain_migration_heads;
DROP TABLE console_domain_migrations;
DROP TABLE platform_console_migration_role_permissions;
DROP TABLE platform_console_migration_permission_extensions;

DROP TRIGGER trg_platform_job_types_delete_prd106;
DELETE FROM platform_job_types WHERE type_code='console.domain.migrate';
CREATE TRIGGER trg_platform_job_types_delete_prd106 BEFORE DELETE ON platform_job_types
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd147_down_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd147_down_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'platform_console_migration_permission_extensions',
    'platform_console_migration_role_permissions','console_domain_migrations',
    'console_domain_migration_heads','platform_console_domain_migrations',
    'console_backfill_cursors','console_backfill_checkpoints','console_backfill_attempts',
    'workspace_domain_reconciliations','workspace_domain_reconciliation_checks',
    'platform_domain_reconciliations','platform_domain_reconciliation_checks',
    'console_contract_releases','console_contract_release_items','console_contract_release_heads',
    'legacy_route_catalog','workspace_legacy_route_tombstones',
    'platform_legacy_route_retirements','platform_console_migration_batches',
    'platform_console_migration_batch_items',
    'platform_console_migration_commands','platform_console_migration_approvals',
    'platform_console_migration_security_reviews','platform_console_migration_events',
    'platform_console_migration_event_outbox','platform_console_migration_audits',
    'scheduler_registry_entries','scheduler_registry_heads','scheduler_baseline_mappings',
    'scheduler_queue_contracts','scheduler_leases','scheduler_watermarks',
    'runtime_surface_manifest','runtime_surface_observations','console_ingress_contract_heads'))
  + (SELECT COUNT(*) FROM platform_job_types WHERE type_code='console.domain.migrate');
DROP TABLE prd147_down_post_guard;

COMMIT;
PRAGMA foreign_keys = ON;
