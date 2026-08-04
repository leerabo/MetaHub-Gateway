"""PRD-067 Gateway 模板 Invocation、内存渲染和失败关闭集成测试。

测试复用 PRD-066 已审定的单次 grant 资源夹具，再只增量装配 0059 模板版本和 Invocation。
Prompt、变量值和渲染正文只存在于测试调用栈；数据库断言会动态扫描相关事实表，证明它们
没有被新实现持久化。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import metahub_gateway.api.internal_chat as internal_chat
import pytest
import test_prd066_internal_chat as prd066
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from metahub_shared.db import connect_database
from metahub_shared.prompt_templates import (
    PromptTemplateOwner,
    canonical_definition_bytes,
    change_summary_hmac,
    encrypt_definition,
    validate_definition,
)
from metahub_shared.rbac import permission_matrix_sha256

# 下列固定值只构造隔离的模板 owner 与内存正文；数据库和错误响应会专项扫描明文零命中。
PROMPT_TEMPLATE_SECRET = "prd067-template-encryption-secret-2026-unique-domain"
PROMPT_TEMPLATE_ID = "ptpl_" + "6" * 32
PROMPT_TEMPLATE_VERSION_ID = "ptv_" + "7" * 32
PROMPT_TEMPLATE_INVOCATION_ID = "ptinv_" + "8" * 32
PROMPT_CONTENT = "请为客户 ${customer_name} 生成一条合规欢迎语。"
VARIABLE_VALUE = "PRD067_VARIABLE_MEMORY_ONLY"
RENDERED_USER = f"请为客户 {VARIABLE_VALUE} 生成一条合规欢迎语。"


def _prepare_template_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    corrupt_ciphertext: bool = False,
) -> tuple[str, str]:
    """在合法 PRD-066 grant 图上装配一份 published 模板和 in-flight Invocation。

    参数：隔离环境、pytest 临时目录和密文损坏开关。返回：数据库 URL 与 tenant ID。
    关键约束：先插入带延迟 head FK 的模板，再插入 building 版本并一次 seal；测试不会
    绕过 0059 trigger，也不会把 canonical definition 明文写入普通数据库列。
    """

    monkeypatch.setenv(
        "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET",
        PROMPT_TEMPLATE_SECRET,
    )
    database_url, tenant_id = prd066._prepare_execution(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    grant = connection.execute(
        "SELECT * FROM chat_execution_grants WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    assert grant is not None
    definition = validate_definition(
        content=PROMPT_CONTENT,
        variables=[
            {
                "name": "customer_name",
                "label": "客户名称",
                "required": True,
                "max_chars": 80,
            }
        ],
        change_summary="发布 PRD-067 Gateway 验收版本",
    )
    owner = PromptTemplateOwner(
        tenant_id=tenant_id,
        prompt_template_id=PROMPT_TEMPLATE_ID,
        prompt_template_version_id=PROMPT_TEMPLATE_VERSION_ID,
        version_number=1,
    )
    ciphertext = encrypt_definition(
        definition,
        secret=PROMPT_TEMPLATE_SECRET,
        owner=owner,
    )
    if corrupt_ciphertext:
        # 保留迁移允许的版本化外形，只篡改认证数据；Gateway 应映射为 response_invalid。
        ciphertext = ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")
    canonical = canonical_definition_bytes(definition)
    now = prd066._time(datetime.now(UTC) + timedelta(seconds=1))
    connection.execute("BEGIN IMMEDIATE")
    # head FK 延迟到 COMMIT，允许同事务随后创建并 seal v1；提交前仍必须证明 published。
    connection.execute(
        """
        INSERT INTO prompt_templates(
          prompt_template_id,tenant_id,name,description,status,current_version_id,
          current_version_state,revision,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,'PRD-067 Gateway 模板','Gateway 集成测试','active',?,
                 'published',1,?,?,?,?)
        """,
        (
            PROMPT_TEMPLATE_ID,
            tenant_id,
            PROMPT_TEMPLATE_VERSION_ID,
            grant["owner_user_id"],
            grant["owner_user_id"],
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO prompt_template_versions(
          prompt_template_version_id,tenant_id,prompt_template_id,version_number,state,
          definition_schema_version,definition_ciphertext,definition_size_bytes,variable_count,
          project_scope,recommended_model_count,change_summary_hmac,created_by,created_at,
          published_at,request_id,trace_id
        ) VALUES(?,?,?,1,'building','prompt-template-v1',?,?,1,'all',0,?,?,?,NULL,
                 'req_prd067_gateway_version','trc_prd067_gateway_version')
        """,
        (
            PROMPT_TEMPLATE_VERSION_ID,
            tenant_id,
            PROMPT_TEMPLATE_ID,
            ciphertext,
            len(canonical),
            change_summary_hmac(PROMPT_TEMPLATE_SECRET, definition.change_summary),
            grant["owner_user_id"],
            now,
        ),
    )
    connection.execute(
        """
        UPDATE prompt_template_versions SET state='published',published_at=?
        WHERE tenant_id=? AND prompt_template_version_id=? AND state='building'
        """,
        (now, tenant_id, PROMPT_TEMPLATE_VERSION_ID),
    )
    connection.execute(
        """
        INSERT INTO chat_prompt_template_invocations(
          prompt_template_invocation_id,tenant_id,prompt_template_id,
          prompt_template_version_id,version_number,chat_session_id,chat_message_id,
          actor_user_id,actor_auth_session_id,gateway_request_id,gateway_trace_id,
          status,failure_code,created_at,completed_at
        ) VALUES(?,?,?,?,1,?,?,?,?,?,?,'in_flight',NULL,?,NULL)
        """,
        (
            PROMPT_TEMPLATE_INVOCATION_ID,
            tenant_id,
            PROMPT_TEMPLATE_ID,
            PROMPT_TEMPLATE_VERSION_ID,
            grant["chat_session_id"],
            grant["chat_message_id"],
            grant["owner_user_id"],
            grant["owner_auth_session_id"],
            prd066.REQUEST_ID,
            prd066.TRACE_ID,
            now,
        ),
    )
    connection.commit()
    connection.close()
    # 返回可信数据库 owner，后续请求不允许通过 body 覆盖 tenant 或模板身份。
    return database_url, tenant_id


def _template_body(*, variables: dict[str, object] | None = None) -> dict[str, object]:
    """构造只在当前 HTTP 调用存活的模板 exact-set wire。"""

    return {
        "grant": prd066.GRANT,
        "history": [],
        "variables": (
            {"customer_name": VARIABLE_VALUE} if variables is None else variables
        ),
    }


def _revoke_prompt_template_use(database_url: str, tenant_id: str) -> None:
    """合法撤销 Viewer 的模板 use 并同步 revision/hash，保留 chat.use 供竞态验收。"""

    connection = connect_database(database_url)
    role = connection.execute(
        "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='viewer'",
        (tenant_id,),
    ).fetchone()
    permission = connection.execute(
        "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='prompt_template.use'",
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
            prd066._time(datetime.now(UTC) + timedelta(seconds=2)),
            tenant_id,
            role["role_id"],
        ),
    )
    connection.commit()
    connection.close()


@pytest.mark.anyio
async def test_template_invocation_renders_before_shared_governance_and_stays_private(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：AAD 解密和内存渲染后追加 final user，再进入既有治理且不落业务库。"""

    database_url, tenant_id = _prepare_template_execution(monkeypatch, tmp_path)
    captured: list[dict[str, Any]] = []

    async def governed_response(request, **kwargs):
        """捕获共享治理入口参数，模拟一个固定非流式 Provider 成功响应。"""

        captured.append(kwargs)
        return JSONResponse(
            {"choices": [{"message": {"role": "assistant", "content": "已生成"}}]}
        )

    monkeypatch.setattr(internal_chat, "_handle_gateway_request", governed_response)
    app = prd066._build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/internal/chat/completions",
            json=_template_body(),
            headers=prd066._headers(),
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["rendered_user"] == RENDERED_USER
    assert len(captured) == 1
    assert captured[0]["trusted_payload"] == {
        "model": "prd066-gateway-chat",
        "messages": [{"role": "user", "content": RENDERED_USER}],
        "stream": False,
    }
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM chat_execution_grants WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0] == "consumed"
    persisted = " ".join(
        str(tuple(row))
        for table in (
            "prompt_templates",
            "prompt_template_versions",
            "chat_prompt_template_invocations",
            "chat_execution_grants",
            "chat_messages",
        )
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
    )
    connection.close()
    assert PROMPT_CONTENT not in persisted
    assert "customer_name" not in persisted
    assert VARIABLE_VALUE not in persisted
    assert RENDERED_USER not in persisted


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure_kind", "expected_code"),
    [
        ("variables", "chat.execution_invalid"),
        ("permission", "chat.execution_invalid"),
        ("secret", "chat.gateway_unavailable"),
        ("ciphertext", "chat.response_invalid"),
    ],
)
async def test_template_failures_do_not_consume_grant_or_call_governance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_kind: str,
    expected_code: str,
) -> None:
    """异常/权限：变量、撤权、部署密钥和认证密文失败均在 Provider 前关闭。"""

    database_url, tenant_id = _prepare_template_execution(
        monkeypatch,
        tmp_path,
        corrupt_ciphertext=failure_kind == "ciphertext",
    )
    # 撤权必须在 Gateway 事务内实时观察，不能依赖 Control 准备阶段的旧权限快照。
    if failure_kind == "permission":
        _revoke_prompt_template_use(database_url, tenant_id)
    app = prd066._build_app()
    # 缺密钥属于部署不可用分支，测试只替换内存 config，不写任何默认或回退密钥。
    if failure_kind == "secret":
        app.state.config = app.state.config.model_copy(
            update={"prompt_template_encryption_secret": None}
        )
    called = False

    async def should_not_execute(request, **kwargs):
        """记录非法越过模板门禁的调用；任一调用都会使测试失败。"""

        nonlocal called
        called = True
        return JSONResponse({})

    monkeypatch.setattr(internal_chat, "_handle_gateway_request", should_not_execute)
    body = (
        _template_body(variables={"unknown_name": VARIABLE_VALUE})
        if failure_kind == "variables"
        else _template_body()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/internal/chat/completions",
            json=body,
            headers=prd066._headers(),
        )

    assert response.status_code >= 400
    assert response.json()["code"] == expected_code
    assert called is False
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM chat_execution_grants WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0] == "issued"
    assert connection.execute(
        "SELECT status FROM chat_prompt_template_invocations WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0] == "in_flight"
    connection.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"grant": prd066.GRANT, "history": []},
        {
            "grant": prd066.GRANT,
            "history": [{"role": "user", "content": "未闭合模板历史"}],
            "variables": {},
        },
        {"grant": prd066.GRANT, "history": [], "variables": {}, "template_id": "forged"},
        {"grant": prd066.GRANT, "history": [], "variables": {"name": {"nested": True}}},
    ],
)
async def test_wire_exact_sets_fail_before_database_or_governance(
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, object],
) -> None:
    """协议异常：空普通 history、半对模板历史、身份注入和嵌套变量固定返回 422。"""

    monkeypatch.setenv("METAHUB_INTERNAL_CHAT_TOKEN", prd066.INTERNAL_TOKEN)
    app = prd066._build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/internal/chat/completions",
            content=json.dumps(body).encode("utf-8"),
            headers={**prd066._headers(), "Content-Type": "application/json"},
        )

    assert (response.status_code, response.json()["code"]) == (
        422,
        "chat.request_invalid",
    )
