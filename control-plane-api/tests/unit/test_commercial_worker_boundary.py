"""Commercial database and object operations must leave the ASGI loop responsive."""

import asyncio
import importlib
import inspect
import threading
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from metahub_shared.runtime.errors import ApiError
from starlette.requests import Request
from starlette.responses import Response

OPERATIONS = {
    "saas_billing": [
        ("get_catalog_plans", "list_public_plans"),
        ("get_subscription", "get_current_subscription"),
        ("post_subscription", "create_subscription"),
        ("post_subscription_change", "change_subscription_plan"),
        ("patch_subscription", "change_subscription_plan"),
        ("patch_subscription", "cancel_subscription"),
        ("get_platform_plans", "list_platform_plans"),
        ("post_platform_plan", "create_platform_plan"),
    ],
    "platform_billing": [
        ("get_billing_summary", "get_workspace_billing"),
        ("get_billing_usage", "get_workspace_billing"),
        ("get_billing_reconciliation_export", "export_workspace_reconciliation_csv"),
        ("get_platform_billing_summary", "list_platform_billing"),
        ("post_platform_billing_adjustment", "create_adjustment"),
        ("post_platform_meter_recovery", "recover_meter_fact"),
    ],
    "business_verification": [
        ("post_business_verification", "submit_business_verification"),
        ("get_business_verification", "get_workspace_business_verification"),
        ("patch_billing_profile", "update_workspace_billing_profile"),
        ("get_platform_business_verifications", "list_platform_business_verifications"),
        ("post_platform_business_verification_review", "review_business_verification"),
        ("post_evidence_download_token", "issue_evidence_download_token"),
    ],
    "payment_commerce": [
        ("get_billing_orders", "list_billing_orders"),
        ("post_payment_intent", "create_payment_intent"),
        ("post_refund", "request_refund"),
        ("post_payment_webhook", "persist_webhook_receipt"),
        ("post_platform_refund_review", "review_refund"),
        ("get_platform_refunds", "list_platform_refunds"),
    ],
    "invoice_reconciliation": [
        ("get_billing_statements", "list_billing_statements"),
        ("post_invoice", "create_invoice"),
        ("read_invoice", "get_invoice"),
        ("post_invoice_download_token", "issue_invoice_download_token"),
        ("get_invoice_download", "consume_invoice_download"),
        ("post_reconciliation", "create_reconciliation"),
        ("get_reconciliation_differences", "list_reconciliation_differences"),
        ("post_invoice_reversal", "create_invoice_reversal"),
        ("post_invoice_reversal_approval", "approve_invoice_reversal"),
    ],
    "notifications": [
        ("get_notifications", "list_notifications"),
        ("patch_notification_read", "mark_notification_read"),
        ("get_preferences", "get_notification_preferences"),
        ("patch_preferences", "update_notification_preferences"),
        ("get_platform_notification_templates", "list_notification_templates"),
        ("post_platform_notification_template", "create_notification_template"),
    ],
}
CASES = [
    pytest.param(module, route, service, id=f"{module}-{route}-{service}")
    for module, operations in OPERATIONS.items()
    for route, service in operations
]
REQUEST_MARKER = ContextVar("commercial_worker_test_request", default=None)


@pytest.mark.parametrize("module_name,route_name,service_name", CASES)
@pytest.mark.parametrize("outcome", ["success", "replay", "error"])
def test_commercial_service_worker_boundary(
    monkeypatch, module_name, route_name, service_name, outcome,
):
    """Exercise every HTTP branch while a separate heartbeat waits for the service."""

    routes = importlib.import_module(f"metahub_control.{module_name}.routes")
    endpoint = getattr(routes, route_name)
    route = next(item for item in routes.router.routes if item.endpoint is endpoint)
    started = threading.Event()
    progressed = threading.Event()
    loop_thread = threading.get_ident()
    marker = object()
    entity = SimpleNamespace(revision=7, state_revision=7)
    result = SimpleNamespace(
        subscription=entity, payment_intent=entity, refund=entity,
        verification=entity, billing_profile=entity, case=entity,
        invoice=entity, reversal=entity, revision=7, catalog_revision=7,
        intent_revision=7, idempotent_replay=outcome == "replay",
        content=b"unit-artifact", mime_type="application/pdf", filename="invoice.pdf",
    )
    payload = SimpleNamespace(
        operation="cancel" if service_name == "cancel_subscription" else "change_plan",
        plan_version_id="spv_" + "1" * 32,
    )
    request = Request({
        "type": "http", "method": next(iter(route.methods)), "path": "/unit",
        "query_string": b"status=requested" if route_name == "get_platform_refunds" else b"",
        "headers": [(b"x-metahub-invoice-download-token", b"unit-download")],
        "app": SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(payment_webhook_max_body_bytes=8192),
        )),
    })

    async def json_body(*args, **kwargs):
        return {}

    async def raw_body(*args, **kwargs):
        return b"{}"

    for helper in ("_required_cookie", "_required_csrf", "_idempotency_key"):
        monkeypatch.setattr(routes, helper, lambda *args, **kwargs: "unit-value")
    if hasattr(routes, "_if_match"):
        monkeypatch.setattr(routes, "_if_match", lambda *args, **kwargs: 7)
    if hasattr(routes, "_model"):
        monkeypatch.setattr(routes, "_model", lambda *args, **kwargs: payload)
    for model_name in (
        "BillingAdjustmentRequest", "MeterRecoveryRequest", "BusinessVerificationSubmitRequest",
        "BillingProfileUpdateRequest", "VerificationReviewRequest",
    ):
        if hasattr(routes, model_name):
            monkeypatch.setattr(
                getattr(routes, model_name), "model_validate", lambda *args: payload,
            )
    monkeypatch.setattr(routes, "_strict_json", json_body)
    if hasattr(routes, "_raw_body"):
        monkeypatch.setattr(routes, "_raw_body", raw_body)
    if hasattr(routes, "_single_header"):
        monkeypatch.setattr(routes, "_single_header", lambda *args: "1")

    calls = []
    error = ApiError("workspace_session.unavailable")

    def service(config, **kwargs):
        calls.append(kwargs)
        assert config is request.app.state.config
        assert REQUEST_MARKER.get() is marker
        started.set()
        assert progressed.wait(1), "Commercial service blocked the ASGI event loop"
        assert threading.get_ident() != loop_thread
        if outcome == "error":
            raise error
        if service_name == "export_workspace_reconciliation_csv":
            return b"unit,csv\n", "reconciliation.csv"
        return result

    monkeypatch.setattr(routes, service_name, service)
    response = Response(status_code=route.status_code or 200)
    known_arguments = {"request": request, "response": response, "_context": None}
    arguments = {
        name: known_arguments.get(name, "unit-id")
        for name, parameter in inspect.signature(endpoint).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }

    async def run():
        async def heartbeat():
            while not started.is_set():
                await asyncio.sleep(0.001)
            progressed.set()

        token = REQUEST_MARKER.set(marker)
        task = asyncio.create_task(heartbeat())
        try:
            if outcome == "error":
                with pytest.raises(ApiError) as caught:
                    await endpoint(**arguments)
                assert caught.value is error
            else:
                actual = await endpoint(**arguments)
                if service_name == "export_workspace_reconciliation_csv":
                    response_to_check = actual
                    assert actual.body == b"unit,csv\n"
                    assert actual.headers["Content-Disposition"].endswith('"reconciliation.csv"')
                elif service_name == "consume_invoice_download":
                    response_to_check = actual
                    assert actual.body == result.content
                    assert actual.headers["Content-Type"] == result.mime_type
                else:
                    response_to_check = response
                    assert actual is result
                if route_name == "get_catalog_plans":
                    assert response_to_check.headers["Cache-Control"] == "public, max-age=60"
                else:
                    assert "no-store" in response_to_check.headers["Cache-Control"]
                if outcome == "replay" and route_name in {
                    "post_subscription", "post_payment_intent", "post_refund",
                    "post_payment_webhook", "post_invoice", "post_invoice_reversal",
                    "post_platform_notification_template", "post_platform_meter_recovery",
                }:
                    assert response.status_code == 200
                if "ETag" in response.headers:
                    assert response.headers["ETag"] == '"7"'
            assert len(calls) == 1
        finally:
            REQUEST_MARKER.reset(token)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
