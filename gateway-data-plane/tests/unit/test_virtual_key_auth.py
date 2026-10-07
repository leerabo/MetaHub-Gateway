"""Gateway Virtual Key 鉴权连接生命周期回归测试。

PRD-049 新增高频 Feedback POST 后，认证成功与失败都必须关闭 SQLite connection；否则
每次模型调用和反馈提交都会积累文件句柄。测试代理真实连接，只观测 close 不接触 Key 日志。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from metahub_gateway.auth import virtual_key as virtual_key_auth
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError

# 固定测试 Key 仅在当前鉴权调用内使用，数据库只保存其哈希和安全前缀。
VIRTUAL_KEY = "mhv_prd049_auth_close_key"


class _TrackingConnection:
    """代理真实 SQLite connection 并记录是否执行 close。

    职责：透明转发鉴权查询、last_used 更新和 commit，同时暴露 ``closed`` 供测试断言。
    边界：代理不读取或记录 SQL 参数与 Virtual Key 明文，close 仍真实释放底层文件句柄。
    """

    def __init__(self, connection) -> None:
        """保存底层连接；``closed`` 初始为 False，供成功/异常路径断言。"""

        self._connection = connection
        self.closed = False

    def __getattr__(self, name: str):
        """把 execute/commit 等数据库操作透明委托给底层真实连接。"""

        return getattr(self._connection, name)

    def close(self) -> None:
        """记录释放动作并关闭底层句柄，保持测试数据库可以立即清理。"""

        self.closed = True
        self._connection.close()


def _sqlite_url(path: Path) -> str:
    """把 pytest 临时路径转换为 shared 数据库层支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def test_virtual_key_auth_closes_connection_on_success_and_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成功恢复与无效 Key 拒绝都必须经过 finally 关闭各自独立连接。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd049-auth-close.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    opened: list[_TrackingConnection] = []

    def tracking_connect(_config):
        """为每次鉴权返回新的可观测代理，模拟生产 `_connect` 的连接所有权。"""

        tracked = _TrackingConnection(connect_database(database_url))
        opened.append(tracked)
        return tracked

    monkeypatch.setattr(virtual_key_auth, "_connect", tracking_connect)
    with pytest.raises(ApiError) as unassigned:
        virtual_key_auth.authenticate_virtual_key(config, f"Bearer {VIRTUAL_KEY}")
    assert unassigned.value.code == "gateway.auth_invalid"
    connection = connect_database(database_url)
    try:
        now = "2026-10-07T00:00:00Z"
        connection.execute(
            "INSERT INTO projects(project_id,tenant_id,project_code,name,status,created_at,updated_at) "
            "VALUES('project_auth_bound',?,'auth-bound','Auth bound','active',?,?)",
            (seed.tenant_id, now, now),
        )
        connection.execute(
            "INSERT INTO applications(application_id,tenant_id,project_id,application_code,name,status,created_at,updated_at) "
            "VALUES('application_auth_bound',?,'project_auth_bound','auth-bound','Auth bound','active',?,?)",
            (seed.tenant_id, now, now),
        )
        connection.execute(
            "UPDATE virtual_keys SET project_id='project_auth_bound', application_id='application_auth_bound' "
            "WHERE tenant_id=?",
            (seed.tenant_id,),
        )
        connection.commit()
    finally:
        connection.close()
    context = virtual_key_auth.authenticate_virtual_key(config, f"Bearer {VIRTUAL_KEY}")
    with pytest.raises(ApiError) as rejected:
        virtual_key_auth.authenticate_virtual_key(config, "Bearer invalid-prd049-key")

    assert context.tenant_id
    assert rejected.value.code == "gateway.auth_invalid"
    assert len(opened) == 3
    assert all(connection.closed for connection in opened)
