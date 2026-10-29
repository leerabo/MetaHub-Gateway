"""验证签名 placement 与 noisy-neighbor 容量准入合同，边界覆盖 owner、过期、释放和配置脱敏。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from metahub_shared.gateway_fleet import (
    GatewayCapacityController,
    admit_gateway_request,
    create_placement_snapshot,
    install_placement_snapshot,
    verify_placement_snapshot,
)
from metahub_shared.runtime.config import ConfigError, config_loaded_log_payload, load_app_config
from metahub_shared.runtime.errors import ApiError

# 固定签名、owner、Cell 与 Key 仅用于隔离运行时测试，不得复用为部署密钥或真实客户标识。
SECRET = "test-prd101-placement-signing-secret-independent-2026"
TENANT = "ten_" + "1" * 32
WORKSPACE = "wsp_" + "2" * 32
CELL = "gcl_" + "3" * 32
KEY = "vk_" + "4" * 32


def _snapshot(*, now: datetime | None = None):
    """创建绑定固定测试 owner 与 Cell 的短期签名快照，允许测试显式控制时间。"""

    return create_placement_snapshot(
        signing_secret=SECRET,
        signing_key_version=1,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        gateway_cell_id=CELL,
        cell_code="cn-north-1-a",
        region_code="cn-north-1",
        placement_revision=7,
        residency_policy_version="cn-residency-v1",
        generation=9,
        ttl_seconds=30,
        now=now,
    )


def test_snapshot_rejects_expiry_signature_and_cross_tenant() -> None:
    """验证过期、错误签名和跨 tenant owner 都折叠为同一 placement 不可用错误。"""

    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    snapshot = _snapshot(now=now)
    verify_placement_snapshot(
        snapshot,
        signing_secret=SECRET,
        signing_key_version=1,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        now=now + timedelta(seconds=1),
    )
    cases = (
        {"tenant_id": "ten_" + "f" * 32, "now": now + timedelta(seconds=1)},
        {"tenant_id": TENANT, "now": now + timedelta(seconds=31)},
    )
    for case in cases:
        with pytest.raises(ApiError) as rejected:
            verify_placement_snapshot(
                snapshot,
                signing_secret=SECRET,
                signing_key_version=1,
                workspace_id=WORKSPACE,
                **case,
            )
        assert rejected.value.code == "gateway.placement_unavailable"
    with pytest.raises(ApiError) as tampered:
        verify_placement_snapshot(
            snapshot,
            signing_secret="different-independent-test-secret-2026-value",
            signing_key_version=1,
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            now=now + timedelta(seconds=1),
        )
    assert tampered.value.code == "gateway.placement_unavailable"


@pytest.mark.anyio
async def test_four_layer_capacity_distinguishes_tenant_and_cell_pressure() -> None:
    """分别耗尽 tenant、Workspace、Virtual Key 与 Cell 维度并验证错误分类和幂等释放。"""

    tenant_controller = GatewayCapacityController(
        tenant_limit=1, workspace_limit=2, key_limit=2, cell_limit=2
    )
    lease = await tenant_controller.acquire(
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        virtual_key_id=KEY,
        gateway_cell_id=CELL,
        generation=1,
    )
    with pytest.raises(ApiError) as tenant_full:
        await tenant_controller.acquire(
            tenant_id=TENANT,
            workspace_id="wsp_" + "5" * 32,
            virtual_key_id="vk_" + "6" * 32,
            gateway_cell_id=CELL,
            generation=1,
        )
    assert tenant_full.value.code == "gateway.tenant_capacity_exhausted"
    await lease.release()
    await lease.release()

    cell_controller = GatewayCapacityController(
        tenant_limit=2, workspace_limit=2, key_limit=2, cell_limit=1
    )
    cell_lease = await cell_controller.acquire(
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        virtual_key_id=KEY,
        gateway_cell_id=CELL,
        generation=1,
    )
    with pytest.raises(ApiError) as cell_full:
        await cell_controller.acquire(
            tenant_id="ten_" + "7" * 32,
            workspace_id="wsp_" + "8" * 32,
            virtual_key_id="vk_" + "9" * 32,
            gateway_cell_id=CELL,
            generation=1,
        )
    assert cell_full.value.code == "gateway.cell_capacity_exhausted"
    await cell_lease.release()

    workspace_controller = GatewayCapacityController(
        tenant_limit=2, workspace_limit=1, key_limit=2, cell_limit=2
    )
    workspace_lease = await workspace_controller.acquire(
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        virtual_key_id=KEY,
        gateway_cell_id=CELL,
        generation=1,
    )
    with pytest.raises(ApiError) as workspace_full:
        await workspace_controller.acquire(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            virtual_key_id="vk_" + "a" * 32,
            gateway_cell_id=CELL,
            generation=1,
        )
    assert workspace_full.value.code == "gateway.tenant_capacity_exhausted"
    await workspace_lease.release()

    key_controller = GatewayCapacityController(
        tenant_limit=2, workspace_limit=2, key_limit=1, cell_limit=2
    )
    key_lease = await key_controller.acquire(
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        virtual_key_id=KEY,
        gateway_cell_id=CELL,
        generation=1,
    )
    with pytest.raises(ApiError) as key_full:
        await key_controller.acquire(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            virtual_key_id=KEY,
            gateway_cell_id=CELL,
            generation=1,
        )
    assert key_full.value.code == "gateway.tenant_capacity_exhausted"
    await key_lease.release()


@pytest.mark.anyio
async def test_thousand_concurrent_noisy_neighbor_attempts_preserve_other_tenant() -> None:
    """验证九百九十九次并发噪声拒绝不污染其他 tenant 的独立容量和最终计数。"""

    controller = GatewayCapacityController(
        tenant_limit=1, workspace_limit=1, key_limit=1, cell_limit=1001
    )
    noisy_lease = await controller.acquire(
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        virtual_key_id=KEY,
        gateway_cell_id=CELL,
        generation=1,
    )

    async def rejected_attempt(index: int) -> str:
        """执行单次预期拒绝并只返回固定错误码，不传播 owner 或异常正文。"""

        try:
            await controller.acquire(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                virtual_key_id=KEY,
                gateway_cell_id=CELL,
                generation=index + 2,
            )
        # 只收集统一领域错误码，测试输出不得包含签名、owner 或内部状态。
        except ApiError as exc:
            return exc.code
        return "unexpected_success"

    pressure = await asyncio.gather(*(rejected_attempt(index) for index in range(999)))
    assert set(pressure) == {"gateway.tenant_capacity_exhausted"}
    independent = await controller.acquire(
        tenant_id="ten_" + "7" * 32,
        workspace_id="wsp_" + "8" * 32,
        virtual_key_id="vk_" + "9" * 32,
        gateway_cell_id=CELL,
        generation=1,
    )
    await independent.release()
    await noisy_lease.release()
    assert controller._counts == {}


@pytest.mark.anyio
async def test_admission_uses_signed_owner_and_releases_stream_generation() -> None:
    """验证准入只使用签名 owner，并在流终态释放 generation 对应的唯一容量令牌。"""

    state = SimpleNamespace()
    snapshot = _snapshot()
    install_placement_snapshot(state, snapshot)
    config = SimpleNamespace(
        gateway_placement_enabled=True,
        gateway_placement_signing_secret=SECRET,
        gateway_placement_signing_version=1,
        gateway_capacity_tenant_limit=1,
        gateway_capacity_workspace_limit=1,
        gateway_capacity_key_limit=1,
        gateway_capacity_cell_limit=1,
    )
    admission = await admit_gateway_request(
        config,
        state,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        virtual_key_id=KEY,
        resource_class="chat",
    )
    assert admission is not None
    assert admission.snapshot.generation == 9
    with pytest.raises(ApiError) as full:
        await admit_gateway_request(
            config,
            state,
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            virtual_key_id=KEY,
            resource_class="embeddings",
        )
    assert full.value.code == "gateway.tenant_capacity_exhausted"
    await admission.release()
    replacement = await admit_gateway_request(
        config,
        state,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        virtual_key_id=KEY,
        resource_class="embeddings",
    )
    assert replacement is not None
    await replacement.release()


def test_gateway_placement_config_requires_explicit_cell_and_masks_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证启用态必须声明本地 Cell，且签名密钥从模型、repr 与配置摘要中脱敏。"""

    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_GATEWAY_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET", SECRET)
    with pytest.raises(ConfigError) as missing_cell:
        load_app_config("gateway-data-plane")
    assert missing_cell.value.code == "infra.config_invalid"

    monkeypatch.setenv("METAHUB_GATEWAY_CELL_CODE", "cn-north-1-a")
    config = load_app_config("gateway-data-plane")
    assert config.gateway_cell_code == "cn-north-1-a"
    assert config.gateway_placement_signing_secret == SECRET
    assert "gateway_placement_signing_secret" not in config.model_dump()
    assert SECRET not in repr(config)
    summary = config_loaded_log_payload(config)
    item = next(
        value
        for value in summary["items"]
        if value["key"] == "METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET"
    )
    assert item["status"] == "masked"
    assert SECRET not in repr(summary)


def test_production_gateway_placement_rejects_missing_or_reused_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证生产启用态拒绝缺失或跨安全域复用的签名密钥，错误消息不包含原值。"""

    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_GATEWAY_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("METAHUB_GATEWAY_CELL_CODE", "cn-north-1-a")
    with pytest.raises(ConfigError) as missing:
        load_app_config("gateway-data-plane")
    assert missing.value.code == "infra.config_invalid"

    reused = "prod-prd101-reused-security-domain-secret-0123456789abcdef"
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", reused)
    monkeypatch.setenv("METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET", reused)
    with pytest.raises(ConfigError) as shared_domain:
        load_app_config("gateway-data-plane")
    assert shared_domain.value.code == "infra.config_invalid"
    assert reused not in shared_domain.value.message
