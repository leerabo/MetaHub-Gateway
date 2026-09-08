-- Rollback refuses invitation facts that depend on the corrected authorization.
LOCK TABLE invitations,invitation_events,audit_logs,product_events,
  tenant_deletion_trigger_manifest,tenant_deletion_table_manifest IN ACCESS EXCLUSIVE MODE;
DO $existing_manifest$
BEGIN
  IF EXISTS(
    WITH expected(source_table,trigger_name,function_name,type_bits,legacy_sha256,body_sha256) AS (
      VALUES
      ('invitations','trg_invitations_owner_insert_prd060','fn_trg_invitations_owner_insert_prd060',7,'ee2292e1395a755be2f217d5c2a0ab117896dffe030539c6d1ba15e799a1d58c','df454dde6878efa1c6f60ea64f4ec0e2296d266879970a78bd9710d93234e25e'),
      ('invitations','trg_invitations_conflict_insert_prd060','fn_trg_invitations_conflict_insert_prd060',7,'876a6919c6ef02a48dd6781b3549326ac794d5e4e4edf3e209fe2fdfc5719274','3e1cbbc4c62cfa504f55ef1d7d34cd46dfbe3f0c892eb7c5b74632b8528a6854'),
      ('invitations','trg_invitations_state_update_prd060','fn_trg_invitations_state_update_prd060',19,'18b6ee007497d47cac18cf5cd1228b4ec71fbeaa8fd2bc70afa683d537616192','8631f2a8356e94407fa2100763f54a55970dc2a9c860fcdb8e01e2a7c0498d57'),
      ('invitations','trg_invitations_delete_prd060','fn_trg_invitations_delete_prd060',11,'62a2e44b89bb5f1cdfd735d745d014c920904bdcc159b1766dad9d843c4287d1','f80a8efd632569bac46a4a4c921fb150abbebafd51358cedbe7fb6149e08595e'),
      ('invitations','trg_invitations_rowid_insert_prd060','fn_trg_invitations_rowid_insert_prd060',5,'002a06493abc9e86ccdadc9b9712b45f5c307f107cb6cd88759c37d79ac48806','d27e004c0859098036a35cc6b0d6b051a495d062273842d95def6d39ab08e1ac'),
      ('invitation_events','trg_invitation_events_owner_insert_prd060','fn_trg_invitation_events_owner_insert_prd060',7,'711b54e07bc6c2caa129b4437871c562abc2816ea1a76e8a3cdbf0895095f1b6','c33e1d0a9a72551d391c11b7545b01876353f8cb2f7869ded3fea2639acc850a'),
      ('invitation_events','trg_invitation_events_conflict_insert_prd060','fn_trg_invitation_events_conflict_insert_prd060',7,'84692c399f89d0607e4889b68674256e0c3c6ebd6cb932e7472999a48de1a459','ea18dbb3da242cfb34020ada92460152d68e734a09457fe535aa3d0e4f12279f'),
      ('invitation_events','trg_invitation_events_state_update_prd060','fn_trg_invitation_events_state_update_prd060',19,'6187288f6e7e7accade6d00ce53a1017f98fa17be509b33129b439c4cd90725b','e02e0e9edcae7afe13f38b7e703832b3738836111a30e47d0223983754dee3fd'),
      ('invitation_events','trg_invitation_events_delete_prd060','fn_trg_invitation_events_delete_prd060',11,'f56d974f541dc429876203aac03cdc80a1be813cf092f5e36b3fa7a562d3a7a1','c7943362b325022be1d6e5752f70ef27fa88a25abaeb4c10dad62096fd8f97e8'),
      ('invitation_events','trg_invitation_events_rowid_insert_prd060','fn_trg_invitation_events_rowid_insert_prd060',5,'63484d0bb97024035ccb30f8df23423bc8ca37c197a77f2cec47ae438fdf95a9','da4d0acae51924aee8270431f292b9be47af5568e9c0258db21b2c2acdcc08e5'),
      ('audit_logs','trg_audit_logs_admin_assistant_owner_insert_prd077','fn_trg_audit_logs_admin_assistant_owner_insert_prd077',7,'20108804785c590287059a56d47369c9dd3db03eefda32c2457c266b52f4ab1c','688701c50140418de4a95be885068ab5a5171f880fa7352b7afaa924968beb29'),
      ('audit_logs','trg_audit_logs_billing_dispute_owner_insert_prd080','fn_trg_audit_logs_billing_dispute_owner_insert_prd080',7,'a0ecf3aa6f236f3b416d403c71f2efc97a9a65639dc19a8064e7f3302e3a27fa','55883dca7a91284bc9597e4e89add3715e89537ce95f0eaa0884e104d1d75456'),
      ('audit_logs','trg_audit_logs_export_sequence_insert_prd058','fn_trg_audit_logs_export_sequence_insert_prd058',5,'ed54d3990d7e4a70f1ff88fe9b922a5413b2db53e67d3814d18dd108d57bed62','692a118f7c5ed6728415d1d49cc8a5fb52b020e8dae6ebe4f903de44d1375b9e'),
      ('audit_logs','trg_audit_logs_immutable_delete','fn_trg_audit_logs_immutable_delete',11,'d3315515e128df9429cdd13ebb5f1abc22ffdc31e6c5b178e1b687f5a323a156','cf0cae389ae4ad9c48bf360b900277bc40eeef81b26905cba09f43c76d319f75'),
      ('audit_logs','trg_audit_logs_immutable_update','fn_trg_audit_logs_immutable_update',19,'9ffc759c0c4f364e472084b54ca15b19dcc148f4c002225997ac5941efbd83b4','f10c1629250495542fdee001c76400d62f3d6b1649d4e801d2a254814151c856'),
      ('audit_logs','trg_audit_logs_owner_insert','fn_trg_audit_logs_owner_insert',7,'54c33c11b615c321581986478b505ed237a38bcb3e266bda29f35c5cb9812d0e','5ed553e92456d61ca50c135f139d3fc3ca5629f9a63c9d578aab85a7de7ac364'),
      ('audit_logs','trg_audit_logs_plan_billing_owner_insert_prd079','fn_trg_audit_logs_plan_billing_owner_insert_prd079',7,'05022a8f67fc36b431029ca7d23e5dcbb2aed8ef7645d113b0d5092b3e06b6d3','78f17e39c552c90c20ef7671dca71767bd9919127b36d9ffd21ee74d38847866'),
      ('audit_logs','trg_audit_logs_policy_template_owner_insert_prd068','fn_trg_audit_logs_policy_template_owner_insert_prd068',7,'8146509b99183a1ec2fd76097328a815d81149b8334e54bdda26c053345c5e35','3c96e72c99166b4800341ff023bbeda501e191520afde49ea5c440c29de0e9e9'),
      ('audit_logs','trg_audit_logs_prd050_conflict_insert','fn_trg_audit_logs_prd050_conflict_insert',7,'0279769ebe6c15f822939567bea763bd6272054bf1c55433b9ed19172b128229','d454c8cecbc891457265c8ed88a9e9c6c5d568809a10492f45913e62b40724b9'),
      ('audit_logs','trg_audit_logs_prd050_rowid_insert','fn_trg_audit_logs_prd050_rowid_insert',5,'c5019343535f043eabb2330c5551563bc16968c35576a2f2726d4c1bf18182f3','3f5ccd4ebd68cc713b149efbd595a42dc6c4748d2db0600abdfb4af63215c27e'),
      ('audit_logs','trg_audit_logs_response_cache_owner_insert_prd076','fn_trg_audit_logs_response_cache_owner_insert_prd076',7,'785139d8bf8758007699f5159aeeb31633d119f7b28750b10b2452e8f8ebb5d0','33e049d4541cd20acf979e6ad8477fee394f4bb8cd14e8a20297d9bec6a354a7'),
      ('audit_logs','trg_audit_logs_session_risk_owner_insert_prd070','fn_trg_audit_logs_session_risk_owner_insert_prd070',7,'d82f771edb42a99281e9ad85a4481cd271aeda1a444d0fe32d7bbbc325ba6c06','f759ac61be2b06de86e58ee599ed6d7c808b4dd55d4cbc3fd575cf84c7948416'),
      ('audit_logs','trg_audit_logs_mcp_owner_insert_prd082','fn_trg_audit_logs_mcp_owner_insert_prd082',7,'945c1124afd70e88a7aff479cca0b045dcffe522f9539259e0f350892ba79756','dbd36a1b9c9eeded07dbca902c50c6bf5f9bf7746cd7fa06e1f6df0b3b3f7a12'),
      ('audit_logs','trg_audit_logs_mcp_operation_insert_prd082','fn_trg_audit_logs_mcp_operation_insert_prd082',7,'8a1e27d08b28efdde0b9fabfe81675f67b2c07615a2957a3e03b9d931ab92786','1a54721206b4dcb0e6f44614dd7262b652621c6b136310859b06f6be549738f9'),
      ('audit_logs','trg_audit_logs_mcp_server_insert_prd082','fn_trg_audit_logs_mcp_server_insert_prd082',7,'2332e7367605ee88a266e8ecf901a98d6fb256f3dc0d82a1d80ab0b0b5bf6a7a','f95d4f6163ec84d6543a8c00da094086d1c488bd0520a46d39b55182e69863fb'),
      ('audit_logs','trg_audit_logs_mcp_tool_insert_prd082','fn_trg_audit_logs_mcp_tool_insert_prd082',7,'eed3a451faabb981e76fe076d5851f3f5a90a1f8bc5509d085394f45085049ff','52a5243c8dcc9ae178ed07776287d926c68c8874fbbd26cc3654c2cc071c3af5'),
      ('audit_logs','trg_audit_logs_mcp_network_policy_insert_prd083','fn_trg_audit_logs_mcp_network_policy_insert_prd083',7,'ec21432bba8d9e36e7e22a0d90f8981fbfabb5e6a3f56adaa153deb42754478c','235d6da1e7882a0b4e29f4438c5ac231a44f585023afb2e281d62a2a072e0149'),
      ('product_events','trg_product_events_owner_insert_prd085','fn_trg_product_events_owner_insert_prd085',7,'1b332d1b5246228dcfb7cdbf5ec6233c67ea9118459194f60ce70dedcaa0ac00','39e4689f7915cd24db2b4f0e69c447baaefb90b8c69e0d69ef3737b32933ea0a'),
      ('product_events','trg_product_events_update_prd085','fn_trg_product_events_update_prd085',19,'be378757e3b3e01b69490d0d74f81316f8c5bb08da39189592aa1fa6b0f0de4d','94f73132aebf9d95a24211361a133e1b3c32f80bee77dac4b07a4c9436aab0a5'),
      ('product_events','trg_product_events_delete_prd085','fn_trg_product_events_delete_prd085',11,'415324839fec931689e63be6f6eebe41c381c3ce33499f7c3802889ce2f4f810','e37c8e41e08292e9ad53a3f2e62f2a58b7872404befe7a47a8abcd883e95a8b6'),
      ('product_events','trg_product_events_outbox_insert_prd085','fn_trg_product_events_outbox_insert_prd085',5,'e60b1faed5e824fa3f7e7b6b2b6e5496742b86b23523040ec10b651a765f00d3','67c6bddbc211a52b0250f5c79bef361712bdb820adf81e73276c76a561a77460'),
      ('audit_logs','trg_audit_logs_product_event_insert_prd085','fn_trg_audit_logs_product_event_insert_prd085',5,'8321d5318ed12fe69598dbd3604188f037c1c27c3edb1a4b0986cd6db35054ec','2e23092b881cd266d2aa9bc9ffbb352f86f03b4dad19581a4e93a77ae9911a33')
    ), actual AS (
      SELECT relation.relname AS source_table,trigger.tgname AS trigger_name,
        trigger.tgenabled,trigger.tgtype,trigger.tgnargs,trigger.tgqual,trigger.tgattr,
        trigger.tgconstraint,routine.proname,routine.prosrc,routine.prosecdef,
        routine.pronamespace,routine.prorettype,routine.prolang,routine.proconfig,
        routine.proisstrict,routine.proleakproof,routine.provolatile,routine.proparallel,
        pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid) AS sql
      FROM pg_trigger trigger JOIN pg_class relation ON relation.oid=trigger.tgrelid
      JOIN pg_proc routine ON routine.oid=trigger.tgfoid
      WHERE relation.relnamespace=current_schema()::regnamespace AND NOT trigger.tgisinternal
        AND relation.relname IN('invitations','invitation_events','audit_logs','product_events')
    ), recorded AS (
      SELECT * FROM tenant_deletion_trigger_manifest
      WHERE source_table IN('invitations','invitation_events','audit_logs','product_events')
    )
    SELECT 1 FROM actual FULL JOIN recorded USING(source_table,trigger_name)
      FULL JOIN expected USING(source_table,trigger_name)
    WHERE actual.trigger_name IS NULL OR recorded.trigger_name IS NULL
      OR expected.trigger_name IS NULL OR actual.tgenabled<>'O'
      OR actual.tgtype<>expected.type_bits OR actual.tgnargs<>0 OR actual.tgqual IS NOT NULL
      OR actual.tgattr::text<>'' OR actual.tgconstraint<>0 OR actual.prosecdef
      OR actual.pronamespace<>current_schema()::regnamespace
      OR actual.prorettype<>'trigger'::regtype
      OR actual.prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
      OR actual.proconfig IS NOT NULL OR actual.proisstrict OR actual.proleakproof
      OR actual.provolatile<>'v' OR actual.proparallel<>'u'
      OR actual.proname<>expected.function_name
      OR metahub_sha256(actual.prosrc)<>expected.body_sha256
      OR ((actual.sql IS DISTINCT FROM recorded.trigger_sql
           OR metahub_sha256(actual.sql) IS DISTINCT FROM recorded.trigger_sha256)
        AND (metahub_sha256(recorded.trigger_sql) IS DISTINCT FROM expected.legacy_sha256
             OR recorded.trigger_sha256 IS DISTINCT FROM expected.legacy_sha256))
  ) OR EXISTS(
    WITH actual AS (
      SELECT source_table,metahub_sha256(string_agg(trigger_name||chr(31)||trigger_sha256,
        chr(30) ORDER BY trigger_name)) AS signature
      FROM tenant_deletion_trigger_manifest
      WHERE source_table IN('invitations','invitation_events','audit_logs','product_events')
      GROUP BY source_table
    ), recorded AS (
      SELECT * FROM tenant_deletion_table_manifest
      WHERE source_table IN('invitations','invitation_events','audit_logs','product_events')
    )
    SELECT 1 FROM actual FULL JOIN recorded USING(source_table)
    WHERE actual.source_table IS NULL OR recorded.source_table IS NULL
      OR actual.signature IS DISTINCT FROM recorded.trigger_set_sha256
  ) OR (SELECT count(*) FROM tenant_deletion_table_manifest
      WHERE source_table IN('invitations','invitation_events','audit_logs','product_events'))<>4
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_authorization_manifest_drift';
  END IF;
END;
$existing_manifest$;
DO $facts$
BEGIN
  IF EXISTS(SELECT 1 FROM invitations) OR EXISTS(SELECT 1 FROM invitation_events)
    OR EXISTS(SELECT 1 FROM audit_logs WHERE action='user.invited')
    OR EXISTS(SELECT 1 FROM product_events WHERE event_type='teammate.invited')
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_authorization_rollback_has_facts';
  END IF;
END;
$facts$;
DO $guard$
BEGIN
  IF EXISTS(
    SELECT 1 FROM (VALUES
    ('fn_trg_invitations_owner_insert_prd060','df454dde6878efa1c6f60ea64f4ec0e2296d266879970a78bd9710d93234e25e'),
    ('fn_trg_invitations_state_update_prd060','8631f2a8356e94407fa2100763f54a55970dc2a9c860fcdb8e01e2a7c0498d57'),
    ('fn_trg_invitation_events_owner_insert_prd060','c33e1d0a9a72551d391c11b7545b01876353f8cb2f7869ded3fea2639acc850a'),
    ('fn_trg_audit_logs_owner_insert','5ed553e92456d61ca50c135f139d3fc3ca5629f9a63c9d578aab85a7de7ac364'),
    ('fn_trg_audit_logs_prd050_conflict_insert','d454c8cecbc891457265c8ed88a9e9c6c5d568809a10492f45913e62b40724b9'),
    ('fn_trg_product_events_owner_insert_prd085','39e4689f7915cd24db2b4f0e69c447baaefb90b8c69e0d69ef3737b32933ea0a'),
    ('fn_trg_audit_logs_product_event_insert_prd085','2e23092b881cd266d2aa9bc9ffbb352f86f03b4dad19581a4e93a77ae9911a33')
    ) expected(function_name,body_sha256)
    LEFT JOIN pg_proc routine ON routine.proname=expected.function_name
      AND routine.pronamespace=current_schema()::regnamespace
      AND routine.pronargs=0
    WHERE routine.oid IS NULL OR routine.prorettype<>'trigger'::regtype
      OR routine.prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
      OR routine.prosecdef OR metahub_sha256(routine.prosrc)<>expected.body_sha256
  ) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_authorization_function_drift';
  END IF;
END;
$guard$;
DO $repair$
DECLARE routine RECORD;
BEGIN
  FOR routine IN
    SELECT oid FROM pg_proc WHERE pronamespace=current_schema()::regnamespace
      AND pronargs=0 AND proname IN('fn_trg_invitations_owner_insert_prd060','fn_trg_invitations_state_update_prd060','fn_trg_invitation_events_owner_insert_prd060','fn_trg_audit_logs_owner_insert','fn_trg_audit_logs_prd050_conflict_insert','fn_trg_product_events_owner_insert_prd085','fn_trg_audit_logs_product_event_insert_prd085')
  LOOP
    EXECUTE replace(replace(pg_get_functiondef(routine.oid),
      quote_literal('user.manage'),quote_literal('user_row.manage')),
      quote_literal('user.invited'),quote_literal('user_row.invited'));
  END LOOP;
END;
$repair$;
DO $guard$
BEGIN
  IF EXISTS(
    SELECT 1 FROM (VALUES
    ('fn_trg_invitations_owner_insert_prd060','5920674d7c4572fe403193ab592023a2a4a631386b397d5e859767537e407c78'),
    ('fn_trg_invitations_state_update_prd060','a0bab5250c8b32d121c364424c654f003647b2b8b5d7be970123c7c7ac7d82e7'),
    ('fn_trg_invitation_events_owner_insert_prd060','48421c33b676341dd2ab2956fb6f32f7e800d496f1ff59adf4d832a0c9ac2097'),
    ('fn_trg_audit_logs_owner_insert','b182820edc7e16371325963b047794af3a6fc6128d7e32eb8072bb11cf1404c8'),
    ('fn_trg_audit_logs_prd050_conflict_insert','b5824691347d14ab07d003976814ccd0b65f1b483b840154f4c176b2e5bc357a'),
    ('fn_trg_product_events_owner_insert_prd085','09c9383a4e4cd85623117ee5855980ae951782fc649e229cfc0ef9c35d99c418'),
    ('fn_trg_audit_logs_product_event_insert_prd085','8fbe41ec932df681a683c11568be309c4b4de9b7dc80d41335434db7b1531ad5')
    ) expected(function_name,body_sha256)
    LEFT JOIN pg_proc routine ON routine.proname=expected.function_name
      AND routine.pronamespace=current_schema()::regnamespace
      AND routine.pronargs=0
    WHERE routine.oid IS NULL OR routine.prorettype<>'trigger'::regtype
      OR routine.prolang<>(SELECT oid FROM pg_language WHERE lanname='plpgsql')
      OR routine.prosecdef OR metahub_sha256(routine.prosrc)<>expected.body_sha256
  ) THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_authorization_function_drift';
  END IF;
END;
$guard$;

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 ON tenant_deletion_trigger_manifest;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089 ON tenant_deletion_table_manifest;
DO $manifest$
DECLARE changed BIGINT;
BEGIN
  UPDATE tenant_deletion_trigger_manifest manifest
  SET trigger_sql=pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid),
      trigger_sha256=metahub_sha256(
        pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid))
  FROM pg_trigger trigger
  JOIN pg_class relation ON relation.oid=trigger.tgrelid
  WHERE trigger.tgname=manifest.trigger_name AND relation.relname=manifest.source_table
    AND relation.relnamespace=current_schema()::regnamespace AND NOT trigger.tgisinternal
    AND trigger.tgname IN('trg_invitations_owner_insert_prd060','trg_invitations_conflict_insert_prd060','trg_invitations_state_update_prd060','trg_invitations_delete_prd060','trg_invitations_rowid_insert_prd060','trg_invitation_events_owner_insert_prd060','trg_invitation_events_conflict_insert_prd060','trg_invitation_events_state_update_prd060','trg_invitation_events_delete_prd060','trg_invitation_events_rowid_insert_prd060','trg_audit_logs_admin_assistant_owner_insert_prd077','trg_audit_logs_billing_dispute_owner_insert_prd080','trg_audit_logs_export_sequence_insert_prd058','trg_audit_logs_immutable_delete','trg_audit_logs_immutable_update','trg_audit_logs_owner_insert','trg_audit_logs_plan_billing_owner_insert_prd079','trg_audit_logs_policy_template_owner_insert_prd068','trg_audit_logs_prd050_conflict_insert','trg_audit_logs_prd050_rowid_insert','trg_audit_logs_response_cache_owner_insert_prd076','trg_audit_logs_session_risk_owner_insert_prd070','trg_audit_logs_mcp_owner_insert_prd082','trg_audit_logs_mcp_operation_insert_prd082','trg_audit_logs_mcp_server_insert_prd082','trg_audit_logs_mcp_tool_insert_prd082','trg_audit_logs_mcp_network_policy_insert_prd083','trg_product_events_owner_insert_prd085','trg_product_events_update_prd085','trg_product_events_delete_prd085','trg_product_events_outbox_insert_prd085','trg_audit_logs_product_event_insert_prd085');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>32 THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_authorization_manifest_missing';
  END IF;
  WITH signatures AS (
    SELECT relation.relname AS source_table,
      metahub_sha256(string_agg(trigger.tgname||chr(31)||metahub_sha256(
        pg_get_triggerdef(trigger.oid)||';'||chr(10)||pg_get_functiondef(trigger.tgfoid)),
        chr(30) ORDER BY trigger.tgname)) AS trigger_set_sha256
    FROM pg_trigger trigger JOIN pg_class relation ON relation.oid=trigger.tgrelid
    WHERE relation.relnamespace=current_schema()::regnamespace AND NOT trigger.tgisinternal
      AND relation.relname IN('invitations','invitation_events','audit_logs','product_events')
    GROUP BY relation.relname
  )
  UPDATE tenant_deletion_table_manifest manifest
  SET trigger_set_sha256=signatures.trigger_set_sha256
  FROM signatures WHERE manifest.source_table=signatures.source_table;
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>4 THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='native_authorization_manifest_missing';
  END IF;
END;
$manifest$;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
EXECUTE FUNCTION fn_trg_tenant_deletion_trigger_manifest_update_prd089();
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
EXECUTE FUNCTION fn_trg_tenant_deletion_table_manifest_update_prd089();
