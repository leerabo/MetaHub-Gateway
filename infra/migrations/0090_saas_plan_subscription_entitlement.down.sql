-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd098_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd098_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0090' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM workspace_subscriptions)
  + (SELECT COUNT(*) FROM subscription_trials)
  + (SELECT COUNT(*) FROM subscription_order_records)
  + (SELECT COUNT(*) FROM entitlement_snapshots)
  + (SELECT COUNT(*) FROM saas_subscription_events)
  + (SELECT COUNT(*) FROM saas_plan_versions)
  + (SELECT COUNT(*) FROM saas_plan_catalogs)
  + (SELECT COUNT(*) FROM platform_audit_logs
      WHERE action_code IN('saas.plan_catalog_created','saas.plan_version_created'))
  + CASE WHEN (SELECT COUNT(*) FROM platform_permission_extensions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_role_permission_extensions)=4 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd098_down_guard;

DROP TRIGGER trg_saas_subscription_events_delete_prd098;
DROP TRIGGER trg_saas_subscription_events_update_prd098;
DROP TRIGGER trg_entitlement_snapshots_delete_prd098;
DROP TRIGGER trg_entitlement_snapshots_update_prd098;
DROP TRIGGER trg_saas_plan_versions_delete_prd098;
DROP TRIGGER trg_saas_plan_versions_published_update_prd098;
DROP TRIGGER trg_platform_role_permission_extensions_delete_prd098;
DROP TRIGGER trg_platform_role_permission_extensions_update_prd098;
DROP TRIGGER trg_platform_role_permission_extensions_insert_prd098;
DROP TRIGGER trg_platform_permission_extensions_delete_prd098;
DROP TRIGGER trg_platform_permission_extensions_update_prd098;
DROP TRIGGER trg_platform_permission_extensions_insert_prd098;

DROP TABLE saas_subscription_events;
DROP TABLE entitlement_snapshots;
DROP TABLE subscription_order_records;
DROP TABLE subscription_trials;
DROP TABLE workspace_subscriptions;
DROP TABLE saas_plan_versions;
DROP TABLE saas_plan_catalogs;
DROP TABLE platform_role_permission_extensions;
DROP TABLE platform_permission_extensions;

CREATE TEMP TABLE prd098_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd098_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd098_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
