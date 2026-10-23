-- Standard PostgreSQL string literals preserve backslashes. Migration 0171
-- double-escaped its decimal point, rejecting the canonical decimal strings
-- emitted by the gateway. Use a character class to avoid escape ambiguity.
-- Keep all numeric bounds and precision checks; validate existing rows too.
ALTER TABLE "routing_candidates"
  DROP CONSTRAINT "routing_candidates_comparison_fx_rate_valid",
  DROP CONSTRAINT "routing_candidates_comparison_cost_valid",
  ADD CONSTRAINT "routing_candidates_comparison_fx_rate_valid"
  CHECK (
    "comparison_fx_rate" IS NULL OR
    ("comparison_fx_rate" ~ '^[0-9]+([.][0-9]+)?$'
      AND "comparison_fx_rate"::numeric > 0)
  ),
  ADD CONSTRAINT "routing_candidates_comparison_cost_valid"
  CHECK (
    "comparison_cost_amount" IS NULL OR
    "comparison_cost_amount" ~ '^[0-9]+[.][0-9]{15}$'
  );
