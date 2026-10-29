-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd105_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd105_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0097' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM notification_templates)=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM notifications)
  + (SELECT COUNT(*) FROM notification_preferences)
  + (SELECT COUNT(*) FROM delivery_attempts)
  + (SELECT COUNT(*) FROM channel_suppressions)
  + (SELECT COUNT(*) FROM notification_outbox)
  + (SELECT COUNT(*) FROM platform_operator_role_bindings
      WHERE role_code='platform_communications')
  + CASE WHEN (SELECT COUNT(*) FROM platform_notification_permission_extensions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_notification_role_permissions)=4 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd105_down_guard;

DROP TRIGGER trg_delivery_attempts_delete_prd105;
DROP TRIGGER trg_delivery_attempts_update_prd105;
DROP TRIGGER trg_notification_outbox_owner_prd105;
DROP TRIGGER trg_notification_outbox_delete_prd105;
DROP TRIGGER trg_channel_suppressions_delete_prd105;
DROP TRIGGER trg_channel_suppressions_update_prd105;
DROP TRIGGER trg_notification_preferences_delete_prd105;
DROP TRIGGER trg_notification_preferences_update_prd105;
DROP TRIGGER trg_notifications_delete_prd105;
DROP TRIGGER trg_notifications_update_prd105;
DROP TRIGGER trg_notification_catalog_delete_prd105;
DROP TRIGGER trg_notification_catalog_insert_prd105;
DROP TRIGGER trg_notification_catalog_update_prd105;
DROP TRIGGER trg_notification_templates_delete_prd105;
DROP TRIGGER trg_notification_templates_update_prd105;

DROP TABLE delivery_attempts;
DROP TABLE notification_outbox;
DROP TABLE channel_suppressions;
DROP TABLE notification_preferences;
DROP TABLE notifications;
DROP TABLE notification_template_catalog_state;
DROP TABLE notification_templates;

DROP TRIGGER trg_platform_notification_role_permissions_delete_prd105;
DROP TRIGGER trg_platform_notification_role_permissions_update_prd105;
DROP TRIGGER trg_platform_notification_role_permissions_insert_prd105;
DROP TRIGGER trg_platform_notification_permissions_delete_prd105;
DROP TRIGGER trg_platform_notification_permissions_update_prd105;
DROP TRIGGER trg_platform_notification_permissions_insert_prd105;
DROP TABLE platform_notification_role_permissions;
DROP TABLE platform_notification_permission_extensions;

DROP TRIGGER trg_platform_roles_delete_prd095;
DROP TRIGGER trg_platform_roles_update_prd095;
DROP TRIGGER trg_platform_roles_insert_prd095;
ALTER TABLE platform_roles RENAME TO platform_roles_prd105;
CREATE TABLE platform_roles(
  role_code TEXT PRIMARY KEY CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security','platform_billing',
    'platform_support','platform_verification','platform_compliance','platform_privacy',
    'release_manager','platform_provider_engineer','platform_channel',
    'platform_customer_success','platform_growth')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1)
);
INSERT INTO platform_roles(role_code,catalog_revision)
SELECT role_code,catalog_revision FROM platform_roles_prd105
WHERE role_code!='platform_communications';
DROP TABLE platform_roles_prd105;
CREATE TRIGGER trg_platform_roles_insert_prd095 BEFORE INSERT ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_roles_update_prd095 BEFORE UPDATE ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_roles_delete_prd095 BEFORE DELETE ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;

CREATE TEMP TABLE prd105_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd105_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_roles)=13 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd105_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
