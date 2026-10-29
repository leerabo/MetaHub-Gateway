-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- PRD-140 contracts legacy health facts into a single platform-owned truth. Rollback is
-- therefore schema-only and is permitted only before any owner-qualified backfill exists.
CREATE TEMP TABLE prd140_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd140_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
      ORDER BY version DESC LIMIT 1)='0132' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM platform_customer_account_projections)
  + (SELECT COUNT(*) FROM platform_customer_contacts)
  + (SELECT COUNT(*) FROM platform_customer_health_snapshots)
  + (SELECT COUNT(*) FROM platform_customer_health_heads)
  + (SELECT COUNT(*) FROM platform_customer_success_plans)
  + (SELECT COUNT(*) FROM platform_renewal_opportunities)
  + (SELECT COUNT(*) FROM platform_customer_tasks)
  + (SELECT COUNT(*) FROM platform_qbr_summaries)
  + (SELECT COUNT(*) FROM platform_customer_task_batches)
  + (SELECT COUNT(*) FROM platform_customer_task_batch_approvals)
  + (SELECT COUNT(*) FROM platform_customer_success_commands)
  + (SELECT COUNT(*) FROM platform_customer_success_legacy_mappings)
  + (SELECT COUNT(*) FROM platform_customer_success_events)
  + (SELECT COUNT(*) FROM platform_customer_success_event_outbox)
  + (SELECT COUNT(*) FROM platform_customer_success_scheduler_state)
  + CASE WHEN (SELECT COUNT(*) FROM platform_customer_success_migration_cursors)=4
      AND NOT EXISTS(SELECT 1 FROM platform_customer_success_migration_cursors
        WHERE source_row_count!=0 OR target_row_count!=0 OR max_source_sequence!=0
          OR pending_count!=0 OR leased_count!=0 OR sent_count!=0 OR failed_count!=0
          OR webhook_delivery_count!=0 OR webhook_dead_letter_count!=0)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_customer_success_permission_extensions)=8
      AND (SELECT COUNT(*) FROM platform_customer_success_role_permissions)=16
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd140)=13
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd140_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd140_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd140_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd140_delete;
CREATE TEMP TABLE prd140_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd140;
DROP TABLE tenant_deletion_manifest_extensions_prd140;
DELETE FROM tenant_deletion_trigger_manifest;
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd140_manifest_sources);
DROP TABLE prd140_manifest_sources;

DROP TRIGGER trg_legacy_workspace_health_delete_prd140;
DROP TRIGGER trg_legacy_workspace_health_update_prd140;
DROP TRIGGER trg_legacy_workspace_health_write_prd140;
DROP TRIGGER trg_legacy_health_scheduler_delete_prd140;
DROP TRIGGER trg_legacy_health_scheduler_update_prd140;
DROP TRIGGER trg_legacy_health_scheduler_write_prd140;
DROP TRIGGER trg_legacy_health_outbox_delete_prd140;
DROP TRIGGER trg_legacy_health_outbox_update_prd140;
DROP TRIGGER trg_legacy_health_outbox_write_prd140;
DROP TRIGGER trg_legacy_health_scores_delete_prd140;
DROP TRIGGER trg_legacy_health_scores_update_prd140;
DROP TRIGGER trg_legacy_health_scores_write_prd140;

DROP TRIGGER trg_platform_customer_scheduler_delete_prd140;
DROP TRIGGER trg_platform_customer_scheduler_update_prd140;
DROP TABLE platform_customer_success_scheduler_state;
DROP TRIGGER trg_platform_customer_outbox_delete_prd140;
DROP TABLE platform_customer_success_event_outbox;
DROP TRIGGER trg_platform_customer_event_delete_prd140;
DROP TRIGGER trg_platform_customer_event_update_prd140;
DROP TABLE platform_customer_success_events;
DROP TRIGGER trg_platform_customer_cursor_delete_prd140;
DROP TRIGGER trg_platform_customer_cursor_update_prd140;
DROP TABLE platform_customer_success_migration_cursors;
DROP TRIGGER trg_platform_customer_mapping_delete_prd140;
DROP TRIGGER trg_platform_customer_mapping_update_prd140;
DROP TABLE platform_customer_success_legacy_mappings;
DROP TRIGGER trg_platform_customer_command_delete_prd140;
DROP TRIGGER trg_platform_customer_command_update_prd140;
DROP TABLE platform_customer_success_commands;
DROP TRIGGER trg_platform_customer_batch_approval_delete_prd140;
DROP TRIGGER trg_platform_customer_batch_approval_update_prd140;
DROP TABLE platform_customer_task_batch_approvals;
DROP TRIGGER trg_platform_customer_batch_delete_prd140;
DROP TRIGGER trg_platform_customer_batch_update_prd140;
DROP TABLE platform_customer_task_batches;
DROP TRIGGER trg_platform_qbr_delete_prd140;
DROP TRIGGER trg_platform_qbr_update_prd140;
DROP TABLE platform_qbr_summaries;
DROP TRIGGER trg_platform_customer_task_delete_prd140;
DROP TRIGGER trg_platform_customer_task_update_prd140;
DROP TABLE platform_customer_tasks;
DROP TRIGGER trg_platform_renewal_delete_prd140;
DROP TRIGGER trg_platform_renewal_update_prd140;
DROP TABLE platform_renewal_opportunities;
DROP TRIGGER trg_platform_customer_plan_delete_prd140;
DROP TRIGGER trg_platform_customer_plan_update_prd140;
DROP TABLE platform_customer_success_plans;
DROP TRIGGER trg_platform_customer_contact_delete_prd140;
DROP TRIGGER trg_platform_customer_contact_update_prd140;
DROP TRIGGER trg_platform_customer_account_delete_prd140;
DROP TRIGGER trg_platform_customer_account_owner_update_prd140;
DROP TRIGGER trg_platform_customer_health_head_delete_prd140;
DROP TRIGGER trg_platform_customer_health_head_update_prd140;
DROP TABLE platform_customer_health_heads;
DROP TRIGGER trg_platform_customer_health_delete_prd140;
DROP TRIGGER trg_platform_customer_health_update_prd140;
DROP TABLE platform_customer_health_snapshots;
DROP TABLE platform_customer_contacts;
DROP TABLE platform_customer_account_projections;
DROP TRIGGER trg_platform_customer_roles_delete_prd140;
DROP TRIGGER trg_platform_customer_roles_update_prd140;
DROP TRIGGER trg_platform_customer_roles_insert_prd140;
DROP TRIGGER trg_platform_customer_permissions_delete_prd140;
DROP TRIGGER trg_platform_customer_permissions_update_prd140;
DROP TRIGGER trg_platform_customer_permissions_insert_prd140;
DROP TABLE platform_customer_success_role_permissions;
DROP TABLE platform_customer_success_permission_extensions;

INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd140_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd140_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_customer_success_permission_extensions',
      'platform_customer_success_role_permissions',
      'platform_customer_account_projections','platform_customer_contacts',
      'platform_customer_health_snapshots','platform_customer_health_heads',
      'platform_customer_success_plans','platform_renewal_opportunities',
      'platform_customer_tasks','platform_qbr_summaries',
      'platform_customer_task_batches','platform_customer_task_batch_approvals',
      'platform_customer_success_commands','platform_customer_success_legacy_mappings',
      'platform_customer_success_migration_cursors','platform_customer_success_events',
      'platform_customer_success_event_outbox',
      'platform_customer_success_scheduler_state',
      'tenant_deletion_manifest_extensions_prd140'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd140_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
