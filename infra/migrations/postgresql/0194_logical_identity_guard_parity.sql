-- Repair only the 103 reviewed, still-attached BEFORE UPDATE guards.
-- SQLite rowid is stable; PostgreSQL ctid changes on every physical UPDATE
-- and NEW.ctid is unassigned in BEFORE triggers. Compare existing logical
-- primary keys instead. Their immutable predicates already exist in each
-- function; every lease/state/actor/audit/content check remains byte-identical.
-- No rows, constraints, retired guards, or historical migrations are changed.
-- Refuse an unexpected body, binding or primary key rather than overwrite it.
DO $repair$
DECLARE
  patch record;
  function_oid oid;
  original_body text;
  original_definition text;
  actual_keys text[];
  bindings integer;
BEGIN
  FOR patch IN SELECT * FROM (VALUES
    ('fn_trg_adapter_contract_event_state_update_prd064', 'adapter_contract_event_outbox', '47c1a15a526fa34dae91ab83f0fa8b82', 'NEW.ctid=OLD.ctid', 'ROW(NEW.event_id) = ROW(OLD.event_id)', ARRAY['event_id']::text[], 1),
    ('fn_trg_approval_outbox_transition_prd078', 'approval_event_outbox', 'd50c1dabcb462bf257c573d624c35df0', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.approval_outbox_id) <> ROW(OLD.approval_outbox_id)', ARRAY['approval_outbox_id']::text[], 1),
    ('fn_trg_approval_challenges_transition_prd078', 'approval_step_up_challenges', 'b890d8d09bf8e884cc05d8452b98b959', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.approval_challenge_id) <> ROW(OLD.approval_challenge_id)', ARRAY['approval_challenge_id']::text[], 1),
    ('fn_trg_approval_tasks_transition_prd078', 'approval_tasks', 'cfd9b9436f2428deb6a03d45c4fa766f', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.approval_task_id) <> ROW(OLD.approval_task_id)', ARRAY['approval_task_id']::text[], 1),
    ('fn_trg_approval_writebacks_transition_prd078', 'approval_writebacks', 'e5c4044642b9a0347480d761ba16b211', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.approval_writeback_id) <> ROW(OLD.approval_writeback_id)', ARRAY['approval_writeback_id']::text[], 1),
    ('fn_trg_batch_attempts_update_prd074', 'batch_attempts', '2ca1b841054207bf76568285156a72a1', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.batch_attempt_id) <> ROW(OLD.batch_attempt_id)', ARRAY['batch_attempt_id']::text[], 1),
    ('fn_trg_batch_allocations_update_prd074', 'batch_budget_allocations', 'd0bee1bf06e43a399ebc20cf19ac69c5', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.batch_budget_allocation_id) <> ROW(OLD.batch_budget_allocation_id)', ARRAY['batch_budget_allocation_id']::text[], 1),
    ('fn_trg_batch_holds_update_prd074', 'batch_budget_holds', '830c683527b6d83ad57e4a2e6da60fb8', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.batch_budget_hold_id) <> ROW(OLD.batch_budget_hold_id)', ARRAY['batch_budget_hold_id']::text[], 1),
    ('fn_trg_batch_grants_update_prd074', 'batch_execution_grants', '952409fe11ef2beec19d050d4156edf7', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.batch_execution_grant_id) <> ROW(OLD.batch_execution_grant_id)', ARRAY['batch_execution_grant_id']::text[], 1),
    ('fn_trg_batch_outbox_update_prd074', 'batch_job_event_outbox', '54ff6a6fa40e2269c58a9f0522e98a20', 'NEW.ctid=OLD.ctid', 'ROW(NEW.batch_job_event_id) = ROW(OLD.batch_job_event_id)', ARRAY['batch_job_event_id']::text[], 1),
    ('fn_trg_batch_jobs_update_prd074', 'batch_jobs', 'a0a7ef3bcbc9418f3b575bea8790ef24', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.batch_job_id) <> ROW(OLD.batch_job_id)', ARRAY['batch_job_id']::text[], 1),
    ('fn_trg_batch_scheduler_update_prd074', 'batch_scheduler_tenants', '798956536011674c106064b059802c1a', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_id) <> ROW(OLD.tenant_id)', ARRAY['tenant_id']::text[], 1),
    ('fn_trg_batch_tasks_update_prd074', 'batch_tasks', 'a1f3fbd7b1f74eb6ef10d7da7b8db09d', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.batch_task_id) <> ROW(OLD.batch_task_id)', ARRAY['batch_task_id']::text[], 1),
    ('fn_trg_billing_dispute_outbox_transition_prd080', 'billing_dispute_event_outbox', '4cd0240ffc78fb9898b6bfcb993e2db6', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.billing_dispute_event_outbox_id) <> ROW(OLD.billing_dispute_event_outbox_id)', ARRAY['billing_dispute_event_outbox_id']::text[], 1),
    ('fn_trg_billing_dispute_scheduler_transition_prd080', 'billing_dispute_scheduler_tenants', '746b3846cb8b52da69d49e24979d3274', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_id) <> ROW(OLD.tenant_id)', ARRAY['tenant_id']::text[], 1),
    ('fn_trg_billing_disputes_transition_prd080', 'billing_disputes', '0bfb9c99bb9f426105a3584eb923e9cf', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.billing_dispute_sequence) <> ROW(OLD.billing_dispute_sequence)', ARRAY['billing_dispute_sequence']::text[], 1),
    ('fn_trg_billing_settlement_holds_transition_prd080', 'billing_settlement_holds', 'd52981b36f22092226ac9dafc6e1dcd0', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.billing_settlement_hold_id) <> ROW(OLD.billing_settlement_hold_id)', ARRAY['billing_settlement_hold_id']::text[], 1),
    ('fn_trg_branding_configs_update_prd088', 'branding_configs', '20f4b31dc988d0fe5cac46b13e88fa67', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.branding_config_id) <> ROW(OLD.branding_config_id)', ARRAY['branding_config_id']::text[], 1),
    ('fn_trg_branding_event_outbox_transition_prd088', 'branding_event_outbox', 'a28d7e1c18c95fec1ea5161381b99263', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.branding_event_outbox_id) <> ROW(OLD.branding_event_outbox_id)', ARRAY['branding_event_outbox_id']::text[], 1),
    ('fn_trg_cache_affinities_prd050_update', 'cache_affinities', '45675908f4072489ec80dfd1514011c8', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.cache_affinity_id) <> ROW(OLD.cache_affinity_id)', ARRAY['cache_affinity_id']::text[], 1),
    ('fn_trg_cache_affinity_bindings_prd050_identity_update', 'cache_affinity_bindings', '83a57763ffd92aeef0f5f41a9cd35b4d', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.cache_affinity_binding_id) <> ROW(OLD.cache_affinity_binding_id)', ARRAY['cache_affinity_binding_id']::text[], 1),
    ('fn_trg_cache_affinity_scopes_prd050_identity_update', 'cache_affinity_scopes', '865ca6b6e2151da34afdd6f6978be467', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.cache_affinity_scope_id) <> ROW(OLD.cache_affinity_scope_id)', ARRAY['cache_affinity_scope_id']::text[], 1),
    ('fn_trg_cache_affinity_selections_prd050_update', 'cache_affinity_selections', '27bc36b25fd4a46aacd4e7e9d7aaf841', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.cache_affinity_selection_id) <> ROW(OLD.cache_affinity_selection_id)', ARRAY['cache_affinity_selection_id']::text[], 1),
    ('fn_trg_chat_execution_grants_state_update_prd066', 'chat_execution_grants', 'ab5c187c47a683a2d0e7135ab256bc0a', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.chat_execution_grant_id) <> ROW(OLD.chat_execution_grant_id)', ARRAY['chat_execution_grant_id']::text[], 1),
    ('fn_trg_chat_messages_state_update_prd066', 'chat_messages', '4dd6208c6930a6777f98cda1493dcba4', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.chat_message_id) <> ROW(OLD.chat_message_id)', ARRAY['chat_message_id']::text[], 1),
    ('fn_trg_chat_prompt_invocations_state_update_prd067', 'chat_prompt_template_invocations', '7e7e33dfde95a6632ce6c5a99bdb6b24', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.prompt_template_invocation_id) <> ROW(OLD.prompt_template_invocation_id)', ARRAY['prompt_template_invocation_id']::text[], 1),
    ('fn_trg_chat_sessions_state_update_prd066', 'chat_sessions', '55e0e871ca33be707ab860508c09b2bb', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.chat_session_id) <> ROW(OLD.chat_session_id)', ARRAY['chat_session_id']::text[], 1),
    ('fn_trg_customer_health_scheduler_transition_prd086', 'customer_health_scheduler_tenants', 'e65f9fdc517fd72a7a59b84d0f46c5d3', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_id) <> ROW(OLD.tenant_id)', ARRAY['tenant_id']::text[], 1),
    ('fn_trg_customer_health_outbox_transition_prd086', 'customer_health_score_outbox', 'f54bd3ef2d3e58dfc032c043a0586a02', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.customer_health_score_outbox_id) <> ROW(OLD.customer_health_score_outbox_id)', ARRAY['customer_health_score_outbox_id']::text[], 1),
    ('fn_trg_deletion_requests_transition_prd089', 'deletion_requests', '2a239c273dcd1260768c02c374cea397', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.deletion_request_id) <> ROW(OLD.deletion_request_id)', ARRAY['deletion_request_id']::text[], 1),
    ('fn_trg_environments_experiment_scope_update_prd075', 'environments', '13c802de62639f00cf34f5a494192b77', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.environment_id) <> ROW(OLD.environment_id)', ARRAY['environment_id']::text[], 1),
    ('fn_trg_policy_template_environment_update_prd068', 'environments', 'f53fe5ef0d9249f77927c1a24b166d1d', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.environment_id) <> ROW(OLD.environment_id)', ARRAY['environment_id']::text[], 1),
    ('fn_trg_experiment_outbox_update_prd075', 'experiment_event_outbox', 'fa5bc4dbf77ee7d44e79eb6ad060765e', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.experiment_event_outbox_id) <> ROW(OLD.experiment_event_outbox_id)', ARRAY['experiment_event_outbox_id']::text[], 1),
    ('fn_trg_experiment_executions_update_prd075', 'experiment_request_executions', '7c09855743324c5317b298dd436824c8', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.execution_id) <> ROW(OLD.execution_id)', ARRAY['execution_id']::text[], 1),
    ('fn_trg_experiment_scheduler_update_prd075', 'experiment_scheduler_tenants', 'e004a81f89b8a8c694be5dcc97fc2fb1', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_id) <> ROW(OLD.tenant_id)', ARRAY['tenant_id']::text[], 1),
    ('fn_trg_experiment_settlements_update_prd075', 'experiment_usage_settlements', 'bfd40a3e66628581d82e0e1af6b0f22d', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.usage_settlement_id) <> ROW(OLD.usage_settlement_id)', ARRAY['usage_settlement_id']::text[], 1),
    ('fn_trg_file_security_event_outbox_state_update_prd073_recovery', 'file_security_event_outbox', '65d01c2882f71a5510c542c4345e2fa7', 'NEW.ctid=OLD.ctid', 'ROW(NEW.file_security_event_id) = ROW(OLD.file_security_event_id)', ARRAY['file_security_event_id']::text[], 1),
    ('fn_trg_local_model_call_outbox_transition_prd084', 'local_model_call_event_outbox', '21f60f3301fa5aae8c0f3e7f8547f487', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.local_model_call_event_outbox_id) <> ROW(OLD.local_model_call_event_outbox_id)', ARRAY['local_model_call_event_outbox_id']::text[], 1),
    ('fn_trg_local_model_catalog_entries_update_prd084', 'local_model_catalog_entries', '1b62462a493a518637574ce7d7d74670', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.local_model_catalog_entry_id) <> ROW(OLD.local_model_catalog_entry_id)', ARRAY['local_model_catalog_entry_id']::text[], 1),
    ('fn_trg_local_model_endpoints_update_prd084', 'local_model_endpoints', '3458c3c44396c57d3556570de7383c03', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.local_endpoint_id) <> ROW(OLD.local_endpoint_id)', ARRAY['local_endpoint_id']::text[], 1),
    ('fn_trg_mcp_attempts_transition_prd082', 'mcp_invocation_attempts', 'b21cab36c3b100103c97362b89e78c58', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_invocation_attempt_id) <> ROW(OLD.mcp_invocation_attempt_id)', ARRAY['mcp_invocation_attempt_id']::text[], 1),
    ('fn_trg_mcp_outbox_transition_prd082', 'mcp_invocation_event_outbox', 'a27b33f13376ba5a06e2c725e0785fea', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_invocation_event_outbox_id) <> ROW(OLD.mcp_invocation_event_outbox_id)', ARRAY['mcp_invocation_event_outbox_id']::text[], 1),
    ('fn_trg_mcp_invocations_transition_prd082', 'mcp_invocations', 'b8c14d9334b74e7c3b2e605a4445d93f', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_invocation_id) <> ROW(OLD.mcp_invocation_id)', ARRAY['mcp_invocation_id']::text[], 1),
    ('fn_trg_mcp_network_circuits_transition_prd083', 'mcp_network_circuits', '5a8d6d51b06f88fbd6fcb35d6579b872', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_id,NEW.mcp_server_id,NEW.policy_revision) <> ROW(OLD.tenant_id,OLD.mcp_server_id,OLD.policy_revision)', ARRAY['tenant_id','mcp_server_id','policy_revision']::text[], 1),
    ('fn_trg_mcp_network_event_outbox_transition_prd083', 'mcp_network_event_outbox', '74be7ed6e58f8585542b0729b6f82703', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_network_event_outbox_id) <> ROW(OLD.mcp_network_event_outbox_id)', ARRAY['mcp_network_event_outbox_id']::text[], 1),
    ('fn_trg_mcp_network_policies_transition_prd083', 'mcp_network_policies', '2b3d5ee2929cfd834b24c96e4b765c57', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_network_policy_id) <> ROW(OLD.mcp_network_policy_id)', ARRAY['mcp_network_policy_id']::text[], 1),
    ('fn_trg_mcp_credentials_transition_prd082', 'mcp_server_credentials', '03b384b0eabc79d32e568214b23ddcff', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_server_credential_id) <> ROW(OLD.mcp_server_credential_id)', ARRAY['mcp_server_credential_id']::text[], 1),
    ('fn_trg_mcp_operations_transition_prd082', 'mcp_server_operations', '339d992d9a72843c85954447a7aeac24', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_server_operation_id) <> ROW(OLD.mcp_server_operation_id)', ARRAY['mcp_server_operation_id']::text[], 1),
    ('fn_trg_mcp_sync_jobs_transition_prd082', 'mcp_server_sync_jobs', '8562c7a29a2b314ae9352cd1c38502b5', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_server_sync_job_id) <> ROW(OLD.mcp_server_sync_job_id)', ARRAY['mcp_server_sync_job_id']::text[], 1),
    ('fn_trg_mcp_servers_transition_prd082', 'mcp_servers', '6bcf6c2af1619e47d250e05bb5e53b12', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_server_id) <> ROW(OLD.mcp_server_id)', ARRAY['mcp_server_id']::text[], 1),
    ('fn_trg_mcp_tools_transition_prd082', 'mcp_tools', 'e64b2f452e367dc482b3380359ac4bd9', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.mcp_tool_id) <> ROW(OLD.mcp_tool_id)', ARRAY['mcp_tool_id']::text[], 1),
    ('fn_trg_aliases_experiment_scope_update_prd075', 'model_aliases', 'b78af71f484e19be9bfe75a704c8bc77', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.model_alias_id) <> ROW(OLD.model_alias_id)', ARRAY['model_alias_id']::text[], 1),
    ('fn_trg_policy_template_model_alias_update_prd068', 'model_aliases', 'bb05ddf1bf09283caf96b2ad03afaffe', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.model_alias_id) <> ROW(OLD.model_alias_id)', ARRAY['model_alias_id']::text[], 1),
    ('fn_trg_model_experiments_immutable_update_prd075', 'model_experiments', 'a5fd6d932597517862552576a93ce0b3', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.experiment_id) <> ROW(OLD.experiment_id)', ARRAY['experiment_id']::text[], 1),
    ('fn_trg_local_endpoint_model_update_prd084', 'models', '685a20871703a82dd80e954bf8e18429', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.model_id) <> ROW(OLD.model_id)', ARRAY['model_id']::text[], 1),
    ('fn_trg_models_experiment_arm_update_prd075', 'models', 'a07fcac98aac1a469bff4933b48275b9', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.model_id) <> ROW(OLD.model_id)', ARRAY['model_id']::text[], 1),
    ('fn_trg_policy_template_model_update_prd068', 'models', 'b8169b07557e2b295b29245485e97dfd', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.model_id) <> ROW(OLD.model_id)', ARRAY['model_id']::text[], 1),
    ('fn_trg_output_safety_scans_owner_update_prd071', 'output_safety_scans', '391460e4219e8ba4d3b74a95e23ee900', 'NEW.ctid IS DISTINCT FROM OLD.ctid', 'ROW(NEW.output_safety_scan_id) IS DISTINCT FROM ROW(OLD.output_safety_scan_id)', ARRAY['output_safety_scan_id']::text[], 1),
    ('fn_trg_output_safety_scans_transition_prd071', 'output_safety_scans', 'f6edf963c68e8148d63e31da9ea28c11', 'NEW.ctid IS NOT DISTINCT FROM OLD.ctid', 'ROW(NEW.output_safety_scan_id) IS NOT DISTINCT FROM ROW(OLD.output_safety_scan_id)', ARRAY['output_safety_scan_id']::text[], 1),
    ('fn_trg_partner_commission_cursor_transition_prd087', 'partner_commission_cursors', 'd787a8aeffcb733156004ca2a4a999d7', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_id,NEW.partner_customer_id) <> ROW(OLD.tenant_id,OLD.partner_customer_id)', ARRAY['tenant_id','partner_customer_id']::text[], 1),
    ('fn_trg_partner_customer_outbox_transition_prd087', 'partner_customer_outbox', 'f011237df65a1d908427080711e97a18', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.partner_customer_outbox_id) <> ROW(OLD.partner_customer_outbox_id)', ARRAY['partner_customer_outbox_id']::text[], 1),
    ('fn_trg_partner_customers_update_prd087', 'partner_customers', '3eebd5e10c99098546fae5c5a811d4bf', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.partner_customer_id) <> ROW(OLD.partner_customer_id)', ARRAY['partner_customer_id']::text[], 1),
    ('fn_trg_partners_update_prd087', 'partners', '11bd493472cb3ef44fcd25882a66de60', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.partner_id) <> ROW(OLD.partner_id)', ARRAY['partner_id']::text[], 1),
    ('fn_trg_plans_transition_prd079', 'plans', 'a3fd521af0748508ccf018de6d2bb60e', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.plan_id) <> ROW(OLD.plan_id)', ARRAY['plan_id']::text[], 1),
    ('fn_trg_policy_template_generated_policy_update_prd068', 'policies', '6c31eb6549e40606b19f01f8bb224d90', 'NEW.ctid=OLD.ctid', 'ROW(NEW.policy_id) = ROW(OLD.policy_id)', ARRAY['policy_id']::text[], 1),
    ('fn_trg_policy_template_applications_state_update_prd068', 'policy_template_applications', '15e44e309a695945cbf387567ee7b75a', 'NEW.ctid=OLD.ctid', 'ROW(NEW.application_id) = ROW(OLD.application_id)', ARRAY['application_id']::text[], 1),
    ('fn_trg_product_event_outbox_transition_prd085', 'product_event_outbox', '6110b8e49b7573af823dd2b6d5c6fe69', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.product_event_outbox_id) <> ROW(OLD.product_event_outbox_id)', ARRAY['product_event_outbox_id']::text[], 1),
    ('fn_trg_policy_template_project_update_prd068', 'projects', 'b94f829c53154875fd1ef26d23a63eb7', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.project_id) <> ROW(OLD.project_id)', ARRAY['project_id']::text[], 1),
    ('fn_trg_projects_experiment_scope_update_prd075', 'projects', '2f98adc29c91a51d4d86cb46c906703c', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.project_id) <> ROW(OLD.project_id)', ARRAY['project_id']::text[], 1),
    ('fn_trg_prompt_template_events_state_update_prd067', 'prompt_template_events', '7f01a1c8a77698bda04b6041b53d8442', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.prompt_template_event_id) <> ROW(OLD.prompt_template_event_id)', ARRAY['prompt_template_event_id']::text[], 1),
    ('fn_trg_prompt_template_versions_seal_update_prd067', 'prompt_template_versions', '009159ebe8c174e02926ace8e57a5e4b', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.prompt_template_version_id) <> ROW(OLD.prompt_template_version_id)', ARRAY['prompt_template_version_id']::text[], 1),
    ('fn_trg_prompt_templates_state_update_prd067', 'prompt_templates', 'c584eefffe476739ab6731fa1cb5e67b', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.prompt_template_id) <> ROW(OLD.prompt_template_id)', ARRAY['prompt_template_id']::text[], 1),
    ('fn_trg_provider_adapter_activations_immutable_update_prd063', 'provider_adapter_catalog_activations', '7f5484da477c088613e74c92bb44e405', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.activation_id) <> ROW(OLD.activation_id)', ARRAY['activation_id']::text[], 1),
    ('fn_trg_provider_error_map_events_state_update_prd056', 'provider_error_map_events', 'b0f36492dd28f670f07096c7ae1bfea4', 'NEW.ctid=OLD.ctid', 'ROW(NEW.provider_error_map_event_id) = ROW(OLD.provider_error_map_event_id)', ARRAY['provider_error_map_event_id']::text[], 2),
    ('fn_trg_provider_error_map_heads_update_prd056', 'provider_error_map_heads', '1090c65dea0fe88e39b3ce5d2c4fc59f', 'NEW.ctid=OLD.ctid', 'ROW(NEW.tenant_id) = ROW(OLD.tenant_id)', ARRAY['tenant_id']::text[], 1),
    ('fn_trg_provider_error_maps_update_prd056', 'provider_error_maps', '4c2c431749b89a20d98246ed9b295dd1', 'NEW.ctid=OLD.ctid', 'ROW(NEW.provider_error_map_id) = ROW(OLD.provider_error_map_id)', ARRAY['provider_error_map_id']::text[], 1),
    ('fn_trg_local_endpoint_provider_update_prd084', 'providers', '48a86615ba3c7955823e884bac550952', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.provider_id) <> ROW(OLD.provider_id)', ARRAY['provider_id']::text[], 1),
    ('fn_trg_policy_template_provider_update_prd068', 'providers', 'd9c40801f54744a65072025cd2212460', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.provider_id) <> ROW(OLD.provider_id)', ARRAY['provider_id']::text[], 1),
    ('fn_trg_providers_experiment_arm_update_prd075', 'providers', '13d0cb2410f587a06925401c78e4bb1c', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.provider_id) <> ROW(OLD.provider_id)', ARRAY['provider_id']::text[], 1),
    ('fn_trg_quota_buckets_transition_prd079', 'quota_buckets', '87c2214dbb18be83a43bed6a7f767894', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.quota_bucket_id) <> ROW(OLD.quota_bucket_id)', ARRAY['quota_bucket_id']::text[], 1),
    ('fn_trg_quota_outbox_transition_prd079', 'quota_event_outbox', 'd59738f869bbaf861e344710c53b43d6', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.quota_event_outbox_id) <> ROW(OLD.quota_event_outbox_id)', ARRAY['quota_event_outbox_id']::text[], 1),
    ('fn_trg_quota_reservation_items_transition_prd079', 'quota_reservation_items', '397c5f6ff6e97fc52c488eb2fb331917', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.quota_reservation_item_id) <> ROW(OLD.quota_reservation_item_id)', ARRAY['quota_reservation_item_id']::text[], 1),
    ('fn_trg_quota_reservations_transition_prd079', 'quota_reservations', '3476d2dc5a7a260d5b557a3fd6c8be73', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.quota_reservation_id) <> ROW(OLD.quota_reservation_id)', ARRAY['quota_reservation_id']::text[], 1),
    ('fn_trg_response_cache_fill_leases_update_prd076', 'response_cache_fill_leases', '050463dc59938ba8a116e19012715bdb', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.response_cache_fill_lease_id) <> ROW(OLD.response_cache_fill_lease_id)', ARRAY['response_cache_fill_lease_id']::text[], 1),
    ('fn_trg_response_cache_key_state_update_prd076', 'response_cache_key_state', '24e8a29031c7ccbdd0d30a689f5e0bcd', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.key_state_id) <> ROW(OLD.key_state_id)', ARRAY['key_state_id']::text[], 1),
    ('fn_trg_response_cache_policies_update_prd076', 'response_cache_policies', 'd49a7212ca09c006756e1a4a35e99513', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.cache_policy_id) <> ROW(OLD.cache_policy_id)', ARRAY['cache_policy_id']::text[], 1),
    ('fn_trg_response_cache_request_facts_update_prd076', 'response_cache_request_facts', '3213c0ed4746bf3bccf313519776842e', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.response_cache_request_fact_id) <> ROW(OLD.response_cache_request_fact_id)', ARRAY['response_cache_request_fact_id']::text[], 1),
    ('fn_trg_role_dashboard_configs_update_prd065', 'role_dashboard_configs', '7148a658fca6d47b69ccf1ac91049761', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.role_dashboard_config_id) <> ROW(OLD.role_dashboard_config_id)', ARRAY['role_dashboard_config_id']::text[], 1),
    ('fn_trg_roles_matrix_update_prd059', 'roles', '1e63bff3ed05ee9af2bed17a1c53c512', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.role_id) <> ROW(OLD.role_id)', ARRAY['role_id']::text[], 1),
    ('fn_trg_routing_attempt_local_fence_update_prd084', 'routing_attempts', '41cfef7da8bf11d16aff9ac32e42cc35', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.routing_attempt_id) <> ROW(OLD.routing_attempt_id)', ARRAY['routing_attempt_id']::text[], 1),
    ('fn_trg_routing_attempt_provider_error_fact_update_prd056', 'routing_attempts', '6d03f2eb3195695db1487794f6e0f6f1', 'NEW.ctid IS DISTINCT FROM OLD.ctid', 'ROW(NEW.routing_attempt_id) IS DISTINCT FROM ROW(OLD.routing_attempt_id)', ARRAY['routing_attempt_id']::text[], 1),
    ('fn_trg_security_event_outbox_identity_update_prd081', 'security_event_outbox', '6933db58688e021c4e3eb31af1300abc', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.security_event_outbox_id) <> ROW(OLD.security_event_outbox_id)', ARRAY['security_event_outbox_id']::text[], 1),
    ('fn_trg_session_risk_freezes_release_update_prd070', 'session_risk_freezes', '66ba8166e50bbf2bac9bb3bab8cbdff4', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.session_risk_freeze_id) <> ROW(OLD.session_risk_freeze_id)', ARRAY['session_risk_freeze_id']::text[], 1),
    ('fn_trg_session_risk_scores_state_update_prd070', 'session_risk_scores', '4c2bba8a0c1fb42abcbce5f6fbf0a63c', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.session_risk_score_id) <> ROW(OLD.session_risk_score_id)', ARRAY['session_risk_score_id']::text[], 1),
    ('fn_trg_stream_session_provider_error_fact_update_prd056', 'stream_sessions', '5db178da1575c1c8873af113418fd6b2', 'NEW.ctid IS DISTINCT FROM OLD.ctid', 'ROW(NEW.stream_session_id) IS DISTINCT FROM ROW(OLD.stream_session_id)', ARRAY['stream_session_id']::text[], 1),
    ('fn_trg_tenant_data_states_transition_prd089', 'tenant_data_states', '0b46001af7e8b87d9b362538ed22b474', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_id) <> ROW(OLD.tenant_id)', ARRAY['tenant_id']::text[], 1),
    ('fn_trg_tenant_export_jobs_transition_prd089', 'tenant_export_jobs', 'b162dc022b23886e9c20d40d8a929f85', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_export_job_id) <> ROW(OLD.tenant_export_job_id)', ARRAY['tenant_export_job_id']::text[], 1),
    ('fn_trg_tenant_lifecycle_outbox_transition_prd089', 'tenant_lifecycle_event_outbox', '3343181cfc7e18bb35d0f3d95ed24c88', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.tenant_lifecycle_event_outbox_id) <> ROW(OLD.tenant_lifecycle_event_outbox_id)', ARRAY['tenant_lifecycle_event_outbox_id']::text[], 1),
    ('fn_trg_virtual_keys_scope_update_prd075', 'virtual_keys', 'cfa71844b5068959dbc4e8c89640e9e2', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.virtual_key_id) <> ROW(OLD.virtual_key_id)', ARRAY['virtual_key_id']::text[], 1),
    ('fn_trg_workspace_health_transition_prd096', 'workspace_health_projections', '839360ecfd1b7e342002ace3f11e7e16', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.workspace_health_projection_id) <> ROW(OLD.workspace_health_projection_id)', ARRAY['workspace_health_projection_id']::text[], 1),
    ('fn_trg_workspace_operational_outbox_transition_prd096', 'workspace_operational_event_outbox', 'fb906c9251639f46b9ed35fc1f9caa50', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.workspace_operational_event_outbox_id) <> ROW(OLD.workspace_operational_event_outbox_id)', ARRAY['workspace_operational_event_outbox_id']::text[], 1),
    ('fn_trg_workspace_notification_transition_prd096', 'workspace_owner_notification_outbox', '319de763eea277a072aacaf1a1db05c8', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.workspace_owner_notification_id) <> ROW(OLD.workspace_owner_notification_id)', ARRAY['workspace_owner_notification_id']::text[], 1),
    ('fn_trg_workspace_risk_tags_transition_prd096', 'workspace_risk_tags', 'a57b08496081cda0f1b91ea6bac6a0f8', 'NEW.ctid<>OLD.ctid', 'ROW(NEW.workspace_risk_tag_id) <> ROW(OLD.workspace_risk_tag_id)', ARRAY['workspace_risk_tag_id']::text[], 1)
  ) AS reviewed(function_name,table_name,source_md5,old_expression,
                new_expression,key_columns,occurrences)
  LOOP
    function_oid := to_regprocedure(format('public.%I()',patch.function_name));
    IF function_oid IS NULL THEN
      RAISE EXCEPTION 'logical_identity_repair_function_missing';
    END IF;
    SELECT prosrc,pg_get_functiondef(oid) INTO original_body,original_definition
      FROM pg_proc WHERE oid=function_oid;
    IF md5(original_body)<>patch.source_md5 OR
       (length(original_body)-length(replace(original_body,patch.old_expression,'')))
         <>length(patch.old_expression)*patch.occurrences THEN
      RAISE EXCEPTION 'logical_identity_repair_source_mismatch';
    END IF;
    SELECT count(*) INTO bindings FROM pg_trigger t
      JOIN pg_class c ON c.oid=t.tgrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE t.tgfoid=function_oid AND NOT t.tgisinternal AND t.tgtype=19
        AND n.nspname='public' AND c.relname=patch.table_name;
    IF bindings<>1 THEN
      RAISE EXCEPTION 'logical_identity_repair_binding_mismatch';
    END IF;
    SELECT ARRAY(SELECT a.attname::text FROM pg_index i
      JOIN unnest(i.indkey) WITH ORDINALITY k(attnum,n) ON true
      JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
      WHERE i.indrelid=to_regclass(format('public.%I',patch.table_name))
        AND i.indisprimary ORDER BY k.n) INTO actual_keys;
    IF actual_keys IS DISTINCT FROM patch.key_columns THEN
      RAISE EXCEPTION 'logical_identity_repair_key_mismatch';
    END IF;
    EXECUTE replace(original_definition,patch.old_expression,patch.new_expression);
  END LOOP;
END;
$repair$;
