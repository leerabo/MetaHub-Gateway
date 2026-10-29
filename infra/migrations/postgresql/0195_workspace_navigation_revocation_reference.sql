-- PostgreSQL 0184 altered constraints in place without renaming preferences.
-- Its existing revocation trigger already references the live table.
-- Keep migration versions aligned without rewriting any business data.
SELECT 1;
