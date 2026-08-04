"""PRD-070 HMAC 轮换 verifier CLI 的安全输出与失败收口测试。

测试不连接数据库或 Redis，只验证正式脚本使用启动配置、稳定退出码和不含 URL/Secret 的 JSON。
核心 Redis/SQLite 行为由 Gateway 专项与真实回环 RESP 测试覆盖。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from metahub_gateway.session_risk import (
    SessionRiskHmacRotationReport,
    SessionRiskRedisHmacReferenceReport,
    SessionRiskStoreUnavailable,
)


def _load_verifier_module() -> ModuleType:
    """从正式带连字符脚本加载唯一模块实例，避免测试复制 CLI 逻辑。"""

    path = Path(__file__).resolve().parents[2] / "preflight" / "prd070-hmac-rotation-verifier.py"
    specification = importlib.util.spec_from_file_location("prd070_hmac_rotation_verifier", path)
    if specification is None or specification.loader is None:
        # 正式脚本无法加载时不能用复制逻辑替代，否则 CLI 可能未经测试。
        raise AssertionError("unable to load PRD-070 HMAC rotation verifier")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 全文件复用同一正式模块身份，避免 monkeypatch 分别作用到不同实例。
VERIFIER = _load_verifier_module()


def _config(*, previous: bool = True) -> SimpleNamespace:
    """构造只含 CLI 必需字段的合成配置，Secret 不会进入断言或输出。"""

    return SimpleNamespace(
        session_risk_enabled=True,
        session_risk_hmac_version=2,
        session_risk_previous_hmac_version=1 if previous else None,
        session_risk_previous_hmac_secret="p" * 32 if previous else None,
        redis_url="redis://synthetic-secret@127.0.0.1:6379/0",
        database_url="sqlite:///synthetic-sensitive-path.db",
    )


def test_cli_run_prints_only_safe_blocked_counts(
    monkeypatch,
    capsys,
) -> None:
    """引用未归零时返回 blocked 退出码，stdout 不泄露连接、Secret 或摘要。"""

    class _Store:
        """记录构造但不连接网络的最小生产 Store 替身。"""

        def __init__(self, redis_url: str) -> None:
            """确认 CLI 使用配置 URL；实例不保存或输出该值。"""

            assert redis_url.startswith("redis://")

    async def _verify(*args, **kwargs) -> SessionRiskHmacRotationReport:
        """返回一条 previous score 引用，模拟完整 verifier 的安全聚合结果。"""

        assert args[0].startswith("sqlite:///")
        assert kwargs == {"current_version": 2, "removing_version": 1}
        # 返回目标版本 score 引用，证明 CLI 会输出 blocked 而非误判 ready。
        return SessionRiskHmacRotationReport(
            current_version=2,
            removing_version=1,
            active_score_references=1,
            unexpired_action_digest_references=0,
            database_unknown_version_references=0,
            redis=SessionRiskRedisHmacReferenceReport(0, 0, 0, 0, 0),
        )

    monkeypatch.setattr(VERIFIER, "load_app_config", lambda _service: _config())
    monkeypatch.setattr(VERIFIER, "RedisSessionRiskStore", _Store)
    monkeypatch.setattr(VERIFIER, "verify_session_risk_hmac_removal", _verify)

    exit_code = asyncio.run(VERIFIER._run())
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert exit_code == VERIFIER.EXIT_BLOCKED
    assert payload["status"] == "blocked"
    assert payload["active_score_references"] == 1
    serialized = output.out + output.err
    assert "synthetic-secret" not in serialized
    assert "synthetic-sensitive-path" not in serialized
    assert "srh_" not in serialized


def test_cli_missing_previous_configuration_fails_with_fixed_error(
    monkeypatch,
    capsys,
) -> None:
    """previous 已被提前删除时 verifier 固定失败，不能把缺配置解释为 ready。"""

    monkeypatch.setattr(VERIFIER, "load_app_config", lambda _service: _config(previous=False))

    exit_code = VERIFIER.main()
    output = capsys.readouterr()

    assert exit_code == VERIFIER.EXIT_UNAVAILABLE
    assert output.out == ""
    assert output.err.strip() == "session_risk.hmac_rotation_verifier_unavailable"
    assert "synthetic" not in output.err


def test_cli_store_construction_failure_has_fixed_error_and_exit_code(
    monkeypatch,
    capsys,
) -> None:
    """Redis URL 在 Store 构造阶段失败时也必须脱敏收口，不能打印 traceback。"""

    class _RejectedStore:
        """模拟生产 URL parser 在 verifier 调用前拒绝连接配置。"""

        def __init__(self, _redis_url: str) -> None:
            """抛出真实 Store 固定异常，且不保存传入的敏感 URL。"""

            # 构造阶段异常刻意发生在 verify 函数调用前，用于锁定 CLI 最外层收口边界。
            raise SessionRiskStoreUnavailable("session risk store unavailable")

    monkeypatch.setattr(VERIFIER, "load_app_config", lambda _service: _config())
    monkeypatch.setattr(VERIFIER, "RedisSessionRiskStore", _RejectedStore)

    exit_code = VERIFIER.main()
    output = capsys.readouterr()

    assert exit_code == VERIFIER.EXIT_UNAVAILABLE
    assert output.out == ""
    assert output.err.strip() == "session_risk.hmac_rotation_verifier_unavailable"
    assert "Traceback" not in output.err
    assert "synthetic-secret" not in output.err
