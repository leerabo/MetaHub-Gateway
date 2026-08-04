"""PRD-070 会话风险 HMAC previous 版本移除前只读 verifier。

运维必须在 current/previous 两把密钥仍配置且 Gateway 已停止新写入时运行本模块。验证器同时
检查 Redis score/pending/turn claim、数据库 active/frozen score 和未过期人工动作摘要；
任一引用或未知版本都阻断删 key。报告只包含计数，不包含 tenant、Virtual Key、Session、
Redis key、HMAC digest、数据库 URL、Redis URL 或 Secret。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Protocol

from metahub_shared.db import DbError, connect_database

from .models import SessionRiskStoreUnavailable
from .store import SessionRiskRedisHmacReferenceReport


class SessionRiskHmacReferenceInspector(Protocol):
    """定义 verifier 所需的最小 Redis 只读能力，便于隔离故障测试。"""

    async def inspect_hmac_rotation_references(
        self,
        *,
        current_version: int,
        removing_version: int,
    ) -> SessionRiskRedisHmacReferenceReport:
        """返回 Redis 中目标版本、未知版本和旧 claim 的安全计数。"""


class SessionRiskHmacRotationVerifierUnavailable(RuntimeError):
    """表示 verifier 无法完整证明引用归零，异常只携带固定文本。"""

    def __init__(self) -> None:
        """创建不包含连接、路径、SQL 或内部状态的固定维护错误。"""

        super().__init__("session risk hmac rotation verifier unavailable")


@dataclass(frozen=True, slots=True)
class SessionRiskHmacRotationReport:
    """保存 previous 版本移除检查的全局安全计数。

    版本密钥是部署级配置，因此数据库检查必须覆盖全部租户，而不是接受调用方 tenant_id。
    各字段均为不可逆聚合数，可写入维护证据，但不能据此绕过先停 Gateway 的操作前提。
    """

    current_version: int
    removing_version: int
    active_score_references: int
    unexpired_action_digest_references: int
    database_unknown_version_references: int
    redis: SessionRiskRedisHmacReferenceReport

    @property
    def ready(self) -> bool:
        """仅当数据库和 Redis 对目标/未知版本均无引用时返回真。"""

        return (
            self.active_score_references == 0
            and self.unexpired_action_digest_references == 0
            and self.database_unknown_version_references == 0
            and self.redis.ready
        )

    def as_safe_dict(self) -> dict[str, int | str]:
        """生成可落盘 JSON 摘要，不序列化连接信息、owner 或 HMAC digest。"""

        return {
            "schema_version": 1,
            "status": "ready" if self.ready else "blocked",
            "current_version": self.current_version,
            "removing_version": self.removing_version,
            "active_score_references": self.active_score_references,
            "unexpired_action_digest_references": self.unexpired_action_digest_references,
            "database_unknown_version_references": self.database_unknown_version_references,
            "redis_score_references": self.redis.score_references,
            "redis_pending_references": self.redis.pending_references,
            "redis_turn_claim_references": self.redis.turn_claim_references,
            "redis_unknown_version_references": self.redis.unknown_version_references,
            "redis_legacy_turn_claims": self.redis.legacy_turn_claims,
        }


async def verify_session_risk_hmac_removal(
    database_url: str,
    redis_inspector: SessionRiskHmacReferenceInspector,
    *,
    current_version: int,
    removing_version: int,
    now_epoch: int | None = None,
) -> SessionRiskHmacRotationReport:
    """核对 previous HMAC 是否可以从部署配置中移除。

    参数：数据库 URL、只读 Redis inspector、current/previous 版本和可选审计时钟。返回：安全
    聚合报告。关键约束：版本必须严格递增；数据库在同一读事务内查询；Redis 或数据库任一
    故障都抛固定 verifier 错误，不能用部分结果标记 ready。
    """

    _validate_versions(current_version, removing_version)
    checked_at = int(time.time()) if now_epoch is None else now_epoch
    if type(checked_at) is not int or checked_at < 1:
        # 非法审计时钟会改变“24 小时内”语义，不能以默认当前时间掩盖调用错误。
        raise SessionRiskHmacRotationVerifierUnavailable()

    try:
        # Redis 先完成全量 SCAN；操作规程要求 Gateway 已停写，因此随后数据库快照不会出现新引用。
        redis_report = await redis_inspector.inspect_hmac_rotation_references(
            current_version=current_version,
            removing_version=removing_version,
        )
        connection = connect_database(database_url)
        try:
            connection.execute("BEGIN")
            _require_rotation_tables(connection)
            active_score_references = _count_value(
                connection,
                """
                SELECT COUNT(*) AS value
                FROM session_risk_scores
                WHERE hmac_key_version=?
                  AND status IN ('monitoring','high_risk','frozen')
                """,
                (removing_version,),
            )
            unexpired_action_references = _count_value(
                connection,
                """
                SELECT COUNT(*) AS value
                FROM session_risk_actions
                WHERE actor_type='user' AND digest_key_version=?
                  AND unixepoch(idempotency_expires_at)>?
                """,
                (removing_version, checked_at),
            )
            database_unknown_references = _count_value(
                connection,
                """
                SELECT
                  (SELECT COUNT(*) FROM session_risk_scores
                   WHERE status IN ('monitoring','high_risk','frozen')
                     AND hmac_key_version NOT IN (?,?))
                  +
                  (SELECT COUNT(*) FROM session_risk_actions
                   WHERE actor_type='user' AND unixepoch(idempotency_expires_at)>?
                     AND digest_key_version NOT IN (?,?)) AS value
                """,
                (
                    current_version,
                    removing_version,
                    checked_at,
                    current_version,
                    removing_version,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    # 所有底层细节都可能携带路径、Redis 地址或坏字段值，只向维护脚本暴露固定错误。
    except (
        DbError,
        OSError,
        sqlite3.Error,
        SessionRiskStoreUnavailable,
        TypeError,
        ValueError,
    ):
        # SQLite 父目录创建也可能在 connect_database 的 sqlite try 之前触发文件系统错误。
        raise SessionRiskHmacRotationVerifierUnavailable() from None

    # 只有 Redis 和数据库两部分都完整成功后才构造报告，禁止返回部分计数。
    return SessionRiskHmacRotationReport(
        current_version=current_version,
        removing_version=removing_version,
        active_score_references=active_score_references,
        unexpired_action_digest_references=unexpired_action_references,
        database_unknown_version_references=database_unknown_references,
        redis=redis_report,
    )


def _validate_versions(current_version: int, removing_version: int) -> None:
    """拒绝 bool、非正数、相等或倒序版本，保持与启动 keyring 合同一致。"""

    if (
        type(current_version) is not int
        or type(removing_version) is not int
        or not 1 <= removing_version < current_version <= 2_147_483_647
    ):
        # current 必须严格大于待删 previous，避免 verifier 被用于删除当前在写版本。
        raise SessionRiskHmacRotationVerifierUnavailable()


def _require_rotation_tables(connection: sqlite3.Connection) -> None:
    """确认 0062 两张权威表存在，旧库不能被误判为零引用。"""

    rows = connection.execute(
        """
        SELECT name FROM sqlite_schema
        WHERE type='table' AND name IN ('session_risk_scores','session_risk_actions')
        """
    ).fetchall()
    if {str(row["name"]) for row in rows} != {
        "session_risk_scores",
        "session_risk_actions",
    }:
        # 缺表代表尚未应用 0062，不存在的表绝不等价于“引用为零”。
        raise SessionRiskHmacRotationVerifierUnavailable()


def _count_value(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> int:
    """执行固定聚合 SQL，并拒绝缺行、bool、负数或非整数结果。"""

    row = connection.execute(statement, parameters).fetchone()
    value = row["value"] if row is not None else None
    if type(value) is not int or value < 0:
        # 聚合形状漂移时无法证明计数可信，禁止宽松转换字符串或 bool。
        raise SessionRiskHmacRotationVerifierUnavailable()
    return value
