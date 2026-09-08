from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from metahub_control.auth.models import AuthSession, AuthTenant, AuthUser
from metahub_control.auth.service import AuthContext
from metahub_control.console import service as console_service
from metahub_control.dlp import service as dlp_service
from metahub_control.environments import service as environment_service
from metahub_control.model_aliases import service as model_alias_service
from metahub_control.model_prices import service as model_price_service
from metahub_control.models import service as model_service
from metahub_control.projects import service as project_service
from metahub_control.provider_health import service as provider_health_service
from metahub_control.providers import service as provider_service
from metahub_control.tenants import service as tenant_service
from metahub_control.users import service as user_service
from metahub_control.virtual_keys import service as virtual_key_service
from metahub_control.webhooks import service as webhook_service


class _QueryFailure(RuntimeError):
    pass


class _FailingConnection:
    def __init__(self) -> None:
        self.closed = False
        self.in_transaction = False

    def execute(self, *_args, **_kwargs):
        raise _QueryFailure("query failed")

    def close(self) -> None:
        self.closed = True


def _context() -> AuthContext:
    return AuthContext(
        user=AuthUser(
            user_id="usr_connection_lifecycle",
            tenant_id="tenant_connection_lifecycle",
            email="admin@example.invalid",
            display_name="Admin",
            role="admin",
            permission_codes=("console.basic.read",),
        ),
        tenant=AuthTenant(
            tenant_id="tenant_connection_lifecycle",
            slug="connection-lifecycle",
            name="Connection Lifecycle",
        ),
        session=AuthSession(
            session_id="ses_connection_lifecycle",
            expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        ),
        token="not-persisted",
        token_jti="jti_connection_lifecycle",
    )


READ_CALLS = (
    (provider_service, lambda cfg, ctx: provider_service.list_providers(
        cfg, ctx, status=None, provider_type=None, request_id="req", trace_id="trc"
    )),
    (model_service, lambda cfg, ctx: model_service.list_models(
        cfg, ctx, status=None, provider_id=None, capability=None,
        request_id="req", trace_id="trc"
    )),
    (project_service, lambda cfg, ctx: project_service.list_projects(
        cfg, ctx, status=None, search=None, request_id="req", trace_id="trc"
    )),
    (project_service, lambda cfg, ctx: project_service.list_applications(
        cfg, ctx, project_id=None, status=None, search=None,
        request_id="req", trace_id="trc"
    )),
    (virtual_key_service, lambda cfg, ctx: virtual_key_service.list_virtual_keys(
        cfg, ctx, status=None, environment_id=None, environment=None,
        request_id="req", trace_id="trc"
    )),
    (console_service, lambda cfg, ctx: console_service.get_console_navigation(
        cfg, ctx, requested_active_item_id=None, request_id="req", trace_id="trc"
    )),
    (console_service, lambda cfg, ctx: console_service.get_user_preferences(
        cfg, ctx, request_id="req", trace_id="trc"
    )),
    (environment_service, lambda cfg, ctx: environment_service.list_environments(
        cfg, ctx, status=None, code=None, request_id="req", trace_id="trc"
    )),
    (tenant_service, lambda cfg, ctx: tenant_service.get_current_tenant(
        cfg, ctx, request_id="req", trace_id="trc"
    )),
    (dlp_service, lambda cfg, ctx: dlp_service.list_dlp_rules(
        cfg, ctx, status=None, rule_type=None, action=None, scan_scope=None,
        request_id="req", trace_id="trc"
    )),
    (model_price_service, lambda cfg, ctx: model_price_service.list_model_price_plans(
        cfg, ctx, model_id=None, effective_status=None, limit=20, offset=0,
        request_id="req", trace_id="trc"
    )),
    (model_alias_service, lambda cfg, ctx: model_alias_service.list_model_aliases(
        cfg, ctx, status=None, environment_id=None, environment=None,
        request_id="req", trace_id="trc"
    )),
    (provider_health_service, lambda cfg, ctx: provider_health_service.list_provider_health(
        cfg, ctx, health_status=None, provider_status=None, provider_type=None,
        provider_id=None, limit=20, offset=0, request_id="req", trace_id="trc"
    )),
    (user_service, lambda cfg, ctx: user_service.list_users(
        cfg, ctx, status=None, request_id="req", trace_id="trc"
    )),
    (webhook_service, lambda cfg, ctx: webhook_service.list_webhooks(
        cfg, ctx, status=None, event_type=None, include_deliveries=False,
        request_id="req", trace_id="trc"
    )),
)


@pytest.mark.parametrize(("service", "call"), READ_CALLS)
def test_control_read_connection_closes_when_query_fails(monkeypatch, service, call):
    connection = _FailingConnection()
    monkeypatch.setattr(service, "_connect", lambda _config: connection)

    with pytest.raises(_QueryFailure):
        call(SimpleNamespace(), _context())

    assert connection.closed is True


def test_local_launcher_preserves_complete_scheduler_profile():
    launcher = (
        __import__("pathlib").Path(__file__).parents[3]
        / "scripts"
        / "dev"
        / "start-control-api.ps1"
    ).read_text(encoding="utf-8")
    assert "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED" not in launcher

    from metahub_shared.runtime.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED"] == "true"


def test_control_read_middleware_uses_the_bounded_idle_connection_pool():
    app_source = (
        __import__("pathlib").Path(__file__).parents[2]
        / "src"
        / "metahub_control"
        / "app.py"
    ).read_text(encoding="utf-8")

    assert 'if request.method in {"GET", "HEAD"}' in app_source
    assert "database_request_scope(reuse_connections=True)" in app_source
