-- metahub: routing-comparison-cost-facts-v1
ALTER TABLE "routing_candidates"
  ADD COLUMN "comparison_currency" TEXT CHECK ("comparison_currency" IS NULL OR "comparison_currency" IN ('CNY')),
  ADD COLUMN "comparison_fx_rate" TEXT,
  ADD COLUMN "comparison_fx_rate_version" TEXT,
  ADD COLUMN "comparison_cost_amount" TEXT;
ALTER TABLE "routing_candidates"
  ADD CONSTRAINT "routing_candidates_comparison_fx_rate_valid"
  CHECK (
    "comparison_fx_rate" IS NULL OR
    ("comparison_fx_rate" ~ '^[0-9]+(\\.[0-9]+)?$' AND "comparison_fx_rate"::numeric > 0)
  );
ALTER TABLE "routing_candidates"
  ADD CONSTRAINT "routing_candidates_comparison_cost_valid"
  CHECK (
    "comparison_cost_amount" IS NULL OR
    ("comparison_cost_amount" ~ '^[0-9]+\\.[0-9]{15}$')
  );
