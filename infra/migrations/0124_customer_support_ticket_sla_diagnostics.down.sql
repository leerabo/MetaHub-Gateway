-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd132_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd132_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0124' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM support_tickets)
  + (SELECT COUNT(*) FROM support_ticket_messages)
  + (SELECT COUNT(*) FROM support_ticket_attachments)
  + (SELECT COUNT(*) FROM support_attachment_scan_jobs)
  + (SELECT COUNT(*) FROM support_sla_clocks)
  + (SELECT COUNT(*) FROM support_sla_clock_events)
  + (SELECT COUNT(*) FROM support_escalations)
  + (SELECT COUNT(*) FROM support_satisfaction_surveys)
  + (SELECT COUNT(*) FROM support_ticket_events)
  + (SELECT COUNT(*) FROM support_event_outbox)
  + (SELECT COUNT(*) FROM support_action_commands)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd132_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd132_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd132_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd132_delete;
CREATE TEMP TABLE prd132_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd132;
DROP TABLE tenant_deletion_manifest_extensions_prd132;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd132_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd132_manifest_sources);
DROP TABLE prd132_manifest_sources;

DROP TRIGGER trg_support_scheduler_state_delete_prd132;
DROP TRIGGER trg_support_scheduler_state_update_prd132;
DROP TABLE support_scheduler_state;
DROP TRIGGER trg_support_actions_delete_prd132;
DROP TRIGGER trg_support_actions_update_prd132;
DROP TABLE support_action_commands;
DROP TRIGGER trg_support_outbox_delete_prd132;
DROP TRIGGER trg_support_outbox_update_prd132;
DROP INDEX idx_support_outbox_due_prd132;
DROP TABLE support_event_outbox;
DROP TRIGGER trg_support_events_delete_prd132;
DROP TRIGGER trg_support_events_update_prd132;
DROP INDEX idx_support_events_ticket_prd132;
DROP TABLE support_ticket_events;
DROP TRIGGER trg_support_surveys_delete_prd132;
DROP TRIGGER trg_support_surveys_update_prd132;
DROP TABLE support_satisfaction_surveys;
DROP TRIGGER trg_support_escalations_delete_prd132;
DROP TRIGGER trg_support_escalations_update_prd132;
DROP INDEX idx_support_escalations_ticket_prd132;
DROP TABLE support_escalations;
DROP TRIGGER trg_support_clock_events_delete_prd132;
DROP TRIGGER trg_support_clock_events_update_prd132;
DROP INDEX idx_support_clock_events_ticket_prd132;
DROP TABLE support_sla_clock_events;
DROP TRIGGER trg_support_sla_clocks_delete_prd132;
DROP TRIGGER trg_support_sla_clocks_update_prd132;
DROP INDEX idx_support_sla_due_prd132;
DROP TABLE support_sla_clocks;
DROP TRIGGER trg_support_attachment_jobs_delete_prd132;
DROP TRIGGER trg_support_attachment_jobs_update_prd132;
DROP INDEX idx_support_attachment_jobs_due_prd132;
DROP TABLE support_attachment_scan_jobs;
DROP TRIGGER trg_support_attachments_delete_prd132;
DROP TRIGGER trg_support_attachments_update_prd132;
DROP INDEX idx_support_attachments_scan_prd132;
DROP INDEX idx_support_attachments_ticket_prd132;
DROP TABLE support_ticket_attachments;
DROP TRIGGER trg_support_messages_delete_prd132;
DROP TRIGGER trg_support_messages_update_prd132;
DROP INDEX idx_support_messages_ticket_prd132;
DROP TABLE support_ticket_messages;
DROP TRIGGER trg_support_tickets_delete_prd132;
DROP TRIGGER trg_support_tickets_update_prd132;
DROP INDEX idx_support_tickets_platform_queue_prd132;
DROP INDEX idx_support_tickets_workspace_prd132;
DROP TABLE support_tickets;
DROP TRIGGER trg_support_sla_policies_delete_prd132;
DROP TRIGGER trg_support_sla_policies_update_prd132;
DROP TRIGGER trg_support_sla_policies_insert_prd132;
DROP TABLE support_sla_policies;
DROP TRIGGER trg_platform_customer_support_roles_delete_prd132;
DROP TRIGGER trg_platform_customer_support_roles_update_prd132;
DROP TRIGGER trg_platform_customer_support_roles_insert_prd132;
DROP TRIGGER trg_platform_customer_support_permissions_delete_prd132;
DROP TRIGGER trg_platform_customer_support_permissions_update_prd132;
DROP TRIGGER trg_platform_customer_support_permissions_insert_prd132;
DROP TABLE platform_customer_support_role_permissions;
DROP TABLE platform_customer_support_permission_extensions;
DROP TRIGGER trg_workspace_customer_support_roles_delete_prd132;
DROP TRIGGER trg_workspace_customer_support_roles_update_prd132;
DROP TRIGGER trg_workspace_customer_support_roles_insert_prd132;
DROP TRIGGER trg_workspace_customer_support_permissions_delete_prd132;
DROP TRIGGER trg_workspace_customer_support_permissions_update_prd132;
DROP TRIGGER trg_workspace_customer_support_permissions_insert_prd132;
DROP TABLE workspace_customer_support_role_permissions;
DROP TABLE workspace_customer_support_permission_extensions;

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

CREATE TEMP TABLE prd132_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd132_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_customer_support_permission_extensions',
      'workspace_customer_support_role_permissions',
      'platform_customer_support_permission_extensions',
      'platform_customer_support_role_permissions','support_sla_policies','support_tickets',
      'support_ticket_messages','support_ticket_attachments','support_attachment_scan_jobs',
      'support_sla_clocks','support_sla_clock_events','support_escalations',
      'support_satisfaction_surveys','support_ticket_events','support_event_outbox',
      'support_action_commands','support_scheduler_state',
      'tenant_deletion_manifest_extensions_prd132'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd132_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
