"""中文注释门禁对已应用迁移的哈希绑定豁免专项测试。

测试只读取正式冻结迁移，并在 pytest 独占临时目录构造漂移副本；不会修改迁移或数据库记录。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# 门禁脚本文件名含连字符，测试通过标准 importlib 加载同一正式实现，不复制判断逻辑。
ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = ROOT / "scripts" / "ci" / "check-comments.py"
FROZEN_MIGRATION_CASES = (
    (ROOT / "infra" / "migrations" / "0051_user_invitation_join_flow.sql", 6, False),
    (ROOT / "infra" / "migrations" / "0067_batch_scheduler_worker.sql", 14, True),
    (ROOT / "infra" / "migrations" / "0067_batch_scheduler_worker.down.sql", 16, True),
)


def _load_checker() -> ModuleType:
    """加载正式注释门禁模块，供测试直接调用哈希与 SQL 检查函数。"""

    specification = importlib.util.spec_from_file_location("metahub_check_comments", CHECKER_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("migration_path", "expected_count", "_needs_header_exemption"),
    FROZEN_MIGRATION_CASES,
)
def test_frozen_exact_checksum_keeps_narrow_comment_exemptions(
    migration_path: Path,
    expected_count: int,
    _needs_header_exemption: bool,
) -> None:
    """已应用迁移精确匹配登记哈希时，只允许清单内冻结操作沿用历史注释基线。"""

    checker = _load_checker()
    exemptions = checker.frozen_sql_comment_exemptions(migration_path)
    assert len(exemptions) == expected_count
    assert checker.check_file_has_chinese_context(migration_path) == []
    assert checker.check_sql_file(migration_path) == []


@pytest.mark.parametrize(
    ("migration_path", "_expected_count", "needs_header_exemption"),
    FROZEN_MIGRATION_CASES,
)
def test_frozen_migration_text_drift_revokes_exemptions(
    migration_path: Path,
    _expected_count: int,
    needs_header_exemption: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结迁移出现任意文本漂移时立即撤销豁免，防止文件名规则掩盖 checksum 变化。"""

    checker = _load_checker()
    candidate = tmp_path / migration_path.name
    candidate.write_text(
        migration_path.read_text(encoding="utf-8") + "\n-- test-only checksum drift\n",
        encoding="utf-8",
    )
    expected_checksum, operations = checker.FROZEN_SQL_COMMENT_EXEMPTIONS[
        migration_path.relative_to(ROOT).as_posix()
    ]
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(
        checker,
        "FROZEN_SQL_COMMENT_EXEMPTIONS",
        {candidate.name: (expected_checksum, operations)},
    )
    assert checker.frozen_sql_comment_exemptions(candidate) == frozenset()
    if needs_header_exemption:
        assert checker.check_file_has_chinese_context(candidate) != []
    else:
        assert checker.check_file_has_chinese_context(candidate) == []
