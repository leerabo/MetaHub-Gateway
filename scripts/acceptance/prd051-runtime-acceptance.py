"""PRD-051 FinOps 正式账本的聚焦真实运行验收。

脚本只连接调用方已经启动的本地 Control API，不负责启动服务、迁移数据库或复制运行库。
它以一个短生命周期管理员 Session 验证账本读取、调整幂等、冲突、冲正和余额上限，
最后撤销 Session，且仅在完整通过后写入不含口令、Token、幂等键或调整原因的 JSON 证据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlsplit

import httpx


# 验收只能连接本机回环 Control API，避免认证头因参数误配发送到外部地址。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# request_id、trace_id 与资源 ID 只接受服务端公共合同允许的安全字符。
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
# JSON 证据连字段名也不保留敏感概念，防止后续维护者误把明文加入结果结构。
FORBIDDEN_EVIDENCE_KEY_PATTERN = re.compile(
    r"password|token|secret|reason|idempotency(?:_key)?|authorization",
    re.IGNORECASE,
)
# 脚本生成的金额很小但非零；全额冲正后再追加一个微元即可稳定触发超额错误。
ADJUSTMENT_AMOUNT = "0.000002"
EXCESS_AMOUNT = "0.000001"


class AcceptanceFailure(RuntimeError):
    """只携带固定 stage/code 的公开异常，禁止响应正文或敏感请求进入终端。"""

    def __init__(self, stage: str, code: str) -> None:
        """构造已脱敏失败；stage 和 code 均由脚本内部常量提供。"""

        super().__init__(f"prd051_runtime_acceptance_failed stage={stage} code={code}")
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class RuntimeConfig:
    """运行验收所需配置，敏感口令只驻留当前 Python 进程内存。"""

    # Control API 必须是无路径、无凭据的 HTTP 回环 origin。
    control_origin: str
    # 证据目录必须位于工作区 ``artifacts/prd051`` 的单个批次目录内。
    artifact_directory: Path
    # 租户 slug 和管理员邮箱来自环境变量，不允许由请求正文自报 tenant_id。
    tenant_slug: str
    admin_email: str
    # 管理员口令只从环境变量读取，解析后立即从环境中删除。
    admin_password: str
    # 单请求硬超时，避免不可用服务让验收无限挂起。
    timeout_seconds: float


def _fail(stage: str, code: str) -> None:
    """抛出固定、可公开的验收错误，不拼接底层异常或不可信值。"""

    raise AcceptanceFailure(stage, code)


def _require(condition: bool, stage: str, code: str) -> None:
    """执行布尔门禁；失败时只暴露调用点声明的固定阶段和错误码。"""

    if not condition:
        _fail(stage, code)


def normalize_loopback_origin(value: str) -> str:
    """规范化 Control origin，并拒绝 HTTPS、外部主机、凭据、路径、查询和 fragment。"""

    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    # URL 类型、端口或 IPv6 解析差异统一压缩为固定配置错误，不回显原始输入。
    except (TypeError, ValueError):
        _fail("config.control_origin", "invalid_url")
    hostname = (parsed.hostname or "").lower()
    _require(parsed.scheme == "http", "config.control_origin", "scheme_not_http")
    _require(hostname in LOOPBACK_HOSTS, "config.control_origin", "host_not_loopback")
    _require(parsed.username is None and parsed.password is None, "config.control_origin", "url_credentials")
    _require(parsed.path in {"", "/"}, "config.control_origin", "url_path")
    _require(not parsed.query and not parsed.fragment, "config.control_origin", "url_suffix")
    _require(port is not None, "config.control_origin", "port_missing")
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{host}:{port}"


def _workspace_root() -> Path:
    """从脚本物理位置返回 MetaHub 工作区根，避免依赖调用方当前目录。"""

    root = Path(__file__).resolve().parents[2]
    _require(root.name.lower() == "metahub", "config.workspace", "unexpected_root")
    return root


def resolve_artifact_directory(value: str | Path, *, workspace_root: Path | None = None) -> Path:
    """把证据目录限制在 ``artifacts/prd051/<batch>``，阻止越界写文件。"""

    root = (workspace_root or _workspace_root()).resolve()
    artifact_root = (root / "artifacts" / "prd051").resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(artifact_root)
    # 证据路径越界时只返回固定阶段码，不能把宿主机物理路径写入失败输出。
    except ValueError:
        _fail("config.artifact_directory", "outside_workspace")
    _require(bool(relative.parts), "config.artifact_directory", "batch_missing")
    _require(len(relative.parts) == 1, "config.artifact_directory", "batch_depth")
    _require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,79}", relative.name) is not None,
        "config.artifact_directory",
        "batch_name",
    )
    return candidate


def parse_config(
    argv: list[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> RuntimeConfig:
    """解析非敏感参数，并只从环境变量取得管理员身份信息。"""

    env = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(description="PRD-051 FinOps 真实运行验收")
    parser.add_argument(
        "--control-url",
        default=env.get("METAHUB_PRD051_CONTROL_URL", "http://localhost:8001"),
    )
    parser.add_argument(
        "--artifact-dir",
        default=env.get("METAHUB_PRD051_ARTIFACT_DIR", ""),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    # 口令在任何网络客户端或后续子进程建立前从环境删除；脚本不支持命令行口令参数。
    password = env.pop("METAHUB_PRD051_ADMIN_PASSWORD", "") or env.pop(
        "METAHUB_ADMIN_PASSWORD", ""
    )
    env.pop("METAHUB_PRD051_ADMIN_PASSWORD", None)
    env.pop("METAHUB_ADMIN_PASSWORD", None)
    _require(bool(password), "config.credentials", "admin_password_missing")
    _require(bool(args.artifact_dir), "config.artifact_directory", "missing")
    _require(1.0 <= args.timeout <= 120.0, "config.timeout", "out_of_range")

    tenant_slug = env.get("METAHUB_PRD051_TENANT_SLUG") or env.get(
        "METAHUB_TENANT_SLUG", "default"
    )
    admin_email = env.get("METAHUB_PRD051_ADMIN_EMAIL") or env.get(
        "METAHUB_ADMIN_EMAIL", "admin@metahub.local"
    )
    _require(bool(tenant_slug.strip()), "config.credentials", "tenant_slug_missing")
    _require("@" in admin_email, "config.credentials", "admin_email_invalid")
    return RuntimeConfig(
        control_origin=normalize_loopback_origin(args.control_url),
        artifact_directory=resolve_artifact_directory(args.artifact_dir),
        tenant_slug=tenant_slug.strip(),
        admin_email=admin_email.strip().lower(),
        admin_password=password,
        timeout_seconds=args.timeout,
    )


def _request_identity(action: str, suffix: str) -> tuple[str, str]:
    """为一次 HTTP 操作生成无业务数据的 request_id 与 trace_id。"""

    request_id = f"req_prd051_accept_{action}_{suffix}"
    trace_id = f"trc_prd051_accept_{action}_{suffix}"
    _require(
        SAFE_IDENTIFIER_PATTERN.fullmatch(request_id) is not None,
        "request.identity",
        "request_id_invalid",
    )
    _require(
        SAFE_IDENTIFIER_PATTERN.fullmatch(trace_id) is not None,
        "request.identity",
        "trace_id_invalid",
    )
    return request_id, trace_id


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    """读取 JSON object；解析失败时不回显 HTML、正文或目标 URL。"""

    try:
        payload = response.json()
    # 解析失败不记录响应正文，防止代理错误页或服务端异常内容进入验收日志。
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
    require_no_store: bool,
    expected_code: str | None = None,
) -> dict[str, Any]:
    """核对状态、统一链路字段、错误码和 FinOps ``no-store`` 缓存合同。"""

    _require(response.status_code == expected_status, stage, "status_mismatch")
    payload = _json_object(response, stage)
    _require(response.headers.get("x-request-id") == request_id, stage, "request_header_mismatch")
    _require(response.headers.get("x-trace-id") == trace_id, stage, "trace_header_mismatch")
    _require(payload.get("request_id") == request_id, stage, "request_body_mismatch")
    _require(payload.get("trace_id") == trace_id, stage, "trace_body_mismatch")
    if require_no_store:
        directives = {
            item.strip().lower()
            for item in response.headers.get("cache-control", "").split(",")
            if item.strip()
        }
        _require("no-store" in directives, stage, "cache_control_missing")
    if expected_code is not None:
        _require(payload.get("code") == expected_code, stage, "error_code_mismatch")
    return payload


def _safe_http_call(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    stage: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """执行固定相对路径 HTTP 调用，并在边界把传输异常压缩为安全错误。"""

    try:
        return client.request(method, path, headers=dict(headers), json=body)
    # 传输异常可能携带目标 URL 或底层错误文本，因此只暴露固定安全错误码。
    except httpx.HTTPError:
        _fail(stage, "transport_error")


def _public_http_evidence(
    response: httpx.Response,
    payload: Mapping[str, Any],
    *,
    include_code: bool = False,
) -> dict[str, Any]:
    """把一次响应投影成状态与链路摘要，不复制原始正文或请求头。"""

    evidence: dict[str, Any] = {
        "http_status": response.status_code,
        "request_id": payload["request_id"],
        "trace_id": payload["trace_id"],
    }
    if response.request.url.path.startswith("/finops/"):
        evidence["cache_control_no_store"] = True
    if include_code:
        evidence["code"] = payload["code"]
    return evidence


class RuntimeAcceptance:
    """通过真实 Control API 驱动一组最小、可追踪的 FinOps 追加事实。"""

    def __init__(self, config: RuntimeConfig, client: httpx.Client) -> None:
        """保存运行配置和连接池；敏感字符串仅作为实例内存字段存在。"""

        self.config = config
        self.client = client
        self.suffix = secrets.token_hex(8)
        self.access_value = ""
        self.tenant_id = ""
        self.draft_marker = f"PRD051运行验收调整-{self.suffix}"
        self.reversal_marker = f"PRD051运行验收冲正-{self.suffix}"

    def _headers(self, action: str, *, include_access: bool = True) -> tuple[dict[str, str], str, str]:
        """构造一次请求的链路头；认证值只交给 httpx，不进入证据。"""

        request_id, trace_id = _request_identity(action, self.suffix)
        headers = {
            "Accept": "application/json",
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
        }
        if include_access:
            _require(bool(self.access_value), "auth.headers", "session_missing")
            headers["Authorization"] = f"Bearer {self.access_value}"
        return headers, request_id, trace_id

    def login(self) -> dict[str, Any]:
        """创建短生命周期管理员 Session，并确认后端返回 admin 与租户归属。"""

        headers, request_id, trace_id = self._headers("login", include_access=False)
        response = _safe_http_call(
            self.client,
            "POST",
            "/auth/login",
            stage="auth.login",
            headers={**headers, "Content-Type": "application/json"},
            body={
                "tenant_slug": self.config.tenant_slug,
                "email": self.config.admin_email,
                "password": self.config.admin_password,
            },
        )
        payload = verify_response_contract(
            response,
            stage="auth.login",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
            require_no_store=False,
        )
        user = payload.get("user")
        _require(isinstance(user, dict), "auth.login", "user_missing")
        _require(user.get("role") == "admin", "auth.login", "role_not_admin")
        _require(
            isinstance(user.get("tenant_id"), str)
            and SAFE_IDENTIFIER_PATTERN.fullmatch(user["tenant_id"]) is not None,
            "auth.login",
            "tenant_id_invalid",
        )
        _require(isinstance(payload.get("access_token"), str), "auth.login", "access_missing")
        _require(bool(payload["access_token"]), "auth.login", "access_empty")
        self.access_value = payload["access_token"]
        self.tenant_id = user["tenant_id"]
        return _public_http_evidence(response, payload)

    def _finops_call(
        self,
        *,
        action: str,
        method: str,
        path: str,
        expected_status: int,
        body: Mapping[str, Any] | None = None,
        replay_value: str | None = None,
        expected_code: str | None = None,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        """调用单个 FinOps 接口并统一验证链路、错误码和禁止缓存响应头。"""

        headers, request_id, trace_id = self._headers(action)
        if body is not None:
            headers["Content-Type"] = "application/json"
        if replay_value is not None:
            headers["Idempotency-Key"] = replay_value
        response = _safe_http_call(
            self.client,
            method,
            path,
            stage=f"finops.{action}",
            headers=headers,
            body=body,
        )
        payload = verify_response_contract(
            response,
            stage=f"finops.{action}",
            expected_status=expected_status,
            request_id=request_id,
            trace_id=trace_id,
            require_no_store=True,
            expected_code=expected_code,
        )
        return response, payload

    def run(self) -> dict[str, Any]:
        """执行完整最小验收，返回尚未落盘的安全证据候选。"""

        login_evidence = self.login()
        before_response, before = self._finops_call(
            action="ledger_before",
            method="GET",
            path="/finops/ledger?limit=20",
            expected_status=200,
        )
        _require(isinstance(before.get("ledger_entries"), list), "finops.ledger_before", "entries_missing")
        _require(isinstance(before.get("total"), int), "finops.ledger_before", "total_invalid")

        replay_value = f"finops_accept_credit_{self.suffix}"
        adjustment_body = {
            "adjustment_type": "manual_credit",
            "amount": ADJUSTMENT_AMOUNT,
            "reason": self.draft_marker,
        }
        create_response, created = self._finops_call(
            action="adjustment_create",
            method="POST",
            path="/finops/adjustments",
            expected_status=201,
            body=adjustment_body,
            replay_value=replay_value,
        )
        adjustment = created.get("adjustment")
        entry = created.get("ledger_entry")
        _require(isinstance(adjustment, dict), "finops.adjustment_create", "adjustment_missing")
        _require(isinstance(entry, dict), "finops.adjustment_create", "entry_missing")
        _require(created.get("idempotent_replay") is False, "finops.adjustment_create", "unexpected_replay")
        _require(adjustment.get("tenant_id") == self.tenant_id, "finops.adjustment_create", "tenant_mismatch")
        _require(entry.get("tenant_id") == self.tenant_id, "finops.adjustment_create", "entry_tenant_mismatch")
        _require(entry.get("entry_type") == "manual_credit", "finops.adjustment_create", "entry_type")
        _require(entry.get("direction") == "credit", "finops.adjustment_create", "entry_direction")
        _require(entry.get("amount") == ADJUSTMENT_AMOUNT, "finops.adjustment_create", "entry_amount")
        manual_adjustment_id = str(adjustment.get("finops_adjustment_id", ""))
        manual_entry_id = str(entry.get("finops_ledger_entry_id", ""))
        _require(
            SAFE_IDENTIFIER_PATTERN.fullmatch(manual_adjustment_id) is not None,
            "finops.adjustment_create",
            "adjustment_id_invalid",
        )
        _require(
            SAFE_IDENTIFIER_PATTERN.fullmatch(manual_entry_id) is not None,
            "finops.adjustment_create",
            "entry_id_invalid",
        )

        replay_response, replayed = self._finops_call(
            action="adjustment_replay",
            method="POST",
            path="/finops/adjustments",
            expected_status=200,
            body=adjustment_body,
            replay_value=replay_value,
        )
        _require(replayed.get("idempotent_replay") is True, "finops.adjustment_replay", "flag_missing")
        _require(
            replayed.get("adjustment", {}).get("finops_adjustment_id") == manual_adjustment_id,
            "finops.adjustment_replay",
            "adjustment_changed",
        )
        _require(
            replayed.get("ledger_entry", {}).get("finops_ledger_entry_id") == manual_entry_id,
            "finops.adjustment_replay",
            "entry_changed",
        )

        conflict_response, conflict = self._finops_call(
            action="adjustment_conflict",
            method="POST",
            path="/finops/adjustments",
            expected_status=409,
            body={**adjustment_body, "amount": "0.000003"},
            replay_value=replay_value,
            expected_code="finops.idempotency_conflict",
        )

        reversal_body = {
            "adjustment_type": "reversal",
            "amount": ADJUSTMENT_AMOUNT,
            "reversal_of_entry_id": manual_entry_id,
            "reason": self.reversal_marker,
        }
        reversal_response, reversed_payload = self._finops_call(
            action="reversal_create",
            method="POST",
            path="/finops/adjustments",
            expected_status=201,
            body=reversal_body,
            replay_value=f"finops_accept_reversal_{self.suffix}",
        )
        reversal_entry = reversed_payload.get("ledger_entry")
        _require(isinstance(reversal_entry, dict), "finops.reversal_create", "entry_missing")
        _require(reversal_entry.get("entry_type") == "reversal", "finops.reversal_create", "entry_type")
        _require(reversal_entry.get("direction") == "debit", "finops.reversal_create", "entry_direction")
        _require(
            reversal_entry.get("reversal_of_entry_id") == manual_entry_id,
            "finops.reversal_create",
            "target_mismatch",
        )
        reversal_entry_id = str(reversal_entry.get("finops_ledger_entry_id", ""))
        reversal_adjustment_id = str(
            reversed_payload.get("adjustment", {}).get("finops_adjustment_id", "")
        )
        _require(
            SAFE_IDENTIFIER_PATTERN.fullmatch(reversal_entry_id) is not None,
            "finops.reversal_create",
            "entry_id_invalid",
        )
        _require(
            SAFE_IDENTIFIER_PATTERN.fullmatch(reversal_adjustment_id) is not None,
            "finops.reversal_create",
            "adjustment_id_invalid",
        )

        excess_response, excess = self._finops_call(
            action="reversal_exceeds",
            method="POST",
            path="/finops/adjustments",
            expected_status=409,
            body={**reversal_body, "amount": EXCESS_AMOUNT},
            replay_value=f"finops_accept_exceeds_{self.suffix}",
            expected_code="finops.reversal_exceeds_remaining",
        )

        after_response, after = self._finops_call(
            action="ledger_after",
            method="GET",
            path=f"/finops/ledger?related_entry_id={manual_entry_id}&limit=20",
            expected_status=200,
        )
        after_entries = after.get("ledger_entries")
        _require(isinstance(after_entries, list), "finops.ledger_after", "entries_missing")
        returned_ids = {
            item.get("finops_ledger_entry_id")
            for item in after_entries
            if isinstance(item, dict)
        }
        _require(
            {manual_entry_id, reversal_entry_id}.issubset(returned_ids),
            "finops.ledger_after",
            "created_entries_missing",
        )

        evidence = {
            "schema_version": 1,
            "status": "passed",
            "prd_id": "PRD-051",
            "tenant_id": self.tenant_id,
            "generated_resources": {
                "manual_adjustment_id": manual_adjustment_id,
                "manual_entry_id": manual_entry_id,
                "reversal_adjustment_id": reversal_adjustment_id,
                "reversal_entry_id": reversal_entry_id,
            },
            "checks": {
                "admin_login": login_evidence,
                "ledger_before": {
                    **_public_http_evidence(before_response, before),
                    "entry_count": before["total"],
                },
                "adjustment_create": {
                    **_public_http_evidence(create_response, created),
                    "created_once": True,
                },
                "adjustment_replay": {
                    **_public_http_evidence(replay_response, replayed),
                    "same_resources": True,
                    "idempotent_replay": True,
                },
                "adjustment_conflict": _public_http_evidence(
                    conflict_response, conflict, include_code=True
                ),
                "reversal_create": {
                    **_public_http_evidence(reversal_response, reversed_payload),
                    "opposite_direction": True,
                },
                "reversal_exceeds": _public_http_evidence(
                    excess_response, excess, include_code=True
                ),
                "ledger_after": {
                    **_public_http_evidence(after_response, after),
                    "created_entries_visible": True,
                },
            },
            "privacy": {
                "environment_only_credentials": True,
                "safe_projection_verified": True,
            },
        }
        assert_safe_evidence(
            evidence,
            sensitive_values=(
                self.config.admin_password,
                self.access_value,
                self.draft_marker,
                self.reversal_marker,
                replay_value,
            ),
        )
        return evidence

    def logout(self) -> dict[str, Any]:
        """撤销本脚本创建的管理员 Session，并返回不含 Session ID 的公开摘要。"""

        _require(bool(self.access_value), "auth.logout", "session_missing")
        headers, request_id, trace_id = self._headers("logout")
        response = _safe_http_call(
            self.client,
            "POST",
            "/auth/logout",
            stage="auth.logout",
            headers=headers,
        )
        payload = verify_response_contract(
            response,
            stage="auth.logout",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
            require_no_store=False,
        )
        _require(payload.get("logged_out") is True, "auth.logout", "not_revoked")
        return _public_http_evidence(response, payload)

    def sensitive_values(self) -> tuple[str, ...]:
        """返回最终证据必须拒绝的进程内敏感值集合。"""

        return (
            self.config.admin_password,
            self.access_value,
            self.draft_marker,
            self.reversal_marker,
        )

    def clear_sensitive_memory(self) -> None:
        """在退出前清空实例持有的敏感引用，减少误用窗口。"""

        self.access_value = ""
        self.draft_marker = ""
        self.reversal_marker = ""


def assert_safe_evidence(value: Any, *, sensitive_values: tuple[str, ...]) -> None:
    """递归核对安全投影，并扫描已知敏感值绝不出现在 JSON 编码中。"""

    def walk(node: Any) -> None:
        """遍历 JSON 候选并拒绝敏感字段名与非 JSON 类型。"""

        if isinstance(node, dict):
            for key, child in node.items():
                _require(isinstance(key, str), "evidence.privacy", "non_string_key")
                _require(
                    FORBIDDEN_EVIDENCE_KEY_PATTERN.search(key) is None,
                    "evidence.privacy",
                    "forbidden_key",
                )
                walk(child)
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        _require(
            node is None or isinstance(node, (str, int, bool)),
            "evidence.privacy",
            "non_json_value",
        )

    walk(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for sensitive in sensitive_values:
        if sensitive:
            _require(sensitive not in encoded, "evidence.privacy", "sensitive_value")
    _require("Bearer " not in encoded, "evidence.privacy", "bearer_value")


def publish_evidence(directory: Path, evidence: Mapping[str, Any]) -> Path:
    """独占创建最终证据文件，避免重跑覆盖旧批次造成新旧事实混合。"""

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "runtime-evidence.json"
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
    # 已有结果必须保留，重跑不得覆盖上一尝试后伪装成同一批次成功。
    except FileExistsError:
        _fail("evidence.publish", "output_exists")
    return target


def execute_runtime(config: RuntimeConfig) -> dict[str, Any]:
    """运行验收、保证 Session 收口，并在全部通过后发布安全证据。"""

    with httpx.Client(
        base_url=config.control_origin,
        timeout=config.timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        acceptance = RuntimeAcceptance(config, client)
        evidence: dict[str, Any] | None = None
        original_failure: BaseException | None = None
        logout_evidence: dict[str, Any] | None = None
        try:
            evidence = acceptance.run()
        except Exception as exc:  # finally 必须先撤销已创建 Session。
            original_failure = exc
        finally:
            sensitive_values = acceptance.sensitive_values()
            if acceptance.access_value:
                try:
                    logout_evidence = acceptance.logout()
                except Exception as exc:  # 仅在无原始失败时提升 cleanup 失败。
                    if original_failure is None:
                        original_failure = exc
            acceptance.clear_sensitive_memory()

        if original_failure is not None:
            if isinstance(original_failure, AcceptanceFailure):
                raise original_failure
            _fail("runtime.execute", "unexpected_failure")
        _require(evidence is not None, "runtime.execute", "evidence_missing")
        _require(logout_evidence is not None, "auth.logout", "evidence_missing")
        evidence["cleanup"] = {"session_revoked": True, **logout_evidence}
        assert_safe_evidence(evidence, sensitive_values=sensitive_values)
        publish_evidence(config.artifact_directory, evidence)
        return evidence


def main(argv: list[str] | None = None) -> int:
    """命令行入口：只向标准输出写最终安全 JSON，失败只写固定 stage/code。"""

    try:
        evidence = execute_runtime(parse_config(argv))
    except AcceptanceFailure as exc:
        print(str(exc), file=os.sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
