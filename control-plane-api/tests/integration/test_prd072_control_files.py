"""PRD-072 文件上传 Control 主流程、异常、权限、租户和隐私集成测试。

文件名显式包含 ``control``，避免与 Shared 的同名专项在根级 pytest 收集时发生模块冲突。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import metahub_control.files.routes as file_routes
import metahub_control.files.service as file_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.files.routes import router as files_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.files import FileContractError
from metahub_shared.runtime.fastapi_app import create_service_app

# 所有密码、密钥和正文金丝雀只属于 D:\MetaHub\.tmp 下的隔离测试资产。
ADMIN_PASSWORD = "Prd072Admin!2026"
VIEWER_PASSWORD = "Prd072Viewer!2026"
SECURITY_PASSWORD = "Prd072Security!2026"
OTHER_PASSWORD = "Prd072Other!2026"
AUTH_SECRET = "prd072-auth-domain-secret-0123456789abcdef"
FILE_SECRET = "prd072-file-domain-secret-0123456789abcdef"
TEXT_CANARY = "PRD072_CONTENT_MUST_ONLY_EXIST_IN_ENCRYPTED_BLOBS"
OTHER_TENANT_ID = "tenant_prd072_other"
OTHER_TENANT_SLUG = "prd072-other"


def _url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为项目 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def prd072_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """迁移/seed 一次并建立 Viewer、SecurityAdmin 与第二租户身份图。"""

    root = tmp_path_factory.mktemp("prd072-control-template")
    database = root / "template.db"
    database_url = _url(database)
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    try:
        apply_migrations(database_url)
        seed = apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous

    connection = connect_database(database_url)
    now = "2026-08-05T00:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        [
            (
                "user_prd072_viewer",
                seed.tenant_id,
                "viewer@prd072.local",
                "PRD072 Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
                now,
                now,
            ),
            (
                "user_prd072_security",
                seed.tenant_id,
                "security@prd072.local",
                "PRD072 Security",
                hash_password(SECURITY_PASSWORD),
                "security_admin",
                now,
                now,
            ),
        ],
    )
    # tenant insert trigger 物化 revision 8 目录与规范角色，第二租户不会借用默认租户权限行。
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,'PRD072 Other','internal_only','active',?,?)
        """,
        (OTHER_TENANT_ID, OTHER_TENANT_SLUG, now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('user_prd072_other',?,'admin@prd072-other.local','Other Admin',?,
                 'admin','active',?,?)
        """,
        (OTHER_TENANT_ID, hash_password(OTHER_PASSWORD), now, now),
    )
    connection.commit()
    connection.close()
    return database


def _clone_environment(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, Path]:
    """复制关闭模板库并把数据库、Blob、密钥和日志限定到当前用例。"""

    database = tmp_path / "prd072-control.db"
    storage = tmp_path / "file-storage"
    shutil.copy2(template, database)
    database_url = _url(database)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_FILE_ENCRYPTION_SECRET", FILE_SECRET)
    monkeypatch.setenv("METAHUB_FILE_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    return database_url, storage


def _build_app():
    """构造只装配认证和文件域的真实中间件应用。"""

    app = create_service_app("control-plane-api", "PRD-072 File Test API")
    app.include_router(auth_router)
    app.include_router(files_router)
    return app


async def _login(
    client: AsyncClient,
    *,
    email: str,
    password: str,
    tenant_slug: str = "default",
) -> str:
    """登录隔离用户并仅向当前调用栈返回 Session Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Session Token 只供当前隔离 HTTP 调用栈使用，禁止写入测试证据、普通日志或持久状态。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造隔离请求的认证与链路 Header。

    参数为当前测试调用栈 Token 和稳定 request ID，返回只含两个公开 Header 的字典；
    tenant、actor 与角色必须由后端 Session 恢复，不能由测试客户端直接注入。
    """

    # 返回值只加入公开认证与链路 Header；tenant、用户和角色必须由后端 Session 恢复，
    # 测试不能通过自定义 Header 绕过真实的后端租户与权限流程。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _multipart_payload(content: bytes) -> tuple[bytes, str]:
    """构造只有一个 file part 的规范 multipart 测试正文。

    参数为有界测试 bytes，返回原始 HTTP 正文和 Content-Type；固定 ASCII 文件名避免大小
    门禁测试被文件名规范化干扰，tenant 与 actor 仍只来自后端 Session。
    """

    boundary = "prd072-bounded-test"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="bounded.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


@pytest.mark.anyio
async def test_file_txt_main_flow_is_encrypted_listed_and_tenant_scoped(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """覆盖 TXT 上传、详情、列表、密文落盘和跨租户 404 主流程。"""

    database_url, storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        admin_token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        created = await client.post(
            "/files",
            files={"file": ("治理说明.txt", TEXT_CANARY.encode(), "text/plain")},
            headers=_headers(admin_token, "prd072-main-upload"),
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store, private"
        body = created.json()
        file_id = body["file"]["file_id"]
        assert body["request_id"] == "prd072-main-upload"
        assert body["file"]["status"] == "ready"
        assert body["file"]["virus_scan_status"] == "not_configured"
        assert body["file"]["dlp_scan_status"] == "clean"
        assert body["file"]["extract"]["estimated_tokens"] > 0

        detail = await client.get(
            f"/files/{file_id}",
            headers=_headers(admin_token, "prd072-main-detail"),
        )
        listed = await client.get(
            "/files?limit=25",
            headers=_headers(admin_token, "prd072-main-list"),
        )
        assert detail.status_code == listed.status_code == 200
        assert detail.json()["file"]["original_name"] == "治理说明.txt"
        assert [item["file_id"] for item in listed.json()["files"]] == [file_id]
        assert listed.json()["next_cursor"] is None

        other_token = await _login(
            client,
            tenant_slug=OTHER_TENANT_SLUG,
            email="admin@prd072-other.local",
            password=OTHER_PASSWORD,
        )
        hidden = await client.get(
            f"/files/{file_id}",
            headers=_headers(other_token, "prd072-cross-detail"),
        )
        other_list = await client.get(
            "/files",
            headers=_headers(other_token, "prd072-cross-list"),
        )
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "file.not_found"
        assert other_list.status_code == 200
        assert other_list.json()["files"] == []

    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT * FROM uploaded_files WHERE file_id=?",
        (file_id,),
    ).fetchone()
    assert row is not None and row["status"] == "ready"
    assert isinstance(row["original_name_ciphertext"], bytes)
    assert "治理说明".encode() not in row["original_name_ciphertext"]
    assert connection.execute(
        "SELECT COUNT(*) FROM file_events WHERE file_id=?",
        (file_id,),
    ).fetchone()[0] == 2
    connection.close()
    blobs = list(storage.rglob("*.bin"))
    assert len(blobs) == 2
    assert all(TEXT_CANARY.encode() not in path.read_bytes() for path in blobs)
    assert TEXT_CANARY.encode() not in Path(database_url.removeprefix("sqlite:///")).read_bytes()


@pytest.mark.anyio
async def test_file_dlp_block_commits_security_fact_without_blob(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """覆盖全文 DLP block、403、可查询终态、命中摘要和零正文对象。"""

    database_url, storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    connection = connect_database(database_url)
    owner = connection.execute(
        "SELECT tenant_id,user_id FROM users WHERE email='admin@metahub.local'"
    ).fetchone()
    pattern = "STRICT_PRD072_BLOCK"
    pattern_hash = "sha256:" + hashlib.sha256(f"keyword:{pattern}".encode()).hexdigest()
    now = "2026-08-05T00:01:00Z"
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,scan_scope,
          status,priority,description,created_by,updated_by,created_at,updated_at
        ) VALUES('dlp_prd072_block',?,'PRD072 block','keyword',?,?,'block','input',
                 'active',1,'',?,?,?,?)
        """,
        (
            owner["tenant_id"],
            pattern,
            pattern_hash,
            owner["user_id"],
            owner["user_id"],
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        blocked = await client.post(
            "/files",
            files={"file": ("blocked.txt", pattern.encode(), "text/plain")},
            headers=_headers(token, "prd072-block-upload"),
        )
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["code"] == "file.dlp_blocked"
        assert blocked.json()["request_id"] == "prd072-block-upload"
        listed = await client.get(
            "/files?status=blocked",
            headers=_headers(token, "prd072-block-list"),
        )
        assert listed.status_code == 200
        assert len(listed.json()["files"]) == 1
        file_id = listed.json()["files"][0]["file_id"]
        assert listed.json()["files"][0]["failure_error_code"] == "file.dlp_blocked"

    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT * FROM uploaded_files WHERE file_id=?",
        (file_id,),
    ).fetchone()
    assert row["status"] == "blocked" and row["original_object_key"] is None
    assert connection.execute(
        "SELECT COUNT(*) FROM file_dlp_matches WHERE file_id=?",
        (file_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM file_events WHERE file_id=?",
        (file_id,),
    ).fetchone()[0] == 2
    connection.close()
    assert not list(storage.rglob("*.bin"))


@pytest.mark.anyio
async def test_file_dlp_log_only_keeps_ready_blobs_and_safe_match_summary(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """覆盖 log_only 命中仍 ready、密文发布和逐规则安全摘要。"""

    database_url, storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    connection = connect_database(database_url)
    owner = connection.execute(
        "SELECT tenant_id,user_id FROM users WHERE email='admin@metahub.local'"
    ).fetchone()
    assert owner is not None
    pattern = "PRD072_LOG_ONLY_CANARY"
    pattern_hash = "sha256:" + hashlib.sha256(f"keyword:{pattern}".encode()).hexdigest()
    now = "2026-08-05T00:01:30Z"
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,scan_scope,
          status,priority,description,created_by,updated_by,created_at,updated_at
        ) VALUES('dlp_prd072_log_only',?,'PRD072 log only','keyword',?,?,'log_only',
                 'input','active',1,'',?,?,?,?)
        """,
        (
            owner["tenant_id"],
            pattern,
            pattern_hash,
            owner["user_id"],
            owner["user_id"],
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        created = await client.post(
            "/files",
            files={"file": ("log-only.txt", pattern.encode(), "text/plain")},
            headers=_headers(token, "prd072-log-only"),
        )
        assert created.status_code == 201, created.text
        info = created.json()["file"]
        file_id = info["file_id"]
        assert (info["status"], info["dlp_scan_status"], info["dlp_action"]) == (
            "ready",
            "matched",
            "log_only",
        )
        assert info["match_count"] == 1

    connection = connect_database(database_url)
    match = connection.execute(
        "SELECT action FROM file_dlp_matches WHERE tenant_id=? AND file_id=?",
        (owner["tenant_id"], file_id),
    ).fetchone()
    assert match is not None and match["action"] == "log_only"
    connection.close()
    # log_only 允许业务继续，但两个对象仍必须是认证密文且不能含命中正文。
    blobs = list(storage.rglob("*.bin"))
    assert len(blobs) == 2
    assert all(pattern.encode() not in path.read_bytes() for path in blobs)


@pytest.mark.anyio
async def test_file_dlp_rule_overflow_fails_closed_without_skipping_tail_rule(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """证明第 101 条 active 规则不会因查询截断而绕过文件全文预扫描。"""

    database_url, storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    connection = connect_database(database_url)
    owner = connection.execute(
        "SELECT tenant_id,user_id FROM users WHERE email='admin@metahub.local'"
    ).fetchone()
    assert owner is not None
    now = "2026-08-05T00:02:00Z"
    rows: list[tuple[object, ...]] = []
    for index in range(101):
        pattern = f"PRD072_RULE_OVERFLOW_{index:03d}"
        pattern_hash = "sha256:" + hashlib.sha256(
            f"keyword:{pattern}".encode()
        ).hexdigest()
        rows.append(
            (
                f"dlp_prd072_overflow_{index:03d}",
                owner["tenant_id"],
                f"PRD072 overflow {index:03d}",
                pattern,
                pattern_hash,
                "block" if index == 100 else "allow",
                index + 1,
                owner["user_id"],
                owner["user_id"],
                now,
                now,
            )
        )
    connection.executemany(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,scan_scope,
          status,priority,description,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'keyword',?,?,?,'input','active',?,'',?,?,?,?)
        """,
        rows,
    )
    connection.commit()
    connection.close()

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        rejected = await client.post(
            "/files",
            files={
                "file": (
                    "overflow.txt",
                    b"PRD072_RULE_OVERFLOW_100",
                    "text/plain",
                )
            },
            headers=_headers(token, "prd072-dlp-overflow"),
        )
        assert rejected.status_code == 503, rejected.text
        assert rejected.json()["code"] == "file.dlp_unavailable"
        assert rejected.headers["cache-control"] == "no-store, private"
        failed = await client.get(
            "/files?status=failed",
            headers=_headers(token, "prd072-dlp-overflow-list"),
        )
        assert failed.status_code == 200
        assert len(failed.json()["files"]) == 1
        file_id = failed.json()["files"][0]["file_id"]
        assert failed.json()["files"][0]["dlp_scan_status"] == "unavailable"

    connection = connect_database(database_url)
    terminal = connection.execute(
        "SELECT status,dlp_scan_status,failure_error_code FROM uploaded_files WHERE file_id=?",
        (file_id,),
    ).fetchone()
    assert terminal is not None
    assert tuple(terminal) == ("failed", "unavailable", "file.dlp_unavailable")
    assert connection.execute(
        "SELECT COUNT(*) FROM file_events WHERE file_id=?",
        (file_id,),
    ).fetchone()[0] == 2
    connection.close()
    # 规则集超限发生在加密发布之前，失败终态不能残留任何正文 Blob。
    assert not list(storage.rglob("*.bin"))


@pytest.mark.anyio
async def test_file_permission_runs_before_multipart_and_viewer_can_upload(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """证明 SecurityAdmin 写请求不读正文，而 Viewer 的数据库权限允许真实上传。"""

    _database_url, _storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    app = _build_app()
    called = False

    async def forbidden_reader(*_args, **_kwargs):
        """若权限依赖错误地晚于 multipart 调用，则让测试立即失败。"""

        nonlocal called
        called = True
        # 读取器一旦被调用就证明权限依赖顺序失效并已接触未授权正文，必须立即终止该用例。
        raise AssertionError("multipart reader must not run")

    original_reader = file_routes.read_single_file_part
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        security_token = await _login(
            client,
            email="security@prd072.local",
            password=SECURITY_PASSWORD,
        )
        monkeypatch.setattr(file_routes, "read_single_file_part", forbidden_reader)
        denied = await client.post(
            "/files",
            files={"file": ("hidden.txt", b"must-not-read", "text/plain")},
            headers=_headers(security_token, "prd072-security-denied"),
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"
        assert denied.headers["cache-control"] == "no-store, private"
        assert called is False
        monkeypatch.setattr(file_routes, "read_single_file_part", original_reader)

        viewer_token = await _login(
            client,
            email="viewer@prd072.local",
            password=VIEWER_PASSWORD,
        )
        allowed = await client.post(
            "/files",
            files={"file": ("viewer.txt", b"viewer content", "text/plain")},
            headers=_headers(viewer_token, "prd072-viewer-upload"),
        )
        assert allowed.status_code == 201, allowed.text


@pytest.mark.anyio
async def test_file_capacity_rejection_happens_before_multipart_read(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """证明解析容量不足返回 429，且授权后仍不会读取任何正文 chunk。"""

    database_url, _storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    app = _build_app()
    acquire = AsyncMock(side_effect=TimeoutError)
    release = Mock()
    reader = AsyncMock(side_effect=AssertionError("multipart body must remain unread"))
    limiter = SimpleNamespace(acquire=acquire, release=release)
    monkeypatch.setattr(file_routes, "_processing_limiter", lambda _request: limiter)
    monkeypatch.setattr(file_routes, "read_single_file_part", reader)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        rejected = await client.post(
            "/files",
            files={"file": ("busy.txt", b"must-not-read", "text/plain")},
            headers=_headers(token, "prd072-capacity-busy"),
        )
        assert rejected.status_code == 429
        assert rejected.json()["code"] == "file.processing_busy"
        assert rejected.headers["cache-control"] == "no-store, private"

    assert acquire.await_count == 1
    assert reader.await_count == 0
    assert release.call_count == 0
    connection = connect_database(database_url)
    assert connection.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_file_second_blob_publish_failure_cleans_objects_and_commits_failed_state(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """注入第二个 Blob 发布故障，验证首个密文补偿删除与 failed 终态。"""

    database_url, storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    original_publish = file_service.EncryptedLocalBlobStore.publish
    publish_count = 0

    def fail_second_publish(blob_store, staged_blob):
        """允许首个密文发布并拒绝第二个，用于复核跨对象补偿边界。"""

        nonlocal publish_count
        publish_count += 1
        if publish_count == 2:
            # 第二对象失败后必须删除首个已发布密文，错误中不能带出对象键或磁盘路径。
            raise FileContractError("file.storage_unavailable")
        return original_publish(blob_store, staged_blob)

    monkeypatch.setattr(
        file_service.EncryptedLocalBlobStore,
        "publish",
        fail_second_publish,
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        failed = await client.post(
            "/files",
            files={"file": ("publish.txt", b"publish failure", "text/plain")},
            headers=_headers(token, "prd072-second-publish-failed"),
        )
        assert failed.status_code == 503, failed.text
        assert failed.json()["code"] == "file.storage_unavailable"

    connection = connect_database(database_url)
    terminal = connection.execute(
        "SELECT status,failure_error_code FROM uploaded_files"
    ).fetchone()
    assert terminal is not None and tuple(terminal) == (
        "failed",
        "file.storage_unavailable",
    )
    assert connection.execute("SELECT COUNT(*) FROM file_events").fetchone()[0] == 2
    connection.close()
    assert publish_count == 2
    assert not [path for path in storage.rglob("*") if path.is_file()]


@pytest.mark.anyio
async def test_file_terminal_commit_failure_deletes_published_blobs(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """注入 ready 终态提交故障，验证已发布密文补偿和 processing 可观测性。"""

    database_url, storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    original_commit = file_service._commit_terminal

    def fail_ready_commit(config, context, processing, spec):
        """仅拒绝 ready 终态事务，保留其他失败终态实现供测试精确定位。"""

        if spec.status == "ready":
            # 模拟 Blob 已发布但数据库写锁失败，调用方必须先补偿删除再返回固定不可用错误。
            raise file_service.ApiError("file.storage_unavailable")
        return original_commit(config, context, processing, spec)

    monkeypatch.setattr(file_service, "_commit_terminal", fail_ready_commit)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        failed = await client.post(
            "/files",
            files={"file": ("commit.txt", b"commit failure", "text/plain")},
            headers=_headers(token, "prd072-terminal-commit-failed"),
        )
        assert failed.status_code == 503, failed.text
        assert failed.json()["code"] == "file.storage_unavailable"

    connection = connect_database(database_url)
    owner = connection.execute("SELECT status FROM uploaded_files").fetchone()
    assert owner is not None and owner["status"] == "processing"
    assert connection.execute("SELECT COUNT(*) FROM file_extracts").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM file_events").fetchone()[0] == 1
    connection.close()
    assert not [path for path in storage.rglob("*") if path.is_file()]


@pytest.mark.anyio
async def test_file_cursor_binds_tenant_filter_and_signature(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证分页游标只可在签发 tenant、筛选和未篡改意图下继续使用。"""

    _database_url, _storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        for suffix in ("a", "b"):
            created = await client.post(
                "/files",
                files={"file": (f"cursor-{suffix}.txt", suffix.encode(), "text/plain")},
                headers=_headers(token, f"prd072-cursor-create-{suffix}"),
            )
            assert created.status_code == 201, created.text
        first = await client.get(
            "/files",
            params={"limit": "1"},
            headers=_headers(token, "prd072-cursor-first"),
        )
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        assert isinstance(cursor, str) and cursor
        second = await client.get(
            "/files",
            params={"limit": "1", "cursor": cursor},
            headers=_headers(token, "prd072-cursor-second"),
        )
        assert second.status_code == 200
        assert second.json()["files"][0]["file_id"] != first.json()["files"][0]["file_id"]

        changed_filter = await client.get(
            "/files",
            params={"status": "ready", "limit": "1", "cursor": cursor},
            headers=_headers(token, "prd072-cursor-filter-drift"),
        )
        tampered_cursor = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        tampered = await client.get(
            "/files",
            params={"limit": "1", "cursor": tampered_cursor},
            headers=_headers(token, "prd072-cursor-tampered"),
        )
        other_token = await _login(
            client,
            tenant_slug=OTHER_TENANT_SLUG,
            email="admin@prd072-other.local",
            password=OTHER_PASSWORD,
        )
        cross_tenant = await client.get(
            "/files",
            params={"limit": "1", "cursor": cursor},
            headers=_headers(other_token, "prd072-cursor-cross-tenant"),
        )
        for rejected in (changed_filter, tampered, cross_tenant):
            assert rejected.status_code == 422
            assert rejected.json()["code"] == "infra.validation_failed"
            assert rejected.headers["cache-control"] == "no-store, private"


@pytest.mark.anyio
async def test_file_webhook_and_logs_exclude_filename_content_and_storage_keys(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证成功链路的 Webhook 与结构化日志不包含文件名、正文和存储定位。"""

    _database_url, _storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    captured_events: list[object] = []

    def capture_dispatch(_config, events, *, request_id, trace_id):
        """在调用栈内截获安全事件，不执行网络派发或写出任何测试正文。"""

        assert request_id == "prd072-privacy-upload"
        assert trace_id
        captured_events.extend(events)

    monkeypatch.setattr(file_routes, "dispatch_file_events_safely", capture_dispatch)
    caplog.set_level(logging.INFO, logger="metahub.control.files")
    app = _build_app()
    filename_canary = "PRD072_PRIVATE_FILENAME.txt"
    content_canary = "PRD072_PRIVATE_BODY_CONTENT"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        created = await client.post(
            "/files",
            files={"file": (filename_canary, content_canary.encode(), "text/plain")},
            headers=_headers(token, "prd072-privacy-upload"),
        )
        assert created.status_code == 201, created.text

    assert len(captured_events) == 2
    event_text = json.dumps(
        [event.payload for event in captured_events],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert filename_canary not in event_text
    assert content_canary not in event_text
    assert all(
        forbidden not in event_text
        for forbidden in (
            "original_name",
            "content_fingerprint",
            "text_fingerprint",
            "object_key",
            "pattern",
        )
    )
    assert filename_canary not in caplog.text
    assert content_canary not in caplog.text


@pytest.mark.anyio
async def test_file_declared_and_actual_size_limits_reject_before_owner(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """覆盖声明 envelope 超限与伪造较小长度后的真实 file part 超限。"""

    database_url, _storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    maximum = 1_048_576
    monkeypatch.setenv("METAHUB_FILE_MAX_UPLOAD_BYTES", str(maximum))
    app = _build_app()
    small_body, content_type = _multipart_payload(b"small")
    large_body, _ = _multipart_payload(b"x" * (maximum + 1))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        declared = await client.post(
            "/files",
            content=small_body,
            headers={
                **_headers(token, "prd072-declared-too-large"),
                "Content-Type": content_type,
                # 声明值高于文件上限和 64 KiB envelope 预算，路由不得开始消费正文。
                "Content-Length": str(maximum + 64 * 1024 + 1),
            },
        )
        actual = await client.post(
            "/files",
            content=large_body,
            headers={
                **_headers(token, "prd072-actual-too-large"),
                "Content-Type": content_type,
            },
        )
        assert declared.status_code == actual.status_code == 413
        assert declared.json()["code"] == actual.json()["code"] == "file.too_large"
        assert declared.headers["cache-control"] == "no-store, private"
        assert actual.headers["cache-control"] == "no-store, private"

    connection = connect_database(database_url)
    assert connection.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_file_transaction_permission_recheck_closes_revocation_race(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """模拟路由鉴权后、processing 写锁内撤销 file.write 的竞态。"""

    database_url, storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    app = _build_app()

    def deny_locked_write(
        _connection,
        _context,
        permission_code,
        *,
        unavailable_error_code,
    ):
        """在事务权限复核点模拟已撤权角色，禁止沿用路由阶段旧授权。"""

        assert permission_code == "file.write"
        assert unavailable_error_code == "file.storage_unavailable"
        # 事务内必须以实时权限为准，模拟撤权后固定关闭为后端权限错误。
        raise file_service.ApiError("auth.permission_denied")

    monkeypatch.setattr(
        file_service,
        "require_transaction_permission",
        deny_locked_write,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        denied = await client.post(
            "/files",
            files={"file": ("revoked.txt", b"revoked content", "text/plain")},
            headers=_headers(token, "prd072-transaction-revoked"),
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"

    connection = connect_database(database_url)
    assert connection.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM file_events").fetchone()[0] == 0
    connection.close()
    assert not [path for path in storage.rglob("*") if path.is_file()]


@pytest.mark.anyio
async def test_file_multipart_and_format_errors_create_no_owner(
    prd072_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """覆盖额外字段、旧 .doc 和 MIME/扩展不一致，并证明门禁前零业务行。"""

    database_url, _storage = _clone_environment(prd072_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        extra = await client.post(
            "/files",
            files={"file": ("valid.txt", b"text", "text/plain")},
            data={"tenant_id": "forged"},
            headers=_headers(token, "prd072-extra-part"),
        )
        old_word = await client.post(
            "/files",
            files={"file": ("legacy.doc", b"legacy", "application/msword")},
            headers=_headers(token, "prd072-old-word"),
        )
        mismatched = await client.post(
            "/files",
            files={"file": ("fake.pdf", b"not-pdf", "application/pdf")},
            headers=_headers(token, "prd072-magic-mismatch"),
        )
        assert extra.status_code == 400
        assert extra.json()["code"] == "file.multipart_invalid"
        assert old_word.status_code == mismatched.status_code == 415
        assert old_word.json()["code"] == "file.unsupported_format"
        assert mismatched.json()["code"] == "file.unsupported_format"
    connection = connect_database(database_url)
    assert connection.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0] == 0
    connection.close()
