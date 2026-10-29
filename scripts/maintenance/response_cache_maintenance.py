"""PRD-076 Response Cache 停服维护的受信核心。

维护命令只从正式 MetaHub 配置读取数据库和版本化 Secret，命令行不接受 URL、Secret、
fingerprint 或租户标识。输出仅包含版本、generation 和聚合计数，避免把可关联摘要写入
终端、日志或验收证据。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

# 维护脚本必须可从仓库直接运行；只加入本仓库源码根，不能依赖调用方偶然配置的路径。
ROOT = Path(__file__).resolve().parents[2]
for _source_root in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
):
    sys.path.insert(0, str(_source_root))

from metahub_gateway.response_cache.failure_spool import (  # noqa: E402
    ResponseCacheFailureSpool,
    ResponseCacheFailureSpoolError,
)
from metahub_shared.db import connect_database, rollback_last_migration  # noqa: E402
from metahub_shared.db.errors import DbError  # noqa: E402
from metahub_shared.response_cache import (  # noqa: E402
    VersionedResponseCacheSecret,
    response_cache_secret_fingerprint,
)
from metahub_shared.runtime.config import (  # noqa: E402
    AppConfig,
    ConfigError,
    load_app_config,
)

# 零指纹只代表迁移后的未初始化状态；维护入口只接受当前最高版本 0069。
ZERO_FINGERPRINT = "0" * 64
EXPECTED_MIGRATION_VERSION = "0069"

# 轮换会在同一 EXCLUSIVE 事务中暂时移除三个阻止维护清理的触发器。固定 schema hash
# 确保 runner 不会在漂移数据库上复制并重新启用未知触发器文本。
MAINTENANCE_TRIGGER_SHA256 = {
    "trg_response_cache_policies_update_prd076": (
        "b3caabeaa29b70663c55ec60779c0bcf2fa056b622e7d828449875b3ca0f99c1"
    ),
    "trg_response_cache_tag_generations_delete_prd076": (
        "d2772daa335a7adc838a7468eb8836ab30cdaa5f4bfa4d95b594959ee5f6202f"
    ),
    "trg_response_cache_key_generations_delete_prd076": (
        "32031282102ec63f9ff1352d4bb972d24d4535b009e568f6e89c050c1d42f50a"
    ),
}


class ResponseCacheMaintenanceError(RuntimeError):
    """只保存固定错误码，禁止底层 SQL、路径或配置值进入公开异常。"""

    def __init__(self, code: str, *, blocked: bool = False) -> None:
        """保存代码内固定错误坐标和是否需要人工解除阻断。"""

        super().__init__(code)
        self.code = code
        self.blocked = blocked


class SafeArgumentParser(argparse.ArgumentParser):
    """参数错误不回显原始 argv，防止误传 Secret 后被 argparse 打到 stderr。"""

    def error(self, _message: str) -> None:
        """拒绝 argparse 原始错误文本，避免回显误传的敏感参数。"""

        # 参数文本可能包含误传的数据库 URL 或 Secret，只能返回固定错误坐标。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_arguments_invalid",
            blocked=True,
        )


@dataclass(frozen=True, slots=True)
class FailureSpoolCounts:
    """failure spool 三态的安全聚合计数，不包含文件名、owner 或宿主路径。"""

    pending: int
    processing: int
    quarantine: int

    @property
    def total(self) -> int:
        """合计三态条目，任一非零值都会阻断维护事务。"""

        return self.pending + self.processing + self.quarantine


@dataclass(frozen=True, slots=True)
class RotationResult:
    """Key 轮换成功后允许公开的版本、围栏和清理计数。"""

    operation: Literal["rotate"]
    status: Literal["completed"]
    hmac_key_version: int
    encryption_key_version: int
    key_state_generation: int
    policies_fenced: int
    entries_purged: int
    fills_purged: int
    tag_generations_purged: int
    key_generations_purged: int
    spool_pending: int
    spool_processing: int
    spool_quarantine: int


@dataclass(frozen=True, slots=True)
class DownResult:
    """记录受信 down runner 可公开的版本和三态归零计数。"""

    operation: Literal["down"]
    status: Literal["completed"]
    rolled_back: str
    spool_pending: int
    spool_processing: int
    spool_quarantine: int


def inspect_failure_spool(config: AppConfig) -> FailureSpoolCounts:
    """在数据库事务前验证本地三态目录，并只返回条目数量。

    processing 也必须为零：它代表仍由旧 worker 持有的未知结算，不能被 pending=0 掩盖。
    任何未知目录项都计入阻断，而不是尝试读取或删除它。
    """

    spool = ResponseCacheFailureSpool(config)
    spool.prepare()

    def count_entries(directory: Path) -> int:
        """只枚举目录项数量，不打开或解析未知恢复记录。"""

        try:
            return sum(1 for _entry in directory.iterdir())
        # 枚举失败时不能把目录路径或底层异常带入维护输出。
        except OSError as exc:
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_inspection_failed"
            ) from exc

    return FailureSpoolCounts(
        pending=count_entries(spool.pending),
        processing=count_entries(spool.processing),
        quarantine=count_entries(spool.quarantine),
    )


def require_empty_failure_spool(config: AppConfig) -> FailureSpoolCounts:
    """证明没有 outcome-unknown 或隔离记录后才允许打开维护事务。"""

    counts = inspect_failure_spool(config)
    if counts.total != 0:
        # 未决或隔离结算可能仍持有预算 owner，轮换和 down 都必须失败关闭。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_spool_not_empty",
            blocked=True,
        )
    return counts


def rotate_response_cache_keys(config: AppConfig) -> RotationResult:
    """在停服 EXCLUSIVE 事务中清空派生缓存并切换 active key-state。

    热路径不能在线替换 Secret。runner 先要求能力全局关闭和 failure spool 归零，再用
    schema hash 冻结维护触发器，推进所有 Policy invalidation generation，清空 entry、
    fill、tag/key generation，最后以 key-state CAS 完成唯一线性化提交。
    """

    _require_disabled(config)
    counts = require_empty_failure_spool(config)
    hmac_key, encryption_key = _configured_cache_keys(config)
    connection = connect_database(config.database_url)
    try:
        # busy_timeout=0 让仍有服务连接时立即失败；显式 --confirm-stopped 不是绕过独占锁。
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN EXCLUSIVE")
        _require_applied_0069(connection)
        state = _load_key_state(connection)
        new_hmac_fingerprint = response_cache_secret_fingerprint(
            hmac_key,
            purpose="hmac",
        )
        new_encryption_fingerprint = response_cache_secret_fingerprint(
            encryption_key,
            purpose="encryption",
        )
        _validate_key_transition(
            state,
            hmac_key=hmac_key,
            hmac_fingerprint=new_hmac_fingerprint,
            encryption_key=encryption_key,
            encryption_fingerprint=new_encryption_fingerprint,
        )
        trigger_sql = _freeze_maintenance_triggers(connection)
        before = _derived_counts(connection)

        for trigger_name in MAINTENANCE_TRIGGER_SHA256:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')

        # 删除 parent 让 SQLite 的 tenant-qualified FK cascade 清理 tag links；随后才能清除
        # 不可变 generation 摘要。所有 DDL/DML 都受同一 EXCLUSIVE rollback 保护。
        connection.execute("DELETE FROM response_cache_entries")
        connection.execute("DELETE FROM response_cache_fill_leases")
        connection.execute("DELETE FROM response_cache_tag_generations")
        connection.execute("DELETE FROM response_cache_key_generations")
        connection.execute(
            """
            UPDATE response_cache_policies
            SET invalidation_generation=invalidation_generation+1
            """
        )
        if int(connection.execute("SELECT changes()").fetchone()[0]) != before["policies"]:
            # Policy 围栏数量必须与事务前快照一致，漏更新会允许旧 fill 回写。
            raise ResponseCacheMaintenanceError(
                "response_cache.maintenance_policy_fence_failed"
            )

        for sql in trigger_sql.values():
            connection.execute(sql)
        _verify_maintenance_triggers(connection)

        connection.execute(
            """
            UPDATE response_cache_key_state
            SET active_hmac_key_version=?,active_encryption_key_version=?,
                hmac_secret_fingerprint=?,encryption_secret_fingerprint=?,
                generation=generation+1,
                updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
            WHERE key_state_id=1 AND key_schema_version=1
              AND active_hmac_key_version=? AND active_encryption_key_version=?
              AND hmac_secret_fingerprint=? AND encryption_secret_fingerprint=?
              AND generation=?
            """,
            (
                hmac_key.version,
                encryption_key.version,
                new_hmac_fingerprint,
                new_encryption_fingerprint,
                int(state["active_hmac_key_version"]),
                int(state["active_encryption_key_version"]),
                str(state["hmac_secret_fingerprint"]),
                str(state["encryption_secret_fingerprint"]),
                int(state["generation"]),
            ),
        )
        if int(connection.execute("SELECT changes()").fetchone()[0]) != 1:
            # 单例 CAS 未命中表示维护期间 key-state 已漂移，不能提交其他清理。
            raise ResponseCacheMaintenanceError(
                "response_cache.maintenance_key_state_conflict",
                blocked=True,
            )
        _verify_empty_derived_state(connection)
        _verify_database_health(connection)
        current = _load_key_state(connection)
        connection.commit()
        return RotationResult(
            operation="rotate",
            status="completed",
            hmac_key_version=int(current["active_hmac_key_version"]),
            encryption_key_version=int(current["active_encryption_key_version"]),
            key_state_generation=int(current["generation"]),
            policies_fenced=before["policies"],
            entries_purged=before["entries"],
            fills_purged=before["fills"],
            tag_generations_purged=before["tag_generations"],
            key_generations_purged=before["key_generations"],
            spool_pending=counts.pending,
            spool_processing=counts.processing,
            spool_quarantine=counts.quarantine,
        )
    # 任一未知错误都必须回滚同事务内的 trigger DDL、派生清理和 key-state 更新。
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def down_response_cache_migration(config: AppConfig) -> DownResult:
    """在外部 spool 门禁通过后调用共享 0069 down，并隐藏底层 SQL 错误。"""

    _require_disabled(config)
    counts = require_empty_failure_spool(config)
    connection = connect_database(config.database_url)
    try:
        _require_applied_0069(
            connection,
            error_code="response_cache.maintenance_down_version_invalid",
        )
    finally:
        connection.close()
    try:
        result = rollback_last_migration(
            config.database_url,
            expected_version=EXPECTED_MIGRATION_VERSION,
        )
    # 维护操作必须先验证停机、配置和数据库状态，任何不确定结果都应失败关闭。
    except DbError as exc:
        if exc.code == "db.migration_version_mismatch":
            # 维护操作必须先验证停机、配置和数据库状态，任何不确定结果都应失败关闭。
            raise ResponseCacheMaintenanceError(
                "response_cache.maintenance_down_version_invalid",
                blocked=True,
            ) from None
        raise
    if result.rolled_back != EXPECTED_MIGRATION_VERSION:
        # 最高版本不是 0069 时不能把其他迁移的 down 冒充 Response Cache 回滚。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_down_version_invalid",
            blocked=True,
        )
    return DownResult(
        operation="down",
        status="completed",
        rolled_back=result.rolled_back,
        spool_pending=counts.pending,
        spool_processing=counts.processing,
        spool_quarantine=counts.quarantine,
    )


def _require_disabled(config: AppConfig) -> None:
    """停服确认之外还要求配置总开关关闭，避免维护命令被在线部署误调用。"""

    if config.response_cache_enabled is not False:
        # 仍启用时旧进程可能在 spool 检查后继续写入，显式停服参数不足以消除竞态。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_disable_required",
            blocked=True,
        )


def _configured_cache_keys(
    config: AppConfig,
) -> tuple[VersionedResponseCacheSecret, VersionedResponseCacheSecret]:
    """读取已通过 AppConfig 隐私门禁的两把 active cache key，缺失时失败关闭。"""

    hmac_key = config.response_cache_hmac_key
    encryption_key = config.response_cache_encryption_key
    if hmac_key is None or encryption_key is None:
        # runner 不允许回退 Auth 或其他业务密钥，也不能用零指纹代替真实配置。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_configuration_unavailable"
        )
    return hmac_key, encryption_key


def _require_applied_0069(
    connection: sqlite3.Connection,
    *,
    error_code: str = "response_cache.maintenance_schema_unavailable",
) -> None:
    """维护只能作用于当前已应用 0069，禁止对未知最高版本做猜测。"""

    row = connection.execute(
        """
        SELECT version,status FROM schema_migrations
        WHERE status='applied' ORDER BY version DESC LIMIT 1
        """
    ).fetchone()
    if row is None or row["version"] != EXPECTED_MIGRATION_VERSION or row["status"] != "applied":
        # 未知最高版本可能含后续业务 schema，维护器绝不能按 0069 表集猜测清理。
        raise ResponseCacheMaintenanceError(
            error_code,
            blocked=True,
        )


def _load_key_state(connection: sqlite3.Connection) -> sqlite3.Row:
    """读取单例 key-state；缺失或多行都视为 schema 完整性失败。"""

    rows = connection.execute(
        """
        SELECT key_state_id,key_schema_version,active_hmac_key_version,
               active_encryption_key_version,hmac_secret_fingerprint,
               encryption_secret_fingerprint,generation
        FROM response_cache_key_state ORDER BY key_state_id
        """
    ).fetchall()
    if len(rows) != 1 or int(rows[0]["key_state_id"]) != 1:
        # 单例缺失或重复说明 owner schema 已损坏，继续轮换会扩大不可恢复状态。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_key_state_invalid"
        )
    return rows[0]


def _validate_key_transition(
    state: sqlite3.Row,
    *,
    hmac_key: VersionedResponseCacheSecret,
    hmac_fingerprint: str,
    encryption_key: VersionedResponseCacheSecret,
    encryption_fingerprint: str,
) -> None:
    """冻结初始化或逐版本轮换，拒绝降级、跳号和同版本换 Secret。"""

    old_hmac = str(state["hmac_secret_fingerprint"])
    old_encryption = str(state["encryption_secret_fingerprint"])
    initializing = old_hmac == ZERO_FINGERPRINT and old_encryption == ZERO_FINGERPRINT
    if (old_hmac == ZERO_FINGERPRINT) != (old_encryption == ZERO_FINGERPRINT):
        # 只初始化一个根域会让缓存身份与密文版本失去同一代际，必须拒绝半状态。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_key_state_invalid"
        )

    def validate_domain(
        old_version: int,
        old_fingerprint: str,
        new_version: int,
        new_fingerprint: str,
    ) -> bool:
        """校验单个根域只能初始化、保持不变或恰好推进一个版本。"""

        unchanged = hmac.compare_digest(old_fingerprint, new_fingerprint)
        if initializing:
            if new_version < old_version:
                # 初始化也不得降低迁移冻结的版本，否则旧进程可能误认更高版本 owner。
                raise ResponseCacheMaintenanceError(
                    "response_cache.maintenance_key_transition_invalid",
                    blocked=True,
                )
            return False
        expected_version = old_version if unchanged else old_version + 1
        if new_version != expected_version:
            # Secret 变化必须版本加一；相同 Secret 则版本保持，禁止跳号或同版换钥。
            raise ResponseCacheMaintenanceError(
                "response_cache.maintenance_key_transition_invalid",
                blocked=True,
            )
        return not unchanged

    hmac_changed = validate_domain(
        int(state["active_hmac_key_version"]),
        old_hmac,
        hmac_key.version,
        hmac_fingerprint,
    )
    encryption_changed = validate_domain(
        int(state["active_encryption_key_version"]),
        old_encryption,
        encryption_key.version,
        encryption_fingerprint,
    )
    if not initializing and not (hmac_changed or encryption_changed):
        # 已初始化 key-state 的无变化运行会无意义清空业务缓存，必须显式拒绝。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_key_transition_noop",
            blocked=True,
        )


def _freeze_maintenance_triggers(connection: sqlite3.Connection) -> dict[str, str]:
    """读取并验证将暂时移除的 exact 0069 trigger 文本。"""

    placeholders = ",".join("?" for _ in MAINTENANCE_TRIGGER_SHA256)
    rows = connection.execute(
        f"SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND name IN ({placeholders})",
        tuple(MAINTENANCE_TRIGGER_SHA256),
    ).fetchall()
    result = {str(row["name"]): str(row["sql"]) for row in rows if row["sql"]}
    if set(result) != set(MAINTENANCE_TRIGGER_SHA256):
        # 缺失任一维护 trigger 都说明 schema 不是 runner 审核过的 0069 版本。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_schema_drift",
            blocked=True,
        )
    for name, expected in MAINTENANCE_TRIGGER_SHA256.items():
        actual = hashlib.sha256(result[name].encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual, expected):
            # 同名 trigger 文本漂移也必须拒绝，不能在事务中复制并重新启用未知逻辑。
            raise ResponseCacheMaintenanceError(
                "response_cache.maintenance_schema_drift",
                blocked=True,
            )
    return result


def _verify_maintenance_triggers(connection: sqlite3.Connection) -> None:
    """重建后再次验证 hash，避免维护成功却遗留弱化 schema。"""

    _freeze_maintenance_triggers(connection)


def _derived_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """在删除前冻结聚合计数，公开报告不包含 tenant 或摘要。"""

    return {
        "policies": int(
            connection.execute("SELECT COUNT(*) FROM response_cache_policies").fetchone()[0]
        ),
        "entries": int(
            connection.execute("SELECT COUNT(*) FROM response_cache_entries").fetchone()[0]
        ),
        "fills": int(
            connection.execute("SELECT COUNT(*) FROM response_cache_fill_leases").fetchone()[0]
        ),
        "tag_generations": int(
            connection.execute(
                "SELECT COUNT(*) FROM response_cache_tag_generations"
            ).fetchone()[0]
        ),
        "key_generations": int(
            connection.execute(
                "SELECT COUNT(*) FROM response_cache_key_generations"
            ).fetchone()[0]
        ),
    }


def _verify_empty_derived_state(connection: sqlite3.Connection) -> None:
    """提交前证明所有使用旧 HMAC/AES 域的派生表已经归零。"""

    counts = _derived_counts(connection)
    derived_names = ("entries", "fills", "tag_generations", "key_generations")
    if any(counts[name] != 0 for name in derived_names):
        # 任一旧派生 owner 残留都会让轮换后的不可达数据长期占用或被错误复用。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_purge_incomplete"
        )


def _verify_database_health(connection: sqlite3.Connection) -> None:
    """在同一事务内执行 FK 与 integrity 门禁，任何异常都回滚 trigger 和业务变化。"""

    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        # 外键违规表示维护删除顺序或历史 owner 已损坏，不能提交新的 key-state。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_foreign_key_invalid"
        )
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if [str(row[0]) for row in integrity] != ["ok"]:
        # integrity 必须只有单一 ok 结果，任何额外诊断都按不可安全提交处理。
        raise ResponseCacheMaintenanceError(
            "response_cache.maintenance_integrity_invalid"
        )


def _parser(operation: Literal["rotate", "down"]) -> SafeArgumentParser:
    """两个入口只接受无敏感值的显式停服确认。"""

    parser = SafeArgumentParser(add_help=True)
    parser.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="confirm all Gateway and Control writers are stopped",
    )
    parser.set_defaults(operation=operation)
    return parser


def run_cli(operation: Literal["rotate", "down"], argv: Sequence[str] | None = None) -> int:
    """执行维护入口，并把所有失败折叠为无路径、无 Secret 的固定 JSON。"""

    try:
        arguments = _parser(operation).parse_args(argv)
        if arguments.confirm_stopped is not True:
            # 无显式停服确认时绝不能依靠瞬时独占锁推断所有服务都已退出。
            raise ResponseCacheMaintenanceError(
                "response_cache.maintenance_stop_confirmation_required",
                blocked=True,
            )
        config = load_app_config("gateway-data-plane")
        result = (
            rotate_response_cache_keys(config)
            if operation == "rotate"
            else down_response_cache_migration(config)
        )
        print(json.dumps(asdict(result), ensure_ascii=True, separators=(",", ":"), sort_keys=True))
        return 0
    # 业务阻断只输出固定 code；不序列化异常对象、配置或数据库上下文。
    except ResponseCacheMaintenanceError as exc:
        code = exc.code
        exit_code = 3 if exc.blocked else 2
    # spool 异常可能含宿主文件信息，统一映射为不可用而不展开原因。
    except ResponseCacheFailureSpoolError:
        code = "response_cache.maintenance_spool_unavailable"
        exit_code = 2
    # 配置异常可能揭示缺失项或 Secret 关系，维护证据只保留固定错误。
    except (ConfigError, ValueError):
        code = "response_cache.maintenance_configuration_unavailable"
        exit_code = 2
    # shared down 的 DbError 带 SQL 诊断，CLI 必须折叠为阻断或数据库不可用。
    except DbError:
        code = (
            "response_cache.maintenance_down_blocked"
            if operation == "down"
            else "response_cache.maintenance_database_unavailable"
        )
        exit_code = 3 if operation == "down" else 2
    # 文件系统和 SQLite 原始消息都可能携带绝对路径，只允许固定失败坐标。
    except (OSError, sqlite3.Error):
        code = "response_cache.maintenance_database_unavailable"
        exit_code = 2
    print(
        json.dumps(
            {"code": code, "operation": operation, "status": "failed"},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return exit_code


__all__ = [
    "DownResult",
    "FailureSpoolCounts",
    "ResponseCacheMaintenanceError",
    "RotationResult",
    "down_response_cache_migration",
    "inspect_failure_spool",
    "require_empty_failure_spool",
    "rotate_response_cache_keys",
    "run_cli",
]
