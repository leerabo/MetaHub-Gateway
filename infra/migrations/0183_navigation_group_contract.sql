-- metahub: navigation-group-contract-v1
-- Replace the last persisted pre-split projection. Runtime navigation accepts
-- only the current Governance/System group contract.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_workspace_navigation_projection_item_update_prd146;
UPDATE workspace_navigation_projection_items
   SET group_code=CASE source_navigation_id
     WHEN 'request-logs' THEN 'governance'
     WHEN 'request-explanations' THEN 'governance'
     WHEN 'feedback-summary' THEN 'system'
     WHEN 'migration-operations' THEN 'system'
     WHEN 'support-tickets' THEN 'system'
     WHEN 'system-status' THEN 'system'
     ELSE group_code
   END
 WHERE group_code='observability';
CREATE TRIGGER trg_workspace_navigation_projection_item_update_prd146
BEFORE UPDATE ON workspace_navigation_projection_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_projection_immutable'); END;

COMMIT;
PRAGMA foreign_keys=ON;
