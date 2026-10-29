-- metahub: backfill-personal-key-application-binding-v1
-- Repair keys created after their member was assigned to an application.
UPDATE virtual_keys
SET project_id=(
      SELECT a.project_id
      FROM application_memberships am
      JOIN workspace_memberships wm ON wm.tenant_id=am.tenant_id
        AND wm.workspace_membership_id=am.workspace_membership_id
      JOIN applications a ON a.tenant_id=am.tenant_id AND a.application_id=am.application_id
      WHERE am.tenant_id=virtual_keys.tenant_id AND wm.legacy_user_id=virtual_keys.created_by
        AND am.status='active' AND wm.status='active'
      ORDER BY am.updated_at DESC, am.application_membership_id DESC
      LIMIT 1
    ),
    application_id=(
      SELECT am.application_id
      FROM application_memberships am
      JOIN workspace_memberships wm ON wm.tenant_id=am.tenant_id
        AND wm.workspace_membership_id=am.workspace_membership_id
      WHERE am.tenant_id=virtual_keys.tenant_id AND wm.legacy_user_id=virtual_keys.created_by
        AND am.status='active' AND wm.status='active'
      ORDER BY am.updated_at DESC, am.application_membership_id DESC
      LIMIT 1
    ),
    status=CASE
      WHEN (
        SELECT a.status='active' AND p.status='active'
        FROM application_memberships am
        JOIN workspace_memberships wm ON wm.tenant_id=am.tenant_id
          AND wm.workspace_membership_id=am.workspace_membership_id
        JOIN applications a ON a.tenant_id=am.tenant_id AND a.application_id=am.application_id
        JOIN projects p ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id
        WHERE am.tenant_id=virtual_keys.tenant_id AND wm.legacy_user_id=virtual_keys.created_by
          AND am.status='active' AND wm.status='active'
        ORDER BY am.updated_at DESC, am.application_membership_id DESC
        LIMIT 1
      ) AND disabled_reason='project_inactive' THEN 'active'
      WHEN (
        SELECT a.status='active' AND p.status='active'
        FROM application_memberships am
        JOIN workspace_memberships wm ON wm.tenant_id=am.tenant_id
          AND wm.workspace_membership_id=am.workspace_membership_id
        JOIN applications a ON a.tenant_id=am.tenant_id AND a.application_id=am.application_id
        JOIN projects p ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id
        WHERE am.tenant_id=virtual_keys.tenant_id AND wm.legacy_user_id=virtual_keys.created_by
          AND am.status='active' AND wm.status='active'
        ORDER BY am.updated_at DESC, am.application_membership_id DESC
        LIMIT 1
      ) THEN status
      ELSE 'disabled'
    END,
    disabled_reason=CASE
      WHEN (
        SELECT a.status='active' AND p.status='active'
        FROM application_memberships am
        JOIN workspace_memberships wm ON wm.tenant_id=am.tenant_id
          AND wm.workspace_membership_id=am.workspace_membership_id
        JOIN applications a ON a.tenant_id=am.tenant_id AND a.application_id=am.application_id
        JOIN projects p ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id
        WHERE am.tenant_id=virtual_keys.tenant_id AND wm.legacy_user_id=virtual_keys.created_by
          AND am.status='active' AND wm.status='active'
        ORDER BY am.updated_at DESC, am.application_membership_id DESC
        LIMIT 1
      ) THEN CASE WHEN disabled_reason='project_inactive' THEN 'manual' ELSE disabled_reason END
      ELSE 'project_inactive'
    END,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE project_id IS NULL AND application_id IS NULL
  AND EXISTS (
    SELECT 1
    FROM application_memberships am
    JOIN workspace_memberships wm ON wm.tenant_id=am.tenant_id
      AND wm.workspace_membership_id=am.workspace_membership_id
    WHERE am.tenant_id=virtual_keys.tenant_id AND wm.legacy_user_id=virtual_keys.created_by
      AND am.status='active' AND wm.status='active'
  );

INSERT INTO virtual_key_binding_history(
  virtual_key_binding_id, tenant_id, virtual_key_id, project_id, application_id,
  effective_from, effective_to, change_reason, created_at
)
SELECT
  'vkbind_' || lower(hex(randomblob(16))), tenant_id, virtual_key_id, project_id, application_id,
  created_at, NULL, 'assigned', created_at
FROM virtual_keys vk
WHERE vk.project_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM virtual_key_binding_history history
    WHERE history.tenant_id=vk.tenant_id AND history.virtual_key_id=vk.virtual_key_id
      AND history.effective_to IS NULL
  );
