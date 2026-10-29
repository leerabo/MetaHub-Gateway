ALTER TABLE budget_rules ADD COLUMN budget_period TEXT NOT NULL DEFAULT 'day'
  CHECK (budget_period IN ('day', 'month', 'year'));
