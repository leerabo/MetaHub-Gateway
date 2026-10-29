-- SQLite already introduced this column in 0154. Assert the source contract
-- while recording the corresponding PostgreSQL repair; no business rows change.
SELECT employee_code FROM workspace_consumers LIMIT 0;
