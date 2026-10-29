"""Navigation database operations must not block the ASGI event loop."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from metahub_control.workspace_navigation import routes
from metahub_shared.runtime.errors import ApiError
from starlette.requests import Request
from starlette.responses import Response

OPERATIONS = [
    ("workspace_navigation_route", "get_workspace_navigation", "_context"),
    ("workspace_getting_started_route", "get_workspace_getting_started", "_context"),
    ("workspace_advanced_tools_route", "get_workspace_advanced_tools", "_context"),
    ("patch_navigation_preference_route", "change_navigation_preference", "_identity"),
    ("open_workspace_advanced_tool_route", "open_workspace_advanced_tool", "_context"),
    ("workspace_page_visit_route", "record_workspace_page_visit", "_context"),
]


@pytest.mark.parametrize("route_name,operation_name,context_name", OPERATIONS)
@pytest.mark.parametrize("fail", [False, True])
def test_navigation_service_leaves_event_loop_responsive(
    monkeypatch, route_name, operation_name, context_name, fail,
):
    started = threading.Event()
    progressed = threading.Event()
    result = SimpleNamespace(
        contract_generation=1, projection_generation=1, task_generation=1,
        entitlement_generation=1, navigation_epoch=1, permission_revision=1,
        revision=1, entitlement_revision=1, permission_fingerprint="unit-fingerprint",
        event_id="unit-event",
    )
    request = Request({
        "type": "http", "method": "POST", "path": "/unit", "query_string": b"",
        "headers": [], "app": SimpleNamespace(state=SimpleNamespace(config=object())),
    })

    async def model(*args, **kwargs):
        return result

    async def reject_body(*args, **kwargs):
        return None

    for helper in ("_workspace_token", "_identity_token", "_identity_csrf", "_idempotency_key"):
        monkeypatch.setattr(routes, helper, lambda *args, **kwargs: "unit-value")
    monkeypatch.setattr(routes, "_if_match", lambda *args, **kwargs: 1)
    monkeypatch.setattr(routes, "_model", model)
    monkeypatch.setattr(routes, "_reject_body", reject_body)

    def operation(config, **kwargs):
        assert config is request.app.state.config
        started.set()
        assert progressed.wait(1), "Database service blocked the event loop"
        if fail:
            raise ApiError("workspace_session.unavailable")
        return result

    monkeypatch.setattr(routes, operation_name, operation)
    arguments = {"request": request, "response": Response(), context_name: None}
    if route_name == "open_workspace_advanced_tool_route":
        arguments["tool_code"] = next(iter(routes._KNOWN_TOOL_CODES))

    async def run():
        async def heartbeat():
            while not started.is_set():
                await asyncio.sleep(0)
            progressed.set()

        task = asyncio.create_task(heartbeat())
        try:
            if fail:
                with pytest.raises(ApiError) as error:
                    await getattr(routes, route_name)(**arguments)
                assert error.value.code == "workspace_session.unavailable"
            else:
                actual = await getattr(routes, route_name)(**arguments)
                assert actual is result
                assert "no-store" in arguments["response"].headers["Cache-Control"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
