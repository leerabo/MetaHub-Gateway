"""Employee-bound OpenAI clients may omit model and use the tenant default alias."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import metahub_gateway.api.openai_compatible as openai_api
from metahub_gateway.routing import models as routing_models
from starlette.requests import Request


class _Connection:
    backend = "sqlite"

    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def execute(self, _sql, _parameters=()):
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


def _row(alias, created_at, capabilities):
    return {"alias": alias, "created_at": created_at, "capabilities": capabilities}


def test_resolve_default_model_alias_prefers_conventional_default(monkeypatch):
    connection = _Connection(
        [
            _row("first-route", "2026-01-01T00:00:00Z", '{"chat": true}'),
            _row("default", "2026-01-02T00:00:00Z", '{"chat": true}'),
            _row("embedding-only", "2026-01-03T00:00:00Z", '{"embeddings": true}'),
        ]
    )
    monkeypatch.setattr(routing_models, "_connect", lambda _config: connection)
    monkeypatch.setattr(routing_models, "_resolve_environment_id", lambda *_args: "env_1")

    assert routing_models.resolve_default_model_alias(
        object(),
        tenant_id="tenant_1",
        environment="dev",
        environment_id="env_1",
        request_kind="chat",
    ) == "default"
    assert connection.closed is True


def test_resolve_default_model_alias_filters_by_request_capability(monkeypatch):
    connection = _Connection(
        [
            _row("chat-route", "2026-01-01T00:00:00Z", '{"chat": true}'),
            _row("embedding-route", "2026-01-02T00:00:00Z", '{"embeddings": true}'),
        ]
    )
    monkeypatch.setattr(routing_models, "_connect", lambda _config: connection)
    monkeypatch.setattr(routing_models, "_resolve_environment_id", lambda *_args: "env_1")

    assert routing_models.resolve_default_model_alias(
        object(),
        tenant_id="tenant_1",
        environment="dev",
        environment_id="env_1",
        request_kind="embeddings",
    ) == "embedding-route"


def test_list_public_model_aliases_returns_openai_discovery_projection(monkeypatch):
    connection = _Connection(
        [
            {"alias": "company-default", "created_at": "2026-01-02T00:00:00Z"},
            {"alias": "fast-chat", "created_at": "not-a-date"},
            {"alias": "", "created_at": "2026-01-03T00:00:00Z"},
        ]
    )
    monkeypatch.setattr(routing_models, "_connect", lambda _config: connection)
    monkeypatch.setattr(routing_models, "_resolve_environment_id", lambda *_args: "env_1")

    assert routing_models.list_public_model_aliases(
        object(),
        tenant_id="tenant_1",
        environment="dev",
        environment_id="env_1",
    ) == (
        {
            "id": "company-default",
            "object": "model",
            "created": 1767312000,
            "owned_by": "metahub",
        },
        {"id": "fast-chat", "object": "model", "created": 0, "owned_by": "metahub"},
    )
    assert connection.closed is True


def test_models_endpoint_uses_same_bearer_key_and_returns_standard_list(monkeypatch):
    context = SimpleNamespace(tenant_id="tenant_1", environment="dev", environment_id="env_1")
    monkeypatch.setattr(openai_api, "_config", lambda _request: object())
    monkeypatch.setattr(openai_api, "authenticate_virtual_key", lambda *_args: context)
    monkeypatch.setattr(openai_api, "_ensure_environment_active", lambda _context: None)
    monkeypatch.setattr(
        openai_api,
        "list_public_model_aliases",
        lambda *_args, **_kwargs: (
            {"id": "default", "object": "model", "created": 0, "owned_by": "metahub"},
        ),
    )
    request = Request({"type": "http", "method": "GET", "path": "/v1/models", "headers": []})

    response = asyncio.run(openai_api.models(request))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["object"] == "list"
    assert payload["data"] == [
        {"id": "default", "object": "model", "created": 0, "owned_by": "metahub"}
    ]
    assert "Cache-Control" in response.headers
