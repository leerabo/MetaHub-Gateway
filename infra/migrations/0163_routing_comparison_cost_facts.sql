-- metahub: routing-comparison-cost-facts-v1
-- Preserve provider-currency billing facts while recording the normalized amount
-- used by cost-first ordering.
ALTER TABLE routing_candidates ADD COLUMN comparison_currency TEXT
  CHECK (comparison_currency IS NULL OR comparison_currency IN ('CNY'));
ALTER TABLE routing_candidates ADD COLUMN comparison_fx_rate TEXT
  CHECK (
    comparison_fx_rate IS NULL
    OR (
      length(comparison_fx_rate) BETWEEN 1 AND 80
      AND comparison_fx_rate NOT GLOB '*[^0-9.]*'
      AND CAST(comparison_fx_rate AS REAL) > 0
    )
  );
ALTER TABLE routing_candidates ADD COLUMN comparison_fx_rate_version TEXT;
ALTER TABLE routing_candidates ADD COLUMN comparison_cost_amount TEXT
  CHECK (
    comparison_cost_amount IS NULL
    OR (
      length(comparison_cost_amount) BETWEEN 17 AND 80
      AND comparison_cost_amount NOT GLOB '*[^0-9.]*'
      AND instr(comparison_cost_amount, '.')>1
      AND instr(substr(comparison_cost_amount, instr(comparison_cost_amount, '.')+1), '.')=0
      AND length(substr(comparison_cost_amount, instr(comparison_cost_amount, '.')+1))=15
    )
  );
