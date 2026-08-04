"""PRD-066 权限 CAS 验收辅助脚本的租户终态测试。

测试只构造位于 pytest ``tmp_path`` 下的最小 SQLite 结构，不访问开发数据库、网络或真实账号。
重点证明默认租户撤权时，其他租户仍有 ``chat.use`` 也不能让终态检查产生假阳性。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

# 动态装载验收辅助脚本时使用独立模块名，避免执行 main 入口或污染其他测试模块。
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = (
    REPOSITORY_ROOT
    / ".artifacts"
    / "prd066-browser-iab-20260802b"
    / "permission_cas.py"
)
MODULE_SPEC = importlib.util.spec_from_file_location("prd066_permission_cas", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 被测权限辅助模块只暴露函数供本文件调用，不执行其命令行入口。
PERMISSION_CAS = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = PERMISSION_CAS
MODULE_SPEC.loader.exec_module(PERMISSION_CAS)


def _create_status_database(database_path: Path) -> None:
    """创建终态查询所需的双租户最小结构，并只授权非默认租户。"""

    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE chat_sessions(status TEXT NOT NULL);
            CREATE TABLE chat_messages(status TEXT NOT NULL);
            CREATE TABLE chat_execution_grants(status TEXT NOT NULL);
            CREATE TABLE tenants(
              tenant_id TEXT PRIMARY KEY,
              slug TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL
            );
            CREATE TABLE roles(
              tenant_id TEXT NOT NULL,
              role_id TEXT NOT NULL,
              code TEXT NOT NULL,
              PRIMARY KEY(tenant_id, role_id)
            );
            CREATE TABLE permissions(
              tenant_id TEXT NOT NULL,
              permission_id TEXT NOT NULL,
              code TEXT NOT NULL,
              PRIMARY KEY(tenant_id, permission_id)
            );
            CREATE TABLE role_permissions(
              tenant_id TEXT NOT NULL,
              role_id TEXT NOT NULL,
              permission_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id, role_id, permission_id)
            );
            INSERT INTO tenants VALUES('tenant_default','default','active');
            INSERT INTO tenants VALUES('tenant_other','other','active');
            INSERT INTO roles VALUES('tenant_default','role_default','developer');
            INSERT INTO roles VALUES('tenant_other','role_other','developer');
            INSERT INTO permissions VALUES('tenant_default','perm_default','chat.use');
            INSERT INTO permissions VALUES('tenant_other','perm_other','chat.use');
            INSERT INTO role_permissions VALUES('tenant_other','role_other','perm_other');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_status_scopes_developer_chat_permission_to_default_tenant(tmp_path: Path) -> None:
    """验证其他租户授权不能掩盖默认租户撤权，恢复后才返回真。"""

    database_path = tmp_path / "permission-status.db"
    _create_status_database(database_path)
    PERMISSION_CAS.DATABASE_PATH = database_path

    revoked_status = PERMISSION_CAS.status()
    assert revoked_status == {
        "active_sessions": 0,
        "in_flight_messages": 0,
        "issued_grants": 0,
        "foreign_key_violations": 0,
        "developer_chat_permission_present": False,
    }

    connection = sqlite3.connect(database_path)
    try:
        # 只有默认租户对应角色获得权限后，最终验收才允许报告恢复成功。
        connection.execute(
            "INSERT INTO role_permissions VALUES('tenant_default','role_default','perm_default')"
        )
        connection.commit()
    finally:
        connection.close()

    restored_status = PERMISSION_CAS.status()
    assert restored_status["developer_chat_permission_present"] is True
