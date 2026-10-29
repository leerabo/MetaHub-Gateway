-- metahub: stream-retry-owners-v1
-- A handshake fallback is a second provider attempt for the same request.  Keep
-- each attempt's stream and output-safety owner distinct while preserving the
-- tenant/request/owner composite keys used by the existing foreign keys.

PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 0027 left a stricter index behind after 0071 introduced the attempt owner key.
DROP INDEX IF EXISTS idx_stream_sessions_tenant_request;

-- The old output-safety table also encoded one scan per request. Rebuild it
-- without that obsolete constraint; all other checks and composite owner keys
-- remain unchanged.
DROP TRIGGER IF EXISTS trg_output_safety_scans_cache_candidate_prd076;
DROP TRIGGER IF EXISTS trg_output_safety_scans_conflict_insert_prd071;
DROP TRIGGER IF EXISTS trg_output_safety_scans_delete_prd071;
DROP TRIGGER IF EXISTS trg_output_safety_scans_owner_insert_prd071;
DROP TRIGGER IF EXISTS trg_output_safety_scans_owner_update_prd071;
DROP TRIGGER IF EXISTS trg_output_safety_scans_rule_update_prd071;
DROP TRIGGER IF EXISTS trg_output_safety_scans_state_insert_prd071;
DROP TRIGGER IF EXISTS trg_output_safety_scans_transition_prd071;

DROP TABLE IF EXISTS output_safety_scans_prd170_new;
CREATE TABLE output_safety_scans_prd170_new (
  output_safety_scan_id TEXT PRIMARY KEY CHECK (
    length(output_safety_scan_id)=36
    AND substr(output_safety_scan_id,1,4)='oss_'
    AND substr(output_safety_scan_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128 AND instr(request_id,char(0))=0
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128 AND instr(trace_id,char(0))=0
  ),
  stream_session_id TEXT,
  scan_mode TEXT NOT NULL CHECK (scan_mode IN ('non_stream_full','stream_window')),
  scan_status TEXT NOT NULL CHECK (
    scan_status IN ('pending','not_configured','completed','unavailable','not_applicable')
  ),
  decision TEXT NOT NULL CHECK (decision IN ('none','clean','matched','blocked','unknown')),
  action TEXT CHECK (action IS NULL OR action IN ('allow','log_only','block')),
  dlp_rule_id TEXT,
  rule_type TEXT CHECK (rule_type IS NULL OR rule_type IN ('keyword','regex')),
  match_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(match_count)='integer' AND match_count>=0),
  ruleset_fingerprint TEXT CHECK (
    ruleset_fingerprint IS NULL OR (
      length(ruleset_fingerprint)=64 AND ruleset_fingerprint NOT GLOB '*[^0-9a-f]*'
    )
  ),
  detector_version TEXT NOT NULL DEFAULT 'output-safety-dlp-v1'
    CHECK (detector_version='output-safety-dlp-v1'),
  interval_tokens INTEGER CHECK (
    interval_tokens IS NULL OR (typeof(interval_tokens)='integer' AND interval_tokens BETWEEN 8 AND 256)
  ),
  window_tokens INTEGER CHECK (
    window_tokens IS NULL OR (typeof(window_tokens)='integer' AND window_tokens BETWEEN 32 AND 1024)
  ),
  checkpoint_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(checkpoint_count)='integer' AND checkpoint_count>=0),
  observed_output_token_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(observed_output_token_count)='integer' AND observed_output_token_count>=0),
  scanned_token_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(scanned_token_count)='integer' AND scanned_token_count>=0),
  emitted_token_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(emitted_token_count)='integer' AND emitted_token_count>=0),
  emitted_sse_event_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(emitted_sse_event_count)='integer' AND emitted_sse_event_count>=0),
  emitted_bytes INTEGER NOT NULL DEFAULT 0 CHECK (typeof(emitted_bytes)='integer' AND emitted_bytes>=0),
  exposure_state TEXT NOT NULL CHECK (exposure_state IN ('none','partial','complete','unknown')),
  billing_disposition TEXT NOT NULL CHECK (
    billing_disposition IN ('not_required','dispute_pending','not_billable_response_cache_candidate')
  ),
  failure_reason_code TEXT CHECK (
    failure_reason_code IS NULL OR failure_reason_code IN (
      'output_safety.fragment_limit_exceeded','output_safety.response_shape_invalid',
      'output_safety.response_too_large','output_safety.evidence_unavailable',
      'output_safety.rule_invalid','output_safety.rule_snapshot_unavailable',
      'output_safety.sse_event_incomplete','output_safety.sse_event_too_large',
      'output_safety.sse_json_duplicate_key','output_safety.sse_json_invalid',
      'output_safety.sse_utf8_invalid','output_safety.stream_incomplete',
      'output_safety.window_capacity_exceeded'
    )
  ),
  started_at TEXT NOT NULL CHECK (length(started_at) BETWEEN 1 AND 64 AND unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK (
    completed_at IS NULL OR (
      length(completed_at) BETWEEN 1 AND 64 AND unixepoch(completed_at) IS NOT NULL
      AND unixepoch(completed_at)>=unixepoch(started_at)
    )
  ),
  UNIQUE (tenant_id,output_safety_scan_id),
  UNIQUE (tenant_id,request_id,output_safety_scan_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,request_id) REFERENCES gateway_request_claims(tenant_id,request_id),
  FOREIGN KEY (tenant_id,dlp_rule_id) REFERENCES dlp_rules(tenant_id,dlp_rule_id),
  FOREIGN KEY (tenant_id,request_id,stream_session_id)
    REFERENCES stream_sessions(tenant_id,request_id,stream_session_id),
  CHECK (
    (scan_mode='non_stream_full' AND stream_session_id IS NULL
      AND interval_tokens IS NULL AND window_tokens IS NULL)
    OR (scan_mode='stream_window' AND stream_session_id IS NOT NULL
      AND interval_tokens IS NOT NULL AND window_tokens IS NOT NULL
      AND window_tokens>=interval_tokens)
  ),
  CHECK (emitted_token_count<=scanned_token_count),
  CHECK (scanned_token_count<=observed_output_token_count),
  CHECK (scan_status!='completed' OR checkpoint_count>=1),
  CHECK (
    scan_status!='completed' OR decision!='blocked' OR scan_mode!='non_stream_full'
    OR (exposure_state='none' AND emitted_token_count=0
      AND emitted_sse_event_count=0 AND emitted_bytes=0)
  ),
  CHECK (
    (scan_status='pending' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NULL)
    OR
    (scan_status='not_configured' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NOT NULL)
    OR
    (scan_status='not_applicable' AND decision='none' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND checkpoint_count=0
      AND observed_output_token_count=0 AND scanned_token_count=0
      AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0
      AND exposure_state='unknown' AND billing_disposition='not_required'
      AND failure_reason_code IS NULL AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='clean' AND action IS NULL
      AND dlp_rule_id IS NULL AND rule_type IS NULL AND match_count=0
      AND ruleset_fingerprint IS NOT NULL AND exposure_state='complete'
      AND billing_disposition='not_required' AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='matched' AND action IN ('allow','log_only')
      AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL AND match_count>=1
      AND ruleset_fingerprint IS NOT NULL AND exposure_state='complete'
      AND billing_disposition='not_required' AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL)
    OR
    (scan_status='completed' AND decision='blocked' AND action='block'
      AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL AND match_count>=1
      AND ruleset_fingerprint IS NOT NULL AND failure_reason_code IS NULL
      AND completed_at IS NOT NULL AND (
        (billing_disposition='dispute_pending' AND exposure_state IN ('none','partial'))
        OR (billing_disposition='not_billable_response_cache_candidate'
          AND scan_mode='non_stream_full' AND exposure_state='none'
          AND emitted_token_count=0 AND emitted_sse_event_count=0 AND emitted_bytes=0)
      ))
    OR
    (scan_status='unavailable' AND decision IN ('unknown','matched')
      AND exposure_state IN ('none','partial','unknown')
      AND billing_disposition='not_required' AND failure_reason_code IS NOT NULL
      AND completed_at IS NOT NULL AND (
        (decision='unknown' AND action IS NULL AND dlp_rule_id IS NULL
          AND rule_type IS NULL AND match_count=0)
        OR (decision='matched' AND action IN ('allow','log_only')
          AND dlp_rule_id IS NOT NULL AND rule_type IS NOT NULL AND match_count>=1
          AND ruleset_fingerprint IS NOT NULL)
      ))
  )
);

INSERT INTO output_safety_scans_prd170_new(
  output_safety_scan_id,tenant_id,request_id,trace_id,stream_session_id,scan_mode,
  scan_status,decision,action,dlp_rule_id,rule_type,match_count,ruleset_fingerprint,
  detector_version,interval_tokens,window_tokens,checkpoint_count,observed_output_token_count,
  scanned_token_count,emitted_token_count,emitted_sse_event_count,emitted_bytes,
  exposure_state,billing_disposition,failure_reason_code,started_at,completed_at
)
SELECT output_safety_scan_id,tenant_id,request_id,trace_id,stream_session_id,scan_mode,
  scan_status,decision,action,dlp_rule_id,rule_type,match_count,ruleset_fingerprint,
  detector_version,interval_tokens,window_tokens,checkpoint_count,observed_output_token_count,
  scanned_token_count,emitted_token_count,emitted_sse_event_count,emitted_bytes,
  exposure_state,billing_disposition,failure_reason_code,started_at,completed_at
FROM output_safety_scans;

DROP TABLE output_safety_scans;
ALTER TABLE output_safety_scans_prd170_new RENAME TO output_safety_scans;

CREATE INDEX idx_output_safety_scans_tenant_status_time_prd071
  ON output_safety_scans(tenant_id,scan_status,decision,started_at DESC);
CREATE INDEX idx_output_safety_scans_tenant_time_prd071
  ON output_safety_scans(tenant_id,started_at DESC,output_safety_scan_id DESC);

CREATE TRIGGER trg_output_safety_scans_conflict_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM output_safety_scans current
  WHERE current.output_safety_scan_id=NEW.output_safety_scan_id
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_immutable'); END;
CREATE TRIGGER trg_output_safety_scans_delete_prd071
BEFORE DELETE ON output_safety_scans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_scan_immutable'); END;
CREATE TRIGGER trg_output_safety_scans_owner_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id AND claim.request_id=NEW.request_id
      AND claim.trace_id=NEW.trace_id
  )
  OR (NEW.scan_mode='stream_window' AND NOT EXISTS (
    SELECT 1 FROM stream_sessions session
    WHERE session.tenant_id=NEW.tenant_id AND session.request_id=NEW.request_id
      AND session.stream_session_id=NEW.stream_session_id
      AND session.trace_id=NEW.trace_id
  ))
  OR (NEW.dlp_rule_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM dlp_rules rule
    WHERE rule.tenant_id=NEW.tenant_id AND rule.dlp_rule_id=NEW.dlp_rule_id
      AND rule.rule_type=NEW.rule_type
  ))
BEGIN SELECT RAISE(ABORT,'output_safety_scan_owner_mismatch'); END;
CREATE TRIGGER trg_output_safety_scans_owner_update_prd071
BEFORE UPDATE ON output_safety_scans FOR EACH ROW
WHEN NEW.rowid IS NOT OLD.rowid
  OR NEW.output_safety_scan_id IS NOT OLD.output_safety_scan_id
  OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.request_id IS NOT OLD.request_id
  OR NEW.trace_id IS NOT OLD.trace_id OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.scan_mode IS NOT OLD.scan_mode OR NEW.detector_version IS NOT OLD.detector_version
  OR NEW.interval_tokens IS NOT OLD.interval_tokens OR NEW.window_tokens IS NOT OLD.window_tokens
  OR NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT,'output_safety_scan_owner_immutable'); END;
CREATE TRIGGER trg_output_safety_scans_rule_update_prd071
BEFORE UPDATE OF dlp_rule_id,rule_type ON output_safety_scans FOR EACH ROW
WHEN NEW.dlp_rule_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM dlp_rules rule
  WHERE rule.tenant_id=NEW.tenant_id AND rule.dlp_rule_id=NEW.dlp_rule_id
    AND rule.rule_type=NEW.rule_type
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_rule_owner_mismatch'); END;
CREATE TRIGGER trg_output_safety_scans_state_insert_prd071
BEFORE INSERT ON output_safety_scans FOR EACH ROW
WHEN NEW.scan_status!='pending'
BEGIN SELECT RAISE(ABORT,'output_safety_scan_initial_state_invalid'); END;
CREATE TRIGGER trg_output_safety_scans_transition_prd071
BEFORE UPDATE ON output_safety_scans FOR EACH ROW
WHEN NOT (
  (NEW.rowid IS OLD.rowid AND NEW.output_safety_scan_id IS OLD.output_safety_scan_id
    AND NEW.tenant_id IS OLD.tenant_id AND NEW.request_id IS OLD.request_id
    AND NEW.trace_id IS OLD.trace_id AND NEW.stream_session_id IS OLD.stream_session_id
    AND NEW.scan_mode IS OLD.scan_mode AND NEW.scan_status IS OLD.scan_status
    AND NEW.decision IS OLD.decision AND NEW.action IS OLD.action
    AND NEW.dlp_rule_id IS OLD.dlp_rule_id AND NEW.rule_type IS OLD.rule_type
    AND NEW.match_count IS OLD.match_count AND NEW.ruleset_fingerprint IS OLD.ruleset_fingerprint
    AND NEW.detector_version IS OLD.detector_version AND NEW.interval_tokens IS OLD.interval_tokens
    AND NEW.window_tokens IS OLD.window_tokens AND NEW.checkpoint_count IS OLD.checkpoint_count
    AND NEW.observed_output_token_count IS OLD.observed_output_token_count
    AND NEW.scanned_token_count IS OLD.scanned_token_count
    AND NEW.emitted_token_count IS OLD.emitted_token_count
    AND NEW.emitted_sse_event_count IS OLD.emitted_sse_event_count
    AND NEW.emitted_bytes IS OLD.emitted_bytes AND NEW.exposure_state IS OLD.exposure_state
    AND NEW.billing_disposition IS OLD.billing_disposition
    AND NEW.failure_reason_code IS OLD.failure_reason_code
    AND NEW.started_at IS OLD.started_at AND NEW.completed_at IS OLD.completed_at)
  OR (OLD.scan_status='pending' AND NEW.scan_status IN ('not_configured','completed','unavailable','not_applicable'))
)
BEGIN SELECT RAISE(ABORT,'output_safety_scan_transition_invalid'); END;
CREATE TRIGGER trg_output_safety_scans_cache_candidate_prd076
BEFORE UPDATE ON output_safety_scans FOR EACH ROW
WHEN NEW.billing_disposition='not_billable_response_cache_candidate'
  AND NOT EXISTS(
    SELECT 1 FROM response_cache_request_facts fact
    WHERE fact.tenant_id=NEW.tenant_id AND fact.request_id=NEW.request_id
      AND fact.trace_id=NEW.trace_id AND fact.status='hit_candidate'
      AND NEW.scan_mode='non_stream_full' AND NEW.scan_status='completed'
      AND NEW.decision='blocked' AND NEW.exposure_state='none'
      AND NEW.emitted_token_count=0 AND NEW.emitted_sse_event_count=0
      AND NEW.emitted_bytes=0
  )
BEGIN SELECT RAISE(ABORT,'output_safety_cache_candidate_owner_mismatch'); END;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
