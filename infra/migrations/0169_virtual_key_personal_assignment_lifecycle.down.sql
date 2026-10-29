DROP INDEX IF EXISTS idx_virtual_keys_personal_lifecycle;
ALTER TABLE virtual_keys DROP COLUMN deleted_at;
