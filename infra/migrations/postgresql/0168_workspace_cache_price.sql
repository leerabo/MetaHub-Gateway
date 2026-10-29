-- metahub: workspace-cache-price-v1
ALTER TABLE "platform_provider_reference_rate_releases"
  ADD COLUMN "cache_rate_micros_per_1k" BIGINT NOT NULL DEFAULT 0;
ALTER TABLE "workspace_provider_cost_rate_versions"
  ADD COLUMN "cache_rate_micros_per_1k" BIGINT NOT NULL DEFAULT 0;
UPDATE "platform_provider_reference_rate_releases"
SET "cache_rate_micros_per_1k"="input_rate_micros_per_1k";
UPDATE "workspace_provider_cost_rate_versions"
SET "cache_rate_micros_per_1k"="input_rate_micros_per_1k";
ALTER TABLE "platform_provider_reference_rate_releases"
  ADD CONSTRAINT "ck_platform_provider_reference_rate_releases_cache_rate"
  CHECK ("cache_rate_micros_per_1k">=0) NOT VALID;
ALTER TABLE "workspace_provider_cost_rate_versions"
  ADD CONSTRAINT "ck_workspace_provider_cost_rate_versions_cache_rate"
  CHECK ("cache_rate_micros_per_1k">=0) NOT VALID;
ALTER TABLE "platform_provider_reference_rate_releases"
  VALIDATE CONSTRAINT "ck_platform_provider_reference_rate_releases_cache_rate";
ALTER TABLE "workspace_provider_cost_rate_versions"
  VALIDATE CONSTRAINT "ck_workspace_provider_cost_rate_versions_cache_rate";
