-- metahub: navigation-group-contract-v1 down
ALTER TABLE workspace_navigation_projection_items DISABLE TRIGGER USER;
UPDATE workspace_navigation_projection_items
   SET group_code='observability'
 WHERE source_navigation_id IN (
   'request-logs','request-explanations','feedback-summary',
   'migration-operations','support-tickets','system-status'
 );
ALTER TABLE workspace_navigation_projection_items ENABLE TRIGGER USER;
