"""Real Workspace routing/evaluation authentication, not batch release signoff."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from functools import partial
from uuid import uuid4

import pytest
from metahub_control.audit import AuditRequestMeta
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.auth.routes import router as legacy_auth_router
from metahub_control.experiments import routes as experiment_routes
from metahub_control.experiments import service as experiments
from metahub_control.experiments.models import CreateExperimentRequest
from metahub_control.model_aliases import cache_affinity_service as affinities
from metahub_control.model_aliases import routes as alias_routes
from metahub_control.model_aliases import service as aliases
from metahub_control.model_aliases.models import (
    CreateModelAliasRequest,
    UpdateFallbackPolicyRequest,
    UpdateModelAliasRequest,
)
from metahub_control.model_quality_scores import routes as score_routes
from metahub_control.model_quality_scores import service as scores
from metahub_control.workspace_access import service as workspace_access
from metahub_control.workspace_access.service import authenticate_workspace_session_as_auth_context
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import (  # noqa: F401
    WORKSPACE_SESSION,
    _headers,
    file_subjects,
    migrated_baseline,
)
from test_prd147_workspace_resource_writes import resource_subjects  # noqa: F401


@pytest.fixture
async def evaluation_subjects(resource_subjects):  # noqa: F811
    app, database, f, ids = resource_subjects
    for router in (alias_routes.router, score_routes.router, experiment_routes.router):
        app.include_router(router)
    assert not app.dependency_overrides
    return app, database, f, ids


@pytest.mark.anyio
async def test_real_workspace_evaluation_reads(evaluation_subjects):
    _, _, f, _ = evaluation_subjects
    for path in ("/model-aliases", "/model-quality-scores", "/experiments"):
        own = await f.source.get(path)
        assert own.status_code == 200, (path, own.json().get("code"))
        anonymous = await f.anonymous.get(path)
        assert anonymous.status_code == 401
        mixed = await f.source.get(path, headers={"Authorization": "Bearer invalid"})
        assert mixed.status_code == 400


def _facts(database):
    database_url = f"sqlite:///{database.as_posix()}" if hasattr(database, "as_posix") else database
    with closing(connect_database(database_url)) as connection:
        return {
            table: hashlib.sha256(
                json.dumps(
                    sorted(
                        (
                            tuple(row)
                            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()
                        ),
                        key=repr,
                    ),
                    default=str,
                ).encode()
            ).hexdigest()
            for table in (
                "model_aliases",
                "model_alias_routing_policies",
                "fallback_policies",
                "cache_affinities",
                "model_quality_scores",
                "model_experiments",
                "audit_logs",
            )
        }


@pytest.mark.anyio
async def test_real_workspace_evaluation_stale_read_context(evaluation_subjects):
    app, database, f, _ = evaluation_subjects
    stale = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    logout = await f.source.post(
        "/identity/logout", headers=_headers(f.source, global_session=True)
    )
    assert logout.status_code == 200
    before = _facts(database)
    for operation, options in (
        (aliases.list_model_aliases, dict(status=None, environment_id=None, environment=None)),
        (scores.list_model_quality_scores, dict(model_id=None, limit=100, offset=0)),
        (experiments.list_experiments, dict(status=None, limit=None, cursor=None)),
    ):
        with pytest.raises(ApiError) as denied:
            operation(
                app.state.config,
                stale,
                request_id="evaluation-stale",
                trace_id="evaluation-stale",
                **options,
            )
        assert denied.value.code.startswith(("workspace_session.", "auth."))
    assert _facts(database) == before


@pytest.fixture
async def evaluation_graph(evaluation_subjects):
    app, database, f, ids = evaluation_subjects
    configured = await f.source.patch(
        f"/models/{ids['model']}", headers=_headers(f.source), json={"context_window": 8192}
    )
    assert configured.status_code == 200, configured.json().get("code")
    second = await f.source.post(
        "/models",
        headers=_headers(f.source),
        json={
            "internal_model_id": "evaluation-b",
            "provider_id": ids["provider"],
            "provider_model_id": "evaluation-b",
            "capabilities": {"chat": True},
            "context_window": 8192,
        },
    )
    assert second.status_code == 201, second.json().get("code")
    ids["model_b"] = second.json()["model"]["model_id"]
    created = await f.source.post(
        "/model-aliases",
        headers=_headers(f.source),
        json={
            "alias": "evaluation",
            "environment_id": ids["environment"],
            "default_model_id": ids["model"],
            "fallback_model_id": ids["model_b"],
        },
    )
    assert created.status_code == 201, created.json().get("code")
    ids["alias"] = created.json()["model_alias"]["model_alias_id"]
    request = CreateExperimentRequest(
        name="Evaluation test",
        project_id=f.source_project_id,
        environment_id=ids["environment"],
        model_alias_id=ids["alias"],
        model_a_id=ids["model"],
        model_b_id=ids["model_b"],
        target_sample_count=10,
        traffic_sample_bps=1000,
        model_a_allocation_bps=5000,
        sampling_duration_seconds=3600,
        feedback_collection_enabled=False,
        feedback_window_seconds=0,
    )
    return app, database, f, ids, request


@pytest.mark.anyio
async def test_workspace_evaluation_mutations_and_replay(evaluation_graph):
    app, database, f, ids, experiment = evaluation_graph

    async def patch(path, payload, key):
        result = await f.source.patch(
            path, json=payload, headers={**_headers(f.source), "X-Request-ID": key}
        )
        assert result.status_code == 200, (path, result.json().get("code"))
        return result.json()

    alias_path = f"/model-aliases/{ids['alias']}"
    routing = {"routing_mode": "cost_first", "expected_version": 1}
    changed = await patch(f"{alias_path}/routing-mode", routing, "evaluation-routing")
    assert changed["changed"] is True and changed["routing_policy"]["version"] == 2
    before = _facts(database)
    replay = await patch(f"{alias_path}/routing-mode", routing, "evaluation-routing")
    assert replay["routing_policy"] == changed["routing_policy"]
    assert _facts(database) == before
    noop = await patch(
        f"{alias_path}/routing-mode", {**routing, "expected_version": 2}, "evaluation-routing-noop"
    )
    assert noop["changed"] is False and _facts(database) == before
    updated = await patch(alias_path, {"description": "Updated"}, "evaluation-description")
    assert updated["model_alias"]["routing_policy"]["version"] == 2
    fallback = await patch(
        f"{alias_path}/fallback",
        {
            "enabled": False,
            "trigger_conditions": ["upstream_timeout"],
            "expected_routing_policy_version": 2,
        },
        "evaluation-fallback",
    )
    assert fallback["fallback_policy"]["status"] == "disabled"
    affinity = {
        "status": "enabled",
        "data_class": "test",
        "minimum_prefix_chars": 256,
        "ttl_seconds": 60,
        "expected_version": 0,
    }
    cached = await patch(f"{alias_path}/cache-affinity", affinity, "evaluation-affinity")
    assert cached["changed"] is True
    cached_noop = await patch(
        f"{alias_path}/cache-affinity",
        {**affinity, "expected_version": 1},
        "evaluation-affinity-noop",
    )
    assert cached_noop["changed"] is False
    assert (
        await patch(
            f"{alias_path}/cache-affinity",
            {**affinity, "expected_version": 1},
            "evaluation-affinity-noop",
        )
    )["changed"] is False
    score = {"model_id": ids["model"], "manual_quality_bps": 8500, "expected_feedback_version": 1}
    scored = await patch("/model-quality-scores", score, "evaluation-score")
    assert scored["changed"] is True and scored["model_quality_score"]["feedback_version"] == 2
    assert (await patch("/model-quality-scores", score, "evaluation-score"))[
        "model_quality_score"
    ] == scored["model_quality_score"]
    assert (
        await patch(
            "/model-quality-scores",
            {**score, "expected_feedback_version": 2},
            "evaluation-score-noop",
        )
    )["changed"] is False
    key = str(uuid4())
    for is_replay in (False, True):
        created = await f.source.post(
            "/experiments",
            json=experiment.model_dump(),
            headers={**_headers(f.source), "Idempotency-Key": key},
        )
        assert created.status_code == 201, created.json().get("code")
        assert created.json()["idempotent_replay"] is is_replay
    experiment_id = created.json()["experiment"]["experiment_id"]
    for suffix in ("", "/results"):
        own = await f.source.get(f"/experiments/{experiment_id}{suffix}")
        assert own.status_code == 200, own.json().get("code")
        assert "no-store" in own.headers["Cache-Control"]
        foreign = await f.target.get(f"/experiments/{experiment_id}{suffix}")
        assert foreign.status_code == 404
    with closing(connect_database(app.state.config.database_url)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_experiments WHERE created_by=?",
                (
                    authenticate_workspace_session_as_auth_context(
                        app.state.config, f.source.cookies.get(WORKSPACE_SESSION)
                    ).user.user_id,
                ),
            ).fetchone()[0]
            == 1
        )


def _write_operations(config, context, ids, experiment):
    def call(function, *args, request_id):
        return partial(
            function,
            config,
            context,
            *args,
            audit_request_meta=AuditRequestMeta(None, None),
            request_id=request_id,
            trace_id=request_id,
        )

    return {
        "alias.create": call(
            aliases.create_model_alias,
            CreateModelAliasRequest(
                alias="evaluation-new",
                environment_id=ids["environment"],
                default_model_id=ids["model"],
            ),
            request_id="evaluation-new",
        ),
        "alias.update": call(
            aliases.update_model_alias,
            ids["alias"],
            UpdateModelAliasRequest(description="Changed description"),
            request_id="evaluation-update",
        ),
        "alias.noop": call(
            aliases.update_model_alias,
            ids["alias"],
            UpdateModelAliasRequest(description=None),
            request_id="evaluation-noop",
        ),
        "routing.write": call(
            aliases.update_routing_mode,
            ids["alias"],
            {
                "routing_mode": "cost_first",
                "expected_version": 1,
            },
            request_id="evaluation-mode",
        ),
        "routing.noop": call(
            aliases.update_routing_mode,
            ids["alias"],
            {
                "routing_mode": "fixed",
                "expected_version": 1,
            },
            request_id="evaluation-mode-noop",
        ),
        "fallback.write": call(
            aliases.update_fallback_policy,
            ids["alias"],
            UpdateFallbackPolicyRequest(
                enabled=False,
                trigger_conditions=["upstream_timeout"],
                expected_routing_policy_version=1,
            ),
            request_id="evaluation-fallback",
        ),
        "affinity.write": call(
            affinities.update_cache_affinity,
            ids["alias"],
            {
                "status": "disabled",
                "data_class": None,
                "minimum_prefix_chars": 256,
                "ttl_seconds": 60,
                "expected_version": 0,
            },
            request_id="evaluation-cache",
        ),
        "score.write": call(
            scores.update_manual_quality_score,
            {
                "model_id": ids["model"],
                "manual_quality_bps": 8000,
                "expected_feedback_version": 1,
            },
            request_id="evaluation-score",
        ),
        "score.noop": call(
            scores.update_manual_quality_score,
            {
                "model_id": ids["model"],
                "manual_quality_bps": None,
                "expected_feedback_version": 1,
            },
            request_id="evaluation-score-noop",
        ),
        "experiment.create": partial(
            call(experiments.create_experiment, experiment, request_id="evaluation-experiment"),
            idempotency_key=str(uuid4()),
        ),
    }


@pytest.mark.anyio
async def test_evaluation_terminal_expiry_rolls_back_and_closes(
    evaluation_graph, monkeypatch, record_property
):
    app, database, f, ids, experiment = evaluation_graph
    current = authenticate_workspace_session_as_auth_context(
        app.state.config, f.source.cookies.get(WORKSPACE_SESSION)
    )
    operations = _write_operations(app.state.config, current, ids, experiment)
    opened = []
    for service in (aliases, affinities, scores, experiments):
        original = service._connect

        def tracked(*args, original=original, **kwargs):
            connection = original(*args, **kwargs)
            opened.append(connection)
            return connection

        monkeypatch.setattr(service, "_connect", tracked)
    real_verify = ResourceWriteTransaction.verify
    seen = set()
    future = workspace_access._utcnow() + timedelta(days=2)

    def terminal_expiry(transaction):
        if transaction.connection in seen:
            with monkeypatch.context() as clock:
                clock.setattr(workspace_access, "_utcnow", lambda: future)
                return real_verify(transaction)
        seen.add(transaction.connection)
        return real_verify(transaction)

    monkeypatch.setattr(ResourceWriteTransaction, "verify", terminal_expiry)
    before = _facts(database)
    for name, operation in operations.items():
        with pytest.raises(ApiError) as denied:
            operation()
        assert denied.value.code == "workspace_session.expired", (name, denied.value.code)
        assert _facts(database) == before, name
    assert len(opened) == len(operations)
    for connection in opened:
        with pytest.raises((sqlite3.ProgrammingError, RuntimeError), match="closed"):
            connection.execute("SELECT 1")
    record_property("terminal_expiry_operations", json.dumps(list(operations)))


@pytest.mark.anyio
async def test_statistics_rechecks_expiry_in_readonly_snapshot(evaluation_graph, monkeypatch):
    app, database, f, _, experiment = evaluation_graph
    created = await f.source.post(
        "/experiments",
        json=experiment.model_dump(),
        headers={**_headers(f.source), "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.json().get("code")
    experiment_id = created.json()["experiment"]["experiment_id"]
    original = experiments.load_statistics_snapshot
    current = workspace_access._utcnow()
    observed = []

    def expire_after_snapshot(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        observed.append(True)
        monkeypatch.setattr(workspace_access, "_utcnow", lambda: current + timedelta(days=2))
        return snapshot

    before = _facts(database)
    with monkeypatch.context() as patch:
        patch.setattr(experiments, "load_statistics_snapshot", expire_after_snapshot)
        denied = await f.source.get(f"/experiments/{experiment_id}/results")
    assert observed == [True]
    assert denied.status_code == 401 and denied.json().get("code") == "workspace_session.expired"
    assert _facts(database) == before
    with database_request_scope(reuse_connections=True):
        with closing(connect_database(app.state.config.database_url)) as connection:
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()


@pytest.mark.anyio
async def test_evaluation_cookie_guards_precede_body(evaluation_graph):
    _, _, f, ids, _ = evaluation_graph
    for method, path in (
        ("POST", "/model-aliases"),
        ("PATCH", f"/model-aliases/{ids['alias']}"),
        ("PATCH", f"/model-aliases/{ids['alias']}/routing-mode"),
        ("PATCH", f"/model-aliases/{ids['alias']}/fallback"),
        ("PATCH", f"/model-aliases/{ids['alias']}/cache-affinity"),
        ("PATCH", "/model-quality-scores"),
        ("POST", "/experiments"),
    ):
        source = _headers(f.source, attack=True)
        for headers, status in (
            ({}, 401),
            ({**source, "X-CSRF-Token": "invalid"}, 403),
            ({**source, "Origin": "https://foreign.test"}, 403),
            ({**source, "Authorization": "Bearer invalid"}, 400),
        ):
            denied = await f.anonymous.request(method, path, headers=headers, content=b"not json")
            assert denied.status_code == status, (path, denied.json().get("code"))


@pytest.mark.anyio
async def test_explicit_legacy_evaluation_transport(evaluation_graph):
    app, database, f, ids, experiment = evaluation_graph
    app.include_router(legacy_auth_router)
    workspace = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    logged_in = await f.anonymous.post(
        "/auth/login",
        json={
            "tenant_slug": workspace.tenant.slug,
            "email": f.source_account.email,
            "password": f.source_account.password,
        },
    )
    assert logged_in.status_code == 200, logged_in.json().get("code")
    headers = {"Authorization": "Bearer " + logged_in.json()["access_token"]}
    for path in ("/model-aliases", "/model-quality-scores", "/experiments"):
        response = await f.anonymous.get(path, headers=headers)
        assert response.status_code == 200, response.json().get("code")
    for path, payload in (
        (
            f"/model-aliases/{ids['alias']}/routing-mode",
            {"routing_mode": "cost_first", "expected_version": 1},
        ),
        (
            f"/model-aliases/{ids['alias']}/cache-affinity",
            {
                "status": "enabled",
                "data_class": "test",
                "minimum_prefix_chars": 256,
                "ttl_seconds": 60,
                "expected_version": 0,
            },
        ),
        (
            "/model-quality-scores",
            {"model_id": ids["model"], "manual_quality_bps": 8500, "expected_feedback_version": 1},
        ),
    ):
        changed = await f.anonymous.patch(
            path, json=payload, headers={**headers, "X-Request-ID": "legacy-" + uuid4().hex}
        )
        assert changed.status_code == 200, changed.json().get("code")
    created = await f.anonymous.post(
        "/experiments",
        json=experiment.model_dump(),
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.json().get("code")
    experiment_id = created.json()["experiment"]["experiment_id"]
    results = await f.anonymous.get(f"/experiments/{experiment_id}/results", headers=headers)
    assert results.status_code == 200, results.json().get("code")
    logged_out = await f.anonymous.post("/auth/logout", headers=headers)
    assert logged_out.status_code == 200
    before = _facts(database)
    denied = await f.anonymous.patch(
        "/model-quality-scores",
        json={
            "model_id": ids["model"],
            "manual_quality_bps": 7000,
            "expected_feedback_version": 2,
        },
        headers=headers,
    )
    assert denied.status_code == 401
    assert _facts(database) == before
    assert (await f.source.get("/model-quality-scores")).status_code == 200
