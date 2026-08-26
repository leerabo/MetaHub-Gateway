"""PRD-122 Console navigation accepts the verified telemetry permission family."""

from metahub_control.console.service import _visible_navigation_items


def test_private_gateway_navigation_accepts_complete_telemetry_extensions() -> None:
    items = _visible_navigation_items(
        (),
        extension_permission_codes=(
            "edge.deployment.manage",
            "edge.deployment.read",
            "edge.telemetry.manage",
            "edge.telemetry.read",
            "edge.usage.read",
        ),
    )

    private_gateway = next(item for item in items if item.id == "private-gateways")
    assert private_gateway.required_permissions == ["edge.deployment.read"]
