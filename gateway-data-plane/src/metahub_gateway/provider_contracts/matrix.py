"""把冻结 spec 与显式夹具转换为 exact-set 合并门禁矩阵。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from metahub_shared.runtime.provider_adapter_spec import ProviderAdapterSpec, provider_adapter_specs

from metahub_gateway.providers import factory as factory_module

from .fixtures import CONTRACT_FIXTURES
from .models import AdapterContractFixture, ContractCaseDefinition


class ContractMatrixError(ValueError):
    """表示夹具、spec、工厂或用例矩阵不满足冻结合同。

    `code` 是报告允许的固定失败分类；异常消息保持固定，不附带未知 Provider 值或文件正文。
    """

    def __init__(self, code: str) -> None:
        """保存固定错误码，供 CLI 映射为非零退出和安全报告。"""

        super().__init__(code)
        self.code = code


# 每个 spec 除鉴权参数化 request 外必须提交九类固定行为用例。
_FIXED_CASES = (
    ("non_stream", "success"),
    ("stream", "success"),
    ("error", "auth_401"),
    ("error", "rate_limit_429"),
    ("error", "server_5xx"),
    ("error", "timeout"),
    ("usage", "standard"),
    ("usage", "missing"),
    ("usage", "invalid"),
)


def _case_key(case_id: str, kind: str, variant: str, auth_type: str | None = None) -> str:
    """生成跨 CI、API 和数据库稳定的有界用例键。"""

    suffix = f":{auth_type}" if auth_type else ""
    return f"{case_id}:{kind}:{variant}{suffix}"


def validate_fixture_coverage(
    fixtures: Mapping[str, AdapterContractFixture] = CONTRACT_FIXTURES,
    specs: Sequence[ProviderAdapterSpec] | None = None,
) -> tuple[ProviderAdapterSpec, ...]:
    """验证 spec、显式夹具和生产工厂 Adapter family 三方完全一致。

    参数：可注入夹具与 spec 只用于无文件修改的负向测试。返回：按 provider type 排序的冻结
    spec。缺失、多余、重复或身份漂移均在启动 mock server 前失败关闭。
    """

    catalog = tuple(specs if specs is not None else provider_adapter_specs())
    expected_ids = {spec.contract_case_id for spec in catalog}
    actual_ids = set(fixtures)
    # 缺少显式夹具意味着新增 Adapter 未提交测试，必须在任何网络资源创建前阻断。
    if expected_ids - actual_ids:
        raise ContractMatrixError("adapter.contract_fixture_missing")
    # 多余夹具可能掩盖已删除 spec 或旧断言，不能静默忽略后继续运行。
    if actual_ids - expected_ids:
        raise ContractMatrixError("adapter.contract_fixture_unexpected")
    expected_families = {spec.adapter_name for spec in catalog}
    actual_families = set(factory_module._ADAPTER_TYPES)
    if expected_families != actual_families:
        # 工厂新增可执行 Adapter 却没有 spec/case 时必须阻断，不能只检查 Provider 类型集合。
        raise ContractMatrixError("adapter.contract_coverage_mismatch")
    seen_providers: set[str] = set()
    for spec in catalog:
        fixture = fixtures[spec.contract_case_id]
        # Provider 类型是工厂选择键，重复定义会造成顺序相关执行，必须失败关闭。
        if spec.provider_type in seen_providers:
            raise ContractMatrixError("adapter.contract_fixture_invalid")
        seen_providers.add(spec.provider_type)
        if (
            fixture.contract_case_id != spec.contract_case_id
            or fixture.tenant_id != "ci-contract"
            or fixture.spec_id != spec.spec_id
            or fixture.provider_type != spec.provider_type
            or fixture.adapter_name != spec.adapter_name
            or fixture.adapter_version != spec.adapter_version
            or (
                b"data: [DONE]" in fixture.stream_body
            ) != spec.stream_profile.final_usage_expected
        ):
            # 任一身份或 stream profile 漂移都说明夹具不再证明当前发布 spec。
            raise ContractMatrixError("adapter.contract_fixture_invalid")
    return tuple(sorted(catalog, key=lambda item: item.provider_type))


def build_contract_matrix(
    fixtures: Mapping[str, AdapterContractFixture] = CONTRACT_FIXTURES,
    specs: Sequence[ProviderAdapterSpec] | None = None,
    *,
    tenant_id: str = "ci-contract",
) -> tuple[ContractCaseDefinition, ...]:
    """生成当前目录完整且不写死数量的 merge gate exact-set。

    每个允许鉴权类型生成一个 request case；其余九类行为对每个 spec 各生成一个 case。当前
    冻结目录自然得到 92 条，但未来变化必须通过集合推导，不能硬编码数量。``tenant_id`` 只
    标记部署租户的持久证据，不参与 case key；夹具仍保持固定合成身份，不能覆盖显式目标租户。
    """

    catalog = validate_fixture_coverage(fixtures, specs)
    cases: list[ContractCaseDefinition] = []
    for spec in catalog:
        fixture = fixtures[spec.contract_case_id]
        for auth_type in spec.allowed_auth_types:
            cases.append(
                ContractCaseDefinition(
                    case_key=_case_key(spec.contract_case_id, "request", "auth", auth_type),
                    contract_case_id=spec.contract_case_id,
                    tenant_id=tenant_id,
                    spec_id=spec.spec_id,
                    provider_type=spec.provider_type,
                    adapter_name=spec.adapter_name,
                    adapter_version=spec.adapter_version,
                    case_kind="request",
                    variant="auth",
                    auth_type=auth_type,
                )
            )
        for kind, variant in _FIXED_CASES:
            cases.append(
                ContractCaseDefinition(
                    case_key=_case_key(spec.contract_case_id, kind, variant),
                    contract_case_id=spec.contract_case_id,
                    tenant_id=tenant_id,
                    spec_id=spec.spec_id,
                    provider_type=spec.provider_type,
                    adapter_name=spec.adapter_name,
                    adapter_version=spec.adapter_version,
                    case_kind=kind,  # type: ignore[arg-type]
                    variant=variant,
                )
            )
    keys = [case.case_key for case in cases]
    # 重复键会让报告和数据库唯一事实丢失一个断言，不能仅按 case 数量判断完整。
    if len(keys) != len(set(keys)):
        raise ContractMatrixError("adapter.contract_coverage_mismatch")
    return tuple(cases)


def validate_result_coverage(
    expected: Sequence[ContractCaseDefinition],
    actual_case_keys: Iterable[str],
) -> None:
    """要求实际结果与预期 case key exact-set 完全一致且没有重复。

    参数：预期矩阵和实际 key 迭代器。返回：完全一致时无返回值。skip、缺失、多余和重复
    都使用同一固定覆盖错误，不把具体 Provider 或 case key 写入 CI 日志。
    """

    expected_keys = {case.case_key for case in expected}
    actual = list(actual_case_keys)
    # 同时比较长度与集合，确保缺失、多余及重复结果都无法伪装成 exact-set。
    if len(actual) != len(set(actual)) or set(actual) != expected_keys:
        raise ContractMatrixError("adapter.contract_coverage_mismatch")
