-- SQLite already retains these DEFERRABLE INITIALLY DEFERRED contracts.
-- PostgreSQL requires the forward-only parity repair under this version.
SELECT 1;
