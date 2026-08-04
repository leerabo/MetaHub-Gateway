"""PRD-069 控制面敏感意图分类与原子结果持久化服务。

分类器完全本地运行，不访问 Provider、网络或外部模型。输入正文只存在当前同步调用栈；
数据库、Webhook、日志与异常仅接收固定 taxonomy、整数分数、版本和内部 owner ID。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.intent_security import (
    DETECTOR_VERSION,
    RULESET_VERSION,
    IntentClassification,
    classify_intent,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_control.auth import AuthContext, require_transaction_permission
from metahub_control.webhooks.service import dispatch_webhook_event

from .models import IntentClassificationResponse, IntentRiskResultInfo

logger = logging.getLogger("metahub.intent_security")


def classify_manual_intent(
    config: AppConfig,
    context: AuthContext,
    texts: tuple[str, ...],
    *,
    request_id: str,
    trace_id: str,
) -> tuple[IntentClassificationResponse, WebhookEvent | None]:
    """执行一次本地分类并原子保存结果及可选 source event。

    参数：服务配置、可信认证上下文、一次性文本元组和内部链路 ID。返回：不含正文的公开
    响应，以及仅在 detected 时存在的已校验 Webhook 事件。关键约束：分类器异常与结果
    存储异常使用不同错误码；detected 只有 result/event 单事务提交成功后才能返回。
    """

    started = time.perf_counter()
    try:
        classification = classify_intent(texts)
    except Exception:
        # 未知分类异常可能携带正文相关对象，不能记录 exc_info 或异常文本；先机会性保存固定失败终态。
        duration_ms = _duration_ms(started)
        _persist_classifier_failure(
            config,
            context,
            request_id=request_id,
            trace_id=trace_id,
            input_code_points=sum(len(value) for value in texts),
            duration_ms=duration_ms,
        )
        # failed 证据已提交后仍返回分类器阶段 503，不能把失败结果伪装成 clean 或 review。
        raise ApiError("intent_risk.classifier_unavailable") from None

    duration_ms = _duration_ms(started)
    try:
        return _persist_classification(
            config,
            context,
            classification,
            request_id=request_id,
            trace_id=trace_id,
            duration_ms=duration_ms,
        )
    finally:
        # 领域结果本身不含正文；释放调用元组引用，路由还会独立覆盖其字符串局部变量。
        texts = ()


def dispatch_intent_event_safely(
    config: AppConfig,
    event: WebhookEvent,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """在响应提交后机会性投递已持久化的检测事件。

    参数：服务配置、精确校验后的事件与内部链路 ID。返回：无。关键约束：Webhook 失败
    不回滚分类结果或改变 HTTP 响应；日志仅含稳定 ID 和固定错误码，不含 payload/Endpoint。
    """

    try:
        dispatch_webhook_event(
            config,
            event.tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
    except Exception:
        # 网络、签名和数据库异常可能包含 Endpoint 或内部路径，只记录固定失败分类。
        logger.warning(
            "intent_risk.webhook_dispatch_failed",
            extra={
                "event": "intent_risk.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "object_id": event.event_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _persist_classification(
    config: AppConfig,
    context: AuthContext,
    classification: IntentClassification,
    *,
    request_id: str,
    trace_id: str,
    duration_ms: int,
) -> tuple[IntentClassificationResponse, WebhookEvent | None]:
    """在一个写事务内复核权限并写入 manual result 与可选 detected event。"""

    result_id = f"irr_{uuid4().hex}"
    event_id = f"irevt_{uuid4().hex}"
    completed_at = _utcnow()
    completed_at_text = _to_db_time(completed_at)
    detected = classification.detected
    status = "detected" if detected else "clean"
    action = "review" if detected else "allow"
    event = (
        _build_event(
            context,
            classification,
            result_id=result_id,
            event_id=event_id,
            request_id=request_id,
            trace_id=trace_id,
            action=action,
            completed_at=completed_at,
        )
        if detected
        else None
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 路由鉴权后角色可能被撤销；写锁内重读 Session 与矩阵，避免撤权竞态产生结果。
        require_transaction_permission(
            connection,
            context,
            "intent_risk.classify",
            unavailable_error_code="intent_risk.result_store_unavailable",
        )
        _insert_result(
            connection,
            context,
            classification,
            result_id=result_id,
            request_id=request_id,
            trace_id=trace_id,
            status=status,
            action=action,
            duration_ms=duration_ms,
            completed_at=completed_at_text,
        )
        if event is not None:
            _insert_event(
                connection,
                context,
                classification,
                result_id=result_id,
                event_id=event_id,
                request_id=request_id,
                trace_id=trace_id,
                action=action,
                completed_at=completed_at_text,
            )
        connection.commit()
    # 事务内实时权限复核失败必须原样保留统一认证错误，同时释放当前 SQLite 写锁。
    except ApiError:
        connection.rollback()
        raise
    # 复合 owner、状态矩阵或不可变 trigger 拒绝说明结果内部不自洽，不能折叠成普通不可用。
    except sqlite3.IntegrityError:
        # Owner/状态/版本触发器失败说明事实无法自洽，不能降级为一个看似成功的 clean 结果。
        connection.rollback()
        raise ApiError("intent_risk.result_integrity_failed") from None
    # 其他 begin/insert/commit 故障只说明结果存储不可用，底层 SQL 与数据库路径不得外泄。
    except sqlite3.Error:
        connection.rollback()
        # result/event 未能原子提交时返回专用存储错误，不能误报为分类器故障。
        raise ApiError("intent_risk.result_store_unavailable") from None
    finally:
        connection.close()

    response = IntentClassificationResponse(
        intent_risk_result=IntentRiskResultInfo(
            intent_risk_result_id=result_id,
            status=status,
            classification_mode="manual_sync",
            risk_type=classification.risk_type,
            risk_score=classification.risk_score,
            risk_score_scale=classification.risk_score_scale,
            action=action,
            reason_code=classification.reason_code,
            detector_version=classification.detector_version,
            ruleset_version=classification.ruleset_version,
            input_code_points=classification.input_code_points,
            input_truncated=False,
            duration_ms=duration_ms,
            completed_at=completed_at,
        ),
        request_id=request_id,
        trace_id=trace_id,
    )
    return response, event


def _persist_classifier_failure(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
    input_code_points: int,
    duration_ms: int,
) -> None:
    """分类器异常时保存不含正文的 manual failed 终态；存储失败优先暴露存储错误。"""

    connection = _connect(config)
    completed_at = _to_db_time(_utcnow())
    try:
        connection.execute("BEGIN IMMEDIATE")
        require_transaction_permission(
            connection,
            context,
            "intent_risk.classify",
            unavailable_error_code="intent_risk.result_store_unavailable",
        )
        connection.execute(
            """
            INSERT INTO intent_risk_results(
              intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
              actor_type,actor_id,status,classification_mode,risk_type,risk_score,
              risk_score_scale,action,reason_code,detector_version,ruleset_version,
              input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
            ) VALUES(?,?,'manual',?,?,'user',?,'failed','manual_sync','none',0,10000,
                     'none','classifier_failed',?,?,?,0,?,
                     'intent_risk.classifier_failed',?,?)
            """,
            (
                f"irr_{uuid4().hex}",
                context.user.tenant_id,
                request_id,
                trace_id,
                context.user.user_id,
                DETECTOR_VERSION,
                RULESET_VERSION,
                min(20_000, max(0, input_code_points)),
                duration_ms,
                completed_at,
                completed_at,
            ),
        )
        connection.commit()
    # 写锁内权限复核失败应保持认证层稳定错误，并回滚尚未提交的 failed 结果。
    except ApiError:
        connection.rollback()
        raise
    # failed 终态本身不可写时存储故障优先，不能继续返回较轻的分类器不可用诊断。
    except sqlite3.Error:
        connection.rollback()
        # 分类器故障不能掩盖更严重的结果不可写事实，调用方据此区分两个运行阶段。
        raise ApiError("intent_risk.result_store_unavailable") from None
    finally:
        connection.close()


def _insert_result(
    connection: sqlite3.Connection,
    context: AuthContext,
    classification: IntentClassification,
    *,
    result_id: str,
    request_id: str,
    trace_id: str,
    status: str,
    action: str,
    duration_ms: int,
    completed_at: str,
) -> None:
    """写入 manual result 的固定列集合，正文和输入 hash 不进入 SQL 参数。"""

    connection.execute(
        """
        INSERT INTO intent_risk_results(
          intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
          actor_type,actor_id,status,classification_mode,risk_type,risk_score,
          risk_score_scale,action,reason_code,detector_version,ruleset_version,
          input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
        ) VALUES(?,?,'manual',?,?,'user',?,?, 'manual_sync',?,?,?,?,?,?,?, ?,0,?,NULL,?,?)
        """,
        (
            result_id,
            context.user.tenant_id,
            request_id,
            trace_id,
            context.user.user_id,
            status,
            classification.risk_type,
            classification.risk_score,
            classification.risk_score_scale,
            action,
            classification.reason_code,
            classification.detector_version,
            classification.ruleset_version,
            classification.input_code_points,
            duration_ms,
            completed_at,
            completed_at,
        ),
    )


def _insert_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    classification: IntentClassification,
    *,
    result_id: str,
    event_id: str,
    request_id: str,
    trace_id: str,
    action: str,
    completed_at: str,
) -> None:
    """写入与 detected result 逐字段绑定的 source event，供提交后投递。"""

    connection.execute(
        """
        INSERT INTO intent_risk_events(
          intent_risk_event_id,tenant_id,intent_risk_result_id,event_type,
          source_request_id,source_trace_id,source_kind,classification_mode,
          risk_type,risk_score,risk_score_scale,action,reason_code,detector_version,
          ruleset_version,input_truncated,actor_type,actor_id,created_at
        ) VALUES(?,?,?,'security.intent_detected',?,?,'manual','manual_sync',?,?,?,?,?,?,?,
                 0,'user',?,?)
        """,
        (
            event_id,
            context.user.tenant_id,
            result_id,
            request_id,
            trace_id,
            classification.risk_type,
            classification.risk_score,
            classification.risk_score_scale,
            action,
            classification.reason_code,
            classification.detector_version,
            classification.ruleset_version,
            context.user.user_id,
            completed_at,
        ),
    )


def _build_event(
    context: AuthContext,
    classification: IntentClassification,
    *,
    result_id: str,
    event_id: str,
    request_id: str,
    trace_id: str,
    action: str,
    completed_at: datetime,
) -> WebhookEvent:
    """在入库前构造并执行 Shared exact-key/envelope 校验。

    参数：可信用户上下文、纯分类摘要和持久 source 身份。返回：不含正文的冻结事件。
    关键约束：构造失败必须阻断 detected 事务，不能保存一条未来无法安全投递的事件事实。
    """

    try:
        return build_webhook_event(
            tenant_id=context.user.tenant_id,
            event_type="security.intent_detected",
            event_id=event_id,
            timestamp=completed_at,
            actor_id=context.user.user_id,
            object_id=result_id,
            trace_id=trace_id,
            source_request_id=request_id,
            payload={
                "intent_risk_result_id": result_id,
                "source_request_id": request_id,
                "source_kind": "manual",
                "classification_mode": "manual_sync",
                "risk_type": classification.risk_type,
                "risk_score": classification.risk_score,
                "risk_score_scale": classification.risk_score_scale,
                "action": action,
                "reason_code": classification.reason_code,
                "detector_version": classification.detector_version,
                "ruleset_version": classification.ruleset_version,
                "input_truncated": False,
                "completed_at": _to_db_time(completed_at),
            },
        )
    # Shared exact-key 或信封 owner 拒绝说明当前领域映射损坏，不能继续写入 source event。
    except (TypeError, ValueError):
        # Shared 合同与领域结果不一致属于发布完整性故障，不能存下无法投递的 detected 事实。
        raise ApiError("intent_risk.result_integrity_failed") from None


def _connect(config: AppConfig) -> sqlite3.Connection:
    """打开启用外键的短连接，并把配置/路径失败折叠成结果存储错误。"""

    try:
        return connect_database(config.database_url)
    # 连接错误可能携带数据库路径或驱动细节，控制面只允许公开固定结果存储阶段错误。
    except DbError:
        # 连接尚未建立时没有可回滚事务，直接切断异常链防止基础设施细节进入响应。
        raise ApiError("intent_risk.result_store_unavailable") from None


def _duration_ms(started: float) -> int:
    """把单次本地分类耗时冻结为数据库允许的非负有界整数毫秒。"""

    return min(86_400_000, max(0, int((time.perf_counter() - started) * 1000)))


def _utcnow() -> datetime:
    """返回秒级 UTC 时间，使 result、source event 与 Webhook 时间逐字一致。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把 aware datetime 转为 SQLite 与 Webhook 共用的 canonical UTC 秒文本。

    参数：服务端生成的 aware 时间。返回：以 ``Z`` 结尾的秒级文本。关键约束：result、
    source event 和 Webhook 必须逐字共用该值，避免同一事实因精度或时区不同无法关联。
    """

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["classify_manual_intent", "dispatch_intent_event_safely"]
