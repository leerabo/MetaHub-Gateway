DROP INDEX IF EXISTS "idx_virtual_keys_project_status";
DROP INDEX IF EXISTS "idx_virtual_key_binding_project_time";
DROP INDEX IF EXISTS "idx_virtual_key_binding_current";
DROP TABLE IF EXISTS "virtual_key_binding_history";
ALTER TABLE "virtual_keys" DROP COLUMN IF EXISTS "disabled_reason";
