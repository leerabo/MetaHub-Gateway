-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
-- 仅在无业务放置和租约状态时回滚云网关持久对象，防止静默丢失运行事实。

CREATE TEMP TABLE prd101_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 回滚前验证版本、空业务状态、原始种子和数据库完整性，任一异常均失败关闭。
INSERT INTO prd101_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0093' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM workspace_gateway_placements)
  + (SELECT COUNT(*) FROM gateway_capacity_leases)
  + (SELECT COUNT(*) FROM gateway_config_snapshot_leases)
  + (SELECT COUNT(*) FROM gateway_releases)
  + (SELECT COUNT(*) FROM gateway_cell_events)
  + (SELECT COUNT(*) FROM gateway_config_outbox)
  + CASE WHEN (SELECT COUNT(*) FROM gateway_cells)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM gateway_cells WHERE revision!=1
      OR status!='active' OR health_status!='healthy' OR admitted_leases!=0
      OR release_watermark!=0 OR config_watermark!=0)=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_gateway_permission_extensions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_gateway_role_permissions)=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd101_down_guard;

DROP TRIGGER trg_gateway_cell_event_outbox_prd101;
DROP TABLE gateway_config_outbox;
DROP TRIGGER trg_gateway_cell_events_delete_prd101;
DROP TRIGGER trg_gateway_cell_events_update_prd101;
DROP TABLE gateway_cell_events;
DROP TRIGGER trg_gateway_release_owner_update_prd101;
DROP TABLE gateway_releases;
DROP TABLE gateway_config_snapshot_leases;
DROP TRIGGER trg_gateway_capacity_owner_update_prd101;
DROP TABLE gateway_capacity_leases;
DROP TRIGGER trg_gateway_placements_delete_prd101;
DROP TRIGGER trg_gateway_placement_owner_update_prd101;
DROP TABLE workspace_gateway_placements;
DROP TRIGGER trg_gateway_cells_delete_prd101;
DROP TRIGGER trg_gateway_cells_owner_update_prd101;
DROP TABLE gateway_cells;
DROP TRIGGER trg_platform_gateway_role_permissions_delete_prd101;
DROP TRIGGER trg_platform_gateway_role_permissions_update_prd101;
DROP TRIGGER trg_platform_gateway_role_permissions_insert_prd101;
DROP TRIGGER trg_platform_gateway_permissions_delete_prd101;
DROP TRIGGER trg_platform_gateway_permissions_update_prd101;
DROP TRIGGER trg_platform_gateway_permissions_insert_prd101;
DROP TABLE platform_gateway_role_permissions;
DROP TABLE platform_gateway_permission_extensions;

CREATE TEMP TABLE prd101_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 删除完成后再次验证外键与数据库完整性，避免提交部分回滚结果。
INSERT INTO prd101_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd101_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
