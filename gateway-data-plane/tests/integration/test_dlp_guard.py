"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/integration/test_dlp_guard.py
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_gateway.api.openai_compatible import router as openai_compatible_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试密钥只用于数据面 DLP 守卫夹具，断言命中文本和密钥不会落日志。
VIRTUAL_KEY = "mhv_prd024_dlp_key"
PROVIDER_KEY = "sk-prd024-provider-secret"


def sqlite_url(path: Path) -> str:
    """生成 SQLite 测试数据库 URL，确保每个 DLP 集成用例使用独立文件。"""

    return f"sqlite:///{path.as_posix()}"


def build_gateway_app():
    """创建只包含 OpenAI-compatible 路由的数据面测试应用。"""

    app = create_service_app("gateway-data-plane", "Gateway PRD-024 DLP Test API")
    app.include_router(openai_compatible_router)
    return app


@pytest.fixture()
def fake_upstream():
    """启动可观测的假上游，用于确认 DLP 阻断是否发生在 Provider 调用前。"""

    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """处理测试上游请求，并记录是否真的穿透到 Provider。"""

        def do_POST(self) -> None:  # noqa: N802
            """返回最小 Chat Completions 响应，供网关完成正常用量入账。"""

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            captured.append({"path": self.path, "body": body})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "chatcmpl_prd024",
                        "object": "chat.completion",
                        "model": body.get("model", "local-chat-model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args: object) -> None:
            """关闭 http.server 默认日志，避免测试输出混入请求正文。"""

            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", captured
    finally:
        server.shutdown()
        thread.join(timeout=2)


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_base_url: str) -> tuple[str, str]:
    """准备包含种子 Virtual Key、模型路由和 DLP 表结构的数据面测试数据库。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    # 返回数据库 URL 和租户 ID，后续断言会验证 DLP 规则、命中记录和调用日志都被租户隔离。
    return database_url, seed.tenant_id


def insert_dlp_rule(
    database_url: str,
    *,
    tenant_id: str,
    rule_id: str,
    name: str,
    rule_type: str,
    pattern: str,
    action: str,
    scan_scope: str = "input",
    priority: int = 100,
) -> str:
    """直接写入带检测方向的 DLP 规则，让集成测试聚焦数据面准入行为。"""

    pattern_hash = f"sha256:{hashlib.sha256(f'{rule_type}:{pattern}'.encode('utf-8')).hexdigest()}"
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id, tenant_id, name, rule_type, pattern, pattern_hash,
          action, scan_scope, status, priority, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            rule_id,
            tenant_id,
            name,
            rule_type,
            pattern,
            pattern_hash,
            action,
            scan_scope,
            priority,
            "2026-07-10T08:00:00Z",
            "2026-07-10T08:00:00Z",
        ),
    )
    connection.commit()
    return rule_id


@pytest.mark.anyio
async def test_dlp_block_stops_before_upstream_and_logs_hash_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """阻断流程：命中 block 规则时返回统一错误，不调用上游，并写入脱敏 DLP 命中摘要。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    rule_id = insert_dlp_rule(
        database_url,
        tenant_id=tenant_id,
        rule_id="dlp_prd024_block",
        name="阻断规则A",
        rule_type="keyword",
        pattern="敏感词",
        action="block",
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(client, "req_prd024_block", [{"role": "user", "content": "这里包含敏感词"}])

    assert response.status_code == 403
    assert response.json()["code"] == "gateway.dlp_blocked"
    assert captured == []
    connection = connect_database(database_url)
    request_log = connection.execute(
        """
        SELECT status, error_code, dlp_status, dlp_rule_id, dlp_action, dlp_match_count, dlp_reason_code
        FROM request_logs
        WHERE request_id='req_prd024_block'
        """
    ).fetchone()
    match = connection.execute(
        """
        SELECT rule_name, rule_type, action, message_index, match_text_hash, match_length
        FROM dlp_matches
        WHERE tenant_id=? AND request_id='req_prd024_block'
        """,
        (tenant_id,),
    ).fetchone()
    dump = "\n".join(
        str(value)
        for table in ["request_logs", "dlp_matches"]
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert request_log["status"] == "failed"
    assert request_log["error_code"] == "gateway.dlp_blocked"
    assert request_log["dlp_status"] == "blocked"
    assert request_log["dlp_rule_id"] == rule_id
    assert request_log["dlp_action"] == "block"
    assert request_log["dlp_match_count"] == 1
    assert request_log["dlp_reason_code"] == "dlp.keyword_matched"
    assert match["rule_name"] == "阻断规则A"
    assert match["rule_type"] == "keyword"
    assert match["action"] == "block"
    assert match["message_index"] == 0
    assert match["match_length"] == 3
    assert match["match_text_hash"].startswith("sha256:")
    assert "敏感词" not in dump
    assert "这里包含" not in dump


@pytest.mark.anyio
async def test_dlp_log_only_allows_upstream_and_logs_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """试运行流程：命中 log_only 规则时继续调用上游，并在调用日志中记录 DLP 命中摘要。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    insert_dlp_rule(
        database_url,
        tenant_id=tenant_id,
        rule_id="dlp_prd024_log_only",
        name="试运行正则",
        rule_type="regex",
        pattern="违规[0-9]+",
        action="log_only",
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(client, "req_prd024_log_only", [{"role": "user", "content": "请检查违规123"}])

    assert response.status_code != 403, response.text
    assert response.json().get("code") != "gateway.dlp_blocked"
    connection = connect_database(database_url)
    request_log = connection.execute(
        "SELECT status, error_code, dlp_status, dlp_action, dlp_match_count FROM request_logs WHERE request_id='req_prd024_log_only'"
    ).fetchone()
    match_count = connection.execute(
        "SELECT COUNT(*) AS count FROM dlp_matches WHERE tenant_id=? AND request_id='req_prd024_log_only'",
        (tenant_id,),
    ).fetchone()["count"]
    assert request_log["status"] in {"success", "failed"}
    assert request_log["error_code"] != "gateway.dlp_blocked"
    assert request_log["dlp_status"] == "matched"
    assert request_log["dlp_action"] == "log_only"
    assert request_log["dlp_match_count"] == 1
    assert match_count == 1


@pytest.mark.anyio
async def test_dlp_scans_only_user_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """范围边界：M0 只扫描 role=user 的 messages 文本，system 中的相同关键词不能触发 DLP。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    insert_dlp_rule(
        database_url,
        tenant_id=tenant_id,
        rule_id="dlp_prd024_user_only",
        name="只扫用户消息",
        rule_type="keyword",
        pattern="系统敏感词",
        action="block",
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(
            client,
            "req_prd024_system_only",
            [
                {"role": "system", "content": "系统敏感词"},
                {"role": "user", "content": "普通问题"},
            ],
        )

    assert response.status_code != 403, response.text
    assert response.json().get("code") != "gateway.dlp_blocked"
    connection = connect_database(database_url)
    request_log = connection.execute(
        "SELECT status, error_code, dlp_status, dlp_match_count FROM request_logs WHERE request_id='req_prd024_system_only'"
    ).fetchone()
    match_count = connection.execute(
        "SELECT COUNT(*) AS count FROM dlp_matches WHERE tenant_id=? AND request_id='req_prd024_system_only'",
        (tenant_id,),
    ).fetchone()["count"]
    assert request_log["status"] in {"success", "failed"}
    assert request_log["error_code"] != "gateway.dlp_blocked"
    assert request_log["dlp_status"] == "clean"
    assert request_log["dlp_match_count"] == 0
    assert match_count == 0


@pytest.mark.anyio
async def test_dlp_input_guard_ignores_output_scope_and_applies_both_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """方向流程：output-only 不扫描输入，both 仍须在调用上游前阻断命中的用户消息。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    insert_dlp_rule(
        database_url,
        tenant_id=tenant_id,
        rule_id="dlp_prd071_output_only",
        name="仅扫描输出",
        rule_type="keyword",
        pattern="输入秘密",
        action="block",
        scan_scope="output",
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        output_only_response = await _chat(
            client,
            "req_prd071_output_only",
            [{"role": "user", "content": "这里包含输入秘密"}],
        )
        insert_dlp_rule(
            database_url,
            tenant_id=tenant_id,
            rule_id="dlp_prd071_both",
            name="双向扫描",
            rule_type="keyword",
            pattern="双向秘密",
            action="block",
            scan_scope="both",
            priority=10,
        )
        both_response = await _chat(
            client,
            "req_prd071_both",
            [{"role": "user", "content": "这里包含双向秘密"}],
        )

    assert output_only_response.status_code == 200, output_only_response.text
    assert both_response.status_code == 403
    assert both_response.json()["code"] == "gateway.dlp_blocked"
    # 第一条请求已调用一次上游；第二条被 both 规则提前阻断，不能增加上游调用次数。
    assert len(captured) == 1
    connection = connect_database(database_url)
    output_only_matches = connection.execute(
        "SELECT COUNT(*) AS count FROM dlp_matches WHERE tenant_id=? AND request_id=?",
        (tenant_id, "req_prd071_output_only"),
    ).fetchone()["count"]
    assert output_only_matches == 0


async def _chat(client: AsyncClient, request_id: str, messages: list[dict[str, str]]):
    """发送一条固定 Chat Completions 请求，便于断言 DLP request_id 追踪链路。"""

    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": request_id},
        json={"model": "default-chat", "messages": messages},
    )
