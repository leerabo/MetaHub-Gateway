DO $$
BEGIN
  IF EXISTS(SELECT 1 FROM assurance_receipt_archives) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='security_assurance_archive_retained';
  END IF;
END;
$$;
DROP TRIGGER trg_tenant_deletion_manifest_prd137_delete ON tenant_deletion_manifest_extensions_prd137;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089 ON tenant_deletion_table_manifest;
DELETE FROM tenant_deletion_manifest_extensions_prd137 WHERE source_table='assurance_receipt_archives';
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table='assurance_receipt_archives';
DELETE FROM tenant_deletion_table_manifest WHERE source_table='assurance_receipt_archives';
CREATE TRIGGER trg_tenant_deletion_manifest_prd137_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd137
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_manifest_prd137_delete();
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_delete_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_delete_prd089();
DROP TABLE assurance_receipt_archives;
DROP FUNCTION fn_assurance_archive_insert_prd137();
DROP FUNCTION fn_assurance_archive_immutable_prd137();
