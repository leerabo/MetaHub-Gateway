-- metahub: backfill-personal-key-application-binding-v1
-- PostgreSQL deployments apply the same reviewed repair after the native migration baseline.
UPDATE "virtual_keys" vk
SET "project_id"=a."project_id", "application_id"=am."application_id",
    "updated_at"=metahub_utc_now()
FROM "application_memberships" am
JOIN "workspace_memberships" wm ON wm."tenant_id"=am."tenant_id"
  AND wm."workspace_membership_id"=am."workspace_membership_id"
JOIN "applications" a ON a."tenant_id"=am."tenant_id" AND a."application_id"=am."application_id"
WHERE vk."tenant_id"=am."tenant_id" AND wm."legacy_user_id"=vk."created_by"
  AND am."status"='active' AND wm."status"='active'
  AND vk."project_id" IS NULL AND vk."application_id" IS NULL;
