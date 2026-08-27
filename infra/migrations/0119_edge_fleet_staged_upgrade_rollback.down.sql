-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd127_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd127_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0119' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_gateway_releases)
  + (SELECT COUNT(*) FROM edge_gateway_release_artifacts)
  + (SELECT COUNT(*) FROM edge_maintenance_windows)
  + (SELECT COUNT(*) FROM edge_deployment_release_heads
      WHERE current_release_id IS NOT NULL OR lkg_release_id IS NOT NULL
        OR desired_generation!=0 OR applied_generation!=0 OR lkg_generation!=0
        OR revision!=1)
  + (SELECT COUNT(*) FROM edge_fleet_rollouts)
  + (SELECT COUNT(*) FROM edge_rollout_batches)
  + (SELECT COUNT(*) FROM edge_rollout_targets)
  + (SELECT COUNT(*) FROM edge_upgrade_commands)
  + (SELECT COUNT(*) FROM edge_upgrade_receipts)
  + (SELECT COUNT(*) FROM edge_rollback_attempts)
  + (SELECT COUNT(*) FROM edge_fleet_upgrade_events)
  + (SELECT COUNT(*) FROM edge_fleet_upgrade_outbox)
  + (SELECT COUNT(*) FROM edge_upgrade_action_commands)
  + (SELECT COUNT(*) FROM platform_jobs WHERE type_code='fleet.rollout.advance')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd127_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd127_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd127_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd127_delete;
CREATE TEMP TABLE prd127_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd127;
DROP TABLE tenant_deletion_manifest_extensions_prd127;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd127_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd127_manifest_sources);
DROP TABLE prd127_manifest_sources;

DROP TRIGGER trg_edge_upgrade_actions_delete_prd127;
DROP TRIGGER trg_edge_upgrade_actions_update_prd127;
DROP TABLE edge_upgrade_action_commands;
DROP TRIGGER trg_edge_fleet_upgrade_outbox_delete_prd127;
DROP TRIGGER trg_edge_fleet_upgrade_outbox_update_prd127;
DROP INDEX idx_edge_fleet_upgrade_outbox_prd127;
DROP TABLE edge_fleet_upgrade_outbox;
DROP TRIGGER trg_edge_fleet_upgrade_events_delete_prd127;
DROP TRIGGER trg_edge_fleet_upgrade_events_update_prd127;
DROP INDEX idx_edge_fleet_upgrade_events_prd127;
DROP TABLE edge_fleet_upgrade_events;
DROP TRIGGER trg_edge_rollback_attempts_delete_prd127;
DROP TRIGGER trg_edge_rollback_attempts_update_prd127;
DROP INDEX idx_edge_rollback_attempts_owner_prd127;
DROP TABLE edge_rollback_attempts;
DROP TRIGGER trg_edge_upgrade_receipts_delete_prd127;
DROP TRIGGER trg_edge_upgrade_receipts_update_prd127;
DROP TABLE edge_upgrade_receipts;
DROP TRIGGER trg_edge_upgrade_commands_delete_prd127;
DROP TRIGGER trg_edge_upgrade_commands_update_prd127;
DROP INDEX idx_edge_upgrade_commands_deadline_prd127;
DROP INDEX idx_edge_upgrade_commands_agent_prd127;
DROP TABLE edge_upgrade_commands;
DROP TRIGGER trg_edge_rollout_targets_delete_prd127;
DROP TRIGGER trg_edge_rollout_targets_update_prd127;
DROP INDEX idx_edge_rollout_targets_batch_prd127;
DROP INDEX idx_edge_rollout_targets_agent_prd127;
DROP TABLE edge_rollout_targets;
DROP TRIGGER trg_edge_rollout_batches_delete_prd127;
DROP TRIGGER trg_edge_rollout_batches_update_prd127;
DROP INDEX idx_edge_rollout_batches_prd127;
DROP TABLE edge_rollout_batches;
DROP TRIGGER trg_edge_fleet_rollouts_delete_prd127;
DROP TRIGGER trg_edge_fleet_rollouts_update_prd127;
DROP INDEX idx_edge_fleet_rollouts_prd127;
DROP TABLE edge_fleet_rollouts;
DROP TRIGGER trg_edge_release_heads_delete_prd127;
DROP TRIGGER trg_edge_release_heads_update_prd127;
DROP TRIGGER trg_edge_release_heads_deployment_insert_prd127;
DROP TABLE edge_deployment_release_heads;
DROP TRIGGER trg_edge_maintenance_windows_delete_prd127;
DROP TRIGGER trg_edge_maintenance_windows_update_prd127;
DROP INDEX idx_edge_maintenance_windows_prd127;
DROP TABLE edge_maintenance_windows;
DROP TRIGGER trg_edge_release_channels_delete_prd127;
DROP TRIGGER trg_edge_release_channels_update_prd127;
DROP TRIGGER trg_edge_release_channels_insert_prd127;
DROP TABLE edge_release_channels;
DROP TRIGGER trg_edge_gateway_artifacts_delete_prd127;
DROP TRIGGER trg_edge_gateway_artifacts_update_prd127;
DROP TRIGGER trg_edge_gateway_artifacts_insert_prd127;
DROP TABLE edge_gateway_release_artifacts;
DROP TRIGGER trg_edge_gateway_releases_delete_prd127;
DROP TRIGGER trg_edge_gateway_releases_update_prd127;
DROP TRIGGER trg_edge_gateway_releases_insert_prd127;
DROP TABLE edge_gateway_releases;

DROP TRIGGER trg_platform_edge_upgrade_roles_delete_prd127;
DROP TRIGGER trg_platform_edge_upgrade_roles_update_prd127;
DROP TRIGGER trg_platform_edge_upgrade_roles_insert_prd127;
DROP TRIGGER trg_platform_edge_upgrade_permissions_delete_prd127;
DROP TRIGGER trg_platform_edge_upgrade_permissions_update_prd127;
DROP TRIGGER trg_platform_edge_upgrade_permissions_insert_prd127;
DROP TABLE platform_edge_upgrade_role_permissions;
DROP TABLE platform_edge_upgrade_permission_extensions;
DROP TRIGGER trg_workspace_edge_upgrade_roles_delete_prd127;
DROP TRIGGER trg_workspace_edge_upgrade_roles_update_prd127;
DROP TRIGGER trg_workspace_edge_upgrade_roles_insert_prd127;
DROP TRIGGER trg_workspace_edge_upgrade_permissions_delete_prd127;
DROP TRIGGER trg_workspace_edge_upgrade_permissions_update_prd127;
DROP TRIGGER trg_workspace_edge_upgrade_permissions_insert_prd127;
DROP TABLE workspace_edge_upgrade_role_permissions;
DROP TABLE workspace_edge_upgrade_permission_extensions;

DROP TRIGGER trg_platform_job_types_delete_prd106;
DELETE FROM platform_job_types WHERE type_code='fleet.rollout.advance';
CREATE TRIGGER trg_platform_job_types_delete_prd106 BEFORE DELETE ON platform_job_types
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

DELETE FROM tenant_deletion_trigger_manifest;
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

CREATE TEMP TABLE prd127_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd127_down_final_guard(issue_count)
SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM platform_job_types
      WHERE type_code='fleet.rollout.advance') THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_upgrade_permission_extensions','workspace_edge_upgrade_role_permissions',
      'platform_edge_upgrade_permission_extensions','platform_edge_upgrade_role_permissions',
      'edge_gateway_releases','edge_gateway_release_artifacts','edge_release_channels',
      'edge_maintenance_windows','edge_deployment_release_heads','edge_fleet_rollouts',
      'edge_rollout_batches','edge_rollout_targets','edge_upgrade_commands',
      'edge_upgrade_receipts','edge_rollback_attempts','edge_fleet_upgrade_events',
      'edge_fleet_upgrade_outbox','edge_upgrade_action_commands',
      'tenant_deletion_manifest_extensions_prd127'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd127_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
