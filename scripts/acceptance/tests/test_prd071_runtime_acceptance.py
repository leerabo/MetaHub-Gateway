"""PRD-071 真实 HTTP 验收器的纯沙箱合同测试。

测试只使用 ``D:/MetaHub`` 内 pytest 临时库与随机回环端口，不连接或启动
``8000/8001/5173``，也不读取开发数据库和服务日志。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest


def _load_runtime_module() -> ModuleType:
    """从带连字符的正式路径加载脚本，且不会执行 ``main``。"""

    path = Path(__file__).resolve().parents[1] / "prd071-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location("prd071_runtime_acceptance", path)
    if specification is None or specification.loader is None:
        # 正式脚本无法加载时不能用测试替身掩盖交付物缺失，必须立即失败。
        raise AssertionError("unable to load PRD-071 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 单测只加载正式脚本一次，保证 FakeProvider 与异常类在所有用例中保持同一类型身份。
RUNTIME = _load_runtime_module()


def _provider_json(port: int, prompt: str, *, authorization: str) -> httpx.Response:
    """向单测随机端口发送最小非流式 Provider 请求。"""

    return httpx.post(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        headers={"Authorization": authorization},
        json={
            "model": "local-chat-model",
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=5.0,
        trust_env=False,
    )


def test_path_and_loopback_configuration_fail_closed() -> None:
    """路径只能落在工作区边界内，URL 只能使用固定端口的无凭据回环 HTTP。"""

    accepted = RUNTIME._workspace_path(
        RUNTIME.ROOT / ".tmp" / "prd071-unit" / "gateway.log",
        RUNTIME.ROOT / ".tmp",
        stage="unit.path",
    )
    assert accepted.is_absolute()
    with pytest.raises(RUNTIME.AcceptanceFailure) as outside:
        RUNTIME._workspace_path(
            RUNTIME.ROOT.parent / "outside.log",
            RUNTIME.ROOT / ".tmp",
            stage="unit.path",
        )
    assert outside.value.code == "path_outside_workspace"

    assert (
        RUNTIME._loopback_base_url("http://127.0.0.1:8000", expected_port=8000, stage="unit.url")
        == "http://127.0.0.1:8000"
    )
    for value in (
        "https://127.0.0.1:8000",
        "http://example.com:8000",
        "http://user:password@127.0.0.1:8000",
        "http://127.0.0.1:8001",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:not-a-port",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME._loopback_base_url(value, expected_port=8000, stage="unit.url")


def test_fake_provider_non_stream_and_chunked_stream_keep_only_counts() -> None:
    """假上游返回三类正文和跨块 SSE，但 observation 不保存 Prompt、Key 或响应。"""

    marker = "prd071-unit-marker-" + "a" * 16
    provider_key = "sk-prd071-unit-provider-key"
    provider = RUNTIME.FakeProvider(marker, provider_key, port=0)
    provider.start()
    port = int(provider.base_url.rsplit(":", 1)[1].split("/", 1)[0])
    clean_prompt = RUNTIME.SCENARIO_CLEAN + "unit"
    malformed_prompt = RUNTIME.SCENARIO_MALFORMED + "unit"
    stream_prompt = RUNTIME.SCENARIO_STREAM_BLOCKED + "unit"
    try:
        clean = _provider_json(
            port,
            clean_prompt,
            authorization=f"Bearer {provider_key}",
        )
        malformed = _provider_json(
            port,
            malformed_prompt,
            authorization=f"Bearer {provider_key}",
        )
        denied = _provider_json(port, clean_prompt, authorization="Bearer wrong-unit-key")
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            with client.stream(
                "POST",
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Authorization": f"Bearer {provider_key}"},
                json={
                    "model": "local-chat-model",
                    "messages": [{"role": "user", "content": stream_prompt}],
                    "stream": True,
                },
            ) as streamed:
                wire = b"".join(streamed.iter_bytes())
        observation = provider.observation()
    finally:
        provider.stop()

    assert clean.status_code == 200
    assert clean.json()["choices"][0]["message"]["content"] == RUNTIME.CLEAN_RESPONSE_TEXT
    assert malformed.status_code == 200
    assert isinstance(malformed.json()["choices"][0]["message"]["content"], dict)
    assert denied.status_code == 401
    assert marker.encode() in wire
    assert wire.endswith(b"data: [DONE]\n\n")
    assert observation.as_evidence() == {
        "calls": 3,
        "invalid_requests": 0,
        "invalid_authorization": 1,
        "clean_calls": 1,
        "blocked_calls": 0,
        "malformed_calls": 1,
        "stream_blocked_calls": 1,
        "cross_chunk_splits": 1,
    }
    serialized_observation = repr(vars(observation))
    for forbidden in (marker, clean_prompt, malformed_prompt, stream_prompt, provider_key):
        assert forbidden not in serialized_observation


def test_stream_chunks_split_inside_marker_and_safe_wire_requires_one_error() -> None:
    """chunk 生成器必须在 marker 内切分；下游合同只接受一条标准 error。"""

    marker = "prd071-unit-cross-chunk-marker"
    chunks = RUNTIME._stream_provider_chunks(marker)
    marker_bytes = marker.encode()
    assert marker_bytes not in chunks[1]
    assert marker_bytes not in chunks[2]
    assert marker_bytes in chunks[1] + chunks[2]
    blocked_payload = json.loads((chunks[1] + chunks[2])[6:].strip())
    assert blocked_payload["choices"][0]["delta"]["content"].endswith("z" * 32)
    assert chunks[0].startswith(b"data: ")
    assert chunks[-1] == b"data: [DONE]\n\n"

    request_id = "req_prd071_unit_stream"
    trace_id = "trc_prd071_unit_stream"
    safe = RUNTIME._sse_event("safe unit prefix")
    error = (
        b"event: error\n"
        + b"data: "
        + json.dumps(
            {
                "error": {
                    "code": "content_policy_violation",
                    "request_id": request_id,
                    "trace_id": trace_id,
                }
            },
            separators=(",", ":"),
        ).encode()
        + b"\n\n"
    )
    assert RUNTIME._validate_stream_wire(safe + error, request_id, trace_id, marker) == {
        "code": "content_policy_violation",
        "body_event_count": 1,
        "error_event_count": 1,
        "done_count": 0,
    }
    with pytest.raises(RUNTIME.AcceptanceFailure) as duplicate:
        RUNTIME._validate_stream_wire(safe + error + error, request_id, trace_id, marker)
    assert duplicate.value.code == "error_event_count_invalid"
    with pytest.raises(RUNTIME.AcceptanceFailure) as exposed:
        RUNTIME._validate_stream_wire(safe + marker_bytes + error, request_id, trace_id, marker)
    assert exposed.value.code == "marker_exposed"


def _create_fact_schema(connection: sqlite3.Connection) -> None:
    """创建 owner 汇总器所需的最小 SQLite 表，不复制生产触发器。"""

    connection.executescript(
        """
        CREATE TABLE request_logs(
          tenant_id TEXT,request_id TEXT,trace_id TEXT,status TEXT,error_code TEXT,
          stream_session_id TEXT,stream_status TEXT
        );
        CREATE TABLE gateway_request_claims(tenant_id TEXT,request_id TEXT,trace_id TEXT);
        CREATE TABLE output_safety_scans(
          tenant_id TEXT,request_id TEXT,trace_id TEXT,output_safety_scan_id TEXT,
          stream_session_id TEXT,scan_mode TEXT,scan_status TEXT,decision TEXT,
          exposure_state TEXT,billing_disposition TEXT,failure_reason_code TEXT
        );
        CREATE TABLE stream_sessions(
          tenant_id TEXT,request_id TEXT,trace_id TEXT,stream_session_id TEXT,
          status TEXT,error_code TEXT
        );
        CREATE TABLE usage_ledger(tenant_id TEXT,request_id TEXT,usage_ledger_id TEXT);
        CREATE TABLE finops_ledger_entries(
          tenant_id TEXT,source_request_id TEXT,source_trace_id TEXT,usage_ledger_id TEXT,
          finops_ledger_entry_id TEXT,entry_type TEXT
        );
        CREATE TABLE finops_ledger_disputes(
          tenant_id TEXT,request_id TEXT,trace_id TEXT,output_safety_scan_id TEXT,
          usage_ledger_id TEXT,finops_ledger_entry_id TEXT,review_status TEXT,
          reversal_eligible INTEGER
        );
        CREATE TABLE output_safety_events(
          tenant_id TEXT,request_id TEXT,trace_id TEXT,output_safety_scan_id TEXT,
          event_id TEXT,event_type TEXT
        );
        """
    )


def _insert_clean_fact(connection: sqlite3.Connection) -> tuple[str, str, str]:
    """插入一套 tenant/request/trace 完全一致的 clean 非流式事实。"""

    tenant_id = "tenant-prd071-unit"
    request_id = "req_prd071_unit_fact"
    trace_id = "trc_prd071_unit_fact"
    connection.execute(
        "INSERT INTO request_logs VALUES(?,?,?,'success',NULL,NULL,NULL)",
        (tenant_id, request_id, trace_id),
    )
    connection.execute(
        "INSERT INTO gateway_request_claims VALUES(?,?,?)",
        (tenant_id, request_id, trace_id),
    )
    connection.execute(
        "INSERT INTO output_safety_scans VALUES(?,?,?,'oss_unit',NULL,'non_stream_full',"
        "'completed','clean','complete','not_required',NULL)",
        (tenant_id, request_id, trace_id),
    )
    connection.execute(
        "INSERT INTO usage_ledger VALUES(?,?,?)",
        (tenant_id, request_id, "usg_unit"),
    )
    connection.execute(
        "INSERT INTO finops_ledger_entries VALUES(?,?,?,?,?,'usage_charge')",
        (tenant_id, request_id, trace_id, "usg_unit", "fle_unit"),
    )
    connection.commit()
    # 返回的都只是合成 owner ID，正文、Key 和 Token 从未写入这个最小测试库。
    return tenant_id, request_id, trace_id


def test_database_fact_summary_accepts_exact_owner_and_rejects_trace_pollution(
    tmp_path: Path,
) -> None:
    """owner 汇总只接受逐层同 tenant/request/trace，任何污染都固定失败关闭。"""

    database = tmp_path / "facts.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    _create_fact_schema(connection)
    tenant_id, request_id, _trace_id = _insert_clean_fact(connection)
    fact = RUNTIME._load_request_fact(connection, tenant_id, request_id)
    RUNTIME._validate_request_fact(
        fact,
        RUNTIME.FactExpectation(
            "success",
            None,
            "non_stream_full",
            "completed",
            "clean",
            "complete",
            "not_required",
            0,
            0,
        ),
    )
    assert fact["usage"]["usage_ledger_id"] == "usg_unit"
    assert fact["charge"]["finops_ledger_entry_id"] == "fle_unit"

    connection.execute(
        "UPDATE output_safety_scans SET trace_id='trc_polluted' WHERE request_id=?",
        (request_id,),
    )
    connection.commit()
    with pytest.raises(RUNTIME.AcceptanceFailure) as polluted:
        RUNTIME._load_request_fact(connection, tenant_id, request_id)
    connection.close()
    assert polluted.value.code == "trace_owner_mismatch"


def test_evidence_validator_rejects_sensitive_fields_and_values() -> None:
    """证据只允许安全元数据，字段名或任意深度明文命中都必须拒绝。"""

    sensitive = ["secret-unit-value"]
    encoded = RUNTIME._validate_evidence(
        {
            "status": "passed",
            "request": {"request_id": "req_unit", "status_code": 200},
        },
        sensitive,
    )
    assert b"req_unit" in encoded
    with pytest.raises(RUNTIME.AcceptanceFailure) as field_error:
        RUNTIME._validate_evidence({"prompt": "redacted"}, sensitive)
    assert field_error.value.code == "forbidden_key"
    with pytest.raises(RUNTIME.AcceptanceFailure) as value_error:
        RUNTIME._validate_evidence(
            {"nested": [{"safe": "prefix secret-unit-value suffix"}]}, sensitive
        )
    assert value_error.value.code == "sensitive_match"


def test_provider_key_setup_and_cleanup_use_control_api_without_plaintext_echo() -> None:
    """一次性 Provider Key 必须经后端创建/禁用，且控制面响应不能回显明文。"""

    observed: list[tuple[str, str, dict[str, object]]] = []
    provider_key = "sk-prd071-unit-control-key"

    def handler(request: httpx.Request) -> httpx.Response:
        """返回带原 request_id/trace_id 的最小控制面响应，并记录安全测试输入。"""

        body = json.loads(request.content)
        observed.append((request.method, request.url.path, body))
        request_id = request.headers["x-request-id"]
        trace_id = request.headers["x-trace-id"]
        status = "active" if request.method == "POST" else "disabled"
        return httpx.Response(
            201 if request.method == "POST" else 200,
            headers={"x-request-id": request_id, "x-trace-id": trace_id},
            json={
                "provider_key": {
                    "provider_key_id": "pcred_prd071_unit",
                    "provider_id": "provider_prd071_unit",
                    "status": status,
                },
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )

    with httpx.Client(
        base_url=RUNTIME.CONTROL_URL,
        transport=httpx.MockTransport(handler),
    ) as control:
        key_id = RUNTIME._create_provider_key(
            control,
            "unit-admin-token",
            "prd071_unit",
            "provider_prd071_unit",
            provider_key,
        )
        assert RUNTIME._disable_provider_key(
            control,
            "unit-admin-token",
            "prd071_unit",
            key_id,
        )

    assert observed == [
        (
            "POST",
            "/providers/provider_prd071_unit/keys",
            {
                "name": "PRD071 Runtime prd071_unit",
                "plain_value": provider_key,
                "weight": 10_000,
            },
        ),
        (
            "PATCH",
            "/provider-keys/pcred_prd071_unit",
            {"status": "disabled"},
        ),
    ]

    def echo_handler(request: httpx.Request) -> httpx.Response:
        """模拟错误回显明文的控制面，验收器必须在解析证据前失败关闭。"""

        return httpx.Response(
            201,
            headers={
                "x-request-id": request.headers["x-request-id"],
                "x-trace-id": request.headers["x-trace-id"],
            },
            json={"plain_value": provider_key},
        )

    with httpx.Client(
        base_url=RUNTIME.CONTROL_URL,
        transport=httpx.MockTransport(echo_handler),
    ) as control:
        with pytest.raises(RUNTIME.AcceptanceFailure) as exposed:
            RUNTIME._create_provider_key(
                control,
                "unit-admin-token",
                "prd071_unit_echo",
                "provider_prd071_unit",
                provider_key,
            )
    assert exposed.value.code == "plain_value_exposed"


def test_source_stays_in_workspace_and_does_not_start_business_services() -> None:
    """源码门禁禁止沙箱外临时目录、提权和业务服务进程编排。"""

    path = Path(RUNTIME.__file__).resolve()
    source = path.read_text(encoding="utf-8")
    assert "C:" + "\\tmp" not in source
    assert "require_" + "escalated" not in source
    assert "subprocess" not in source
    assert "Start-Process" not in source
    assert 'ROOT / ".tmp" / "prd071-live"' in source
    assert 'ROOT / ".artifacts" / "prd071-runtime"' in source
