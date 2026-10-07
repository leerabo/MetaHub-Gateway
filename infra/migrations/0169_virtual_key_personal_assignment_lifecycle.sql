-- metahub: virtual-key-personal-assignment-lifecycle-v1
-- A deleted personal key remains in immutable history but no longer occupies
-- the developer's one-key slot. Unassigned personal keys are disabled until
-- an active project and application are assigned.

ALTER TABLE virtual_keys
  ADD COLUMN deleted_at TEXT
  CHECK(deleted_at IS NULL OR unixepoch(deleted_at) IS NOT NULL);

-- Migration 0168 repairs members that already belong to an application. Any
-- remaining unbound active developer key is therefore a key awaiting assignment.
UPDATE virtual_keys
SET status='disabled',
    disabled_reason='manual',
    disabled_at=COALESCE(disabled_at, updated_at),
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE status='active'
  AND project_id IS NULL
  AND application_id IS NULL
  AND EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id=virtual_keys.tenant_id
      AND u.user_id=virtual_keys.created_by
      AND u.role='developer'
  );

CREATE INDEX idx_virtual_keys_personal_lifecycle
  ON virtual_keys(tenant_id, created_by, deleted_at, status);
