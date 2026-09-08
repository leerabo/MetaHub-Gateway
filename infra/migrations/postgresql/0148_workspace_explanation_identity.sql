LOCK TABLE request_explanations,tenant_deletion_table_manifest,tenant_deletion_trigger_manifest IN ACCESS EXCLUSIVE MODE;
DO $guard$ BEGIN
  IF EXISTS(
    WITH expected(source_table,name,function,bits,sha256,legacy_sha256) AS (VALUES ('request_explanations','trg_request_explanations_owner_insert_prd054','fn_trg_request_explanations_owner_insert_prd054',7,'6ff9f6cafce5080bf4e72076d5c033077a9bae91f6fd0ffbf53759ec72ebfa6a','d67ff8f0d3e646674b6687d9bac4e78a87fb863a1c373af981adede632ede79e'),
('request_explanations','trg_request_explanations_conflict_insert_prd054','fn_trg_request_explanations_conflict_insert_prd054',7,'94cfa26063c460adff1c0f8c66269eccdbe2fc2f743f864d28f5526e529182f4','9f1a7e3a54a441c9ceedf900c6669f490fcbc13f62f5807532f57e868e68f085'),
('request_explanations','trg_request_explanations_rowid_insert_prd054','fn_trg_request_explanations_rowid_insert_prd054',5,'b893853cde9da3d965fb648c7db7d7eae5c7e50f83198d9eb64c0cd04460661f','1a0061627c7f76c492304a4245caff7fd01fd877c84e6c7d8f9e208b6a3bd9b6'),
('request_explanations','trg_request_explanations_state_update_prd054','fn_trg_request_explanations_state_update_prd054',19,'f627b5d2351f61625ce719c83f5004af80bf37ce51988058b3fe9426070133b2','a78d2b9478da13319623704cdf3f99854899852b74c47b267eb18f66d1f1de2c'),
('request_explanations','trg_request_explanations_immutable_delete_prd054','fn_trg_request_explanations_immutable_delete_prd054',11,'4c8754b6b0c72ac83b7e5948a96d8fa0f09c0bcad0ff0e853b60d63cb766321c','2703791bc5b6a130842d218f9f1a7bd3017c9c057f0827cfa39afa149863a4dd')), actual AS (
      SELECT relation.relname AS source_table,trigger.tgname,trigger.tgenabled,trigger.tgtype,trigger.tgnargs,trigger.tgqual,
        trigger.tgattr,trigger.tgconstraint,routine.proname,routine.prosrc,routine.prosecdef,
        routine.proconfig,routine.proisstrict,routine.proleakproof,routine.provolatile,
        routine.proparallel,routine.pronamespace,routine.prorettype,routine.prolang,
        pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid) AS sql
      FROM pg_trigger trigger JOIN pg_proc routine ON routine.oid=trigger.tgfoid
      JOIN pg_class relation ON relation.oid=trigger.tgrelid
      WHERE trigger.tgrelid IN('request_explanations'::regclass)
        AND NOT trigger.tgisinternal
    ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest
      WHERE source_table IN('request_explanations'))
    SELECT 1 FROM expected FULL JOIN actual ON expected.name=actual.tgname
      FULL JOIN recorded ON recorded.trigger_name=coalesce(expected.name,actual.tgname)
    WHERE expected.name IS NULL OR actual.tgname IS NULL OR actual.proname<>expected.function
      OR actual.source_table<>expected.source_table OR recorded.trigger_name IS NULL
      OR recorded.source_table<>expected.source_table
      OR metahub_sha256(recorded.trigger_sql)<>recorded.trigger_sha256
      OR NOT(recorded.trigger_sql=actual.sql
        OR (TRUE AND recorded.trigger_sha256=expected.legacy_sha256))
      OR actual.tgenabled<>'O' OR actual.tgtype<>expected.bits OR actual.tgnargs<>0
      OR actual.tgqual IS NOT NULL OR actual.tgattr::text<>'' OR actual.tgconstraint<>0
      OR metahub_sha256(actual.prosrc)<>expected.sha256 OR actual.prosecdef
      OR actual.proconfig IS NOT NULL OR actual.proisstrict OR actual.proleakproof
      OR actual.provolatile<>'v' OR actual.proparallel<>'u'
      OR actual.pronamespace<>current_schema()::regnamespace OR actual.prorettype<>'trigger'::regtype
      OR actual.prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
  ) OR (SELECT count(*) FROM tenant_deletion_table_manifest
      WHERE source_table IN('request_explanations'))<>1
    OR (SELECT array_agg(attname::text ORDER BY attnum) FROM pg_attribute
    WHERE attrelid='request_explanations'::regclass AND attnum>0 AND NOT attisdropped)
    IS DISTINCT FROM ARRAY['request_explanation_id','event_id','tenant_id','source_request_id','source_trace_id','actor_user_id','actor_session_id','source_status','status','evidence_version','section_status_json','missing_sections_json','truncated_sections_json','source_created_at','source_updated_at','operation_request_id','operation_trace_id','event_dispatch_status','event_dispatch_attempts','event_next_attempt_at','event_dispatched_at','event_claim_token','event_lease_owner_id','event_lease_expires_at','event_last_error_code','viewed_at','created_at','updated_at']::text[]
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_explanation_schema_drift'; END IF;
END; $guard$;
ALTER TABLE request_explanations ALTER COLUMN actor_session_id DROP NOT NULL;
ALTER TABLE request_explanations ADD COLUMN workspace_session_id TEXT REFERENCES workspace_sessions(workspace_session_id);
ALTER TABLE request_explanations ADD CONSTRAINT ck_explanation_session_domain_prd147
  CHECK((actor_session_id IS NULL)<>(workspace_session_id IS NULL));
CREATE OR REPLACE FUNCTION fn_trg_request_explanations_owner_insert_prd054() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  IF (NOT EXISTS ( SELECT 1 FROM request_logs request WHERE request.tenant_id=NEW.tenant_id AND request.request_id=NEW.source_request_id AND request.trace_id=NEW.source_trace_id AND request.status=NEW.source_status AND request.created_at=NEW.source_created_at AND coalesce(request.updated_at, request.created_at)=NEW.source_updated_at ) OR NOT EXISTS ( SELECT 1 FROM users user_row WHERE user_row.tenant_id=NEW.tenant_id AND user_row.user_id=NEW.actor_user_id ) OR NOT (
  (NEW.workspace_session_id IS NULL AND NEW.actor_session_id IS NOT NULL AND EXISTS (
  SELECT 1 FROM sessions session
  WHERE session.tenant_id=NEW.tenant_id
    AND session.user_id=NEW.actor_user_id
    AND session.session_id=NEW.actor_session_id
))
  OR (NEW.actor_session_id IS NULL AND NEW.workspace_session_id IS NOT NULL AND EXISTS(
  SELECT 1 FROM workspace_sessions session
  JOIN workspace_memberships member ON member.workspace_membership_id=session.membership_id
  JOIN workspace_tenant_bindings binding ON binding.workspace_id=session.workspace_id
  JOIN users actor ON actor.user_id=member.legacy_user_id
  WHERE session.workspace_session_id=NEW.workspace_session_id
    AND session.tenant_id=NEW.tenant_id AND member.tenant_id=NEW.tenant_id
    AND binding.tenant_id=NEW.tenant_id AND actor.tenant_id=NEW.tenant_id
    AND member.workspace_id=session.workspace_id AND member.account_id=session.account_id
    AND member.legacy_user_id=NEW.actor_user_id
))
) OR (SELECT COUNT(*) FROM json_each(NEW.section_status_json))<>11 OR EXISTS ( SELECT 1 FROM json_each(NEW.section_status_json) section WHERE section.key NOT IN ( 'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing', 'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks' ) OR section.type<>'text' OR section.value NOT IN ('complete', 'partial', 'not_recorded', 'not_applicable', 'pending') ) OR EXISTS ( SELECT 1 FROM json_each(NEW.missing_sections_json) section WHERE section.type<>'text' OR section.value NOT IN ( 'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing', 'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks' ) ) OR EXISTS ( SELECT 1 FROM json_each(NEW.truncated_sections_json) section WHERE section.type<>'text' OR section.value NOT IN ( 'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing', 'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks' ) ) OR (SELECT COUNT(*) FROM json_each(NEW.missing_sections_json)) <>(SELECT COUNT(DISTINCT value) FROM json_each(NEW.missing_sections_json)) OR (SELECT COUNT(*) FROM json_each(NEW.truncated_sections_json)) <>(SELECT COUNT(DISTINCT value) FROM json_each(NEW.truncated_sections_json)) OR EXISTS ( SELECT 1 FROM json_each(NEW.missing_sections_json) missing JOIN json_each(NEW.truncated_sections_json) truncated ON truncated.value=missing.value ) OR ( NEW.status='complete' AND ( json_array_length(NEW.missing_sections_json)<>0 OR json_array_length(NEW.truncated_sections_json)<>0 ) ) OR ( NEW.status='partial' AND json_array_length(NEW.missing_sections_json)=0 AND json_array_length(NEW.truncated_sections_json)=0 )) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='request_explanation_owner_mismatch';
  END IF;
  RETURN NEW;
END;
$trigger$;
CREATE OR REPLACE FUNCTION fn_trg_request_explanations_state_update_prd054() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  IF (NOT ( NEW.request_explanation_id=OLD.request_explanation_id AND NEW.event_id=OLD.event_id AND NEW.tenant_id=OLD.tenant_id AND NEW.source_request_id=OLD.source_request_id AND NEW.source_trace_id=OLD.source_trace_id AND NEW.actor_user_id=OLD.actor_user_id AND NEW.actor_session_id IS NOT DISTINCT FROM OLD.actor_session_id AND NEW.workspace_session_id IS NOT DISTINCT FROM OLD.workspace_session_id AND NEW.source_status=OLD.source_status AND NEW.status=OLD.status AND NEW.evidence_version=OLD.evidence_version AND NEW.section_status_json=OLD.section_status_json AND NEW.missing_sections_json=OLD.missing_sections_json AND NEW.truncated_sections_json=OLD.truncated_sections_json AND NEW.source_created_at=OLD.source_created_at AND NEW.source_updated_at=OLD.source_updated_at AND NEW.operation_request_id=OLD.operation_request_id AND NEW.operation_trace_id=OLD.operation_trace_id AND NEW.viewed_at=OLD.viewed_at AND NEW.created_at=OLD.created_at AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at) AND ( (OLD.event_dispatch_status='pending' AND NEW.event_dispatch_status='processing' AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1 AND unixepoch(OLD.event_next_attempt_at)<=unixepoch(NEW.updated_at) AND NEW.event_next_attempt_at IS NULL AND NEW.event_claim_token IS DISTINCT FROM OLD.event_claim_token) OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='processing' AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1 AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at) AND NEW.event_next_attempt_at IS NULL AND NEW.event_claim_token IS DISTINCT FROM OLD.event_claim_token) OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='pending' AND OLD.event_dispatch_attempts<10 AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts AND NEW.event_next_attempt_at IS NOT NULL AND unixepoch(NEW.event_next_attempt_at)>unixepoch(NEW.updated_at) AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NOT NULL) OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='dispatched' AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts AND NEW.event_dispatched_at IS NOT NULL AND NEW.event_next_attempt_at IS NULL AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NULL) OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='failed' AND NEW.event_dispatch_attempts=10 AND NEW.event_next_attempt_at IS NULL AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NOT NULL) ) )) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='request_explanation_state_invalid';
  END IF;
  RETURN NEW;
END;
$trigger$;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute
  LEFT JOIN pg_attrdef default_value ON default_value.adrelid=attribute.attrelid
    AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='request_explanations'::regclass
    AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='request_explanations'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='request_explanations'::regclass AND NOT tgisinternal
)
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256('CREATE TABLE request_explanations ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  column_signature_sha256=metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),''))
WHERE source_table='request_explanations';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
UPDATE tenant_deletion_trigger_manifest manifest SET
  trigger_sql=pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid))
FROM pg_trigger trigger WHERE trigger.tgrelid='request_explanations'::regclass
  AND manifest.source_table='request_explanations' AND manifest.trigger_name=trigger.tgname;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
