-- Budget limits may use a Shanghai calendar day, month, or year. Existing
-- rules keep their daily behavior. The amount column retains its historical
-- name for storage compatibility; budget_period defines its actual window.
ALTER TABLE budget_rules ADD COLUMN budget_period TEXT NOT NULL DEFAULT 'day'
  CHECK (budget_period IN ('day', 'month', 'year'));
