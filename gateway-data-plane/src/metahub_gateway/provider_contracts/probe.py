"""PRD-064 显式 Gateway 真实探测执行器。

模块职责：使用租户 Virtual Key 调用 MetaHub Gateway 的 OpenAI-compatible 入口，并生成
``suite_kind=probe`` 的安全报告。维护边界：禁止直连 Provider URL、跟随重定向、使用系统
代理或把 Key、模型别名、请求/响应正文写入报告和日志。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import httpx
from metahub_shared.runtime.provider_adapter_spec import (
    CATALOG_HASH,
    CATALOG_VERSION,
    ProviderAdapterSpec,
    ProviderAdapterSpecError,
    provider_adapter_spec,
)

from .models import ContractCaseResult, ContractRunReport
from .network import (
    NetworkBoundaryError,
    ServiceOrigin,
    TRUSTED_GATEWAY_ORIGIN_ENV,
    TRUSTED_TENANT_ID_ENV,
    identifier_exposes_secret,
    normalize_service_origin,
    response_is_same_origin,
    service_origins_match,
)
from .runner import REPORT_SCHEMA_VERSION, RUNNER_VERSION


# 配置和响应上限用于约束人工探测输入，避免 CI 因错误文件或上游无限正文耗尽内存。
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_PROVIDER_MODELS = 32
_TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MODEL_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,127}\Z")
_REQUEST_ID = re.compile(r"req_[A-Za-z0-9][A-Za-z0-9._:-]{0,155}\Z")
_TRACE_ID = re.compile(r"trc_[A-Za-z0-9][A-Za-z0-9._:-]{0,155}\Z")
_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
# 专用租户头由真实 Gateway 在 Virtual Key 鉴权后核对并回送，用于证明报告 owner 不是客户端自报。
_CONTRACT_TENANT_HEADER = "X-MetaHub-Contract-Tenant-ID"
_CONTRACT_IDENTITY_PATH = "/v1/adapter-contract-probes/identity"
_CONFIG_FIELDS = frozenset(
    {
        "gateway_url",
        "tenant_id",
        "provider_models",
        "virtual_key_env",
        "stream",
        "timeout_seconds",
        "allow_loopback_http",
    }
)
_REQUIRED_CONFIG_FIELDS = frozenset(
    {"gateway_url", "tenant_id", "provider_models", "virtual_key_env"}
)
_USAGE_ROOTS = ("usage", "usage_metadata", "usageMetadata")
_USAGE_TOKEN_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "cached_tokens",
        "promptTokenCount",
        "candidatesTokenCount",
        "totalTokenCount",
    }
)


class ProbeConfigError(ValueError):
    """表示真实探测配置或 Virtual Key 不满足固定安全边界。"""

    code = "adapter.contract_probe_config_required"

    def __init__(self) -> None:
        """构造固定配置错误，不保存文件路径、URL、环境变量值或 Key。"""

        super().__init__(self.code)


class _DuplicateJsonKey(ValueError):
    """标记配置中的重复 JSON key；异常不保存不可信字段名。"""


class _ProbeExecutionError(RuntimeError):
    """标记单个 Gateway 请求合同失败；正文固定且不会进入报告。"""


@dataclass(frozen=True)
class GatewayProbeConfig:
    """保存已经严格验证的 Gateway 探测配置。

    ``virtual_key`` 只来自进程环境且隐藏 repr；provider_models 使用排序 tuple 保持执行顺序与
    报告稳定，模型别名仅进入瞬时请求，不进入 case 结果。
    """

    origin: ServiceOrigin
    tenant_id: str
    provider_models: tuple[tuple[str, str], ...]
    virtual_key_env: str
    virtual_key: str = field(repr=False)
    stream: bool
    timeout_seconds: float


def _fail_config() -> NoReturn:
    """统一抛出不含配置原值的 probe 配置错误。"""

    # 所有配置分支共享一个固定错误，避免 CI stderr 暴露路径、URL 或环境变量名。
    raise ProbeConfigError


def load_gateway_probe_config(
    path: Path,
    *,
    workspace_root: Path,
    environ: Mapping[str, str] | None = None,
) -> GatewayProbeConfig:
    """从工作区内文件读取严格 Gateway probe 配置。

    参数：配置路径、工作区根和可注入环境映射。返回：已绑定环境 Key 的不可变配置。
    约束：解析前先做路径归属和字节上限校验，重复/未知字段与缺 Key 均固定失败。
    """

    # 用户明确禁止任何 `.tmp` 访问；必须先做纯词法判断，不能先 resolve/stat 再补救。
    if any(part.lower() == ".tmp" for part in path.parts):
        _fail_config()
    try:
        root = workspace_root.resolve(strict=True)
        target = path.resolve(strict=True)
    # 缺失、不可读或循环链接路径都只属于 probe 配置错误，不能进入通用报告错误分支。
    except OSError:
        _fail_config()
    try:
        relative = target.relative_to(root)
    # 配置解析只能读取工作区内文件，越界路径在 read/stat 前立即关闭。
    except ValueError:
        _fail_config()
    if any(part.lower() == ".tmp" for part in relative.parts) or not target.is_file():
        # 禁止临时链接树和非普通文件成为配置来源，避免越界读取或不稳定验收证据。
        _fail_config()
    try:
        size = target.stat().st_size
    # stat 权限或链接竞态不得穿透机器路径，统一按配置不可用处理。
    except OSError:
        _fail_config()
    if not 1 <= size <= _MAX_CONFIG_BYTES:
        _fail_config()
    try:
        payload = json.loads(
            target.read_text(encoding="utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
        ValueError,
    ):
        # JSON 异常可能引用配置片段或路径，切断异常链后只保留固定 probe code。
        _fail_config()
    if type(payload) is not dict:
        _fail_config()
    keys = frozenset(payload)
    if not _REQUIRED_CONFIG_FIELDS <= keys or not keys <= _CONFIG_FIELDS:
        _fail_config()

    tenant_id = payload.get("tenant_id")
    provider_models = payload.get("provider_models")
    virtual_key_env = payload.get("virtual_key_env")
    stream = payload.get("stream", False)
    timeout_value = payload.get("timeout_seconds", 15)
    allow_loopback_http = payload.get("allow_loopback_http", False)
    if (
        not isinstance(tenant_id, str)
        or not _TENANT_ID.fullmatch(tenant_id)
        or type(provider_models) is not dict
        or not 1 <= len(provider_models) <= _MAX_PROVIDER_MODELS
        or not isinstance(virtual_key_env, str)
        or not _ENV_NAME.fullmatch(virtual_key_env)
        or not virtual_key_env.startswith("METAHUB_")
        or "VIRTUAL_KEY" not in virtual_key_env
        or type(stream) is not bool
        or type(allow_loopback_http) is not bool
        or type(timeout_value) not in {int, float}
        or not 1 <= float(timeout_value) <= 30
    ):
        _fail_config()
    environment = environ if environ is not None else os.environ
    try:
        origin = normalize_service_origin(
            payload.get("gateway_url"),
            allow_loopback_http=allow_loopback_http,
        )
        trusted_origin = normalize_service_origin(
            environment.get(TRUSTED_GATEWAY_ORIGIN_ENV),
            allow_loopback_http=allow_loopback_http,
        )
    # 网络边界错误不回显内部 Gateway origin，只映射为 probe 配置失败。
    except NetworkBoundaryError:
        _fail_config()
    if (
        not service_origins_match(origin, trusted_origin)
        or environment.get(TRUSTED_TENANT_ID_ENV) != tenant_id
    ):
        # JSON 目标不能自行证明其是 MetaHub Gateway；独立受信 origin/tenant 漂移时不得读取 Key。
        _fail_config()

    normalized_models: list[tuple[str, str]] = []
    for provider_type, model_alias in provider_models.items():
        if (
            not isinstance(provider_type, str)
            or not isinstance(model_alias, str)
            or not _MODEL_ALIAS.fullmatch(model_alias)
        ):
            _fail_config()
        try:
            provider_adapter_spec(provider_type)
        # 未知 Provider 和竞品名称不能回退到 Generic Adapter 或任意 URL 探测。
        except ProviderAdapterSpecError:
            _fail_config()
        normalized_models.append((provider_type, model_alias))
    normalized_models.sort(key=lambda item: item[0])

    virtual_key = environment.get(virtual_key_env)
    if (
        not isinstance(virtual_key, str)
        or not 8 <= len(virtual_key) <= 512
        or any(character.isspace() or ord(character) < 32 for character in virtual_key)
    ):
        # Key 缺失或含控制字符时不得启动网络，也不能把环境变量名或值打印到 stderr。
        _fail_config()
    return GatewayProbeConfig(
        origin=origin,
        tenant_id=tenant_id,
        provider_models=tuple(normalized_models),
        virtual_key_env=virtual_key_env,
        virtual_key=virtual_key,
        stream=stream,
        timeout_seconds=float(timeout_value),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON 对象并拒绝任意层重复 key，不把 key 原文写入异常。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    """拒绝 NaN/Infinity，保证配置在不同 JSON 解析器间只有一种语义。"""

    # JSON 标准之外的数值会破坏跨语言 canonical 语义，必须在模型验证前拒绝。
    raise ValueError


def _utc_text() -> str:
    """返回无微秒 canonical UTC 秒文本，供报告与数据库稳定排序。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_hash(value: Mapping[str, object]) -> str:
    """对只含固定断言事实的映射计算 canonical SHA-256。"""

    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _case_request_id(report_request_id: str, case_key: str) -> str:
    """为每个 Gateway 请求派生唯一安全 request_id，同时保留报告级关联。"""

    digest = hashlib.sha256(f"{report_request_id}:{case_key}".encode("utf-8")).hexdigest()
    return f"req_{digest[:32]}"


def _request_payload(
    model_alias: str,
    spec: ProviderAdapterSpec,
    *,
    stream: bool,
) -> dict[str, object]:
    """构造只发往 Gateway 的合成请求，按 spec 决定是否请求 OpenAI final usage。"""

    payload: dict[str, object] = {
        "model": model_alias,
        "messages": [{"role": "user", "content": "metahub-contract-probe"}],
        "stream": stream,
    }
    # Generic Adapter 不能收到 OpenAI 私有选项；这里只对冻结 OpenAI profile 注入。
    if stream and spec.stream_profile.allow_include_usage:
        payload["stream_options"] = {"include_usage": True}
    return payload


async def _read_bounded(response: httpx.Response) -> bytes:
    """有界读取响应并关闭连接，Content-Length 和实际分块均不得超过 256 KiB。"""

    raw_length = response.headers.get("content-length")
    if raw_length is not None:
        # 非 canonical 长度可能造成代理与客户端读取边界分歧，不能继续消费正文。
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise _ProbeExecutionError
        # 声明超限时在读取首个分块前失败，避免不必要的内存与带宽消耗。
        if int(raw_length) > _MAX_RESPONSE_BYTES:
            raise _ProbeExecutionError
    body = bytearray()
    async for chunk in response.aiter_bytes():
        # 分块传输不能绕过 Content-Length 上限，实际累计值同样逐块检查。
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise _ProbeExecutionError
        body.extend(chunk)
    return bytes(body)


def _strict_json_object(body: bytes) -> Mapping[str, object]:
    """解析有界 UTF-8 JSON object，拒绝重复 key、非有限数和数组根。"""

    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
        ValueError,
    ):
        # 解析异常可能引用响应片段，固定转换后不保留原异常链。
        raise _ProbeExecutionError from None
    # 探测只接受 JSON object；数组或标量无法证明 OpenAI-compatible 响应合同。
    if type(value) is not dict:
        raise _ProbeExecutionError
    return value


def _usage_present(payload: Mapping[str, object]) -> bool:
    """判断响应是否含至少一个非负 Token 数值，不返回或保留 raw usage。"""

    candidates: list[object] = [payload.get(root) for root in _USAGE_ROOTS]
    output = payload.get("output")
    if isinstance(output, Mapping):
        candidates.append(output.get("usage"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if any(
            key in _USAGE_TOKEN_FIELDS
            and type(value) is int
            and value >= 0
            for key, value in candidate.items()
        ):
            return True
    return False


def _stream_facts(body: bytes) -> tuple[bool, bool]:
    """从有界 SSE 提取 `[DONE]` 与 usage 布尔事实，不保留 chunk 正文。"""

    has_done = False
    has_usage = False
    for raw_line in body.splitlines():
        if not raw_line.startswith(b"data:"):
            continue
        data = raw_line[5:].strip()
        if data == b"[DONE]":
            has_done = True
            continue
        if not data:
            continue
        event = _strict_json_object(data)
        has_usage = has_usage or _usage_present(event)
    return has_done, has_usage


async def _verify_gateway_identity(
    config: GatewayProbeConfig,
    *,
    request_id: str,
    trace_id: str,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """在发送 Virtual Key 前无凭据验证同源目标确实是 MetaHub Gateway。

    参数：严格配置、报告链路标识和仅供测试注入的 transport。返回：成功时无返回值。
    状态、服务身份、媒体类型或链路字段任一漂移都固定失败，响应正文不会进入异常或报告。
    """

    endpoint = f"{config.origin.base_url}/health"
    headers = {"X-Request-ID": request_id, "X-Trace-ID": trace_id}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(config.timeout_seconds),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        async with client.stream("GET", endpoint, headers=headers) as response:
            body = await _read_bounded(response)
            media_type = response.headers.get("content-type", "").partition(";")[0]
            if (
                response.status_code != 200
                or response.is_redirect
                or not response_is_same_origin(config.origin, response.url)
                or media_type.lower() != "application/json"
                or response.headers.get("x-request-id") != request_id
                or response.headers.get("x-trace-id") != trace_id
            ):
                # 身份预检不携带 Key；任何 HTTP 合同漂移都必须阻止后续有凭据 POST。
                raise _ProbeExecutionError
    payload = _strict_json_object(body)
    if (
        payload.get("status") != "ok"
        or payload.get("service_name") != "gateway-data-plane"
        or payload.get("request_id") != request_id
        or payload.get("trace_id") != trace_id
    ):
        # Provider 或错误端口即使伪装成 200 JSON，也不能通过服务身份与链路正文核对。
        raise _ProbeExecutionError


async def _verify_gateway_tenant_owner(
    config: GatewayProbeConfig,
    *,
    request_id: str,
    trace_id: str,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """使用 Virtual Key 的实际鉴权结果证明 probe 报告租户归属。

    参数：严格配置、报告链路标识和仅供测试注入的 transport。返回：证明通过时无返回值。
    该请求只在无凭据 health 成功后发出；失败会在报告生成前转成配置错误，禁止把错租户
    Key 形成普通失败报告并上报到配置租户。
    """

    identity_request_id = _case_request_id(request_id, "tenant-owner")
    endpoint = f"{config.origin.base_url}{_CONTRACT_IDENTITY_PATH}"
    headers = {
        "Authorization": f"Bearer {config.virtual_key}",
        _CONTRACT_TENANT_HEADER: config.tenant_id,
        "X-Request-ID": identity_request_id,
        "X-Trace-ID": trace_id,
    }
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(config.timeout_seconds),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        async with client.stream("POST", endpoint, headers=headers) as response:
            body = await _read_bounded(response)
            media_type = response.headers.get("content-type", "").partition(";")[0]
            if (
                response.status_code != 200
                or response.is_redirect
                or not response_is_same_origin(config.origin, response.url)
                or media_type.lower() != "application/json"
                or response.headers.get("x-request-id") != identity_request_id
                or response.headers.get("x-trace-id") != trace_id
                or response.headers.get_list(_CONTRACT_TENANT_HEADER)
                != [config.tenant_id]
            ):
                # HTTP、链路或唯一证明头任一不一致，都不能生成带配置租户的报告。
                raise _ProbeExecutionError
    payload = _strict_json_object(body)
    if (
        payload.get("status") != "ok"
        or payload.get("service_name") != "gateway-data-plane"
        or payload.get("request_id") != identity_request_id
        or payload.get("trace_id") != trace_id
    ):
        # 正文身份必须与响应头交叉一致，禁止代理只伪造其中一个通道。
        raise _ProbeExecutionError


async def _execute_probe_case(
    config: GatewayProbeConfig,
    spec: ProviderAdapterSpec,
    model_alias: str,
    *,
    stream: bool,
    report_request_id: str,
    trace_id: str,
    run_id: str,
    transport: httpx.AsyncBaseTransport | None,
) -> ContractCaseResult:
    """执行一个非流式或流式 Gateway case，并把任何自由失败折叠为固定结果。"""

    case_kind = "stream" if stream else "non_stream"
    case_key = f"{spec.contract_case_id}:{case_kind}:success"
    case_request_id = _case_request_id(report_request_id, case_key)
    started = time.perf_counter()
    status = "failed"
    failure_code: str | None = "adapter.contract_probe_failed"
    evidence: Mapping[str, object] = {"passed": False, "stream": stream}
    try:
        headers = {
            "Authorization": f"Bearer {config.virtual_key}",
            "Content-Type": "application/json",
            _CONTRACT_TENANT_HEADER: config.tenant_id,
            "X-Request-ID": case_request_id,
            "X-Trace-ID": trace_id,
        }
        endpoint = f"{config.origin.base_url}/v1/chat/completions"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            async with client.stream(
                "POST",
                endpoint,
                headers=headers,
                json=_request_payload(model_alias, spec, stream=stream),
            ) as response:
                body = await _read_bounded(response)
                if (
                    response.status_code != 200
                    or response.is_redirect
                    or not response_is_same_origin(config.origin, response.url)
                    or response.headers.get("x-request-id") != case_request_id
                    or response.headers.get("x-trace-id") != trace_id
                    or response.headers.get_list(_CONTRACT_TENANT_HEADER)
                    != [config.tenant_id]
                ):
                    # 状态、同源、链路或鉴权态租户证明任一漂移都表示治理链未被完整证明。
                    raise _ProbeExecutionError
                media_type = response.headers.get("content-type", "").partition(";")[0]
                if stream:
                    # 流式入口必须明确返回 SSE，普通 JSON 200 不能冒充流成功。
                    if media_type.lower() != "text/event-stream":
                        raise _ProbeExecutionError
                    has_done, has_usage = _stream_facts(body)
                    # 终止标记由冻结 Adapter profile 决定，Generic 不得套用 OpenAI 断言。
                    if bool(spec.stream_profile.termination_marker) is not has_done:
                        raise _ProbeExecutionError
                    # OpenAI profile 已显式请求 final usage，缺失时不能把成本链标为可验收。
                    if spec.stream_profile.final_usage_expected and not has_usage:
                        raise _ProbeExecutionError
                else:
                    # 非流式入口必须返回 JSON，HTML 或 SSE 正文即使状态 200 也失败关闭。
                    if media_type.lower() != "application/json":
                        raise _ProbeExecutionError
                    payload = _strict_json_object(body)
                    choices = payload.get("choices")
                    has_usage = _usage_present(payload)
                    # choices 与 usage 同时证明响应和计量主链，缺任一项都不能通过真实探测。
                    if not isinstance(choices, list) or not has_usage:
                        raise _ProbeExecutionError
        status = "passed"
        failure_code = None
        evidence = {"passed": True, "stream": stream, "usage": has_usage}
    # URL、Key、模型别名、响应正文和底层异常都不能进入报告或 stderr，只保留固定失败码。
    except (
        _ProbeExecutionError,
        httpx.HTTPError,
        KeyError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        pass
    duration_ms = min(max(int((time.perf_counter() - started) * 1000), 0), 300_000)
    evidence_hash = _safe_hash(
        {
            "case_key": case_key,
            "status": status,
            "failure_code": failure_code,
            "assertions": evidence if status == "passed" else {"passed": False},
        }
    )
    return ContractCaseResult(
        case_key=case_key,
        adapter_contract_case_id=(
            "acc_"
            + hashlib.sha256(f"{run_id}:{case_key}".encode("utf-8")).hexdigest()[:32]
        ),
        contract_case_id=spec.contract_case_id,
        tenant_id=config.tenant_id,
        spec_id=spec.spec_id,
        provider_type=spec.provider_type,
        adapter_name=spec.adapter_name,
        adapter_version=spec.adapter_version,
        case_kind=case_kind,
        variant="success",
        auth_type=None,
        status=status,
        failure_code=failure_code,
        duration_ms=duration_ms,
        evidence_hash=evidence_hash,
    )


async def run_gateway_probe(
    *,
    config: GatewayProbeConfig,
    request_id: str,
    trace_id: str,
    source_revision_hash: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ContractRunReport:
    """执行显式 Provider 子集的 Gateway 探测并返回非覆盖型安全报告。

    参数：严格配置、报告链路 ID、制品摘要和仅供测试注入的 transport。返回：probe 报告。
    每个 case 独立关闭连接并继续收集失败，任何通过都不能冒充 merge gate exact-set。
    """

    # 非标准链路 ID 或源码摘要必须在首个网络请求前失败，避免生成不可上报报告。
    if (
        not _REQUEST_ID.fullmatch(request_id)
        or not _TRACE_ID.fullmatch(trace_id)
        or not _LOWER_HEX_64.fullmatch(source_revision_hash)
        or not _ENV_NAME.fullmatch(config.virtual_key_env)
        or not config.virtual_key_env.startswith("METAHUB_")
        or "VIRTUAL_KEY" not in config.virtual_key_env
        or identifier_exposes_secret(request_id, config.virtual_key)
        or identifier_exposes_secret(trace_id, config.virtual_key)
    ):
        # 多行正则判断统一收敛到固定配置码，不能回显具体不合规链路字段。
        raise ProbeConfigError
    started_at = _utc_text()
    run_id = f"acr_{uuid4().hex}"
    results: list[ContractCaseResult] = []
    try:
        await _verify_gateway_identity(
            config,
            request_id=request_id,
            trace_id=trace_id,
            transport=transport,
        )
        await _verify_gateway_tenant_owner(
            config,
            request_id=request_id,
            trace_id=trace_id,
            transport=transport,
        )
    # 健康身份预检失败时不能生成部分报告或进入携 Key case，CLI 统一返回配置退出码 3。
    except (_ProbeExecutionError, httpx.HTTPError, RuntimeError, TypeError, ValueError):
        raise ProbeConfigError from None
    for provider_type, model_alias in config.provider_models:
        spec = provider_adapter_spec(provider_type)
        results.append(
            await _execute_probe_case(
                config,
                spec,
                model_alias,
                stream=False,
                report_request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                transport=transport,
            )
        )
        if config.stream:
            # stream 是显式附加 case；关闭时仍保留每个 Provider 的非流式健康事实。
            results.append(
                await _execute_probe_case(
                    config,
                    spec,
                    model_alias,
                    stream=True,
                    report_request_id=request_id,
                    trace_id=trace_id,
                    run_id=run_id,
                    transport=transport,
                )
            )
    results.sort(key=lambda item: item.case_key)
    passed = sum(result.status == "passed" for result in results)
    failed = len(results) - passed
    failure_codes = tuple(
        sorted(
            {
                result.failure_code
                for result in results
                if result.failure_code is not None
            }
        )
    )
    return ContractRunReport(
        schema_version=REPORT_SCHEMA_VERSION,
        tenant_id=config.tenant_id,
        run_id=run_id,
        request_id=request_id,
        trace_id=trace_id,
        catalog_version=CATALOG_VERSION,
        catalog_hash=CATALOG_HASH,
        runner_version=RUNNER_VERSION,
        suite_kind="probe",
        mode="gateway_probe",
        source_revision_hash=source_revision_hash,
        status="passed" if failed == 0 else "failed",
        coverage_complete=False,
        total_cases=len(results),
        passed_cases=passed,
        failed_cases=failed,
        failure_codes=failure_codes,
        started_at=started_at,
        completed_at=_utc_text(),
        cases=tuple(results),
    )


def run_gateway_probe_sync(**kwargs: object) -> ContractRunReport:
    """为 CLI 提供同步 probe 入口，已有 event loop 中不得隐式嵌套。"""

    return asyncio.run(run_gateway_probe(**kwargs))


__all__ = [
    "GatewayProbeConfig",
    "ProbeConfigError",
    "load_gateway_probe_config",
    "run_gateway_probe",
    "run_gateway_probe_sync",
]
