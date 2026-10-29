ALTER TABLE "routing_candidates"
  DROP CONSTRAINT IF EXISTS "routing_candidates_comparison_cost_valid";
ALTER TABLE "routing_candidates"
  DROP CONSTRAINT IF EXISTS "routing_candidates_comparison_fx_rate_valid";
ALTER TABLE "routing_candidates"
  DROP COLUMN IF EXISTS "comparison_cost_amount",
  DROP COLUMN IF EXISTS "comparison_fx_rate_version",
  DROP COLUMN IF EXISTS "comparison_fx_rate",
  DROP COLUMN IF EXISTS "comparison_currency";
