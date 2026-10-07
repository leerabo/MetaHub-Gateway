-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_workspace_navigation_projection_item_update_prd146;
UPDATE workspace_navigation_projection_items
   SET group_code='observability'
 WHERE source_navigation_id IN (
   'request-logs','request-explanations','feedback-summary',
   'migration-operations','support-tickets','system-status'
 );
CREATE TRIGGER trg_workspace_navigation_projection_item_update_prd146
BEFORE UPDATE ON workspace_navigation_projection_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_projection_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;
