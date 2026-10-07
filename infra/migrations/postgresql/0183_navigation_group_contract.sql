-- metahub: navigation-group-contract-v1
-- Replace the last persisted pre-split projection. Runtime navigation accepts
-- only the current Governance/System group contract.

BEGIN;
ALTER TABLE workspace_navigation_projection_items DISABLE TRIGGER USER;
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
ALTER TABLE workspace_navigation_projection_items ENABLE TRIGGER USER;
COMMIT;
