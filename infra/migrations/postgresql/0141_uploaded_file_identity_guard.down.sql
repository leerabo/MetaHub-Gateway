-- Restore the previous guard without deleting file or extraction facts.
CREATE OR REPLACE FUNCTION fn_trg_uploaded_files_state_update_prd072() RETURNS trigger
LANGUAGE plpgsql AS $trigger$
BEGIN
  IF NEW.ctid<>OLD.ctid
    OR NEW.file_id<>OLD.file_id OR NEW.tenant_id<>OLD.tenant_id
    OR NEW.uploaded_by<>OLD.uploaded_by
    OR NEW.source_request_id<>OLD.source_request_id OR NEW.source_trace_id<>OLD.source_trace_id
    OR NEW.original_name_ciphertext IS DISTINCT FROM OLD.original_name_ciphertext
    OR NEW.file_extension<>OLD.file_extension OR NEW.media_type<>OLD.media_type
    OR NEW.size_bytes<>OLD.size_bytes OR NEW.content_fingerprint<>OLD.content_fingerprint
    OR NEW.virus_scan_status<>OLD.virus_scan_status OR NEW.created_at<>OLD.created_at
    OR (OLD.status='processing' AND NEW.status NOT IN ('ready','blocked','failed'))
    OR (OLD.status<>'processing' AND (
      NEW.status<>OLD.status OR NEW.dlp_scan_status<>OLD.dlp_scan_status
      OR NEW.dlp_action IS DISTINCT FROM OLD.dlp_action
      OR NEW.effective_dlp_rule_id IS DISTINCT FROM OLD.effective_dlp_rule_id
      OR NEW.match_count<>OLD.match_count
      OR NEW.ruleset_fingerprint IS DISTINCT FROM OLD.ruleset_fingerprint
      OR NEW.storage_backend IS DISTINCT FROM OLD.storage_backend
      OR NEW.original_object_key IS DISTINCT FROM OLD.original_object_key
      OR NEW.failure_error_code IS DISTINCT FROM OLD.failure_error_code
      OR NEW.updated_at<>OLD.updated_at OR NEW.completed_at IS DISTINCT FROM OLD.completed_at
    ))
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='file_state_immutable';
  END IF;
  RETURN NEW;
END;
$trigger$;
