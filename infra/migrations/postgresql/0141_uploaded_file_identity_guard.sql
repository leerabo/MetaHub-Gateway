-- Physical tuple addresses change on valid PostgreSQL file-state updates.
CREATE OR REPLACE FUNCTION fn_trg_uploaded_files_state_update_prd072() RETURNS trigger
LANGUAGE plpgsql AS $trigger$
BEGIN
  IF NEW.file_id IS DISTINCT FROM OLD.file_id
    OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
    OR NEW.uploaded_by IS DISTINCT FROM OLD.uploaded_by
    OR NEW.source_request_id IS DISTINCT FROM OLD.source_request_id
    OR NEW.source_trace_id IS DISTINCT FROM OLD.source_trace_id
    OR NEW.original_name_ciphertext IS DISTINCT FROM OLD.original_name_ciphertext
    OR NEW.file_extension IS DISTINCT FROM OLD.file_extension
    OR NEW.media_type IS DISTINCT FROM OLD.media_type
    OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
    OR NEW.content_fingerprint IS DISTINCT FROM OLD.content_fingerprint
    OR NEW.virus_scan_status IS DISTINCT FROM OLD.virus_scan_status
    OR NEW.created_at IS DISTINCT FROM OLD.created_at
    OR (OLD.status='processing' AND NEW.status NOT IN ('ready','blocked','failed'))
    OR (OLD.status<>'processing' AND (
      NEW.status IS DISTINCT FROM OLD.status
      OR NEW.dlp_scan_status IS DISTINCT FROM OLD.dlp_scan_status
      OR NEW.dlp_action IS DISTINCT FROM OLD.dlp_action
      OR NEW.effective_dlp_rule_id IS DISTINCT FROM OLD.effective_dlp_rule_id
      OR NEW.match_count IS DISTINCT FROM OLD.match_count
      OR NEW.ruleset_fingerprint IS DISTINCT FROM OLD.ruleset_fingerprint
      OR NEW.storage_backend IS DISTINCT FROM OLD.storage_backend
      OR NEW.original_object_key IS DISTINCT FROM OLD.original_object_key
      OR NEW.failure_error_code IS DISTINCT FROM OLD.failure_error_code
      OR NEW.updated_at IS DISTINCT FROM OLD.updated_at
      OR NEW.completed_at IS DISTINCT FROM OLD.completed_at
    ))
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='file_state_immutable';
  END IF;
  RETURN NEW;
END;
$trigger$;
