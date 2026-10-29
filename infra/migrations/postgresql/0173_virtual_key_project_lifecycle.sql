-- metahub: virtual-key-project-lifecycle-v1
-- Keep one current project binding per key while preserving every binding interval for audit.

ALTER TABLE "virtual_keys"
  ADD COLUMN "disabled_reason" TEXT NOT NULL DEFAULT 'manual'
  CHECK ("disabled_reason" IN ('manual', 'project_inactive', 'member_removed'));

CREATE TABLE "virtual_key_binding_history"(
  "virtual_key_binding_id" TEXT PRIMARY KEY,
  "tenant_id" TEXT NOT NULL,
  "virtual_key_id" TEXT NOT NULL,
  "project_id" TEXT NOT NULL,
  "application_id" TEXT,
  "effective_from" TEXT NOT NULL CHECK(unixepoch("effective_from") IS NOT NULL),
  "effective_to" TEXT CHECK("effective_to" IS NULL OR unixepoch("effective_to") IS NOT NULL),
  "change_reason" TEXT NOT NULL CHECK("change_reason" IN ('assigned', 'reassigned', 'unbound')),
  "created_at" TEXT NOT NULL CHECK(unixepoch("created_at") IS NOT NULL),
  FOREIGN KEY("tenant_id") REFERENCES "tenants"("tenant_id"),
  FOREIGN KEY("virtual_key_id") REFERENCES "virtual_keys"("virtual_key_id"),
  FOREIGN KEY("project_id") REFERENCES "projects"("project_id"),
  FOREIGN KEY("application_id") REFERENCES "applications"("application_id"),
  CHECK("effective_to" IS NULL OR "effective_to" >= "effective_from")
);

CREATE UNIQUE INDEX "idx_virtual_key_binding_current"
  ON "virtual_key_binding_history"("tenant_id", "virtual_key_id")
  WHERE "effective_to" IS NULL;
CREATE INDEX "idx_virtual_key_binding_project_time"
  ON "virtual_key_binding_history"("tenant_id", "project_id", "effective_from" DESC);

INSERT INTO "virtual_key_binding_history"(
  "virtual_key_binding_id", "tenant_id", "virtual_key_id", "project_id", "application_id",
  "effective_from", "effective_to", "change_reason", "created_at"
)
SELECT
  'vkbind_' || substr(metahub_sha256(vk."virtual_key_id" || ':' || vk."project_id" || ':' || coalesce(vk."application_id", '')), 1, 32),
  vk."tenant_id", vk."virtual_key_id", vk."project_id", vk."application_id",
  vk."created_at", NULL, 'assigned', vk."created_at"
FROM "virtual_keys" vk
JOIN "projects" p ON p."tenant_id"=vk."tenant_id" AND p."project_id"=vk."project_id"
WHERE vk."project_id" IS NOT NULL;

CREATE INDEX "idx_virtual_keys_project_status"
  ON "virtual_keys"("tenant_id", "project_id", "status", "disabled_reason");
