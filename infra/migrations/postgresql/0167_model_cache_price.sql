-- metahub: model-cache-price-v1
ALTER TABLE "models"
  ADD COLUMN "cache_price_per_1k" NUMERIC NOT NULL DEFAULT 0
  CHECK (cache_price_per_1k >= 0);
ALTER TABLE "model_price_plans"
  ADD COLUMN "cache_price_per_1k" NUMERIC NOT NULL DEFAULT 0
  CHECK (cache_price_per_1k >= 0);
ALTER TABLE "price_snapshots"
  ADD COLUMN "cache_price_per_1k" NUMERIC NOT NULL DEFAULT 0
  CHECK (cache_price_per_1k >= 0);
UPDATE "models" SET "cache_price_per_1k"="input_price_per_1k";
UPDATE "model_price_plans" SET "cache_price_per_1k"="input_price_per_1k";
UPDATE "price_snapshots" SET "cache_price_per_1k"="input_price_per_1k";
