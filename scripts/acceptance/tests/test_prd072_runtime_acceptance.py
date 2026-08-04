"""PRD-072 真实 HTTP 验收器的沙箱内合同测试。

这些用例只验证验收器自身的路径边界、样本生成、清理 SQL 和证据脱敏，
不连接 ``8000/8001/5173``，也不读取开发数据库或正式文件对象目录。
"""

from __future__ import annotations

import importlib.util
import io
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest
from docx import Document
from pypdf import PdfReader


def _load_runtime_module() -> ModuleType:
    """从带连字符的正式路径加载脚本，且不执行脚本 ``main``。"""

    path = Path(__file__).resolve().parents[1] / "prd072-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location("prd072_runtime_acceptance", path)
    if specification is None or specification.loader is None:
        # 正式脚本缺失时不能用测试替身掩盖交付问题，必须立即失败。
        raise AssertionError("unable to load PRD-072 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 模块只加载一次，保证异常类和 dataclass 在所有用例中拥有稳定类型身份。
RUNTIME = _load_runtime_module()


def test_admin_password_is_required_scrubbed_and_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """管理员密码必须显式注入、读取后移除，且不能出现在配置 repr。"""

    environment_name = "METAHUB_PRD072_ADMIN_PASSWORD"
    monkeypatch.delenv(environment_name, raising=False)
    with pytest.raises(RUNTIME.AcceptanceFailure) as missing:
        RUNTIME.RuntimeConfig.from_environment()
    assert missing.value.stage == "config.identity"
    assert missing.value.code == "admin_password_missing"

    unit_password = "unit-only-password-value"
    monkeypatch.setenv(environment_name, unit_password)
    config = RUNTIME.RuntimeConfig.from_environment()
    assert config.admin_password == unit_password
    assert environment_name not in RUNTIME.os.environ
    assert unit_password not in repr(config)


def test_path_and_loopback_guards_fail_closed() -> None:
    """文件只准落在工作区边界，服务 URL 只准使用固定回环端口。"""

    accepted = RUNTIME._workspace_path(
        RUNTIME.ROOT / ".tmp" / "prd072-unit" / "control.log",
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

    assert RUNTIME._loopback_url(
        "http://127.0.0.1:8001", port=8001, stage="unit.url"
    ) == "http://127.0.0.1:8001"
    for value in (
        "https://127.0.0.1:8001",
        "http://example.com:8001",
        "http://user:password@127.0.0.1:8001",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8001/files",
        "http://127.0.0.1:not-a-port",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME._loopback_url(value, port=8001, stage="unit.url")


def test_generated_docx_and_pdf_are_really_extractable() -> None:
    """验收样本必须是标准解析器可读取的真实 DOCX/PDF，而非扩展名伪造。"""

    marker = "PRD072_RUNTIME_SAMPLE"
    document = Document(io.BytesIO(RUNTIME._build_docx(marker)))
    assert marker in "\n".join(paragraph.text for paragraph in document.paragraphs)

    reader = PdfReader(io.BytesIO(RUNTIME._build_pdf(marker)))
    assert len(reader.pages) == 1
    assert marker in (reader.pages[0].extract_text() or "")


def test_temporary_identity_passwords_are_random_and_hidden() -> None:
    """真实临时账号必须逐批使用随机凭据，并从 dataclass repr 与公开常量中消失。"""

    first = RUNTIME.RunSecrets.create()
    second = RUNTIME.RunSecrets.create()
    assert first.security_password != second.security_password
    assert first.other_password != second.other_password
    assert first.security_password != first.other_password
    assert len(first.security_password) >= 32
    assert len(first.other_password) >= 32
    assert first.security_password not in repr(first)
    assert first.other_password not in repr(first)
    assert first.security_password.encode() in first.sensitive_values("admin-unit-password")
    assert first.other_password.encode() in first.sensitive_values("admin-unit-password")
    assert not hasattr(RUNTIME, "SECURITY_PASSWORD")
    assert not hasattr(RUNTIME, "OTHER_PASSWORD")


def test_cleanup_revokes_real_sessions_table(tmp_path: Path) -> None:
    """临时身份清理必须更新生产认证使用的 ``sessions`` 表并提交终态。"""

    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE dlp_rules(dlp_rule_id TEXT PRIMARY KEY,status TEXT,updated_at TEXT);
        CREATE TABLE users(user_id TEXT PRIMARY KEY,status TEXT,updated_at TEXT);
        CREATE TABLE sessions(
          session_id TEXT PRIMARY KEY,user_id TEXT,status TEXT,revoked_at TEXT,updated_at TEXT
        );
        INSERT INTO dlp_rules VALUES('dlp_unit','active','2026-01-01T00:00:00Z');
        INSERT INTO users VALUES('usr_security','active','2026-01-01T00:00:00Z');
        INSERT INTO users VALUES('usr_other','active','2026-01-01T00:00:00Z');
        INSERT INTO sessions
        VALUES('ses_security','usr_security','active',NULL,'2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES('ses_other','usr_other','active',NULL,'2026-01-01T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()
    identities = RUNTIME.RuntimeIdentities(
        default_tenant_id="tenant_default",
        security_user_id="usr_security",
        other_tenant_id="tenant_other",
        other_user_id="usr_other",
        dlp_rule_id="dlp_unit",
    )

    assert RUNTIME._cleanup_runtime_identities(database, identities) == {
        "disabled_rules": 1,
        "disabled_users": 2,
        "revoked_sessions": 2,
    }
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT status FROM dlp_rules").fetchone()[0] == "disabled"
    disabled_users = connection.execute(
        "SELECT COUNT(*) FROM users WHERE status='disabled'"
    ).fetchone()[0]
    revoked_sessions = connection.execute(
        "SELECT COUNT(*) FROM sessions WHERE status='revoked'"
    ).fetchone()[0]
    assert disabled_users == 2
    assert revoked_sessions == 2
    connection.close()


def test_cleanup_rejects_missing_identity_instead_of_reporting_fixed_counts(
    tmp_path: Path,
) -> None:
    """错误资源 ID 必须回滚并失败，不能仍返回一条规则和两个用户的固定成功计数。"""

    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE dlp_rules(dlp_rule_id TEXT PRIMARY KEY,status TEXT,updated_at TEXT);
        CREATE TABLE users(user_id TEXT PRIMARY KEY,status TEXT,updated_at TEXT);
        CREATE TABLE sessions(
          session_id TEXT PRIMARY KEY,user_id TEXT,status TEXT,revoked_at TEXT,updated_at TEXT
        );
        INSERT INTO dlp_rules VALUES('dlp_real','active','2026-01-01T00:00:00Z');
        INSERT INTO users VALUES('usr_real_a','active','2026-01-01T00:00:00Z');
        INSERT INTO users VALUES('usr_real_b','active','2026-01-01T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()
    missing = RUNTIME.RuntimeIdentities(
        default_tenant_id="tenant_default",
        security_user_id="usr_missing_a",
        other_tenant_id="tenant_other",
        other_user_id="usr_missing_b",
        dlp_rule_id="dlp_missing",
    )

    with pytest.raises(RUNTIME.AcceptanceFailure) as error:
        RUNTIME._cleanup_runtime_identities(database, missing)
    assert error.value.stage == "cleanup"
    assert error.value.code == "rule_update_count_invalid"
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT status FROM dlp_rules").fetchone()[0] == "active"
    assert connection.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0] == 2
    connection.close()


def test_run_cleans_identities_when_http_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """身份创建后的 HTTP 异常必须经过 finally 清理，且原固定失败坐标继续向上返回。"""

    database = tmp_path / "runtime.db"
    database.touch()
    storage = tmp_path / "storage"
    storage.mkdir()
    config = RUNTIME.RuntimeConfig(
        database=database,
        storage=storage,
        control_log=tmp_path / "control.log",
        gateway_log=tmp_path / "gateway.log",
        console_log=tmp_path / "console.log",
        artifact_root=tmp_path / "artifacts",
        control_url=RUNTIME.CONTROL_URL,
        gateway_url=RUNTIME.GATEWAY_URL,
        console_url=RUNTIME.CONSOLE_URL,
        admin_password="unit-admin-password",
    )
    identities = RUNTIME.RuntimeIdentities(
        default_tenant_id="tenant_default",
        security_user_id="usr_security",
        other_tenant_id="tenant_other",
        other_user_id="usr_other",
        dlp_rule_id="dlp_unit",
    )
    cleanup_calls = []
    monkeypatch.setattr(RUNTIME.RuntimeConfig, "from_environment", staticmethod(lambda: config))
    monkeypatch.setattr(RUNTIME, "_prepare_identities", lambda *_args: identities)

    def fail_http(*_args):
        """模拟身份已创建后的固定 HTTP 失败，不携带正文或凭据。"""

        raise RUNTIME.AcceptanceFailure("http.injected", "forced_failure")

    def record_cleanup(path, actual_identities):
        """记录 finally 清理调用，并返回不含身份标识的安全计数。"""

        cleanup_calls.append((path, actual_identities))
        # 测试替身只表达清理已执行；真实函数另由 SQLite 终态用例覆盖。
        return {"disabled_rules": 1, "disabled_users": 2, "revoked_sessions": 0}

    monkeypatch.setattr(RUNTIME, "_verify_http_boundaries", fail_http)
    monkeypatch.setattr(RUNTIME, "_cleanup_runtime_identities", record_cleanup)

    with pytest.raises(RUNTIME.AcceptanceFailure) as error:
        RUNTIME.run()
    assert error.value.stage == "http.injected"
    assert error.value.code == "forced_failure"
    assert cleanup_calls == [(database, identities)]


def test_evidence_guard_rejects_sensitive_keys_and_values() -> None:
    """正式证据不得包含禁用字段名，也不得递归携带本轮敏感明文。"""

    secret = b"prd072-secret-marker"
    RUNTIME._assert_safe_evidence({"status": "passed", "counts": [1, 2]}, (secret,))
    with pytest.raises(RUNTIME.AcceptanceFailure) as key_error:
        RUNTIME._assert_safe_evidence({"filename": "redacted"}, (secret,))
    assert key_error.value.code == "forbidden_key"
    with pytest.raises(RUNTIME.AcceptanceFailure) as value_error:
        RUNTIME._assert_safe_evidence({"message": secret.decode("ascii")}, (secret,))
    assert value_error.value.code == "secret_value"


def test_log_scan_covers_utf8_and_both_utf16_byte_orders(tmp_path: Path) -> None:
    """日志 canary 必须覆盖 PowerShell UTF-16 重定向，不能把编码差异当成零命中。"""

    gateway_log = tmp_path / "gateway.log"
    control_log = tmp_path / "control.log"
    console_log = tmp_path / "console.log"
    gateway_log.write_bytes("普通网关日志".encode())
    control_log.write_bytes("普通控制面日志".encode("utf-16-le"))
    console_log.write_bytes("普通控制台日志".encode("utf-16-be"))
    config = RUNTIME.RuntimeConfig(
        database=tmp_path / "runtime.db",
        storage=tmp_path / "storage",
        control_log=control_log,
        gateway_log=gateway_log,
        console_log=console_log,
        artifact_root=tmp_path / "artifacts",
        control_url=RUNTIME.CONTROL_URL,
        gateway_url=RUNTIME.GATEWAY_URL,
        console_url=RUNTIME.CONSOLE_URL,
    )
    secret = "PRD072-中文-Session-Token".encode()

    assert RUNTIME._scan_logs(config, (secret,)) == {
        "files_scanned": 3,
        "sensitive_matches": 0,
    }
    control_log.write_bytes(
        "prefix PRD072-中文-Session-Token suffix".encode("utf-16-le")
    )
    with pytest.raises(RUNTIME.AcceptanceFailure) as error:
        RUNTIME._scan_logs(config, (secret,))
    assert error.value.stage == "logs"
    assert error.value.code == "sensitive_value_found"
