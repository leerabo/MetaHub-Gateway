"""Compile Control routing metadata before workers, without executing request code."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, routing
from metahub_control.platform_jobs.supervisor import install_platform_job_supervisor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _application(events):
    @asynccontextmanager
    async def base(_app):
        events.append("base.enter")
        yield
        events.append("base.exit")

    app = FastAPI(lifespan=base)
    app.state.config = SimpleNamespace(control_background_schedulers_enabled=False)

    def install_scheduler(current_app):
        @asynccontextmanager
        async def scheduler(_app):
            events.append("scheduler.enter")
            yield
            events.append("scheduler.exit")

        current_app.router.lifespan_context = scheduler

    install_platform_job_supervisor(app, scheduler_installers=(install_scheduler,))
    return app


@pytest.mark.anyio
async def test_prepares_late_nested_and_hidden_routes_before_lifespans(monkeypatch):
    lazy_routing = hasattr(routing, "iter_route_contexts")
    events = []
    app = _application(events)
    child = APIRouter()

    async def deny():
        events.append("authorize")
        raise HTTPException(status_code=403)

    @child.get("/hidden", include_in_schema=False)
    async def hidden(value: int):
        events.append("hidden")
        return {"value": value}

    @child.get("/private", dependencies=[Depends(deny)])
    async def private():
        events.append("private")
        return {"unexpected": True}

    parent = APIRouter(prefix="/nested")
    parent.include_router(child)
    app.include_router(parent, prefix="/late")
    original = routing.get_dependant

    def compile_dependency(*args, **kwargs):
        events.append("compile")
        return original(*args, **kwargs)

    monkeypatch.setattr(routing, "get_dependant", compile_dependency)
    async with app.router.lifespan_context(app):
        if lazy_routing:
            assert "compile" in events
            assert events.index("compile") < events.index("base.enter")
        assert events[-2:] == ["base.enter", "scheduler.enter"]
        assert not {"authorize", "hidden", "private"}.intersection(events)
        compile_count = events.count("compile")

        def reject_late_compilation(*_args, **_kwargs):
            raise AssertionError("Request attempted cold route compilation")

        monkeypatch.setattr(routing, "get_dependant", reject_late_compilation)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/late/nested/private")).status_code == 403
            result = await client.get("/late/nested/hidden", params={"value": 7})
            assert result.status_code == 200
            assert result.json() == {"value": 7}
            assert (await client.get("/late/nested/hidden?value=invalid")).status_code == 422
        assert events.count("compile") == compile_count
        assert events.count("authorize") == 1
        assert events.count("hidden") == 1
        assert "private" not in events
        assert "/late/nested/private" in app.openapi()["paths"]
        assert "/late/nested/hidden" not in app.openapi()["paths"]
    assert events[-2:] == ["scheduler.exit", "base.exit"]


@pytest.mark.anyio
async def test_preparation_failure_starts_no_lifespans_or_workers(monkeypatch):
    events = []
    app = _application(events)

    def fail_preparation(_routes):
        raise RuntimeError("test-only routing preparation failure")

    monkeypatch.setattr(routing, "iter_route_contexts", fail_preparation)
    with pytest.raises(RuntimeError, match="test-only routing preparation failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("Failed preparation must not serve requests")
    assert events == []


@pytest.mark.anyio
async def test_older_eager_routing_without_preparation_api_remains_usable(monkeypatch):
    events = []
    app = _application(events)

    @app.get("/eager")
    async def eager():
        return {"ready": True}

    monkeypatch.delattr(routing, "iter_route_contexts", raising=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get("/eager")
            assert result.status_code == 200
            assert result.json() == {"ready": True}
    assert events == ["base.enter", "scheduler.enter", "scheduler.exit", "base.exit"]


@pytest.mark.anyio
async def test_each_start_prepares_the_current_route_graph(monkeypatch):
    events = []
    app = _application(events)
    counts = []
    original = routing.iter_route_contexts

    def prepare(routes):
        contexts = list(original(routes))
        counts.append(len(contexts))
        return iter(contexts)

    monkeypatch.setattr(routing, "iter_route_contexts", prepare)
    async with app.router.lifespan_context(app):
        pass

    @app.get("/later")
    async def later():
        return {"later": True}

    async with app.router.lifespan_context(app):
        pass
    assert len(counts) == 2
    assert counts[1] == counts[0] + 1


@pytest.mark.anyio
async def test_complete_control_catalog_prepares_without_requests(monkeypatch, record_property):
    from metahub_control.app import app as control_app

    events = []
    app = _application(events)
    # Use the real route graph but not its database, identity, or worker lifespans.
    app.router.routes = list(control_app.routes)
    original_routes = tuple(app.routes)
    async with app.router.lifespan_context(app):
        def reject_late_compilation(*_args, **_kwargs):
            raise AssertionError("Control catalog remained cold after startup")

        monkeypatch.setattr(routing, "get_dependant", reject_late_compilation)
        contexts = list(routing.iter_route_contexts(app.routes))
        record_property("route_context_count", len(contexts))
        paths = app.openapi()["paths"]
        assert {"/identity/login", "/feedback/summary", "/memberships"} <= paths.keys()
        assert tuple(app.routes) == original_routes
    assert events == ["base.enter", "scheduler.enter", "scheduler.exit", "base.exit"]
