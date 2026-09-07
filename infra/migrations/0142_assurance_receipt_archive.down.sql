-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE assurance_archive_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO assurance_archive_down_guard
SELECT COUNT(*) FROM assurance_receipt_archives;
DROP TABLE assurance_archive_down_guard;
DROP TRIGGER trg_tenant_deletion_manifest_prd137_delete;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DELETE FROM tenant_deletion_manifest_extensions_prd137 WHERE source_table='assurance_receipt_archives';
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table='assurance_receipt_archives';
DELETE FROM tenant_deletion_table_manifest WHERE source_table='assurance_receipt_archives';
CREATE TRIGGER trg_tenant_deletion_manifest_prd137_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd137
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
DROP TABLE assurance_receipt_archives;

COMMIT;
PRAGMA foreign_keys = ON;
