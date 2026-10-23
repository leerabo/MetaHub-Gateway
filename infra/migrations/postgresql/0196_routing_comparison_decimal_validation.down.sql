-- Forward repair only. Retain the valid decimal guards when rolling back
-- application code; never reinstate a constraint that rejects gateway facts.
SELECT 1;
