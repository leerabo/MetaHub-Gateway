LOCK TABLE request_log_query_events,audit_exports,audit_export_events,saved_filters,tenant_deletion_table_manifest,tenant_deletion_trigger_manifest IN ACCESS EXCLUSIVE MODE;
DO $guard$ BEGIN
  IF EXISTS(
    WITH expected(source_table,name,function,bits,sha256,legacy_sha256) AS (VALUES ('saved_filters','trg_saved_filters_owner_insert_prd055','fn_trg_saved_filters_owner_insert_prd055',7,'1984bf27b1b77ab03c641130e09664764affad8e46ce4fc82ba79b062f2d4409','2383b5ecdc03cffddb84d669637b2094feb012c90745df8c90c74de1202ab1e0'),
('saved_filters','trg_saved_filters_state_insert_prd055','fn_trg_saved_filters_state_insert_prd055',7,'228a5b751b31ab80b09585582cc964af17dfe919d37555f933453c139770b4c9','0dd0d122d691557bf8da7f8df62e71e435981817600de65103e02ed63a9943f7'),
('saved_filters','trg_saved_filters_payload_insert_prd055','fn_trg_saved_filters_payload_insert_prd055',7,'183f311b4604e64dfcbe65b7d853fc82b5b1df00222eaef5f9b2f1b9ac676d2c','9678e2e45dfe69a8c19a4b31f2d724687b781f6c5bebf798d4853323930e4c20'),
('saved_filters','trg_saved_filters_conflict_insert_prd055','fn_trg_saved_filters_conflict_insert_prd055',7,'0a2bcff427d047c8f6c329fb1df1cfce9c825b5bbdc032147e7ffbf7ebf86b21','95f9aedc20062236028a0bafd92df3a13cbcbbdc917167c8d5eb2ad8be2a8459'),
('saved_filters','trg_saved_filters_rowid_insert_prd055','fn_trg_saved_filters_rowid_insert_prd055',5,'a2a16c9500e0796c55a5fae418d8f1ff6b03eaa79e0a604496c4adc819af0800','8829e96f71d0791de6f52799b645522106d0732619b6cca5d16bd9d325090a8d'),
('saved_filters','trg_saved_filters_state_update_prd055','fn_trg_saved_filters_state_update_prd055',19,'3ee1b24155650dc3d5f4f3053eff496e31be05a594d0386560085283d038a300','4c60fb7b579a0ce2467cca4e706b933160babba13ba4b5d9e13bafe6d85d14e6'),
('saved_filters','trg_saved_filters_payload_update_prd055','fn_trg_saved_filters_payload_update_prd055',19,'183f311b4604e64dfcbe65b7d853fc82b5b1df00222eaef5f9b2f1b9ac676d2c','a90aacf2fbb54c9c6f6bb97a12693317fdaf06d5808726ddb7edf4935d1f296a'),
('saved_filters','trg_saved_filters_immutable_delete_prd055','fn_trg_saved_filters_immutable_delete_prd055',11,'678cecee51a9380a222ef0c1b80229fca358222221f00c0c64aeba7822cc9ec4','2a85cd5583e37222fe2ba815a9fbe4bec7e5a29e6e6365aecd290605fad36027'),
('request_log_query_events','trg_request_log_query_events_owner_insert_prd055','fn_trg_request_log_query_events_owner_insert_prd055',7,'eefa08aa67fb3901d4ee53f534245bd717262e688d21e5d5b1ecd0b492141d24','990840803c7e48284849f48f3cc9a16ec48d19a46745d52117daaa5545d1152f'),
('request_log_query_events','trg_request_log_query_events_state_insert_prd055','fn_trg_request_log_query_events_state_insert_prd055',7,'1ad9df089d3ff436e0a589b73b64d7c159f208aaa6b6a440cf77253e9ec49531','97b70ea959bd3bfbdfe603c1b41133519b6c36f3a3f6ee2305ac556a1d4c344c'),
('request_log_query_events','trg_request_log_query_events_conflict_insert_prd055','fn_trg_request_log_query_events_conflict_insert_prd055',7,'df0666a1f285e18561b9b6b25e8e066f9c2c32bc283626c06128230a5199d8ba','58a0228bfce5e304e278e2213bfdcf5b81703fc2b76301e755adc82f12736d36'),
('request_log_query_events','trg_request_log_query_events_rowid_insert_prd055','fn_trg_request_log_query_events_rowid_insert_prd055',5,'87b7a0909d745064dd8307f1d5811d5035c2ec825c66e88ae5df086248fe68d7','ba81202fd8aaf46ac61d374d26394412adbbb3e3d06ef6dc3d3a0a9031fe7a52'),
('request_log_query_events','trg_request_log_query_events_state_update_prd055','fn_trg_request_log_query_events_state_update_prd055',19,'ffe513a7b7607891856556828fb075c9f12164015f29a27c304f9f1f88d6ac7a','6b1fb2d43eaabdb0732f50c5d25e2f0b2d69a68680bbec0a185d42b41830d7ba'),
('request_log_query_events','trg_request_log_query_events_immutable_delete_prd055','fn_trg_request_log_query_events_immutable_delete_prd055',11,'6d3f623e38d29335a4dae2ab5f2ec12ae297715eab48bfdbbaf81dcd817c3e66','baf523eee7795c7c79a3db624440d43f0a513bed28996adca5024c79de6c9b82'),
('audit_exports','trg_audit_exports_rowid_insert_prd058','fn_trg_audit_exports_rowid_insert_prd058',5,'9cbe0d9612844d8b525332d9679dffb4d28bac32ba8935cca92969f77b98bc1b','d4f602b1e760e0da951d4c9a9bc4304b10dcf69065b6284ee79083889b19e400'),
('audit_exports','trg_audit_exports_update_prd058','fn_trg_audit_exports_update_prd058',19,'0e3ed1ed56413a784e07de662d1a4869f6ac4be5feb7095c464da24f63742165','605cc9a2af616e9c351dd9cb044354f8c4096dbc8dfba50564d71ae02dfaaef2'),
('audit_exports','trg_audit_exports_delete_prd058','fn_trg_audit_exports_delete_prd058',11,'c4e4883bc764869dec490cc73ea8d4311c7e941aa64db932dd485c395bb0e23e','c4fe857d731c716b73d62849987cffefcf4cd4824a8f4c580d8d9a64f20d590c'),
('audit_export_events','trg_audit_export_events_owner_insert_prd058','fn_trg_audit_export_events_owner_insert_prd058',7,'a570cb361edc8378521f2a90b0a2bf60bd97edf76bb159d7e90c794869b589cd','77a67a2e07e073afd9b2aad49366276e27a6d3654be6665a4e5e776fb60e82db'),
('audit_export_events','trg_audit_export_events_state_update_prd058','fn_trg_audit_export_events_state_update_prd058',19,'e8334d81cef1cae3b373700f4905f5922fd42aa08e0bd7989917863cd057ea60','645f4edaa1a5c7350caacbc624f326ac053989dd79df6747636bebc564d03c6d'),
('audit_export_events','trg_audit_export_events_conflict_insert_prd058','fn_trg_audit_export_events_conflict_insert_prd058',7,'7192b2606b603b10ee14206f30c4ea5ce4bc2c5a74310cf1f8ebce994b133a37','2659a36b3a4a3a23d8eab9683bc79dd674798849cb64c45267f6a3679a1d6296'),
('audit_export_events','trg_audit_export_events_rowid_insert_prd058','fn_trg_audit_export_events_rowid_insert_prd058',5,'356b6ee3ca000cf593ddf9d6c55f1de02e39192ff2ca1169ef1a94cca843fb2e','69eb1e386da5a41ffda433ea15eee71b15161620382d847a2664051ba70f6da6'),
('audit_export_events','trg_audit_export_events_delete_prd058','fn_trg_audit_export_events_delete_prd058',11,'2db4cfac0003b3a9e1e4b6407ebb3e20f3e76bb7f6fdde8ee79aa92467b14620','18f588d093b8df3a3180db44cf4e0d8bf2f222fc35834d9dbe2d9a0d4495dc9f'),
('audit_exports','trg_audit_exports_owner_insert_prd058','fn_trg_audit_exports_owner_insert_prd058',7,'bf22066f5ebc82dd1b91ca2c4c8f7689672ffaab5bece6120a26961f311ab17b','425b6369d201da87013c21356d96e41d253753e84e3bfc908fa260a30710b86b'),
('audit_exports','trg_audit_exports_conflict_insert_prd058','fn_trg_audit_exports_conflict_insert_prd058',7,'25d3af6a29a3d4104db662b65cfe6b6577fa45d3ca3bb80d1ae4d9044e159b81','683e147937c74d1382a36d3374d56e3edba3870db96aec8d2fc0a6386349c4de'),
('request_log_query_events','trg_request_log_query_events_keys_insert_prd055','fn_trg_request_log_query_events_keys_insert_prd055',7,'21f342a5f00f4593916d608b62aeec80b4e3eb472acacc57d646a5f92578c33a','23cd018aef5dfab0eae322ec85aef939f42876e248beddf1bb9ba22fda29364f'),
('request_log_query_events','trg_request_log_queries_product_event_insert_prd085','fn_trg_request_log_queries_product_event_insert_prd085',5,'af00256b54aa9c079acc6a35de220d8ade885e92c5e031985a1a18bbaba125ff','3ff88d20141a3684b2ce5a5138996a086fa467912bb790a16c6036abcce62ad5')), actual AS (
      SELECT relation.relname AS source_table,trigger.tgname,trigger.tgenabled,trigger.tgtype,trigger.tgnargs,trigger.tgqual,
        trigger.tgattr,trigger.tgconstraint,routine.proname,routine.prosrc,routine.prosecdef,
        routine.proconfig,routine.proisstrict,routine.proleakproof,routine.provolatile,
        routine.proparallel,routine.pronamespace,routine.prorettype,routine.prolang,
        pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid) AS sql
      FROM pg_trigger trigger JOIN pg_proc routine ON routine.oid=trigger.tgfoid
      JOIN pg_class relation ON relation.oid=trigger.tgrelid
      WHERE trigger.tgrelid IN('request_log_query_events'::regclass,'audit_exports'::regclass,'audit_export_events'::regclass,'saved_filters'::regclass)
        AND NOT trigger.tgisinternal
    ), recorded AS (SELECT * FROM tenant_deletion_trigger_manifest
      WHERE source_table IN('request_log_query_events','audit_exports','audit_export_events','saved_filters'))
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
      WHERE source_table IN('request_log_query_events','audit_exports','audit_export_events','saved_filters'))<>4
    OR (SELECT array_agg(attname::text ORDER BY attnum) FROM pg_attribute
    WHERE attrelid='request_log_query_events'::regclass AND attnum>0 AND NOT attisdropped)
    IS DISTINCT FROM ARRAY['query_event_id','event_id','tenant_id','actor_user_id','actor_session_id','filter_version','filter_fingerprint','filter_keys_json','saved_filter_id','sort_by','sort_direction','query_limit','query_offset','matched_count','returned_count','has_more','query_duration_ms','operation_request_id','operation_trace_id','event_dispatch_status','event_dispatch_attempts','event_next_attempt_at','event_dispatched_at','event_claim_token','event_lease_owner_id','event_lease_expires_at','event_last_error_code','executed_at','created_at','updated_at']::text[]
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_log_query_schema_drift'; END IF;
END; $guard$;
CREATE TEMP TABLE log_identity_expected_export(
  file_name TEXT CONSTRAINT log_identity_expected_name CHECK(length(file_name) BETWEEN 5 AND 128 AND substr(file_name,-4)='.csv' AND file_name OPERATOR(public.!@@) '*[^A-Za-z0-9._-]*' AND instr(file_name,'/')=0 AND instr(file_name,'\')=0));
DO $guard$ BEGIN
  IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint
      WHERE conrelid='audit_exports'::regclass AND conname='ck_audit_exports_13')
    IS DISTINCT FROM (SELECT pg_get_constraintdef(oid) FROM pg_constraint
      WHERE conrelid='log_identity_expected_export'::regclass AND conname='log_identity_expected_name')
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_log_export_schema_drift'; END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_constraint fk
      WHERE fk.conrelid='audit_exports'::regclass AND fk.conname='fk_audit_exports_0_audit_logs'
        AND fk.contype='f' AND fk.confrelid='audit_logs'::regclass
        AND fk.convalidated AND fk.confupdtype='a' AND fk.confdeltype='a' AND fk.confmatchtype='s'
        AND fk.condeferrable=FALSE AND fk.condeferred=FALSE
        AND (SELECT array_agg(attname::text ORDER BY key.ordinal)
          FROM unnest(fk.conkey) WITH ORDINALITY key(attnum,ordinal)
          JOIN pg_attribute attribute ON attribute.attrelid=fk.conrelid AND attribute.attnum=key.attnum)
          =ARRAY['tenant_id','audit_log_id']::text[]
        AND (SELECT array_agg(attname::text ORDER BY key.ordinal)
          FROM unnest(fk.confkey) WITH ORDINALITY key(attnum,ordinal)
          JOIN pg_attribute attribute ON attribute.attrelid=fk.confrelid AND attribute.attnum=key.attnum)
          =ARRAY['tenant_id','audit_log_id']::text[])
  THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='workspace_log_export_fk_drift'; END IF;
END; $guard$;
DROP TABLE log_identity_expected_export;
ALTER TABLE audit_exports DROP CONSTRAINT ck_audit_exports_13;
ALTER TABLE audit_exports ADD CONSTRAINT ck_audit_exports_13 CHECK(length(file_name) BETWEEN 5 AND 128 AND right(file_name,4)='.csv' AND file_name OPERATOR(public.!@@) '*[^A-Za-z0-9._-]*' AND instr(file_name,'/')=0 AND instr(file_name,'\')=0);
ALTER TABLE audit_exports ALTER CONSTRAINT fk_audit_exports_0_audit_logs DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE request_log_query_events ALTER COLUMN actor_session_id DROP NOT NULL;
ALTER TABLE request_log_query_events ADD COLUMN workspace_session_id TEXT REFERENCES workspace_sessions(workspace_session_id);
ALTER TABLE request_log_query_events ADD CONSTRAINT ck_query_event_session_domain_prd147
  CHECK((actor_session_id IS NULL)<>(workspace_session_id IS NULL));
CREATE OR REPLACE FUNCTION fn_trg_request_log_query_events_owner_insert_prd055() RETURNS trigger LANGUAGE plpgsql AS $trigger$BEGIN
  IF NOT EXISTS(
    SELECT 1 FROM users actor WHERE actor.user_id=NEW.actor_user_id AND actor.tenant_id=NEW.tenant_id
  ) OR NOT (
    (NEW.workspace_session_id IS NULL AND NEW.actor_session_id IS NOT NULL AND EXISTS(
    SELECT 1 FROM sessions session WHERE session.session_id=NEW.actor_session_id
      AND session.user_id=NEW.actor_user_id AND session.tenant_id=NEW.tenant_id
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
  ) OR (NEW.saved_filter_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM saved_filters saved WHERE saved.saved_filter_id=NEW.saved_filter_id
      AND saved.tenant_id=NEW.tenant_id AND saved.owner_user_id=NEW.actor_user_id
      AND saved.status='active'
  )) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='request_log_query_event_owner_invalid';
  END IF;
  RETURN NEW;
END;$trigger$;
CREATE OR REPLACE FUNCTION fn_trg_request_log_query_events_state_update_prd055() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  IF (NOT ( NEW.query_event_id=OLD.query_event_id AND NEW.event_id=OLD.event_id AND NEW.tenant_id=OLD.tenant_id AND NEW.actor_user_id=OLD.actor_user_id AND NEW.actor_session_id IS NOT DISTINCT FROM OLD.actor_session_id AND NEW.workspace_session_id IS NOT DISTINCT FROM OLD.workspace_session_id AND NEW.filter_version=OLD.filter_version AND NEW.filter_fingerprint=OLD.filter_fingerprint AND NEW.filter_keys_json=OLD.filter_keys_json AND NEW.saved_filter_id IS NOT DISTINCT FROM OLD.saved_filter_id AND NEW.sort_by=OLD.sort_by AND NEW.sort_direction=OLD.sort_direction AND NEW.query_limit=OLD.query_limit AND NEW.query_offset=OLD.query_offset AND NEW.matched_count=OLD.matched_count AND NEW.returned_count=OLD.returned_count AND NEW.has_more=OLD.has_more AND NEW.query_duration_ms=OLD.query_duration_ms AND NEW.operation_request_id=OLD.operation_request_id AND NEW.operation_trace_id=OLD.operation_trace_id AND NEW.executed_at=OLD.executed_at AND NEW.created_at=OLD.created_at AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at) AND ( (OLD.event_dispatch_status='pending' AND NEW.event_dispatch_status='processing' AND OLD.event_dispatch_attempts BETWEEN 0 AND 9 AND unixepoch(OLD.event_next_attempt_at)<=unixepoch(NEW.updated_at) AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1 AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL AND length(NEW.event_claim_token) BETWEEN 16 AND 128 AND length(NEW.event_lease_owner_id) BETWEEN 1 AND 128 AND unixepoch(NEW.event_lease_expires_at)>unixepoch(NEW.updated_at) AND NEW.event_last_error_code IS NULL) OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='processing' AND OLD.event_dispatch_attempts BETWEEN 1 AND 9 AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1 AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at) AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL AND length(NEW.event_claim_token) BETWEEN 16 AND 128 AND length(NEW.event_lease_owner_id) BETWEEN 1 AND 128 AND unixepoch(NEW.event_lease_expires_at)>unixepoch(NEW.updated_at) AND NEW.event_last_error_code IS NULL) OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='dispatched' AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts AND unixepoch(OLD.event_lease_expires_at)>unixepoch(NEW.updated_at) AND NEW.event_dispatched_at=NEW.updated_at AND NEW.event_next_attempt_at IS NULL AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NULL) OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='pending' AND OLD.event_dispatch_attempts BETWEEN 1 AND 9 AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts AND unixepoch(OLD.event_lease_expires_at)>unixepoch(NEW.updated_at) AND unixepoch(NEW.event_next_attempt_at)>unixepoch(NEW.updated_at) AND NEW.event_dispatched_at IS NULL AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NOT NULL) OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='failed' AND OLD.event_dispatch_attempts=10 AND NEW.event_dispatch_attempts=10 AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NOT NULL) ) )) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='request_log_query_event_immutable';
  END IF;
  RETURN NEW;
END;
$trigger$;
CREATE OR REPLACE FUNCTION fn_trg_audit_export_events_state_update_prd058() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  IF (NOT ( NEW.audit_export_event_id=OLD.audit_export_event_id AND NEW.tenant_id=OLD.tenant_id AND NEW.audit_export_id=OLD.audit_export_id AND NEW.created_at=OLD.created_at AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at) AND ( ( ( (OLD.status='pending' AND unixepoch(OLD.next_attempt_at)<=unixepoch(NEW.updated_at)) OR (OLD.status='processing' AND OLD.attempt_count BETWEEN 1 AND 9 AND unixepoch(OLD.lease_until)<=unixepoch(NEW.updated_at)) ) AND NEW.status='processing' AND NEW.attempt_count=OLD.attempt_count+1 AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL AND NEW.claim_token IS NOT NULL AND length(NEW.claim_token)=32 AND NEW.claim_token OPERATOR(public.!@@) '*[^0-9a-f]*' AND (OLD.status<>'processing' OR NEW.claim_token IS DISTINCT FROM OLD.claim_token) AND NEW.claim_owner IS NOT NULL AND length(NEW.claim_owner) BETWEEN 1 AND 128 AND NEW.claim_owner OPERATOR(public.!@@) '*[^A-Za-z0-9._:-]*' AND unixepoch(NEW.lease_until)=unixepoch(NEW.updated_at)+30 AND NEW.last_error_code IS NULL ) OR ( OLD.status='processing' AND NEW.attempt_count=OLD.attempt_count AND NEW.claim_token IS NULL AND NEW.claim_owner IS NULL AND NEW.lease_until IS NULL AND ( ( unixepoch(OLD.lease_until)>unixepoch(NEW.updated_at) AND ( (NEW.status='dispatched' AND NEW.dispatched_at=NEW.updated_at AND NEW.next_attempt_at IS NULL AND NEW.last_error_code IS NULL) OR (OLD.attempt_count<10 AND NEW.status='pending' AND NEW.dispatched_at IS NULL AND NEW.last_error_code IS NOT NULL AND unixepoch(NEW.next_attempt_at)=unixepoch(NEW.updated_at)+CASE WHEN OLD.attempt_count=1 THEN 5 WHEN OLD.attempt_count=2 THEN 30 ELSE 300 END) OR (OLD.attempt_count=10 AND NEW.status='failed' AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL AND NEW.last_error_code IS NOT NULL) ) ) OR (OLD.attempt_count=10 AND unixepoch(OLD.lease_until)<=unixepoch(NEW.updated_at) AND NEW.status='failed' AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL AND NEW.last_error_code IS NOT NULL) ) ) ) )) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='audit_export_event_state_invalid';
  END IF;
  RETURN NEW;
END;
$trigger$;
CREATE OR REPLACE FUNCTION fn_trg_saved_filters_state_update_prd055() RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
  IF (NOT ( NEW.saved_filter_id=OLD.saved_filter_id AND NEW.tenant_id=OLD.tenant_id AND NEW.owner_user_id=OLD.owner_user_id AND NEW.filter_version=OLD.filter_version AND NEW.created_at=OLD.created_at AND NEW.version=OLD.version+1 AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at) AND OLD.status='active' AND NOT EXISTS ( SELECT 1 FROM saved_filters other WHERE other.status='active' AND other.saved_filter_id<>OLD.saved_filter_id AND other.tenant_id=NEW.tenant_id AND other.owner_user_id=NEW.owner_user_id AND other.name_normalized=NEW.name_normalized ) AND ( (NEW.status='active' AND NEW.deleted_at IS NULL) OR ( NEW.status='deleted' AND NEW.deleted_at=NEW.updated_at AND NEW.name=OLD.name AND NEW.name_normalized=OLD.name_normalized AND NEW.filter_json=OLD.filter_json AND NEW.sort_by=OLD.sort_by AND NEW.sort_direction=OLD.sort_direction ) ) )) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='saved_filter_state_invalid';
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
  WHERE attribute.attrelid='request_log_query_events'::regclass
    AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='request_log_query_events'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='request_log_query_events'::regclass AND NOT tgisinternal
)
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256('CREATE TABLE request_log_query_events ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  column_signature_sha256=metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),''))
WHERE source_table='request_log_query_events';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
UPDATE tenant_deletion_trigger_manifest manifest SET
  trigger_sql=pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid))
FROM pg_trigger trigger WHERE trigger.tgrelid='request_log_query_events'::regclass
  AND manifest.source_table='request_log_query_events' AND manifest.trigger_name=trigger.tgname;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute
  LEFT JOIN pg_attrdef default_value ON default_value.adrelid=attribute.attrelid
    AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='audit_exports'::regclass
    AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='audit_exports'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='audit_exports'::regclass AND NOT tgisinternal
)
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256('CREATE TABLE audit_exports ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  column_signature_sha256=metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),''))
WHERE source_table='audit_exports';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
UPDATE tenant_deletion_trigger_manifest manifest SET
  trigger_sql=pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid))
FROM pg_trigger trigger WHERE trigger.tgrelid='audit_exports'::regclass
  AND manifest.source_table='audit_exports' AND manifest.trigger_name=trigger.tgname;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute
  LEFT JOIN pg_attrdef default_value ON default_value.adrelid=attribute.attrelid
    AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='audit_export_events'::regclass
    AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='audit_export_events'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='audit_export_events'::regclass AND NOT tgisinternal
)
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256('CREATE TABLE audit_export_events ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  column_signature_sha256=metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),''))
WHERE source_table='audit_export_events';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
UPDATE tenant_deletion_trigger_manifest manifest SET
  trigger_sql=pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid))
FROM pg_trigger trigger WHERE trigger.tgrelid='audit_export_events'::regclass
  AND manifest.source_table='audit_export_events' AND manifest.trigger_name=trigger.tgname;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
WITH columns AS (
  SELECT attribute.attnum,attribute.attname,
    format_type(attribute.atttypid,attribute.atttypmod) AS type_name,
    attribute.attnotnull,pg_get_expr(default_value.adbin,default_value.adrelid) AS default_sql
  FROM pg_attribute attribute
  LEFT JOIN pg_attrdef default_value ON default_value.adrelid=attribute.attrelid
    AND default_value.adnum=attribute.attnum
  WHERE attribute.attrelid='saved_filters'::regclass
    AND attribute.attnum>0 AND NOT attribute.attisdropped
), definitions AS (
  SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
    ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
    ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
  UNION ALL
  SELECT 10000+row_number() OVER(ORDER BY conname),
    'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid='saved_filters'::regclass
), triggers AS (
  SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
  FROM pg_trigger WHERE tgrelid='saved_filters'::regclass AND NOT tgisinternal
)
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256('CREATE TABLE saved_filters ('
    ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
  column_signature_sha256=metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
    ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30)
    ORDER BY tgname) FROM triggers),''))
WHERE source_table='saved_filters';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
UPDATE tenant_deletion_trigger_manifest manifest SET
  trigger_sql=pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid),
  trigger_sha256=metahub_sha256(pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid))
FROM pg_trigger trigger WHERE trigger.tgrelid='saved_filters'::regclass
  AND manifest.source_table='saved_filters' AND manifest.trigger_name=trigger.tgname;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest
FOR EACH ROW EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
