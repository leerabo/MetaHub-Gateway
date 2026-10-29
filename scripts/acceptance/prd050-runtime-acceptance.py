"""PRD-050 Prompt Cache 感知粘性路由的真实三服务运行验收。

脚本只接受 ``artifacts/prd050/<batch>`` 内已经完成 0040 迁移的独立 SQLite 数据库，
并连接调用方已经启动的 Gateway、Control API 与 Console。它通过公开 API 创建本地
OpenAI-compatible Provider、两把临时托管 Key、ModelAlias、Virtual Key 和 Webhook，
再验证长/短 Prompt、冷到 warm、权限、防枚举、用量口径和安全日志投影。

密码、Session Token、Virtual Key、Provider Key、Webhook Secret、Prompt 和 Response
只存在于进程内存。每个可能写状态的操作都会在发出前登记安全 mutation；成功和失败路径
都在 ``finally`` 中按 tenant 和稳定 ID 禁用临时凭据与入口。状态文件只保存 ID、计数和
布尔结论，不保存任何密钥、正文、Key prefix、Credential、Binding、digest 或 data_class。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO

import httpx
from metahub_shared.runtime.webhooks import verify_webhook_signature

# PRD-048 的统一 JSON 断言、认证装配、清理和状态脱敏已经过真实验收；本脚本只复用这些
# 通用能力，不执行 PRD-048 的质量路由流程，也不会加载任何外部 Provider。
_BASE_PATH = Path(__file__).with_name("prd048-runtime-acceptance.py")
_BASE_SPEC = importlib.util.spec_from_file_location("prd050_prd048_base", _BASE_PATH)
assert _BASE_SPEC is not None and _BASE_SPEC.loader is not None
# 动态模块要先建立稳定对象再登记到 sys.modules，避免 dataclass、traceback 和测试导入
# 因脚本文件名含连字符而解析到两个模块实例；该对象只复用本地验收 helper。
_BASE = importlib.util.module_from_spec(_BASE_SPEC)
sys.modules[_BASE_SPEC.name] = _BASE
_BASE_SPEC.loader.exec_module(_BASE)

# 管理请求、Gateway 调用和异步 Webhook 都有硬超时，服务卡死必须使验收失败而非无限等待。
HTTP_TIMEOUT = httpx.Timeout(20.0)
EVENT_WAIT_SECONDS = 12.0
MAX_HTTP_BODY_BYTES = 1_048_576
# 假上游只为前两个长 Prompt 保留一个短并发窗口；超时必须固定失败，不能永久占用验收线程。
UPSTREAM_PAIR_WAIT_SECONDS = 8.0
# Future 超时略大于单请求 HTTP 硬超时，确保线程调度异常也能由主线程有界收口。
CONCURRENT_REQUEST_WAIT_SECONDS = 25.0
# 状态证据必须遵循 write-once：无论入口已存在还是发布瞬间发生竞争，都只返回同一固定码，
# 不能把目标路径、旧文件内容或随机临时文件名拼入异常而扩大本地验收信息暴露面。
RUNTIME_STATE_OUTPUT_COLLISION_CODE = "PRD050_RUNTIME_STATE_OUTPUT_COLLISION"
# 随机名即便遭遇极低概率碰撞也只做有限重试；达到上限说明目录状态不可信，应失败关闭，
# 不能回退到可覆盖名称或扫描删除同前缀文件。
RUNTIME_STATE_TEMPORARY_CREATE_ATTEMPTS = 32
# generation 同时是批次目录身份和跨 producer 关联键；格式必须与静态 runner 完全一致，
# 避免 Runtime、Browser 或 closeout 对同一批次使用宽严不同的路径语义。
GENERATION_PATTERN = re.compile(r"^\d{8}T\d{6}$")
# 标准输入只允许一条有界管理员口令，避免管道异常向进程内存灌入无界敏感数据。
MAX_STDIN_PASSWORD_BYTES = 4096

# 0040 版本号之外还要核对关键表和状态机 trigger，避免 checksum/手工库让版本号形成假阳性。
PRD050_TABLES = frozenset(
    {
        "cache_affinities",
        "cache_affinity_scopes",
        "cache_affinity_bindings",
        "cache_affinity_selections",
        "cache_affinity_config_events",
    }
)
PRD050_RUNTIME_TRIGGERS = frozenset(
    {
        "trg_cache_affinities_prd050_owner_insert",
        "trg_cache_affinity_scopes_prd050_pointer_update",
        "trg_cache_affinity_bindings_prd050_graph_insert",
        "trg_cache_affinity_bindings_prd050_transition_update",
        "trg_cache_affinity_selections_prd050_graph_insert",
        "trg_cache_affinity_selections_prd050_update",
        "trg_cache_affinity_config_events_prd050_graph_insert",
        "trg_cache_affinity_config_events_prd050_immutable_update",
    }
)

# PAGE-073 与 RequestLog DTO 的字段集合必须保持精确；额外字段可能直接泄露内部粘性域。
SAFE_SELECTION_FIELDS = frozenset(
    {
        "cache_affinity_selection_id",
        "cache_affinity_id",
        "cache_affinity_version",
        "attempt_sequence",
        "selection_reason",
        "estimated_hit",
        "endpoint_preference_applied",
        "target_kind",
        "model_id",
        "provider_id",
        "provider_code",
        "created_at",
    }
)
RUNTIME_EVENT_FIELDS = frozenset(
    {
        "trigger",
        "request_id",
        "cache_affinity_selection_id",
        "cache_affinity_id",
        "cache_affinity_version",
        "model_alias_id",
        "model_endpoint_id",
        "selected_provider_id",
        "target_kind",
        "selection_reason",
        "estimated_hit",
    }
)
CONFIG_EVENT_FIELDS = frozenset(
    {
        "trigger",
        "request_id",
        "cache_affinity_id",
        "cache_affinity_version",
        "model_alias_id",
        "change_kind",
        "changed_fields",
        "status",
    }
)
# 标准 Webhook 信封必须逐字段冻结；先删除未知字段再断言会掩盖租户漂移或顶层敏感扩展。
WEBHOOK_ENVELOPE_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "actor_id",
        "object_id",
        "trace_id",
        "payload",
    }
)
CAPTURED_WEBHOOK_EVENT_FIELDS = frozenset(
    {
        *WEBHOOK_ENVELOPE_FIELDS,
        "payload_keys",
        "signature_valid",
        "privacy_ok",
    }
)

# Webhook 接收器递归拒绝这些内部字段。测试端使用独立白名单，避免生产校验与夹具同时漏项。
FORBIDDEN_EVENT_KEYS = frozenset(
    {
        "authorization",
        "base_url",
        "cache_affinity_binding_id",
        "cache_affinity_scope_id",
        "credential",
        "data_class",
        "digest",
        "endpoint_revision",
        "encrypted_key",
        "key_prefix",
        "password",
        "plain_value",
        "prompt",
        "provider_credential_id",
        "provider_key",
        "response",
        "secret",
        "token",
    }
)


def _assert_exact_config_event_pairs(
    config_events: list[Any],
    *,
    create_request_id: str,
    noop_request_id: str,
) -> None:
    """校验配置事件数量和请求绑定，不把同秒随机 ID 排序误认为业务因果顺序。

    参数：数据库读取的配置事件行、创建操作 request_id 和 noop 操作 request_id。
    返回：无；恰好包含两种预期事件时通过，否则抛出 AssertionError。
    关键约束：长度检查与集合检查必须同时保留，防止集合去重掩盖重复事件。
    """

    event_pairs = [
        (row["event_type"], row["operation_request_id"]) for row in config_events
    ]
    assert len(event_pairs) == 2
    assert set(event_pairs) == {
        ("created", create_request_id),
        ("noop", noop_request_id),
    }


class AcceptanceServer(ThreadingHTTPServer):
    """同时承载本地 OpenAI-compatible 假上游和 HMAC Webhook 接收端。

    服务只保存上游类型、Key 槽位、缓存 Token 和事件白名单字段。原始 Header、请求正文、
    Prompt、Response 与签名 Secret 都不会进入共享记录或默认 HTTP 访问日志。
    """

    daemon_threads = True

    def __init__(self, long_prompt: str, short_prompt: str, response_sentinel: str) -> None:
        """绑定随机回环端口并保存仅供当前进程比较的敏感哨兵。

        参数是长 Prompt、短 Prompt 与固定 Response；返回值为无。关键约束是这些值只能
        用于等值判断，不能进入 ``provider_calls``、``webhook_events`` 或异常文本。
        """

        super().__init__(("127.0.0.1", 0), AcceptanceHandler)
        self.long_prompt = long_prompt
        self.short_prompt = short_prompt
        self.response_sentinel = response_sentinel
        self.signing_secret = ""
        self._provider_keys: list[str] = []
        self._provider_calls: list[dict[str, Any]] = []
        self._webhook_events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # Event 只协调前两个相同长 Prompt：首请求在 Provider 成功前停住，让第二请求
        # 有机会提交 binding_pending_reused；第二个上游调用到达后同时释放两者。
        self._long_pair_ready = threading.Event()

    def install_provider_keys(self, values: list[str]) -> None:
        """把两把临时 Provider Key 只安装到接收器内存。

        参数是 API 创建前已经随机生成的明文列表；返回值为无。列表使用副本，退出上下文
        时立即清空，任何记录只保存匹配后的 ``slot_a/slot_b`` 安全标签。
        """

        assert len(values) == 2 and all(values)
        with self._lock:
            self._provider_keys = list(values)

    def record_provider_call(
        self,
        *,
        prompt_kind: str,
        model: str,
        authorization: str,
    ) -> tuple[int, bool] | None:
        """验证 Authorization、登记脱敏调用并返回缓存 Token 与并发门结果。

        ``None`` 表示认证不属于本轮两把 Key，调用方必须返回 401。前两个长 Prompt 都实报
        17 且在第二个到达前阻塞，后续长 Prompt 实报 0，短 Prompt 实报 5；返回布尔值说明
        并发窗口是否在硬超时内成对到达，从而不把夹具超时误写成 Provider 成功。
        """

        wait_for_pair = False
        with self._lock:
            slot = ""
            for index, value in enumerate(self._provider_keys):
                expected = f"Bearer {value}"
                # 明文 Key 只能在当前栈帧做恒定时间比较，不能写入调用摘要或失败诊断；
                # compare_digest 也避免普通字符串比较过早退出而放大本地认证探针差异。
                if secrets.compare_digest(authorization, expected):
                    slot = "slot_a" if index == 0 else "slot_b"
                    break
            if not slot:
                return None
            long_ordinal = 0
            if prompt_kind == "long":
                long_ordinal = 1 + sum(
                    1 for item in self._provider_calls if item["prompt_kind"] == "long"
                )
            cache_tokens = 17 if prompt_kind == "long" and long_ordinal <= 2 else 0
            if prompt_kind == "short":
                cache_tokens = 5
            wait_for_pair = prompt_kind == "long" and long_ordinal <= 2
            if long_ordinal == 2:
                self._long_pair_ready.set()
            self._provider_calls.append(
                {
                    "prompt_kind": prompt_kind,
                    "model": model,
                    "authorization_slot": slot,
                    "cache_tokens": cache_tokens,
                }
            )
        pair_ready = True
        if wait_for_pair:
            # 首请求必须等到第二个真实 Provider 调用已进入；Event.wait 有硬上限，避免夹具
            # 或 Gateway 并发回归导致验收线程永久挂起。
            pair_ready = self._long_pair_ready.wait(UPSTREAM_PAIR_WAIT_SECONDS)
        # 返回值只含整数用量和门禁布尔，不返回 Authorization、Key 槽位映射或 Prompt。
        return cache_tokens, pair_ready

    def provider_calls(self) -> list[dict[str, Any]]:
        """返回上游安全摘要副本，避免 HTTP 线程和主线程共享可变对象。"""

        with self._lock:
            return [dict(item) for item in self._provider_calls]

    def record_webhook_event(self, event: dict[str, Any]) -> None:
        """保存已经验签且通过独立隐私门禁的 CacheAffinity 事件安全副本。"""

        with self._lock:
            self._webhook_events.append(dict(event))

    def webhook_events(self) -> list[dict[str, Any]]:
        """返回事件安全摘要副本，供主线程按持久 event ID 有界等待。"""

        with self._lock:
            return [dict(item) for item in self._webhook_events]

    def clear_sensitive_values(self) -> None:
        """清空接收器持有的全部明文哨兵和凭据引用。"""

        with self._lock:
            self._provider_keys.clear()
            self.long_prompt = ""
            self.short_prompt = ""
            self.response_sentinel = ""
            self.signing_secret = ""


class AcceptanceHandler(BaseHTTPRequestHandler):
    """实现固定本地 Provider 与只记录白名单摘要的 Webhook HTTP 协议。"""

    def do_GET(self) -> None:  # noqa: N802
        """为可选 Provider 探测返回本地模型目录，未知路径返回固定 404。"""

        if self.path.endswith("/models"):
            self._json_response(
                200,
                {"object": "list", "data": [{"id": "prd050-cache-model"}]},
            )
            return
        self._json_response(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        """有界读取正文并分派 Provider 或 Webhook，其他路径固定拒绝。"""

        raw = self._read_body()
        if raw is None:
            return
        if self.path == "/hook":
            self._handle_webhook(raw)
            return
        if self.path == "/v1/chat/completions":
            self._handle_provider(raw)
            return
        self._json_response(404, {"error": "not_found"})

    def log_message(self, _format: str, *args: object) -> None:
        """关闭标准库访问日志，防止 URL、Header 或错误参数意外写到服务日志。"""

        del args

    def _read_body(self) -> bytes | None:
        """按 Content-Length 有界读取正文，非法或超过一 MiB 时直接返回 413。"""

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        # Content-Length 属于不可信 wire；转换错误只能折叠到固定 413，禁止回显原始 Header。
        # 同时不能继续读取连接，否则攻击者可绕过一 MiB 的硬内存上限。
        except ValueError:
            length = -1
        if length < 1 or length > MAX_HTTP_BODY_BYTES:
            self._json_response(413, {"error": "body_too_large"})
            return None
        return self.rfile.read(length)

    def _handle_provider(self, raw: bytes) -> None:
        """验证 Prompt 和两把 Key 的真实调用，并返回方言化缓存 Token。

        原始正文只在本栈帧解析；共享状态仅登记 long/short、模型、Key 槽位和整数用量。
        """

        server = self.server
        assert isinstance(server, AcceptanceServer)
        try:
            payload = json.loads(raw.decode("utf-8", errors="strict"))
        # 假上游只接受严格 UTF-8 JSON；异常可能携带正文片段，因此响应和日志均使用固定码。
        # 原始 bytes 在当前函数返回后即释放，不能保存到共享 server 状态。
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": "invalid_json"})
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            self._json_response(400, {"error": "invalid_payload"})
            return
        messages = payload["messages"]
        system_content = ""
        # 亲和指纹只取连续 leading system/developer；夹具在这里精确检查首条 system，
        # 不遍历或保存后续 user 正文，避免验收接收器自身扩大 Prompt 生命周期。
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            system_content = messages[0].get("content", "")
        if system_content == server.long_prompt:
            prompt_kind = "long"
        elif system_content == server.short_prompt:
            prompt_kind = "short"
        else:
            self._json_response(400, {"error": "prompt_mismatch"})
            return
        model = payload.get("model")
        if not isinstance(model, str) or model != "prd050-cache-model":
            self._json_response(400, {"error": "model_mismatch"})
            return
        result = server.record_provider_call(
            prompt_kind=prompt_kind,
            model=model,
            authorization=self.headers.get("Authorization", ""),
        )
        # None 表示两把进程内托管 Key 均未命中；必须在返回业务正文前固定拒绝，
        # 不能把未知 Authorization 当匿名本地端点放行或记录其原值。
        if result is None:
            self._json_response(401, {"error": "unauthorized"})
            return
        cache_tokens, pair_ready = result
        # 并发对未在硬窗口内到齐说明 Gateway/夹具没有形成可验收 pending，不得让首请求
        # 先成功并把后续请求伪装成 warm；固定 504 也不回显任何 Prompt 或凭据。
        if not pair_ready:
            self._json_response(504, {"error": "concurrent_pair_timeout"})
            return
        self._json_response(
            200,
            {
                "id": "chatcmpl_prd050_runtime",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": server.response_sentinel},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 320 if prompt_kind == "long" else 20,
                    "completion_tokens": 2,
                    "total_tokens": 322 if prompt_kind == "long" else 22,
                    "prompt_tokens_details": {"cached_tokens": cache_tokens},
                },
            },
        )

    def _handle_webhook(self, raw: bytes) -> None:
        """验签事件、递归拒绝敏感键，并只保存 CacheAffinity 安全信封。"""

        server = self.server
        assert isinstance(server, AcceptanceServer)
        try:
            envelope = json.loads(raw.decode("utf-8", errors="strict"))
        # Webhook 原文由外部投递控制，解码异常可能含签名正文片段；固定错误可防止日志泄露，
        # 且失败事件绝不能进入接收器的安全摘要列表。
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": "invalid_json"})
            return
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        signature = self.headers.get("X-MetaHub-Signature", "")
        signature_valid = bool(server.signing_secret) and verify_webhook_signature(
            signature,
            server.signing_secret,
            raw,
        )
        # 隐私门禁必须覆盖完整信封，不能先投影掉顶层 Credential、Secret 或正文扩展再判定安全。
        privacy_ok = isinstance(envelope, dict) and not _contains_forbidden_event_key(envelope)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != WEBHOOK_ENVELOPE_FIELDS
            or not isinstance(payload, dict)
            or envelope.get("event_type") != "cache.affinity_selected"
            or not signature_valid
            or not privacy_ok
        ):
            self._json_response(401, {"error": "unsafe_event"})
            return
        # 完整信封已经过独立递归隐私检查；保留 tenant/trace 才能在主线程证明 wire 身份，
        # 这些安全 ID 只存在于当前验收进程内，不会进入最终状态文件。
        safe_payload = dict(payload)
        server.record_webhook_event(
            {
                "event_id": envelope["event_id"],
                "event_type": "cache.affinity_selected",
                "timestamp": envelope["timestamp"],
                "tenant_id": envelope["tenant_id"],
                "actor_id": envelope["actor_id"],
                "object_id": envelope["object_id"],
                "trace_id": envelope["trace_id"],
                "payload": safe_payload,
                "payload_keys": sorted(str(key) for key in safe_payload),
                "signature_valid": True,
                "privacy_ok": True,
            }
        )
        self.send_response(204)
        self.end_headers()

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        """写入固定 UTF-8 JSON 和长度，防止客户端等待连接关闭。"""

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class RunningAcceptanceServer(AbstractContextManager[AcceptanceServer]):
    """管理回环假上游和 Webhook 线程，保证异常路径也关闭端口并清空明文。"""

    def __init__(self, long_prompt: str, short_prompt: str, response_sentinel: str) -> None:
        """保存三个内存哨兵；只有进入上下文后才真正绑定随机回环端口。"""

        self.values = (long_prompt, short_prompt, response_sentinel)
        self.server: AcceptanceServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> AcceptanceServer:
        """启动 daemon HTTP 线程并返回可查询安全摘要的服务对象。"""

        self.server = AcceptanceServer(*self.values)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *_exc_info: object) -> None:
        """停止监听、等待线程结束并主动清空 Server 持有的敏感值。"""

        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server.clear_sensitive_values()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.values = ("", "", "")


def _contains_forbidden_event_key(value: object) -> bool:
    """递归判断事件对象是否携带正文、凭据、Binding、分类值或摘要字段。"""

    if isinstance(value, dict):
        for raw_key, child in value.items():
            normalized = str(raw_key).lower().replace("-", "_")
            if (
                normalized in FORBIDDEN_EVENT_KEYS
                or normalized.endswith(("_secret", "_password", "_api_key"))
                or "digest" in normalized
                or "credential" in normalized
                or "binding" in normalized
            ):
                return True
            if _contains_forbidden_event_key(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_event_key(item) for item in value)
    return False


def _is_utc_webhook_timestamp(value: object) -> bool:
    """判断 Webhook 时间是否为带 ``Z`` 的有效 UTC ISO-8601 文本。"""

    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    # Webhook 时间属于不可信 wire；解析错误只能返回固定布尔失败，不能把原值带入异常诊断。
    # 调用方随后以无消息 assert 失败关闭，避免畸形时间污染验收输出或状态制品。
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _assert_common_webhook_event(
    event: dict[str, Any],
    *,
    tenant_id: str,
    trace_id: str,
    object_id: str,
) -> dict[str, Any]:
    """核对接收信封的精确字段、租户、trace、对象、时间和隐私结论。"""

    assert set(event) == CAPTURED_WEBHOOK_EVENT_FIELDS
    assert event["event_type"] == "cache.affinity_selected"
    assert event["tenant_id"] == tenant_id
    assert event["trace_id"] == trace_id
    assert event["object_id"] == object_id
    assert _is_utc_webhook_timestamp(event["timestamp"])
    assert event["signature_valid"] is True and event["privacy_ok"] is True
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert event["payload_keys"] == sorted(payload)
    return payload


def _assert_runtime_webhook_event(
    event: dict[str, Any],
    *,
    tenant_id: str,
    request_id: str,
    selection_id: str,
    cache_affinity_id: str,
    cache_affinity_version: int,
    model_alias_id: str,
    model_endpoint_id: str,
    selected_provider_id: str,
    selection_reason: str,
    estimated_hit: bool,
) -> None:
    """把 runtime Webhook 与真实 Selection、租户、路由目标及请求 trace 精确绑定。"""

    payload = _assert_common_webhook_event(
        event,
        tenant_id=tenant_id,
        trace_id=request_id.replace("req_", "trc_", 1),
        object_id=cache_affinity_id,
    )
    assert event["event_id"] == selection_id
    assert event["actor_id"] is None
    assert payload == {
        "trigger": "request_selected",
        "request_id": request_id,
        "cache_affinity_selection_id": selection_id,
        "cache_affinity_id": cache_affinity_id,
        "cache_affinity_version": cache_affinity_version,
        "model_alias_id": model_alias_id,
        "model_endpoint_id": model_endpoint_id,
        "selected_provider_id": selected_provider_id,
        "target_kind": "managed",
        "selection_reason": selection_reason,
        "estimated_hit": estimated_hit,
    }


def _assert_configuration_webhook_event(
    event: dict[str, Any],
    *,
    tenant_id: str,
    request_id: str,
    actor_id: str,
    cache_affinity_id: str,
    cache_affinity_version: int,
    model_alias_id: str,
) -> None:
    """把配置 Webhook 与真实管理员、租户、Alias、配置版本和创建请求精确绑定。"""

    payload = _assert_common_webhook_event(
        event,
        tenant_id=tenant_id,
        trace_id=request_id.replace("req_", "trc_", 1),
        object_id=cache_affinity_id,
    )
    assert isinstance(event["event_id"], str) and event["event_id"].startswith("cae_")
    assert event["actor_id"] == actor_id
    assert payload == {
        "trigger": "configuration_changed",
        "request_id": request_id,
        "cache_affinity_id": cache_affinity_id,
        "cache_affinity_version": cache_affinity_version,
        "model_alias_id": model_alias_id,
        "change_kind": "created",
        "changed_fields": [
            "status",
            "data_class",
            "minimum_prefix_chars",
            "ttl_seconds",
        ],
        "status": "enabled",
    }


class RuntimeAcceptance:
    """编排 PRD-050 真实 API、Gateway、Webhook、数据库与隐私验收。"""

    def __init__(self, args: argparse.Namespace) -> None:
        """初始化客户端、资源账本和只驻留内存的敏感值。

        参数已经通过回环 URL、独立库和同批次日志路径校验。构造函数不写数据库；每个
        mutation 会在 HTTP 或 SQL 执行前加入安全账本，供异常后的精确资源解析和清理。
        """

        self.args = args
        self.base = _BASE.RuntimeAcceptance(args)
        self.control = self.base.control
        self.gateway = self.base.gateway
        self.suffix = args.suffix
        random_prompt = secrets.token_urlsafe(24)
        self.long_prompt = f"prd050-long-{random_prompt}-" + ("stable-cache-prefix-" * 18)
        self.short_prompt = f"prd050-short-{secrets.token_urlsafe(18)}"
        self.user_prompt = f"prd050-user-{secrets.token_urlsafe(18)}"
        self.response_sentinel = f"prd050-response-{secrets.token_urlsafe(18)}"
        self.provider_keys: list[str] = []
        self.mutation_ledger: list[dict[str, str]] = []
        self.log_paths: tuple[Path, ...] = tuple(args.log_path)
        self.request_ids: dict[str, str] = {}

    @property
    def resource_ids(self) -> dict[str, str]:
        """返回基础装配器共享的稳定资源 ID 账本。"""

        return self.base.resource_ids

    @property
    def mutation_count(self) -> int:
        """返回所有在执行前已登记的写操作数量，仅作为安全生命周期证据。"""

        return len(self.mutation_ledger)

    def close(self) -> None:
        """关闭 HTTP 连接池并主动清空全部长生命周期敏感引用。"""

        self.provider_keys.clear()
        self.long_prompt = ""
        self.short_prompt = ""
        self.user_prompt = ""
        self.response_sentinel = ""
        self.mutation_ledger.clear()
        self.base.close()

    def sensitive_values(self) -> list[str]:
        """返回状态、SQLite 和服务日志中禁止出现的本批次敏感明文。"""

        values = [
            *self.base.sensitive_values(),
            self.long_prompt,
            self.short_prompt,
            self.user_prompt,
            self.response_sentinel,
            *self.provider_keys,
        ]
        return [value for value in values if value]

    def run(self, receiver: AcceptanceServer) -> dict[str, Any]:
        """执行完整真实验收并返回仅含脱敏事实的状态对象。

        任一断言失败都会冒泡，外层 ``finally`` 再撤销全部已登记资源；本函数不写局部
        passed 状态，也不把不可信 HTTP 正文放入错误消息。
        """

        database_ready = self._verify_database_ready()
        self.base._verify_service_health()
        self._pre_register_mutation("admin_login", f"req_prd048_runtime_login_admin_{self.suffix}")
        self.base.admin_token = self.base._login(
            self.args.admin_email,
            self.args.admin_password,
            role="admin",
        )
        self.base._current_identity()
        environment = self.base._default_environment()
        self._create_role_users()
        self._create_cross_tenant_alias()
        route = self._create_route(environment, receiver)
        self.base.virtual_key = self._create_virtual_key(environment["environment_id"])
        self._create_webhook(receiver)

        configuration = self._verify_configuration_contract(route)
        request_ids = self._exercise_gateway(route)
        provider_evidence = self._verify_provider_calls(receiver)
        log_evidence = self._verify_request_logs(request_ids, route, configuration)
        webhook_evidence = self._wait_for_webhooks(
            receiver,
            configuration,
            request_ids,
            log_evidence,
        )
        database_evidence = self._verify_database(
            configuration,
            request_ids,
            log_evidence,
        )
        privacy = self._verify_privacy()

        safe_resource_keys = {
            "main_tenant_id",
            "provider_a_id",
            "model_a_id",
            "alias_id",
            "routing_policy_id",
            "virtual_key_id",
            "webhook_endpoint_id",
            "cache_affinity_id",
            "cross_tenant_id",
            "cross_tenant_alias_id",
        }
        return {
            "schema_version": 1,
            "status": "passed",
            "prd_id": "PRD-050",
            "suffix": self.suffix,
            "database_file": self.args.database_path.name,
            "resource_ids": {
                key: value for key, value in self.resource_ids.items() if key in safe_resource_keys
            },
            "request_ids": dict(request_ids),
            "checks": {
                **database_ready,
                **configuration["checks"],
                **provider_evidence,
                **log_evidence["checks"],
                **webhook_evidence,
                **database_evidence,
                **privacy,
                "registered_mutation_count": self.mutation_count,
            },
        }

    def _pre_register_mutation(
        self,
        kind: str,
        request_id: str,
        *,
        resource_key: str = "",
        resolver: str = "",
        lookup_value: str = "",
    ) -> int:
        """在执行写操作前登记不含凭据的恢复线索并返回账本下标。

        参数为固定操作标签、request ID、可选资源键和安全查找值；返回新记录下标。
        关键约束：明文 Key、密码、Token、Prompt 和 Secret 绝不能作为 lookup 值登记。
        """

        assert kind and request_id
        record = {
            "kind": kind,
            "request_id": request_id,
            "resource_key": resource_key,
            "resolver": resolver,
            "lookup_value": lookup_value,
            "object_id": "",
        }
        self.mutation_ledger.append(record)
        return len(self.mutation_ledger) - 1

    def _bind_mutation(self, index: int, object_id: str) -> None:
        """把成功响应中的稳定 ID 绑定到预登记 mutation，不接受空值或敏感内容。"""

        assert 0 <= index < len(self.mutation_ledger)
        assert isinstance(object_id, str) and _BASE.SAFE_ID_PATTERN.fullmatch(object_id)
        self.mutation_ledger[index]["object_id"] = object_id
        resource_key = self.mutation_ledger[index]["resource_key"]
        if resource_key:
            self.resource_ids[resource_key] = object_id

    def _verify_database_ready(self) -> dict[str, Any]:
        """确认独立库至少为 0040，并核对五张表和八个关键状态机 trigger。"""

        with closing(self._connect()) as connection:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            version = int(version_row[0])
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if str(row[0]) in PRD050_TABLES
            }
            triggers = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
                if str(row[0]) in PRD050_RUNTIME_TRIGGERS
            }
        assert version >= 40, "database migration 0040 has not been applied"
        assert tables == PRD050_TABLES, "PRD-050 runtime tables are incomplete"
        assert triggers == PRD050_RUNTIME_TRIGGERS, "PRD-050 runtime triggers are incomplete"
        return {
            "migration_0040_applied": True,
            "migration_0040_table_count": len(tables),
            "migration_0040_trigger_count": len(triggers),
        }

    def _create_role_users(self) -> None:
        """预登记 developer/viewer 的创建与登录，再复用公开 API 完成真实 RBAC 身份。"""

        records: dict[tuple[str, str], int] = {}
        for role in ("developer", "viewer"):
            email = f"prd048-{role}-{self.suffix}@metahub.local"
            records[(role, "user")] = self._pre_register_mutation(
                f"{role}_user_create",
                f"req_prd048_runtime_user_{role}_{self.suffix}",
                resource_key=f"{role}_user_id",
                resolver="user_email",
                lookup_value=email,
            )
            records[(role, "session")] = self._pre_register_mutation(
                f"{role}_login",
                f"req_prd048_runtime_login_{role}_{self.suffix}",
                resource_key=f"{role}_session_id",
            )
        self.base.role_tokens = self.base._create_role_users()
        for role in ("developer", "viewer"):
            self._bind_mutation(records[(role, "user")], self.resource_ids[f"{role}_user_id"])
            self._bind_mutation(
                records[(role, "session")],
                self.resource_ids[f"{role}_session_id"],
            )

    def _create_cross_tenant_alias(self) -> None:
        """在独立库事务内建立第二租户 Alias，用于真实防枚举攻击。

        所有 ID 都由脚本固定且不含凭据；租户、默认 Environment、Provider、Model、Alias、
        RoutingPolicy 和质量初始行必须同事务完整提交，失败时整体回滚。
        """

        tenant_id = f"tenant_prd050_cross_{self.suffix}"
        provider_id = f"provider_prd050_cross_{self.suffix}"
        model_id = f"model_prd050_cross_{self.suffix}"
        alias_id = f"alias_prd050_cross_{self.suffix}"
        self._pre_register_mutation(
            "cross_tenant_fixture",
            f"sql_prd050_cross_{self.suffix}",
            resolver="cross_tenant",
            lookup_value=tenant_id,
        )
        now = _BASE.utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO tenants(
                  tenant_id, slug, name, service_mode, status, created_at, updated_at
                ) VALUES(?, ?, ?, 'internal_only', 'active', ?, ?)
                """,
                (
                    tenant_id,
                    f"prd050-cross-{self.suffix}",
                    f"PRD050 cross tenant {self.suffix}",
                    now,
                    now,
                ),
            )
            environment = connection.execute(
                "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
                (tenant_id,),
            ).fetchone()
            assert environment is not None
            environment_id = str(environment["environment_id"])
            connection.execute(
                """
                INSERT INTO providers(
                  provider_id, tenant_id, provider_code, name, base_url, status,
                  provider_type, auth_type, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'http://127.0.0.1:9/v1', 'active',
                         'openai_compatible', 'none', ?, ?)
                """,
                (
                    provider_id,
                    tenant_id,
                    f"prd050_cross_{self.suffix}",
                    f"PRD050 cross Provider {self.suffix}",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO models(
                  model_id, tenant_id, provider_id, model_alias, upstream_model_name,
                  capabilities, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'prd050-cross-model', '{"chat":true}',
                         'active', ?, ?)
                """,
                (
                    model_id,
                    tenant_id,
                    provider_id,
                    f"prd050-cross-{self.suffix}",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO model_aliases(
                  model_alias_id, tenant_id, alias, environment, environment_id,
                  default_model_id, status, created_at, updated_at
                ) VALUES(?, ?, ?, 'dev', ?, ?, 'active', ?, ?)
                """,
                (
                    alias_id,
                    tenant_id,
                    f"prd050-cross-alias-{self.suffix}",
                    environment_id,
                    model_id,
                    now,
                    now,
                ),
            )
            score = connection.execute(
                """
                SELECT model_quality_score_id FROM model_quality_scores
                WHERE tenant_id=? AND model_id=?
                """,
                (tenant_id, model_id),
            ).fetchone()
            assert score is not None
            score_id = str(score["model_quality_score_id"])
        fixture = {
            "cross_tenant_id": tenant_id,
            "cross_tenant_environment_id": environment_id,
            "cross_tenant_provider_id": provider_id,
            "cross_tenant_model_id": model_id,
            "cross_tenant_score_id": score_id,
            "cross_tenant_alias_id": alias_id,
        }
        # 事务已经提交后才把完整图交给清理账本；若这一极窄窗口抛错，预登记 resolver
        # 会按 tenant 和确定性对象 ID 重新定位全部资源，不能把“未写账本”误解成“未提交”。
        self._bind_cross_tenant_fixture_resources(fixture)

    def _bind_cross_tenant_fixture_resources(self, fixture: dict[str, str]) -> None:
        """把已提交的第二租户完整稳定 ID 图绑定到清理账本。

        参数是事务内得到的六个安全 ID；返回值为无。独立方法让测试可在提交后注入异常，
        证明 finally 能从预登记 resolver 恢复，而不是依赖本次内存更新一定成功。
        """

        assert set(fixture) == {
            "cross_tenant_id",
            "cross_tenant_environment_id",
            "cross_tenant_provider_id",
            "cross_tenant_model_id",
            "cross_tenant_score_id",
            "cross_tenant_alias_id",
        }
        assert all(_BASE.SAFE_ID_PATTERN.fullmatch(value) for value in fixture.values())
        self.resource_ids.update(fixture)

    def _create_route(
        self,
        environment: dict[str, str],
        receiver: AcceptanceServer,
    ) -> dict[str, Any]:
        """通过公开 API 创建本地 bearer Provider、两把 Key、Model 和 fixed Alias。

        参数：当前租户 Environment 安全摘要与回环接收器；返回 Provider、Model、Alias
        和 Environment 的稳定 ID/名称。关键约束：每次写操作先登记 mutation；两把 Key
        明文只交给 Control API 和接收器内存，响应、资源账本与异常都不得保存明文或前缀。
        """

        host, port = receiver.server_address
        provider_code = f"prd050_runtime_{self.suffix}"
        provider_request_id = f"req_prd050_runtime_provider_{self.suffix}"
        provider_mutation = self._pre_register_mutation(
            "provider_create",
            provider_request_id,
            resource_key="provider_a_id",
            resolver="provider_code",
            lookup_value=provider_code,
        )
        provider_body = _BASE.expect_json(
            self.control.post(
                "/providers",
                headers=self.base._headers(self.base.admin_token, provider_request_id),
                json={
                    "provider_code": provider_code,
                    "name": f"PRD050 local Provider {self.suffix}",
                    "provider_type": "openai_compatible",
                    "base_url": f"http://{host}:{port}/v1",
                    "auth_type": "bearer",
                    "description": "PRD-050 isolated local OpenAI-compatible acceptance",
                },
            ),
            201,
            provider_request_id,
        )
        provider = provider_body.get("provider")
        assert isinstance(provider, dict) and provider.get("provider_code") == provider_code
        provider_id = str(provider["provider_id"])
        self._bind_mutation(provider_mutation, provider_id)

        for label in ("a", "b"):
            plain_value = f"sk-prd050-local-{secrets.token_urlsafe(24)}"
            self.provider_keys.append(plain_value)
            name = f"PRD050 runtime Key {label.upper()} {self.suffix}"
            operation_id = f"req_prd050_runtime_provider_key_{label}_{self.suffix}"
            mutation = self._pre_register_mutation(
                f"provider_key_{label}_create",
                operation_id,
                resource_key=f"provider_key_{label}_id",
                resolver="provider_key_name",
                lookup_value=name,
            )
            key_body = _BASE.expect_json(
                self.control.post(
                    f"/providers/{provider_id}/keys",
                    headers=self.base._headers(self.base.admin_token, operation_id),
                    json={"name": name, "plain_value": plain_value, "weight": 100},
                ),
                201,
                operation_id,
            )
            key_info = key_body.get("provider_key")
            assert isinstance(key_info, dict) and key_info.get("status") == "active"
            assert plain_value not in json.dumps(key_body, ensure_ascii=False)
            self._bind_mutation(mutation, str(key_info["provider_key_id"]))
        receiver.install_provider_keys(self.provider_keys)

        internal_model_id = f"prd050-runtime-{self.suffix}"
        model_request_id = f"req_prd050_runtime_model_{self.suffix}"
        model_mutation = self._pre_register_mutation(
            "model_create",
            model_request_id,
            resource_key="model_a_id",
            resolver="model_alias",
            lookup_value=internal_model_id,
        )
        model_body = _BASE.expect_json(
            self.control.post(
                "/models",
                headers=self.base._headers(self.base.admin_token, model_request_id),
                json={
                    "internal_model_id": internal_model_id,
                    "provider_id": provider_id,
                    "provider_model_id": "prd050-cache-model",
                    "display_name": f"PRD050 Cache Model {self.suffix}",
                    "capabilities": {"chat": True, "stream": True, "embeddings": False},
                    "context_window": 8192,
                    "input_price_per_1k": 1,
                    "output_price_per_1k": 1,
                    "currency": "CNY",
                    "price_schema_version": "prd050-runtime-v1",
                    "description": "PRD-050 isolated runtime acceptance",
                    "status": "active",
                },
            ),
            201,
            model_request_id,
        )
        model = model_body.get("model")
        assert isinstance(model, dict)
        model_id = str(model["model_id"])
        self._bind_mutation(model_mutation, model_id)

        alias_name = f"prd050-runtime-{self.suffix}"
        alias_request_id = f"req_prd050_runtime_alias_{self.suffix}"
        alias_mutation = self._pre_register_mutation(
            "model_alias_create",
            alias_request_id,
            resource_key="alias_id",
            resolver="alias_name",
            lookup_value=alias_name,
        )
        alias_body = _BASE.expect_json(
            self.control.post(
                "/model-aliases",
                headers=self.base._headers(self.base.admin_token, alias_request_id),
                json={
                    "alias": alias_name,
                    "environment_id": environment["environment_id"],
                    "default_model_id": model_id,
                    "description": "PRD-050 cache affinity runtime acceptance",
                    "status": "active",
                },
            ),
            201,
            alias_request_id,
        )
        alias = alias_body.get("model_alias")
        assert isinstance(alias, dict) and alias.get("cache_affinity") is None
        policy = alias.get("routing_policy")
        assert isinstance(policy, dict) and policy.get("routing_mode") == "fixed"
        alias_id = str(alias["model_alias_id"])
        self._bind_mutation(alias_mutation, alias_id)
        self.resource_ids["alias_name"] = alias_name
        self.resource_ids["routing_policy_id"] = str(policy["routing_policy_id"])
        return {
            "provider_id": provider_id,
            "provider_code": provider_code,
            "model_id": model_id,
            "alias_id": alias_id,
            "alias_name": alias_name,
            "environment_id": environment["environment_id"],
        }

    def _create_virtual_key(self, environment_id: str) -> str:
        """创建一次性 Virtual Key；明文只进入当前对象和 Gateway Authorization Header。"""

        name = f"PRD050 runtime Virtual Key {self.suffix}"
        operation_id = f"req_prd050_runtime_virtual_key_{self.suffix}"
        mutation = self._pre_register_mutation(
            "virtual_key_create",
            operation_id,
            resource_key="virtual_key_id",
            resolver="virtual_key_name",
            lookup_value=name,
        )
        body = _BASE.expect_json(
            self.control.post(
                "/virtual-keys",
                headers=self.base._headers(self.base.admin_token, operation_id),
                json={
                    "name": name,
                    "environment_id": environment_id,
                    "description": "PRD-050 isolated runtime acceptance",
                },
            ),
            201,
            operation_id,
        )
        info = body.get("virtual_key")
        plain_value = str(body.get("plain_value", ""))
        assert isinstance(info, dict) and plain_value.startswith("mhv_")
        self._bind_mutation(mutation, str(info["virtual_key_id"]))
        return plain_value

    def _create_webhook(self, receiver: AcceptanceServer) -> None:
        """创建只订阅 CacheAffinity 的本地 HMAC Endpoint，并登记一次性 Secret。"""

        host, port = receiver.server_address
        name = f"PRD050 runtime Webhook {self.suffix}"
        operation_id = f"req_prd050_runtime_webhook_{self.suffix}"
        mutation = self._pre_register_mutation(
            "webhook_create",
            operation_id,
            resource_key="webhook_endpoint_id",
            resolver="webhook_name",
            lookup_value=name,
        )
        body = _BASE.expect_json(
            self.control.post(
                "/webhooks",
                headers=self.base._headers(self.base.admin_token, operation_id),
                json={
                    "name": name,
                    "endpoint_url": f"http://{host}:{port}/hook",
                    "event_types": ["cache.affinity_selected"],
                    "timeout_ms": 2000,
                    "status": "active",
                },
            ),
            200,
            operation_id,
        )
        webhook = body.get("webhook")
        secret = str(body.get("signing_secret", ""))
        assert isinstance(webhook, dict) and secret
        self._bind_mutation(mutation, str(webhook["webhook_endpoint_id"]))
        self.base.webhook_secret = secret
        receiver.signing_secret = secret

    def _read_alias(self, token: str, route: dict[str, Any], *, label: str) -> dict[str, Any]:
        """读取当前租户 Alias 列表并返回目标行，禁止客户端发送 tenant_id。"""

        operation_id = f"req_prd050_runtime_alias_get_{label}_{self.suffix}"
        response = self.control.get(
            "/model-aliases",
            params={"environment_id": route["environment_id"]},
            headers=self.base._headers(token, operation_id),
        )
        body = _BASE.expect_json(response, 200, operation_id)
        aliases = body.get("model_aliases")
        assert isinstance(aliases, list)
        assert all(isinstance(item, dict) for item in aliases)
        alias_ids = [str(item.get("model_alias_id", "")) for item in aliases]
        # 第二租户 Alias 在本轮调用前已经真实提交；admin/developer 任一列表若出现该 ID，
        # 即代表后端租户过滤失效，不能只筛出主 Alias 后继续把验收判为通过。
        assert self.resource_ids["cross_tenant_alias_id"] not in alias_ids
        assert all(item.get("tenant_id") == self.resource_ids["main_tenant_id"] for item in aliases)
        matches = [
            item
            for item in aliases
            if isinstance(item, dict) and item.get("model_alias_id") == route["alias_id"]
        ]
        assert len(matches) == 1
        return matches[0]

    @staticmethod
    def _assert_cache_affinity_projection(
        value: object,
        *,
        tenant_id: str,
        alias_id: str,
    ) -> dict[str, Any]:
        """核对 GET/PATCH 共用的十字段安全配置投影并返回类型收窄结果。"""

        assert isinstance(value, dict)
        assert set(value) == {
            "cache_affinity_id",
            "tenant_id",
            "model_alias_id",
            "status",
            "data_class",
            "minimum_prefix_chars",
            "ttl_seconds",
            "version",
            "created_at",
            "updated_at",
        }
        assert value["tenant_id"] == tenant_id
        assert value["model_alias_id"] == alias_id
        return value

    def _verify_configuration_contract(self, route: dict[str, Any]) -> dict[str, Any]:
        """验证严格 DTO、RBAC、CAS、幂等、409 GET 刷新和第二租户防枚举。"""

        tenant_id = self.resource_ids["main_tenant_id"]
        assert (
            self._read_alias(self.base.admin_token, route, label="admin_before")["cache_affinity"]
            is None
        )
        assert (
            self._read_alias(self.base.role_tokens["developer"], route, label="developer_before")[
                "cache_affinity"
            ]
            is None
        )

        viewer_get_id = f"req_prd050_runtime_viewer_get_denied_{self.suffix}"
        viewer_get = self.control.get(
            "/model-aliases",
            headers=self.base._headers(self.base.role_tokens["viewer"], viewer_get_id),
        )
        viewer_get_body = _BASE.expect_json(viewer_get, 403, viewer_get_id)
        assert viewer_get_body.get("code") == "auth.permission_denied"

        for role in ("developer", "viewer"):
            operation_id = f"req_prd050_runtime_{role}_patch_denied_{self.suffix}"
            self._pre_register_mutation(f"{role}_patch_denied", operation_id)
            denied = self.control.patch(
                f"/model-aliases/{route['alias_id']}/cache-affinity",
                headers=self.base._headers(self.base.role_tokens[role], operation_id),
                content=b"not-json",
            )
            denied_body = _BASE.expect_json(denied, 403, operation_id)
            assert denied_body.get("code") == "auth.permission_denied"

        desired = {
            "status": "enabled",
            "data_class": "regulated.poc",
            "minimum_prefix_chars": 256,
            "ttl_seconds": 3600,
            "expected_version": 0,
        }
        invalid_id = f"req_prd050_runtime_strict_extra_{self.suffix}"
        self._pre_register_mutation("strict_extra_rejected", invalid_id)
        invalid = self.control.patch(
            f"/model-aliases/{route['alias_id']}/cache-affinity",
            headers=self.base._headers(self.base.admin_token, invalid_id),
            json={**desired, "tenant_id": tenant_id},
        )
        invalid_body = _BASE.expect_json(invalid, 422, invalid_id)
        assert invalid_body.get("code") == "cache_affinity.input_invalid"

        create_id = f"req_prd050_runtime_config_create_{self.suffix}"
        create_trace = create_id.replace("req_", "trc_", 1)
        mutation = self._pre_register_mutation(
            "cache_affinity_create",
            create_id,
            resource_key="cache_affinity_id",
            resolver="cache_affinity_alias",
            lookup_value=route["alias_id"],
        )
        create = self.control.patch(
            f"/model-aliases/{route['alias_id']}/cache-affinity",
            headers=self.base._headers(self.base.admin_token, create_id, create_trace),
            json=desired,
        )
        created = _BASE.expect_json(create, 200, create_id, create_trace)
        assert created.get("changed") is True
        config = self._assert_cache_affinity_projection(
            created.get("cache_affinity"),
            tenant_id=tenant_id,
            alias_id=route["alias_id"],
        )
        assert (
            config["status"],
            config["data_class"],
            config["minimum_prefix_chars"],
            config["ttl_seconds"],
            config["version"],
        ) == ("enabled", "regulated.poc", 256, 3600, 1)
        self._bind_mutation(mutation, str(config["cache_affinity_id"]))

        # 网络结果未知时必须原样复用 actor、路径、request ID、trace 和完整正文。
        replay = _BASE.expect_json(
            self.control.patch(
                f"/model-aliases/{route['alias_id']}/cache-affinity",
                headers=self.base._headers(self.base.admin_token, create_id, create_trace),
                json=desired,
            ),
            200,
            create_id,
            create_trace,
        )
        assert replay.get("changed") is True and replay.get("cache_affinity") == config

        noop_id = f"req_prd050_runtime_config_noop_{self.suffix}"
        self._pre_register_mutation("cache_affinity_noop", noop_id)
        noop_payload = {**desired, "expected_version": 1}
        noop = _BASE.expect_json(
            self.control.patch(
                f"/model-aliases/{route['alias_id']}/cache-affinity",
                headers=self.base._headers(self.base.admin_token, noop_id),
                json=noop_payload,
            ),
            200,
            noop_id,
        )
        assert noop.get("changed") is False and noop.get("cache_affinity") == config

        self._pre_register_mutation("cache_affinity_request_conflict", create_id)
        request_conflict = _BASE.expect_json(
            self.control.patch(
                f"/model-aliases/{route['alias_id']}/cache-affinity",
                headers=self.base._headers(self.base.admin_token, create_id),
                json={**desired, "ttl_seconds": 7200},
            ),
            409,
            create_id,
        )
        assert request_conflict.get("code") == "cache_affinity.request_conflict"

        bad_cas_id = f"req_prd050_runtime_bad_cas_{self.suffix}"
        self._pre_register_mutation("cache_affinity_bad_cas", bad_cas_id)
        bad_cas = _BASE.expect_json(
            self.control.patch(
                f"/model-aliases/{route['alias_id']}/cache-affinity",
                headers=self.base._headers(self.base.admin_token, bad_cas_id),
                json=desired,
            ),
            409,
            bad_cas_id,
        )
        assert bad_cas.get("code") == "cache_affinity.version_conflict"
        assert "details" not in bad_cas and "cache_affinity" not in bad_cas
        refreshed = self._read_alias(
            self.base.role_tokens["developer"],
            route,
            label="developer_refresh_after_409",
        )
        refreshed_config = self._assert_cache_affinity_projection(
            refreshed.get("cache_affinity"),
            tenant_id=tenant_id,
            alias_id=route["alias_id"],
        )
        assert refreshed_config == config

        cross_id = f"req_prd050_runtime_cross_tenant_hidden_{self.suffix}"
        self._pre_register_mutation("cross_tenant_patch_hidden", cross_id)
        hidden = _BASE.expect_json(
            self.control.patch(
                f"/model-aliases/{self.resource_ids['cross_tenant_alias_id']}/cache-affinity",
                headers=self.base._headers(self.base.admin_token, cross_id),
                json=desired,
            ),
            404,
            cross_id,
        )
        assert hidden.get("code") == "model_alias.not_found"
        admin_after = self._read_alias(self.base.admin_token, route, label="admin_after")
        assert admin_after.get("cache_affinity") == config

        return {
            "cache_affinity_id": str(config["cache_affinity_id"]),
            "version": 1,
            "create_request_id": create_id,
            "noop_request_id": noop_id,
            "checks": {
                "admin_patch": True,
                "admin_read": True,
                "developer_read_only": True,
                "viewer_read_denied": True,
                "viewer_write_denied_before_body": True,
                "strict_dto_extra_rejected": True,
                "idempotent_replay": True,
                "noop_version_stable": True,
                "request_conflict": True,
                "version_conflict": True,
                "conflict_refresh_via_same_tenant_get": True,
                "cross_tenant_alias_hidden": True,
            },
        }

    def _send_gateway_call(
        self,
        client: httpx.Client,
        route: dict[str, Any],
        *,
        request_id: str,
        prompt: str,
    ) -> httpx.Response:
        """使用指定客户端向真实 Gateway 发送一次非流式 Chat 请求。

        参数：有硬超时的客户端、路由安全摘要、链路 ID 和进程内 Prompt；返回原始响应供
        调用方统一断言。关键约束：Virtual Key 和 Prompt 只进入当前网络调用，不加入线程
        名称、mutation 账本、异常文本或返回状态。
        """

        trace_id = request_id.replace("req_", "trc_", 1)
        return client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.base.virtual_key}",
                "X-Request-ID": request_id,
                "X-Trace-ID": trace_id,
            },
            json={
                "model": route["alias_name"],
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": self.user_prompt},
                ],
                "max_completion_tokens": 2,
            },
        )

    def _assert_gateway_success(self, response: httpx.Response, request_id: str) -> None:
        """核对真实 Gateway 成功 wire 和内存 Response，不把正文写入状态。"""

        body = _BASE.expect_json(
            response,
            200,
            request_id,
            request_id.replace("req_", "trc_", 1),
        )
        assert body.get("choices", [{}])[0].get("message", {}).get("content") == (
            self.response_sentinel
        )

    def _selection_execution_fact(self, request_id: str) -> dict[str, Any]:
        """读取本轮真实 Selection 的原因、终态和内部 Credential 快照。

        参数是脚本预生成的 request ID；返回只在当前进程使用的三字段字典。查询始终绑定
        当前 tenant+request，Credential ID 只用于真实 Key disable/rebound，不进入状态、
        日志或公开 RequestLog DTO。
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT selection_reason, outcome_status, provider_credential_id_snapshot
                FROM cache_affinity_selections
                WHERE tenant_id=? AND request_id=? AND attempt_sequence=1
                """,
                (self.resource_ids["main_tenant_id"], request_id),
            ).fetchone()
        assert row is not None
        return {
            "selection_reason": str(row["selection_reason"]),
            "outcome_status": str(row["outcome_status"]),
            "provider_credential_id": str(row["provider_credential_id_snapshot"] or ""),
        }

    def _disable_selected_provider_key(self, provider_key_id: str) -> None:
        """通过真实 Control API 禁用已 warm 的临时 Key，为 rebound 制造硬不合格事实。

        参数是 Selection 内部快照且必须属于本轮两把临时 Key；返回值为无。写操作先登记
        mutation，响应只在栈内核对脱敏状态；finally 会再次禁用两把 Key，因此异常和成功
        路径都不会遗留可用凭据，也无需恢复任何客户原有 Key。
        """

        assert provider_key_id in {
            self.resource_ids["provider_key_a_id"],
            self.resource_ids["provider_key_b_id"],
        }
        operation_id = f"req_prd050_runtime_disable_warm_key_{self.suffix}"
        mutation = self._pre_register_mutation("provider_key_disable_for_rebound", operation_id)
        body = _BASE.expect_json(
            self.control.patch(
                f"/provider-keys/{provider_key_id}",
                headers=self.base._headers(self.base.admin_token, operation_id),
                json={"status": "disabled"},
            ),
            200,
            operation_id,
        )
        provider_key = body.get("provider_key")
        assert isinstance(provider_key, dict)
        assert provider_key.get("provider_key_id") == provider_key_id
        assert provider_key.get("status") == "disabled"
        for sensitive in self.provider_keys:
            assert sensitive not in json.dumps(body, ensure_ascii=False)
        self._bind_mutation(mutation, provider_key_id)

    def _exercise_gateway(self, route: dict[str, Any]) -> dict[str, str]:
        """真实生成 created、pending、warm、rebound 和短 Prompt 五类请求。

        前两个 request ID 在主线程预登记后并发发送；假上游在两者均进入 Provider 前不返回，
        因而第二个 Selection 必须看到 pending 而不是 warm。两者成功后再产生 warm，随后用
        Control API 禁用其 Credential 并调用同一 Prompt 产生 rebound，最后验证短 Prompt。
        """

        pair_ids = (
            f"req_prd050_runtime_concurrent_a_{self.suffix}",
            f"req_prd050_runtime_concurrent_b_{self.suffix}",
        )
        for index, request_id in enumerate(pair_ids, start=1):
            self._pre_register_mutation(f"gateway_concurrent_{index}", request_id)

        def run_concurrent(request_id: str) -> httpx.Response:
            """为一个并发请求创建独立连接池，避免跨线程共享 HTTP 客户端状态。"""

            with httpx.Client(base_url=self.args.gateway_url, timeout=HTTP_TIMEOUT) as client:
                return self._send_gateway_call(
                    client,
                    route,
                    request_id=request_id,
                    prompt=self.long_prompt,
                )

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="prd050-pair") as executor:
            futures = {
                request_id: executor.submit(run_concurrent, request_id) for request_id in pair_ids
            }
            responses = {
                request_id: future.result(timeout=CONCURRENT_REQUEST_WAIT_SECONDS)
                for request_id, future in futures.items()
            }
        for request_id, response in responses.items():
            self._assert_gateway_success(response, request_id)

        pair_facts = {
            request_id: self._selection_execution_fact(request_id) for request_id in pair_ids
        }
        assert all(fact["outcome_status"] == "succeeded" for fact in pair_facts.values())
        by_reason = {
            fact["selection_reason"]: request_id for request_id, fact in pair_facts.items()
        }
        assert set(by_reason) == {"binding_created", "binding_pending_reused"}
        created_id = by_reason["binding_created"]
        pending_id = by_reason["binding_pending_reused"]
        first_credential = pair_facts[created_id]["provider_credential_id"]
        assert first_credential and (
            first_credential == pair_facts[pending_id]["provider_credential_id"]
        )

        warm_id = f"req_prd050_runtime_warm_{self.suffix}"
        self._pre_register_mutation("gateway_warm", warm_id)
        warm_response = self._send_gateway_call(
            self.gateway,
            route,
            request_id=warm_id,
            prompt=self.long_prompt,
        )
        self._assert_gateway_success(warm_response, warm_id)
        warm_fact = self._selection_execution_fact(warm_id)
        assert warm_fact == {
            "selection_reason": "binding_warm_reused",
            "outcome_status": "succeeded",
            "provider_credential_id": first_credential,
        }

        self._disable_selected_provider_key(first_credential)
        rebound_id = f"req_prd050_runtime_rebound_{self.suffix}"
        self._pre_register_mutation("gateway_rebound", rebound_id)
        rebound_response = self._send_gateway_call(
            self.gateway,
            route,
            request_id=rebound_id,
            prompt=self.long_prompt,
        )
        self._assert_gateway_success(rebound_response, rebound_id)
        rebound_fact = self._selection_execution_fact(rebound_id)
        assert rebound_fact["selection_reason"] == "binding_rebound"
        assert rebound_fact["outcome_status"] == "succeeded"
        assert rebound_fact["provider_credential_id"] in {
            self.resource_ids["provider_key_a_id"],
            self.resource_ids["provider_key_b_id"],
        }
        assert rebound_fact["provider_credential_id"] != first_credential

        short_id = f"req_prd050_runtime_short_{self.suffix}"
        self._pre_register_mutation("gateway_short_legacy", short_id)
        short_response = self._send_gateway_call(
            self.gateway,
            route,
            request_id=short_id,
            prompt=self.short_prompt,
        )
        self._assert_gateway_success(short_response, short_id)

        requests = {
            "cache_affinity_created": created_id,
            "cache_affinity_pending": pending_id,
            "cache_affinity_warm": warm_id,
            "cache_affinity_rebound": rebound_id,
            "short_legacy": short_id,
        }
        assert len(set(requests.values())) == 5
        self.request_ids.update(requests)
        return requests

    def _verify_provider_calls(self, receiver: AcceptanceServer) -> dict[str, Any]:
        """核对并发粘 K1、warm 复用、禁用后 rebound 到 K2 和短请求旧路径。"""

        calls = receiver.provider_calls()
        assert [item["prompt_kind"] for item in calls] == [
            "long",
            "long",
            "long",
            "long",
            "short",
        ]
        assert [item["cache_tokens"] for item in calls] == [17, 17, 0, 0, 5]
        slots = [str(item["authorization_slot"]) for item in calls]
        assert len(set(slots[:3])) == 1
        assert slots[3] != slots[0]
        assert slots[4] == slots[3]
        assert all(item["model"] == "prd050-cache-model" for item in calls)
        return {
            "local_openai_compatible_provider": True,
            "provider_call_count": len(calls),
            "concurrent_long_prompt_same_managed_key": True,
            "warm_prompt_same_managed_key": True,
            "rebound_selected_second_managed_key": True,
            "short_prompt_legacy_keypool": True,
            "provider_cache_tokens_sequence": [17, 17, 0, 0, 5],
        }

    def _request_log_detail(self, source_request_id: str, *, label: str) -> dict[str, Any]:
        """由 developer 有界读取 RequestLog，并拒绝响应中的本轮敏感明文。"""

        operation_id = f"req_prd050_runtime_log_{label}_{self.suffix}"
        deadline = time.monotonic() + EVENT_WAIT_SECONDS
        response: httpx.Response | None = None
        while time.monotonic() < deadline:
            response = self.control.get(
                f"/request-logs/{source_request_id}",
                headers=self.base._headers(self.base.role_tokens["developer"], operation_id),
            )
            if response.status_code == 200:
                break
            time.sleep(0.1)
        assert response is not None
        body = _BASE.expect_json(response, 200, operation_id)
        assert response.headers.get("cache-control", "") == "no-store, private"
        for sensitive in self.sensitive_values():
            assert sensitive not in response.text
        detail = body.get("request_log")
        assert isinstance(detail, dict)
        return detail

    def _verify_request_logs(
        self,
        request_ids: dict[str, str],
        route: dict[str, Any],
        configuration: dict[str, Any],
    ) -> dict[str, Any]:
        """核对 Selection 白名单、冷到 warm、短请求无事实及 cache_tokens 独立口径。"""

        details = {
            label: self._request_log_detail(request_id, label=label)
            for label, request_id in request_ids.items()
        }
        expected = {
            "cache_affinity_created": ("binding_created", False, 17),
            "cache_affinity_pending": ("binding_pending_reused", False, 17),
            "cache_affinity_warm": ("binding_warm_reused", True, 0),
            "cache_affinity_rebound": ("binding_rebound", False, 0),
        }
        selection_ids: dict[str, str] = {}
        for label, (reason, estimated_hit, cache_tokens) in expected.items():
            selections = details[label].get("cache_affinity_selections")
            assert isinstance(selections, list) and len(selections) == 1
            selection = selections[0]
            assert isinstance(selection, dict) and set(selection) == SAFE_SELECTION_FIELDS
            assert selection["cache_affinity_id"] == configuration["cache_affinity_id"]
            assert selection["cache_affinity_version"] == configuration["version"]
            assert selection["attempt_sequence"] == 1
            assert selection["selection_reason"] == reason
            assert selection["estimated_hit"] is estimated_hit
            # fixed 模式只复用目标，不允许端点偏好改写原有主备排序。
            assert selection["endpoint_preference_applied"] is False
            assert selection["target_kind"] == "managed"
            assert selection["model_id"] == route["model_id"]
            assert selection["provider_id"] == route["provider_id"]
            assert selection["provider_code"] == route["provider_code"]
            selection_ids[label] = str(selection["cache_affinity_selection_id"])
            usage = details[label].get("usage")
            assert isinstance(usage, list) and len(usage) == 1
            assert usage[0]["cache_tokens"] == cache_tokens
            assert usage[0]["normalized_usage"]["cache_tokens"] == cache_tokens

        short_selections = details["short_legacy"].get("cache_affinity_selections")
        assert short_selections == []
        short_usage = details["short_legacy"].get("usage")
        assert isinstance(short_usage, list) and len(short_usage) == 1
        assert short_usage[0]["cache_tokens"] == 5
        # 两组反例明确冻结“预计复用”和“上游报告缓存相关 Token”互不推导。
        assert expected["cache_affinity_created"][1:] == (False, 17)
        assert expected["cache_affinity_warm"][1:] == (True, 0)
        return {
            "selection_ids": selection_ids,
            "checks": {
                "first_long_prompt_estimated_miss": True,
                "concurrent_prompt_pending_reused": True,
                "successful_warm_then_estimated_hit": True,
                "disabled_warm_key_triggered_rebound": True,
                "short_prompt_has_no_selection": True,
                "request_log_selection_safe_fields": True,
                "provider_cache_tokens_not_derived_from_estimate": True,
                "fixed_mode_not_reordered": True,
            },
        }

    def _wait_for_webhooks(
        self,
        receiver: AcceptanceServer,
        configuration: dict[str, Any],
        request_ids: dict[str, str],
        log_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """有界等待一个配置事件和四种真实 runtime Selection，并核对 HMAC 与隐私。"""

        expected_request_ids = {
            configuration["create_request_id"],
            request_ids["cache_affinity_created"],
            request_ids["cache_affinity_pending"],
            request_ids["cache_affinity_warm"],
            request_ids["cache_affinity_rebound"],
        }
        deadline = time.monotonic() + EVENT_WAIT_SECONDS
        unique: dict[str, dict[str, Any]] = {}
        while time.monotonic() < deadline:
            events = receiver.webhook_events()
            unique = {
                str(event["event_id"]): event
                for event in events
                if isinstance(event.get("payload"), dict)
                and event["payload"].get("request_id") in expected_request_ids
            }
            observed = {event["payload"].get("request_id") for event in unique.values()}
            # 配置与 created/pending/warm/rebound 五个源请求必须全部出现才可结束等待；
            # 仅凭总数退出会把同一幂等配置事件的重复投递误判为四种运行证据齐全。
            if observed == expected_request_ids:
                break
            time.sleep(0.1)
        assert {
            event["payload"].get("request_id") for event in unique.values()
        } == expected_request_ids

        config_events = [
            event
            for event in unique.values()
            if event["payload"].get("trigger") == "configuration_changed"
        ]
        runtime_events = [
            event
            for event in unique.values()
            if event["payload"].get("trigger") == "request_selected"
        ]
        assert len(config_events) == 1 and len(runtime_events) == 4
        config_event = config_events[0]
        _assert_configuration_webhook_event(
            config_event,
            tenant_id=self.resource_ids["main_tenant_id"],
            request_id=configuration["create_request_id"],
            actor_id=self.resource_ids["admin_user_id"],
            cache_affinity_id=configuration["cache_affinity_id"],
            cache_affinity_version=configuration["version"],
            model_alias_id=self.resource_ids["alias_id"],
        )

        by_request = {event["payload"]["request_id"]: event for event in runtime_events}
        expected_runtime = {
            request_ids["cache_affinity_created"]: (
                "cache_affinity_created",
                "binding_created",
                False,
            ),
            request_ids["cache_affinity_pending"]: (
                "cache_affinity_pending",
                "binding_pending_reused",
                False,
            ),
            request_ids["cache_affinity_warm"]: (
                "cache_affinity_warm",
                "binding_warm_reused",
                True,
            ),
            request_ids["cache_affinity_rebound"]: (
                "cache_affinity_rebound",
                "binding_rebound",
                False,
            ),
        }
        for request_id, (label, reason, estimated_hit) in expected_runtime.items():
            _assert_runtime_webhook_event(
                by_request[request_id],
                tenant_id=self.resource_ids["main_tenant_id"],
                request_id=request_id,
                selection_id=log_evidence["selection_ids"][label],
                cache_affinity_id=configuration["cache_affinity_id"],
                cache_affinity_version=configuration["version"],
                model_alias_id=self.resource_ids["alias_id"],
                model_endpoint_id=self.resource_ids["model_a_id"],
                selected_provider_id=self.resource_ids["provider_a_id"],
                selection_reason=reason,
                estimated_hit=estimated_hit,
            )
        assert all(
            event["payload"].get("request_id") != request_ids["short_legacy"]
            for event in receiver.webhook_events()
        )
        return {
            "webhook_unique_event_count": len(unique),
            "webhook_hmac_valid": True,
            "webhook_configuration_event": True,
            "webhook_runtime_selection_events": 4,
            "webhook_short_prompt_absent": True,
            "webhook_payload_privacy": True,
        }

    def _verify_database(
        self,
        configuration: dict[str, Any],
        request_ids: dict[str, str],
        log_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """核对配置、Selection、warm、用量、事件、租户隔离、pending 和完整性。"""

        tenant_id = self.resource_ids["main_tenant_id"]
        with closing(self._connect()) as connection:
            config = connection.execute(
                """
                SELECT cache_affinity_id, version, status, data_class,
                       minimum_prefix_chars, ttl_seconds
                FROM cache_affinities WHERE tenant_id=? AND model_alias_id=?
                """,
                (tenant_id, self.resource_ids["alias_id"]),
            ).fetchone()
            assert config is not None and tuple(config) == (
                configuration["cache_affinity_id"],
                1,
                "enabled",
                "regulated.poc",
                256,
                3600,
            )
            rows = connection.execute(
                """
                SELECT request_id, cache_affinity_selection_id, selection_reason,
                       estimated_hit, outcome_status, provider_credential_id_snapshot
                FROM cache_affinity_selections
                WHERE tenant_id=? AND request_id IN (?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    request_ids["cache_affinity_created"],
                    request_ids["cache_affinity_pending"],
                    request_ids["cache_affinity_warm"],
                    request_ids["cache_affinity_rebound"],
                ),
            ).fetchall()
            selections = {str(row["request_id"]): row for row in rows}
            assert set(selections) == {
                request_ids["cache_affinity_created"],
                request_ids["cache_affinity_pending"],
                request_ids["cache_affinity_warm"],
                request_ids["cache_affinity_rebound"],
            }
            expected_selections = {
                "cache_affinity_created": ("binding_created", 0),
                "cache_affinity_pending": ("binding_pending_reused", 0),
                "cache_affinity_warm": ("binding_warm_reused", 1),
                "cache_affinity_rebound": ("binding_rebound", 0),
            }
            for label, (reason, estimated_hit) in expected_selections.items():
                row = selections[request_ids[label]]
                assert tuple(row)[1:5] == (
                    log_evidence["selection_ids"][label],
                    reason,
                    estimated_hit,
                    "succeeded",
                )
            created = selections[request_ids["cache_affinity_created"]]
            pending = selections[request_ids["cache_affinity_pending"]]
            warm = selections[request_ids["cache_affinity_warm"]]
            rebound = selections[request_ids["cache_affinity_rebound"]]
            first_credential = str(created["provider_credential_id_snapshot"])
            rebound_credential = str(rebound["provider_credential_id_snapshot"])
            assert first_credential == pending["provider_credential_id_snapshot"]
            assert first_credential == warm["provider_credential_id_snapshot"]
            assert first_credential in {
                self.resource_ids["provider_key_a_id"],
                self.resource_ids["provider_key_b_id"],
            }
            assert rebound_credential in {
                self.resource_ids["provider_key_a_id"],
                self.resource_ids["provider_key_b_id"],
            }
            assert rebound_credential != first_credential
            credential_statuses = {
                str(row["provider_credential_id"]): str(row["status"])
                for row in connection.execute(
                    """
                    SELECT provider_credential_id, status FROM provider_credentials
                    WHERE tenant_id=? AND provider_credential_id IN (?, ?)
                    """,
                    (tenant_id, first_credential, rebound_credential),
                )
            }
            # K1 由真实 Control API 禁用，K2 在 rebound 与短请求后仍 active；finally 会再将
            # 两者都禁用，因此此处只验证重绑前提而不把 Credential ID 写入状态。
            assert credential_statuses == {
                first_credential: "disabled",
                rebound_credential: "active",
            }
            short_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM cache_affinity_selections
                    WHERE tenant_id=? AND request_id=?
                    """,
                    (tenant_id, request_ids["short_legacy"]),
                ).fetchone()[0]
            )
            scope_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM cache_affinity_scopes
                    WHERE tenant_id=? AND virtual_key_id=?
                    """,
                    (tenant_id, self.resource_ids["virtual_key_id"]),
                ).fetchone()[0]
            )
            binding = connection.execute(
                """
                SELECT selection_count, estimated_hit_count, active_selection_count,
                       last_success_at
                FROM cache_affinity_bindings
                WHERE tenant_id=? AND cache_affinity_scope_id IN (
                  SELECT cache_affinity_scope_id FROM cache_affinity_scopes
                  WHERE tenant_id=? AND virtual_key_id=?
                )
                """,
                (tenant_id, tenant_id, self.resource_ids["virtual_key_id"]),
            ).fetchone()
            assert binding is not None
            assert tuple(binding)[:3] == (4, 1, 0) and binding["last_success_at"] is not None
            pending_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM cache_affinity_selections
                    WHERE tenant_id=? AND outcome_status='pending'
                    """,
                    (tenant_id,),
                ).fetchone()[0]
            )
            active_count = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(active_selection_count), 0)
                    FROM cache_affinity_bindings WHERE tenant_id=?
                    """,
                    (tenant_id,),
                ).fetchone()[0]
            )
            usage = {
                str(row["request_id"]): int(row["cache_tokens"])
                for row in connection.execute(
                    """
                    SELECT request_id, cache_tokens FROM usage_ledger
                    WHERE tenant_id=? AND request_id IN (?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        request_ids["cache_affinity_created"],
                        request_ids["cache_affinity_pending"],
                        request_ids["cache_affinity_warm"],
                        request_ids["cache_affinity_rebound"],
                        request_ids["short_legacy"],
                    ),
                )
            }
            config_events = connection.execute(
                """
                SELECT cache_affinity_config_event_id, event_type, operation_request_id
                FROM cache_affinity_config_events
                WHERE tenant_id=? AND cache_affinity_id=?
                ORDER BY created_at, cache_affinity_config_event_id
                """,
                (tenant_id, configuration["cache_affinity_id"]),
            ).fetchall()
            created_event_ids = [
                str(row["cache_affinity_config_event_id"])
                for row in config_events
                if row["event_type"] == "created"
                and row["operation_request_id"] == configuration["create_request_id"]
            ]
            # 配置 delivery 必须绑定本轮唯一 cae_ ConfigEvent，不能用 caf_ 配置 ID或租户历史
            # 事件总数代替，否则旧验收事实可能掩盖本轮配置 Webhook 根本没有投递。
            assert len(created_event_ids) == 1 and created_event_ids[0].startswith("cae_")
            audit_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs
                    WHERE tenant_id=? AND object_type='cache_affinity'
                      AND object_id=? AND action='cache_affinity.created'
                    """,
                    (tenant_id, configuration["cache_affinity_id"]),
                ).fetchone()[0]
            )
            delivery_count = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT event_id) FROM webhook_deliveries
                    WHERE tenant_id=? AND event_type='cache.affinity_selected'
                      AND event_id IN (?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        log_evidence["selection_ids"]["cache_affinity_created"],
                        log_evidence["selection_ids"]["cache_affinity_pending"],
                        log_evidence["selection_ids"]["cache_affinity_warm"],
                        log_evidence["selection_ids"]["cache_affinity_rebound"],
                        created_event_ids[0],
                    ),
                ).fetchone()[0]
            )
            hidden_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM cache_affinities
                    WHERE tenant_id=? AND model_alias_id=?
                    """,
                    (
                        self.resource_ids["cross_tenant_id"],
                        self.resource_ids["cross_tenant_alias_id"],
                    ),
                ).fetchone()[0]
            )
            foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchall()

        assert short_count == 0 and scope_count == 1
        assert pending_count == 0 and active_count == 0
        assert usage == {
            request_ids["cache_affinity_created"]: 17,
            request_ids["cache_affinity_pending"]: 17,
            request_ids["cache_affinity_warm"]: 0,
            request_ids["cache_affinity_rebound"]: 0,
            request_ids["short_legacy"]: 5,
        }
        # SQLite 的 created_at 只有秒级精度，同一秒内的随机事件 ID 不能表达因果顺序。
        _assert_exact_config_event_pairs(
            config_events,
            create_request_id=configuration["create_request_id"],
            noop_request_id=configuration["noop_request_id"],
        )
        assert audit_count == 1 and hidden_count == 0
        assert delivery_count == 5
        assert foreign_key_issues == []
        assert [tuple(row) for row in integrity] == [("ok",)]
        return {
            "database_selection_count": len(rows),
            "database_scope_count": scope_count,
            "database_binding_selection_count": int(binding["selection_count"]),
            "database_binding_estimated_hit_count": int(binding["estimated_hit_count"]),
            "database_config_event_count": len(config_events),
            "database_cache_affinity_audit_count": audit_count,
            "database_webhook_delivery_count": delivery_count,
            "database_pending_selection_count": pending_count,
            "database_active_selection_count": active_count,
            "foreign_key_check_clean": True,
            "integrity_check_ok": True,
        }

    def _verify_privacy(self) -> dict[str, int]:
        """扫描独立 SQLite 全表和显式服务日志，证明本轮敏感明文未落盘。"""

        sensitive = self.sensitive_values()
        database_hits = 0
        with closing(self._connect()) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                columns = [
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({_BASE.quote_identifier(table)})"
                    )
                ]
                if not columns:
                    continue
                predicate = " OR ".join(
                    f"instr(CAST({_BASE.quote_identifier(column)} AS TEXT), ?) > 0"
                    for column in columns
                )
                query = f"SELECT COUNT(*) FROM {_BASE.quote_identifier(table)} WHERE {predicate}"
                for value in sensitive:
                    database_hits += int(
                        connection.execute(
                            query,
                            tuple(value for _ in columns),
                        ).fetchone()[0]
                    )
        log_hits = 0
        for path in self.log_paths:
            raw = path.read_bytes()
            for value in sensitive:
                log_hits += raw.count(value.encode("utf-8"))
        assert database_hits == 0
        assert log_hits == 0
        return {
            "database_sensitive_hits": database_hits,
            "log_sensitive_hits": log_hits,
            "log_files_scanned": len(self.log_paths),
        }

    def _resolve_cross_tenant_fixture(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
    ) -> None:
        """按第二租户 owner 和确定性对象 ID 恢复已提交夹具的完整清理图。

        参数是当前独立库连接和预登记 tenant ID；返回值为无。该 resolver 专门覆盖事务已
        提交、但进程尚未来得及更新内存账本的异常窗口，所有查询仍同时限定 tenant 和对象。
        """

        expected_tenant_id = f"tenant_prd050_cross_{self.suffix}"
        assert tenant_id == expected_tenant_id
        provider_id = f"provider_prd050_cross_{self.suffix}"
        model_id = f"model_prd050_cross_{self.suffix}"
        alias_id = f"alias_prd050_cross_{self.suffix}"
        lookups = (
            (
                "cross_tenant_id",
                "SELECT tenant_id AS object_id FROM tenants WHERE tenant_id=?",
                (tenant_id,),
            ),
            (
                "cross_tenant_environment_id",
                """
                SELECT environment_id AS object_id FROM environments
                WHERE tenant_id=? AND code='dev'
                """,
                (tenant_id,),
            ),
            (
                "cross_tenant_provider_id",
                """
                SELECT provider_id AS object_id FROM providers
                WHERE tenant_id=? AND provider_id=?
                """,
                (tenant_id, provider_id),
            ),
            (
                "cross_tenant_model_id",
                """
                SELECT model_id AS object_id FROM models
                WHERE tenant_id=? AND model_id=? AND provider_id=?
                """,
                (tenant_id, model_id, provider_id),
            ),
            (
                "cross_tenant_alias_id",
                """
                SELECT model_alias_id AS object_id FROM model_aliases
                WHERE tenant_id=? AND model_alias_id=? AND default_model_id=?
                """,
                (tenant_id, alias_id, model_id),
            ),
            (
                "cross_tenant_score_id",
                """
                SELECT model_quality_score_id AS object_id FROM model_quality_scores
                WHERE tenant_id=? AND model_id=?
                """,
                (tenant_id, model_id),
            ),
        )
        for resource_key, query, parameters in lookups:
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                continue
            object_id = str(row["object_id"])
            assert _BASE.SAFE_ID_PATTERN.fullmatch(object_id)
            self.resource_ids[resource_key] = object_id

    def _resolve_unbound_resources(self) -> None:
        """按预登记安全 lookup 恢复可能已提交但响应丢失的资源 ID。

        查询类型来自代码内固定 switch，不能由命令行或服务响应提供表名/列名。主业务恢复
        只在 main tenant 内执行，第二租户使用独立 owner resolver；Provider Key 还要再次
        绑定本轮 Provider，避免扩大清理范围。
        """

        tenant_id = self.resource_ids.get("main_tenant_id")
        # 主租户尚未由 /auth/me 冻结或独立库已不存在时，禁止按名称执行宽查询；
        # 已登记的 Session 仍由基础清理使用精确全局 ID 收口。
        if not tenant_id or not self.args.database_path.is_file():
            return
        with closing(self._connect()) as connection:
            for record in self.mutation_ledger:
                resolver = record["resolver"]
                lookup = record["lookup_value"]
                # 第二租户 mutation 没有单一 resource_key；即使提交后账本更新失败，也必须
                # 先恢复完整图，不能被通用“空 resource_key”分支提前跳过。
                if resolver == "cross_tenant":
                    self._resolve_cross_tenant_fixture(connection, lookup)
                    continue
                resource_key = record["resource_key"]
                if not resource_key or self.resource_ids.get(resource_key):
                    continue
                row: sqlite3.Row | None = None
                # Provider code 由脚本在 mutation 前生成且查询继续限定当前 tenant，
                # 用于恢复服务已提交但 HTTP 响应在客户端超时后丢失的 Provider ID。
                if resolver == "provider_code":
                    row = connection.execute(
                        """
                        SELECT provider_id AS object_id FROM providers
                        WHERE tenant_id=? AND provider_code=?
                        """,
                        (tenant_id, lookup),
                    ).fetchone()
                # Model 的 internal_model_id 实际持久化在 model_alias 列；仍需 tenant 条件，
                # 不能用全局名称恢复其他租户恰好同名的模型。
                elif resolver == "model_alias":
                    row = connection.execute(
                        """
                        SELECT model_id AS object_id FROM models
                        WHERE tenant_id=? AND model_alias=?
                        """,
                        (tenant_id, lookup),
                    ).fetchone()
                elif resolver == "alias_name":
                    row = connection.execute(
                        """
                        SELECT model_alias_id AS object_id FROM model_aliases
                        WHERE tenant_id=? AND alias=?
                        """,
                        (tenant_id, lookup),
                    ).fetchone()
                # Provider Key 名称只能在本轮 Provider owner 已恢复后使用；两层 owner 条件
                # 可防止失败清理仅凭展示名称禁用同租户其他 Provider 的凭据，明文 Key 不参与查询。
                elif resolver == "provider_key_name" and self.resource_ids.get("provider_a_id"):
                    row = connection.execute(
                        """
                        SELECT provider_credential_id AS object_id FROM provider_credentials
                        WHERE tenant_id=? AND provider_id=? AND name=?
                        """,
                        (tenant_id, self.resource_ids["provider_a_id"], lookup),
                    ).fetchone()
                elif resolver == "virtual_key_name":
                    row = connection.execute(
                        """
                        SELECT virtual_key_id AS object_id FROM virtual_keys
                        WHERE tenant_id=? AND name=?
                        """,
                        (tenant_id, lookup),
                    ).fetchone()
                elif resolver == "webhook_name":
                    row = connection.execute(
                        """
                        SELECT webhook_endpoint_id AS object_id FROM webhook_endpoints
                        WHERE tenant_id=? AND name=?
                        """,
                        (tenant_id, lookup),
                    ).fetchone()
                elif resolver == "user_email":
                    row = connection.execute(
                        "SELECT user_id AS object_id FROM users WHERE tenant_id=? AND email=?",
                        (tenant_id, lookup),
                    ).fetchone()
                elif resolver == "cache_affinity_alias":
                    row = connection.execute(
                        """
                        SELECT cache_affinity_id AS object_id FROM cache_affinities
                        WHERE tenant_id=? AND model_alias_id=?
                        """,
                        (tenant_id, lookup),
                    ).fetchone()
                if row is not None:
                    object_id = str(row["object_id"])
                    assert _BASE.SAFE_ID_PATTERN.fullmatch(object_id)
                    self.resource_ids[resource_key] = object_id
                    record["object_id"] = object_id

    def cleanup_credentials(self) -> dict[str, Any]:
        """解析不确定 mutation 后禁用两把 Provider Key、跨租户 Alias 和基础资源图。"""

        if not self.args.database_path.is_file():
            return {"verified": True, "changed": 0}
        self._resolve_unbound_resources()
        tenant_id = self.resource_ids.get("main_tenant_id")
        changed = 0
        now = _BASE.utc_now()
        # 所有业务入口只有在可信主租户已冻结时才允许更新；否则保持空操作，避免扩大范围。
        if tenant_id:
            with closing(self._connect()) as connection, connection:
                for label in ("a", "b"):
                    provider_key_id = self.resource_ids.get(f"provider_key_{label}_id")
                    # 每把 Key 只按 tenant+稳定 Credential ID 禁用；不使用名称、前缀或明文匹配，
                    # 重复 cleanup 得到零变更且不会影响客户已有 KeyPool。
                    if provider_key_id:
                        changed += max(
                            connection.execute(
                                """
                                UPDATE provider_credentials
                                SET status='disabled', disabled_at=COALESCE(disabled_at, ?),
                                    updated_at=?
                                WHERE tenant_id=? AND provider_credential_id=?
                                  AND status!='disabled'
                                """,
                                (now, now, tenant_id, provider_key_id),
                            ).rowcount,
                            0,
                        )
                cross_alias = self.resource_ids.get("cross_tenant_alias_id")
                cross_tenant = self.resource_ids.get("cross_tenant_id")
                # 第二租户 Alias 必须同时具备 tenant 与对象 ID 才可禁用，防枚举夹具的清理
                # 不能退化成只凭全局 Alias ID 或名称的跨租户写操作。
                if cross_alias and cross_tenant:
                    changed += max(
                        connection.execute(
                            """
                            UPDATE model_aliases
                            SET status='disabled', disabled_at=COALESCE(disabled_at, ?),
                                updated_at=?
                            WHERE tenant_id=? AND model_alias_id=? AND status!='disabled'
                            """,
                            (now, now, cross_tenant, cross_alias),
                        ).rowcount,
                        0,
                    )
        base_cleanup = self.base.cleanup(require_complete=False)
        changed += sum(
            int(value)
            for key, value in base_cleanup.items()
            if key != "verified" and isinstance(value, int)
        )
        with closing(self._connect()) as connection:
            active_provider_keys = 0
            # 主租户存在时才复核两把 Credential；缺 tenant 的失败路径不能发起宽扫描。
            # IN 列表仍由两个已登记稳定 ID 参数绑定，不接受状态文件提供 SQL。
            if tenant_id:
                active_provider_keys = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM provider_credentials
                        WHERE tenant_id=? AND provider_credential_id IN (?, ?)
                          AND status!='disabled'
                        """,
                        (
                            tenant_id,
                            self.resource_ids.get("provider_key_a_id", ""),
                            self.resource_ids.get("provider_key_b_id", ""),
                        ),
                    ).fetchone()[0]
                )
            active_cross_alias = 0
            # 跨租户验证同样要求预登记 tenant owner，避免空 owner 被解释成默认租户。
            # 精确 Alias ID 证明异常路径没有遗留可继续接收新请求的第二租户入口。
            if self.resource_ids.get("cross_tenant_id"):
                active_cross_alias = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM model_aliases
                        WHERE tenant_id=? AND model_alias_id=? AND status!='disabled'
                        """,
                        (
                            self.resource_ids["cross_tenant_id"],
                            self.resource_ids.get("cross_tenant_alias_id", ""),
                        ),
                    ).fetchone()[0]
                )
        assert base_cleanup["verified"] is True
        assert active_provider_keys == 0 and active_cross_alias == 0
        return {"verified": True, "changed": changed}

    def _connect(self) -> sqlite3.Connection:
        """连接显式独立验收库并始终启用外键约束。"""

        connection = sqlite3.connect(self.args.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def execute_runtime(
    args: argparse.Namespace,
    *,
    acceptance_factory: Any = RuntimeAcceptance,
    server_factory: Any = RunningAcceptanceServer,
) -> dict[str, Any]:
    """运行验收、finally 清理并只在完整通过后以 write-once 语义发布脱敏状态。

    参数：已校验的命令行参数，以及测试可替换的验收器、接收器工厂。返回值：完整脱敏的
    passed 状态。关键约束：入口已有任意同名目录项时必须在创建验收器前固定码拒绝；发布
    竞争也不得覆盖竞争者证据，失败路径仍须完成已发生业务 mutation 的凭据清理。
    """

    _assert_state_output_absent(args.state_output)
    acceptance = acceptance_factory(args)
    cleanup: dict[str, Any] = {"verified": False, "changed": 0}
    sensitive_values: list[str] = []
    mutation_count = 0
    state: dict[str, Any] | None = None
    try:
        with server_factory(
            acceptance.long_prompt,
            acceptance.short_prompt,
            acceptance.response_sentinel,
        ) as receiver:
            candidate = acceptance.run(receiver)
        assert candidate.get("status") == "passed"
        # Runtime 状态是 Browser 与 closeout 的跨进程合同；发布前同时冻结 schema 和
        # PRD 身份，避免旧结构或其他 PRD 的 passed 文件被同批次消费者误接纳。
        schema_version = candidate.get("schema_version")
        assert isinstance(schema_version, int) and not isinstance(schema_version, bool)
        assert schema_version == 1
        assert candidate.get("prd_id") == "PRD-050"
        # CLI generation 由 runner 绑定到批次目录；业务验收器不得用自生成值覆盖该关联键。
        # 兼容当前 run() 尚未组装此字段，但若未来提前写入，则冲突必须失败关闭。
        assert candidate.get("generation", args.generation) == args.generation
        candidate["generation"] = args.generation
        # 必须保留 close 前的敏感值快照；只有接收器正常退出且首次脱敏通过后，candidate
        # 才能成为可落盘 state，避免 finally 清空内存后把失败候选误写成 passed。
        sensitive_values = acceptance.sensitive_values()
        _BASE.assert_redacted_state(candidate, sensitive_values)
        state = candidate
    finally:
        try:
            cleanup = acceptance.cleanup_credentials()
        finally:
            try:
                mutation_count = acceptance.mutation_count
            finally:
                acceptance.close()

    # 代码只会在 run、Context 退出、首次脱敏、cleanup 和 close 全部未抛错时到达这里；
    # cleanup 即使以返回值报告未验证，也必须失败关闭且不能产生 passed 状态文件。
    assert state is not None
    assert cleanup.get("verified") is True, "runtime cleanup verification failed"
    state["lifecycle"] = {
        "credentials_cleanup_completed": True,
        "credentials_cleanup_verified": True,
        "credentials_changed": cleanup["changed"],
        "mutations_pre_registered": mutation_count,
    }
    _BASE.assert_redacted_state(state, sensitive_values)
    _publish_state_once(args.state_output, state)
    return state


def _assert_state_output_absent(path: Path) -> None:
    """拒绝已经存在的状态目录项，且不读取、删除或改写其内容。

    参数：本轮计划发布的状态路径。返回值：无。关键约束：使用 lexists 将普通文件、
    目录、链接以及悬空链接都视为碰撞；异常只携带固定码，使调用方不能借失败输出枚举路径。
    """

    if os.path.lexists(path):
        # 已存在目录项属于上一轮或并发进程，既不能删除也不能泄露其路径和内容；
        # 因此只返回稳定碰撞码，让 runner 明确归档整代候选而不是原地重跑。
        raise RuntimeError(RUNTIME_STATE_OUTPUT_COLLISION_CODE)


def _publish_state_once(path: Path, state: dict[str, Any]) -> None:
    """在目标同目录独占写临时文件，并通过硬链接以 create-if-absent 语义发布状态。

    参数：最终状态路径和已经完成脱敏复核的状态对象。返回值：无。关键约束：临时文件用
    xb 独占创建，写入后执行 flush/fsync；os.link 在目标存在时天然拒绝覆盖。清理只针对
    本函数确实创建成功的随机临时文件，绝不扫描或删除其他进程的同前缀目录项。
    """

    encoded = (
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    stream: Any | None = None
    for _attempt in range(RUNTIME_STATE_TEMPORARY_CREATE_ATTEMPTS):
        candidate = path.with_name(
            f".{path.name}.{secrets.token_hex(16)}.tmp"
        )
        try:
            # xb 是临时文件归属的唯一建立点；只有本分支成功后，finally 才有权清理
            # candidate，随机名碰撞产生的外来文件必须原样保留。
            stream = candidate.open("xb")
        except FileExistsError:
            continue
        temporary = candidate
        break
    if temporary is None or stream is None:
        # 连续独占创建失败说明目录状态不再可信；此时必须固定失败，不能回退到可覆盖
        # 文件名或清理未归属本进程的碰撞文件，否则会破坏 one-shot 证据边界。
        raise RuntimeError("PRD050_RUNTIME_STATE_TEMPORARY_CREATE_FAILED")

    try:
        with stream:
            stream.write(encoded)
            # 先把 Python 缓冲区和操作系统文件缓存同步完成，再让目标目录出现硬链接；
            # 因而消费者不会观察到已发布但尚未完整持久化的半截 JSON。
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # 同目录硬链接只在目标目录项尚不存在时创建；与 replace/rename 不同，它不会
            # 覆盖入口检查后由并发进程发布的证据。
            os.link(temporary, path)
        except FileExistsError:
            raise RuntimeError(RUNTIME_STATE_OUTPUT_COLLISION_CODE) from None
    finally:
        # temporary 只有在本进程 xb 成功后才赋值；这里不做 glob，也不删除碰撞候选。
        # 正常发布后删除临时链接不会影响共享同一 inode 的最终状态文件。
        temporary.unlink(missing_ok=True)


def _artifact_root() -> Path:
    """返回仓库内 PRD-050 独立验收制品根目录。"""

    return (Path.cwd().resolve() / "artifacts" / "prd050").resolve()


def _path_inside(path: Path, root: Path, *, label: str) -> Path:
    """规范化路径并强制其位于 PRD-050 制品根的批次子目录。"""

    resolved = path.resolve()
    assert root in resolved.parents, f"{label} must be inside artifacts/prd050"
    return resolved


def _read_admin_password_from_stdin(stream: TextIO) -> str:
    """从已重定向标准输入读取唯一一行管理员口令，并拒绝空值、多行和超长输入。"""

    source = stream.read(MAX_STDIN_PASSWORD_BYTES + 1)
    assert source, "password stdin is empty"
    assert len(source.encode("utf-8")) <= MAX_STDIN_PASSWORD_BYTES, (
        "password stdin exceeds the bounded input size"
    )
    if source.endswith("\r\n"):
        password = source[:-2]
    elif source.endswith("\n"):
        password = source[:-1]
    else:
        password = source
    # 仅剥离一个终止换行；中间或额外换行表示调用方写入了多条敏感输入，必须失败关闭。
    assert "\r" not in password and "\n" not in password, (
        "password stdin must contain exactly one line"
    )
    assert len(password) >= 8, "password stdin is missing or too short"
    return password


def parse_args(
    argv: list[str] | None = None,
    *,
    password_stream: TextIO | None = None,
) -> argparse.Namespace:
    """解析回环服务、独立数据库、状态、日志和标准输入凭据参数。"""

    # 管理员历史环境变量只做清除，绝不作为认证输入；正式口令只从标准输入读取。
    # 开发者和查看者仅为本批次临时账号，可选覆盖值读取后立即从进程环境删除。
    os.environ.pop("METAHUB_PRD050_ADMIN_PASSWORD", None)
    os.environ.pop("METAHUB_ADMIN_PASSWORD", None)
    developer_password = os.environ.pop("METAHUB_PRD050_DEVELOPER_PASSWORD", "")
    viewer_password = os.environ.pop("METAHUB_PRD050_VIEWER_PASSWORD", "")
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-url", default="http://127.0.0.1:18001")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18000")
    parser.add_argument("--console-url", default="http://127.0.0.1:15173")
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--state-output", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, action="append", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--confirm-local-state-mutation", action="store_true")
    parser.add_argument(
        "--suffix",
        default=datetime.now(UTC).strftime("%Y%m%dt%H%M%S").lower() + secrets.token_hex(3),
    )
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument("--admin-email", default="admin@metahub.local")
    parser.add_argument(
        "--developer-password",
        default=developer_password or f"Prd050Developer!{secrets.token_urlsafe(18)}",
    )
    parser.add_argument(
        "--viewer-password",
        default=viewer_password or f"Prd050Viewer!{secrets.token_urlsafe(18)}",
    )
    args = parser.parse_args(argv)
    assert args.password_stdin, (
        "pass --password-stdin and redirect exactly one administrator password line"
    )
    args.admin_password = _read_admin_password_from_stdin(
        password_stream if password_stream is not None else sys.stdin
    )
    root = _artifact_root()
    for label in ("control_url", "gateway_url", "console_url"):
        _BASE.assert_loopback_url(getattr(args, label), label)
    args.database_path = _path_inside(args.database_path, root, label="database")
    args.state_output = _path_inside(args.state_output, root, label="state output")
    args.log_path = [_path_inside(path, root, label="log path") for path in args.log_path]
    assert args.database_path.is_file() and args.database_path.suffix == ".db"
    assert args.state_output.suffix == ".json"
    assert args.database_path.parent == args.state_output.parent
    assert GENERATION_PATTERN.fullmatch(args.generation)
    assert args.generation == args.database_path.parent.name
    assert all(path.is_file() for path in args.log_path)
    assert all(path.parent == args.database_path.parent for path in args.log_path)
    assert _BASE.SAFE_SUFFIX_PATTERN.fullmatch(args.suffix)
    assert args.confirm_local_state_mutation, (
        "pass --confirm-local-state-mutation for this disposable local acceptance run"
    )
    return args


def main(argv: list[str] | None = None) -> int:
    """执行真实验收并只向终端输出脱敏 JSON 状态。"""

    state = execute_runtime(parse_args(argv))
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
