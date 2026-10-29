-- metahub: virtual-key-personal-assignment-lifecycle-v1
-- Keep deleted personal keys for accounting/audit while releasing the one-key slot.

ALTER TABLE "virtual_keys"
  ADD COLUMN "deleted_at" TEXT
  CHECK("deleted_at" IS NULL OR unixepoch("deleted_at") IS NOT NULL);

UPDATE "virtual_keys" vk
SET "status"='disabled',
    "disabled_reason"='manual',
    "disabled_at"=COALESCE(vk."disabled_at", vk."updated_at"),
    "updated_at"=metahub_utc_now()
FROM "users" u
WHERE u."tenant_id"=vk."tenant_id"
  AND u."user_id"=vk."created_by"
  AND u."role"='developer'
  AND vk."status"='active'
  AND vk."project_id" IS NULL
  AND vk."application_id" IS NULL;

CREATE INDEX "idx_virtual_keys_personal_lifecycle"
  ON "virtual_keys"("tenant_id", "created_by", "deleted_at", "status");
