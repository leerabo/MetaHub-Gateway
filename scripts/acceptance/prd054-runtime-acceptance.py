"""PRD-054 Request Explainability Center 真实运行验收器。

脚本只连接调用方已经启动的本机三服务，并把所有受控数据限定在当前批次的
``artifacts/prd054/<batch>/runtime.db``。它不会迁移数据库、启动服务、访问外网或写入
系统临时目录；管理员口令、Session Token 和隐私陷阱只驻留当前进程内存。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


# 公开 DTO 的顺序属于 PRD-054 版本合同，验收器保留独立副本以发现服务端漂移。
STAGE_ORDER = (
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider",
    "usage_and_finops",
    "logs_and_webhooks",
)
SECTION_ORDER = (
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider_response",
    "usage_and_finops",
    "logs",
    "webhooks",
)
# 每类事实只允许服务端设计审查通过的扁平字段，新增数据库列不能自动穿透验收。
FACT_VALUE_KEYS: dict[str, frozenset[str]] = {
    "authentication": frozenset(
        {
            "virtual_key_id", "virtual_key_prefix", "environment_id",
            "environment_name", "project_id", "project_name", "application_id",
            "application_name", "snapshot_version", "policy_snapshot_status",
        }
    ),
    "rate_limit": frozenset({"decision", "error_code"}),
    "budget_evaluation": frozenset(
        {
            "budget_rule_id", "scope_type", "scope_id", "budget_date",
            "spent_before_cny", "estimated_request_cost_cny", "projected_cost_cny",
            "budget_cny", "threshold_percent", "budget_status", "action_taken",
            "reason_code",
        }
    ),
    "budget_scope_event": frozenset(
        {
            "event_id", "budget_rule_id", "scope_type", "scope_id", "action_taken",
            "posted_cost_micros", "reserved_cost_micros",
            "estimated_request_cost_micros", "projected_cost_micros", "budget_micros",
        }
    ),
    "dlp_match": frozenset(
        {
            "dlp_rule_id", "rule_name", "rule_type", "action", "message_index",
            "match_text_hash", "match_length",
        }
    ),
    "policy_snapshot": frozenset(
        {
            "snapshot_version", "snapshot_status", "policy_id", "policy_action_type",
            "policy_target_model_id", "policy_decision", "policy_reason_code",
            "routing_policy_id", "routing_policy_version", "routing_mode",
        }
    ),
    "routing_decision": frozenset(
        {
            "decision_status", "decision_reason", "requested_model", "selected_model_id",
            "selected_internal_model_id", "provider_id", "provider_code", "error_code",
        }
    ),
    "routing_candidate": frozenset(
        {
            "model_id", "provider_id", "candidate_role", "configured_priority", "rank",
            "status", "reason_code", "health_state", "health_fresh", "keypool_state",
            "price_available", "price_source", "price_schema_version", "currency",
            "estimated_cost_amount", "quality_component_bps", "cost_component_bps",
            "latency_component_bps", "weighted_score_bps",
        }
    ),
    "cache_affinity": frozenset(
        {
            "attempt_sequence", "routing_attempt_id", "target_kind", "target_generation",
            "provider_id", "provider_code", "endpoint_preference_applied",
            "endpoint_preference_state", "binding_state_before", "selection_reason",
            "estimated_hit", "outcome_status",
        }
    ),
    "routing_attempt": frozenset(
        {
            "attempt_sequence", "attempt_role", "trigger_type", "trigger_error_code",
            "model_id", "internal_model_id", "provider_id", "provider_code", "status",
            "error_code", "error_category", "upstream_http_status", "retryable",
            "latency_ms", "usage_status", "estimated_cost_cny",
        }
    ),
    "usage": frozenset(
        {
            "input_tokens", "output_tokens", "total_tokens", "cache_tokens",
            "cost_amount", "cost_cny", "cost_usd", "currency", "price_schema_version",
            "price_snapshot_id", "estimated", "usage_source", "routing_attempt_id",
        }
    ),
    "finops_entry": frozenset(
        {
            "ledger_sequence", "event_id", "entry_type", "direction", "amount_micros",
            "currency", "normalized_cny_micros", "cny_normalization_status",
            "price_evidence_status", "price_snapshot_source", "posting_source",
            "quota_type", "price_snapshot_id", "price_schema_version", "input_tokens",
            "output_tokens", "total_tokens", "cache_tokens", "estimated", "status",
        }
    ),
    "request_log": frozenset(
        {
            "source_status", "error_code", "stream_status", "latency_ms",
            "fallback_triggered", "routing_attempt_count", "final_routing_attempt_id",
        }
    ),
    "stream_session": frozenset(
        {
            "status", "inter_token_timeout_ms", "chunk_count", "bytes_sent",
            "first_chunk_at", "last_chunk_at", "error_code", "completed_at",
        }
    ),
    "webhook_delivery": frozenset(
        {
            "event_id", "event_type", "webhook_endpoint_id", "endpoint_name", "status",
            "attempt_count", "max_attempts", "http_status", "duration_ms", "error_code",
            "next_retry_at", "delivery_kind", "replayed_from_delivery_id",
        }
    ),
    "webhook_attempt": frozenset(
        {
            "webhook_delivery_id", "webhook_endpoint_id", "attempt_no", "status",
            "http_status", "duration_ms", "error_code",
        }
    ),
}
# 区块只能承载指定事实类型，防止合法字段被放入错误业务语境后误导验收者。
SECTION_FACT_KINDS = {
    "authentication": frozenset({"authentication"}),
    "rate_limit": frozenset({"rate_limit"}),
    "budget": frozenset({"budget_evaluation", "budget_scope_event"}),
    "dlp": frozenset({"dlp_match"}),
    "policy": frozenset({"policy_snapshot"}),
    "routing": frozenset({"routing_decision", "routing_candidate"}),
    "keypool": frozenset({"routing_candidate", "cache_affinity", "routing_attempt"}),
    "provider_response": frozenset({"routing_attempt"}),
    "usage_and_finops": frozenset({"usage", "finops_entry"}),
    "logs": frozenset({"request_log", "stream_session"}),
    "webhooks": frozenset({"webhook_delivery", "webhook_attempt"}),
}

# 所有运行入口、标识和批次名采用显式白名单；不做 trim 或 Unicode 归一化。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
BATCH_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$")
EXPLANATION_ID_PATTERN = re.compile(r"^rxe_[0-9a-f]{32}$")
EVENT_ID_PATTERN = re.compile(r"^rxevt_[0-9a-f]{32}$")
EVIDENCE_STATES = frozenset(
    {"complete", "partial", "not_recorded", "not_applicable", "pending"}
)
STAGE_STATUSES = frozenset(
    {"passed", "blocked", "failed", "pending", "not_evaluated", "not_recorded"}
)

# 响应中明确禁止的正文/凭据槽位按规范化字段名判断；合法的 source request_id 不受影响。
FORBIDDEN_KEYS = frozenset(
    {
        "authorization", "credential_id", "encrypted_secret", "endpoint_url",
        "error_summary", "key_hash", "key_plaintext", "key_secret", "key_value",
        "match_text", "messages", "password", "payload", "payload_json", "prompt", "provider_key",
        "raw_usage", "response", "response_body", "secret", "token",
    }
)
FORBIDDEN_VALUE_PATTERN = re.compile(
    r"Bearer\s+[^\s\"']{8,}|\bmhv_[A-Za-z0-9_-]{16,}\b|\bsk[-_][A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)

# 这四个陷阱会写进运行库内部字段，只允许用于内存等值扫描，绝不能进入证据或终端。
PRIVATE_WEBHOOK_URL = "http://127.0.0.1:9/prd054-private-hook"
PRIVATE_WEBHOOK_TOKEN = "Bearer prd054-private-token-value"
PRIVATE_PROVIDER_RESPONSE = "prd054 private provider response body"
PRIVATE_ERROR_SUMMARY = "prd054 private provider error summary"

# 0044 的关键对象必须同时存在；仅有版本号不足以证明迁移可实际运行。
REQUIRED_TABLES = frozenset({"request_explanations", "request_webhook_delivery_links"})
REQUIRED_INDEXES = frozenset(
    {
        "idx_request_explanations_request_time_prd054",
        "idx_request_explanations_actor_time_prd054",
        "idx_request_explanations_pending_event_prd054",
        "idx_request_webhook_links_request_time_prd054",
        "idx_budget_scope_events_request_prd054",
        "idx_usage_ledger_request_prd054",
    }
)
REQUIRED_TRIGGERS = frozenset(
    {
        "trg_request_explanations_owner_insert_prd054",
        "trg_request_explanations_conflict_insert_prd054",
        "trg_request_explanations_rowid_insert_prd054",
        "trg_request_explanations_state_update_prd054",
        "trg_request_explanations_immutable_delete_prd054",
        "trg_request_webhook_links_owner_insert_prd054",
        "trg_request_webhook_links_conflict_insert_prd054",
        "trg_request_webhook_links_rowid_insert_prd054",
        "trg_request_webhook_links_immutable_update_prd054",
        "trg_request_webhook_links_immutable_delete_prd054",
        "trg_webhook_deliveries_request_link_insert_prd054",
    }
)


class AcceptanceFailure(RuntimeError):
    """只暴露固定 stage/code 的脱敏失败，禁止传播 HTTP、SQLite 或 DOM 正文。"""

    def __init__(self, stage: str, code: str) -> None:
        """保存固定失败分类；调用点不得把外部输入或异常文本拼入字段。"""

        super().__init__(f"prd054_runtime_acceptance_failed stage={stage} code={code}")
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class RuntimeConfig:
    """真实运行验收配置；管理员口令只在当前 Python 进程内存中存在。"""

    control_origin: str
    gateway_origin: str
    console_origin: str
    artifact_directory: Path
    database_path: Path
    tenant_slug: str
    admin_email: str
    admin_password: str
    timeout_seconds: float


@dataclass
class SessionCredential:
    """保存短生命周期 Session；清理阶段会主动擦除 Token 与密码引用。"""

    role: str
    tenant_id: str
    user_id: str
    session_id: str
    access_value: str
    password: str


def _fail(stage: str, code: str) -> None:
    """以固定错误分类终止验收，不把不可信值带入终端。"""

    raise AcceptanceFailure(stage, code)


def _require(condition: bool, stage: str, code: str) -> None:
    """执行失败关闭断言；失败时只使用调用点声明的稳定分类。"""

    if not condition:
        _fail(stage, code)


def _safe_identifier(value: object, stage: str, code: str) -> str:
    """读取可公开不透明 ID，并拒绝空值、超长值和正文型字符。"""

    _require(isinstance(value, str), stage, code)
    _require(SAFE_IDENTIFIER_PATTERN.fullmatch(value) is not None, stage, code)
    return value


def normalize_loopback_origin(value: str, *, stage: str) -> str:
    """规范化显式端口的 HTTP 回环 origin，拒绝凭据、路径、查询和 fragment。"""

    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    # URL 解析异常可能携带调用方输入，只折叠为固定配置错误。
    except (TypeError, ValueError):
        _fail(stage, "invalid_url")
    hostname = (parsed.hostname or "").lower()
    _require(parsed.scheme == "http", stage, "scheme_not_http")
    _require(hostname in LOOPBACK_HOSTS, stage, "host_not_loopback")
    _require(parsed.username is None and parsed.password is None, stage, "url_credentials")
    _require(parsed.path in {"", "/"}, stage, "url_path")
    _require(not parsed.query and not parsed.fragment, stage, "url_suffix")
    _require(port is not None, stage, "port_missing")
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{host}:{port}"


def _workspace_root() -> Path:
    """从脚本物理位置恢复唯一 MetaHub 根，避免调用方当前目录改变边界。"""

    root = Path(__file__).resolve().parents[2]
    _require(root.name.lower() == "metahub", "config.workspace", "unexpected_root")
    return root


def resolve_artifact_directory(
    value: str | Path,
    *,
    workspace_root: Path | None = None,
) -> Path:
    """把证据目录限制为 ``artifacts/prd054`` 下恰好一个安全批次层级。"""

    root = (workspace_root or _workspace_root()).resolve()
    artifact_root = (root / "artifacts" / "prd054").resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(artifact_root)
    # 越出证据根目录时只返回固定分类，不能回显主机绝对路径。
    except ValueError:
        _fail("config.artifact_directory", "outside_workspace")
    _require(len(relative.parts) == 1, "config.artifact_directory", "batch_depth")
    _require(
        BATCH_NAME_PATTERN.fullmatch(relative.name) is not None,
        "config.artifact_directory",
        "batch_name",
    )
    return candidate


def sqlite_url_for_path(path: Path) -> str:
    """把绝对数据库路径转换成项目支持的 canonical SQLite URL。"""

    resolved = path.resolve()
    _require(resolved.is_absolute(), "config.database_url", "path_not_absolute")
    return f"sqlite:///{resolved.as_posix()}"


def resolve_runtime_database(value: str, *, artifact_directory: Path) -> Path:
    """只接受当前验收批次内名为 ``runtime.db`` 的非链接 SQLite 文件路径。"""

    try:
        parsed = urlsplit(str(value))
    # SQLite URL 解析失败不得回显数据库路径或调用方原始文本。
    except (TypeError, ValueError):
        _fail("config.database_url", "invalid_url")
    _require(parsed.scheme == "sqlite", "config.database_url", "scheme")
    _require(not parsed.netloc, "config.database_url", "authority")
    _require(not parsed.query and not parsed.fragment, "config.database_url", "url_suffix")
    _require(parsed.path not in {"", "/"}, "config.database_url", "path_missing")
    _require("%" not in parsed.path and "\\" not in parsed.path, "config.database_url", "noncanonical")
    raw_path = parsed.path
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    source = Path(raw_path)
    _require(source.is_absolute(), "config.database_url", "path_not_absolute")
    candidate = source.resolve()
    expected = (artifact_directory / "runtime.db").resolve()
    _require(candidate == expected, "config.database_url", "not_batch_runtime")
    _require(not source.is_symlink(), "config.database_url", "database_symlink")
    return candidate


def parse_config(
    argv: list[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> RuntimeConfig:
    """解析回环地址与批次路径，并在返回前从环境删除管理员凭据。"""

    env = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(description="PRD-054 请求解释中心真实运行验收")
    parser.add_argument("--control-url", default=env.get("METAHUB_PRD054_CONTROL_URL", "http://localhost:8001"))
    parser.add_argument("--gateway-url", default=env.get("METAHUB_PRD054_GATEWAY_URL", "http://localhost:8000"))
    parser.add_argument("--console-url", default=env.get("METAHUB_PRD054_CONSOLE_URL", "http://localhost:5173"))
    parser.add_argument("--artifact-dir", default=env.get("METAHUB_PRD054_ARTIFACT_DIR", ""))
    parser.add_argument("--database-url", default=env.get("METAHUB_PRD054_DATABASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    password = env.pop("METAHUB_PRD054_ADMIN_PASSWORD", "") or env.pop("METAHUB_ADMIN_PASSWORD", "")
    _require(bool(password), "config.credentials", "admin_password_missing")
    _require(bool(args.artifact_dir), "config.artifact_directory", "missing")
    _require(bool(args.database_url), "config.database_url", "missing")
    _require(1.0 <= args.timeout <= 120.0, "config.timeout", "out_of_range")
    artifact_directory = resolve_artifact_directory(args.artifact_dir)
    database_path = resolve_runtime_database(args.database_url, artifact_directory=artifact_directory)
    origins = {
        normalize_loopback_origin(args.control_url, stage="config.control_origin"),
        normalize_loopback_origin(args.gateway_url, stage="config.gateway_origin"),
        normalize_loopback_origin(args.console_url, stage="config.console_origin"),
    }
    _require(len(origins) == 3, "config.origins", "must_be_distinct")
    tenant_slug = env.get("METAHUB_PRD054_TENANT_SLUG") or env.get("METAHUB_TENANT_SLUG", "default")
    admin_email = env.get("METAHUB_PRD054_ADMIN_EMAIL") or env.get("METAHUB_ADMIN_EMAIL", "admin@metahub.local")
    _require(bool(tenant_slug.strip()), "config.credentials", "tenant_slug_missing")
    _require("@" in admin_email, "config.credentials", "admin_email_invalid")
    return RuntimeConfig(
        control_origin=normalize_loopback_origin(args.control_url, stage="config.control_origin"),
        gateway_origin=normalize_loopback_origin(args.gateway_url, stage="config.gateway_origin"),
        console_origin=normalize_loopback_origin(args.console_url, stage="config.console_origin"),
        artifact_directory=artifact_directory,
        database_path=database_path,
        tenant_slug=tenant_slug.strip(),
        admin_email=admin_email.strip().lower(),
        admin_password=password,
        timeout_seconds=args.timeout,
    )


def _request_identity(action: str, suffix: str) -> tuple[str, str]:
    """为一个真实调用生成互不复用的 operation request_id 与 trace_id。"""

    request_id = f"req_prd054_accept_{action}_{suffix}"
    trace_id = f"trc_prd054_accept_{action}_{suffix}"
    _safe_identifier(request_id, "request.identity", "request_id_invalid")
    _safe_identifier(trace_id, "request.identity", "trace_id_invalid")
    _require(len(request_id) <= 128 and len(trace_id) <= 128, "request.identity", "too_long")
    return request_id, trace_id


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    """读取 JSON object；代理页或异常正文解析失败时只返回固定错误。"""

    try:
        payload = response.json()
    # 非 JSON 或非法编码正文可能含内部错误，只返回固定响应分类。
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        _fail(stage, "response_not_json")
    _require(isinstance(payload, dict), stage, "response_not_object")
    return payload


def verify_response_contract(
    response: httpx.Response,
    *,
    stage: str,
    expected_status: int,
    request_id: str,
    trace_id: str,
    expected_code: str | None = None,
    require_explanation_cache_headers: bool = False,
) -> dict[str, Any]:
    """核对 HTTP 状态、统一链路、领域错误和 Explain 防缓存响应头。"""

    _require(response.status_code == expected_status, stage, "status_mismatch")
    payload = _json_object(response, stage)
    _require(response.headers.get("x-request-id") == request_id, stage, "request_header")
    _require(response.headers.get("x-trace-id") == trace_id, stage, "trace_header")
    _require(payload.get("request_id") == request_id, stage, "request_body")
    _require(payload.get("trace_id") == trace_id, stage, "trace_body")
    if expected_code is not None:
        _require(payload.get("code") == expected_code, stage, "error_code")
    if require_explanation_cache_headers:
        _require(response.headers.get("cache-control") == "no-store, private", stage, "cache_control")
        _require(response.headers.get("pragma") == "no-cache", stage, "pragma")
        vary = {item.strip().lower() for item in response.headers.get("vary", "").split(",")}
        _require("authorization" in vary, stage, "vary")
    return payload


def _safe_http_call(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    stage: str,
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """只向预绑定回环 origin 发送相对路径，并把传输异常折叠为固定错误。"""

    _require(path.startswith("/") and not path.startswith("//"), stage, "path_invalid")
    _require("\r" not in path and "\n" not in path, stage, "path_invalid")
    try:
        return client.request(method, path, headers=dict(headers or {}), json=body)
    # 传输异常可能包含 URL、Header 或正文，必须折叠后再输出。
    except httpx.HTTPError:
        _fail(stage, "transport_failed")


def _normalize_key(value: str) -> str:
    """统一 camelCase、连字符和空白为下划线小写，供隐私键门禁复用。"""

    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", snake).strip("_").lower()


def assert_private_payload(value: Any, *, sensitive_values: tuple[str, ...] = ()) -> None:
    """递归拒绝未审查字段、嵌套非 JSON 值和已知敏感明文。"""

    def walk(node: Any) -> None:
        """遍历单个 JSON 节点；字典键和值分别应用最小披露约束。"""

        if isinstance(node, Mapping):
            for key, child in node.items():
                _require(isinstance(key, str), "privacy.scan", "non_string_key")
                _require(_normalize_key(key) not in FORBIDDEN_KEYS, "privacy.scan", "forbidden_key")
                walk(child)
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        _require(node is None or isinstance(node, (str, int, bool)), "privacy.scan", "non_json_value")

    walk(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    _require(FORBIDDEN_VALUE_PATTERN.search(encoded) is None, "privacy.scan", "credential_pattern")
    for sensitive in sensitive_values:
        if sensitive:
            _require(sensitive not in encoded, "privacy.scan", "known_sensitive_value")


def validate_explanation_document(
    payload: Mapping[str, Any],
    *,
    expected_source_request_id: str,
    expected_status: str,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    """验证 RequestExplanation 的固定顺序、exact keys、扁平事实和隐私合同。"""

    assert_private_payload(payload, sensitive_values=sensitive_values)
    _require(set(payload) == {"request_explanation", "request_id", "trace_id"}, "dto.response", "keys")
    _safe_identifier(payload.get("request_id"), "dto.response", "request_id")
    _safe_identifier(payload.get("trace_id"), "dto.response", "trace_id")
    explanation = payload.get("request_explanation")
    _require(isinstance(explanation, dict), "dto.explanation", "missing")
    expected_keys = {
        "request_explanation_id", "source_request_id", "source_trace_id", "status",
        "evidence_version", "captured_at", "summary", "decision_chain", *SECTION_ORDER,
        "missing_sections", "truncated_sections", "view_event_status",
    }
    _require(set(explanation) == expected_keys, "dto.explanation", "keys")
    _require(EXPLANATION_ID_PATTERN.fullmatch(str(explanation["request_explanation_id"])) is not None, "dto.explanation", "id")
    _require(explanation["source_request_id"] == expected_source_request_id, "dto.explanation", "source_request_id")
    _safe_identifier(explanation["source_trace_id"], "dto.explanation", "source_trace_id")
    _require(explanation["status"] == expected_status, "dto.explanation", "status")
    _require(explanation["evidence_version"] == "request_explanation_v1", "dto.explanation", "version")
    _require(explanation["view_event_status"] == "pending", "dto.explanation", "view_event_status")
    stages = explanation["decision_chain"]
    _require(isinstance(stages, list) and len(stages) == 10, "dto.stages", "count")
    _require(tuple(item.get("stage") for item in stages if isinstance(item, dict)) == STAGE_ORDER, "dto.stages", "order")
    for stage in stages:
        _require(isinstance(stage, dict), "dto.stage", "not_object")
        _require(set(stage) == {"stage", "status", "evidence_state", "reason_code", "occurred_at", "evidence_refs"}, "dto.stage", "keys")
        _require(stage["status"] in STAGE_STATUSES, "dto.stage", "status")
        _require(stage["evidence_state"] in EVIDENCE_STATES, "dto.stage", "evidence_state")
        _require(isinstance(stage["evidence_refs"], list) and len(stage["evidence_refs"]) <= 16, "dto.stage", "refs")
        for reference in stage["evidence_refs"]:
            _safe_identifier(reference, "dto.stage", "ref")
    missing = explanation["missing_sections"]
    truncated = explanation["truncated_sections"]
    _require(isinstance(missing, list) and isinstance(truncated, list), "dto.completeness", "arrays")
    positions = {name: index for index, name in enumerate(SECTION_ORDER)}
    _require(all(name in positions for name in [*missing, *truncated]), "dto.completeness", "unknown_section")
    _require(missing == sorted(set(missing), key=positions.__getitem__), "dto.completeness", "missing_order")
    _require(truncated == sorted(set(truncated), key=positions.__getitem__), "dto.completeness", "truncated_order")
    _require(not (set(missing) & set(truncated)), "dto.completeness", "overlap")
    if expected_status == "complete":
        _require(not missing and not truncated, "dto.completeness", "complete_has_gap")
    else:
        _require(bool(missing or truncated), "dto.completeness", "partial_without_gap")
    for section_name in SECTION_ORDER:
        section = explanation[section_name]
        _require(isinstance(section, dict), f"dto.section.{section_name}", "not_object")
        _require(set(section) == {"evidence_state", "reason_code", "facts", "truncated"}, f"dto.section.{section_name}", "keys")
        _require(section["evidence_state"] in EVIDENCE_STATES, f"dto.section.{section_name}", "state")
        _require(section["truncated"] is (section_name in truncated), f"dto.section.{section_name}", "truncated")
        _require(isinstance(section["facts"], list), f"dto.section.{section_name}", "facts")
        for fact in section["facts"]:
            _require(isinstance(fact, dict), f"dto.fact.{section_name}", "not_object")
            _require(set(fact) == {"kind", "fact_id", "occurred_at", "values"}, f"dto.fact.{section_name}", "keys")
            kind = fact["kind"]
            _require(kind in SECTION_FACT_KINDS[section_name], f"dto.fact.{section_name}", "kind")
            values = fact["values"]
            _require(isinstance(values, dict) and set(values) == FACT_VALUE_KEYS[kind], f"dto.fact.{section_name}", "value_keys")
            _require(all(item is None or isinstance(item, (str, int, bool)) for item in values.values()), f"dto.fact.{section_name}", "nested_value")
    summary = explanation["summary"]
    summary_keys = {
        "source_status", "error_code", "request_model", "resolved_model", "provider_code",
        "environment_id", "environment_name", "project_id", "project_name",
        "application_id", "application_name", "snapshot_version", "latency_ms",
        "input_tokens", "output_tokens", "total_tokens", "estimated_cost_amount",
        "currency", "fallback_triggered", "created_at", "updated_at",
    }
    _require(isinstance(summary, dict) and set(summary) == summary_keys, "dto.summary", "keys")
    _require(summary["source_status"] in {"success", "failed"}, "dto.summary", "source_status")
    _require(summary["estimated_cost_amount"] is None or isinstance(summary["estimated_cost_amount"], str), "dto.summary", "decimal_precision")
    return {
        "status": expected_status,
        "decision_stage_count": 10,
        "section_count": 11,
        "privacy_verified": True,
        "view_event_pending": True,
    }


def _public_http_evidence(
    response: httpx.Response,
    payload: Mapping[str, Any],
    *,
    include_code: bool = False,
) -> dict[str, Any]:
    """把 HTTP 结果压缩成只含状态、链路和可选固定错误码的安全证据。"""

    result: dict[str, Any] = {
        "status": response.status_code,
        "request_id": str(payload.get("request_id", "")),
        "trace_id": str(payload.get("trace_id", "")),
    }
    if include_code:
        result["code"] = str(payload.get("code", ""))
    return result


class RuntimeAcceptance:
    """通过真实 Control API、三服务健康端点和隔离运行库验收 PRD-054。"""

    def __init__(
        self,
        config: RuntimeConfig,
        control_client: httpx.Client,
        gateway_client: httpx.Client,
        console_client: httpx.Client,
    ) -> None:
        """保存客户端与随机后缀；认证材料仅保留在当前实例内存。"""

        self.config = config
        self.control = control_client
        self.gateway = gateway_client
        self.console = console_client
        self.suffix = secrets.token_hex(6)
        self.sessions: dict[str, SessionCredential] = {}
        self.resource_ids: dict[str, str] = {}
        self.http_evidence: dict[str, dict[str, Any]] = {}
        self.known_sensitive: list[str] = [
            config.admin_password,
            PRIVATE_WEBHOOK_URL,
            PRIVATE_WEBHOOK_TOKEN,
            PRIVATE_PROVIDER_RESPONSE,
            PRIVATE_ERROR_SUMMARY,
        ]

    def _connect_runtime(self) -> sqlite3.Connection:
        """连接已存在的批次运行库，禁止 SQLite 隐式创建其他文件。"""

        _require(self.config.database_path.is_file(), "database.connect", "runtime_missing")
        _require(not self.config.database_path.is_symlink(), "database.connect", "database_symlink")
        try:
            connection = sqlite3.connect(
                self.config.database_path,
                timeout=self.config.timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        # 打开失败可能暴露运行库路径，统一转换为固定数据库错误。
        except sqlite3.Error:
            _fail("database.connect", "open_failed")

    def verify_runtime_database_ready(self) -> dict[str, Any]:
        """确认 0044、关键对象、完整性和外键均已由调用方准备完成。"""

        connection = self._connect_runtime()
        try:
            migration = connection.execute(
                "SELECT status FROM schema_migrations WHERE version='0044'"
            ).fetchone()
            _require(migration is not None and migration["status"] == "applied", "database.readiness", "migration_0044_missing")
            objects = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table','index','trigger')"
            ).fetchall()
            by_type = {
                kind: {str(row["name"]) for row in objects if row["type"] == kind}
                for kind in ("table", "index", "trigger")
            }
            _require(REQUIRED_TABLES <= by_type["table"], "database.readiness", "table_missing")
            _require(REQUIRED_INDEXES <= by_type["index"], "database.readiness", "index_missing")
            _require(REQUIRED_TRIGGERS <= by_type["trigger"], "database.readiness", "trigger_missing")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            _require(len(integrity) == 1 and str(integrity[0][0]).lower() == "ok", "database.readiness", "integrity_failed")
            _require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "database.readiness", "foreign_key_failed")
        # 缺表、锁和损坏均不得把 SQL 或路径写入终端证据。
        except sqlite3.Error:
            _fail("database.readiness", "query_failed")
        finally:
            connection.close()
        return {
            "database_in_batch": True,
            "migration_0044_applied": True,
            "required_tables_present": True,
            "required_indexes_present": True,
            "required_triggers_present": True,
            "integrity_verified": True,
        }

    def verify_services(self) -> dict[str, bool]:
        """验证调用方已启动的 Gateway、Control 和 Console 均可由回环地址访问。"""

        gateway = _safe_http_call(self.gateway, "GET", "/health", stage="services.gateway")
        control = _safe_http_call(self.control, "GET", "/health", stage="services.control")
        console = _safe_http_call(self.console, "GET", "/", stage="services.console")
        _require(gateway.status_code == 200, "services.gateway", "health_status")
        _require(control.status_code == 200, "services.control", "health_status")
        _require(console.status_code == 200, "services.console", "root_status")
        _require("text/html" in console.headers.get("content-type", "").lower(), "services.console", "content_type")
        return {"gateway_http_200": True, "control_http_200": True, "console_http_200": True}

    def _headers(
        self,
        action: str,
        *,
        role: str | None,
        include_access: bool = True,
    ) -> tuple[dict[str, str], str, str]:
        """构造一个真实请求的认证与 operation 链路头，Token 不进入返回证据。"""

        request_id, trace_id = _request_identity(action, self.suffix)
        headers = {"Accept": "application/json", "X-Request-ID": request_id, "X-Trace-ID": trace_id}
        if include_access:
            _require(role in self.sessions, "auth.headers", "session_missing")
            access = self.sessions[str(role)].access_value
            _require(bool(access), "auth.headers", "session_empty")
            headers["Authorization"] = f"Bearer {access}"
        return headers, request_id, trace_id

    def _control_call(
        self,
        *,
        action: str,
        role: str | None,
        method: str,
        path: str,
        expected_status: int,
        body: Mapping[str, Any] | None = None,
        expected_code: str | None = None,
        explanation_cache_headers: bool = False,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        """调用真实 Control API 并立即验证统一响应合同。"""

        headers, request_id, trace_id = self._headers(action, role=role, include_access=role is not None)
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = _safe_http_call(
            self.control,
            method,
            path,
            stage=f"control.{action}",
            headers=headers,
            body=body,
        )
        payload = verify_response_contract(
            response,
            stage=f"control.{action}",
            expected_status=expected_status,
            request_id=request_id,
            trace_id=trace_id,
            expected_code=expected_code,
            require_explanation_cache_headers=explanation_cache_headers,
        )
        self.http_evidence[action] = _public_http_evidence(
            response,
            payload,
            include_code=expected_code is not None,
        )
        return response, payload

    def _login(
        self,
        *,
        role: str,
        email: str,
        password: str,
        expected_user_id: str | None = None,
    ) -> dict[str, Any]:
        """通过公开登录接口建立 Session，并核对角色、租户和同库绑定。"""

        action = f"login_{role}"
        headers, request_id, trace_id = self._headers(action, role=None, include_access=False)
        response = _safe_http_call(
            self.control,
            "POST",
            "/auth/login",
            stage=f"auth.{action}",
            headers={**headers, "Content-Type": "application/json"},
            body={"tenant_slug": self.config.tenant_slug, "email": email, "password": password},
        )
        provisional = _json_object(response, f"auth.{action}")
        access = provisional.get("access_token")
        user = provisional.get("user")
        if isinstance(access, str) and access and isinstance(user, dict):
            self.sessions[role] = SessionCredential(
                role=role,
                tenant_id=str(user.get("tenant_id", "")),
                user_id=str(user.get("user_id", "")),
                session_id=str(provisional.get("session_id", "")),
                access_value=access,
                password=password,
            )
            self.known_sensitive.extend((password, access))
        payload = verify_response_contract(
            response,
            stage=f"auth.{action}",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
        )
        user = payload.get("user")
        _require(isinstance(user, dict) and user.get("role") == role, f"auth.{action}", "role")
        tenant_id = _safe_identifier(user.get("tenant_id"), f"auth.{action}", "tenant_id")
        user_id = _safe_identifier(user.get("user_id"), f"auth.{action}", "user_id")
        session_id = _safe_identifier(payload.get("session_id"), f"auth.{action}", "session_id")
        access = payload.get("access_token")
        _require(isinstance(access, str) and bool(access), f"auth.{action}", "access_missing")
        if expected_user_id is not None:
            _require(user_id == expected_user_id, f"auth.{action}", "user_id_mismatch")
        # 首次登录冻结租户 owner，后续角色 Session 必须绑定同一租户。
        if self.resource_ids.get("tenant_id"):
            _require(tenant_id == self.resource_ids["tenant_id"], f"auth.{action}", "tenant_mismatch")
        self.sessions[role] = SessionCredential(role, tenant_id, user_id, session_id, access, password)
        self.resource_ids.setdefault("tenant_id", tenant_id)
        self.resource_ids[f"{role}_user_id"] = user_id
        _require(self.verify_session_database_binding(role), f"auth.{action}", "database_binding")
        evidence = _public_http_evidence(response, payload)
        self.http_evidence[action] = evidence
        return evidence

    def verify_session_database_binding(self, role: str) -> bool:
        """通过 Session 行与不可逆 Token 哈希证明服务确实绑定当前 runtime.db。"""

        credential = self.sessions.get(role)
        _require(credential is not None, "database.session_binding", "session_missing")
        expected_hash = hashlib.sha256(credential.access_value.encode("utf-8")).hexdigest()
        connection = self._connect_runtime()
        try:
            row = connection.execute(
                """
                SELECT s.tenant_id, s.user_id, s.token_hash, s.status, u.role
                FROM sessions s
                JOIN users u ON u.tenant_id=s.tenant_id AND u.user_id=s.user_id
                WHERE s.session_id=?
                """,
                (credential.session_id,),
            ).fetchone()
        # Session 绑定查询失败只返回固定分类，Token 哈希和 SQL 不得泄露。
        except sqlite3.Error:
            _fail("database.session_binding", "query_failed")
        finally:
            connection.close()
        _require(row is not None, "database.session_binding", "session_not_found")
        return bool(
            row["tenant_id"] == credential.tenant_id
            and row["user_id"] == credential.user_id
            and row["role"] == role
            and row["status"] == "active"
            and hmac.compare_digest(str(row["token_hash"]), expected_hash)
        )

    def create_role_users(self) -> dict[str, bool]:
        """经管理员 API 创建 developer/viewer，并登录形成真实权限攻击 Session。"""

        for role in ("developer", "viewer"):
            password = f"Prd054!{secrets.token_urlsafe(18)}"
            email = f"prd054-{role}-{self.suffix}@metahub.local"
            _, payload = self._control_call(
                action=f"create_{role}",
                role="admin",
                method="POST",
                path="/users",
                expected_status=201,
                body={
                    "email": email,
                    "display_name": f"PRD054 runtime {role}",
                    "password": password,
                    "role": role,
                    "status": "active",
                },
            )
            user = payload.get("user")
            _require(isinstance(user, dict) and user.get("role") == role, f"users.{role}", "role")
            user_id = _safe_identifier(user.get("user_id"), f"users.{role}", "user_id")
            self._login(role=role, email=email, password=password, expected_user_id=user_id)
        # 只返回角色绑定布尔结果，一次性邮箱与口令继续留在进程内存。
        return {"developer_session_bound": True, "viewer_session_bound": True}

    def create_explanation_fixtures(self) -> dict[str, bool]:
        """在同库写入 complete、partial、跨租户请求和只用于脱敏验证的内部陷阱。"""

        tenant_id = self.sessions["admin"].tenant_id
        complete_id = f"req_prd054_complete_{self.suffix}"
        partial_id = f"req_prd054_partial_{self.suffix}"
        cross_id = f"req_prd054_cross_{self.suffix}"
        other_tenant_id = f"tenant_prd054_other_{self.suffix}"
        timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        connection = self._connect_runtime()
        try:
            connection.execute("BEGIN IMMEDIATE")
            metadata = connection.execute(
                """
                SELECT vk.virtual_key_id, vk.environment_id,
                       environment.code AS environment_code
                FROM virtual_keys vk
                JOIN environments environment
                  ON environment.tenant_id=vk.tenant_id
                 AND environment.environment_id=vk.environment_id
                WHERE vk.tenant_id=? AND vk.status='active'
                ORDER BY vk.created_at, vk.virtual_key_id LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
            _require(metadata is not None, "fixtures.metadata", "virtual_key_missing")
            connection.execute(
                """
                INSERT INTO tenants(
                  tenant_id, slug, name, service_mode, status, created_at, updated_at
                ) VALUES(?, ?, 'PRD054 Other Tenant', 'internal_only', 'active', ?, ?)
                """,
                (other_tenant_id, f"prd054-other-{self.suffix}", timestamp, timestamp),
            )
            claims = [
                (tenant_id, complete_id, f"trc_prd054_complete_{self.suffix}", metadata["virtual_key_id"], timestamp),
                (tenant_id, partial_id, f"trc_prd054_partial_{self.suffix}", metadata["virtual_key_id"], timestamp),
                (other_tenant_id, cross_id, f"trc_prd054_cross_{self.suffix}", None, timestamp),
            ]
            connection.executemany(
                """
                INSERT INTO gateway_request_claims(
                  tenant_id, request_id, trace_id, virtual_key_id,
                  snapshot_version, policy_snapshot_status, claimed_at
                ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
                """,
                claims,
            )
            connection.execute(
                """
                INSERT INTO request_logs(
                  request_id, tenant_id, trace_id, virtual_key_id, environment_id,
                  environment_code, endpoint, request_model, status, error_code,
                  latency_ms, policy_snapshot_status, fallback_triggered,
                  routing_attempt_count, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, '/v1/chat/completions', 'company-default',
                         'failed', 'gateway.rate_limit_exceeded', 3, 'disabled', 0, 0, ?, ?)
                """,
                (
                    complete_id, tenant_id, f"trc_prd054_complete_{self.suffix}",
                    metadata["virtual_key_id"], metadata["environment_id"],
                    metadata["environment_code"], timestamp, timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO request_logs(
                  request_id, tenant_id, trace_id, virtual_key_id, environment_id,
                  environment_code, endpoint, request_model, resolved_model,
                  provider_code, status, latency_ms, input_tokens, output_tokens,
                  total_tokens, estimated_cost_amount, currency, policy_snapshot_status,
                  dlp_status, fallback_triggered, routing_attempt_count, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, '/v1/chat/completions', 'company-default',
                         'default-chat', 'local_openai', 'success', 41, 11, 7, 18,
                         0.123456, 'CNY', 'disabled', 'clean', 0, 0, ?, ?)
                """,
                (
                    partial_id, tenant_id, f"trc_prd054_partial_{self.suffix}",
                    metadata["virtual_key_id"], metadata["environment_id"],
                    metadata["environment_code"], timestamp, timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO request_logs(
                  request_id, tenant_id, trace_id, status, policy_snapshot_status,
                  created_at, updated_at
                ) VALUES(?, ?, ?, 'success', 'disabled', ?, ?)
                """,
                (cross_id, other_tenant_id, f"trc_prd054_cross_{self.suffix}", timestamp, timestamp),
            )
            self._insert_sensitive_webhook_history(connection, tenant_id, partial_id, timestamp)
            connection.commit()
            link = connection.execute(
                """
                SELECT source_request_id FROM request_webhook_delivery_links
                WHERE tenant_id=? AND source_request_id=?
                """,
                (tenant_id, partial_id),
            ).fetchone()
            _require(link is not None, "fixtures.webhook_link", "missing")
        except AcceptanceFailure:
            connection.rollback()
            raise
        # fixture 写入失败必须回滚，且不能把内部陷阱值或 SQL 写入错误。
        except sqlite3.Error:
            connection.rollback()
            _fail("fixtures.database", "write_failed")
        finally:
            connection.close()
        self.resource_ids.update(
            {
                "complete_request_id": complete_id,
                "partial_request_id": partial_id,
                "cross_tenant_request_id": cross_id,
                "other_tenant_id": other_tenant_id,
            }
        )
        return {
            "complete_fixture_created": True,
            "partial_fixture_created": True,
            "cross_tenant_fixture_created": True,
            "sensitive_webhook_link_created": True,
        }

    def _insert_sensitive_webhook_history(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        source_request_id: str,
        timestamp: str,
    ) -> None:
        """写入可关联但绝不能出现在 Explain DTO 的 URL、payload 和错误正文。"""

        endpoint_id = f"whe_prd054_{self.suffix}"
        delivery_id = f"whd_prd054_{self.suffix}"
        attempt_id = f"wha_prd054_{self.suffix}"
        event_id = f"evt_prd054_{self.suffix}"
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
              encrypted_secret, secret_prefix, secret_fingerprint, status,
              timeout_ms, max_attempts, created_at, updated_at
            ) VALUES(?, ?, 'PRD054 Private Receiver', ?, '["gateway.request_failed"]',
                     'prd054-private-encrypted-secret', 'whsec_pr',
                     'prd054-private-fingerprint', 'active', 1000, 3, ?, ?)
            """,
            (endpoint_id, tenant_id, PRIVATE_WEBHOOK_URL, timestamp, timestamp),
        )
        envelope = {
            "event_id": event_id,
            "event_type": "gateway.request_failed",
            "tenant_id": tenant_id,
            "timestamp": timestamp,
            "actor_id": None,
            "object_id": source_request_id,
            "trace_id": f"trc_prd054_partial_{self.suffix}",
            "payload": {
                "request_id": source_request_id,
                "token": PRIVATE_WEBHOOK_TOKEN,
                "provider_response": PRIVATE_PROVIDER_RESPONSE,
            },
        }
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id,
              event_type, event_timestamp, payload_json, status, attempt_count,
              max_attempts, http_status, duration_ms, error_code, error_summary,
              secret_version, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'gateway.request_failed', ?, ?, 'failed', 1, 3,
                     500, 25, 'webhook.remote_error', ?, 1, ?, ?)
            """,
            (
                delivery_id, tenant_id, endpoint_id, event_id, timestamp,
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
                PRIVATE_ERROR_SUMMARY, timestamp, timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO webhook_delivery_attempts(
              webhook_delivery_attempt_id, tenant_id, webhook_delivery_id,
              webhook_endpoint_id, attempt_no, status, http_status, duration_ms,
              error_code, error_summary, created_at
            ) VALUES(?, ?, ?, ?, 1, 'failed', 500, 25,
                     'webhook.remote_error', ?, ?)
            """,
            (attempt_id, tenant_id, delivery_id, endpoint_id, PRIVATE_ERROR_SUMMARY, timestamp),
        )

    def verify_permissions_errors_and_explanations(self) -> dict[str, Any]:
        """验证三角色、权限优先、同形 404、严格错误以及 complete/partial DTO。"""

        complete_id = self.resource_ids["complete_request_id"]
        partial_id = self.resource_ids["partial_request_id"]
        cross_id = self.resource_ids["cross_tenant_request_id"]
        # 匿名和 viewer 都提交畸形输入，证明认证/RBAC 先于 path/query/body 业务解析。
        self._control_call(
            action="anonymous_malformed",
            role=None,
            method="GET",
            path="/request-explain/%20?tenant_id=forged",
            expected_status=401,
            body={"prompt": "must-not-be-read"},
            expected_code="auth.token_missing",
        )
        self._control_call(
            action="viewer_malformed",
            role="viewer",
            method="GET",
            path="/request-explain/%20?tenant_id=forged",
            expected_status=403,
            body={"prompt": "must-not-be-read"},
            expected_code="auth.permission_denied",
        )
        _, admin_complete = self._control_call(
            action="admin_complete",
            role="admin",
            method="GET",
            path=f"/request-explain/{complete_id}",
            expected_status=200,
            explanation_cache_headers=True,
        )
        _, developer_complete = self._control_call(
            action="developer_complete",
            role="developer",
            method="GET",
            path=f"/request-explain/{complete_id}",
            expected_status=200,
            explanation_cache_headers=True,
        )
        _, admin_partial = self._control_call(
            action="admin_partial",
            role="admin",
            method="GET",
            path=f"/request-explain/{partial_id}",
            expected_status=200,
            explanation_cache_headers=True,
        )
        sensitive = tuple(self.known_sensitive)
        complete_contract = validate_explanation_document(
            admin_complete,
            expected_source_request_id=complete_id,
            expected_status="complete",
            sensitive_values=sensitive,
        )
        validate_explanation_document(
            developer_complete,
            expected_source_request_id=complete_id,
            expected_status="complete",
            sensitive_values=sensitive,
        )
        partial_contract = validate_explanation_document(
            admin_partial,
            expected_source_request_id=partial_id,
            expected_status="partial",
            sensitive_values=sensitive,
        )
        _, cross = self._control_call(
            action="cross_tenant",
            role="admin",
            method="GET",
            path=f"/request-explain/{cross_id}",
            expected_status=404,
            expected_code="request_explanation.not_found",
        )
        _, missing = self._control_call(
            action="missing",
            role="admin",
            method="GET",
            path=f"/request-explain/req_prd054_missing_{self.suffix}",
            expected_status=404,
            expected_code="request_explanation.not_found",
        )
        _require(set(cross) == set(missing), "errors.not_found", "shape_mismatch")
        for key in set(cross) - {"request_id", "trace_id"}:
            _require(cross[key] == missing[key], "errors.not_found", "body_mismatch")
        self._control_call(
            action="invalid_query",
            role="admin",
            method="GET",
            path=f"/request-explain/{complete_id}?include_raw=true",
            expected_status=422,
            expected_code="request_explanation.request_invalid",
        )
        self._control_call(
            action="invalid_source_id",
            role="admin",
            method="GET",
            path="/request-explain/%20",
            expected_status=422,
            expected_code="request_explanation.request_id_invalid",
        )
        return {
            "admin_allowed": True,
            "developer_allowed": True,
            "viewer_denied_before_input": True,
            "anonymous_denied_before_input": True,
            "cross_tenant_same_shape_404": True,
            "strict_query_and_path_errors": True,
            "complete_contract": complete_contract,
            "partial_contract": partial_contract,
        }

    def verify_access_outbox(self) -> dict[str, Any]:
        """核对成功读取的不可变访问事实、独立稳定事件和失败请求零残留。"""

        tenant_id = self.resource_ids["tenant_id"]
        expected_operations = {
            _request_identity(action, self.suffix)[0]
            for action in ("admin_complete", "developer_complete", "admin_partial")
        }
        connection = self._connect_runtime()
        try:
            rows = connection.execute(
                """
                SELECT request_explanation_id, event_id, tenant_id, source_request_id,
                       actor_user_id, actor_session_id, operation_request_id,
                       operation_trace_id, event_dispatch_status, event_dispatch_attempts
                FROM request_explanations
                WHERE tenant_id=? AND source_request_id IN (?, ?)
                ORDER BY request_explanation_id
                """,
                (
                    tenant_id,
                    self.resource_ids["complete_request_id"],
                    self.resource_ids["partial_request_id"],
                ),
            ).fetchall()
            duplicated = connection.execute(
                "SELECT COUNT(*) AS total FROM audit_logs WHERE tenant_id=? AND action='request.explanation_viewed'",
                (tenant_id,),
            ).fetchone()
            foreign_rows = connection.execute(
                "SELECT COUNT(*) AS total FROM request_explanations WHERE tenant_id!=?",
                (tenant_id,),
            ).fetchone()
        # outbox 核验失败只报告固定分类，避免数据库正文进入验收输出。
        except sqlite3.Error:
            _fail("outbox.database", "query_failed")
        finally:
            connection.close()
        _require(len(rows) == 3, "outbox.rows", "success_count")
        _require({row["operation_request_id"] for row in rows} == expected_operations, "outbox.rows", "operation_binding")
        _require(len({row["request_explanation_id"] for row in rows}) == 3, "outbox.rows", "explanation_id_unique")
        _require(len({row["event_id"] for row in rows}) == 3, "outbox.rows", "event_id_unique")
        _require(all(EXPLANATION_ID_PATTERN.fullmatch(row["request_explanation_id"]) for row in rows), "outbox.rows", "explanation_id_shape")
        _require(all(EVENT_ID_PATTERN.fullmatch(row["event_id"]) for row in rows), "outbox.rows", "event_id_shape")
        _require(all(row["event_dispatch_status"] in {"pending", "processing", "dispatched", "failed"} for row in rows), "outbox.rows", "dispatch_state")
        _require(duplicated is not None and int(duplicated["total"]) == 0, "outbox.rows", "duplicate_generic_audit")
        _require(foreign_rows is not None and int(foreign_rows["total"]) == 0, "outbox.rows", "cross_tenant_residue")
        return {
            "successful_view_fact_count": 3,
            "failed_request_fact_count": 0,
            "stable_event_ids_unique": True,
            "source_outbox_persisted": True,
            "dispatch_state_valid": True,
            "generic_audit_not_duplicated": True,
        }

    def run(self) -> dict[str, Any]:
        """按数据库、服务、身份、fixture、API 和 outbox 顺序执行聚焦验收。"""

        database = self.verify_runtime_database_ready()
        services = self.verify_services()
        admin_login = self._login(role="admin", email=self.config.admin_email, password=self.config.admin_password)
        roles = self.create_role_users()
        fixtures = self.create_explanation_fixtures()
        api = self.verify_permissions_errors_and_explanations()
        outbox = self.verify_access_outbox()
        return {
            "schema_version": 1,
            "prd_id": "PRD-054",
            "status": "passed",
            "generated_resources": {
                key: value
                for key, value in self.resource_ids.items()
                if not key.endswith("_user_id")
            },
            "checks": {
                "database": database,
                "services": services,
                "admin_login": admin_login,
                "roles": roles,
                "fixtures": fixtures,
                "api": api,
                "outbox": outbox,
            },
            "http": dict(self.http_evidence),
        }

    def _logout_one(self, role: str) -> bool:
        """注销一个已登记 Session，并通过同库状态证明服务端完成撤销。"""

        credential = self.sessions.get(role)
        if credential is None or not credential.access_value:
            return False
        headers, request_id, trace_id = self._headers(f"logout_{role}", role=role)
        response = _safe_http_call(self.control, "POST", "/auth/logout", stage=f"auth.logout_{role}", headers=headers)
        verify_response_contract(
            response,
            stage=f"auth.logout_{role}",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection = self._connect_runtime()
        try:
            row = connection.execute("SELECT status FROM sessions WHERE session_id=?", (credential.session_id,)).fetchone()
        # 注销后的数据库核验失败不能回显 Session ID 或数据库路径。
        except sqlite3.Error:
            _fail(f"auth.logout_{role}", "query_failed")
        finally:
            connection.close()
        return row is not None and row["status"] == "revoked"

    def logout_all(self) -> dict[str, bool]:
        """按低权限到管理员顺序撤销本轮所有 Session，兼容中途失败清理。"""

        results: dict[str, bool] = {}
        for role in ("viewer", "developer", "admin"):
            # 只注销本轮实际创建的 Session，缺席角色不伪造成功证据。
            if role in self.sessions:
                results[f"{role}_session_revoked"] = self._logout_one(role)
        expected = {f"{role}_session_revoked" for role in self.sessions}
        results["session_revoked"] = bool(expected) and all(results.get(key) is True for key in expected)
        return results

    def sensitive_values(self) -> tuple[str, ...]:
        """返回当前内存中已知明文，供最终证据做等值泄露扫描。"""

        return tuple(value for value in self.known_sensitive if value)

    def clear_sensitive_memory(self) -> None:
        """擦除验收器持有的密码、Token 和隐私陷阱引用。"""

        for credential in self.sessions.values():
            credential.access_value = ""
            credential.password = ""
        self.known_sensitive.clear()


def assert_safe_evidence(value: Any, *, sensitive_values: tuple[str, ...]) -> None:
    """对最终 JSON 执行字段、凭据形态和本轮已知明文三重隐私门禁。"""

    assert_private_payload(value, sensitive_values=sensitive_values)


def publish_evidence(directory: Path, evidence: Mapping[str, Any]) -> Path:
    """以独占创建方式发布证据；同批次重跑不得覆盖既有验收事实。"""

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "runtime-acceptance.json"
    encoded = (json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with target.open("xb") as handle:
            handle.write(encoded)
    # 已存在证据必须失败关闭，防止重跑覆盖先前验收事实。
    except FileExistsError:
        _fail("evidence.publish", "already_exists")
    # 文件系统写入异常只保留固定分类，不暴露绝对路径与正文。
    except OSError:
        _fail("evidence.publish", "write_failed")
    return target


def execute_runtime(config: RuntimeConfig) -> dict[str, Any]:
    """执行真实验收、强制注销全部 Session、隐私扫描并发布一次证据。"""

    acceptance: RuntimeAcceptance | None = None
    candidate: dict[str, Any] | None = None
    cleanup: dict[str, bool] = {"session_revoked": False}
    sensitive: tuple[str, ...] = (config.admin_password,)
    with (
        httpx.Client(base_url=config.control_origin, timeout=config.timeout_seconds, follow_redirects=False, trust_env=False) as control,
        httpx.Client(base_url=config.gateway_origin, timeout=config.timeout_seconds, follow_redirects=False, trust_env=False) as gateway,
        httpx.Client(base_url=config.console_origin, timeout=config.timeout_seconds, follow_redirects=False, trust_env=False) as console,
    ):
        acceptance = RuntimeAcceptance(config, control, gateway, console)
        try:
            candidate = acceptance.run()
        finally:
            sensitive = acceptance.sensitive_values()
            cleanup = acceptance.logout_all()
            acceptance.clear_sensitive_memory()
    _require(candidate is not None, "runtime.execute", "candidate_missing")
    _require(
        cleanup.get("admin_session_revoked") is True
        and cleanup.get("developer_session_revoked") is True
        and cleanup.get("viewer_session_revoked") is True
        and cleanup.get("session_revoked") is True,
        "runtime.cleanup",
        "session_not_revoked",
    )
    candidate["checks"]["cleanup"] = cleanup
    assert_safe_evidence(candidate, sensitive_values=sensitive)
    publish_evidence(config.artifact_directory, candidate)
    return candidate


def main(argv: list[str] | None = None) -> int:
    """命令行入口只输出固定成功摘要或脱敏失败分类。"""

    try:
        config = parse_config(argv)
        execute_runtime(config)
    except AcceptanceFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("prd054_runtime_acceptance_failed stage=runtime.unhandled code=unexpected", file=sys.stderr)
        return 1
    print('{"prd_id":"PRD-054","status":"passed","evidence":"runtime-acceptance.json"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
