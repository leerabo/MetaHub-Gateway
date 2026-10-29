-- Retire personal workspaces. Purge only their workspace/tenant identifiers.
-- Deletion guards are restored inside the same transaction; failures roll everything back.
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE retired_personal_workspaces(workspace_id TEXT PRIMARY KEY, tenant_id TEXT);
INSERT INTO retired_personal_workspaces SELECT w.workspace_id,b.tenant_id FROM workspaces w LEFT JOIN workspace_tenant_bindings b ON b.workspace_id=w.workspace_id WHERE w.kind='personal';
DROP TRIGGER "trg_access_token_grants_delete_prd118";
DROP TRIGGER "trg_account_identity_links_delete_prd091";
DROP TRIGGER "trg_activation_checkpoints_delete_prd097";
DROP TRIGGER "trg_adapter_contract_cases_immutable_delete_prd064";
DROP TRIGGER "trg_adapter_contract_event_immutable_delete_prd064";
DROP TRIGGER "trg_adapter_contract_runs_immutable_delete_prd064";
DROP TRIGGER "trg_aliases_experiment_scope_delete_prd075";
DROP TRIGGER "trg_approval_challenges_delete_prd078";
DROP TRIGGER "trg_approval_decisions_delete_prd078";
DROP TRIGGER "trg_approval_events_delete_prd078";
DROP TRIGGER "trg_approval_guards_delete_prd078";
DROP TRIGGER "trg_approval_outbox_delete_prd078";
DROP TRIGGER "trg_approval_replays_delete_prd078";
DROP TRIGGER "trg_approval_scheduler_delete_prd078";
DROP TRIGGER "trg_approval_tasks_delete_prd078";
DROP TRIGGER "trg_approval_writebacks_delete_prd078";
DROP TRIGGER "trg_assistant_audits_delete_prd077";
DROP TRIGGER "trg_assistant_queries_delete_prd077";
DROP TRIGGER "trg_assurance_archive_delete_prd137";
DROP TRIGGER "trg_assurance_cleanup_delete_prd137";
DROP TRIGGER "trg_assurance_command_delete_prd137";
DROP TRIGGER "trg_assurance_event_delete_prd137";
DROP TRIGGER "trg_assurance_outbox_delete_prd137";
DROP TRIGGER "trg_audit_export_events_delete_prd058";
DROP TRIGGER "trg_audit_exports_delete_prd058";
DROP TRIGGER "trg_audit_log_export_sequences_delete_prd058";
DROP TRIGGER "trg_audit_logs_immutable_delete";
DROP TRIGGER "trg_backup_sets_delete_prd100";
DROP TRIGGER "trg_batch_allocations_delete_prd074";
DROP TRIGGER "trg_batch_attempts_delete_prd074";
DROP TRIGGER "trg_batch_events_delete_prd074";
DROP TRIGGER "trg_batch_grants_delete_prd074";
DROP TRIGGER "trg_batch_holds_delete_prd074";
DROP TRIGGER "trg_batch_jobs_delete_prd074";
DROP TRIGGER "trg_batch_outbox_delete_prd074";
DROP TRIGGER "trg_batch_reconciliation_delete_prd074";
DROP TRIGGER "trg_batch_scheduler_delete_prd074";
DROP TRIGGER "trg_batch_tasks_delete_prd074";
DROP TRIGGER "trg_billing_dispute_adjustment_links_delete_prd080";
DROP TRIGGER "trg_billing_dispute_events_delete_prd080";
DROP TRIGGER "trg_billing_dispute_items_delete_prd080";
DROP TRIGGER "trg_billing_dispute_outbox_delete_prd080";
DROP TRIGGER "trg_billing_dispute_scheduler_delete_prd080";
DROP TRIGGER "trg_billing_dispute_transitions_delete_prd080";
DROP TRIGGER "trg_billing_disputes_delete_prd080";
DROP TRIGGER "trg_billing_domain_reconciliation_difference_delete_prd144";
DROP TRIGGER "trg_billing_domain_reconciliation_source_delete_prd144";
DROP TRIGGER "trg_billing_settlement_holds_delete_prd080";
DROP TRIGGER "trg_billing_statements_delete_prd104";
DROP TRIGGER "trg_brand_legacy_mapping_delete_prd142";
DROP TRIGGER "trg_branding_configs_cutover_delete_prd142";
DROP TRIGGER "trg_branding_configs_delete_prd088";
DROP TRIGGER "trg_branding_event_outbox_delete_prd088";
DROP TRIGGER "trg_branding_events_delete_prd088";
DROP TRIGGER "trg_budget_scope_events_prd052_immutable_delete";
DROP TRIGGER "trg_business_verification_intent_delete_prd102";
DROP TRIGGER "trg_cache_affinities_prd050_delete";
DROP TRIGGER "trg_cache_affinity_bindings_prd050_delete";
DROP TRIGGER "trg_cache_affinity_config_events_prd050_immutable_delete";
DROP TRIGGER "trg_cache_affinity_scopes_prd050_delete";
DROP TRIGGER "trg_cache_affinity_selections_prd050_delete";
DROP TRIGGER "trg_cache_watermarks_delete_prd108";
DROP TRIGGER "trg_channel_action_delete_prd139";
DROP TRIGGER "trg_channel_brands_delete_prd142";
DROP TRIGGER "trg_channel_event_delete_prd139";
DROP TRIGGER "trg_channel_mapping_delete_prd139";
DROP TRIGGER "trg_channel_membership_delete_prd139";
DROP TRIGGER "trg_channel_outbox_delete_prd139";
DROP TRIGGER "trg_channel_partner_delete_prd139";
DROP TRIGGER "trg_channel_rule_delete_prd139";
DROP TRIGGER "trg_channel_statement_delete_prd139";
DROP TRIGGER "trg_channel_statement_line_delete_prd139";
DROP TRIGGER "trg_chat_execution_grants_delete_prd066";
DROP TRIGGER "trg_chat_messages_delete_prd066";
DROP TRIGGER "trg_chat_prompt_invocations_delete_prd067";
DROP TRIGGER "trg_chat_sessions_delete_prd066";
DROP TRIGGER "trg_client_credentials_delete_prd118";
DROP TRIGGER "trg_commercial_effects_delete_prd103";
DROP TRIGGER "trg_commission_records_delete_prd087";
DROP TRIGGER "trg_compliance_commands_delete_prd135";
DROP TRIGGER "trg_compliance_events_delete_prd135";
DROP TRIGGER "trg_compliance_evidence_delete_prd135";
DROP TRIGGER "trg_compliance_exceptions_delete_prd135";
DROP TRIGGER "trg_compliance_outbox_delete_prd135";
DROP TRIGGER "trg_compliance_remediation_delete_prd135";
DROP TRIGGER "trg_compliance_replay_delete_prd135";
DROP TRIGGER "trg_compliance_report_items_delete_prd135";
DROP TRIGGER "trg_compliance_reports_delete_prd135";
DROP TRIGGER "trg_config_acks_delete_prd108";
DROP TRIGGER "trg_config_deltas_delete_prd108";
DROP TRIGGER "trg_config_event_outbox_delete_prd108";
DROP TRIGGER "trg_config_exports_immutable_delete_prd062";
DROP TRIGGER "trg_config_import_rows_immutable_delete_prd062";
DROP TRIGGER "trg_config_imports_immutable_delete_prd062";
DROP TRIGGER "trg_config_rebuild_jobs_delete_prd108";
DROP TRIGGER "trg_config_snapshot_heads_delete_prd108";
DROP TRIGGER "trg_config_snapshots_delete_prd108";
DROP TRIGGER "trg_console_backfill_checkpoint_delete_prd147";
DROP TRIGGER "trg_console_migration_event_delete_prd147";
DROP TRIGGER "trg_consumers_delete";
DROP TRIGGER "trg_contract_profiles_delete_prd102";
DROP TRIGGER "trg_cost_anomalies_immutable_delete_prd053";
DROP TRIGGER "trg_cost_anomaly_actions_immutable_delete_prd053";
DROP TRIGGER "trg_cost_anomaly_runs_immutable_delete_prd053";
DROP TRIGGER "trg_customer_health_outbox_delete_prd086";
DROP TRIGGER "trg_customer_health_scheduler_delete_prd086";
DROP TRIGGER "trg_customer_health_scores_delete_prd086";
DROP TRIGGER "trg_data_classifications_delete_prd117";
DROP TRIGGER "trg_data_governance_commands_delete_prd117";
DROP TRIGGER "trg_data_governance_events_delete_prd117";
DROP TRIGGER "trg_data_governance_fences_delete_prd117";
DROP TRIGGER "trg_data_governance_outbox_delete_prd117";
DROP TRIGGER "trg_data_subject_requests_delete_prd134";
DROP TRIGGER "trg_deletion_requests_delete_prd089";
DROP TRIGGER "trg_deletion_schedules_delete_prd117";
DROP TRIGGER "trg_deployment_brands_delete_prd142";
DROP TRIGGER "trg_dunning_cases_delete_prd103";
DROP TRIGGER "trg_edge_acks_delete_prd121";
DROP TRIGGER "trg_edge_apply_attempts_delete_prd121";
DROP TRIGGER "trg_edge_backup_events_delete_prd129";
DROP TRIGGER "trg_edge_backup_outbox_delete_prd129";
DROP TRIGGER "trg_edge_backup_reports_delete_prd129";
DROP TRIGGER "trg_edge_backup_status_delete_prd129";
DROP TRIGGER "trg_edge_capacity_grants_delete_prd128";
DROP TRIGGER "trg_edge_delivery_acceptances_delete_prd125";
DROP TRIGGER "trg_edge_delivery_events_delete_prd125";
DROP TRIGGER "trg_edge_delivery_outbox_delete_prd125";
DROP TRIGGER "trg_edge_desired_approvals_delete_prd121";
DROP TRIGGER "trg_edge_desired_events_delete_prd121";
DROP TRIGGER "trg_edge_desired_heads_delete_prd121";
DROP TRIGGER "trg_edge_desired_states_delete_prd121";
DROP TRIGGER "trg_edge_diagnostic_actions_delete_prd126";
DROP TRIGGER "trg_edge_diagnostic_commands_delete_prd126";
DROP TRIGGER "trg_edge_diagnostic_events_delete_prd126";
DROP TRIGGER "trg_edge_diagnostic_grants_delete_prd126";
DROP TRIGGER "trg_edge_diagnostic_outbox_delete_prd126";
DROP TRIGGER "trg_edge_diagnostic_results_delete_prd126";
DROP TRIGGER "trg_edge_failure_summaries_delete_prd122";
DROP TRIGGER "trg_edge_fleet_index_delete_prd126";
DROP TRIGGER "trg_edge_fleet_upgrade_events_delete_prd127";
DROP TRIGGER "trg_edge_fleet_upgrade_outbox_delete_prd127";
DROP TRIGGER "trg_edge_health_heads_delete_prd126";
DROP TRIGGER "trg_edge_health_snapshots_delete_prd126";
DROP TRIGGER "trg_edge_identity_events_delete_prd120";
DROP TRIGGER "trg_edge_install_audits_delete_prd124";
DROP TRIGGER "trg_edge_install_commands_delete_prd124";
DROP TRIGGER "trg_edge_install_outbox_delete_prd124";
DROP TRIGGER "trg_edge_installations_delete_prd124";
DROP TRIGGER "trg_edge_license_acks_delete_prd128";
DROP TRIGGER "trg_edge_license_audits_delete_prd128";
DROP TRIGGER "trg_edge_license_lease_reports_delete_prd128";
DROP TRIGGER "trg_edge_license_outbox_delete_prd128";
DROP TRIGGER "trg_edge_license_refresh_delete_prd128";
DROP TRIGGER "trg_edge_licenses_delete_prd128";
DROP TRIGGER "trg_edge_lkg_delete_prd121";
DROP TRIGGER "trg_edge_machine_commands_delete_prd120";
DROP TRIGGER "trg_edge_maintenance_windows_delete_prd127";
DROP TRIGGER "trg_edge_offline_bundles_delete_prd130";
DROP TRIGGER "trg_edge_offline_conflicts_delete_prd130";
DROP TRIGGER "trg_edge_offline_cursors_delete_prd130";
DROP TRIGGER "trg_edge_offline_events_delete_prd130";
DROP TRIGGER "trg_edge_offline_outbox_delete_prd130";
DROP TRIGGER "trg_edge_offline_requests_delete_prd130";
DROP TRIGGER "trg_edge_offline_resolutions_delete_prd130";
DROP TRIGGER "trg_edge_offline_transfers_delete_prd130";
DROP TRIGGER "trg_edge_preflight_reports_delete_prd124";
DROP TRIGGER "trg_edge_release_heads_delete_prd127";
DROP TRIGGER "trg_edge_restore_jobs_delete_prd129";
DROP TRIGGER "trg_edge_restore_verifications_delete_prd129";
DROP TRIGGER "trg_edge_rollback_attempts_delete_prd127";
DROP TRIGGER "trg_edge_rollout_targets_delete_prd127";
DROP TRIGGER "trg_edge_secret_events_delete_prd123";
DROP TRIGGER "trg_edge_secret_outbox_delete_prd123";
DROP TRIGGER "trg_edge_secret_references_delete_prd123";
DROP TRIGGER "trg_edge_secret_sync_batches_delete_prd123";
DROP TRIGGER "trg_edge_secret_sync_receipts_delete_prd123";
DROP TRIGGER "trg_edge_signed_entitlements_delete_prd128";
DROP TRIGGER "trg_edge_signed_snapshots_delete_prd121";
DROP TRIGGER "trg_edge_snapshot_artifacts_delete_prd121";
DROP TRIGGER "trg_edge_support_bundles_delete_prd125";
DROP TRIGGER "trg_edge_support_downloads_delete_prd125";
DROP TRIGGER "trg_edge_telemetry_batches_delete_prd122";
DROP TRIGGER "trg_edge_telemetry_events_delete_prd122";
DROP TRIGGER "trg_edge_telemetry_policies_delete_prd122";
DROP TRIGGER "trg_edge_telemetry_policy_heads_delete_prd122";
DROP TRIGGER "trg_edge_telemetry_receipts_delete_prd122";
DROP TRIGGER "trg_edge_upgrade_commands_delete_prd127";
DROP TRIGGER "trg_edge_upgrade_receipts_delete_prd127";
DROP TRIGGER "trg_edge_usage_cursors_delete_prd122";
DROP TRIGGER "trg_edge_usage_reconciliations_delete_prd122";
DROP TRIGGER "trg_edge_validation_checks_delete_prd125";
DROP TRIGGER "trg_edge_validation_commands_delete_prd125";
DROP TRIGGER "trg_edge_validation_runs_delete_prd125";
DROP TRIGGER "trg_enterprise_identity_commands_delete_prd114";
DROP TRIGGER "trg_enterprise_identity_events_delete_prd114";
DROP TRIGGER "trg_enterprise_identity_session_grants_delete_prd114";
DROP TRIGGER "trg_entitlement_snapshots_delete_prd098";
DROP TRIGGER "trg_erasure_decisions_delete_prd134";
DROP TRIGGER "trg_experiment_assignments_delete_prd075";
DROP TRIGGER "trg_experiment_events_delete_prd075";
DROP TRIGGER "trg_experiment_executions_delete_prd075";
DROP TRIGGER "trg_experiment_feedback_delete_prd075";
DROP TRIGGER "trg_experiment_outbox_delete_prd075";
DROP TRIGGER "trg_experiment_outcomes_delete_prd075";
DROP TRIGGER "trg_experiment_results_delete_prd075";
DROP TRIGGER "trg_experiment_settlements_delete_prd075";
DROP TRIGGER "trg_feature_event_delete_prd107";
DROP TRIGGER "trg_feature_exposures_delete_prd107";
DROP TRIGGER "trg_federation_links_delete_prd114";
DROP TRIGGER "trg_file_dlp_matches_delete_prd072";
DROP TRIGGER "trg_file_events_delete_prd072";
DROP TRIGGER "trg_file_extracts_delete_prd072";
DROP TRIGGER "trg_file_security_event_outbox_delete_prd073_recovery";
DROP TRIGGER "trg_file_security_events_delete_prd073";
DROP TRIGGER "trg_file_security_findings_delete_prd073";
DROP TRIGGER "trg_file_security_scans_delete_prd073";
DROP TRIGGER "trg_finops_adjustments_immutable_delete_prd051";
DROP TRIGGER "trg_finops_ledger_disputes_delete_prd071";
DROP TRIGGER "trg_finops_ledger_immutable_delete_prd051";
DROP TRIGGER "trg_flag_evaluations_delete_prd107";
DROP TRIGGER "trg_ga_launch_delete_guard";
DROP TRIGGER "trg_gateway_cell_events_delete_prd101";
DROP TRIGGER "trg_gateway_claims_scope_delete_prd075";
DROP TRIGGER "trg_gateway_placements_delete_prd101";
DROP TRIGGER "trg_gateway_request_claims_immutable_delete";
DROP TRIGGER "trg_gateway_scope_claims_delete_prd075";
DROP TRIGGER "trg_growth_assignment_delete_prd141";
DROP TRIGGER "trg_growth_attribution_delete_prd141";
DROP TRIGGER "trg_growth_eligibility_delete_prd141";
DROP TRIGGER "trg_growth_export_subject_delete_prd141";
DROP TRIGGER "trg_growth_legacy_mapping_delete_prd141";
DROP TRIGGER "trg_growth_legacy_reconciliation_delete_prd141";
DROP TRIGGER "trg_identity_exports_delete_prd134";
DROP TRIGGER "trg_identity_security_events_delete_prd116";
DROP TRIGGER "trg_incident_scopes_delete_prd112";
DROP TRIGGER "trg_intent_risk_events_delete_prd069";
DROP TRIGGER "trg_intent_risk_results_delete_prd069";
DROP TRIGGER "trg_invitation_events_delete_prd060";
DROP TRIGGER "trg_invitation_identity_bindings_delete_prd094";
DROP TRIGGER "trg_invitations_delete_prd060";
DROP TRIGGER "trg_invoice_artifacts_delete_prd104";
DROP TRIGGER "trg_invoice_download_grants_delete_prd104";
DROP TRIGGER "trg_invoice_requests_delete_prd104";
DROP TRIGGER "trg_isolation_run_delete_prd137";
DROP TRIGGER "trg_key_consumers_delete";
DROP TRIGGER "trg_launch_action_delete_prd138";
DROP TRIGGER "trg_launch_artifact_delete_prd138";
DROP TRIGGER "trg_launch_event_delete_prd138";
DROP TRIGGER "trg_launch_evidence_delete_prd138";
DROP TRIGGER "trg_launch_outbox_delete_prd138";
DROP TRIGGER "trg_launch_review_delete_prd138";
DROP TRIGGER "trg_launch_rollback_delete_prd138";
DROP TRIGGER "trg_launch_wave_delete_prd138";
DROP TRIGGER "trg_legacy_commissions_delete_prd139";
DROP TRIGGER "trg_legacy_health_outbox_delete_prd140";
DROP TRIGGER "trg_legacy_health_scheduler_delete_prd140";
DROP TRIGGER "trg_legacy_health_scores_delete_prd140";
DROP TRIGGER "trg_legacy_partner_cursors_delete_prd139";
DROP TRIGGER "trg_legacy_partner_customers_delete_prd139";
DROP TRIGGER "trg_legacy_partner_operations_delete_prd139";
DROP TRIGGER "trg_legacy_partner_outbox_delete_prd139";
DROP TRIGGER "trg_legacy_partners_delete_prd139";
DROP TRIGGER "trg_legacy_workspace_health_delete_prd140";
DROP TRIGGER "trg_legal_entity_delete_prd102";
DROP TRIGGER "trg_legal_holds_delete_prd117";
DROP TRIGGER "trg_local_endpoint_create_receipts_delete_prd084";
DROP TRIGGER "trg_local_endpoint_model_delete_prd084";
DROP TRIGGER "trg_local_endpoint_provider_delete_prd084";
DROP TRIGGER "trg_local_model_call_events_delete_prd084";
DROP TRIGGER "trg_local_model_call_outbox_delete_prd084";
DROP TRIGGER "trg_local_model_catalog_entries_delete_prd084";
DROP TRIGGER "trg_local_model_endpoints_delete_prd084";
DROP TRIGGER "trg_local_workspace_admission_delete";
DROP TRIGGER "trg_machine_identity_audits_delete_prd118";
DROP TRIGGER "trg_machine_identity_commands_delete_prd118";
DROP TRIGGER "trg_machine_identity_outbox_delete_prd118";
DROP TRIGGER "trg_maintenance_delete_prd112";
DROP TRIGGER "trg_mcp_attempts_delete_prd082";
DROP TRIGGER "trg_mcp_credentials_delete_prd082";
DROP TRIGGER "trg_mcp_events_delete_prd082";
DROP TRIGGER "trg_mcp_invocations_delete_prd082";
DROP TRIGGER "trg_mcp_network_bindings_delete_prd083";
DROP TRIGGER "trg_mcp_network_call_intents_delete_prd083";
DROP TRIGGER "trg_mcp_network_circuits_delete_prd083";
DROP TRIGGER "trg_mcp_network_decisions_delete_prd083";
DROP TRIGGER "trg_mcp_network_event_outbox_delete_prd083";
DROP TRIGGER "trg_mcp_network_events_delete_prd083";
DROP TRIGGER "trg_mcp_network_policies_delete_prd083";
DROP TRIGGER "trg_mcp_network_revisions_delete_prd083";
DROP TRIGGER "trg_mcp_operations_delete_prd082";
DROP TRIGGER "trg_mcp_outbox_delete_prd082";
DROP TRIGGER "trg_mcp_role_grants_delete_prd082";
DROP TRIGGER "trg_mcp_scheduler_delete_prd082";
DROP TRIGGER "trg_mcp_servers_delete_prd082";
DROP TRIGGER "trg_mcp_sync_jobs_delete_prd082";
DROP TRIGGER "trg_mcp_tools_delete_prd082";
DROP TRIGGER "trg_mcp_user_grants_delete_prd082";
DROP TRIGGER "trg_membership_events_delete_prd094";
DROP TRIGGER "trg_membership_invitations_delete_prd094";
DROP TRIGGER "trg_meter_recovery_delete";
DROP TRIGGER "trg_migration_assessments_delete_prd131";
DROP TRIGGER "trg_migration_checkpoints_delete_prd131";
DROP TRIGGER "trg_migration_cutover_heads_delete_prd131";
DROP TRIGGER "trg_migration_differences_delete_prd131";
DROP TRIGGER "trg_migration_events_delete_prd131";
DROP TRIGGER "trg_migration_outbox_delete_prd131";
DROP TRIGGER "trg_migration_plans_delete_prd131";
DROP TRIGGER "trg_migration_rollback_plans_delete_prd131";
DROP TRIGGER "trg_migration_runs_delete_prd131";
DROP TRIGGER "trg_migration_snapshots_delete_prd131";
DROP TRIGGER "trg_migration_support_audits_delete_prd131";
DROP TRIGGER "trg_model_experiments_delete_prd075";
DROP TRIGGER "trg_model_feedback_statistics_delete_prd049";
DROP TRIGGER "trg_model_quality_score_events_prd048_immutable_delete";
DROP TRIGGER "trg_model_quality_scores_prd048_delete";
DROP TRIGGER "trg_models_experiment_arm_delete_prd075";
DROP TRIGGER "trg_notification_outbox_delete_prd105";
DROP TRIGGER "trg_notification_preferences_delete_prd105";
DROP TRIGGER "trg_notifications_delete_prd105";
DROP TRIGGER "trg_oauth_clients_delete_prd118";
DROP TRIGGER "trg_onboarding_ack_delete_prd097";
DROP TRIGGER "trg_onboarding_events_delete_prd097";
DROP TRIGGER "trg_onboarding_progress_delete_prd097";
DROP TRIGGER "trg_onboarding_step_facts_delete_prd097";
DROP TRIGGER "trg_output_safety_events_delete_prd071";
DROP TRIGGER "trg_output_safety_scans_delete_prd071";
DROP TRIGGER "trg_owner_transfers_delete_prd094";
DROP TRIGGER "trg_partner_commission_cursor_delete_prd087";
DROP TRIGGER "trg_partner_customer_outbox_delete_prd087";
DROP TRIGGER "trg_partner_customers_delete_prd087";
DROP TRIGGER "trg_partner_operations_delete_prd087";
DROP TRIGGER "trg_partners_delete_prd087";
DROP TRIGGER "trg_patch_release_delete_prd136";
DROP TRIGGER "trg_payment_intents_delete_prd103";
DROP TRIGGER "trg_pen_test_finding_delete_prd137";
DROP TRIGGER "trg_permissions_delete_prd059";
DROP TRIGGER "trg_plans_delete_prd079";
DROP TRIGGER "trg_platform_action_intents_delete_prd096";
DROP TRIGGER "trg_platform_billing_adjustments_delete_prd099";
DROP TRIGGER "trg_platform_billing_summaries_delete_prd099";
DROP TRIGGER "trg_platform_customer_account_delete_prd140";
DROP TRIGGER "trg_platform_customer_command_delete_prd140";
DROP TRIGGER "trg_platform_customer_contact_delete_prd140";
DROP TRIGGER "trg_platform_customer_event_delete_prd140";
DROP TRIGGER "trg_platform_customer_health_delete_prd140";
DROP TRIGGER "trg_platform_customer_health_head_delete_prd140";
DROP TRIGGER "trg_platform_customer_mapping_delete_prd140";
DROP TRIGGER "trg_platform_customer_outbox_delete_prd140";
DROP TRIGGER "trg_platform_customer_plan_delete_prd140";
DROP TRIGGER "trg_platform_customer_scheduler_delete_prd140";
DROP TRIGGER "trg_platform_customer_task_delete_prd140";
DROP TRIGGER "trg_platform_meter_readings_delete_prd099";
DROP TRIGGER "trg_platform_qbr_delete_prd140";
DROP TRIGGER "trg_platform_quota_allocations_delete_prd099";
DROP TRIGGER "trg_platform_renewal_delete_prd140";
DROP TRIGGER "trg_policy_events_immutable_delete";
DROP TRIGGER "trg_policy_explanations_immutable_delete";
DROP TRIGGER "trg_policy_snapshot_events_immutable_delete";
DROP TRIGGER "trg_policy_snapshot_heads_delete";
DROP TRIGGER "trg_policy_snapshot_items_delete_guard";
DROP TRIGGER "trg_policy_snapshots_delete_guard";
DROP TRIGGER "trg_policy_template_application_items_delete_prd068";
DROP TRIGGER "trg_policy_template_applications_delete_prd068";
DROP TRIGGER "trg_policy_template_dependencies_delete_prd068";
DROP TRIGGER "trg_policy_template_environment_delete_prd068";
DROP TRIGGER "trg_policy_template_events_delete_prd068";
DROP TRIGGER "trg_policy_template_generated_action_delete_prd068";
DROP TRIGGER "trg_policy_template_generated_policy_delete_prd068";
DROP TRIGGER "trg_policy_template_generated_rule_delete_prd068";
DROP TRIGGER "trg_policy_template_model_alias_delete_prd068";
DROP TRIGGER "trg_policy_template_model_delete_prd068";
DROP TRIGGER "trg_policy_template_project_delete_prd068";
DROP TRIGGER "trg_policy_template_provider_delete_prd068";
DROP TRIGGER "trg_policy_templates_delete_prd068";
DROP TRIGGER "trg_postmortems_delete_prd112";
DROP TRIGGER "trg_price_snapshots_immutable_delete_prd051";
DROP TRIGGER "trg_privacy_commands_delete_prd134";
DROP TRIGGER "trg_privacy_events_delete_prd134";
DROP TRIGGER "trg_privacy_outbox_delete_prd134";
DROP TRIGGER "trg_privacy_owner_jobs_delete_prd134";
DROP TRIGGER "trg_privacy_preferences_delete_prd134";
DROP TRIGGER "trg_private_dr_drills_delete_prd129";
DROP TRIGGER "trg_product_event_outbox_delete_prd085";
DROP TRIGGER "trg_product_events_delete_prd085";
DROP TRIGGER "trg_prompt_template_events_delete_prd067";
DROP TRIGGER "trg_prompt_template_operations_delete_prd067";
DROP TRIGGER "trg_prompt_template_version_models_delete_prd067";
DROP TRIGGER "trg_prompt_template_version_projects_delete_prd067";
DROP TRIGGER "trg_prompt_template_versions_delete_prd067";
DROP TRIGGER "trg_prompt_templates_delete_prd067";
DROP TRIGGER "trg_provider_adapter_activations_immutable_delete_prd063";
DROP TRIGGER "trg_provider_catalog_events_delete_prd119";
DROP TRIGGER "trg_provider_catalog_instances_delete_prd119";
DROP TRIGGER "trg_provider_error_facts_delete_prd056";
DROP TRIGGER "trg_provider_error_map_events_delete_prd056";
DROP TRIGGER "trg_provider_error_map_head_versions_delete_prd056";
DROP TRIGGER "trg_provider_error_map_heads_delete_prd056";
DROP TRIGGER "trg_provider_error_map_operations_delete_prd056";
DROP TRIGGER "trg_provider_error_map_versions_delete_prd056";
DROP TRIGGER "trg_provider_error_maps_delete_prd056";
DROP TRIGGER "trg_provider_key_anomaly_delete_prd081";
DROP TRIGGER "trg_provider_preflight_runs_delete_prd119";
DROP TRIGGER "trg_providers_experiment_arm_delete_prd075";
DROP TRIGGER "trg_quota_adjustments_delete_prd079";
DROP TRIGGER "trg_quota_allocations_delete_prd079";
DROP TRIGGER "trg_quota_buckets_delete_prd079";
DROP TRIGGER "trg_quota_events_delete_prd079";
DROP TRIGGER "trg_quota_outbox_delete_prd079";
DROP TRIGGER "trg_quota_reservation_items_delete_prd079";
DROP TRIGGER "trg_quota_reservations_delete_prd079";
DROP TRIGGER "trg_quota_settlements_delete_prd079";
DROP TRIGGER "trg_reconciliation_differences_delete_prd104";
DROP TRIGGER "trg_recovery_points_delete_prd111";
DROP TRIGGER "trg_refunds_delete_prd103";
DROP TRIGGER "trg_request_explanations_immutable_delete_prd054";
DROP TRIGGER "trg_request_feedback_event_immutable_delete_prd049";
DROP TRIGGER "trg_request_feedback_immutable_delete_prd049";
DROP TRIGGER "trg_request_log_feedback_parent_delete_prd049";
DROP TRIGGER "trg_request_log_query_events_immutable_delete_prd055";
DROP TRIGGER "trg_request_webhook_links_immutable_delete_prd054";
DROP TRIGGER "trg_residency_evidence_delete_prd117";
DROP TRIGGER "trg_residency_policies_delete_prd117";
DROP TRIGGER "trg_response_cache_entry_tags_delete_prd076";
DROP TRIGGER "trg_response_cache_event_outbox_delete_prd076";
DROP TRIGGER "trg_response_cache_events_delete_prd076";
DROP TRIGGER "trg_response_cache_fill_tags_delete_prd076";
DROP TRIGGER "trg_response_cache_hits_delete_prd076";
DROP TRIGGER "trg_response_cache_invalidations_delete_prd076";
DROP TRIGGER "trg_response_cache_key_generations_delete_prd076";
DROP TRIGGER "trg_response_cache_policies_delete_prd076";
DROP TRIGGER "trg_response_cache_policy_events_delete_prd076";
DROP TRIGGER "trg_response_cache_request_facts_delete_prd076";
DROP TRIGGER "trg_response_cache_tag_generations_delete_prd076";
DROP TRIGGER "trg_restore_evidence_delete_prd111";
DROP TRIGGER "trg_retention_exceptions_delete_prd134";
DROP TRIGGER "trg_retention_policies_delete_prd117";
DROP TRIGGER "trg_risk_acceptance_delete_prd137";
DROP TRIGGER "trg_risk_appeals_delete_prd110";
DROP TRIGGER "trg_risk_cases_delete_prd110";
DROP TRIGGER "trg_risk_challenges_delete_prd110";
DROP TRIGGER "trg_risk_decisions_delete_prd110";
DROP TRIGGER "trg_risk_event_outbox_delete_prd110";
DROP TRIGGER "trg_risk_restrictions_delete_prd110";
DROP TRIGGER "trg_risk_signals_delete_prd110";
DROP TRIGGER "trg_role_dashboard_configs_delete_prd065";
DROP TRIGGER "trg_role_permission_events_delete_prd059";
DROP TRIGGER "trg_role_permissions_delete_prd059";
DROP TRIGGER "trg_roles_delete_prd059";
DROP TRIGGER "trg_routing_attempt_feedback_parent_delete_prd049";
DROP TRIGGER "trg_routing_attempt_provider_error_fact_delete_prd056";
DROP TRIGGER "trg_routing_candidates_prd048_immutable_delete";
DROP TRIGGER "trg_routing_decisions_experiment_delete_prd075";
DROP TRIGGER "trg_routing_policies_prd048_delete";
DROP TRIGGER "trg_routing_policy_events_prd048_immutable_delete";
DROP TRIGGER "trg_saas_subscription_events_delete_prd098";
DROP TRIGGER "trg_saved_filters_immutable_delete_prd055";
DROP TRIGGER "trg_scim_events_delete_prd115";
DROP TRIGGER "trg_security_assessment_delete_prd137";
DROP TRIGGER "trg_security_event_aggregates_delete_prd081";
DROP TRIGGER "trg_security_event_members_delete_prd081";
DROP TRIGGER "trg_security_event_outbox_delete_prd081";
DROP TRIGGER "trg_security_event_sources_delete_prd081";
DROP TRIGGER "trg_security_remediation_delete_prd137";
DROP TRIGGER "trg_service_accounts_delete_prd118";
DROP TRIGGER "trg_session_risk_actions_delete_prd070";
DROP TRIGGER "trg_session_risk_events_delete_prd070";
DROP TRIGGER "trg_session_risk_freezes_delete_prd070";
DROP TRIGGER "trg_session_risk_observations_delete_prd070";
DROP TRIGGER "trg_session_risk_scores_delete_prd070";
DROP TRIGGER "trg_sso_certificates_delete_prd114";
DROP TRIGGER "trg_sso_connections_delete_prd114";
DROP TRIGGER "trg_sso_login_states_delete_prd114";
DROP TRIGGER "trg_sso_policies_delete_prd114";
DROP TRIGGER "trg_sso_replay_guards_delete_prd114";
DROP TRIGGER "trg_status_events_delete_prd112";
DROP TRIGGER "trg_status_subscriptions_delete_prd112";
DROP TRIGGER "trg_stream_session_provider_error_fact_delete_prd056";
DROP TRIGGER "trg_subscription_orders_delete_prd103";
DROP TRIGGER "trg_supply_chain_exception_delete_prd136";
DROP TRIGGER "trg_support_actions_delete_prd132";
DROP TRIGGER "trg_support_attachment_jobs_delete_prd132";
DROP TRIGGER "trg_support_attachments_delete_prd132";
DROP TRIGGER "trg_support_clock_events_delete_prd132";
DROP TRIGGER "trg_support_escalations_delete_prd132";
DROP TRIGGER "trg_support_events_delete_prd132";
DROP TRIGGER "trg_support_messages_delete_prd132";
DROP TRIGGER "trg_support_outbox_delete_prd132";
DROP TRIGGER "trg_support_sla_clocks_delete_prd132";
DROP TRIGGER "trg_support_surveys_delete_prd132";
DROP TRIGGER "trg_support_tickets_delete_prd132";
DROP TRIGGER "trg_telemetry_policies_delete_prd109";
DROP TRIGGER "trg_temporary_rate_limit_overrides_immutable_delete_prd053";
DROP TRIGGER "trg_tenant_data_states_delete_prd089";
DROP TRIGGER "trg_tenant_export_jobs_delete_prd089";
DROP TRIGGER "trg_tenant_lifecycle_events_delete_prd089";
DROP TRIGGER "trg_tenant_lifecycle_outbox_delete_prd089";
DROP TRIGGER "trg_tenant_retention_archives_delete_prd089";
DROP TRIGGER "trg_uploaded_files_delete_prd072";
DROP TRIGGER "trg_usage_export_events_immutable_delete";
DROP TRIGGER "trg_usage_export_ledger_sequences_immutable_delete";
DROP TRIGGER "trg_usage_ledger_export_append_only_delete";
DROP TRIGGER "trg_verification_evidence_delete_prd102";
DROP TRIGGER "trg_verified_domains_delete_prd114";
DROP TRIGGER "trg_virtual_keys_scope_delete_prd075";
DROP TRIGGER "trg_webhook_dead_letters_delete_prd057";
DROP TRIGGER "trg_webhook_security_rate_claim_delete_prd081";
DROP TRIGGER "trg_workspace_advanced_tool_delete_prd146";
DROP TRIGGER "trg_workspace_assistant_delete_prd145";
DROP TRIGGER "trg_workspace_billing_profile_delete_prd102";
DROP TRIGGER "trg_workspace_console_event_delete_prd145";
DROP TRIGGER "trg_workspace_creation_requests_delete_prd093";
DROP TRIGGER "trg_workspace_dashboard_source_delete_prd145";
DROP TRIGGER "trg_workspace_dashboard_version_delete_prd145";
DROP TRIGGER "trg_workspace_edge_commands_delete_prd120";
DROP TRIGGER "trg_workspace_edge_state_commands_delete_prd121";
DROP TRIGGER "trg_workspace_edge_telemetry_commands_delete_prd122";
DROP TRIGGER "trg_workspace_environment_origins_delete_prd093";
DROP TRIGGER "trg_workspace_events_delete_prd093";
DROP TRIGGER "trg_workspace_health_delete_prd096";
DROP TRIGGER "trg_workspace_membership_conflicts_delete_prd094";
DROP TRIGGER "trg_workspace_memberships_delete_prd094";
DROP TRIGGER "trg_workspace_navigation_contract_delete_prd146";
DROP TRIGGER "trg_workspace_navigation_event_delete_prd146";
DROP TRIGGER "trg_workspace_navigation_item_delete_prd146";
DROP TRIGGER "trg_workspace_navigation_projection_delete_prd146";
DROP TRIGGER "trg_workspace_navigation_projection_item_delete_prd146";
DROP TRIGGER "trg_workspace_notification_delete_prd096";
DROP TRIGGER "trg_workspace_operational_delete_prd096";
DROP TRIGGER "trg_workspace_operational_events_delete_prd096";
DROP TRIGGER "trg_workspace_projection_claims_delete_prd093";
DROP TRIGGER "trg_workspace_provider_catalog_commands_delete_prd119";
DROP TRIGGER "trg_workspace_provider_override_version_delete_prd143";
DROP TRIGGER "trg_workspace_regions_delete_prd093";
DROP TRIGGER "trg_workspace_risk_tags_delete_prd096";
DROP TRIGGER "trg_workspace_role_task_delete_prd146";
DROP TRIGGER "trg_workspace_role_task_version_delete_prd146";
DROP TRIGGER "trg_workspace_tenant_bindings_delete_prd093";
DROP TRIGGER "trg_workspace_tenant_creation_claims_delete_prd093";
DELETE FROM "workspace_role_task_home_sources" WHERE "role_task_home_version_id" IN (SELECT "role_task_home_version_id" FROM "workspace_role_task_home_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "workspace_provider_cost_rate_versions" WHERE "workspace_provider_cost_rate_id" IN (SELECT "workspace_provider_cost_rate_id" FROM "workspace_provider_cost_rates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "workspace_provider_cost_rate_versions" WHERE "created_by_membership_id" IN (SELECT "workspace_membership_id" FROM "workspace_memberships" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "workspace_provider_cost_rate_heads" WHERE "workspace_provider_cost_rate_id" IN (SELECT "workspace_provider_cost_rate_id" FROM "workspace_provider_cost_rates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "workspace_operational_event_outbox" WHERE "workspace_operational_event_id" IN (SELECT "workspace_operational_event_id" FROM "workspace_operational_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "workspace_navigation_event_outbox" WHERE "workspace_navigation_event_id" IN (SELECT "workspace_navigation_event_id" FROM "workspace_navigation_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "workspace_event_outbox" WHERE "workspace_event_id" IN (SELECT "workspace_event_id" FROM "workspace_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "workspace_console_event_outbox" WHERE "workspace_console_event_id" IN (SELECT "workspace_console_event_id" FROM "workspace_console_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "workspace_advanced_tool_entitlement_sources" WHERE "advanced_tool_entitlement_id" IN (SELECT "advanced_tool_entitlement_id" FROM "workspace_advanced_tool_entitlement_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "verification_reviews" WHERE "business_verification_intent_id" IN (SELECT "business_verification_intent_id" FROM "business_verification_intents" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "verification_evidence_download_tokens" WHERE "verification_evidence_id" IN (SELECT "verification_evidence_id" FROM "verification_evidence" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "verification_evidence_download_tokens" WHERE "business_verification_intent_id" IN (SELECT "business_verification_intent_id" FROM "business_verification_intents" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "support_grant_approvals" WHERE "membership_id" IN (SELECT "workspace_membership_id" FROM "workspace_memberships" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "support_grant_approvals" WHERE "grant_id" IN (SELECT "grant_id" FROM "support_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "service_account_scopes" WHERE "service_account_id" IN (SELECT "service_account_id" FROM "service_accounts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "risk_commands" WHERE "result_decision_id" IN (SELECT "risk_decision_id" FROM "risk_decisions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "risk_commands" WHERE "result_case_id" IN (SELECT "risk_case_id" FROM "risk_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "risk_challenge_commands" WHERE "risk_challenge_id" IN (SELECT "risk_challenge_id" FROM "risk_challenges" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "risk_challenge_attempts" WHERE "risk_challenge_id" IN (SELECT "risk_challenge_id" FROM "risk_challenges" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "risk_case_transitions" WHERE "risk_case_id" IN (SELECT "risk_case_id" FROM "risk_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "registration_intents" WHERE "invitation_id" IN (SELECT "invitation_id" FROM "invitations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "provider_catalog_event_outbox" WHERE "provider_catalog_event_id" IN (SELECT "provider_catalog_event_id" FROM "provider_catalog_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "post_ga_reviews" WHERE "ga_launch_id" IN (SELECT "ga_launch_id" FROM "ga_launches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_meter_outbox" WHERE "source_usage_ledger_id" IN (SELECT "usage_ledger_id" FROM "usage_ledger" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_invoices" WHERE "source_invoice_request_id" IN (SELECT "invoice_request_id" FROM "invoice_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_invoices" WHERE "source_billing_statement_id" IN (SELECT "billing_statement_id" FROM "billing_statements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_console_migration_security_reviews" WHERE "console_domain_migration_id" IN (SELECT "console_domain_migration_id" FROM "console_domain_migrations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_console_migration_event_outbox" WHERE "platform_console_migration_event_id" IN (SELECT "platform_console_migration_event_id" FROM "platform_console_migration_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_console_migration_commands" WHERE "console_domain_migration_id" IN (SELECT "console_domain_migration_id" FROM "console_domain_migrations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_console_migration_approvals" WHERE "console_domain_migration_id" IN (SELECT "console_domain_migration_id" FROM "console_domain_migrations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_channel_partner_scopes" WHERE "channel_partner_id" IN (SELECT "channel_partner_id" FROM "channel_partners" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_acquisition_sources" WHERE "channel_referral_code_id" IN (SELECT "channel_referral_code_id" FROM "channel_referral_codes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "platform_acquisition_sources" WHERE "channel_partner_id" IN (SELECT "channel_partner_id" FROM "channel_partners" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "payment_attempts" WHERE "payment_intent_id" IN (SELECT "payment_intent_id" FROM "payment_intents" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "onboarding_event_outbox" WHERE "onboarding_event_id" IN (SELECT "onboarding_event_id" FROM "onboarding_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "mfa_challenges" WHERE "workspace_session_id" IN (SELECT "workspace_session_id" FROM "workspace_sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "membership_event_outbox" WHERE "membership_event_id" IN (SELECT "membership_event_id" FROM "membership_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "invoice_reversal_requests" WHERE "invoice_request_id" IN (SELECT "invoice_request_id" FROM "invoice_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "invoice_provider_receipts" WHERE "invoice_request_id" IN (SELECT "invoice_request_id" FROM "invoice_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "invoice_provider_outbox" WHERE "invoice_request_id" IN (SELECT "invoice_request_id" FROM "invoice_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "identity_security_event_outbox" WHERE "identity_security_event_id" IN (SELECT "identity_security_event_id" FROM "identity_security_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "gateway_config_outbox" WHERE "gateway_cell_event_id" IN (SELECT "gateway_cell_event_id" FROM "gateway_cell_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "ga_rollbacks" WHERE "ga_launch_id" IN (SELECT "ga_launch_id" FROM "ga_launches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "ga_evidence" WHERE "source_evidence_id" IN (SELECT "launch_evidence_id" FROM "launch_evidence" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "ga_events" WHERE "ga_launch_id" IN (SELECT "ga_launch_id" FROM "ga_launches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "ga_decisions" WHERE "ga_launch_id" IN (SELECT "ga_launch_id" FROM "ga_launches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "edge_telemetry_event_outbox" WHERE "edge_telemetry_event_id" IN (SELECT "edge_telemetry_event_id" FROM "edge_telemetry_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "edge_identity_event_outbox" WHERE "edge_identity_event_id" IN (SELECT "edge_identity_event_id" FROM "edge_identity_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "edge_fleet_telemetry_projections" WHERE "edge_deployment_id" IN (SELECT "edge_deployment_id" FROM "edge_deployments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "edge_desired_state_event_outbox" WHERE "edge_desired_state_event_id" IN (SELECT "edge_desired_state_event_id" FROM "edge_desired_state_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "dunning_attempts" WHERE "dunning_case_id" IN (SELECT "dunning_case_id" FROM "dunning_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "dr_run_recovery_bindings" WHERE "recovery_point_id" IN (SELECT "recovery_point_id" FROM "recovery_points" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "deployment_brand_acks" WHERE "deployment_brand_bundle_id" IN (SELECT "deployment_brand_bundle_id" FROM "deployment_brand_bundles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "delivery_attempts" WHERE "notification_outbox_id" IN (SELECT "notification_outbox_id" FROM "notification_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "delivery_attempts" WHERE "notification_id" IN (SELECT "notification_id" FROM "notifications" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "break_glass_reviews" WHERE "membership_id" IN (SELECT "workspace_membership_id" FROM "workspace_memberships" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "break_glass_reviews" WHERE "break_glass_id" IN (SELECT "break_glass_id" FROM "break_glass_sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "brand_verified_domains" WHERE "deployment_brand_id" IN (SELECT "deployment_brand_id" FROM "deployment_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "brand_verified_domains" WHERE "channel_brand_id" IN (SELECT "channel_brand_id" FROM "channel_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "brand_releases" WHERE "deployment_brand_id" IN (SELECT "deployment_brand_id" FROM "deployment_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "brand_releases" WHERE "channel_brand_id" IN (SELECT "channel_brand_id" FROM "channel_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "brand_public_heads" WHERE "channel_brand_id" IN (SELECT "channel_brand_id" FROM "channel_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "brand_assets" WHERE "deployment_brand_id" IN (SELECT "deployment_brand_id" FROM "deployment_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "brand_assets" WHERE "channel_brand_id" IN (SELECT "channel_brand_id" FROM "channel_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "billing_dispute_case_transitions" WHERE "billing_dispute_case_id" IN (SELECT "billing_dispute_case_id" FROM "billing_dispute_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "billing_dispute_case_items" WHERE "billing_dispute_case_id" IN (SELECT "billing_dispute_case_id" FROM "billing_dispute_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "billing_dispute_case_decisions" WHERE "billing_dispute_case_id" IN (SELECT "billing_dispute_case_id" FROM "billing_dispute_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "backup_restore_verifications" WHERE "backup_id" IN (SELECT "backup_id" FROM "backup_sets" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "attribution_change_approvals" WHERE "attribution_change_id" IN (SELECT "attribution_change_id" FROM "attribution_changes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces));
DELETE FROM "access_token_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "account_federation_links" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "activation_checkpoints" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "adapter_contract_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "adapter_contract_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "adapter_contract_runs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "application_memberships" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "applications" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_action_replays" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_decisions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_scheduler_tenants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_step_up_challenges" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_step_up_guards" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_tasks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "approval_writebacks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "assistant_audits" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "assistant_queries" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "assurance_cleanup_journal" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "assurance_receipt_archives" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "assurance_runner_leases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "attribution_changes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "audit_export_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "audit_exports" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "audit_log_export_sequences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "audit_logs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "backup_sets" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_attempts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_budget_allocations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_budget_holds" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_execution_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_job_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_job_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_reconciliation_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_scheduler_tenants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "batch_tasks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_dispute_adjustment_links" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_dispute_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_dispute_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_dispute_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_dispute_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_dispute_scheduler_tenants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_dispute_transitions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_disputes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_domain_reconciliation_differences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_domain_reconciliation_source_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_settlement_holds" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "billing_statements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "brand_legacy_object_mappings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "branding_configs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "branding_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "branding_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "break_glass_sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "budget_alert_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "budget_block_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "budget_blocks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "budget_evaluations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "budget_reservations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "budget_rules" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "budget_scope_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "business_verification_intents" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cache_affinities" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cache_affinity_bindings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cache_affinity_config_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cache_affinity_scopes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cache_affinity_selections" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cache_watermarks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_action_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_commission_rule_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_legacy_object_mappings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_memberships" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_partners" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_referral_codes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_registered_leads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_statement_lines" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "channel_statements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "chat_execution_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "chat_messages" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "chat_prompt_template_invocations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "chat_sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "client_credentials" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "commercial_effects" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "commission_records" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_action_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_collector_replay_guards" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_evidence" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_exceptions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_remediation_tasks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_report_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "compliance_reports" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "conditional_access_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "conditional_access_simulations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_acks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_deltas" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_exports" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_import_rows" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_imports" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_rebuild_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_snapshot_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "config_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "console_backfill_attempts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "console_backfill_checkpoints" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "console_backfill_cursors" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "console_domain_migration_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "console_domain_migrations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "console_page_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "contract_profiles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cost_anomalies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cost_anomaly_actions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "cost_anomaly_detection_runs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "customer_health_scheduler_tenants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "customer_health_score_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "customer_health_scores" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "data_classifications" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "data_governance_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "data_governance_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "data_governance_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "data_governance_fences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "data_subject_request_owner_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "data_subject_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "debug_console_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "deletion_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "deletion_schedules" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "deployment_brand_bundles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "deployment_brands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "developer_portal_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "dlp_matches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "dlp_rules" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "dunning_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_acks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_apply_attempts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_backup_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_backup_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_backup_reports" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_backup_statuses" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_capacity_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_delivery_acceptances" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_delivery_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_delivery_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_deployment_release_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_deployments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_desired_state_approvals" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_desired_state_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_desired_state_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_desired_states" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_device_certificates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_device_identities" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_action_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_results" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_failure_summaries" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_fleet_deployment_index" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_fleet_upgrade_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_fleet_upgrade_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_health_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_health_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_identity_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_install_audits" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_install_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_install_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_installations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_acknowledgements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_audits" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_lease_reports" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_refresh_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_licenses" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_lkg_observations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_machine_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_maintenance_windows" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundle_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundle_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundle_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundle_transfers" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_conflict_resolutions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_conflicts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_sync_cursors" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_preflight_reports" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_registration_intents" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_restore_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_restore_verifications" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_rollback_attempts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_rollout_targets" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_references" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_sync_batches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_sync_receipts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_signed_entitlements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_signed_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_snapshot_artifacts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_support_bundle_download_audits" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_support_bundles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_batches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_policy_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_receipts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_upgrade_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_upgrade_receipts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_usage_reconciliations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_usage_sync_cursors" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_validation_checks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_validation_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "edge_validation_runs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "enterprise_identity_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "enterprise_identity_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "enterprise_identity_session_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "entitlement_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "environment_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "environments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "erasure_decisions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_assignments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_feedback_attributions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_request_executions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_request_outcomes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_results" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_scheduler_tenants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "experiment_usage_settlements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "fallback_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "feature_exposures" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "file_dlp_matches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "file_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "file_extracts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "file_security_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "file_security_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "file_security_findings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "file_security_scans" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "finops_adjustments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "finops_ledger_disputes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "finops_ledger_entries" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "flag_evaluation_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "ga_launches" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "gateway_capacity_leases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "gateway_cell_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "gateway_request_claims" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "gateway_request_scope_claims" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "identity_exports" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "identity_security_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "incident_workspace_scopes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "intent_risk_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "intent_risk_results" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "internal_finops_ledger_owner_bindings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "invitation_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "invitation_identity_bindings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "invitations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "invoice_artifacts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "invoice_download_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "invoice_requests" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "isolation_test_runs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "launch_action_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "launch_candidate_artifacts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "launch_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "launch_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "launch_evidence" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "launch_rollbacks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "launch_waves" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "legal_entity_profiles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "legal_holds" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "local_endpoint_create_receipts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "local_model_call_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "local_model_call_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "local_model_catalog_entries" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "local_model_endpoints" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "local_workspace_creation_admissions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "machine_identity_audits" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "machine_identity_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "machine_identity_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "maintenance_windows" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_invocation_attempts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_invocation_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_invocation_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_invocation_network_bindings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_invocations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_network_call_intents" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_network_circuits" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_network_decisions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_network_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_network_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_network_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_network_policy_revisions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_scheduler_tenants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_server_credentials" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_server_operations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_server_sync_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_servers" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_tool_role_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_tool_user_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "mcp_tools" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "membership_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "membership_invitations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_assessment_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_assessments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_cutover_checkpoints" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_cutover_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_differences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_plans" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_rollback_plans" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_runs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "migration_support_access_audits" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_alias_fallback_candidates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_alias_routing_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_aliases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_experiments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_feedback_statistics" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_price_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_price_plans" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_quality_score_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "model_quality_scores" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "models" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "notification_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "notification_preferences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "notifications" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "oauth_clients" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "onboarding_acknowledgements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "onboarding_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "onboarding_progress" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "onboarding_step_facts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "output_safety_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "output_safety_scans" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "owner_transfers" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "partner_commission_cursors" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "partner_customer_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "partner_customers" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "partner_portal_operations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "partners" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "patch_releases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "payment_intents" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "pen_test_findings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "permissions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "plans" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_action_intents" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_billing_adjustments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_billing_cycles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_billing_summaries" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_commercial_adjustments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_console_migration_batch_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_console_migration_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_conversion_attributions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_account_projections" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_contacts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_health_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_health_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_legacy_mappings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_plans" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_scheduler_state" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_tasks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_growth_event_eligibility" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_growth_experiment_assignments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_growth_export_subjects" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_growth_legacy_mappings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_growth_legacy_reconciliations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_invoice_workspace_bindings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_meter_readings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_meter_recovery_actions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_qbr_summaries" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_quota_allocations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "platform_renewal_opportunities" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_actions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_explanations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_rules" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_snapshot_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_snapshot_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_snapshot_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_template_application_dependencies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_template_application_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_template_applications" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_template_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "policy_templates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "post_launch_reviews" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "postmortems" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "price_snapshot_provider_cost_rate_bindings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "price_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "privacy_action_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "privacy_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "privacy_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "privacy_preferences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "private_dr_drills" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "product_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "product_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "project_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "projects" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "prompt_template_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "prompt_template_operations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "prompt_template_version_models" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "prompt_template_version_projects" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "prompt_template_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "prompt_templates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_adapter_catalog_activations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_catalog_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_catalog_instances" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_compatibility_projections" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_credentials" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_error_facts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_error_map_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_error_map_head_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_error_map_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_error_map_operations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_error_map_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_error_maps" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_health_probe_logs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_key_anomaly_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "provider_preflight_runs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "providers" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "quota_adjustments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "quota_buckets" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "quota_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "quota_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "quota_reservation_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "quota_reservations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "quota_usage_allocations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "quota_usage_settlements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "reconciliation_differences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "recovery_points" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "refunds" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "request_explanations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "request_feedback_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "request_feedbacks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "request_log_query_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "request_logs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "request_webhook_delivery_links" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "residency_evidence" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "residency_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_entries" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_entry_tags" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_fill_leases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_fill_tags" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_hits" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_invalidations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_key_generations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_policy_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_request_facts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "response_cache_tag_generations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "restore_evidence" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "retention_exceptions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "retention_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "risk_appeals" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "risk_cases" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "risk_challenges" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "risk_decisions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "risk_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "risk_restrictions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "risk_signals" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "role_dashboard_configs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "role_permission_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "role_permissions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "roles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "routing_attempts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "routing_candidates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "routing_decisions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "routing_policy_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "saas_subscription_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "saved_filters" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_conflicts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_group_links" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_group_members" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_pending_invitations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_role_mappings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_sync_runs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_tokens" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "scim_user_links" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_assessments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_assurance_action_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_assurance_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_assurance_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_event_aggregate_members" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_event_aggregates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_event_sources" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_remediations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "security_risk_acceptances" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "seed_runs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "service_accounts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "session_risk_actions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "session_risk_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "session_risk_freezes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "session_risk_observations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "session_risk_scores" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "sso_certificates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "sso_connections" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "sso_login_states" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "sso_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "sso_replay_guards" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "status_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "status_subscriptions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "step_up_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "stream_sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "subscription_order_records" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "subscription_orders" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "subscription_trials" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "supply_chain_security_exceptions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_action_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_attachment_scan_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_escalations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_grants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_satisfaction_surveys" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_sla_clock_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_sla_clocks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_ticket_attachments" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_ticket_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_ticket_messages" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "support_tickets" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "telemetry_policies" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "temporary_rate_limit_overrides" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "tenant_data_states" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "tenant_export_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "tenant_lifecycle_event_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "tenant_lifecycle_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "tenant_retention_archives" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "tenants" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "uploaded_files" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "usage_export_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "usage_export_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "usage_export_ledger_sequences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "usage_ledger" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "user_preference_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "user_preferences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "users" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "verification_evidence" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "verified_domains" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "virtual_key_binding_history" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "virtual_key_consumers" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "virtual_key_rate_limit_rules" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "virtual_keys" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "webhook_dead_letter_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "webhook_deliveries" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "webhook_delivery_attempts" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "webhook_endpoints" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "webhook_security_rate_claims" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_advanced_tool_entitlement_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_advanced_tool_entitlement_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_assistant_query_projections" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_attributions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_billing_profiles" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_console_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_consumers" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_dashboard_projection_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_dashboard_projection_sources" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_dashboard_projection_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_deployment_diagnostic_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_deployment_diagnostic_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_approval_queue_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_approval_rejections" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_approval_source_cursors" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_reconciliation_checks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_reconciliations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_edge_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_edge_state_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_edge_telemetry_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_environment_origins" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_fx_rate_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_gateway_placements" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_health_projections" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_internal_finops_disputes" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_legacy_route_tombstones" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_memberships" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_contract_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_contract_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_contract_snapshots" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_member_states" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_preference_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_preference_source_mappings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_preferences" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_projection_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_projection_items" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_projection_sources" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_projection_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_reconciliations" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_operational_events" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_operational_states" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_owner_notification_outbox" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_private_gateway_capabilities" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_catalog_commands" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_cost_rates" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_error_override_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_error_override_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_error_overrides" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provisioning_jobs" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_quota_owner_bindings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_regions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_risk_tags" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_role_task_home_heads" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_role_task_home_tasks" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_role_task_home_versions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_sessions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_subscription_projections" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_subscriptions" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_tenant_bindings" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_tenant_creation_claims" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "workspace_user_projection_claims" WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM "access_token_grants" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "account_federation_links" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "activation_checkpoints" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "assurance_cleanup_journal" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "assurance_receipt_archives" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "assurance_runner_leases" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "backup_sets" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "billing_dispute_cases" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "billing_domain_reconciliation_differences" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "billing_domain_reconciliation_source_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "billing_statements" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "break_glass_sessions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "business_verification_intents" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "cache_watermarks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "client_credentials" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "commercial_effects" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_action_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_collector_replay_guards" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_evidence" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_exceptions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_remediation_tasks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_report_items" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "compliance_reports" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "conditional_access_policies" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "conditional_access_simulations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "config_acks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "config_deltas" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "config_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "config_rebuild_jobs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "config_snapshot_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "config_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "console_backfill_attempts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "console_backfill_checkpoints" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "console_backfill_cursors" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "console_domain_migration_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "console_domain_migrations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "contract_profiles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "data_classifications" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "data_governance_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "data_governance_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "data_governance_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "data_governance_fences" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "data_subject_request_owner_jobs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "data_subject_requests" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "deletion_schedules" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "deployment_brand_bundles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "deployment_brands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "dunning_cases" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_acks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_apply_attempts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_backup_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_backup_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_backup_reports" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_backup_statuses" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_capacity_grants" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_delivery_acceptances" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_delivery_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_delivery_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_deployment_release_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_deployments" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_desired_state_approvals" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_desired_state_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_desired_state_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_desired_states" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_device_certificates" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_device_identities" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_action_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_grants" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_diagnostic_results" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_failure_summaries" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_fleet_deployment_index" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_fleet_upgrade_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_fleet_upgrade_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_health_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_health_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_identity_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_install_audits" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_install_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_install_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_installations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_acknowledgements" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_audits" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_lease_reports" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_license_refresh_requests" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_licenses" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_lkg_observations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_machine_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_maintenance_windows" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundle_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundle_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundle_requests" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundle_transfers" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_bundles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_conflict_resolutions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_conflicts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_offline_sync_cursors" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_preflight_reports" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_registration_intents" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_restore_jobs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_restore_verifications" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_rollback_attempts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_rollout_targets" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_references" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_sync_batches" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_secret_sync_receipts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_sessions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_signed_entitlements" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_signed_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_snapshot_artifacts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_support_bundle_download_audits" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_support_bundles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_batches" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_policies" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_policy_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_telemetry_receipts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_upgrade_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_upgrade_receipts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_usage_reconciliations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_usage_sync_cursors" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_validation_checks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_validation_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "edge_validation_runs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "enterprise_identity_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "enterprise_identity_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "enterprise_identity_session_grants" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "entitlement_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "erasure_decisions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "feature_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "feature_exposures" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "flag_evaluation_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "ga_launches" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "gateway_capacity_leases" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "gateway_cell_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "identity_exports" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "identity_security_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "incident_workspace_scopes" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "internal_finops_ledger_owner_bindings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "invoice_artifacts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "invoice_download_grants" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "invoice_requests" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "isolation_test_runs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "launch_action_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "launch_candidate_artifacts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "launch_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "launch_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "launch_evidence" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "launch_rollbacks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "launch_waves" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "legal_entity_profiles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "legal_holds" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "local_workspace_creation_admissions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "machine_identity_audits" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "machine_identity_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "machine_identity_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "maintenance_windows" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "membership_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "membership_invitations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_assessment_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_assessments" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_cutover_checkpoints" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_cutover_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_differences" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_plans" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_rollback_plans" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_runs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "migration_support_access_audits" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "notification_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "notification_preferences" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "notifications" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "oauth_clients" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "onboarding_acknowledgements" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "onboarding_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "onboarding_progress" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "onboarding_step_facts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "organization_profiles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "owner_transfers" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "patch_releases" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "payment_intents" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "pen_test_findings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "personal_profiles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_action_intents" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_billing_adjustments" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_billing_cycles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_billing_summaries" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_commercial_adjustments" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_console_migration_batch_items" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_console_migration_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_account_projections" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_contacts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_health_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_health_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_legacy_mappings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_plans" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_success_scheduler_state" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_customer_tasks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_invoice_workspace_bindings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_meter_readings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_meter_recovery_actions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_qbr_summaries" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_quota_allocations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "platform_renewal_opportunities" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "post_launch_reviews" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "postmortems" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "price_snapshot_provider_cost_rate_bindings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "privacy_action_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "privacy_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "privacy_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "privacy_preferences" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "private_dr_drills" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "provider_catalog_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "provider_catalog_instances" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "provider_compatibility_projections" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "provider_preflight_runs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "reconciliation_differences" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "recovery_points" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "refunds" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "residency_evidence" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "residency_policies" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "restore_evidence" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "retention_exceptions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "retention_policies" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "risk_appeals" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "risk_cases" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "risk_challenges" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "risk_decisions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "risk_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "risk_restrictions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "risk_signals" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "saas_subscription_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_conflicts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_group_links" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_group_members" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_pending_invitations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_role_mappings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_sync_runs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_tokens" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "scim_user_links" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "security_assessments" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "security_assurance_action_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "security_assurance_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "security_assurance_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "security_remediations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "security_risk_acceptances" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "service_accounts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "sso_certificates" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "sso_connections" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "sso_login_states" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "sso_policies" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "sso_replay_guards" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "status_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "status_subscriptions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "step_up_grants" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "subscription_order_records" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "subscription_orders" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "subscription_trials" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "supply_chain_security_exceptions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_action_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_attachment_scan_jobs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_escalations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_event_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_grants" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_satisfaction_surveys" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_sessions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_sla_clock_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_sla_clocks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_ticket_attachments" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_ticket_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_ticket_messages" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "support_tickets" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "telemetry_policies" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "verification_evidence" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "verified_domains" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_advanced_tool_entitlement_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_advanced_tool_entitlement_versions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_assistant_query_projections" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_billing_profiles" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_console_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_creation_requests" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_dashboard_projection_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_dashboard_projection_sources" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_dashboard_projection_versions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_deployment_diagnostic_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_deployment_diagnostic_versions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_approval_queue_items" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_approval_rejections" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_approval_source_cursors" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_reconciliation_checks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_domain_reconciliations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_edge_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_edge_state_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_edge_telemetry_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_environment_origins" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_fx_rate_versions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_gateway_placements" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_health_projections" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_internal_finops_disputes" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_legacy_route_tombstones" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_membership_migration_conflicts" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_memberships" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_contract_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_contract_items" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_contract_snapshots" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_member_states" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_preference_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_preference_source_mappings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_preferences" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_projection_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_projection_items" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_projection_sources" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_projection_versions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_navigation_reconciliations" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_operational_events" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_operational_states" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_owner_notification_outbox" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_private_gateway_capabilities" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_catalog_commands" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_cost_rates" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_error_override_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_error_override_versions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provider_error_overrides" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_provisioning_jobs" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_quota_owner_bindings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_regions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_risk_tags" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_role_task_home_heads" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_role_task_home_tasks" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_role_task_home_versions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_sessions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_subscription_projections" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_subscriptions" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_tenant_bindings" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_tenant_creation_claims" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspace_user_projection_claims" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM "workspaces" WHERE workspace_id IN (SELECT workspace_id FROM retired_personal_workspaces);
DELETE FROM account_identity_links WHERE legacy_tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM account_preference_source_mappings WHERE legacy_tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM users WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM environments WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DELETE FROM tenants WHERE tenant_id IN (SELECT tenant_id FROM retired_personal_workspaces);
DROP TABLE personal_profiles;
CREATE TABLE workspaces_organization_only(
  workspace_id TEXT PRIMARY KEY CHECK(length(workspace_id)=36
    AND substr(workspace_id,1,4)='wsp_'
    AND substr(workspace_id,5) NOT GLOB '*[^0-9a-f]*'),
  kind TEXT NOT NULL CHECK(kind ='organization'),
  status TEXT NOT NULL CHECK(status IN('active','migration_conflict')),
  profile_state TEXT NOT NULL CHECK(profile_state IN('draft','confirmed','migration_conflict')),
  creation_source TEXT NOT NULL CHECK(creation_source IN(
    'user_created','legacy_bootstrap','legacy_backfill')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  created_by_account_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  CHECK(creation_source!='user_created' OR created_by_account_id IS NOT NULL)
);
INSERT INTO workspaces_organization_only SELECT * FROM workspaces;
DROP TABLE workspaces;
ALTER TABLE workspaces_organization_only RENAME TO workspaces;
CREATE INDEX idx_workspaces_creator_prd093
  ON workspaces(created_by_account_id,status,created_at,workspace_id);
CREATE TRIGGER trg_workspaces_owner_update_prd093
BEFORE UPDATE ON workspaces FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.kind!=OLD.kind
  OR NEW.creation_source!=OLD.creation_source
  OR (NEW.created_by_account_id IS NOT OLD.created_by_account_id AND NOT (
    OLD.creation_source IN('legacy_bootstrap','legacy_backfill')
    AND OLD.status='migration_conflict' AND NEW.status='active'
    AND OLD.created_by_account_id IS NULL AND NEW.created_by_account_id IS NOT NULL
  ))
  OR NEW.created_at!=OLD.created_at
BEGIN
  SELECT CASE WHEN
    NEW.workspace_id=OLD.workspace_id AND NEW.kind=OLD.kind
    AND NEW.creation_source=OLD.creation_source AND NEW.created_at=OLD.created_at
    AND OLD.creation_source IN('legacy_bootstrap','legacy_backfill')
    AND OLD.status='active' AND NEW.status='active'
    AND OLD.profile_state='confirmed' AND NEW.profile_state='confirmed'
    AND OLD.created_by_account_id IS NOT NULL
    AND NEW.created_by_account_id IS NOT OLD.created_by_account_id
    AND EXISTS(SELECT 1 FROM workspace_memberships owner_member
      WHERE owner_member.workspace_id=OLD.workspace_id
        AND owner_member.account_id=OLD.created_by_account_id
        AND owner_member.role_code='owner' AND owner_member.status='active')
    AND EXISTS(SELECT 1 FROM workspace_memberships projected_member
      WHERE projected_member.workspace_id=OLD.workspace_id
        AND projected_member.account_id=NEW.created_by_account_id
        AND projected_member.role_code!='owner' AND projected_member.status='active')
    THEN RAISE(IGNORE) ELSE RAISE(ABORT,'workspace_owner_immutable') END;
END;
CREATE TABLE onboarding_progress_organization_only(
  onboarding_progress_id TEXT PRIMARY KEY CHECK(length(onboarding_progress_id)=36
    AND substr(onboarding_progress_id,1,4)='onp_'
    AND substr(onboarding_progress_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  workspace_kind TEXT NOT NULL CHECK(workspace_kind ='organization'),
  initialization_source TEXT NOT NULL CHECK(initialization_source IN(
    'migration_backfill','workspace_created')),
  generation INTEGER NOT NULL CHECK(generation>=0),
  completed_steps INTEGER NOT NULL CHECK(completed_steps BETWEEN 0 AND 8),
  status TEXT NOT NULL CHECK(status IN('incomplete','active')),
  projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64
    AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  evaluated_at TEXT NOT NULL CHECK(unixepoch(evaluated_at) IS NOT NULL),
  activated_at TEXT CHECK(activated_at IS NULL OR unixepoch(activated_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,tenant_id),
  CHECK((status='active' AND completed_steps=8 AND activated_at IS NOT NULL)
    OR(status='incomplete' AND completed_steps<8))
);
INSERT INTO onboarding_progress_organization_only SELECT * FROM onboarding_progress;
DROP TABLE onboarding_progress;
ALTER TABLE onboarding_progress_organization_only RENAME TO onboarding_progress;
CREATE INDEX idx_onboarding_progress_status_prd097
  ON onboarding_progress(status,updated_at DESC,workspace_id);
CREATE TRIGGER trg_onboarding_progress_transition_prd097
BEFORE UPDATE ON onboarding_progress FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.workspace_kind!=OLD.workspace_kind
  OR NEW.initialization_source!=OLD.initialization_source
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_progress_transition_invalid'); END;
CREATE TABLE platform_customer_account_projections_organization_only(
  customer_account_id TEXT PRIMARY KEY CHECK(length(customer_account_id)=36
    AND substr(customer_account_id,1,4)='csa_'
    AND substr(customer_account_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  workspace_revision INTEGER NOT NULL CHECK(workspace_revision>=1),
  display_name TEXT NOT NULL CHECK(length(CAST(display_name AS BLOB)) BETWEEN 1 AND 160),
  slug TEXT NOT NULL CHECK(length(slug) BETWEEN 3 AND 48),
  workspace_kind TEXT NOT NULL CHECK(workspace_kind ='organization'),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 40),
  operational_status TEXT NOT NULL CHECK(operational_status IN(
    'active','suspended','deletion_pending','deleted','migration_conflict')),
  activation_phase TEXT NOT NULL CHECK(activation_phase IN(
    'not_started','in_progress','activated')),
  activation_completed_steps INTEGER NOT NULL CHECK(activation_completed_steps BETWEEN 0 AND 8),
  adoption_band TEXT NOT NULL CHECK(adoption_band IN('unknown','low','medium','high')),
  subscription_id TEXT,
  plan_version_id TEXT,
  subscription_status TEXT NOT NULL CHECK(subscription_status IN(
    'not_configured','pending','active','trialing','past_due','canceled','expired')),
  renewal_at TEXT CHECK(renewal_at IS NULL OR unixepoch(renewal_at) IS NOT NULL),
  latest_health_snapshot_id TEXT,
  latest_health_sequence INTEGER CHECK(latest_health_sequence IS NULL OR latest_health_sequence>=1),
  health_risk TEXT NOT NULL CHECK(health_risk IN(
    'unknown','healthy','watch','at_risk','critical')),
  open_support_ticket_count INTEGER NOT NULL CHECK(open_support_ticket_count>=0),
  breached_sla_count INTEGER NOT NULL CHECK(breached_sla_count>=0),
  source_revision_sha256 TEXT NOT NULL CHECK(length(source_revision_sha256)=64
    AND source_revision_sha256 NOT GLOB '*[^0-9a-f]*'),
  projection_version TEXT NOT NULL CHECK(projection_version='customer-account-v1'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  projected_at TEXT NOT NULL CHECK(unixepoch(projected_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(subscription_id) REFERENCES workspace_subscriptions(subscription_id),
  UNIQUE(customer_account_id,workspace_id,tenant_id),
  CHECK((subscription_status='not_configured' AND subscription_id IS NULL
      AND plan_version_id IS NULL AND renewal_at IS NULL)
    OR(subscription_status!='not_configured' AND subscription_id IS NOT NULL
      AND plan_version_id IS NOT NULL)),
  CHECK((latest_health_snapshot_id IS NULL AND latest_health_sequence IS NULL
      AND health_risk='unknown')
    OR(latest_health_snapshot_id IS NOT NULL AND latest_health_sequence IS NOT NULL
      AND health_risk!='unknown'))
);
INSERT INTO platform_customer_account_projections_organization_only SELECT * FROM platform_customer_account_projections;
DROP TABLE platform_customer_account_projections;
ALTER TABLE platform_customer_account_projections_organization_only RENAME TO platform_customer_account_projections;
CREATE INDEX idx_platform_customer_directory_prd140
  ON platform_customer_account_projections(updated_at DESC,customer_account_id);
CREATE INDEX idx_platform_customer_filter_prd140
  ON platform_customer_account_projections(
    operational_status,region_code,activation_phase,adoption_band,health_risk,
    subscription_status,updated_at DESC,customer_account_id);
CREATE TRIGGER trg_platform_customer_account_owner_update_prd140
BEFORE UPDATE ON platform_customer_account_projections FOR EACH ROW
WHEN NEW.customer_account_id!=OLD.customer_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.projection_version!=OLD.projection_version
  OR NEW.revision!=OLD.revision+1 OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
BEGIN SELECT RAISE(ABORT,'customer_account_projection_transition_invalid'); END;
CREATE TRIGGER trg_access_token_grants_delete_prd118
BEFORE DELETE ON access_token_grants
BEGIN SELECT RAISE(ABORT,'access_token_grant_immutable'); END;
CREATE TRIGGER trg_account_identity_links_delete_prd091
BEFORE DELETE ON account_identity_links FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_link_immutable'); END;
CREATE TRIGGER trg_activation_checkpoints_delete_prd097
BEFORE DELETE ON activation_checkpoints FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.activation_checkpoint_immutable'); END;
CREATE TRIGGER trg_adapter_contract_cases_immutable_delete_prd064
BEFORE DELETE ON adapter_contract_cases FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'adapter_contract_case_immutable'); END;
CREATE TRIGGER trg_adapter_contract_event_immutable_delete_prd064
BEFORE DELETE ON adapter_contract_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'adapter_contract_event_immutable'); END;
CREATE TRIGGER trg_adapter_contract_runs_immutable_delete_prd064
BEFORE DELETE ON adapter_contract_runs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'adapter_contract_run_immutable'); END;
CREATE TRIGGER trg_aliases_experiment_scope_delete_prd075
BEFORE DELETE ON model_aliases FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
  WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
    AND experiment.model_alias_id=OLD.model_alias_id)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;
CREATE TRIGGER trg_approval_challenges_delete_prd078
BEFORE DELETE ON approval_step_up_challenges FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_challenge_immutable'); END;
CREATE TRIGGER trg_approval_decisions_delete_prd078
BEFORE DELETE ON approval_decisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_decision_immutable'); END;
CREATE TRIGGER trg_approval_events_delete_prd078
BEFORE DELETE ON approval_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_event_immutable'); END;
CREATE TRIGGER trg_approval_guards_delete_prd078
BEFORE DELETE ON approval_step_up_guards FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_step_up_guard_immutable'); END;
CREATE TRIGGER trg_approval_outbox_delete_prd078
BEFORE DELETE ON approval_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_outbox_immutable'); END;
CREATE TRIGGER trg_approval_replays_delete_prd078
BEFORE DELETE ON approval_action_replays FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_replay_immutable'); END;
CREATE TRIGGER trg_approval_scheduler_delete_prd078
BEFORE DELETE ON approval_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_scheduler_immutable'); END;
CREATE TRIGGER trg_approval_tasks_delete_prd078
BEFORE DELETE ON approval_tasks FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_task_immutable'); END;
CREATE TRIGGER trg_approval_writebacks_delete_prd078
BEFORE DELETE ON approval_writebacks FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'approval_writeback_immutable'); END;
CREATE TRIGGER trg_assistant_audits_delete_prd077
BEFORE DELETE ON assistant_audits FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'assistant_audit_immutable'); END;
CREATE TRIGGER trg_assistant_queries_delete_prd077
BEFORE DELETE ON assistant_queries FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'assistant_query_immutable'); END;
CREATE TRIGGER trg_assurance_archive_delete_prd137 BEFORE DELETE ON assurance_receipt_archives
BEGIN SELECT RAISE(ABORT,'security_assurance_archive_immutable'); END;
CREATE TRIGGER trg_assurance_cleanup_delete_prd137 BEFORE DELETE ON assurance_cleanup_journal
BEGIN SELECT RAISE(ABORT,'security_assurance_cleanup_immutable'); END;
CREATE TRIGGER trg_assurance_command_delete_prd137 BEFORE DELETE ON security_assurance_action_commands
BEGIN SELECT RAISE(ABORT,'security_assurance_command_immutable'); END;
CREATE TRIGGER trg_assurance_event_delete_prd137 BEFORE DELETE ON security_assurance_events
BEGIN SELECT RAISE(ABORT,'security_assurance_event_immutable'); END;
CREATE TRIGGER trg_assurance_outbox_delete_prd137 BEFORE DELETE ON security_assurance_event_outbox
BEGIN SELECT RAISE(ABORT,'security_assurance_outbox_immutable'); END;
CREATE TRIGGER trg_audit_export_events_delete_prd058
BEFORE DELETE ON audit_export_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_export_event_immutable'); END;
CREATE TRIGGER trg_audit_exports_delete_prd058
BEFORE DELETE ON audit_exports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_export_immutable'); END;
CREATE TRIGGER trg_audit_log_export_sequences_delete_prd058
BEFORE DELETE ON audit_log_export_sequences FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_backup_sets_delete_prd100
BEFORE DELETE ON backup_sets
BEGIN SELECT RAISE(ABORT,'backup_set_immutable'); END;
CREATE TRIGGER trg_batch_allocations_delete_prd074 BEFORE DELETE ON batch_budget_allocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_budget_immutable'); END;
CREATE TRIGGER trg_batch_attempts_delete_prd074 BEFORE DELETE ON batch_attempts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_attempt_immutable'); END;
CREATE TRIGGER trg_batch_events_delete_prd074 BEFORE DELETE ON batch_job_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_event_immutable'); END;
CREATE TRIGGER trg_batch_grants_delete_prd074 BEFORE DELETE ON batch_execution_grants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_grant_immutable'); END;
CREATE TRIGGER trg_batch_holds_delete_prd074 BEFORE DELETE ON batch_budget_holds FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_budget_immutable'); END;
CREATE TRIGGER trg_batch_jobs_delete_prd074 BEFORE DELETE ON batch_jobs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_job_immutable'); END;
CREATE TRIGGER trg_batch_outbox_delete_prd074 BEFORE DELETE ON batch_job_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_outbox_immutable'); END;
CREATE TRIGGER trg_batch_reconciliation_delete_prd074 BEFORE DELETE ON batch_reconciliation_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_reconciliation_immutable'); END;
CREATE TRIGGER trg_batch_scheduler_delete_prd074
BEFORE DELETE ON batch_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_scheduler_cursor_immutable'); END;
CREATE TRIGGER trg_batch_tasks_delete_prd074 BEFORE DELETE ON batch_tasks FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'batch_task_immutable'); END;
CREATE TRIGGER trg_billing_dispute_adjustment_links_delete_prd080
BEFORE DELETE ON billing_dispute_adjustment_links FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_adjustment_link_immutable'); END;
CREATE TRIGGER trg_billing_dispute_events_delete_prd080
BEFORE DELETE ON billing_dispute_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_event_immutable'); END;
CREATE TRIGGER trg_billing_dispute_items_delete_prd080
BEFORE DELETE ON billing_dispute_items FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_item_immutable'); END;
CREATE TRIGGER trg_billing_dispute_outbox_delete_prd080
BEFORE DELETE ON billing_dispute_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_outbox_immutable'); END;
CREATE TRIGGER trg_billing_dispute_scheduler_delete_prd080
BEFORE DELETE ON billing_dispute_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_scheduler_cursor_immutable'); END;
CREATE TRIGGER trg_billing_dispute_transitions_delete_prd080
BEFORE DELETE ON billing_dispute_transitions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_transition_immutable'); END;
CREATE TRIGGER trg_billing_disputes_delete_prd080
BEFORE DELETE ON billing_disputes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_dispute_immutable'); END;
CREATE TRIGGER trg_billing_domain_reconciliation_difference_delete_prd144
BEFORE DELETE ON billing_domain_reconciliation_differences
BEGIN SELECT RAISE(ABORT,'billing_domain_reconciliation_difference_immutable'); END;
CREATE TRIGGER trg_billing_domain_reconciliation_source_delete_prd144
BEFORE DELETE ON billing_domain_reconciliation_source_snapshots
BEGIN SELECT RAISE(ABORT,'billing_domain_reconciliation_source_immutable'); END;
CREATE TRIGGER trg_billing_settlement_holds_delete_prd080
BEFORE DELETE ON billing_settlement_holds FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'billing_settlement_hold_immutable'); END;
CREATE TRIGGER trg_billing_statements_delete_prd104 BEFORE DELETE ON billing_statements
BEGIN SELECT RAISE(ABORT,'billing_statement_immutable'); END;
CREATE TRIGGER trg_brand_legacy_mapping_delete_prd142 BEFORE DELETE ON brand_legacy_object_mappings
BEGIN SELECT RAISE(ABORT,'brand_mapping_immutable'); END;
CREATE TRIGGER trg_branding_configs_cutover_delete_prd142 BEFORE DELETE ON branding_configs
BEGIN SELECT RAISE(ABORT,'branding_cutover_read_only'); END;
CREATE TRIGGER trg_branding_configs_delete_prd088 BEFORE DELETE ON branding_configs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'branding_config_immutable'); END;
CREATE TRIGGER trg_branding_event_outbox_delete_prd088
BEFORE DELETE ON branding_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'branding_outbox_immutable'); END;
CREATE TRIGGER trg_branding_events_delete_prd088 BEFORE DELETE ON branding_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'branding_event_immutable'); END;
CREATE TRIGGER trg_budget_scope_events_prd052_immutable_delete
BEFORE DELETE ON budget_scope_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'budget_scope_event_immutable');
END;
CREATE TRIGGER trg_business_verification_intent_delete_prd102
BEFORE DELETE ON business_verification_intents
BEGIN SELECT RAISE(ABORT,'business_verification_intent_immutable'); END;
CREATE TRIGGER trg_cache_affinities_prd050_delete
BEFORE DELETE ON cache_affinities
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_delete_forbidden');
END;
CREATE TRIGGER trg_cache_affinity_bindings_prd050_delete
BEFORE DELETE ON cache_affinity_bindings
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinity_scopes scope
    WHERE scope.tenant_id=OLD.tenant_id
      AND scope.cache_affinity_scope_id=OLD.cache_affinity_scope_id
      AND scope.preferred_binding_id=OLD.cache_affinity_binding_id
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_selections selection
    WHERE selection.tenant_id=OLD.tenant_id
      AND selection.cache_affinity_binding_id_snapshot=OLD.cache_affinity_binding_id
      AND selection.outcome_status='pending'
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_binding_delete_unsafe');
END;
CREATE TRIGGER trg_cache_affinity_config_events_prd050_immutable_delete
BEFORE DELETE ON cache_affinity_config_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_config_event_immutable');
END;
CREATE TRIGGER trg_cache_affinity_scopes_prd050_delete
BEFORE DELETE ON cache_affinity_scopes
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM cache_affinity_bindings binding
    WHERE binding.tenant_id=OLD.tenant_id
      AND binding.cache_affinity_scope_id=OLD.cache_affinity_scope_id
  )
  OR EXISTS (
    SELECT 1 FROM cache_affinity_selections selection
    WHERE selection.tenant_id=OLD.tenant_id
      AND selection.cache_affinity_scope_id_snapshot=OLD.cache_affinity_scope_id
      AND selection.outcome_status='pending'
  )
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_scope_delete_unsafe');
END;
CREATE TRIGGER trg_cache_affinity_selections_prd050_delete
BEFORE DELETE ON cache_affinity_selections
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cache_affinity_selection_immutable');
END;
CREATE TRIGGER trg_cache_watermarks_delete_prd108 BEFORE DELETE ON cache_watermarks
BEGIN SELECT RAISE(ABORT,'cache_watermark_immutable'); END;
CREATE TRIGGER trg_channel_action_delete_prd139 BEFORE DELETE ON channel_action_commands
BEGIN SELECT RAISE(ABORT,'channel_action_immutable'); END;
CREATE TRIGGER trg_channel_brands_delete_prd142 BEFORE DELETE ON channel_brands
BEGIN SELECT RAISE(ABORT,'channel_brand_immutable'); END;
CREATE TRIGGER trg_channel_event_delete_prd139 BEFORE DELETE ON channel_events
BEGIN SELECT RAISE(ABORT,'channel_event_immutable'); END;
CREATE TRIGGER trg_channel_mapping_delete_prd139 BEFORE DELETE ON channel_legacy_object_mappings
BEGIN SELECT RAISE(ABORT,'channel_mapping_immutable'); END;
CREATE TRIGGER trg_channel_membership_delete_prd139 BEFORE DELETE ON channel_memberships
BEGIN SELECT RAISE(ABORT,'channel_membership_immutable'); END;
CREATE TRIGGER trg_channel_outbox_delete_prd139 BEFORE DELETE ON channel_event_outbox
BEGIN SELECT RAISE(ABORT,'channel_outbox_immutable'); END;
CREATE TRIGGER trg_channel_partner_delete_prd139 BEFORE DELETE ON channel_partners
BEGIN SELECT RAISE(ABORT,'channel_partner_immutable'); END;
CREATE TRIGGER trg_channel_rule_delete_prd139 BEFORE DELETE ON channel_commission_rule_snapshots
BEGIN SELECT RAISE(ABORT,'channel_rule_immutable'); END;
CREATE TRIGGER trg_channel_statement_delete_prd139 BEFORE DELETE ON channel_statements
BEGIN SELECT RAISE(ABORT,'channel_statement_immutable'); END;
CREATE TRIGGER trg_channel_statement_line_delete_prd139 BEFORE DELETE ON channel_statement_lines
BEGIN SELECT RAISE(ABORT,'channel_statement_line_immutable'); END;
CREATE TRIGGER trg_chat_execution_grants_delete_prd066
BEFORE DELETE ON chat_execution_grants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'chat_grant_immutable'); END;
CREATE TRIGGER trg_chat_messages_delete_prd066
BEFORE DELETE ON chat_messages FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'chat_message_immutable'); END;
CREATE TRIGGER trg_chat_prompt_invocations_delete_prd067
BEFORE DELETE ON chat_prompt_template_invocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_invocation_immutable'); END;
CREATE TRIGGER trg_chat_sessions_delete_prd066
BEFORE DELETE ON chat_sessions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'chat_session_immutable'); END;
CREATE TRIGGER trg_client_credentials_delete_prd118
BEFORE DELETE ON client_credentials
BEGIN SELECT RAISE(ABORT,'client_credential_immutable'); END;
CREATE TRIGGER trg_commercial_effects_delete_prd103
BEFORE DELETE ON commercial_effects
BEGIN SELECT RAISE(ABORT,'commercial_effect_immutable'); END;
CREATE TRIGGER trg_commission_records_delete_prd087 BEFORE DELETE ON commission_records FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'commission_record_immutable'); END;
CREATE TRIGGER trg_compliance_commands_delete_prd135 BEFORE DELETE ON compliance_action_commands
BEGIN SELECT RAISE(ABORT,'compliance_command_immutable'); END;
CREATE TRIGGER trg_compliance_events_delete_prd135 BEFORE DELETE ON compliance_events
BEGIN SELECT RAISE(ABORT,'compliance_event_immutable'); END;
CREATE TRIGGER trg_compliance_evidence_delete_prd135 BEFORE DELETE ON compliance_evidence
BEGIN SELECT RAISE(ABORT,'compliance_evidence_immutable'); END;
CREATE TRIGGER trg_compliance_exceptions_delete_prd135 BEFORE DELETE ON compliance_exceptions
BEGIN SELECT RAISE(ABORT,'compliance_exception_immutable'); END;
CREATE TRIGGER trg_compliance_outbox_delete_prd135 BEFORE DELETE ON compliance_event_outbox
BEGIN SELECT RAISE(ABORT,'compliance_outbox_immutable'); END;
CREATE TRIGGER trg_compliance_remediation_delete_prd135 BEFORE DELETE ON compliance_remediation_tasks
BEGIN SELECT RAISE(ABORT,'compliance_remediation_immutable'); END;
CREATE TRIGGER trg_compliance_replay_delete_prd135 BEFORE DELETE ON compliance_collector_replay_guards
BEGIN SELECT RAISE(ABORT,'compliance_replay_guard_immutable'); END;
CREATE TRIGGER trg_compliance_report_items_delete_prd135 BEFORE DELETE ON compliance_report_items
BEGIN SELECT RAISE(ABORT,'compliance_report_immutable'); END;
CREATE TRIGGER trg_compliance_reports_delete_prd135 BEFORE DELETE ON compliance_reports
BEGIN SELECT RAISE(ABORT,'compliance_report_immutable'); END;
CREATE TRIGGER trg_config_acks_delete_prd108 BEFORE DELETE ON config_acks
BEGIN SELECT RAISE(ABORT,'config_ack_immutable'); END;
CREATE TRIGGER trg_config_deltas_delete_prd108 BEFORE DELETE ON config_deltas
BEGIN SELECT RAISE(ABORT,'config_delta_immutable'); END;
CREATE TRIGGER trg_config_event_outbox_delete_prd108 BEFORE DELETE ON config_event_outbox
BEGIN SELECT RAISE(ABORT,'config_event_immutable'); END;
CREATE TRIGGER trg_config_exports_immutable_delete_prd062
BEFORE DELETE ON config_exports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_export_immutable'); END;
CREATE TRIGGER trg_config_import_rows_immutable_delete_prd062
BEFORE DELETE ON config_import_rows FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_import_row_immutable'); END;
CREATE TRIGGER trg_config_imports_immutable_delete_prd062
BEFORE DELETE ON config_imports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'config_import_immutable'); END;
CREATE TRIGGER trg_config_rebuild_jobs_delete_prd108 BEFORE DELETE ON config_rebuild_jobs
BEGIN SELECT RAISE(ABORT,'config_rebuild_job_immutable'); END;
CREATE TRIGGER trg_config_snapshot_heads_delete_prd108 BEFORE DELETE ON config_snapshot_heads
BEGIN SELECT RAISE(ABORT,'config_snapshot_head_immutable'); END;
CREATE TRIGGER trg_config_snapshots_delete_prd108 BEFORE DELETE ON config_snapshots
BEGIN SELECT RAISE(ABORT,'config_snapshot_immutable'); END;
CREATE TRIGGER trg_console_backfill_checkpoint_delete_prd147
BEFORE DELETE ON console_backfill_checkpoints
BEGIN SELECT RAISE(ABORT,'console_backfill_checkpoint_immutable'); END;
CREATE TRIGGER trg_console_migration_event_delete_prd147
BEFORE DELETE ON platform_console_migration_events
BEGIN SELECT RAISE(ABORT,'console_migration_event_immutable'); END;
CREATE TRIGGER trg_consumers_delete BEFORE DELETE ON workspace_consumers
BEGIN SELECT RAISE(ABORT,'consumer_identity_immutable'); END;
CREATE TRIGGER trg_contract_profiles_delete_prd102
BEFORE DELETE ON contract_profiles
BEGIN SELECT RAISE(ABORT,'contract_profile_immutable'); END;
CREATE TRIGGER trg_cost_anomalies_immutable_delete_prd053
BEFORE DELETE ON cost_anomalies
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_immutable');
END;
CREATE TRIGGER trg_cost_anomaly_actions_immutable_delete_prd053
BEFORE DELETE ON cost_anomaly_actions
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_action_immutable');
END;
CREATE TRIGGER trg_cost_anomaly_runs_immutable_delete_prd053
BEFORE DELETE ON cost_anomaly_detection_runs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_run_immutable');
END;
CREATE TRIGGER trg_customer_health_outbox_delete_prd086
BEFORE DELETE ON customer_health_score_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'customer_health_outbox_immutable'); END;
CREATE TRIGGER trg_customer_health_scheduler_delete_prd086
BEFORE DELETE ON customer_health_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'customer_health_scheduler_immutable'); END;
CREATE TRIGGER trg_customer_health_scores_delete_prd086
BEFORE DELETE ON customer_health_scores FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'customer_health_score_immutable'); END;
CREATE TRIGGER trg_data_classifications_delete_prd117 BEFORE DELETE ON data_classifications
BEGIN SELECT RAISE(ABORT,'data_classification_immutable'); END;
CREATE TRIGGER trg_data_governance_commands_delete_prd117 BEFORE DELETE ON data_governance_commands
BEGIN SELECT RAISE(ABORT,'data_governance_command_immutable'); END;
CREATE TRIGGER trg_data_governance_events_delete_prd117 BEFORE DELETE ON data_governance_events
BEGIN SELECT RAISE(ABORT,'data_governance_event_immutable'); END;
CREATE TRIGGER trg_data_governance_fences_delete_prd117 BEFORE DELETE ON data_governance_fences
BEGIN SELECT RAISE(ABORT,'data_governance_fence_immutable'); END;
CREATE TRIGGER trg_data_governance_outbox_delete_prd117 BEFORE DELETE ON data_governance_event_outbox
BEGIN SELECT RAISE(ABORT,'data_governance_event_immutable'); END;
CREATE TRIGGER trg_data_subject_requests_delete_prd134 BEFORE DELETE ON data_subject_requests
BEGIN SELECT RAISE(ABORT,'privacy_request_immutable'); END;
CREATE TRIGGER trg_deletion_requests_delete_prd089 BEFORE DELETE ON deletion_requests FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_immutable'); END;
CREATE TRIGGER trg_deletion_schedules_delete_prd117 BEFORE DELETE ON deletion_schedules
BEGIN SELECT RAISE(ABORT,'deletion_schedule_immutable'); END;
CREATE TRIGGER trg_deployment_brands_delete_prd142 BEFORE DELETE ON deployment_brands
BEGIN SELECT RAISE(ABORT,'deployment_brand_immutable'); END;
CREATE TRIGGER trg_dunning_cases_delete_prd103
BEFORE DELETE ON dunning_cases
BEGIN SELECT RAISE(ABORT,'dunning_case_immutable'); END;
CREATE TRIGGER trg_edge_acks_delete_prd121 BEFORE DELETE ON edge_acks
BEGIN SELECT RAISE(ABORT,'edge_ack_immutable'); END;
CREATE TRIGGER trg_edge_apply_attempts_delete_prd121
BEFORE DELETE ON edge_apply_attempts
BEGIN SELECT RAISE(ABORT,'edge_apply_attempt_immutable'); END;
CREATE TRIGGER trg_edge_backup_events_delete_prd129 BEFORE DELETE ON edge_backup_events
BEGIN SELECT RAISE(ABORT,'edge_backup_event_immutable'); END;
CREATE TRIGGER trg_edge_backup_outbox_delete_prd129 BEFORE DELETE ON edge_backup_outbox
BEGIN SELECT RAISE(ABORT,'edge_backup_outbox_immutable'); END;
CREATE TRIGGER trg_edge_backup_reports_delete_prd129
BEFORE DELETE ON edge_backup_reports
BEGIN SELECT RAISE(ABORT,'edge_backup_report_immutable'); END;
CREATE TRIGGER trg_edge_backup_status_delete_prd129
BEFORE DELETE ON edge_backup_statuses
BEGIN SELECT RAISE(ABORT,'edge_backup_status_immutable'); END;
CREATE TRIGGER trg_edge_capacity_grants_delete_prd128 BEFORE DELETE ON edge_capacity_grants
BEGIN SELECT RAISE(ABORT,'edge_capacity_grant_immutable'); END;
CREATE TRIGGER trg_edge_delivery_acceptances_delete_prd125
BEFORE DELETE ON edge_delivery_acceptances
BEGIN SELECT RAISE(ABORT,'edge_delivery_acceptance_immutable'); END;
CREATE TRIGGER trg_edge_delivery_events_delete_prd125
BEFORE DELETE ON edge_delivery_events
BEGIN SELECT RAISE(ABORT,'edge_delivery_event_immutable'); END;
CREATE TRIGGER trg_edge_delivery_outbox_delete_prd125
BEFORE DELETE ON edge_delivery_outbox
BEGIN SELECT RAISE(ABORT,'edge_delivery_outbox_immutable'); END;
CREATE TRIGGER trg_edge_desired_approvals_delete_prd121
BEFORE DELETE ON edge_desired_state_approvals
BEGIN SELECT RAISE(ABORT,'edge_desired_state_approval_immutable'); END;
CREATE TRIGGER trg_edge_desired_events_delete_prd121
BEFORE DELETE ON edge_desired_state_events
BEGIN SELECT RAISE(ABORT,'edge_desired_state_event_immutable'); END;
CREATE TRIGGER trg_edge_desired_heads_delete_prd121
BEFORE DELETE ON edge_desired_state_heads
BEGIN SELECT RAISE(ABORT,'edge_desired_state_head_immutable'); END;
CREATE TRIGGER trg_edge_desired_states_delete_prd121
BEFORE DELETE ON edge_desired_states
BEGIN SELECT RAISE(ABORT,'edge_desired_state_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_actions_delete_prd126
BEFORE DELETE ON edge_diagnostic_action_commands
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_action_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_commands_delete_prd126
BEFORE DELETE ON edge_diagnostic_commands
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_command_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_events_delete_prd126
BEFORE DELETE ON edge_diagnostic_events
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_event_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_grants_delete_prd126
BEFORE DELETE ON edge_diagnostic_grants
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_grant_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_outbox_delete_prd126
BEFORE DELETE ON edge_diagnostic_outbox
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_outbox_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_results_delete_prd126
BEFORE DELETE ON edge_diagnostic_results
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_result_immutable'); END;
CREATE TRIGGER trg_edge_failure_summaries_delete_prd122
BEFORE DELETE ON edge_failure_summaries
BEGIN SELECT RAISE(ABORT,'edge_failure_summary_immutable'); END;
CREATE TRIGGER trg_edge_fleet_index_delete_prd126
BEFORE DELETE ON edge_fleet_deployment_index
BEGIN SELECT RAISE(ABORT,'edge_fleet_index_immutable'); END;
CREATE TRIGGER trg_edge_fleet_upgrade_events_delete_prd127 BEFORE DELETE ON edge_fleet_upgrade_events
BEGIN SELECT RAISE(ABORT,'edge_fleet_upgrade_event_immutable'); END;
CREATE TRIGGER trg_edge_fleet_upgrade_outbox_delete_prd127 BEFORE DELETE ON edge_fleet_upgrade_outbox
BEGIN SELECT RAISE(ABORT,'edge_fleet_upgrade_outbox_immutable'); END;
CREATE TRIGGER trg_edge_health_heads_delete_prd126
BEFORE DELETE ON edge_health_heads
BEGIN SELECT RAISE(ABORT,'edge_health_head_immutable'); END;
CREATE TRIGGER trg_edge_health_snapshots_delete_prd126
BEFORE DELETE ON edge_health_snapshots
BEGIN SELECT RAISE(ABORT,'edge_health_snapshot_immutable'); END;
CREATE TRIGGER trg_edge_identity_events_delete_prd120
BEFORE DELETE ON edge_identity_events
BEGIN SELECT RAISE(ABORT,'edge_identity_event_immutable'); END;
CREATE TRIGGER trg_edge_install_audits_delete_prd124
BEFORE DELETE ON edge_install_audits
BEGIN SELECT RAISE(ABORT,'edge_install_audit_immutable'); END;
CREATE TRIGGER trg_edge_install_commands_delete_prd124
BEFORE DELETE ON edge_install_commands
BEGIN SELECT RAISE(ABORT,'edge_install_command_immutable'); END;
CREATE TRIGGER trg_edge_install_outbox_delete_prd124
BEFORE DELETE ON edge_install_outbox
BEGIN SELECT RAISE(ABORT,'edge_install_outbox_immutable'); END;
CREATE TRIGGER trg_edge_installations_delete_prd124
BEFORE DELETE ON edge_installations
BEGIN SELECT RAISE(ABORT,'edge_installation_immutable'); END;
CREATE TRIGGER trg_edge_license_acks_delete_prd128
BEFORE DELETE ON edge_license_acknowledgements
BEGIN SELECT RAISE(ABORT,'edge_license_ack_immutable'); END;
CREATE TRIGGER trg_edge_license_audits_delete_prd128 BEFORE DELETE ON edge_license_audits
BEGIN SELECT RAISE(ABORT,'edge_license_audit_immutable'); END;
CREATE TRIGGER trg_edge_license_lease_reports_delete_prd128
BEFORE DELETE ON edge_license_lease_reports
BEGIN SELECT RAISE(ABORT,'edge_license_lease_report_immutable'); END;
CREATE TRIGGER trg_edge_license_outbox_delete_prd128 BEFORE DELETE ON edge_license_outbox
BEGIN SELECT RAISE(ABORT,'edge_license_outbox_immutable'); END;
CREATE TRIGGER trg_edge_license_refresh_delete_prd128
BEFORE DELETE ON edge_license_refresh_requests
BEGIN SELECT RAISE(ABORT,'edge_license_refresh_immutable'); END;
CREATE TRIGGER trg_edge_licenses_delete_prd128 BEFORE DELETE ON edge_licenses
BEGIN SELECT RAISE(ABORT,'edge_license_immutable'); END;
CREATE TRIGGER trg_edge_lkg_delete_prd121 BEFORE DELETE ON edge_lkg_observations
BEGIN SELECT RAISE(ABORT,'edge_lkg_observation_immutable'); END;
CREATE TRIGGER trg_edge_machine_commands_delete_prd120
BEFORE DELETE ON edge_machine_commands
BEGIN SELECT RAISE(ABORT,'edge_machine_command_immutable'); END;
CREATE TRIGGER trg_edge_maintenance_windows_delete_prd127
BEFORE DELETE ON edge_maintenance_windows
BEGIN SELECT RAISE(ABORT,'edge_maintenance_window_immutable'); END;
CREATE TRIGGER trg_edge_offline_bundles_delete_prd130
BEFORE DELETE ON edge_offline_bundles
BEGIN SELECT RAISE(ABORT,'edge_offline_bundle_immutable'); END;
CREATE TRIGGER trg_edge_offline_conflicts_delete_prd130
BEFORE DELETE ON edge_offline_conflicts
BEGIN SELECT RAISE(ABORT,'edge_offline_conflict_immutable'); END;
CREATE TRIGGER trg_edge_offline_cursors_delete_prd130
BEFORE DELETE ON edge_offline_sync_cursors
BEGIN SELECT RAISE(ABORT,'edge_offline_cursor_immutable'); END;
CREATE TRIGGER trg_edge_offline_events_delete_prd130
BEFORE DELETE ON edge_offline_bundle_events
BEGIN SELECT RAISE(ABORT,'edge_offline_event_immutable'); END;
CREATE TRIGGER trg_edge_offline_outbox_delete_prd130
BEFORE DELETE ON edge_offline_bundle_outbox
BEGIN SELECT RAISE(ABORT,'edge_offline_outbox_immutable'); END;
CREATE TRIGGER trg_edge_offline_requests_delete_prd130
BEFORE DELETE ON edge_offline_bundle_requests
BEGIN SELECT RAISE(ABORT,'edge_offline_request_immutable'); END;
CREATE TRIGGER trg_edge_offline_resolutions_delete_prd130
BEFORE DELETE ON edge_offline_conflict_resolutions
BEGIN SELECT RAISE(ABORT,'edge_offline_resolution_immutable'); END;
CREATE TRIGGER trg_edge_offline_transfers_delete_prd130
BEFORE DELETE ON edge_offline_bundle_transfers
BEGIN SELECT RAISE(ABORT,'edge_offline_transfer_immutable'); END;
CREATE TRIGGER trg_edge_preflight_reports_delete_prd124
BEFORE DELETE ON edge_preflight_reports
BEGIN SELECT RAISE(ABORT,'edge_preflight_report_immutable'); END;
CREATE TRIGGER trg_edge_release_heads_delete_prd127
BEFORE DELETE ON edge_deployment_release_heads
BEGIN SELECT RAISE(ABORT,'edge_release_head_immutable'); END;
CREATE TRIGGER trg_edge_restore_jobs_delete_prd129
BEFORE DELETE ON edge_restore_jobs
BEGIN SELECT RAISE(ABORT,'edge_restore_job_immutable'); END;
CREATE TRIGGER trg_edge_restore_verifications_delete_prd129
BEFORE DELETE ON edge_restore_verifications
BEGIN SELECT RAISE(ABORT,'edge_restore_verification_immutable'); END;
CREATE TRIGGER trg_edge_rollback_attempts_delete_prd127 BEFORE DELETE ON edge_rollback_attempts
BEGIN SELECT RAISE(ABORT,'edge_rollback_attempt_immutable'); END;
CREATE TRIGGER trg_edge_rollout_targets_delete_prd127 BEFORE DELETE ON edge_rollout_targets
BEGIN SELECT RAISE(ABORT,'edge_rollout_target_immutable'); END;
CREATE TRIGGER trg_edge_secret_events_delete_prd123
BEFORE DELETE ON edge_secret_events
BEGIN SELECT RAISE(ABORT,'edge_secret_event_immutable'); END;
CREATE TRIGGER trg_edge_secret_outbox_delete_prd123
BEFORE DELETE ON edge_secret_event_outbox
BEGIN SELECT RAISE(ABORT,'edge_secret_outbox_immutable'); END;
CREATE TRIGGER trg_edge_secret_references_delete_prd123
BEFORE DELETE ON edge_secret_references
BEGIN SELECT RAISE(ABORT,'edge_secret_reference_immutable'); END;
CREATE TRIGGER trg_edge_secret_sync_batches_delete_prd123
BEFORE DELETE ON edge_secret_sync_batches
BEGIN SELECT RAISE(ABORT,'edge_secret_sync_batch_immutable'); END;
CREATE TRIGGER trg_edge_secret_sync_receipts_delete_prd123
BEFORE DELETE ON edge_secret_sync_receipts
BEGIN SELECT RAISE(ABORT,'edge_secret_sync_receipt_immutable'); END;
CREATE TRIGGER trg_edge_signed_entitlements_delete_prd128
BEFORE DELETE ON edge_signed_entitlements
BEGIN SELECT RAISE(ABORT,'edge_signed_entitlement_immutable'); END;
CREATE TRIGGER trg_edge_signed_snapshots_delete_prd121
BEFORE DELETE ON edge_signed_snapshots
BEGIN SELECT RAISE(ABORT,'edge_signed_snapshot_immutable'); END;
CREATE TRIGGER trg_edge_snapshot_artifacts_delete_prd121
BEFORE DELETE ON edge_snapshot_artifacts
BEGIN SELECT RAISE(ABORT,'edge_snapshot_artifact_immutable'); END;
CREATE TRIGGER trg_edge_support_bundles_delete_prd125
BEFORE DELETE ON edge_support_bundles
BEGIN SELECT RAISE(ABORT,'edge_support_bundle_immutable'); END;
CREATE TRIGGER trg_edge_support_downloads_delete_prd125
BEFORE DELETE ON edge_support_bundle_download_audits
BEGIN SELECT RAISE(ABORT,'edge_support_bundle_download_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_batches_delete_prd122
BEFORE DELETE ON edge_telemetry_batches
BEGIN SELECT RAISE(ABORT,'edge_telemetry_batch_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_events_delete_prd122
BEFORE DELETE ON edge_telemetry_events
BEGIN SELECT RAISE(ABORT,'edge_telemetry_event_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_policies_delete_prd122
BEFORE DELETE ON edge_telemetry_policies
BEGIN SELECT RAISE(ABORT,'edge_telemetry_policy_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_policy_heads_delete_prd122
BEFORE DELETE ON edge_telemetry_policy_heads
BEGIN SELECT RAISE(ABORT,'edge_telemetry_policy_head_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_receipts_delete_prd122
BEFORE DELETE ON edge_telemetry_receipts
BEGIN SELECT RAISE(ABORT,'edge_telemetry_receipt_immutable'); END;
CREATE TRIGGER trg_edge_upgrade_commands_delete_prd127 BEFORE DELETE ON edge_upgrade_commands
BEGIN SELECT RAISE(ABORT,'edge_upgrade_command_immutable'); END;
CREATE TRIGGER trg_edge_upgrade_receipts_delete_prd127 BEFORE DELETE ON edge_upgrade_receipts
BEGIN SELECT RAISE(ABORT,'edge_upgrade_receipt_immutable'); END;
CREATE TRIGGER trg_edge_usage_cursors_delete_prd122
BEFORE DELETE ON edge_usage_sync_cursors
BEGIN SELECT RAISE(ABORT,'edge_usage_cursor_immutable'); END;
CREATE TRIGGER trg_edge_usage_reconciliations_delete_prd122
BEFORE DELETE ON edge_usage_reconciliations
BEGIN SELECT RAISE(ABORT,'edge_usage_reconciliation_immutable'); END;
CREATE TRIGGER trg_edge_validation_checks_delete_prd125
BEFORE DELETE ON edge_validation_checks
BEGIN SELECT RAISE(ABORT,'edge_validation_check_immutable'); END;
CREATE TRIGGER trg_edge_validation_commands_delete_prd125
BEFORE DELETE ON edge_validation_commands
BEGIN SELECT RAISE(ABORT,'edge_validation_command_immutable'); END;
CREATE TRIGGER trg_edge_validation_runs_delete_prd125
BEFORE DELETE ON edge_validation_runs
BEGIN SELECT RAISE(ABORT,'edge_validation_run_immutable'); END;
CREATE TRIGGER trg_enterprise_identity_commands_delete_prd114
BEFORE DELETE ON enterprise_identity_commands
BEGIN SELECT RAISE(ABORT,'enterprise_identity_command_immutable'); END;
CREATE TRIGGER trg_enterprise_identity_events_delete_prd114
BEFORE DELETE ON enterprise_identity_event_outbox
BEGIN SELECT RAISE(ABORT,'enterprise_identity_event_immutable'); END;
CREATE TRIGGER trg_enterprise_identity_session_grants_delete_prd114
BEFORE DELETE ON enterprise_identity_session_grants
BEGIN SELECT RAISE(ABORT,'enterprise_identity_session_grant_immutable'); END;
CREATE TRIGGER trg_entitlement_snapshots_delete_prd098
BEFORE DELETE ON entitlement_snapshots FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'entitlement_snapshot_immutable'); END;
CREATE TRIGGER trg_erasure_decisions_delete_prd134 BEFORE DELETE ON erasure_decisions
BEGIN SELECT RAISE(ABORT,'erasure_decision_immutable'); END;
CREATE TRIGGER trg_experiment_assignments_delete_prd075
BEFORE DELETE ON experiment_assignments FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_assignment_immutable'); END;
CREATE TRIGGER trg_experiment_events_delete_prd075
BEFORE DELETE ON experiment_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_event_immutable'); END;
CREATE TRIGGER trg_experiment_executions_delete_prd075
BEFORE DELETE ON experiment_request_executions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_execution_immutable'); END;
CREATE TRIGGER trg_experiment_feedback_delete_prd075
BEFORE DELETE ON experiment_feedback_attributions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_feedback_immutable'); END;
CREATE TRIGGER trg_experiment_outbox_delete_prd075
BEFORE DELETE ON experiment_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_outbox_immutable'); END;
CREATE TRIGGER trg_experiment_outcomes_delete_prd075
BEFORE DELETE ON experiment_request_outcomes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_outcome_immutable'); END;
CREATE TRIGGER trg_experiment_results_delete_prd075
BEFORE DELETE ON experiment_results FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_result_immutable'); END;
CREATE TRIGGER trg_experiment_settlements_delete_prd075
BEFORE DELETE ON experiment_usage_settlements FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_settlement_immutable'); END;
CREATE TRIGGER trg_feature_event_delete_prd107 BEFORE DELETE ON feature_event_outbox
BEGIN SELECT RAISE(ABORT,'feature_event_immutable'); END;
CREATE TRIGGER trg_feature_exposures_delete_prd107 BEFORE DELETE ON feature_exposures
BEGIN SELECT RAISE(ABORT,'feature_exposure_immutable'); END;
CREATE TRIGGER trg_federation_links_delete_prd114 BEFORE DELETE ON account_federation_links
BEGIN SELECT RAISE(ABORT,'federation_link_immutable'); END;
CREATE TRIGGER trg_file_dlp_matches_delete_prd072
BEFORE DELETE ON file_dlp_matches FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_dlp_match_immutable'); END;
CREATE TRIGGER trg_file_events_delete_prd072
BEFORE DELETE ON file_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_event_immutable'); END;
CREATE TRIGGER trg_file_extracts_delete_prd072
BEFORE DELETE ON file_extracts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_extract_immutable'); END;
CREATE TRIGGER trg_file_security_event_outbox_delete_prd073_recovery
BEFORE DELETE ON file_security_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_event_outbox_immutable'); END;
CREATE TRIGGER trg_file_security_events_delete_prd073
BEFORE DELETE ON file_security_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_event_immutable'); END;
CREATE TRIGGER trg_file_security_findings_delete_prd073
BEFORE DELETE ON file_security_findings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_finding_immutable'); END;
CREATE TRIGGER trg_file_security_scans_delete_prd073
BEFORE DELETE ON file_security_scans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_security_scan_immutable'); END;
CREATE TRIGGER trg_finops_adjustments_immutable_delete_prd051
BEFORE DELETE ON finops_adjustments
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'finops_adjustment_append_only');
END;
CREATE TRIGGER trg_finops_ledger_disputes_delete_prd071
BEFORE DELETE ON finops_ledger_disputes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'finops_ledger_dispute_immutable'); END;
CREATE TRIGGER trg_finops_ledger_immutable_delete_prd051
BEFORE DELETE ON finops_ledger_entries
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_append_only');
END;
CREATE TRIGGER trg_flag_evaluations_delete_prd107 BEFORE DELETE ON flag_evaluation_snapshots
BEGIN SELECT RAISE(ABORT,'flag_evaluation_immutable'); END;
CREATE TRIGGER trg_ga_launch_delete_guard
BEFORE DELETE ON ga_launches
BEGIN SELECT RAISE(ABORT,'ga_launch_immutable'); END;
CREATE TRIGGER trg_gateway_cell_events_delete_prd101
BEFORE DELETE ON gateway_cell_events
BEGIN SELECT RAISE(ABORT,'gateway_cell_event_immutable'); END;
CREATE TRIGGER trg_gateway_claims_scope_delete_prd075
BEFORE DELETE ON gateway_request_claims FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM gateway_request_scope_claims scope
  WHERE scope.tenant_id=OLD.tenant_id AND scope.request_id=OLD.request_id)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;
CREATE TRIGGER trg_gateway_placements_delete_prd101
BEFORE DELETE ON workspace_gateway_placements
BEGIN SELECT RAISE(ABORT,'gateway_placement_immutable'); END;
CREATE TRIGGER trg_gateway_request_claims_immutable_delete
BEFORE DELETE ON gateway_request_claims
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_claim_immutable');
END;
CREATE TRIGGER trg_gateway_scope_claims_delete_prd075
BEFORE DELETE ON gateway_request_scope_claims FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_scope_claim_immutable'); END;
CREATE TRIGGER trg_growth_assignment_delete_prd141 BEFORE DELETE ON platform_growth_experiment_assignments
BEGIN SELECT RAISE(ABORT,'growth_assignment_immutable'); END;
CREATE TRIGGER trg_growth_attribution_delete_prd141 BEFORE DELETE ON platform_conversion_attributions
BEGIN SELECT RAISE(ABORT,'growth_attribution_immutable'); END;
CREATE TRIGGER trg_growth_eligibility_delete_prd141 BEFORE DELETE ON platform_growth_event_eligibility
BEGIN SELECT RAISE(ABORT,'growth_eligibility_immutable'); END;
CREATE TRIGGER trg_growth_export_subject_delete_prd141 BEFORE DELETE ON platform_growth_export_subjects
BEGIN SELECT RAISE(ABORT,'growth_export_subject_immutable'); END;
CREATE TRIGGER trg_growth_legacy_mapping_delete_prd141 BEFORE DELETE ON platform_growth_legacy_mappings
BEGIN SELECT RAISE(ABORT,'growth_mapping_immutable'); END;
CREATE TRIGGER trg_growth_legacy_reconciliation_delete_prd141 BEFORE DELETE ON platform_growth_legacy_reconciliations
BEGIN SELECT RAISE(ABORT,'growth_reconciliation_immutable'); END;
CREATE TRIGGER trg_identity_exports_delete_prd134 BEFORE DELETE ON identity_exports
BEGIN SELECT RAISE(ABORT,'identity_export_immutable'); END;
CREATE TRIGGER trg_identity_security_events_delete_prd116
BEFORE DELETE ON identity_security_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'identity_security_event_immutable'); END;
CREATE TRIGGER trg_incident_scopes_delete_prd112 BEFORE DELETE ON incident_workspace_scopes
BEGIN SELECT RAISE(ABORT,'incident_scope_immutable'); END;
CREATE TRIGGER trg_intent_risk_events_delete_prd069
BEFORE DELETE ON intent_risk_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_event_immutable'); END;
CREATE TRIGGER trg_intent_risk_results_delete_prd069
BEFORE DELETE ON intent_risk_results FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_result_immutable'); END;
CREATE TRIGGER trg_invitation_events_delete_prd060
BEFORE DELETE ON invitation_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_event_immutable'); END;
CREATE TRIGGER trg_invitation_identity_bindings_delete_prd094
BEFORE DELETE ON invitation_identity_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_identity_binding_immutable'); END;
CREATE TRIGGER trg_invitations_delete_prd060
BEFORE DELETE ON invitations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_immutable'); END;
CREATE TRIGGER trg_invoice_artifacts_delete_prd104 BEFORE DELETE ON invoice_artifacts
BEGIN SELECT RAISE(ABORT,'invoice_artifact_immutable'); END;
CREATE TRIGGER trg_invoice_download_grants_delete_prd104 BEFORE DELETE ON invoice_download_grants
BEGIN SELECT RAISE(ABORT,'invoice_download_grant_immutable'); END;
CREATE TRIGGER trg_invoice_requests_delete_prd104 BEFORE DELETE ON invoice_requests
BEGIN SELECT RAISE(ABORT,'invoice_request_immutable'); END;
CREATE TRIGGER trg_isolation_run_delete_prd137 BEFORE DELETE ON isolation_test_runs
BEGIN SELECT RAISE(ABORT,'isolation_test_run_immutable'); END;
CREATE TRIGGER trg_key_consumers_delete BEFORE DELETE ON virtual_key_consumers
BEGIN SELECT RAISE(ABORT,'consumer_binding_immutable'); END;
CREATE TRIGGER trg_launch_action_delete_prd138 BEFORE DELETE ON launch_action_commands
BEGIN SELECT RAISE(ABORT,'launch_action_immutable'); END;
CREATE TRIGGER trg_launch_artifact_delete_prd138 BEFORE DELETE ON launch_candidate_artifacts
BEGIN SELECT RAISE(ABORT,'launch_artifact_immutable'); END;
CREATE TRIGGER trg_launch_event_delete_prd138 BEFORE DELETE ON launch_events
BEGIN SELECT RAISE(ABORT,'launch_event_immutable'); END;
CREATE TRIGGER trg_launch_evidence_delete_prd138 BEFORE DELETE ON launch_evidence
BEGIN SELECT RAISE(ABORT,'launch_evidence_immutable'); END;
CREATE TRIGGER trg_launch_outbox_delete_prd138 BEFORE DELETE ON launch_event_outbox
BEGIN SELECT RAISE(ABORT,'launch_outbox_immutable'); END;
CREATE TRIGGER trg_launch_review_delete_prd138 BEFORE DELETE ON post_launch_reviews
BEGIN SELECT RAISE(ABORT,'launch_review_immutable'); END;
CREATE TRIGGER trg_launch_rollback_delete_prd138 BEFORE DELETE ON launch_rollbacks
BEGIN SELECT RAISE(ABORT,'launch_rollback_immutable'); END;
CREATE TRIGGER trg_launch_wave_delete_prd138 BEFORE DELETE ON launch_waves
BEGIN SELECT RAISE(ABORT,'launch_wave_immutable'); END;
CREATE TRIGGER trg_legacy_commissions_delete_prd139 BEFORE DELETE ON commission_records
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_health_outbox_delete_prd140 BEFORE DELETE ON customer_health_score_outbox
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_scheduler_delete_prd140 BEFORE DELETE ON customer_health_scheduler_tenants
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_health_scores_delete_prd140 BEFORE DELETE ON customer_health_scores
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legacy_partner_cursors_delete_prd139 BEFORE DELETE ON partner_commission_cursors
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_customers_delete_prd139 BEFORE DELETE ON partner_customers
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_operations_delete_prd139 BEFORE DELETE ON partner_portal_operations
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partner_outbox_delete_prd139 BEFORE DELETE ON partner_customer_outbox
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_partners_delete_prd139 BEFORE DELETE ON partners
BEGIN SELECT RAISE(ABORT,'legacy_partner_retired'); END;
CREATE TRIGGER trg_legacy_workspace_health_delete_prd140 BEFORE DELETE ON workspace_health_projections
BEGIN SELECT RAISE(ABORT,'legacy_customer_health_retired'); END;
CREATE TRIGGER trg_legal_entity_delete_prd102
BEFORE DELETE ON legal_entity_profiles
BEGIN SELECT RAISE(ABORT,'legal_entity_immutable'); END;
CREATE TRIGGER trg_legal_holds_delete_prd117 BEFORE DELETE ON legal_holds
BEGIN SELECT RAISE(ABORT,'legal_hold_immutable'); END;
CREATE TRIGGER trg_local_endpoint_create_receipts_delete_prd084
BEFORE DELETE ON local_endpoint_create_receipts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_endpoint_receipt_immutable'); END;
CREATE TRIGGER trg_local_endpoint_model_delete_prd084
BEFORE DELETE ON models FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM local_model_endpoints endpoint
  WHERE endpoint.tenant_id=OLD.tenant_id AND endpoint.initial_model_id=OLD.model_id)
BEGIN SELECT RAISE(ABORT,'local_endpoint_model_immutable'); END;
CREATE TRIGGER trg_local_endpoint_provider_delete_prd084
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM local_model_endpoints endpoint
  WHERE endpoint.tenant_id=OLD.tenant_id AND endpoint.provider_id=OLD.provider_id)
BEGIN SELECT RAISE(ABORT,'local_endpoint_provider_immutable'); END;
CREATE TRIGGER trg_local_model_call_events_delete_prd084
BEFORE DELETE ON local_model_call_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_model_call_event_immutable'); END;
CREATE TRIGGER trg_local_model_call_outbox_delete_prd084
BEFORE DELETE ON local_model_call_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_model_call_outbox_immutable'); END;
CREATE TRIGGER trg_local_model_catalog_entries_delete_prd084
BEFORE DELETE ON local_model_catalog_entries FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_endpoint_catalog_immutable'); END;
CREATE TRIGGER trg_local_model_endpoints_delete_prd084
BEFORE DELETE ON local_model_endpoints FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_endpoint_immutable'); END;
CREATE TRIGGER trg_local_workspace_admission_delete BEFORE DELETE ON local_workspace_creation_admissions
BEGIN SELECT RAISE(ABORT,'local_workspace_admission_immutable'); END;
CREATE TRIGGER trg_machine_identity_audits_delete_prd118
BEFORE DELETE ON machine_identity_audits
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_immutable'); END;
CREATE TRIGGER trg_machine_identity_commands_delete_prd118
BEFORE DELETE ON machine_identity_commands
BEGIN SELECT RAISE(ABORT,'machine_identity_command_immutable'); END;
CREATE TRIGGER trg_machine_identity_outbox_delete_prd118
BEFORE DELETE ON machine_identity_event_outbox
BEGIN SELECT RAISE(ABORT,'machine_identity_outbox_immutable'); END;
CREATE TRIGGER trg_maintenance_delete_prd112 BEFORE DELETE ON maintenance_windows
BEGIN SELECT RAISE(ABORT,'maintenance_immutable'); END;
CREATE TRIGGER trg_mcp_attempts_delete_prd082
BEFORE DELETE ON mcp_invocation_attempts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_attempt_immutable'); END;
CREATE TRIGGER trg_mcp_credentials_delete_prd082
BEFORE DELETE ON mcp_server_credentials FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_credential_immutable'); END;
CREATE TRIGGER trg_mcp_events_delete_prd082
BEFORE DELETE ON mcp_invocation_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_event_immutable'); END;
CREATE TRIGGER trg_mcp_invocations_delete_prd082
BEFORE DELETE ON mcp_invocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_invocation_immutable'); END;
CREATE TRIGGER trg_mcp_network_bindings_delete_prd083
BEFORE DELETE ON mcp_invocation_network_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_binding_immutable'); END;
CREATE TRIGGER trg_mcp_network_call_intents_delete_prd083
BEFORE DELETE ON mcp_network_call_intents FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_call_intent_immutable'); END;
CREATE TRIGGER trg_mcp_network_circuits_delete_prd083
BEFORE DELETE ON mcp_network_circuits FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_circuit_immutable'); END;
CREATE TRIGGER trg_mcp_network_decisions_delete_prd083
BEFORE DELETE ON mcp_network_decisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_decision_immutable'); END;
CREATE TRIGGER trg_mcp_network_event_outbox_delete_prd083
BEFORE DELETE ON mcp_network_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_outbox_immutable'); END;
CREATE TRIGGER trg_mcp_network_events_delete_prd083
BEFORE DELETE ON mcp_network_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_event_immutable'); END;
CREATE TRIGGER trg_mcp_network_policies_delete_prd083
BEFORE DELETE ON mcp_network_policies FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_policy_immutable'); END;
CREATE TRIGGER trg_mcp_network_revisions_delete_prd083
BEFORE DELETE ON mcp_network_policy_revisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_network_revision_immutable'); END;
CREATE TRIGGER trg_mcp_operations_delete_prd082
BEFORE DELETE ON mcp_server_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_operation_immutable'); END;
CREATE TRIGGER trg_mcp_outbox_delete_prd082
BEFORE DELETE ON mcp_invocation_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_outbox_immutable'); END;
CREATE TRIGGER trg_mcp_role_grants_delete_prd082
BEFORE DELETE ON mcp_tool_role_grants FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM mcp_tools tool
    WHERE tool.tenant_id=OLD.tenant_id AND tool.mcp_tool_id=OLD.mcp_tool_id
      AND tool.status='active')
  AND NOT EXISTS(SELECT 1 FROM mcp_tool_role_grants other
    WHERE other.tenant_id=OLD.tenant_id AND other.mcp_tool_id=OLD.mcp_tool_id
      AND other.mcp_tool_role_grant_id!=OLD.mcp_tool_role_grant_id)
  AND NOT EXISTS(SELECT 1 FROM mcp_tool_user_grants other
    WHERE other.tenant_id=OLD.tenant_id AND other.mcp_tool_id=OLD.mcp_tool_id)
BEGIN SELECT RAISE(ABORT,'mcp_tool_last_grant_required'); END;
CREATE TRIGGER trg_mcp_scheduler_delete_prd082
BEFORE DELETE ON mcp_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_scheduler_immutable'); END;
CREATE TRIGGER trg_mcp_servers_delete_prd082
BEFORE DELETE ON mcp_servers FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_server_immutable'); END;
CREATE TRIGGER trg_mcp_sync_jobs_delete_prd082
BEFORE DELETE ON mcp_server_sync_jobs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_sync_job_immutable'); END;
CREATE TRIGGER trg_mcp_tools_delete_prd082
BEFORE DELETE ON mcp_tools FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'mcp_tool_immutable'); END;
CREATE TRIGGER trg_mcp_user_grants_delete_prd082
BEFORE DELETE ON mcp_tool_user_grants FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM mcp_tools tool
    WHERE tool.tenant_id=OLD.tenant_id AND tool.mcp_tool_id=OLD.mcp_tool_id
      AND tool.status='active')
  AND NOT EXISTS(SELECT 1 FROM mcp_tool_user_grants other
    WHERE other.tenant_id=OLD.tenant_id AND other.mcp_tool_id=OLD.mcp_tool_id
      AND other.mcp_tool_user_grant_id!=OLD.mcp_tool_user_grant_id)
  AND NOT EXISTS(SELECT 1 FROM mcp_tool_role_grants other
    WHERE other.tenant_id=OLD.tenant_id AND other.mcp_tool_id=OLD.mcp_tool_id)
BEGIN SELECT RAISE(ABORT,'mcp_tool_last_grant_required'); END;
CREATE TRIGGER trg_membership_events_delete_prd094
BEFORE DELETE ON membership_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'membership_event_immutable'); END;
CREATE TRIGGER trg_membership_invitations_delete_prd094
BEFORE DELETE ON membership_invitations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'membership_invitation_immutable'); END;
CREATE TRIGGER trg_meter_recovery_delete BEFORE DELETE ON platform_meter_recovery_actions
BEGIN SELECT RAISE(ABORT,'meter_recovery_immutable'); END;
CREATE TRIGGER trg_migration_assessments_delete_prd131
BEFORE DELETE ON migration_assessments
BEGIN SELECT RAISE(ABORT,'migration_assessment_immutable'); END;
CREATE TRIGGER trg_migration_checkpoints_delete_prd131
BEFORE DELETE ON migration_cutover_checkpoints
BEGIN SELECT RAISE(ABORT,'migration_checkpoint_immutable'); END;
CREATE TRIGGER trg_migration_cutover_heads_delete_prd131
BEFORE DELETE ON migration_cutover_heads
BEGIN SELECT RAISE(ABORT,'migration_cutover_head_immutable'); END;
CREATE TRIGGER trg_migration_differences_delete_prd131
BEFORE DELETE ON migration_differences
BEGIN SELECT RAISE(ABORT,'migration_difference_immutable'); END;
CREATE TRIGGER trg_migration_events_delete_prd131
BEFORE DELETE ON migration_events
BEGIN SELECT RAISE(ABORT,'migration_event_immutable'); END;
CREATE TRIGGER trg_migration_outbox_delete_prd131
BEFORE DELETE ON migration_event_outbox
BEGIN SELECT RAISE(ABORT,'migration_outbox_immutable'); END;
CREATE TRIGGER trg_migration_plans_delete_prd131
BEFORE DELETE ON migration_plans
BEGIN SELECT RAISE(ABORT,'migration_plan_immutable'); END;
CREATE TRIGGER trg_migration_rollback_plans_delete_prd131
BEFORE DELETE ON migration_rollback_plans
BEGIN SELECT RAISE(ABORT,'migration_rollback_plan_immutable'); END;
CREATE TRIGGER trg_migration_runs_delete_prd131
BEFORE DELETE ON migration_runs
BEGIN SELECT RAISE(ABORT,'migration_run_immutable'); END;
CREATE TRIGGER trg_migration_snapshots_delete_prd131
BEFORE DELETE ON migration_assessment_snapshots
BEGIN SELECT RAISE(ABORT,'migration_snapshot_immutable'); END;
CREATE TRIGGER trg_migration_support_audits_delete_prd131
BEFORE DELETE ON migration_support_access_audits
BEGIN SELECT RAISE(ABORT,'migration_support_audit_immutable'); END;
CREATE TRIGGER trg_model_experiments_delete_prd075
BEFORE DELETE ON model_experiments FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'experiment_immutable'); END;
CREATE TRIGGER trg_model_feedback_statistics_delete_prd049
BEFORE DELETE ON model_feedback_statistics
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'model_feedback_statistics_immutable');
END;
CREATE TRIGGER trg_model_quality_score_events_prd048_immutable_delete
BEFORE DELETE ON model_quality_score_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_event_immutable');
END;
CREATE TRIGGER trg_model_quality_scores_prd048_delete
BEFORE DELETE ON model_quality_scores
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_delete_forbidden');
END;
CREATE TRIGGER trg_models_experiment_arm_delete_prd075
BEFORE DELETE ON models FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
  WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
    AND OLD.model_id IN (experiment.model_a_id,experiment.model_b_id))
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;
CREATE TRIGGER trg_notification_outbox_delete_prd105 BEFORE DELETE ON notification_outbox
BEGIN SELECT RAISE(ABORT,'notification_outbox_immutable'); END;
CREATE TRIGGER trg_notification_preferences_delete_prd105
BEFORE DELETE ON notification_preferences
BEGIN SELECT RAISE(ABORT,'notification_preference_immutable'); END;
CREATE TRIGGER trg_notifications_delete_prd105 BEFORE DELETE ON notifications
BEGIN SELECT RAISE(ABORT,'notification_immutable'); END;
CREATE TRIGGER trg_oauth_clients_delete_prd118
BEFORE DELETE ON oauth_clients
BEGIN SELECT RAISE(ABORT,'oauth_client_immutable'); END;
CREATE TRIGGER trg_onboarding_ack_delete_prd097
BEFORE DELETE ON onboarding_acknowledgements FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_ack_immutable'); END;
CREATE TRIGGER trg_onboarding_events_delete_prd097
BEFORE DELETE ON onboarding_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_event_immutable'); END;
CREATE TRIGGER trg_onboarding_progress_delete_prd097
BEFORE DELETE ON onboarding_progress FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_progress_delete_forbidden'); END;
CREATE TRIGGER trg_onboarding_step_facts_delete_prd097
BEFORE DELETE ON onboarding_step_facts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prd097.onboarding_step_delete_forbidden'); END;
CREATE TRIGGER trg_output_safety_events_delete_prd071
BEFORE DELETE ON output_safety_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_event_immutable'); END;
CREATE TRIGGER trg_output_safety_scans_delete_prd071
BEFORE DELETE ON output_safety_scans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'output_safety_scan_immutable'); END;
CREATE TRIGGER trg_owner_transfers_delete_prd094
BEFORE DELETE ON owner_transfers FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'owner_transfer_immutable'); END;
CREATE TRIGGER trg_partner_commission_cursor_delete_prd087
BEFORE DELETE ON partner_commission_cursors FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_commission_cursor_immutable'); END;
CREATE TRIGGER trg_partner_customer_outbox_delete_prd087
BEFORE DELETE ON partner_customer_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_customer_outbox_immutable'); END;
CREATE TRIGGER trg_partner_customers_delete_prd087 BEFORE DELETE ON partner_customers FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_customer_immutable'); END;
CREATE TRIGGER trg_partner_operations_delete_prd087 BEFORE DELETE ON partner_portal_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_operation_immutable'); END;
CREATE TRIGGER trg_partners_delete_prd087 BEFORE DELETE ON partners FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_immutable'); END;
CREATE TRIGGER trg_patch_release_delete_prd136 BEFORE DELETE ON patch_releases
BEGIN SELECT RAISE(ABORT,'patch_release_immutable'); END;
CREATE TRIGGER trg_payment_intents_delete_prd103
BEFORE DELETE ON payment_intents
BEGIN SELECT RAISE(ABORT,'payment_intent_immutable'); END;
CREATE TRIGGER trg_pen_test_finding_delete_prd137 BEFORE DELETE ON pen_test_findings
BEGIN SELECT RAISE(ABORT,'pen_test_finding_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_plans_delete_prd079 BEFORE DELETE ON plans FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'plan_immutable'); END;
CREATE TRIGGER trg_platform_action_intents_delete_prd096
BEFORE DELETE ON platform_action_intents FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'platform_action_intent_immutable'); END;
CREATE TRIGGER trg_platform_billing_adjustments_delete_prd099
BEFORE DELETE ON platform_billing_adjustments
BEGIN SELECT RAISE(ABORT,'platform_billing_adjustment_immutable'); END;
CREATE TRIGGER trg_platform_billing_summaries_delete_prd099
BEFORE DELETE ON platform_billing_summaries
BEGIN SELECT RAISE(ABORT,'platform_billing_summary_immutable'); END;
CREATE TRIGGER trg_platform_customer_account_delete_prd140
BEFORE DELETE ON platform_customer_account_projections
BEGIN SELECT RAISE(ABORT,'customer_account_projection_immutable'); END;
CREATE TRIGGER trg_platform_customer_command_delete_prd140
BEFORE DELETE ON platform_customer_success_commands
BEGIN SELECT RAISE(ABORT,'customer_success_command_immutable'); END;
CREATE TRIGGER trg_platform_customer_contact_delete_prd140
BEFORE DELETE ON platform_customer_contacts
BEGIN SELECT RAISE(ABORT,'customer_contact_immutable'); END;
CREATE TRIGGER trg_platform_customer_event_delete_prd140
BEFORE DELETE ON platform_customer_success_events
BEGIN SELECT RAISE(ABORT,'customer_success_event_immutable'); END;
CREATE TRIGGER trg_platform_customer_health_delete_prd140
BEFORE DELETE ON platform_customer_health_snapshots
BEGIN SELECT RAISE(ABORT,'customer_health_snapshot_immutable'); END;
CREATE TRIGGER trg_platform_customer_health_head_delete_prd140
BEFORE DELETE ON platform_customer_health_heads
BEGIN SELECT RAISE(ABORT,'customer_health_head_immutable'); END;
CREATE TRIGGER trg_platform_customer_mapping_delete_prd140
BEFORE DELETE ON platform_customer_success_legacy_mappings
BEGIN SELECT RAISE(ABORT,'customer_success_mapping_immutable'); END;
CREATE TRIGGER trg_platform_customer_outbox_delete_prd140
BEFORE DELETE ON platform_customer_success_event_outbox
BEGIN SELECT RAISE(ABORT,'customer_success_outbox_immutable'); END;
CREATE TRIGGER trg_platform_customer_plan_delete_prd140
BEFORE DELETE ON platform_customer_success_plans
BEGIN SELECT RAISE(ABORT,'customer_success_plan_immutable'); END;
CREATE TRIGGER trg_platform_customer_scheduler_delete_prd140
BEFORE DELETE ON platform_customer_success_scheduler_state
BEGIN SELECT RAISE(ABORT,'customer_success_scheduler_required'); END;
CREATE TRIGGER trg_platform_customer_task_delete_prd140
BEFORE DELETE ON platform_customer_tasks
BEGIN SELECT RAISE(ABORT,'customer_task_immutable'); END;
CREATE TRIGGER trg_platform_meter_readings_delete_prd099
BEFORE DELETE ON platform_meter_readings
BEGIN SELECT RAISE(ABORT,'platform_meter_reading_immutable'); END;
CREATE TRIGGER trg_platform_qbr_delete_prd140 BEFORE DELETE ON platform_qbr_summaries
BEGIN SELECT RAISE(ABORT,'qbr_summary_immutable'); END;
CREATE TRIGGER trg_platform_quota_allocations_delete_prd099
BEFORE DELETE ON platform_quota_allocations
BEGIN SELECT RAISE(ABORT,'platform_quota_allocation_immutable'); END;
CREATE TRIGGER trg_platform_renewal_delete_prd140
BEFORE DELETE ON platform_renewal_opportunities
BEGIN SELECT RAISE(ABORT,'renewal_opportunity_immutable'); END;
CREATE TRIGGER trg_policy_events_immutable_delete
BEFORE DELETE ON policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_event_immutable');
END;
CREATE TRIGGER trg_policy_explanations_immutable_delete
BEFORE DELETE ON policy_explanations
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_explanation_immutable');
END;
CREATE TRIGGER trg_policy_snapshot_events_immutable_delete
BEFORE DELETE ON policy_snapshot_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_event_immutable');
END;
CREATE TRIGGER trg_policy_snapshot_heads_delete
BEFORE DELETE ON policy_snapshot_heads
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_head_immutable');
END;
CREATE TRIGGER trg_policy_snapshot_items_delete_guard
BEFORE DELETE ON policy_snapshot_items
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM policy_snapshots snapshot
  WHERE snapshot.tenant_id = OLD.tenant_id
    AND snapshot.policy_snapshot_id = OLD.policy_snapshot_id
    AND snapshot.state = 'building'
)
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_item_immutable');
END;
CREATE TRIGGER trg_policy_snapshots_delete_guard
BEFORE DELETE ON policy_snapshots
FOR EACH ROW
WHEN
  OLD.state = 'published'
  OR EXISTS (
    SELECT 1 FROM policy_snapshot_heads h
    WHERE h.tenant_id = OLD.tenant_id AND h.current_snapshot_id = OLD.policy_snapshot_id
  )
  OR EXISTS (
    SELECT 1 FROM policy_snapshot_events event
    WHERE event.tenant_id = OLD.tenant_id
      AND event.policy_snapshot_id = OLD.policy_snapshot_id
  )
BEGIN
  SELECT RAISE(ABORT, 'policy_snapshot_immutable');
END;
CREATE TRIGGER trg_policy_template_application_items_delete_prd068
BEFORE DELETE ON policy_template_application_items FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_application_item_immutable'); END;
CREATE TRIGGER trg_policy_template_applications_delete_prd068
BEFORE DELETE ON policy_template_applications FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_application_immutable'); END;
CREATE TRIGGER trg_policy_template_dependencies_delete_prd068
BEFORE DELETE ON policy_template_application_dependencies FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_environment_delete_prd068
BEFORE DELETE ON environments FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='environment'
    AND dependency.resource_id=OLD.environment_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_events_delete_prd068
BEFORE DELETE ON policy_template_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_event_immutable'); END;
CREATE TRIGGER trg_policy_template_generated_action_delete_prd068
BEFORE DELETE ON policy_actions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;
CREATE TRIGGER trg_policy_template_generated_policy_delete_prd068
BEFORE DELETE ON policies FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;
CREATE TRIGGER trg_policy_template_generated_rule_delete_prd068
BEFORE DELETE ON policy_rules FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM policy_template_applications application
  WHERE application.tenant_id=OLD.tenant_id
    AND application.generated_policy_id=OLD.policy_id
)
BEGIN SELECT RAISE(ABORT,'policy_template_generated_policy_immutable'); END;
CREATE TRIGGER trg_policy_template_model_alias_delete_prd068
BEFORE DELETE ON model_aliases FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='model_alias'
    AND dependency.resource_id=OLD.model_alias_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_model_delete_prd068
BEFORE DELETE ON models FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='model'
    AND dependency.resource_id=OLD.model_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_project_delete_prd068
BEFORE DELETE ON projects FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id
    AND dependency.resource_type='project'
    AND dependency.resource_id=OLD.project_id
    AND application.status='applied'
)
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_provider_delete_prd068
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id AND dependency.resource_type='provider'
    AND dependency.resource_id=OLD.provider_id AND application.status='applied')
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_templates_delete_prd068
BEFORE DELETE ON policy_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'policy_template_immutable'); END;
CREATE TRIGGER trg_postmortems_delete_prd112 BEFORE DELETE ON postmortems
BEGIN SELECT RAISE(ABORT,'postmortem_immutable'); END;
CREATE TRIGGER trg_price_snapshots_immutable_delete_prd051
BEFORE DELETE ON price_snapshots
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'price_snapshot_append_only');
END;
CREATE TRIGGER trg_privacy_commands_delete_prd134 BEFORE DELETE ON privacy_action_commands
BEGIN SELECT RAISE(ABORT,'privacy_command_immutable'); END;
CREATE TRIGGER trg_privacy_events_delete_prd134 BEFORE DELETE ON privacy_events
BEGIN SELECT RAISE(ABORT,'privacy_event_immutable'); END;
CREATE TRIGGER trg_privacy_outbox_delete_prd134 BEFORE DELETE ON privacy_event_outbox
BEGIN SELECT RAISE(ABORT,'privacy_outbox_immutable'); END;
CREATE TRIGGER trg_privacy_owner_jobs_delete_prd134
BEFORE DELETE ON data_subject_request_owner_jobs
BEGIN SELECT RAISE(ABORT,'privacy_owner_job_immutable'); END;
CREATE TRIGGER trg_privacy_preferences_delete_prd134 BEFORE DELETE ON privacy_preferences
BEGIN SELECT RAISE(ABORT,'privacy_preference_immutable'); END;
CREATE TRIGGER trg_private_dr_drills_delete_prd129
BEFORE DELETE ON private_dr_drills
BEGIN SELECT RAISE(ABORT,'edge_dr_drill_immutable'); END;
CREATE TRIGGER trg_product_event_outbox_delete_prd085
BEFORE DELETE ON product_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'product_event_outbox_immutable'); END;
CREATE TRIGGER trg_product_events_delete_prd085
BEFORE DELETE ON product_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'product_event_immutable'); END;
CREATE TRIGGER trg_prompt_template_events_delete_prd067
BEFORE DELETE ON prompt_template_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_event_immutable'); END;
CREATE TRIGGER trg_prompt_template_operations_delete_prd067
BEFORE DELETE ON prompt_template_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_operation_immutable'); END;
CREATE TRIGGER trg_prompt_template_version_models_delete_prd067
BEFORE DELETE ON prompt_template_version_models FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM prompt_template_versions version
  WHERE version.tenant_id=OLD.tenant_id
    AND version.prompt_template_id=OLD.prompt_template_id
    AND version.prompt_template_version_id=OLD.prompt_template_version_id
    AND version.state='published'
)
BEGIN SELECT RAISE(ABORT,'prompt_template_binding_immutable'); END;
CREATE TRIGGER trg_prompt_template_version_projects_delete_prd067
BEFORE DELETE ON prompt_template_version_projects FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM prompt_template_versions version
  WHERE version.tenant_id=OLD.tenant_id
    AND version.prompt_template_id=OLD.prompt_template_id
    AND version.prompt_template_version_id=OLD.prompt_template_version_id
    AND version.state='published'
)
BEGIN SELECT RAISE(ABORT,'prompt_template_binding_immutable'); END;
CREATE TRIGGER trg_prompt_template_versions_delete_prd067
BEFORE DELETE ON prompt_template_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_version_immutable'); END;
CREATE TRIGGER trg_prompt_templates_delete_prd067
BEFORE DELETE ON prompt_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'prompt_template_immutable'); END;
CREATE TRIGGER trg_provider_adapter_activations_immutable_delete_prd063
BEFORE DELETE ON provider_adapter_catalog_activations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_immutable'); END;
CREATE TRIGGER trg_provider_catalog_events_delete_prd119
BEFORE DELETE ON provider_catalog_events
BEGIN SELECT RAISE(ABORT,'provider_catalog_event_immutable'); END;
CREATE TRIGGER trg_provider_catalog_instances_delete_prd119
BEFORE DELETE ON provider_catalog_instances
BEGIN SELECT RAISE(ABORT,'provider_catalog_instance_immutable'); END;
CREATE TRIGGER trg_provider_error_facts_delete_prd056
BEFORE DELETE ON provider_error_facts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_fact_immutable'); END;
CREATE TRIGGER trg_provider_error_map_events_delete_prd056
BEFORE DELETE ON provider_error_map_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_event_immutable'); END;
CREATE TRIGGER trg_provider_error_map_head_versions_delete_prd056
BEFORE DELETE ON provider_error_map_head_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_version_immutable'); END;
CREATE TRIGGER trg_provider_error_map_heads_delete_prd056
BEFORE DELETE ON provider_error_map_heads FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_immutable'); END;
CREATE TRIGGER trg_provider_error_map_operations_delete_prd056
BEFORE DELETE ON provider_error_map_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_operation_immutable'); END;
CREATE TRIGGER trg_provider_error_map_versions_delete_prd056
BEFORE DELETE ON provider_error_map_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_version_immutable'); END;
CREATE TRIGGER trg_provider_error_maps_delete_prd056
BEFORE DELETE ON provider_error_maps FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_immutable'); END;
CREATE TRIGGER trg_provider_key_anomaly_delete_prd081
BEFORE DELETE ON provider_key_anomaly_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_key_anomaly_immutable'); END;
CREATE TRIGGER trg_provider_preflight_runs_delete_prd119
BEFORE DELETE ON provider_preflight_runs
BEGIN SELECT RAISE(ABORT,'provider_preflight_run_immutable'); END;
CREATE TRIGGER trg_providers_experiment_arm_delete_prd075
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
  WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
    AND OLD.provider_id IN(experiment.model_a_provider_id,experiment.model_b_provider_id))
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;
CREATE TRIGGER trg_quota_adjustments_delete_prd079 BEFORE DELETE ON quota_adjustments FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_adjustment_immutable'); END;
CREATE TRIGGER trg_quota_allocations_delete_prd079
BEFORE DELETE ON quota_usage_allocations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_allocation_immutable'); END;
CREATE TRIGGER trg_quota_buckets_delete_prd079 BEFORE DELETE ON quota_buckets FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_bucket_immutable'); END;
CREATE TRIGGER trg_quota_events_delete_prd079 BEFORE DELETE ON quota_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_event_immutable'); END;
CREATE TRIGGER trg_quota_outbox_delete_prd079 BEFORE DELETE ON quota_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_outbox_immutable'); END;
CREATE TRIGGER trg_quota_reservation_items_delete_prd079
BEFORE DELETE ON quota_reservation_items FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_reservation_item_immutable'); END;
CREATE TRIGGER trg_quota_reservations_delete_prd079 BEFORE DELETE ON quota_reservations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_reservation_immutable'); END;
CREATE TRIGGER trg_quota_settlements_delete_prd079
BEFORE DELETE ON quota_usage_settlements FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'quota_settlement_immutable'); END;
CREATE TRIGGER trg_reconciliation_differences_delete_prd104
BEFORE DELETE ON reconciliation_differences
BEGIN SELECT RAISE(ABORT,'reconciliation_difference_immutable'); END;
CREATE TRIGGER trg_recovery_points_delete_prd111 BEFORE DELETE ON recovery_points
BEGIN SELECT RAISE(ABORT,'recovery_point_immutable'); END;
CREATE TRIGGER trg_refunds_delete_prd103
BEFORE DELETE ON refunds
BEGIN SELECT RAISE(ABORT,'refund_immutable'); END;
CREATE TRIGGER trg_request_explanations_immutable_delete_prd054
BEFORE DELETE ON request_explanations
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_immutable');
END;
CREATE TRIGGER trg_request_feedback_event_immutable_delete_prd049
BEFORE DELETE ON request_feedback_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_event_immutable');
END;
CREATE TRIGGER trg_request_feedback_immutable_delete_prd049
BEFORE DELETE ON request_feedbacks
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_immutable');
END;
CREATE TRIGGER trg_request_log_feedback_parent_delete_prd049
BEFORE DELETE ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_feedbacks feedback
  WHERE feedback.tenant_id=OLD.tenant_id AND feedback.source_request_id=OLD.request_id
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;
CREATE TRIGGER trg_request_log_query_events_immutable_delete_prd055
BEFORE DELETE ON request_log_query_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;
CREATE TRIGGER trg_request_webhook_links_immutable_delete_prd054
BEFORE DELETE ON request_webhook_delivery_links
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_webhook_link_immutable');
END;
CREATE TRIGGER trg_residency_evidence_delete_prd117 BEFORE DELETE ON residency_evidence
BEGIN SELECT RAISE(ABORT,'residency_evidence_immutable'); END;
CREATE TRIGGER trg_residency_policies_delete_prd117 BEFORE DELETE ON residency_policies
BEGIN SELECT RAISE(ABORT,'residency_policy_immutable'); END;
CREATE TRIGGER trg_response_cache_entry_tags_delete_prd076
BEFORE DELETE ON response_cache_entry_tags FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM response_cache_entries entry
  WHERE entry.tenant_id=OLD.tenant_id
    AND entry.response_cache_entry_id=OLD.response_cache_entry_id
)
BEGIN SELECT RAISE(ABORT,'response_cache_entry_tag_immutable'); END;
CREATE TRIGGER trg_response_cache_event_outbox_delete_prd076
BEFORE DELETE ON response_cache_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_event_outbox_immutable'); END;
CREATE TRIGGER trg_response_cache_events_delete_prd076
BEFORE DELETE ON response_cache_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_event_immutable'); END;
CREATE TRIGGER trg_response_cache_fill_tags_delete_prd076
BEFORE DELETE ON response_cache_fill_tags FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM response_cache_fill_leases lease
  WHERE lease.tenant_id=OLD.tenant_id
    AND lease.response_cache_fill_lease_id=OLD.response_cache_fill_lease_id
)
BEGIN SELECT RAISE(ABORT,'response_cache_fill_tag_immutable'); END;
CREATE TRIGGER trg_response_cache_hits_delete_prd076
BEFORE DELETE ON response_cache_hits FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_hit_immutable'); END;
CREATE TRIGGER trg_response_cache_invalidations_delete_prd076
BEFORE DELETE ON response_cache_invalidations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_invalidation_immutable'); END;
CREATE TRIGGER trg_response_cache_key_generations_delete_prd076
BEFORE DELETE ON response_cache_key_generations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_key_generation_immutable'); END;
CREATE TRIGGER trg_response_cache_policies_delete_prd076
BEFORE DELETE ON response_cache_policies FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_policy_immutable'); END;
CREATE TRIGGER trg_response_cache_policy_events_delete_prd076
BEFORE DELETE ON response_cache_policy_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_policy_event_immutable'); END;
CREATE TRIGGER trg_response_cache_request_facts_delete_prd076
BEFORE DELETE ON response_cache_request_facts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_request_fact_immutable'); END;
CREATE TRIGGER trg_response_cache_tag_generations_delete_prd076
BEFORE DELETE ON response_cache_tag_generations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'response_cache_tag_generation_immutable'); END;
CREATE TRIGGER trg_restore_evidence_delete_prd111 BEFORE DELETE ON restore_evidence
BEGIN SELECT RAISE(ABORT,'restore_evidence_immutable'); END;
CREATE TRIGGER trg_retention_exceptions_delete_prd134 BEFORE DELETE ON retention_exceptions
BEGIN SELECT RAISE(ABORT,'retention_exception_immutable'); END;
CREATE TRIGGER trg_retention_policies_delete_prd117 BEFORE DELETE ON retention_policies
BEGIN SELECT RAISE(ABORT,'retention_policy_immutable'); END;
CREATE TRIGGER trg_risk_acceptance_delete_prd137 BEFORE DELETE ON security_risk_acceptances
BEGIN SELECT RAISE(ABORT,'security_risk_acceptance_immutable'); END;
CREATE TRIGGER trg_risk_appeals_delete_prd110 BEFORE DELETE ON risk_appeals
BEGIN SELECT RAISE(ABORT,'risk_appeal_immutable'); END;
CREATE TRIGGER trg_risk_cases_delete_prd110 BEFORE DELETE ON risk_cases
BEGIN SELECT RAISE(ABORT,'risk_case_immutable'); END;
CREATE TRIGGER trg_risk_challenges_delete_prd110 BEFORE DELETE ON risk_challenges
BEGIN SELECT RAISE(ABORT,'risk_challenge_immutable'); END;
CREATE TRIGGER trg_risk_decisions_delete_prd110 BEFORE DELETE ON risk_decisions
BEGIN SELECT RAISE(ABORT,'risk_decision_immutable'); END;
CREATE TRIGGER trg_risk_event_outbox_delete_prd110 BEFORE DELETE ON risk_event_outbox
BEGIN SELECT RAISE(ABORT,'risk_event_immutable'); END;
CREATE TRIGGER trg_risk_restrictions_delete_prd110 BEFORE DELETE ON risk_restrictions
BEGIN SELECT RAISE(ABORT,'risk_restriction_immutable'); END;
CREATE TRIGGER trg_risk_signals_delete_prd110 BEFORE DELETE ON risk_signals
BEGIN SELECT RAISE(ABORT,'risk_signal_immutable'); END;
CREATE TRIGGER trg_role_dashboard_configs_delete_prd065
BEFORE DELETE ON role_dashboard_configs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'role_dashboard_config_immutable'); END;
CREATE TRIGGER trg_role_permission_events_delete_prd059
BEFORE DELETE ON role_permission_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_event_immutable'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_roles_delete_prd059
BEFORE DELETE ON roles FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_routing_attempt_feedback_parent_delete_prd049
BEFORE DELETE ON routing_attempts
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM request_feedbacks feedback
  JOIN request_logs request
    ON request.tenant_id=feedback.tenant_id
   AND request.request_id=feedback.source_request_id
  WHERE request.final_routing_attempt_id=OLD.routing_attempt_id
    AND OLD.tenant_id=request.tenant_id
    AND OLD.request_id=request.request_id
)
BEGIN
  SELECT RAISE(ABORT, 'routing_attempt_feedback_parent_immutable');
END;
CREATE TRIGGER trg_routing_attempt_provider_error_fact_delete_prd056
BEFORE DELETE ON routing_attempts FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts fact
  WHERE fact.tenant_id=OLD.tenant_id AND fact.routing_attempt_id=OLD.routing_attempt_id
)
BEGIN SELECT RAISE(ABORT,'routing_attempt_provider_error_fact_immutable'); END;
CREATE TRIGGER trg_routing_candidates_prd048_immutable_delete
BEFORE DELETE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END;
CREATE TRIGGER trg_routing_decisions_experiment_delete_prd075
BEFORE DELETE ON routing_decisions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM experiment_assignments assignment
  WHERE assignment.tenant_id=OLD.tenant_id
    AND assignment.routing_decision_id=OLD.routing_decision_id)
BEGIN SELECT RAISE(ABORT,'experiment_routing_decision_immutable'); END;
CREATE TRIGGER trg_routing_policies_prd048_delete
BEFORE DELETE ON model_alias_routing_policies
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM model_aliases a
  WHERE a.tenant_id=OLD.tenant_id AND a.model_alias_id=OLD.model_alias_id
)
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_alias_bound');
END;
CREATE TRIGGER trg_routing_policy_events_prd048_immutable_delete
BEFORE DELETE ON routing_policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_immutable');
END;
CREATE TRIGGER trg_saas_subscription_events_delete_prd098
BEFORE DELETE ON saas_subscription_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'saas_subscription_event_immutable'); END;
CREATE TRIGGER trg_saved_filters_immutable_delete_prd055
BEFORE DELETE ON saved_filters FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'saved_filter_immutable'); END;
CREATE TRIGGER trg_scim_events_delete_prd115 BEFORE DELETE ON scim_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'scim_event_immutable'); END;
CREATE TRIGGER trg_security_assessment_delete_prd137 BEFORE DELETE ON security_assessments
BEGIN SELECT RAISE(ABORT,'security_assessment_immutable'); END;
CREATE TRIGGER trg_security_event_aggregates_delete_prd081
BEFORE DELETE ON security_event_aggregates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_aggregate_immutable'); END;
CREATE TRIGGER trg_security_event_members_delete_prd081
BEFORE DELETE ON security_event_aggregate_members FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_member_immutable'); END;
CREATE TRIGGER trg_security_event_outbox_delete_prd081
BEFORE DELETE ON security_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_outbox_immutable'); END;
CREATE TRIGGER trg_security_event_sources_delete_prd081
BEFORE DELETE ON security_event_sources FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_source_immutable'); END;
CREATE TRIGGER trg_security_remediation_delete_prd137 BEFORE DELETE ON security_remediations
BEGIN SELECT RAISE(ABORT,'security_remediation_immutable'); END;
CREATE TRIGGER trg_service_accounts_delete_prd118
BEFORE DELETE ON service_accounts
BEGIN SELECT RAISE(ABORT,'service_account_immutable'); END;
CREATE TRIGGER trg_session_risk_actions_delete_prd070
BEFORE DELETE ON session_risk_actions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_action_immutable'); END;
CREATE TRIGGER trg_session_risk_events_delete_prd070
BEFORE DELETE ON session_risk_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_event_immutable'); END;
CREATE TRIGGER trg_session_risk_freezes_delete_prd070
BEFORE DELETE ON session_risk_freezes FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_freeze_immutable'); END;
CREATE TRIGGER trg_session_risk_observations_delete_prd070
BEFORE DELETE ON session_risk_observations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_observation_immutable'); END;
CREATE TRIGGER trg_session_risk_scores_delete_prd070
BEFORE DELETE ON session_risk_scores FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'session_risk_score_immutable'); END;
CREATE TRIGGER trg_sso_certificates_delete_prd114 BEFORE DELETE ON sso_certificates
BEGIN SELECT RAISE(ABORT,'sso_certificate_immutable'); END;
CREATE TRIGGER trg_sso_connections_delete_prd114 BEFORE DELETE ON sso_connections
BEGIN SELECT RAISE(ABORT,'sso_connection_immutable'); END;
CREATE TRIGGER trg_sso_login_states_delete_prd114 BEFORE DELETE ON sso_login_states
BEGIN SELECT RAISE(ABORT,'sso_login_state_immutable'); END;
CREATE TRIGGER trg_sso_policies_delete_prd114 BEFORE DELETE ON sso_policies
BEGIN SELECT RAISE(ABORT,'sso_policy_immutable'); END;
CREATE TRIGGER trg_sso_replay_guards_delete_prd114 BEFORE DELETE ON sso_replay_guards
BEGIN SELECT RAISE(ABORT,'sso_replay_guard_immutable'); END;
CREATE TRIGGER trg_status_events_delete_prd112 BEFORE DELETE ON status_event_outbox
BEGIN SELECT RAISE(ABORT,'status_event_immutable'); END;
CREATE TRIGGER trg_status_subscriptions_delete_prd112 BEFORE DELETE ON status_subscriptions
BEGIN SELECT RAISE(ABORT,'status_subscription_immutable'); END;
CREATE TRIGGER trg_stream_session_provider_error_fact_delete_prd056
BEFORE DELETE ON stream_sessions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts fact
  WHERE fact.tenant_id=OLD.tenant_id AND fact.stream_session_id=OLD.stream_session_id
)
BEGIN SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable'); END;
CREATE TRIGGER trg_subscription_orders_delete_prd103
BEFORE DELETE ON subscription_orders
BEGIN SELECT RAISE(ABORT,'subscription_order_immutable'); END;
CREATE TRIGGER trg_supply_chain_exception_delete_prd136
BEFORE DELETE ON supply_chain_security_exceptions
BEGIN SELECT RAISE(ABORT,'supply_chain_exception_immutable'); END;
CREATE TRIGGER trg_support_actions_delete_prd132 BEFORE DELETE ON support_action_commands
BEGIN SELECT RAISE(ABORT,'support_action_immutable'); END;
CREATE TRIGGER trg_support_attachment_jobs_delete_prd132 BEFORE DELETE ON support_attachment_scan_jobs
BEGIN SELECT RAISE(ABORT,'support_attachment_job_immutable'); END;
CREATE TRIGGER trg_support_attachments_delete_prd132 BEFORE DELETE ON support_ticket_attachments
BEGIN SELECT RAISE(ABORT,'support_attachment_immutable'); END;
CREATE TRIGGER trg_support_clock_events_delete_prd132 BEFORE DELETE ON support_sla_clock_events
BEGIN SELECT RAISE(ABORT,'support_sla_clock_event_immutable'); END;
CREATE TRIGGER trg_support_escalations_delete_prd132 BEFORE DELETE ON support_escalations
BEGIN SELECT RAISE(ABORT,'support_escalation_immutable'); END;
CREATE TRIGGER trg_support_events_delete_prd132 BEFORE DELETE ON support_ticket_events
BEGIN SELECT RAISE(ABORT,'support_ticket_event_immutable'); END;
CREATE TRIGGER trg_support_messages_delete_prd132 BEFORE DELETE ON support_ticket_messages
BEGIN SELECT RAISE(ABORT,'support_message_immutable'); END;
CREATE TRIGGER trg_support_outbox_delete_prd132 BEFORE DELETE ON support_event_outbox
BEGIN SELECT RAISE(ABORT,'support_outbox_immutable'); END;
CREATE TRIGGER trg_support_sla_clocks_delete_prd132 BEFORE DELETE ON support_sla_clocks
BEGIN SELECT RAISE(ABORT,'support_sla_clock_immutable'); END;
CREATE TRIGGER trg_support_surveys_delete_prd132 BEFORE DELETE ON support_satisfaction_surveys
BEGIN SELECT RAISE(ABORT,'support_survey_immutable'); END;
CREATE TRIGGER trg_support_tickets_delete_prd132 BEFORE DELETE ON support_tickets
BEGIN SELECT RAISE(ABORT,'support_ticket_immutable'); END;
CREATE TRIGGER trg_telemetry_policies_delete_prd109 BEFORE DELETE ON telemetry_policies
BEGIN SELECT RAISE(ABORT,'telemetry_policy_immutable'); END;
CREATE TRIGGER trg_temporary_rate_limit_overrides_immutable_delete_prd053
BEFORE DELETE ON temporary_rate_limit_overrides
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'temporary_rate_limit_override_immutable');
END;
CREATE TRIGGER trg_tenant_data_states_delete_prd089 BEFORE DELETE ON tenant_data_states FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_data_state_immutable'); END;
CREATE TRIGGER trg_tenant_export_jobs_delete_prd089 BEFORE DELETE ON tenant_export_jobs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_export_immutable'); END;
CREATE TRIGGER trg_tenant_lifecycle_events_delete_prd089 BEFORE DELETE ON tenant_lifecycle_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_lifecycle_event_immutable'); END;
CREATE TRIGGER trg_tenant_lifecycle_outbox_delete_prd089 BEFORE DELETE ON tenant_lifecycle_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_lifecycle_outbox_immutable'); END;
CREATE TRIGGER trg_tenant_retention_archives_delete_prd089 BEFORE DELETE ON tenant_retention_archives FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_retention_immutable'); END;
CREATE TRIGGER trg_uploaded_files_delete_prd072
BEFORE DELETE ON uploaded_files FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'file_immutable'); END;
CREATE TRIGGER trg_usage_export_events_immutable_delete
BEFORE DELETE ON usage_export_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_export_event_immutable');
END;
CREATE TRIGGER trg_usage_export_ledger_sequences_immutable_delete
BEFORE DELETE ON usage_export_ledger_sequences
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_export_ledger_sequence_immutable');
END;
CREATE TRIGGER trg_usage_ledger_export_append_only_delete
BEFORE DELETE ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;
CREATE TRIGGER trg_verification_evidence_delete_prd102
BEFORE DELETE ON verification_evidence
BEGIN SELECT RAISE(ABORT,'verification_evidence_immutable'); END;
CREATE TRIGGER trg_verified_domains_delete_prd114 BEFORE DELETE ON verified_domains
BEGIN SELECT RAISE(ABORT,'verified_domain_immutable'); END;
CREATE TRIGGER trg_virtual_keys_scope_delete_prd075
BEFORE DELETE ON virtual_keys FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM gateway_request_scope_claims scope
  WHERE scope.tenant_id=OLD.tenant_id AND scope.virtual_key_id=OLD.virtual_key_id)
BEGIN SELECT RAISE(ABORT,'experiment_scope_parent_immutable'); END;
CREATE TRIGGER trg_webhook_dead_letters_delete_prd057
BEFORE DELETE ON webhook_dead_letter_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_immutable'); END;
CREATE TRIGGER trg_webhook_security_rate_claim_delete_prd081
BEFORE DELETE ON webhook_security_rate_claims FOR EACH ROW
WHEN unixepoch(OLD.expires_at)>unixepoch('now') OR EXISTS(
  SELECT 1 FROM webhook_deliveries delivery
  WHERE delivery.tenant_id=OLD.tenant_id
    AND delivery.webhook_delivery_id=OLD.webhook_delivery_id
    AND delivery.status='pending'
)
BEGIN SELECT RAISE(ABORT,'webhook_security_rate_claim_immutable'); END;
CREATE TRIGGER trg_workspace_advanced_tool_delete_prd146
BEFORE DELETE ON workspace_advanced_tool_entitlement_versions
BEGIN SELECT RAISE(ABORT,'workspace_advanced_tool_immutable'); END;
CREATE TRIGGER trg_workspace_assistant_delete_prd145
BEFORE DELETE ON workspace_assistant_query_projections
BEGIN SELECT RAISE(ABORT,'workspace_assistant_projection_immutable'); END;
CREATE TRIGGER trg_workspace_billing_profile_delete_prd102
BEFORE DELETE ON workspace_billing_profiles
BEGIN SELECT RAISE(ABORT,'workspace_billing_profile_immutable'); END;
CREATE TRIGGER trg_workspace_console_event_delete_prd145
BEFORE DELETE ON workspace_console_events
BEGIN SELECT RAISE(ABORT,'workspace_console_event_immutable'); END;
CREATE TRIGGER trg_workspace_creation_requests_delete_prd093
BEFORE DELETE ON workspace_creation_requests FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_creation_request_immutable'); END;
CREATE TRIGGER trg_workspace_dashboard_source_delete_prd145
BEFORE DELETE ON workspace_dashboard_projection_sources
BEGIN SELECT RAISE(ABORT,'workspace_dashboard_source_immutable'); END;
CREATE TRIGGER trg_workspace_dashboard_version_delete_prd145
BEFORE DELETE ON workspace_dashboard_projection_versions
BEGIN SELECT RAISE(ABORT,'workspace_dashboard_version_immutable'); END;
CREATE TRIGGER trg_workspace_edge_commands_delete_prd120
BEFORE DELETE ON workspace_edge_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_command_immutable'); END;
CREATE TRIGGER trg_workspace_edge_state_commands_delete_prd121
BEFORE DELETE ON workspace_edge_state_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_state_command_immutable'); END;
CREATE TRIGGER trg_workspace_edge_telemetry_commands_delete_prd122
BEFORE DELETE ON workspace_edge_telemetry_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_command_immutable'); END;
CREATE TRIGGER trg_workspace_environment_origins_delete_prd093
BEFORE DELETE ON workspace_environment_origins FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_environment_origin_immutable'); END;
CREATE TRIGGER trg_workspace_events_delete_prd093
BEFORE DELETE ON workspace_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_event_immutable'); END;
CREATE TRIGGER trg_workspace_health_delete_prd096
BEFORE DELETE ON workspace_health_projections FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_health_projection_immutable'); END;
CREATE TRIGGER trg_workspace_membership_conflicts_delete_prd094
BEFORE DELETE ON workspace_membership_migration_conflicts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_conflict_immutable'); END;
CREATE TRIGGER trg_workspace_memberships_delete_prd094
BEFORE DELETE ON workspace_memberships FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_contract_delete_prd146
BEFORE DELETE ON workspace_navigation_contract_snapshots
BEGIN SELECT RAISE(ABORT,'workspace_navigation_contract_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_event_delete_prd146
BEFORE DELETE ON workspace_navigation_events
BEGIN SELECT RAISE(ABORT,'workspace_navigation_event_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_item_delete_prd146
BEFORE DELETE ON workspace_navigation_contract_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_item_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_projection_delete_prd146
BEFORE DELETE ON workspace_navigation_projection_versions
BEGIN SELECT RAISE(ABORT,'workspace_navigation_projection_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_projection_item_delete_prd146
BEFORE DELETE ON workspace_navigation_projection_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_projection_item_immutable'); END;
CREATE TRIGGER trg_workspace_notification_delete_prd096
BEFORE DELETE ON workspace_owner_notification_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_notification_immutable'); END;
CREATE TRIGGER trg_workspace_operational_delete_prd096
BEFORE DELETE ON workspace_operational_states FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_operational_immutable'); END;
CREATE TRIGGER trg_workspace_operational_events_delete_prd096
BEFORE DELETE ON workspace_operational_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_operational_event_immutable'); END;
CREATE TRIGGER trg_workspace_projection_claims_delete_prd093
BEFORE DELETE ON workspace_user_projection_claims FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_projection_claim_immutable'); END;
CREATE TRIGGER trg_workspace_provider_catalog_commands_delete_prd119
BEFORE DELETE ON workspace_provider_catalog_commands
BEGIN SELECT RAISE(ABORT,'provider_catalog_command_immutable'); END;
CREATE TRIGGER trg_workspace_provider_override_version_delete_prd143
BEFORE DELETE ON workspace_provider_error_override_versions
BEGIN SELECT RAISE(ABORT,'provider_override_version_immutable'); END;
CREATE TRIGGER trg_workspace_regions_delete_prd093
BEFORE DELETE ON workspace_regions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_region_immutable'); END;
CREATE TRIGGER trg_workspace_risk_tags_delete_prd096
BEFORE DELETE ON workspace_risk_tags FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_risk_immutable'); END;
CREATE TRIGGER trg_workspace_role_task_delete_prd146
BEFORE DELETE ON workspace_role_task_home_tasks
BEGIN SELECT RAISE(ABORT,'workspace_role_task_immutable'); END;
CREATE TRIGGER trg_workspace_role_task_version_delete_prd146
BEFORE DELETE ON workspace_role_task_home_versions
BEGIN SELECT RAISE(ABORT,'workspace_role_task_version_immutable'); END;
CREATE TRIGGER trg_workspace_tenant_bindings_delete_prd093
BEFORE DELETE ON workspace_tenant_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_tenant_binding_immutable'); END;
CREATE TRIGGER trg_workspace_tenant_creation_claims_delete_prd093
BEFORE DELETE ON workspace_tenant_creation_claims FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_tenant_creation_claim_immutable'); END;
DROP TABLE retired_personal_workspaces;
CREATE TEMP TABLE prd190_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd190_guard SELECT (SELECT count(*) FROM workspaces WHERE kind!='organization') + (SELECT count(*) FROM sqlite_schema WHERE name='personal_profiles') + (SELECT count(*) FROM pragma_foreign_key_check);
DROP TABLE prd190_guard;
COMMIT;
PRAGMA foreign_keys=ON;
PRAGMA legacy_alter_table=OFF;
