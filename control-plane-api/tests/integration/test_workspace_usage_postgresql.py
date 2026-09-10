"""Native PostgreSQL proves Workspace usage and export SQL, not SQLite emulation."""

import csv
import io

import g6_postgresql_support as native_support
import test_postgresql_native_migrations as native_migrations
from metahub_control.audit import AuditRequestMeta
from metahub_control.consumers.models import CreateConsumerRequest
from metahub_control.consumers.service import save_consumer
from metahub_control.identity.self_service import register_local_account
from metahub_control.usage.export_service import (
    claim_next_usage_export,
    download_usage_export,
    get_usage_export_status,
    process_claimed_usage_export,
    request_usage_export,
)
from metahub_control.usage.service import get_usage_summary, list_usage_records
from metahub_control.virtual_keys.models import CreateVirtualKeyRequest
from metahub_control.virtual_keys.service import create_virtual_key
from metahub_control.workspace_access.service import (
    authenticate_workspace_session_as_auth_context,
    switch_workspace,
)
from metahub_control.workspaces.service import create_workspace
from metahub_shared.runtime.config import load_app_config
from test_workspace_usage import _usage

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


def test_native_workspace_usage_consumer_currencies_and_async_export(native_business_database):
    config = load_app_config("control-plane-api").model_copy(
        update={
            "database_url": native_business_database,
            "environment": "dev",
            "identity_session_secret": "usage-native-global-session-secret-2026",
            "identity_challenge_secret": "usage-native-challenge-secret-2026",
            "workspace_session_secret": "usage-native-workspace-session-secret-2026",
            "identity_challenge_delivery_mode": "disabled",
            "usage_export_sync_row_limit": 1,
            "usage_export_enabled": True,
        }
    )
    issued = register_local_account(
        config,
        email="usage-native-owner@example.test",
        display_name="Owner",
        password="NativeUsageOwner!2026",
        consent_version="privacy-local-2026-09-09",
        invitation_token=None,
        device_token="native-usage-device",
        client_host="127.0.0.1",
        user_agent="native-test",
        request_id="usage-native-register",
        trace_id="usage-native",
    )
    workspace = create_workspace(
        config,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
        idempotency_key="usage-native-organization",
        kind="organization",
        name="Native Usage",
        slug="native-usage",
        region="cn-north-1",
        service_mode="internal_only",
        request_id="usage-native-create",
        trace_id="usage-native",
    ).workspace
    _, token, csrf = switch_workspace(
        config,
        global_session_token=issued.session_token,
        global_csrf_token=issued.csrf_token,
        workspace_id=workspace.workspace_id,
        request_id="usage-native-switch",
        trace_id="usage-native",
    )
    context = authenticate_workspace_session_as_auth_context(config, token, csrf_token=csrf)
    audit = AuditRequestMeta(client_ip=None, user_agent=None)
    employee = save_consumer(
        config,
        context,
        CreateConsumerRequest(display_name="Native Usage Employee", department="Finance"),
        audit_request_meta=audit,
        request_id="usage-native-consumer",
        trace_id="usage-native",
    ).consumer
    key = create_virtual_key(
        config,
        context,
        CreateVirtualKeyRequest(name="Native Usage Tool", consumer_id=employee.consumer_id),
        audit_request_meta=audit,
        request_id="usage-native-key",
        trace_id="usage-native",
    )
    _usage(native_business_database, key.model_dump())
    _usage(native_business_database, key.model_dump(), currency="USD", estimated=True)
    filters = dict(
        start_date=None,
        end_date=None,
        model_id=None,
        virtual_key_id=None,
        consumer_id=employee.consumer_id,
        environment_id=None,
        provider_id=None,
        project_id=None,
        application_id=None,
        estimated=None,
        request_id_filter=None,
        trace_id_filter=None,
    )
    summary = get_usage_summary(
        config, context, **filters, request_id="native-summary", trace_id="usage-native"
    )
    assert summary.totals.request_count == 2
    assert summary.totals.cost_cny == summary.totals.cost_usd == 0.25
    assert summary.by_consumer[0].consumer_name == "Native Usage Employee"
    assert summary.by_consumer[0].estimated_records == 1
    records = list_usage_records(
        config,
        context,
        **filters,
        limit=20,
        offset=0,
        request_id="native-records",
        trace_id="usage-native",
    )
    assert records.total == 2
    assert all(record.consumer_id == employee.consumer_id for record in records.usage_records)
    requested = request_usage_export(
        config,
        context,
        **filters,
        request_id="native-export",
        trace_id="usage-native",
        audit_request_meta=audit,
    )
    assert requested.content is None
    claim = claim_next_usage_export(config)
    assert claim and process_claimed_usage_export(config, claim)
    status = get_usage_export_status(
        config,
        context,
        claim.usage_export_job_id,
        request_id="native-status",
        trace_id="usage-native",
    )
    assert status.export_job.status == "completed"
    assert status.export_job.filters["consumer_id"] == employee.consumer_id
    downloaded = download_usage_export(
        config,
        context,
        claim.usage_export_job_id,
        request_id="native-download",
        trace_id="usage-native",
        audit_request_meta=audit,
    )
    assert len(list(csv.DictReader(io.StringIO(downloaded.content.decode("utf-8-sig"))))) == 2
