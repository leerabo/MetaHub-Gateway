"""PRD-064 Gateway 真实探测与 Control 上报专项测试。

测试通过 httpx 内存 transport 执行完整 HTTP 编码、响应上限、同源和 Header 合同，不访问
公网或真实服务。pytest basetemp 只允许位于仓库 `.tmp` 或 `.artifacts`，其中需要交给生产
路径门禁读取的配置、报告仍映射到仓库 `.artifacts`；Virtual Key 与内部 Token canary 只
存在于进程内 Header，不能进入报告、stdout 或异常文本。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from metahub_gateway.provider_contracts import cli as cli_module
from metahub_gateway.provider_contracts.models import ContractRunReport
from metahub_gateway.provider_contracts.network import ServiceOrigin
from metahub_gateway.provider_contracts.probe import (
    GatewayProbeConfig,
    ProbeConfigError,
    load_gateway_probe_config,
    run_gateway_probe,
)
from metahub_gateway.provider_contracts.report import (
    canonical_report_bytes,
    report_hash,
    validate_report,
)
from metahub_gateway.provider_contracts.upload import (
    ControlUploadConfig,
    ControlUploadError,
    build_control_upload_config,
    upload_report,
)

# Canary 只用于证明 transport 收到凭据且报告未泄露，绝不能替换为开发或客户真实 Key。
VIRTUAL_KEY_CANARY = "mh_vk_prd064_probe_secret_canary"
INTERNAL_TOKEN_CANARY = "prd064-control-internal-token-canary"
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def _artifact_file(tmp_path: Path, name: str) -> Path:
    """校验工作区内 pytest 临时根，并返回生产路径门禁认可的制品文件路径。

    参数：pytest 分配的用例目录与目标文件名。返回：工作区 `.artifacts` 下的隔离路径。
    约束：basetemp 只接受工作区内 `.tmp`/`.artifacts`；链接逃逸或其他目录必须在写入前
    失败。`.tmp` 只提供沙箱内的用例隔离标识，正式探测配置仍不得绕过生产侧制品门禁。
    """

    workspace_root = WORKSPACE_ROOT.resolve(strict=True)
    resolved = tmp_path.resolve()
    try:
        relative = resolved.relative_to(workspace_root)
    # resolve 后越出工作区通常意味着系统临时目录或链接逃逸，必须在任何写入前失败。
    except ValueError as exc:
        raise AssertionError(
            "PRD-064 tests require a workspace-local .tmp or .artifacts basetemp"
        ) from exc
    if not relative.parts or relative.parts[0].casefold() not in {
        ".tmp",
        ".artifacts",
    }:
        # 工作区内的源码、数据或其他隐藏目录也不是 pytest 写入根，不能只凭“未越出仓库”放行。
        raise AssertionError(
            "PRD-064 tests require a workspace-local .tmp or .artifacts basetemp"
        )
    if relative.parts[0].casefold() == ".artifacts":
        return resolved / name

    # 沙箱测试统一把 pytest 临时目录放在 `.tmp`，但生产配置读取明确拒绝 `.tmp`。这里只把
    # 已校验的相对用例标识映射到正式制品根，不能把任意调用方路径直接拼入输出位置。
    artifact_root = (workspace_root / ".artifacts").resolve(strict=False)
    try:
        artifact_root.relative_to(workspace_root)
    # `.artifacts` 若是指向工作区外的链接，也不能借测试辅助逻辑绕过生产安全边界。
    except ValueError as exc:
        raise AssertionError("PRD-064 artifact root must remain in workspace") from exc
    artifact_dir = (
        artifact_root
        / "pytest-prd064"
        / "gateway-probe-upload"
        / Path(*relative.parts[1:])
    ).resolve(strict=False)
    try:
        artifact_dir.relative_to(artifact_root)
    # 即使未来隔离标识生成规则变化，也必须再次证明最终目录仍属于正式制品根。
    except ValueError as exc:
        raise AssertionError("PRD-064 artifact path must remain in .artifacts") from exc
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir / name


def test_artifact_file_rejects_workspace_external_basetemp() -> None:
    """路径权限：工作区外 basetemp 必须在创建配置或报告前被辅助门禁拒绝。"""

    with pytest.raises(AssertionError, match="workspace-local"):
        _artifact_file(WORKSPACE_ROOT.parent / "prd064-outside", "forbidden.json")


def _probe_config(*, stream: bool = True) -> GatewayProbeConfig:
    """构造只连接 HTTPS 测试 origin、覆盖 OpenAI/Generic 两类 family 的配置。"""

    return GatewayProbeConfig(
        origin=ServiceOrigin("https://gateway.test", "https", "gateway.test", 443),
        tenant_id="default",
        provider_models=(
            ("custom", "probe-custom"),
            ("deepseek", "probe-deepseek"),
        ),
        virtual_key_env="METAHUB_PRD064_VIRTUAL_KEY",
        virtual_key=VIRTUAL_KEY_CANARY,
        stream=stream,
        timeout_seconds=5.0,
    )


def _gateway_success_response(request: httpx.Request) -> httpx.Response:
    """模拟 Gateway 的两类非流式/流式响应，并校验请求只携带 Virtual Key。"""

    if request.url.path == "/health":
        assert request.method == "GET"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": request.headers["x-request-id"],
                "X-Trace-ID": request.headers["x-trace-id"],
            },
            json={
                "status": "ok",
                "service_name": "gateway-data-plane",
                "environment": "test",
                "request_id": request.headers["x-request-id"],
                "trace_id": request.headers["x-trace-id"],
            },
            request=request,
        )
    if request.url.path == "/v1/adapter-contract-probes/identity":
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {VIRTUAL_KEY_CANARY}"
        assert request.headers["x-metahub-contract-tenant-id"] == "default"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
                "X-Request-ID": request.headers["x-request-id"],
                "X-Trace-ID": request.headers["x-trace-id"],
                "X-MetaHub-Contract-Tenant-ID": "default",
            },
            json={
                "status": "ok",
                "service_name": "gateway-data-plane",
                "request_id": request.headers["x-request-id"],
                "trace_id": request.headers["x-trace-id"],
            },
            request=request,
        )
    assert request.url == httpx.URL("https://gateway.test/v1/chat/completions")
    assert request.headers["authorization"] == f"Bearer {VIRTUAL_KEY_CANARY}"
    assert request.headers["x-metahub-contract-tenant-id"] == "default"
    payload = json.loads(request.content)
    model = payload["model"]
    stream = payload["stream"]
    headers = {
        "X-Request-ID": request.headers["x-request-id"],
        "X-Trace-ID": request.headers["x-trace-id"],
        "X-MetaHub-Contract-Tenant-ID": request.headers[
            "x-metahub-contract-tenant-id"
        ],
    }
    if not stream:
        usage_key = "usage" if model == "probe-deepseek" else "usage_metadata"
        response = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            usage_key: {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        }
        return httpx.Response(
            200,
            headers={**headers, "Content-Type": "application/json"},
            json=response,
            request=request,
        )
    if model == "probe-deepseek":
        body = (
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":2,'
            b'"completion_tokens":1,"total_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        )
        assert payload["stream_options"] == {"include_usage": True}
    else:
        body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        # Generic family 不能被 probe 客户端注入 OpenAI 私有 final usage 选项。
        assert "stream_options" not in payload
    return httpx.Response(
        200,
        headers={**headers, "Content-Type": "text/event-stream"},
        content=body,
        request=request,
    )


@pytest.fixture(scope="module")
def probe_report() -> ContractRunReport:
    """运行一次两 Provider、双模式真实探测报告，供上报与 CLI 测试复用。"""

    transport = httpx.MockTransport(_gateway_success_response)
    return asyncio.run(
        run_gateway_probe(
            config=_probe_config(),
            request_id="req_prd064_probe",
            trace_id="trc_prd064_probe",
            source_revision_hash="a" * 64,
            transport=transport,
        )
    )


def test_probe_config_reads_key_only_from_named_environment(tmp_path: Path) -> None:
    """配置流程：严格 JSON 只保存 Key 环境变量名，加载结果 repr 不含 Key 明文。"""

    path = _artifact_file(tmp_path, "probe-config.json")
    path.write_text(
        json.dumps(
            {
                "gateway_url": "http://127.0.0.1:8000",
                "tenant_id": "tenant:prd064",
                "provider_models": {"deepseek": "probe-deepseek"},
                "virtual_key_env": "METAHUB_PRD064_VIRTUAL_KEY",
                "stream": True,
                "timeout_seconds": 5,
                "allow_loopback_http": True,
            }
        ),
        encoding="utf-8",
    )

    config = load_gateway_probe_config(
        path,
        workspace_root=WORKSPACE_ROOT,
        environ={
            "METAHUB_CONTRACT_TRUSTED_GATEWAY_ORIGIN": "http://127.0.0.1:8000",
            "METAHUB_CONTRACT_TRUSTED_TENANT_ID": "tenant:prd064",
            "METAHUB_PRD064_VIRTUAL_KEY": VIRTUAL_KEY_CANARY,
        },
    )

    assert config.origin.base_url == "http://127.0.0.1:8000"
    assert config.tenant_id == "tenant:prd064"
    assert config.provider_models == (("deepseek", "probe-deepseek"),)
    assert VIRTUAL_KEY_CANARY not in repr(config)


def test_probe_config_rejects_duplicate_unknown_and_nonloopback_http(
    tmp_path: Path,
) -> None:
    """配置异常：重复 key、未知 Provider 与非 loopback HTTP 均固定失败且不启动网络。"""

    raw_configs = (
        '{"gateway_url":"https://gateway.test","tenant_id":"default",'
        '"tenant_id":"other","provider_models":{"deepseek":"alias"},'
        '"virtual_key_env":"METAHUB_PRD064_VIRTUAL_KEY"}',
        json.dumps(
            {
                "gateway_url": "https://gateway.test",
                "tenant_id": "default",
                "provider_models": {"openrouter": "alias"},
                "virtual_key_env": "METAHUB_PRD064_VIRTUAL_KEY",
            }
        ),
        json.dumps(
            {
                "gateway_url": "http://gateway.example",
                "tenant_id": "default",
                "provider_models": {"deepseek": "alias"},
                "virtual_key_env": "METAHUB_PRD064_VIRTUAL_KEY",
                "allow_loopback_http": True,
            }
        ),
        json.dumps(
            {
                "gateway_url": "https://gateway.test",
                "tenant_id": "default",
                "provider_models": {"deepseek": "alias"},
                "virtual_key_env": "AWS_SECRET_ACCESS_KEY",
            }
        ),
    )
    for index, raw in enumerate(raw_configs):
        path = _artifact_file(tmp_path, f"invalid-{index}.json")
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(ProbeConfigError) as caught:
            load_gateway_probe_config(
                path,
                workspace_root=WORKSPACE_ROOT,
                environ={
                    "METAHUB_CONTRACT_TRUSTED_GATEWAY_ORIGIN": "https://gateway.test",
                    "METAHUB_CONTRACT_TRUSTED_TENANT_ID": "default",
                    "METAHUB_PRD064_VIRTUAL_KEY": VIRTUAL_KEY_CANARY,
                    "AWS_SECRET_ACCESS_KEY": VIRTUAL_KEY_CANARY,
                },
            )
        assert caught.value.code == "adapter.contract_probe_config_required"


def test_probe_config_rejects_untrusted_origin_and_tenant_before_key_read(
    tmp_path: Path,
) -> None:
    """安全异常：JSON 目标或租户与独立受信配置不一致时，即使未提供 Key 也固定失败关闭。"""

    path = _artifact_file(tmp_path, "untrusted-probe-config.json")
    path.write_text(
        json.dumps(
            {
                "gateway_url": "https://provider-attacker.test",
                "tenant_id": "other-tenant",
                "provider_models": {"deepseek": "probe-deepseek"},
                "virtual_key_env": "METAHUB_PRD064_VIRTUAL_KEY",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProbeConfigError):
        load_gateway_probe_config(
            path,
            workspace_root=WORKSPACE_ROOT,
            environ={
                "METAHUB_CONTRACT_TRUSTED_GATEWAY_ORIGIN": "https://gateway.test",
                "METAHUB_CONTRACT_TRUSTED_TENANT_ID": "default",
            },
        )


def test_probe_config_rejects_tmp_lexically_before_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路径安全：`.TMP` 片段必须在 resolve/stat/read 前按大小写不敏感方式拒绝。"""

    def unexpected_resolve(*_args: object, **_kwargs: object) -> Path:
        """若被调用即证明词法门禁顺序回归，测试必须立即失败。"""

        # 该哨兵确保测试不会因目标恰好不存在而误判“已经在访问前拒绝”。
        raise AssertionError("resolve must not run for a lexically rejected path")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)
    with pytest.raises(ProbeConfigError):
        load_gateway_probe_config(
            Path("evidence/.TMP/probe.json"),
            workspace_root=Path("workspace"),
            environ={},
        )


def test_gateway_probe_covers_both_families_stream_and_usage_without_leaks(
    probe_report: ContractRunReport,
) -> None:
    """探测主流程：两类 Adapter 的非流式、SSE 与 usage 全部通过且报告保持脱敏。"""

    validate_report(probe_report)
    encoded = canonical_report_bytes(probe_report)

    assert probe_report.suite_kind == "probe"
    assert probe_report.mode == "gateway_probe"
    assert probe_report.coverage_complete is False
    assert probe_report.total_cases == 4
    assert probe_report.passed_cases == 4
    assert probe_report.failed_cases == 0
    assert {case.case_kind for case in probe_report.cases} == {"non_stream", "stream"}
    for forbidden in (
        VIRTUAL_KEY_CANARY.encode(),
        b"probe-custom",
        b"probe-deepseek",
        b"gateway.test",
        b"metahub-contract-probe",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize("status_code", (429, 503))
def test_gateway_probe_turns_429_and_5xx_into_failed_report(status_code: int) -> None:
    """异常流程：Gateway 429/5xx 不抛自由正文，而是生成可上报的固定 probe 失败报告。"""

    def rate_limited(request: httpx.Request) -> httpx.Response:
        """返回带 canary 正文的 429，证明探测报告不会保存错误文本。"""

        if request.url.path in {
            "/health",
            "/v1/adapter-contract-probes/identity",
        }:
            return _gateway_success_response(request)
        return httpx.Response(
            status_code,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": request.headers["x-request-id"],
                "X-Trace-ID": request.headers["x-trace-id"],
            },
            json={"error": {"message": VIRTUAL_KEY_CANARY}},
            request=request,
        )

    report = asyncio.run(
        run_gateway_probe(
            config=GatewayProbeConfig(
                origin=ServiceOrigin(
                    "https://gateway.test", "https", "gateway.test", 443
                ),
                tenant_id="default",
                provider_models=(("deepseek", "probe-deepseek"),),
                virtual_key_env="METAHUB_PRD064_VIRTUAL_KEY",
                virtual_key=VIRTUAL_KEY_CANARY,
                stream=False,
                timeout_seconds=5.0,
            ),
            request_id=f"req_prd064_probe_{status_code}",
            trace_id=f"trc_prd064_probe_{status_code}",
            source_revision_hash="b" * 64,
            transport=httpx.MockTransport(rate_limited),
        )
    )

    validate_report(report)
    assert report.status == "failed"
    assert report.failed_cases == 1
    assert report.failure_codes == ("adapter.contract_probe_failed",)
    assert VIRTUAL_KEY_CANARY.encode() not in canonical_report_bytes(report)


def test_gateway_probe_rejects_wrong_health_identity_before_sending_key() -> None:
    """安全流程：health 服务身份不符时固定配置失败，transport 永远看不到 Authorization。"""

    requests: list[httpx.Request] = []

    def provider_health(request: httpx.Request) -> httpx.Response:
        """模拟同源 Provider 伪装的健康页，并记录请求以证明未发生后续有 Key POST。"""

        requests.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": request.headers["x-request-id"],
                "X-Trace-ID": request.headers["x-trace-id"],
            },
            json={
                "status": "ok",
                "service_name": "provider-endpoint",
                "request_id": request.headers["x-request-id"],
                "trace_id": request.headers["x-trace-id"],
            },
            request=request,
        )

    with pytest.raises(ProbeConfigError):
        asyncio.run(
            run_gateway_probe(
                config=_probe_config(stream=False),
                request_id="req_prd064_wrong_health",
                trace_id="trc_prd064_wrong_health",
                source_revision_hash="c" * 64,
                transport=httpx.MockTransport(provider_health),
            )
        )

    assert len(requests) == 1
    assert requests[0].url.path == "/health"


def test_gateway_probe_rejects_wrong_tenant_owner_before_model_cases() -> None:
    """租户流程：Key owner 证明失败时退出配置流程，不生成可落盘的普通失败报告。"""

    requests: list[httpx.Request] = []

    def wrong_tenant(request: httpx.Request) -> httpx.Response:
        """先返回合法 health，再用统一 401 模拟 Virtual Key 实际属于其他租户。"""

        requests.append(request)
        if request.url.path == "/health":
            return _gateway_success_response(request)
        assert request.url.path == "/v1/adapter-contract-probes/identity"
        return httpx.Response(
            401,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
                "X-Request-ID": request.headers["x-request-id"],
                "X-Trace-ID": request.headers["x-trace-id"],
            },
            json={
                "code": "gateway.auth_invalid",
                "request_id": request.headers["x-request-id"],
                "trace_id": request.headers["x-trace-id"],
            },
            request=request,
        )

    with pytest.raises(ProbeConfigError):
        asyncio.run(
            run_gateway_probe(
                config=_probe_config(stream=False),
                request_id="req_prd064_wrong_tenant",
                trace_id="trc_prd064_wrong_tenant",
                source_revision_hash="d" * 64,
                transport=httpx.MockTransport(wrong_tenant),
            )
        )

    assert [request.url.path for request in requests] == [
        "/health",
        "/v1/adapter-contract-probes/identity",
    ]


def test_gateway_probe_rejects_duplicate_tenant_owner_proof_without_leak() -> None:
    """租户异常：identity 重复证明头不能择一通过，声明 canary 不进入固定异常。"""

    tenant_canary = "tenant-proof-sensitive-canary-prd064"
    requests: list[httpx.Request] = []

    def duplicate_proof(request: httpx.Request) -> httpx.Response:
        """先返回合法 health，再模拟同时回送可信值与攻击者值的重复租户证明头。"""

        requests.append(request)
        if request.url.path == "/health":
            return _gateway_success_response(request)
        assert request.url.path == "/v1/adapter-contract-probes/identity"
        return httpx.Response(
            200,
            headers=[
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-store"),
                ("X-Request-ID", request.headers["x-request-id"]),
                ("X-Trace-ID", request.headers["x-trace-id"]),
                ("X-MetaHub-Contract-Tenant-ID", "default"),
                ("X-MetaHub-Contract-Tenant-ID", tenant_canary),
            ],
            json={
                "status": "ok",
                "service_name": "gateway-data-plane",
                "request_id": request.headers["x-request-id"],
                "trace_id": request.headers["x-trace-id"],
            },
            request=request,
        )

    with pytest.raises(ProbeConfigError) as caught:
        asyncio.run(
            run_gateway_probe(
                config=_probe_config(stream=False),
                request_id="req_prd064_duplicate_tenant_proof",
                trace_id="trc_prd064_duplicate_tenant_proof",
                source_revision_hash="e" * 64,
                transport=httpx.MockTransport(duplicate_proof),
            )
        )

    assert [request.url.path for request in requests] == [
        "/health",
        "/v1/adapter-contract-probes/identity",
    ]
    assert str(caught.value) == "adapter.contract_probe_config_required"
    assert tenant_canary not in str(caught.value)


def test_gateway_probe_rejects_key_fragment_in_link_id_before_health() -> None:
    """脱敏流程：request_id 含 Key 可复用片段时，连无凭据 health 日志也不得产生。"""

    def unexpected_network(_request: httpx.Request) -> httpx.Response:
        """任何 transport 调用都表示凭据片段已可能进入服务日志。"""

        # 链路碰撞必须先于 health；哨兵不返回伪响应，避免错误实现继续执行。
        raise AssertionError("network must not run for a credential-derived link id")

    with pytest.raises(ProbeConfigError):
        asyncio.run(
            run_gateway_probe(
                config=_probe_config(stream=False),
                request_id="req_prd064_probe_secret",
                trace_id="trc_prd064_probe_secret",
                source_revision_hash="e" * 64,
                transport=httpx.MockTransport(unexpected_network),
            )
        )


def test_control_upload_config_requires_named_token_and_explicit_loopback() -> None:
    """上报配置：内部 Token 只从环境读取，明文 HTTP 仅在显式 loopback 条件下允许。"""

    config = build_control_upload_config(
        "http://127.0.0.1:8001",
        token_env="METAHUB_INTERNAL_CONFIG_TOKEN",
        allow_loopback_http=True,
        environ={
            "METAHUB_CONTRACT_TRUSTED_CONTROL_ORIGIN": "http://127.0.0.1:8001",
            "METAHUB_CONTRACT_TRUSTED_TENANT_ID": "default",
            "METAHUB_INTERNAL_CONFIG_TOKEN": INTERNAL_TOKEN_CANARY,
        },
    )

    assert config.origin.base_url == "http://127.0.0.1:8001"
    assert INTERNAL_TOKEN_CANARY not in repr(config)
    with pytest.raises(ControlUploadError):
        build_control_upload_config(
            "http://127.0.0.1:8001",
            token_env="METAHUB_OTHER_SECRET",
            allow_loopback_http=True,
            environ={
                "METAHUB_CONTRACT_TRUSTED_CONTROL_ORIGIN": "http://127.0.0.1:8001",
                "METAHUB_CONTRACT_TRUSTED_TENANT_ID": "default",
                "METAHUB_OTHER_SECRET": INTERNAL_TOKEN_CANARY,
            },
        )
    with pytest.raises(ControlUploadError):
        build_control_upload_config(
            "http://control.example",
            token_env="METAHUB_INTERNAL_CONFIG_TOKEN",
            allow_loopback_http=True,
            environ={
                "METAHUB_CONTRACT_TRUSTED_CONTROL_ORIGIN": "http://127.0.0.1:8001",
                "METAHUB_CONTRACT_TRUSTED_TENANT_ID": "default",
                "METAHUB_INTERNAL_CONFIG_TOKEN": INTERNAL_TOKEN_CANARY,
            },
        )

    # 合法 HTTPS 也不能绕过独立受信 origin；校验发生在 Token 读取之前。
    with pytest.raises(ControlUploadError):
        build_control_upload_config(
            "https://control-attacker.test",
            token_env="METAHUB_INTERNAL_CONFIG_TOKEN",
            environ={
                "METAHUB_CONTRACT_TRUSTED_CONTROL_ORIGIN": "https://control.test",
                "METAHUB_CONTRACT_TRUSTED_TENANT_ID": "default",
            },
        )


def test_control_uploader_sends_canonical_report_and_validates_ack(
    probe_report: ContractRunReport,
) -> None:
    """上报主流程：Token、链路与 canonical body 精确发送，并核对 run/hash/no-store 确认。"""

    def acknowledge(request: httpx.Request) -> httpx.Response:
        """验证内部请求安全字段后返回与 Control DTO 同形的最小确认。"""

        if request.url.path == "/health":
            assert "x-internal-token" not in request.headers
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "X-Request-ID": probe_report.request_id,
                    "X-Trace-ID": probe_report.trace_id,
                },
                json={
                    "status": "ok",
                    "service_name": "control-plane-api",
                    "environment": "test",
                    "request_id": probe_report.request_id,
                    "trace_id": probe_report.trace_id,
                },
                request=request,
            )
        assert request.url.path == "/internal/tenants/default/adapter-contract-runs"
        assert request.headers["x-internal-token"] == INTERNAL_TOKEN_CANARY
        assert request.headers["x-request-id"] == probe_report.request_id
        assert request.headers["x-trace-id"] == probe_report.trace_id
        assert request.content == canonical_report_bytes(probe_report)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
                "X-Request-ID": probe_report.request_id,
                "X-Trace-ID": probe_report.trace_id,
            },
            json={
                "adapter_contract_run": {
                    "adapter_contract_run_id": probe_report.run_id,
                    "tenant_id": probe_report.tenant_id,
                    "report_hash": report_hash(probe_report),
                },
                "replayed": False,
                "request_id": probe_report.request_id,
                "trace_id": probe_report.trace_id,
            },
            request=request,
        )

    result = upload_report(
        probe_report,
        ControlUploadConfig(
            origin=ServiceOrigin("https://control.test", "https", "control.test", 443),
            tenant_id="default",
            internal_token_env="METAHUB_INTERNAL_CONFIG_TOKEN",
            internal_token=INTERNAL_TOKEN_CANARY,
        ),
        transport=httpx.MockTransport(acknowledge),
    )

    assert result.run_id == probe_report.run_id
    assert result.replayed is False


def test_control_uploader_rejects_error_response_without_leaking_body(
    probe_report: ContractRunReport,
) -> None:
    """上报异常：Control 5xx 与自由 canary 正文只映射为固定 upload error。"""

    def unavailable(request: httpx.Request) -> httpx.Response:
        """返回带敏感 canary 的服务错误，供脱敏异常边界验证。"""

        if request.url.path == "/health":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "X-Request-ID": probe_report.request_id,
                    "X-Trace-ID": probe_report.trace_id,
                },
                json={
                    "status": "ok",
                    "service_name": "control-plane-api",
                    "request_id": probe_report.request_id,
                    "trace_id": probe_report.trace_id,
                },
                request=request,
            )
        return httpx.Response(
            503,
            headers={"Content-Type": "application/json"},
            json={"error": {"message": INTERNAL_TOKEN_CANARY}},
            request=request,
        )

    with pytest.raises(ControlUploadError) as caught:
        upload_report(
            probe_report,
            ControlUploadConfig(
                origin=ServiceOrigin(
                    "https://control.test", "https", "control.test", 443
                ),
                tenant_id="default",
                internal_token_env="METAHUB_INTERNAL_CONFIG_TOKEN",
                internal_token=INTERNAL_TOKEN_CANARY,
            ),
            transport=httpx.MockTransport(unavailable),
        )

    assert str(caught.value) == "adapter.contract_upload_failed"
    assert INTERNAL_TOKEN_CANARY not in str(caught.value)


def test_control_uploader_rejects_wrong_health_before_sending_token(
    probe_report: ContractRunReport,
) -> None:
    """安全流程：错误 Control 身份在无凭据 health 阶段失败，内部 Token 不会进入 transport。"""

    requests: list[httpx.Request] = []

    def wrong_control(request: httpx.Request) -> httpx.Response:
        """返回错误服务身份并断言预检请求没有内部鉴权头。"""

        requests.append(request)
        assert "x-internal-token" not in request.headers
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": probe_report.request_id,
                "X-Trace-ID": probe_report.trace_id,
            },
            json={
                "status": "ok",
                "service_name": "gateway-data-plane",
                "request_id": probe_report.request_id,
                "trace_id": probe_report.trace_id,
            },
            request=request,
        )

    with pytest.raises(ControlUploadError):
        upload_report(
            probe_report,
            ControlUploadConfig(
                origin=ServiceOrigin(
                    "https://control.test", "https", "control.test", 443
                ),
                tenant_id="default",
                internal_token_env="METAHUB_INTERNAL_CONFIG_TOKEN",
                internal_token=INTERNAL_TOKEN_CANARY,
            ),
            transport=httpx.MockTransport(wrong_control),
        )

    assert len(requests) == 1
    assert requests[0].url.path == "/health"


def test_control_uploader_rejects_token_fragment_before_health(
    probe_report: ContractRunReport,
) -> None:
    """脱敏流程：报告链路 ID 含内部 Token 片段时，不得先把该 ID 发送到 health。"""

    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        """记录意外网络调用；正常实现不会进入该函数。"""

        requests.append(request)
        # 任何 health 调用都已违反“凭据片段不得进入无认证日志”的前置边界。
        raise AssertionError("health must not receive a credential-derived link id")

    unsafe_report = replace(
        probe_report,
        request_id="req_prd064-control-internal",
    )
    with pytest.raises(ControlUploadError):
        upload_report(
            unsafe_report,
            ControlUploadConfig(
                origin=ServiceOrigin(
                    "https://control.test", "https", "control.test", 443
                ),
                tenant_id="default",
                internal_token_env="METAHUB_INTERNAL_CONFIG_TOKEN",
                internal_token=INTERNAL_TOKEN_CANARY,
            ),
            transport=httpx.MockTransport(capture),
        )

    assert requests == []


def test_cli_gateway_probe_writes_report_and_returns_zero(
    probe_report: ContractRunReport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 探测流程：显式配置进入 probe runner，报告先落盘并返回安全成功摘要。"""

    config_path = _artifact_file(tmp_path, "cli-probe-config.json")
    config_path.write_text(
        json.dumps(
            {
                "gateway_url": "https://gateway.test",
                "tenant_id": "default",
                "provider_models": {"deepseek": "probe-deepseek"},
                "virtual_key_env": "METAHUB_PRD064_VIRTUAL_KEY",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("METAHUB_PRD064_VIRTUAL_KEY", VIRTUAL_KEY_CANARY)
    monkeypatch.setenv(
        "METAHUB_CONTRACT_TRUSTED_GATEWAY_ORIGIN", "https://gateway.test"
    )
    monkeypatch.setenv("METAHUB_CONTRACT_TRUSTED_TENANT_ID", "default")
    monkeypatch.delenv("METAHUB_CONTRACT_CONTROL_URL", raising=False)
    monkeypatch.setattr(cli_module, "contract_source_revision_hash", lambda: "a" * 64)
    monkeypatch.setattr(
        cli_module,
        "run_gateway_probe_sync",
        lambda **_kwargs: probe_report,
    )
    output = _artifact_file(tmp_path, "cli-probe-report.json")

    exit_code = cli_module.main(
        [
            "--mode",
            "gateway_probe",
            "--probe-config",
            str(config_path),
            "--workspace-root",
            str(WORKSPACE_ROOT),
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert exit_code == 0
    assert output.exists()
    assert summary["suite_kind"] == "probe"
    assert summary["uploaded"] is False
    assert captured.err == ""


def test_cli_returns_four_when_passed_report_upload_fails(
    probe_report: ContractRunReport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 上报异常：通过报告保留在 `.artifacts`，强制上报失败以退出码 4 阻断 CI。"""

    upload_config = ControlUploadConfig(
        origin=ServiceOrigin("https://control.test", "https", "control.test", 443),
        tenant_id="default",
        internal_token_env="METAHUB_INTERNAL_CONFIG_TOKEN",
        internal_token=INTERNAL_TOKEN_CANARY,
    )
    monkeypatch.setattr(cli_module, "run_merge_gate_sync", lambda **_kwargs: probe_report)
    monkeypatch.setattr(cli_module, "contract_source_revision_hash", lambda: "a" * 64)
    monkeypatch.setattr(
        cli_module,
        "build_control_upload_config",
        lambda *_args, **_kwargs: upload_config,
    )

    def reject_upload(*_args: object, **_kwargs: object) -> object:
        """模拟固定 Control 上报失败，不把 Token 或 URL 放入异常。"""

        # 该哨兵只验证 CLI 退出优先级，异常本身不携带配置或报告内容。
        raise ControlUploadError

    monkeypatch.setattr(cli_module, "upload_report", reject_upload)
    output = _artifact_file(tmp_path, "cli-upload-failed-report.json")
    exit_code = cli_module.main(
        [
            "--mode",
            "mock",
            "--tenant-id",
            "default",
            "--control-url",
            "https://control.test",
            "--workspace-root",
            str(WORKSPACE_ROOT),
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 4
    assert output.exists()
    assert json.loads(captured.out)["uploaded"] is False
    assert captured.err.strip() == "adapter.contract_upload_failed"
    assert INTERNAL_TOKEN_CANARY not in captured.out + captured.err
