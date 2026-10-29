-- metahub: workspace-cache-price-v1
-- Workspace/provider accounting prices must carry an explicit cached-input rate;
-- old rows are backfilled from the historical input rate for deterministic replay.
ALTER TABLE platform_provider_reference_rate_releases
  ADD COLUMN cache_rate_micros_per_1k INTEGER NOT NULL DEFAULT 0
  CHECK (cache_rate_micros_per_1k >= 0);

ALTER TABLE workspace_provider_cost_rate_versions
  ADD COLUMN cache_rate_micros_per_1k INTEGER NOT NULL DEFAULT 0
  CHECK (cache_rate_micros_per_1k >= 0);

UPDATE platform_provider_reference_rate_releases
SET cache_rate_micros_per_1k=input_rate_micros_per_1k;
UPDATE workspace_provider_cost_rate_versions
SET cache_rate_micros_per_1k=input_rate_micros_per_1k;
