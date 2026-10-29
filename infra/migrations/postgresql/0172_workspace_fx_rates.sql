-- metahub: workspace-fx-rates-v1
CREATE TABLE "workspace_fx_rate_versions"(
  "workspace_fx_rate_version_id" TEXT PRIMARY KEY,
  "workspace_id" TEXT NOT NULL,
  "tenant_id" TEXT NOT NULL,
  "version_number" BIGINT NOT NULL CHECK("version_number">=1),
  "usd_to_cny" TEXT NOT NULL CHECK(
    "usd_to_cny" ~ '^[0-9]+(\.[0-9]+)?$'
    AND "usd_to_cny"::numeric>0 AND "usd_to_cny"::numeric<=100),
  "effective_from" TEXT NOT NULL CHECK(unixepoch("effective_from") IS NOT NULL),
  "created_by_membership_id" TEXT,
  "created_at" TEXT NOT NULL CHECK(unixepoch("created_at") IS NOT NULL),
  FOREIGN KEY("workspace_id","tenant_id")
    REFERENCES "workspace_tenant_bindings"("workspace_id","tenant_id"),
  FOREIGN KEY("created_by_membership_id")
    REFERENCES "workspace_memberships"("workspace_membership_id"),
  UNIQUE("workspace_id","tenant_id","version_number"),
  UNIQUE("workspace_id","tenant_id","effective_from")
);
CREATE INDEX "idx_workspace_fx_rate_effective"
  ON "workspace_fx_rate_versions"("tenant_id","effective_from" DESC,"version_number" DESC);
INSERT INTO "workspace_fx_rate_versions"(
  "workspace_fx_rate_version_id","workspace_id","tenant_id","version_number",
  "usd_to_cny","effective_from","created_by_membership_id","created_at")
SELECT 'wfxv_'||substr(metahub_sha256("workspace_id"||':'||"tenant_id"),1,32),
  "workspace_id","tenant_id",1,'7.2','1970-01-01T00:00:00Z',NULL,metahub_utc_now()
FROM "workspace_tenant_bindings" WHERE "status"='active';
