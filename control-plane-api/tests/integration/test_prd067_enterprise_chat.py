"""PRD-067 模板接入 Control 企业 Chat 的主流程、异常、权限与隐私集成测试。

本文件复用 PRD-066 已验证的隔离库、登录、Session 和正式 Usage 夹具，只增加模板域事实，
避免复制一整套 Chat 测试环境后让两个基线漂移。Prompt、变量值和渲染正文只在当前测试调用栈
存在；数据库断言必须证明 Invocation 可追踪而三类明文均未落库。
"""

from __future__ import annotations

from pathlib import Path

import metahub_control.enterprise_chat.service as chat_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.prompt_templates.routes import router as prompt_template_router
from metahub_shared.db import connect_database
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.errors import ApiError
from test_prd066_enterprise_chat import (
    ADMIN_PASSWORD,
    VIEWER_PASSWORD,
    _build_app,
    _headers,
    _insert_default_tenant_viewer,
    _login,
    _prepare_database,
    _successful_gateway,
)
from test_prd066_enterprise_chat import (
    PROMPT_CANARY as _PRD066_PROMPT_CANARY,
)

# 三个金丝雀分别覆盖加密定义、浏览器变量和渲染正文；固定值只用于零落库断言。
_TEMPLATE_CANARY = "PRD067_TEMPLATE_ONLY_IN_CIPHERTEXT"
_VARIABLE_CANARY = "PRD067_VARIABLE_ONLY_IN_MEMORY"
_RENDERED_USER = f"{_TEMPLATE_CANARY}: {_VARIABLE_CANARY}"
_TEMPLATE_SECRET = "prd067-enterprise-chat-template-secret-0123456789abcdef"


def _template_payload(project_id: str, alias_id: str) -> dict[str, object]:
    """构造一个仅适用于当前测试 Project 的不可变模板 v1 请求。"""

    return {
        "name": "企业 Chat 受控模板",
        "description": "仅供 PRD-067 Control Chat 集成测试",
        "content": f"{_TEMPLATE_CANARY}: ${{customer_name}}",
        "variables": [
            {
                "name": "customer_name",
                "label": "客户名称",
                "required": True,
                "max_chars": 80,
            }
        ],
        "project_scope": "selected",
        "project_ids": [project_id],
        "recommended_model_alias_ids": [alias_id],
        "change_summary": "创建企业 Chat 模板",
    }


def _successful_template_gateway(database_url: str):
    """把 PRD-066 正式治理事实替身扩展为模板成功响应。

    参数：当前测试隔离库 URL。返回：接受模板变量 wire 的异步 Gateway 替身。关键约束：
    只验证变量 map 和完整历史对，不保存变量或渲染正文；RequestLog、Usage 和 grant 消费仍由
    已覆盖的 PRD-066 夹具生成，避免模板测试伪造另一套成本口径。
    """

    normal_gateway = _successful_gateway(database_url)

    async def call_gateway(
        config,
        *,
        grant: str,
        history: list[dict[str, str]],
        request_id: str,
        trace_id: str,
        template_variables: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """消费模板 grant，并只在当前返回对象附加渲染 user 正文。"""

        if history != [] or template_variables != {"customer_name": _VARIABLE_CANARY}:
            # 失败信息固定且不拼接变量值，防止测试异常把客户输入复制到输出证据。
            raise AssertionError("template gateway wire mismatch")
        payload = await normal_gateway(
            config,
            grant=grant,
            # 复用夹具只需要一条不会落库的合法 user history；真实模板 history 仍在上方精确验证为空。
            history=[{"role": "user", "content": _PRD066_PROMPT_CANARY}],
            request_id=request_id,
            trace_id=trace_id,
        )
        return {**payload, "rendered_user": _RENDERED_USER}

    return call_gateway


def _revoke_viewer_template_use(database_url: str, tenant_id: str) -> None:
    """合法撤销当前租户 Viewer 的 prompt_template.use 并同步权限矩阵指纹。"""

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
            "2026-08-02T09:30:00Z",
            tenant_id,
            role["role_id"],
        ),
    )
    connection.commit()
    connection.close()


async def _create_template_and_session(
    client: AsyncClient,
    *,
    admin_token: str,
    viewer_token: str,
    project_id: str,
    environment_id: str,
    alias_id: str,
) -> tuple[str, str]:
    """创建模板 v1 和 Viewer ChatSession，并返回版本与会话 ID。"""

    created = await client.post(
        "/prompt-templates",
        json=_template_payload(project_id, alias_id),
        headers=_headers(admin_token, "prd067-chat-template-create"),
    )
    assert created.status_code == 201
    version_id = str(created.json()["version"]["prompt_template_version_id"])
    session = await client.post(
        "/chat/sessions",
        json={
            "project_id": project_id,
            "environment_id": environment_id,
            "model_alias_id": alias_id,
        },
        headers=_headers(viewer_token, "prd067-chat-session-create"),
    )
    assert session.status_code == 201
    # 返回后端创建的同租户 version/session owner，测试请求不得自行伪造 tenant 或模板 ID。
    return version_id, str(session.json()["session"]["chat_session_id"])


@pytest.mark.anyio
async def test_prd067_control_chat_tracks_template_without_persisting_plaintext(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主/异常：目录、变量拒绝、成功终态、停用和明文零落库均成立。"""

    database_url, tenant_id, project_id, environment_id, alias_id, _ = _prepare_database(
        monkeypatch, tmp_path
    )
    _insert_default_tenant_viewer(database_url, tenant_id)
    monkeypatch.setenv("METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET", _TEMPLATE_SECRET)
    monkeypatch.setattr(
        chat_service,
        "_call_gateway",
        _successful_template_gateway(database_url),
    )
    app = _build_app()
    app.include_router(prompt_template_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id="prd067-chat-admin-login",
        )
        viewer_token = await _login(
            client,
            tenant_slug="default",
            email="default-viewer-prd066@example.com",
            password=VIEWER_PASSWORD,
            request_id="prd067-chat-viewer-login",
        )
        version_id, session_id = await _create_template_and_session(
            client,
            admin_token=admin_token,
            viewer_token=viewer_token,
            project_id=project_id,
            environment_id=environment_id,
            alias_id=alias_id,
        )
        options = await client.get(
            "/chat/options",
            headers=_headers(viewer_token, "prd067-chat-options"),
        )
        assert options.status_code == 200
        assert options.headers["cache-control"] == "no-store"
        option_version_ids = [
            item["prompt_template_version_id"]
            for item in options.json()["prompt_templates"]
        ]
        assert option_version_ids == [version_id]
        assert _TEMPLATE_CANARY not in options.text

        invalid_variables = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": "67000000-0000-4000-8000-000000000001",
                "history": [],
                "prompt_template_version_id": version_id,
                "template_variables": {},
            },
            headers=_headers(viewer_token, "prd067-chat-invalid-variables"),
        )
        assert invalid_variables.status_code == 422
        assert invalid_variables.json()["code"] == "prompt_template.variables_invalid"

        succeeded = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": "67000000-0000-4000-8000-000000000002",
                "history": [],
                "prompt_template_version_id": version_id,
                "template_variables": {"customer_name": _VARIABLE_CANARY},
            },
            headers=_headers(viewer_token, "prd067-chat-success"),
        )
        assert succeeded.status_code == 200
        assert succeeded.json()["rendered_user"] == _RENDERED_USER

        detail = await client.get(
            "/prompt-templates?limit=10&offset=0",
            headers=_headers(admin_token, "prd067-chat-template-list"),
        )
        template = detail.json()["prompt_templates"][0]
        disabled = await client.patch(
            f"/prompt-templates/{template['prompt_template_id']}",
            json={"expected_revision": template["metadata_revision"], "status": "disabled"},
            headers=_headers(admin_token, "prd067-chat-template-disable"),
        )
        assert disabled.status_code == 200
        rejected_disabled = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": "67000000-0000-4000-8000-000000000003",
                "history": [],
                "prompt_template_version_id": version_id,
                "template_variables": {"customer_name": _VARIABLE_CANARY},
            },
            headers=_headers(viewer_token, "prd067-chat-disabled"),
        )
        assert rejected_disabled.status_code == 409
        assert rejected_disabled.json()["code"] == "prompt_template.disabled"

    connection = connect_database(database_url)
    invocation = connection.execute(
        """
        SELECT status,failure_code,completed_at FROM chat_prompt_template_invocations
        WHERE tenant_id=? AND prompt_template_version_id=?
        """,
        (tenant_id, version_id),
    ).fetchone()
    assert invocation is not None
    assert (invocation["status"], invocation["failure_code"]) == ("succeeded", None)
    assert invocation["completed_at"] is not None
    assert connection.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0] == 1
    database_dump = "\n".join(connection.iterdump())
    connection.close()
    assert _TEMPLATE_CANARY not in database_dump
    assert _VARIABLE_CANARY not in database_dump
    assert _RENDERED_USER not in database_dump


@pytest.mark.anyio
async def test_prd067_control_chat_rechecks_template_use_before_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限：目录读取后实时撤销 use，发送必须 403 且 Message/Grant/Invocation 零写入。"""

    database_url, tenant_id, project_id, environment_id, alias_id, _ = _prepare_database(
        monkeypatch, tmp_path
    )
    _insert_default_tenant_viewer(database_url, tenant_id)
    monkeypatch.setenv("METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET", _TEMPLATE_SECRET)

    async def gateway_must_not_run(*args, **kwargs):
        """撤权必须在 Control 写事务内截止，绝不能到达 Gateway 或 Provider。"""

        # 一旦触达此桩就证明后端实时权限门禁失效，立即中止而不记录任何敏感请求参数。
        raise AssertionError("gateway called after prompt template permission revocation")

    monkeypatch.setattr(chat_service, "_call_gateway", gateway_must_not_run)
    app = _build_app()
    app.include_router(prompt_template_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id="prd067-chat-revoke-admin-login",
        )
        viewer_token = await _login(
            client,
            tenant_slug="default",
            email="default-viewer-prd066@example.com",
            password=VIEWER_PASSWORD,
            request_id="prd067-chat-revoke-viewer-login",
        )
        version_id, session_id = await _create_template_and_session(
            client,
            admin_token=admin_token,
            viewer_token=viewer_token,
            project_id=project_id,
            environment_id=environment_id,
            alias_id=alias_id,
        )
        before_revoke = await client.get(
            "/chat/options",
            headers=_headers(viewer_token, "prd067-chat-revoke-options"),
        )
        assert before_revoke.status_code == 200
        assert len(before_revoke.json()["prompt_templates"]) == 1
        _revoke_viewer_template_use(database_url, tenant_id)
        denied = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": "67000000-0000-4000-8000-000000000004",
                "history": [],
                "prompt_template_version_id": version_id,
                "template_variables": {"customer_name": _VARIABLE_CANARY},
            },
            headers=_headers(viewer_token, "prd067-chat-revoke-send"),
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"

    connection = connect_database(database_url)
    counts = tuple(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
        for table in (
            "chat_messages",
            "chat_execution_grants",
            "chat_prompt_template_invocations",
        )
    )
    connection.close()
    assert counts == (0, 0, 0)


@pytest.mark.anyio
async def test_prd067_control_chat_rejects_stale_version_and_finalizes_gateway_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """竞态/失败：旧版本零写入，新版本调用失败时三类执行事实同步终结。"""

    database_url, tenant_id, project_id, environment_id, alias_id, _ = _prepare_database(
        monkeypatch, tmp_path
    )
    _insert_default_tenant_viewer(database_url, tenant_id)
    monkeypatch.setenv("METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET", _TEMPLATE_SECRET)

    async def fail_gateway(*args, **kwargs):
        """模拟已进入内部调用后的固定 Gateway 故障，不保存正文或变量参数。"""

        # 只抛目录化错误，确保 Control 终结路径不会把传入变量或底层网络详情落库。
        raise ApiError("chat.gateway_unavailable")

    monkeypatch.setattr(chat_service, "_call_gateway", fail_gateway)
    app = _build_app()
    app.include_router(prompt_template_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id="prd067-chat-stale-admin-login",
        )
        viewer_token = await _login(
            client,
            tenant_slug="default",
            email="default-viewer-prd066@example.com",
            password=VIEWER_PASSWORD,
            request_id="prd067-chat-stale-viewer-login",
        )
        version_v1, session_id = await _create_template_and_session(
            client,
            admin_token=admin_token,
            viewer_token=viewer_token,
            project_id=project_id,
            environment_id=environment_id,
            alias_id=alias_id,
        )
        listing = await client.get(
            "/prompt-templates?limit=10&offset=0",
            headers=_headers(admin_token, "prd067-chat-stale-list"),
        )
        template = listing.json()["prompt_templates"][0]
        published = await client.post(
            f"/prompt-templates/{template['prompt_template_id']}/versions",
            json={
                "expected_revision": template["metadata_revision"],
                "content": f"{_TEMPLATE_CANARY}: ${{customer_name}}",
                "variables": [
                    {
                        "name": "customer_name",
                        "label": "客户名称",
                        "required": True,
                        "max_chars": 80,
                    }
                ],
                "project_scope": "selected",
                "project_ids": [project_id],
                "recommended_model_alias_ids": [alias_id],
                "change_summary": "发布并发校验版本",
            },
            headers=_headers(admin_token, "prd067-chat-publish-v2"),
        )
        assert published.status_code == 201
        version_v2 = published.json()["version"]["prompt_template_version_id"]

        stale = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": "67000000-0000-4000-8000-000000000005",
                "history": [],
                "prompt_template_version_id": version_v1,
                "template_variables": {"customer_name": _VARIABLE_CANARY},
            },
            headers=_headers(viewer_token, "prd067-chat-stale-version"),
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "prompt_template.version_conflict"

        failed = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": "67000000-0000-4000-8000-000000000006",
                "history": [],
                "prompt_template_version_id": version_v2,
                "template_variables": {"customer_name": _VARIABLE_CANARY},
            },
            headers=_headers(viewer_token, "prd067-chat-gateway-failure"),
        )
        assert failed.status_code == 503
        assert failed.json()["code"] == "chat.gateway_unavailable"

    connection = connect_database(database_url)
    invocation = connection.execute(
        """
        SELECT status,failure_code,completed_at FROM chat_prompt_template_invocations
        WHERE tenant_id=? AND prompt_template_version_id=?
        """,
        (tenant_id, version_v2),
    ).fetchone()
    message = connection.execute(
        "SELECT status,error_code,completed_at FROM chat_messages WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    grant = connection.execute(
        "SELECT status,revoked_at FROM chat_execution_grants WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    # 旧版本请求必须在建事实前失败，因此库中只允许出现新版本失败调用的一组三联终态。
    counts = tuple(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0]
        for table in (
            "chat_messages",
            "chat_execution_grants",
            "chat_prompt_template_invocations",
        )
    )
    database_dump = "\n".join(connection.iterdump())
    connection.close()
    assert counts == (1, 1, 1)
    assert invocation is not None
    assert tuple(invocation[:2]) == ("failed", "chat.gateway_unavailable")
    assert invocation["completed_at"] is not None
    assert message is not None
    assert tuple(message[:2]) == ("failed", "chat.gateway_unavailable")
    assert message["completed_at"] is not None
    assert grant is not None and tuple(grant) == ("revoked", grant["revoked_at"])
    assert grant["revoked_at"] is not None
    assert _TEMPLATE_CANARY not in database_dump
    assert _VARIABLE_CANARY not in database_dump
    assert _RENDERED_USER not in database_dump
