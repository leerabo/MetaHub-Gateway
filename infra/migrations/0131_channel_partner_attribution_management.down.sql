-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd139_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd139_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0131' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM channel_partners)
  + (SELECT COUNT(*) FROM channel_memberships)
  + (SELECT COUNT(*) FROM channel_referral_codes)
  + (SELECT COUNT(*) FROM channel_registered_leads)
  + (SELECT COUNT(*) FROM channel_commission_rule_snapshots)
  + (SELECT COUNT(*) FROM workspace_attributions)
  + (SELECT COUNT(*) FROM attribution_changes)
  + (SELECT COUNT(*) FROM attribution_change_approvals)
  + (SELECT COUNT(*) FROM channel_statements)
  + (SELECT COUNT(*) FROM channel_statement_lines)
  + (SELECT COUNT(*) FROM channel_legacy_object_mappings)
  + (SELECT COUNT(*) FROM channel_action_commands)
  + (SELECT COUNT(*) FROM channel_events)
  + (SELECT COUNT(*) FROM channel_event_outbox)
  + CASE WHEN (SELECT COUNT(*) FROM channel_migration_cursors)=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_channel_permission_extensions)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_channel_role_permissions)=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM channel_scheduler_state)=1
      AND (SELECT fencing_generation FROM channel_scheduler_state)=0
      AND (SELECT revision FROM channel_scheduler_state)=1
      AND (SELECT lease_owner_id FROM channel_scheduler_state) IS NULL
      AND (SELECT lease_token_sha256 FROM channel_scheduler_state) IS NULL
      AND (SELECT lease_expires_at FROM channel_scheduler_state) IS NULL
      AND (SELECT statement_watermark_at FROM channel_scheduler_state)
        ='1970-01-01T00:00:00.000Z'
      AND (SELECT outbox_watermark_at FROM channel_scheduler_state)
        ='1970-01-01T00:00:00.000Z'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd139_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd139_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd139_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd139_delete;
CREATE TEMP TABLE prd139_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd139;
DROP TABLE tenant_deletion_manifest_extensions_prd139;
DELETE FROM tenant_deletion_trigger_manifest;
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd139_manifest_sources);
DROP TABLE prd139_manifest_sources;

DROP TRIGGER trg_legacy_partner_outbox_delete_prd139;
DROP TRIGGER trg_legacy_partner_outbox_update_prd139;
DROP TRIGGER trg_legacy_partner_outbox_write_prd139;
DROP TRIGGER trg_legacy_partner_operations_delete_prd139;
DROP TRIGGER trg_legacy_partner_operations_update_prd139;
DROP TRIGGER trg_legacy_partner_operations_write_prd139;
DROP TRIGGER trg_legacy_commissions_delete_prd139;
DROP TRIGGER trg_legacy_commissions_update_prd139;
DROP TRIGGER trg_legacy_commissions_write_prd139;
DROP TRIGGER trg_legacy_partner_cursors_delete_prd139;
DROP TRIGGER trg_legacy_partner_cursors_update_prd139;
DROP TRIGGER trg_legacy_partner_cursors_write_prd139;
DROP TRIGGER trg_legacy_partner_customers_delete_prd139;
DROP TRIGGER trg_legacy_partner_customers_update_prd139;
DROP TRIGGER trg_legacy_partner_customers_write_prd139;
DROP TRIGGER trg_legacy_partners_delete_prd139;
DROP TRIGGER trg_legacy_partners_update_prd139;
DROP TRIGGER trg_legacy_partners_write_prd139;

DROP TRIGGER trg_channel_scheduler_delete_prd139;
DROP TRIGGER trg_channel_scheduler_update_prd139;
DROP TABLE channel_scheduler_state;
DROP TRIGGER trg_channel_outbox_delete_prd139;
DROP TABLE channel_event_outbox;
DROP TRIGGER trg_channel_event_delete_prd139;
DROP TRIGGER trg_channel_event_update_prd139;
DROP TABLE channel_events;
DROP TRIGGER trg_channel_action_delete_prd139;
DROP TRIGGER trg_channel_action_update_prd139;
DROP TABLE channel_action_commands;
DROP TRIGGER trg_channel_migration_cursor_delete_prd139;
DROP TRIGGER trg_channel_migration_cursor_update_prd139;
DROP TABLE channel_migration_cursors;
DROP TRIGGER trg_channel_mapping_delete_prd139;
DROP TRIGGER trg_channel_mapping_update_prd139;
DROP TABLE channel_legacy_object_mappings;
DROP TRIGGER trg_channel_statement_line_delete_prd139;
DROP TRIGGER trg_channel_statement_line_update_prd139;
DROP TABLE channel_statement_lines;
DROP TRIGGER trg_channel_statement_delete_prd139;
DROP TRIGGER trg_channel_statement_update_prd139;
DROP TABLE channel_statements;
DROP TRIGGER trg_attribution_approval_delete_prd139;
DROP TRIGGER trg_attribution_approval_update_prd139;
DROP TABLE attribution_change_approvals;
DROP TABLE attribution_changes;
DROP TRIGGER trg_workspace_attribution_owner_insert_prd139;
DROP INDEX uq_workspace_active_attribution_prd139;
DROP INDEX idx_workspace_attribution_partner_prd139;
DROP TABLE workspace_attributions;
DROP TRIGGER trg_channel_rule_delete_prd139;
DROP TRIGGER trg_channel_rule_update_prd139;
DROP TABLE channel_commission_rule_snapshots;
DROP INDEX idx_channel_leads_directory_prd139;
DROP TABLE channel_registered_leads;
DROP INDEX uq_channel_referral_active_prd139;
DROP TABLE channel_referral_codes;
DROP TRIGGER trg_channel_membership_delete_prd139;
DROP TRIGGER trg_channel_membership_update_prd139;
DROP TRIGGER trg_channel_membership_owner_insert_prd139;
DROP INDEX idx_channel_membership_account_prd139;
DROP TABLE channel_memberships;
DROP TRIGGER trg_channel_partner_delete_prd139;
DROP TRIGGER trg_channel_partner_owner_update_prd139;
DROP INDEX idx_channel_partners_directory_prd139;
DROP TABLE channel_partners;
DROP TRIGGER trg_platform_channel_roles_delete_prd139;
DROP TRIGGER trg_platform_channel_roles_update_prd139;
DROP TRIGGER trg_platform_channel_roles_insert_prd139;
DROP TRIGGER trg_platform_channel_permissions_delete_prd139;
DROP TRIGGER trg_platform_channel_permissions_update_prd139;
DROP TRIGGER trg_platform_channel_permissions_insert_prd139;
DROP TABLE platform_channel_role_permissions;
DROP TABLE platform_channel_permission_extensions;

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

CREATE TEMP TABLE prd139_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd139_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_channel_permission_extensions','platform_channel_role_permissions',
      'channel_partners','channel_memberships','channel_referral_codes',
      'channel_registered_leads','channel_commission_rule_snapshots','workspace_attributions',
      'attribution_changes','attribution_change_approvals','channel_statements',
      'channel_statement_lines','channel_legacy_object_mappings','channel_migration_cursors',
      'channel_action_commands','channel_events','channel_event_outbox',
      'channel_scheduler_state','tenant_deletion_manifest_extensions_prd139'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd139_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
