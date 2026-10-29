CREATE TABLE ga_gate_policies(
  ga_gate_id TEXT PRIMARY KEY CHECK(ga_gate_id ~ '^gag_[0-9a-f]{32}$'),
  launch_gate_id TEXT NOT NULL UNIQUE REFERENCES launch_gate_policies(launch_gate_id),
  gate_code TEXT NOT NULL UNIQUE,
  policy_version INTEGER NOT NULL CHECK(policy_version=1),
  verifier_code TEXT NOT NULL,
  verifier_version INTEGER NOT NULL CHECK(verifier_version=1),
  freshness_seconds INTEGER NOT NULL CHECK(freshness_seconds BETWEEN 300 AND 604800),
  blocking_severity TEXT NOT NULL CHECK(blocking_severity='p0'),
  policy_sha256 TEXT NOT NULL UNIQUE CHECK(policy_sha256 ~ '^[0-9a-f]{64}$'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
INSERT INTO ga_gate_policies
SELECT 'gag_'||substr(launch_gate_id,5),launch_gate_id,gate_code,policy_version,verifier_code,
  verifier_version,freshness_seconds,blocking_severity,policy_sha256,created_at
FROM launch_gate_policies;
CREATE FUNCTION ga_gate_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public, pg_temp AS $$
BEGIN RAISE EXCEPTION 'ga_gate_immutable'; END $$;
CREATE TRIGGER trg_ga_gate_immutable BEFORE INSERT OR UPDATE OR DELETE ON ga_gate_policies
FOR EACH ROW EXECUTE FUNCTION ga_gate_immutable();

CREATE TABLE ga_launches(
  ga_launch_id TEXT PRIMARY KEY CHECK(ga_launch_id ~ '^gal_[0-9a-f]{32}$'),
  launch_candidate_id TEXT NOT NULL UNIQUE REFERENCES launch_candidates(launch_candidate_id),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('approved','launched','rollback_pending','rolled_back','failed')),
  candidate_generation BIGINT NOT NULL CHECK(candidate_generation>=1),
  revision BIGINT NOT NULL CHECK(revision>=1),
  approved_at TEXT NOT NULL CHECK(unixepoch(approved_at) IS NOT NULL),
  launched_at TEXT CHECK(launched_at IS NULL OR unixepoch(launched_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE FUNCTION ga_launch_guard() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP='INSERT' THEN
    RETURN NEW;
  END IF;
  IF TG_OP='DELETE' THEN
    RAISE EXCEPTION 'ga_launch_immutable';
  END IF;
  IF NEW.ga_launch_id!=OLD.ga_launch_id OR NEW.launch_candidate_id!=OLD.launch_candidate_id
    OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
    OR NEW.candidate_generation!=OLD.candidate_generation OR NEW.approved_at!=OLD.approved_at
    OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
    OR NOT((OLD.status='approved' AND NEW.status IN('approved','launched','rollback_pending','failed'))
      OR (OLD.status='launched' AND NEW.status IN('launched','rollback_pending','failed'))
      OR (OLD.status='rollback_pending' AND NEW.status IN('rollback_pending','rolled_back','failed'))
      OR (OLD.status IN('rolled_back','failed') AND NEW.status=OLD.status)) THEN
    RAISE EXCEPTION 'ga_launch_transition_invalid';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_ga_launch_guard BEFORE INSERT OR UPDATE OR DELETE ON ga_launches
FOR EACH ROW EXECUTE FUNCTION ga_launch_guard();

CREATE TABLE ga_decisions(
  ga_decision_id TEXT PRIMARY KEY CHECK(ga_decision_id ~ '^gad_[0-9a-f]{32}$'),
  ga_launch_id TEXT NOT NULL REFERENCES ga_launches(ga_launch_id),
  actor_kind TEXT NOT NULL CHECK(actor_kind IN('product','sre','security','test','finance','legal')),
  operator_id TEXT NOT NULL REFERENCES platform_operators(operator_id),
  decision TEXT NOT NULL CHECK(decision='approved'),
  source_decision_id TEXT NOT NULL UNIQUE REFERENCES launch_go_no_go_decisions(go_no_go_decision_id),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  UNIQUE(ga_launch_id,actor_kind), UNIQUE(ga_launch_id,operator_id)
);
CREATE FUNCTION ga_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public, pg_temp AS $$
BEGIN IF TG_OP!='INSERT' THEN RAISE EXCEPTION 'ga_fact_immutable'; END IF; RETURN NEW; END $$;
CREATE TRIGGER trg_ga_decision_immutable BEFORE INSERT OR UPDATE OR DELETE ON ga_decisions FOR EACH ROW EXECUTE FUNCTION ga_immutable();

CREATE TABLE ga_evidence(
  ga_evidence_id TEXT PRIMARY KEY CHECK(ga_evidence_id ~ '^gae_[0-9a-f]{32}$'),
  launch_candidate_id TEXT NOT NULL REFERENCES launch_candidates(launch_candidate_id),
  ga_gate_id TEXT NOT NULL REFERENCES ga_gate_policies(ga_gate_id),
  source_evidence_id TEXT NOT NULL UNIQUE REFERENCES launch_evidence(launch_evidence_id),
  outcome TEXT NOT NULL CHECK(outcome='passed'),
  finished_at TEXT NOT NULL CHECK(unixepoch(finished_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(launch_candidate_id,ga_gate_id)
);
CREATE TRIGGER trg_ga_evidence_immutable BEFORE INSERT OR UPDATE OR DELETE ON ga_evidence FOR EACH ROW EXECUTE FUNCTION ga_immutable();

CREATE TABLE ga_rollbacks(
  ga_rollback_id TEXT PRIMARY KEY CHECK(ga_rollback_id ~ '^gar_[0-9a-f]{32}$'),
  ga_launch_id TEXT NOT NULL REFERENCES ga_launches(ga_launch_id),
  from_status TEXT NOT NULL CHECK(from_status IN('approved','launched','rollback_pending')),
  trigger_code TEXT NOT NULL CHECK(trigger_code IN('gate_failed','health_slo','payment_failure','security_finding','operator_request')),
  trigger_sha256 TEXT NOT NULL CHECK(trigger_sha256 ~ '^[0-9a-f]{64}$'),
  checkpoint_sha256 TEXT NOT NULL CHECK(checkpoint_sha256 ~ '^[0-9a-f]{64}$'),
  job_receipt_sha256 TEXT NOT NULL CHECK(job_receipt_sha256 ~ '^[0-9a-f]{64}$'),
  verification_sha256 TEXT CHECK(verification_sha256 IS NULL OR verification_sha256 ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL CHECK(status IN('pending','passed','failed')),
  revision BIGINT NOT NULL CHECK(revision>=1),
  requested_by_operator_id TEXT NOT NULL REFERENCES platform_operators(operator_id),
  verified_by_operator_id TEXT REFERENCES platform_operators(operator_id),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
CREATE FUNCTION ga_rollback_guard() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP='INSERT' THEN
    RETURN NEW;
  END IF;
  IF TG_OP='DELETE' THEN
    RAISE EXCEPTION 'ga_rollback_immutable';
  END IF;
  IF NEW.ga_rollback_id!=OLD.ga_rollback_id OR NEW.ga_launch_id!=OLD.ga_launch_id
    OR NEW.from_status!=OLD.from_status OR NEW.trigger_code!=OLD.trigger_code
    OR NEW.trigger_sha256!=OLD.trigger_sha256 OR NEW.checkpoint_sha256!=OLD.checkpoint_sha256
    OR NEW.job_receipt_sha256!=OLD.job_receipt_sha256 OR NEW.requested_by_operator_id!=OLD.requested_by_operator_id
    OR NEW.requested_at!=OLD.requested_at OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
    OR NOT((OLD.status='pending' AND NEW.status IN('pending','passed','failed'))
      OR (OLD.status IN('passed','failed') AND NEW.status=OLD.status)) THEN
    RAISE EXCEPTION 'ga_rollback_transition_invalid';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_ga_rollback_guard BEFORE INSERT OR UPDATE OR DELETE ON ga_rollbacks FOR EACH ROW EXECUTE FUNCTION ga_rollback_guard();

CREATE TABLE post_ga_reviews(
  post_ga_review_id TEXT PRIMARY KEY CHECK(post_ga_review_id ~ '^pgr_[0-9a-f]{32}$'),
  ga_launch_id TEXT NOT NULL UNIQUE REFERENCES ga_launches(ga_launch_id),
  summary_sha256 TEXT NOT NULL CHECK(summary_sha256 ~ '^[0-9a-f]{64}$'),
  action_items_sha256 TEXT NOT NULL CHECK(action_items_sha256 ~ '^[0-9a-f]{64}$'),
  conclusion TEXT NOT NULL CHECK(conclusion IN('ga_launched','ga_rolled_back')),
  reviewed_by_operator_id TEXT NOT NULL REFERENCES platform_operators(operator_id),
  reviewed_at TEXT NOT NULL CHECK(unixepoch(reviewed_at) IS NOT NULL)
);
CREATE TRIGGER trg_post_ga_review_immutable BEFORE INSERT OR UPDATE OR DELETE ON post_ga_reviews FOR EACH ROW EXECUTE FUNCTION ga_immutable();

CREATE TABLE ga_events(
  ga_event_id TEXT PRIMARY KEY CHECK(ga_event_id ~ '^gev_[0-9a-f]{32}$'),
  ga_launch_id TEXT NOT NULL REFERENCES ga_launches(ga_launch_id),
  event_type TEXT NOT NULL CHECK(event_type IN('ga.approved','ga.launched','ga.rolled_back','ga.gate_failed')),
  payload_sha256 TEXT NOT NULL CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL)
);
CREATE TABLE ga_event_outbox(
  ga_event_outbox_id TEXT PRIMARY KEY CHECK(ga_event_outbox_id ~ '^geo_[0-9a-f]{32}$'),
  ga_event_id TEXT NOT NULL UNIQUE REFERENCES ga_events(ga_event_id),
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count BIGINT NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
CREATE TABLE ga_action_commands(
  ga_action_command_id TEXT PRIMARY KEY CHECK(ga_action_command_id ~ '^gac_[0-9a-f]{32}$'),
  operator_id TEXT NOT NULL REFERENCES platform_operators(operator_id),
  action_code TEXT NOT NULL,
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL,
  result_revision BIGINT NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(operator_id,action_code,idempotency_key_sha256)
);
