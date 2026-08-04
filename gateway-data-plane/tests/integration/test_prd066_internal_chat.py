"""PRD-066 Gateway 内部 Chat 身份、grant、治理复用和隐私集成测试。"""

from __future__ import annotations

import hashlib
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

import metahub_gateway.api.internal_chat as internal_chat
from metahub_gateway.api.internal_chat import router as internal_chat_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 内部身份、单次 grant、正文金丝雀和链路标识只服务隔离测试，不写入正式验收数据库。
INTERNAL_TOKEN = "prd066-internal-chat-token-2026-abcdef"
GRANT = "A" * 64
PROMPT_CANARY = "PRD066_GATEWAY_PROMPT_MEMORY_ONLY"
REQUEST_ID = "req_prd066_gateway"
TRACE_ID = "trc_prd066_gateway"


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _time(value: datetime) -> str:
    """把测试时间格式化为 0058 trigger 使用的 canonical UTC 秒。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _prepare_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str]:
    """创建一套合法单租户资源、in-flight Message 和 30 秒单次 grant。

    返回数据库 URL 和 tenant ID。测试数据库不保存 grant 明文或 Prompt；grant 只以 SHA-256
    写入，与生产入口保持同一安全边界。
    """

    database_url = _sqlite_url(tmp_path / "prd066-gateway.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_INTERNAL_CHAT_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "PRD066GatewayAdmin!2026")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    # Admin 默认矩阵不可变，不能用于撤权验收；创建可合法调整矩阵的 Viewer 作为 grant owner。
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(
          'user_prd066_gateway_viewer',?,?,?,?,'viewer','active',datetime('now'),datetime('now')
        )
        """,
        (
            seed.tenant_id,
            "viewer-prd066-gateway@example.com",
            "PRD066 Gateway Viewer",
            hash_password("PRD066GatewayViewer!2026"),
        ),
    )
    user = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='viewer' AND status='active'",
        (seed.tenant_id,),
    ).fetchone()
    environment = connection.execute(
        """
        SELECT environment_id,code FROM environments
        WHERE tenant_id=? AND status='active' ORDER BY environment_id LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND status='active' LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND status='active' LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    assert all(item is not None for item in (user, environment, model, virtual_key))
    now = datetime.now(UTC).replace(microsecond=0)
    created_at = _time(now - timedelta(seconds=1))
    updated_at = _time(now)
    session_expires_at = _time(now + timedelta(hours=8) - timedelta(seconds=1))
    grant_expires_at = _time(now + timedelta(seconds=30))
    graph = {
        "user_id": str(user["user_id"]),
        "environment_id": str(environment["environment_id"]),
        "environment_code": str(environment["code"]),
        "model_id": str(model["model_id"]),
        "virtual_key_id": str(virtual_key["virtual_key_id"]),
        "project_id": "prj_prd066_gateway",
        "model_alias_id": "mal_prd066_gateway",
        "model_alias": "prd066-gateway-chat",
        "auth_session_id": "session_prd066_gateway",
        "chat_session_id": "chs_" + "1" * 32,
        "chat_message_id": "chm_" + "2" * 32,
        "grant_id": "chg_" + "3" * 32,
    }
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'PRD-066 Gateway','',?,'active',?,?,?,?)
        """,
        (
            graph["project_id"],
            seed.tenant_id,
            "prd066-gateway",
            graph["user_id"],
            graph["user_id"],
            graph["user_id"],
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
          fallback_model_id,status,description,created_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,NULL,'active','PRD-066 Gateway Alias',?,?,?)
        """,
        (
            graph["model_alias_id"],
            seed.tenant_id,
            graph["model_alias"],
            graph["environment_code"],
            graph["environment_id"],
            graph["model_id"],
            graph["user_id"],
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        UPDATE virtual_keys
        SET project_id=?,environment_id=?,updated_at=?
        WHERE tenant_id=? AND virtual_key_id=?
        """,
        (
            graph["project_id"],
            graph["environment_id"],
            updated_at,
            seed.tenant_id,
            graph["virtual_key_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO sessions(
          session_id,tenant_id,user_id,token_jti,token_hash,status,expires_at,
          revoked_at,last_seen_at,user_agent,created_ip,created_at,updated_at
        ) VALUES(?,?,?,'jti_prd066_gateway',?,'active',?,NULL,NULL,
                 'prd066-test','127.0.0.1',?,?)
        """,
        (
            graph["auth_session_id"],
            seed.tenant_id,
            graph["user_id"],
            "b" * 64,
            session_expires_at,
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO chat_sessions(
          chat_session_id,tenant_id,owner_user_id,owner_auth_session_id,
          project_id,environment_id,model_alias_id,model_alias,virtual_key_id,
          status,message_sequence,state_revision,expires_at,cleared_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'active',0,1,?,NULL,?,?)
        """,
        (
            graph["chat_session_id"],
            seed.tenant_id,
            graph["user_id"],
            graph["auth_session_id"],
            graph["project_id"],
            graph["environment_id"],
            graph["model_alias_id"],
            graph["model_alias"],
            graph["virtual_key_id"],
            session_expires_at,
            created_at,
            created_at,
        ),
    )
    # Message INSERT trigger 要求父 Session 已占用相同 sequence；两步仍在同一事务中。
    connection.execute(
        """
        UPDATE chat_sessions
        SET message_sequence=1,state_revision=2,updated_at=?
        WHERE tenant_id=? AND chat_session_id=?
        """,
        (updated_at, seed.tenant_id, graph["chat_session_id"]),
    )
    connection.execute(
        """
        INSERT INTO chat_messages(
          chat_message_id,tenant_id,chat_session_id,owner_user_id,client_message_id,
          message_sequence,gateway_request_id,gateway_trace_id,status,webhook_event_id,started_at
        ) VALUES(?,?,?,?,?,1,?,?,'in_flight',?,?)
        """,
        (
            graph["chat_message_id"],
            seed.tenant_id,
            graph["chat_session_id"],
            graph["user_id"],
            "11111111-1111-4111-8111-111111111111",
            REQUEST_ID,
            TRACE_ID,
            f"evt_{graph['chat_message_id']}",
            updated_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO chat_execution_grants(
          chat_execution_grant_id,tenant_id,grant_hash,chat_session_id,chat_message_id,
          owner_user_id,owner_auth_session_id,project_id,environment_id,model_alias_id,
          virtual_key_id,gateway_request_id,gateway_trace_id,status,expires_at,issued_at,
          consumed_at,revoked_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'issued',?,?,NULL,NULL)
        """,
        (
            graph["grant_id"],
            seed.tenant_id,
            hashlib.sha256(GRANT.encode("ascii")).hexdigest(),
            graph["chat_session_id"],
            graph["chat_message_id"],
            graph["user_id"],
            graph["auth_session_id"],
            graph["project_id"],
            graph["environment_id"],
            graph["model_alias_id"],
            graph["virtual_key_id"],
            REQUEST_ID,
            TRACE_ID,
            grant_expires_at,
            updated_at,
        ),
    )
    connection.commit()
    connection.close()
    # 只返回数据库 URL 与可信 tenant，grant 明文和 Prompt 继续停留在当前测试调用栈。
    return database_url, seed.tenant_id


def _build_app():
    """构造只包含内部 Chat 路由和共享中间件的隔离 Gateway 应用。"""

    app = create_service_app("gateway-data-plane", "PRD-066 Internal Chat Test API")
    app.include_router(internal_chat_router)
    return app


def _body() -> dict[str, object]:
    """返回只在当前请求内存中使用的 exact-set grant/history。"""

    return {
        "grant": GRANT,
        "history": [{"role": "user", "content": PROMPT_CANARY}],
    }


def _headers(token: str = INTERNAL_TOKEN) -> dict[str, str]:
    """返回内部 Token 和与 grant 冻结值一致的链路 Header。"""

    return {
        "X-MetaHub-Internal-Chat-Token": token,
        "X-Request-ID": REQUEST_ID,
        "X-Trace-ID": TRACE_ID,
    }


def _revoke_chat_permission(database_url: str, tenant_id: str) -> None:
    """合法撤销 Viewer 的 chat.use 并同步 revision/hash，供实时权限消费测试使用。"""

    connection = connect_database(database_url)
    role = connection.execute(
        "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='viewer'",
        (tenant_id,),
    ).fetchone()
    permission = connection.execute(
        "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='chat.use'",
        (tenant_id,),
    ).fetchone()
    assert role is not None and permission is not None
    connection.execute(
        "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
        (tenant_id, role["role_id"], permission["permission_id"]),
    )
    remaining = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
            """,
            (tenant_id, role["role_id"]),
        ).fetchall()
    }
    connection.execute(
        """
        UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=?
        """,
        (
            int(role["permission_revision"]) + 1,
            permission_matrix_sha256(remaining),
            _time(datetime.now(UTC) + timedelta(seconds=1)),
            tenant_id,
            role["role_id"],
        ),
    )
    connection.commit()
    connection.close()


def _invalidate_execution_resource(
    database_url: str,
    tenant_id: str,
    resource_kind: str,
) -> None:
    """在 grant 签发后让一项可信资源失效，模拟调用前的实时状态漂移。

    参数：隔离数据库、可信租户和测试定义的资源类别。返回值：无。关键约束：所有更新
    都从 tenant-qualified grant 图反查 ID，不能用另一租户同名资源制造假阳性。
    """

    connection = connect_database(database_url)
    now = _time(datetime.now(UTC) + timedelta(seconds=1))
    if resource_kind == "project":
        # 项目停用后旧 grant 不能继续借用此前通过的 Control 校验结果。
        connection.execute(
            """
            UPDATE projects SET status='disabled',updated_at=?
            WHERE tenant_id=? AND project_id=(
              SELECT project_id FROM chat_execution_grants WHERE tenant_id=?
            )
            """,
            (now, tenant_id, tenant_id),
        )
    elif resource_kind == "environment":
        # 环境是项目 Key 和别名共同边界，停用必须在消费 grant 前失败关闭。
        connection.execute(
            """
            UPDATE environments SET status='disabled',updated_at=?
            WHERE tenant_id=? AND environment_id=(
              SELECT environment_id FROM chat_execution_grants WHERE tenant_id=?
            )
            """,
            (now, tenant_id, tenant_id),
        )
    elif resource_kind == "alias":
        # 别名状态可能在 options/create 之后漂移，Gateway 仍需实时复核。
        connection.execute(
            """
            UPDATE model_aliases SET status='disabled',disabled_at=?,updated_at=?
            WHERE tenant_id=? AND model_alias_id=(
              SELECT model_alias_id FROM chat_execution_grants WHERE tenant_id=?
            )
            """,
            (now, now, tenant_id, tenant_id),
        )
    elif resource_kind == "virtual_key":
        # Key 停用必须阻断 Provider，不能因为内部入口不接收 Bearer 就跳过 Key 生命周期。
        connection.execute(
            """
            UPDATE virtual_keys SET status='disabled',disabled_at=?,updated_at=?
            WHERE tenant_id=? AND virtual_key_id=(
              SELECT virtual_key_id FROM chat_execution_grants WHERE tenant_id=?
            )
            """,
            (now, now, tenant_id, tenant_id),
        )
    elif resource_kind == "second_active_key":
        # 会话只在恰好一把 Project Key 时确定；后续新增第二把必须使旧 grant 失效。
        binding = connection.execute(
            """
            SELECT grant.project_id,grant.environment_id,key.environment,key.created_by
            FROM chat_execution_grants grant
            JOIN virtual_keys key
              ON key.tenant_id=grant.tenant_id AND key.virtual_key_id=grant.virtual_key_id
            WHERE grant.tenant_id=?
            """,
            (tenant_id,),
        ).fetchone()
        assert binding is not None
        connection.execute(
            """
            INSERT INTO virtual_keys(
              virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
              environment,description,last_used_at,created_by,disabled_at,disabled_by,
              project_id,application_id,environment_id
            ) VALUES(
              'vk_prd066_second_active',?,'PRD066 Second Active','mhv_prd066_second',?,
              'active',?,?,?,'PRD-066 uniqueness drift',NULL,?,NULL,NULL,?,NULL,?
            )
            """,
            (
                tenant_id,
                "d" * 64,
                now,
                now,
                binding["environment"],
                binding["created_by"],
                binding["project_id"],
                binding["environment_id"],
            ),
        )
    else:
        # 参数集合由源码冻结；未知类别表示测试本身有缺陷，不能静默跳过失效动作。
        raise AssertionError("unknown PRD-066 resource kind")
    assert connection.execute("SELECT changes()").fetchone()[0] == 1
    connection.commit()
    connection.close()


@pytest.mark.anyio
async def test_internal_chat_auth_binding_single_consume_and_endpoint_split(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主/异常：身份优先、链路绑定、单次消费和共用治理参数均符合冻结合同。"""

    database_url, tenant_id = _prepare_execution(monkeypatch, tmp_path)
    captured: list[dict[str, Any]] = []

    async def governed_response(request, **kwargs):
        """捕获可信治理输入并返回不含 Provider 内部字段的最小成功响应。"""

        captured.append(kwargs)
        return JSONResponse(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    monkeypatch.setattr(internal_chat, "_handle_gateway_request", governed_response)
    app = _build_app()
    current_db_now = internal_chat._db_now
    duplicate_body = (
        "{"
        f'"grant":"{GRANT}",'
        f'"grant":"{GRANT}",'
        f'"history":[{{"role":"user","content":"{PROMPT_CANARY}"}}]'
        "}"
    ).encode("utf-8")
    # 只超过 wire 上限一个字节，证明拒绝发生在 JSON 解析和 grant 消费之前。
    oversized_body = b"{" + (b"x" * internal_chat._MAX_WIRE_BODY_BYTES)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        missing = await client.post(
            "/internal/chat/completions",
            json=_body(),
            headers={"X-Request-ID": REQUEST_ID, "X-Trace-ID": TRACE_ID},
        )
        missing_oversized = await client.post(
            "/internal/chat/completions",
            content=oversized_body,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": REQUEST_ID,
                "X-Trace-ID": TRACE_ID,
            },
        )
        wrong = await client.post(
            "/internal/chat/completions", json=_body(), headers=_headers("x" * 40)
        )
        bearer = await client.post(
            "/internal/chat/completions",
            json=_body(),
            headers={**_headers(), "Authorization": "Bearer forged-virtual-key"},
        )
        cookie = await client.post(
            "/internal/chat/completions",
            json=_body(),
            headers={**_headers(), "Cookie": "session=forged"},
        )
        forged_tenant = await client.post(
            "/internal/chat/completions",
            json={**_body(), "tenant_id": "tenant_forged"},
            headers=_headers(),
        )
        duplicate = await client.post(
            "/internal/chat/completions",
            content=duplicate_body,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        oversized = await client.post(
            "/internal/chat/completions",
            content=oversized_body,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        mismatch = await client.post(
            "/internal/chat/completions",
            json=_body(),
            headers={**_headers(), "X-Request-ID": "req_prd066_wrong"},
        )
        # 用同一夹具把 Gateway 时钟推进到 grant 窗口外；拒绝后恢复真实时钟，证明过期判定
        # 不会错误消费 issued grant，也无需为单个时间分支再执行一轮 0058 全迁移。
        monkeypatch.setattr(
            internal_chat,
            "_db_now",
            lambda: _time(datetime.now(UTC) + timedelta(minutes=1)),
        )
        expired = await client.post(
            "/internal/chat/completions", json=_body(), headers=_headers()
        )
        monkeypatch.setattr(internal_chat, "_db_now", current_db_now)
        success = await client.post(
            "/internal/chat/completions", json=_body(), headers=_headers()
        )
        replay = await client.post(
            "/internal/chat/completions", json=_body(), headers=_headers()
        )

    assert (missing.status_code, missing.json()["code"]) == (
        401,
        "auth.internal_token_required",
    )
    assert (missing_oversized.status_code, missing_oversized.json()["code"]) == (
        401,
        "auth.internal_token_required",
    )
    assert (wrong.status_code, wrong.json()["code"]) == (403, "auth.internal_token_invalid")
    assert (bearer.status_code, bearer.json()["code"]) == (403, "chat.execution_invalid")
    assert (cookie.status_code, cookie.json()["code"]) == (403, "chat.execution_invalid")
    assert (forged_tenant.status_code, forged_tenant.json()["code"]) == (
        422,
        "chat.request_invalid",
    )
    assert (duplicate.status_code, duplicate.json()["code"]) == (422, "chat.request_invalid")
    assert (oversized.status_code, oversized.json()["code"]) == (422, "chat.request_invalid")
    assert (mismatch.status_code, mismatch.json()["code"]) == (
        403,
        "chat.execution_invalid",
    )
    assert (expired.status_code, expired.json()["code"]) == (
        403,
        "chat.execution_invalid",
    )
    assert success.status_code == 200
    assert success.headers["Cache-Control"] == "no-store"
    assert (replay.status_code, replay.json()["code"]) == (403, "chat.execution_invalid")
    assert len(captured) == 1
    facts = captured[0]
    assert facts["endpoint"] == "chat/completions"
    assert facts["audit_endpoint"] == "/internal/chat/completions"
    assert facts["request_kind"] == "chat"
    assert facts["trusted_auth_context"].tenant_id == tenant_id
    assert facts["trusted_payload"] == {
        "model": "prd066-gateway-chat",
        "messages": [{"role": "user", "content": PROMPT_CANARY}],
        "stream": False,
    }
    connection = connect_database(database_url)
    grant_row = connection.execute(
        "SELECT status,grant_hash FROM chat_execution_grants"
    ).fetchone()
    database_text = " ".join(
        str(tuple(row))
        for table in ("chat_sessions", "chat_messages", "chat_execution_grants")
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
    )
    connection.close()
    assert grant_row["status"] == "consumed"
    assert grant_row["grant_hash"] == hashlib.sha256(GRANT.encode("ascii")).hexdigest()
    assert GRANT not in database_text
    assert PROMPT_CANARY not in database_text


@pytest.mark.anyio
async def test_internal_chat_missing_runtime_token_fails_unavailable() -> None:
    """配置异常：生产未加载内部 Token 时固定 503，且在读取 grant/正文前失败关闭。"""

    app = _build_app()
    app.state.config = app.state.config.model_copy(update={"internal_chat_token": None})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        unavailable = await client.post(
            "/internal/chat/completions", json=_body(), headers=_headers()
        )

    assert (unavailable.status_code, unavailable.json()["code"]) == (
        503,
        "chat.gateway_unavailable",
    )


@pytest.mark.anyio
async def test_internal_chat_rechecks_permission_before_governance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限：grant 签发后撤销 chat.use，Gateway 必须在 Provider/治理编排前失败关闭。"""

    database_url, tenant_id = _prepare_execution(monkeypatch, tmp_path)
    _revoke_chat_permission(database_url, tenant_id)
    called = False

    async def should_not_execute(request, **kwargs):
        """若实时权限门禁失效则立即让测试失败。"""

        nonlocal called
        called = True
        return JSONResponse({})

    monkeypatch.setattr(internal_chat, "_handle_gateway_request", should_not_execute)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        denied = await client.post(
            "/internal/chat/completions", json=_body(), headers=_headers()
        )

    assert (denied.status_code, denied.json()["code"]) == (403, "chat.execution_invalid")
    assert denied.headers["Cache-Control"] == "no-store"
    assert called is False
    connection = connect_database(database_url)
    status = connection.execute("SELECT status FROM chat_execution_grants").fetchone()[0]
    connection.close()
    # 权限失败发生在消费 CAS 前，Control 后续可以安全撤销这条 issued grant。
    assert status == "issued"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "resource_kind",
    ["project", "environment", "alias", "virtual_key", "second_active_key"],
)
async def test_internal_chat_rechecks_resource_state_before_governance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resource_kind: str,
) -> None:
    """资源：签发后任一 owner 资源失效或 Key 不再唯一，都不得进入治理/Provider。"""

    database_url, tenant_id = _prepare_execution(monkeypatch, tmp_path)
    _invalidate_execution_resource(database_url, tenant_id, resource_kind)
    called = False

    async def should_not_execute(request, **kwargs):
        """记录越过实时资源门禁的非法调用，并让断言能够给出精确原因。"""

        nonlocal called
        called = True
        return JSONResponse({})

    monkeypatch.setattr(internal_chat, "_handle_gateway_request", should_not_execute)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        denied = await client.post(
            "/internal/chat/completions", json=_body(), headers=_headers()
        )

    assert (denied.status_code, denied.json()["code"]) == (403, "chat.execution_invalid")
    assert denied.headers["Cache-Control"] == "no-store"
    assert called is False
    connection = connect_database(database_url)
    status = connection.execute("SELECT status FROM chat_execution_grants").fetchone()[0]
    connection.close()
    # 资源漂移在原子消费前被发现，Control 仍能把 issued grant 安全撤销。
    assert status == "issued"


def test_internal_chat_source_uses_governance_core_without_adapter_import() -> None:
    """架构门禁：内部路由只能调用共用编排，不能直接依赖或实例化 Provider Adapter。"""

    source = Path(internal_chat.__file__).read_text(encoding="utf-8")
    assert "_handle_gateway_request(" in source
    assert "adapter_for_route" not in source
    assert "ProviderAdapter" not in source


def test_request_log_endpoint_keeps_internal_identity_and_public_contract() -> None:
    """审计合同：内部绝对入口不加公开前缀，公开相对入口继续使用 `/v1/`。"""

    assert (
        internal_chat._handle_gateway_request.__module__
        == "metahub_gateway.api.openai_compatible"
    )
    from metahub_gateway.api import openai_compatible

    assert (
        openai_compatible._canonical_request_log_endpoint(
            "/internal/chat/completions"
        )
        == "/internal/chat/completions"
    )
    assert (
        openai_compatible._canonical_request_log_endpoint("chat/completions")
        == "/v1/chat/completions"
    )


@pytest.mark.anyio
async def test_internal_chat_malformed_json_drops_sensitive_exception_chain() -> None:
    """隐私：畸形 JSON 中的 Prompt 金丝雀不得进入统一错误的格式化 traceback。"""

    raw_body = (
        b'{"grant":"' + GRANT.encode("ascii")
        + b'","history":[{"role":"user","content":"'
        + PROMPT_CANARY.encode("ascii")
        + b'"}'
    )
    consumed = False

    async def receive() -> dict[str, object]:
        """仅向 Starlette Request 提供一次带金丝雀的畸形 HTTP 正文。"""

        nonlocal consumed
        if consumed:
            return {"type": "http.request", "body": b"", "more_body": False}
        consumed = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/chat/completions",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    with pytest.raises(ApiError) as exc:
        await internal_chat._validated_body(request)

    assert exc.value.code == "chat.request_invalid"
    # ``from None`` 必须隐藏 JSONDecodeError 的原文档，避免通用错误上报器收集 Prompt。
    assert PROMPT_CANARY not in "".join(traceback.format_exception(exc.value))
