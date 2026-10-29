"""Gateway 轻量敏感意图闸门、结果持久化与异步任务执行服务。

同步强规则位于 DLP 之后、Rate Limit 之前并采用失败关闭；完整轻量分类只在容量有界的
进程内队列旁路执行并采用失败开放。两条路径都只持久化固定安全元数据，不调用 Control、
Provider 或外部模型，也不把 Prompt、命中片段和异常正文写入日志、数据库或 Webhook。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.intent_security import (
    DETECTOR_VERSION,
    RULESET_VERSION,
    IntentClassification,
    classify_intent,
    classify_strong_intent,
)
from metahub_shared.session_risk import (
    SessionIntentClassification,
    classify_session_intent,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_gateway.dlp import extract_session_user_texts, extract_user_texts
from metahub_gateway.events import enqueue_runtime_event
from metahub_gateway.usage.ledger import RequestLogContext

from .models import (
    IntentBlockedError,
    IntentClassificationJob,
    IntentClassifierUnavailableError,
    IntentResultStoreUnavailableError,
    IntentRiskDecision,
    IntentScheduleStatus,
)

logger = logging.getLogger("metahub.gateway.intent_security")


class _ResultStoreUnavailable(Exception):
    """标记内部结果事务失败，异常文本固定且不携带数据库或输入信息。"""


def enforce_intent_guard(
    config: AppConfig,
    app_state: object,
    context: RequestLogContext,
    payload: dict[str, Any],
    *,
    request_kind: str,
) -> RequestLogContext:
    """在 Provider 前执行强规则，并把未命中请求机会性投给异步队列。

    参数：运行配置、应用状态、可信 RequestLog 上下文、已应用 Policy mask 的正文和请求类型。
    返回：带 queued/skipped 摘要的新上下文。关键约束：仅扫描 Chat 的 ``role=user`` 文本；
    强分类或同步持久化异常必须失败关闭，异步队列缺失或满载必须失败开放。
    """

    if request_kind != "chat":
        return context

    indexed_texts, input_truncated = extract_user_texts(payload)
    texts = tuple(text for _, text in indexed_texts)
    result_id = f"irr_{uuid4().hex}"
    started = time.perf_counter()
    try:
        try:
            classification = classify_strong_intent(texts)
        except Exception as exc:
            # 分类异常对象可能间接引用正文，只记录异常类型，并先尝试保存固定 failed 终态。
            duration_ms = _duration_ms(started)
            try:
                _persist_classifier_failure(
                    config,
                    context,
                    result_id=result_id,
                    classification_mode="sync_strong",
                    input_code_points=sum(len(text) for text in texts),
                    input_truncated=input_truncated,
                    duration_ms=duration_ms,
                )
            except _ResultStoreUnavailable as store_exc:
                decision = _failed_closed_decision(
                    result_id=None,
                    reason_code="result_store_unavailable",
                    input_truncated=input_truncated,
                )
                _log_sync_failure(context, "gateway.intent_result_store_unavailable", store_exc)
                # failed 终态也无法提交时证据不足，必须使用独立存储错误阻断后续阶段。
                raise IntentResultStoreUnavailableError(
                    "gateway.intent_result_store_unavailable", decision
                ) from None
            decision = _failed_closed_decision(
                result_id=result_id,
                reason_code="classifier_failed",
                input_truncated=input_truncated,
            )
            _log_sync_failure(context, "gateway.intent_classifier_unavailable", exc)
            # 分类失败已经保存固定 failed 事实，公开错误仍需与存储故障严格区分。
            raise IntentClassifierUnavailableError(
                "gateway.intent_classifier_unavailable", decision
            ) from None

        if classification.detected:
            # blocked 只有 result/event 单事务提交成功后才成立；存储失败不得误报业务阻断。
            try:
                event = _persist_classification(
                    config,
                    context,
                    classification,
                    result_id=result_id,
                    classification_mode="sync_strong",
                    status="blocked",
                    action="block",
                    input_truncated=input_truncated,
                    created_at=_utcnow(),
                    duration_ms=_duration_ms(started),
                )
            except _ResultStoreUnavailable as exc:
                decision = _failed_closed_decision(
                    result_id=None,
                    reason_code="result_store_unavailable",
                    input_truncated=input_truncated,
                )
                _log_sync_failure(context, "gateway.intent_result_store_unavailable", exc)
                # 命中不能在结果/event 事务失败后冒充已阻断，改用存储错误失败关闭。
                raise IntentResultStoreUnavailableError(
                    "gateway.intent_result_store_unavailable", decision
                ) from None
            if event is not None:
                # Webhook 队列故障发生在安全事实提交后，只影响通知，不回滚结果或改变阻断语义。
                enqueue_runtime_event(app_state, event)
            decision = _blocked_decision(result_id, classification, input_truncated)
            raise IntentBlockedError("gateway.intent_blocked", decision)

        job = IntentClassificationJob(
            tenant_id=context.tenant_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            virtual_key_id=context.virtual_key_id or "",
            intent_risk_result_id=result_id,
            detector_version=DETECTOR_VERSION,
            ruleset_version=RULESET_VERSION,
            input_truncated=input_truncated,
            enqueued_at=_utcnow(),
            texts=texts,
        )
        decision = _enqueue_async_job(app_state, job)
        return apply_intent_decision(context, decision)
    finally:
        # 释放本函数的额外正文引用；成功入队时仅 queue job 保留后续分类所需的短生命周期引用。
        texts = ()


def enforce_session_intent_guard(
    config: AppConfig,
    app_state: object,
    context: RequestLogContext,
    payload: dict[str, Any],
) -> tuple[RequestLogContext, SessionIntentClassification]:
    """为可信 session 同步执行一次历史 strong/current-turn light 分类。

    参数：运行配置、应用状态、认证 RequestLog 上下文及 Policy mask 后 Chat payload。
    返回：已写入 ``sync_session/completed`` 摘要的上下文和双分类摘要。关键约束：只调用
    Shared ``classify_session_intent`` 一次且不投异步队列；历史 strong 可阻断，但只有
    ``current_turn`` 由 PRD-070 累计。分类/结果存储故障继续采用 PRD-069 失败关闭合同。
    """

    texts = extract_session_user_texts(payload)
    result_id = f"irr_{uuid4().hex}"
    started = time.perf_counter()
    try:
        try:
            session_classification = classify_session_intent(texts)
        except Exception as exc:
            try:
                _persist_classifier_failure(
                    config,
                    context,
                    result_id=result_id,
                    classification_mode="sync_session",
                    input_code_points=sum(len(text) for text in texts),
                    input_truncated=False,
                    duration_ms=_duration_ms(started),
                )
            except _ResultStoreUnavailable as store_exc:
                decision = _failed_closed_decision(
                    result_id=None,
                    reason_code="result_store_unavailable",
                    input_truncated=False,
                    classification_mode="sync_session",
                )
                _log_sync_failure(context, "gateway.intent_result_store_unavailable", store_exc)
                # 结果未持久化时必须失败关闭，不能返回看似成功但不可审计的分类结果。
                raise IntentResultStoreUnavailableError(
                    "gateway.intent_result_store_unavailable", decision
                ) from None
            decision = _failed_closed_decision(
                result_id=result_id,
                reason_code="classifier_failed",
                input_truncated=False,
                classification_mode="sync_session",
            )
            _log_sync_failure(context, "gateway.intent_classifier_unavailable", exc)
            # 分类器异常只能暴露统一错误码，原始异常和正文不得进入响应或日志。
            raise IntentClassifierUnavailableError(
                "gateway.intent_classifier_unavailable", decision
            ) from None

        effective = (
            session_classification.strong_guard
            if session_classification.strong_guard.detected
            else session_classification.current_turn
        )
        blocked = session_classification.strong_guard.detected
        status = "blocked" if blocked else ("detected" if effective.detected else "clean")
        action = "block" if blocked else ("observe" if effective.detected else "allow")
        try:
            event = _persist_classification(
                config,
                context,
                effective,
                result_id=result_id,
                classification_mode="sync_session",
                status=status,
                action=action,
                input_truncated=session_classification.input_truncated,
                created_at=_utcnow(),
                duration_ms=_duration_ms(started),
            )
        except _ResultStoreUnavailable as exc:
            decision = _failed_closed_decision(
                result_id=None,
                reason_code="result_store_unavailable",
                input_truncated=session_classification.input_truncated,
                classification_mode="sync_session",
            )
            _log_sync_failure(context, "gateway.intent_result_store_unavailable", exc)
            # 同步会话结果写库失败时禁止继续累计，避免 Redis 与数据库形成不同安全事实。
            raise IntentResultStoreUnavailableError(
                "gateway.intent_result_store_unavailable", decision
            ) from None
        if event is not None:
            # source result/event 已提交；Webhook 队列失败不能回滚安全事实或改写阻断语义。
            enqueue_runtime_event(app_state, event)
        decision = IntentRiskDecision(
            intent_risk_result_id=result_id,
            # sync_session 无论 clean/detected/blocked 都已同步完成；阻断语义由 action/result 状态表达。
            schedule_status="completed",
            classification_mode="sync_session",
            # 0062 的 completed RequestLog 用 NULL 表达 clean；结果表仍保留 canonical ``none``。
            risk_type=None if effective.risk_type == "none" else effective.risk_type,
            risk_score=effective.risk_score,
            action=action,
            reason_code=effective.reason_code,
            detector_version=effective.detector_version,
            input_truncated=session_classification.input_truncated,
        )
        updated_context = apply_intent_decision(context, decision)
        # strong_guard 命中已形成持久 blocked 事实，必须在 Rate/Budget/Provider 前终止。
        if blocked:
            raise IntentBlockedError(
                "gateway.intent_blocked",
                decision,
                session_classification=session_classification,
            )
        # 仅 clean/detected 非阻断结果返回可信当前轮摘要，供会话引擎执行一次原子累计。
        return updated_context, session_classification
    finally:
        # 即使持久化或 Webhook 构造失败，也及时释放本函数额外持有的正文 tuple 引用。
        texts = ()


def apply_intent_decision(
    context: RequestLogContext,
    decision: IntentRiskDecision,
) -> RequestLogContext:
    """把固定意图摘要复制到 RequestLogContext，不接触或派生用户正文。"""

    return replace(
        context,
        intent_risk_result_id=decision.intent_risk_result_id,
        intent_risk_schedule_status=decision.schedule_status,
        intent_risk_mode=decision.classification_mode,
        intent_risk_type=decision.risk_type,
        intent_risk_score=decision.risk_score,
        intent_risk_action=decision.action,
        intent_risk_reason_code=decision.reason_code,
        intent_risk_detector_version=decision.detector_version,
        intent_risk_input_truncated=decision.input_truncated,
    )


def process_intent_job(
    config: AppConfig,
    job: IntentClassificationJob,
) -> WebhookEvent | None:
    """在线程中执行完整本地分类并保存 async_light 终态。

    参数：数据库配置与已入队任务。返回：detected 时返回提交后的 canonical Webhook 事件，
    clean/failed 时返回空。关键约束：分类失败会另开短事务保存 failed；数据库事务失败时
    不伪造终态，RequestLog 继续表达 pending/best_effort，所有日志只含固定码和异常类型。
    """

    started = time.perf_counter()
    try:
        classification = classify_intent(job.texts)
    except Exception as exc:
        try:
            _persist_classifier_failure(
                config,
                _context_from_job(job),
                result_id=job.intent_risk_result_id,
                classification_mode="async_light",
                input_code_points=sum(len(text) for text in job.texts),
                input_truncated=job.input_truncated,
                duration_ms=_duration_ms(started),
            )
        except _ResultStoreUnavailable as store_exc:
            _log_async_failure(job, "intent_risk.result_store_unavailable", store_exc)
        else:
            _log_async_failure(job, "intent_risk.classifier_failed", exc)
        return None

    status = "detected" if classification.detected else "clean"
    action = "observe" if classification.detected else "allow"
    try:
        return _persist_classification(
            config,
            _context_from_job(job),
            classification,
            result_id=job.intent_risk_result_id,
            classification_mode="async_light",
            status=status,
            action=action,
            input_truncated=job.input_truncated,
            created_at=job.enqueued_at,
            duration_ms=_duration_ms(started),
        )
    except _ResultStoreUnavailable as exc:
        # 已完成分类但 SQLite 事务失败时没有可信终态；异步旁路必须保持主请求成功。
        _log_async_failure(job, "intent_risk.result_store_unavailable", exc)
        return None


def _enqueue_async_job(app_state: object, job: IntentClassificationJob) -> IntentRiskDecision:
    """用 ``put_nowait`` 投递任务，并把缺失 worker 与满载映射为固定 skipped 原因。"""

    queue = getattr(app_state, "intent_classification_queue", None)
    if not isinstance(queue, asyncio.Queue):
        decision = _async_decision(job, schedule_status="skipped", reason_code="worker_unavailable")
        _log_enqueue_skipped(job, "worker_unavailable")
        del job
        return decision
    try:
        queue.put_nowait(job)
    except asyncio.QueueFull:
        decision = _async_decision(job, schedule_status="skipped", reason_code="queue_full")
        _log_enqueue_skipped(job, "queue_full")
        del job
        return decision
    return _async_decision(job, schedule_status="queued", reason_code="queued")


def _persist_classification(
    config: AppConfig,
    context: RequestLogContext,
    classification: IntentClassification,
    *,
    result_id: str,
    classification_mode: str,
    status: str,
    action: str,
    input_truncated: bool,
    created_at: datetime,
    duration_ms: int,
) -> WebhookEvent | None:
    """原子保存 Gateway result 和可选 source event，提交后返回已校验事件。"""

    completed_at = _utcnow()
    completed_at_text = _to_db_time(completed_at)
    event_id = f"irevt_{uuid4().hex}"
    event = (
        _build_event(
            context,
            classification,
            result_id=result_id,
            event_id=event_id,
            classification_mode=classification_mode,
            action=action,
            input_truncated=input_truncated,
            completed_at=completed_at,
        )
        if classification.detected
        else None
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO intent_risk_results(
              intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
              actor_type,actor_id,status,classification_mode,risk_type,risk_score,
              risk_score_scale,action,reason_code,detector_version,ruleset_version,
              input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
            ) VALUES(?,?,'gateway',?,?,'virtual_key',?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
            """,
            (
                result_id,
                context.tenant_id,
                context.request_id,
                context.trace_id,
                context.virtual_key_id,
                status,
                classification_mode,
                classification.risk_type,
                classification.risk_score,
                classification.risk_score_scale,
                action,
                classification.reason_code,
                classification.detector_version,
                classification.ruleset_version,
                classification.input_code_points,
                1 if input_truncated else 0,
                duration_ms,
                _to_db_time(created_at),
                completed_at_text,
            ),
        )
        if event is not None:
            _insert_event(
                connection,
                context,
                classification,
                result_id=result_id,
                event_id=event_id,
                classification_mode=classification_mode,
                action=action,
                input_truncated=input_truncated,
                completed_at=completed_at_text,
            )
        connection.commit()
    # 任一 result/event SQL 或 commit 失败都回滚整个安全事实，禁止留下半事件。
    except sqlite3.Error:
        _rollback_safely(connection)
        raise _ResultStoreUnavailable() from None
    finally:
        _close_safely(connection)
    return event


def _persist_classifier_failure(
    config: AppConfig,
    context: RequestLogContext,
    *,
    result_id: str,
    classification_mode: str,
    input_code_points: int,
    input_truncated: bool,
    duration_ms: int,
) -> None:
    """分类器异常时在独立短事务保存不含正文的 failed 终态。"""

    completed_at = _to_db_time(_utcnow())
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO intent_risk_results(
              intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
              actor_type,actor_id,status,classification_mode,risk_type,risk_score,
              risk_score_scale,action,reason_code,detector_version,ruleset_version,
              input_code_points,input_truncated,duration_ms,error_code,created_at,completed_at
            ) VALUES(?,?,'gateway',?,?,'virtual_key',?,'failed',?,'none',0,10000,
                     'none','classifier_failed',?,?,?, ?,?,
                     'intent_risk.classifier_failed',?,?)
            """,
            (
                result_id,
                context.tenant_id,
                context.request_id,
                context.trace_id,
                context.virtual_key_id,
                classification_mode,
                DETECTOR_VERSION,
                RULESET_VERSION,
                min(20_000, max(0, input_code_points)),
                1 if input_truncated else 0,
                duration_ms,
                completed_at,
                completed_at,
            ),
        )
        connection.commit()
    # failed 终态也必须原子提交；失败时交由同步闸门转换成独立存储错误。
    except sqlite3.Error:
        _rollback_safely(connection)
        raise _ResultStoreUnavailable() from None
    finally:
        _close_safely(connection)


def _insert_event(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    classification: IntentClassification,
    *,
    result_id: str,
    event_id: str,
    classification_mode: str,
    action: str,
    input_truncated: bool,
    completed_at: str,
) -> None:
    """写入与 result 逐字段绑定的 ``security.intent_detected`` source event。

    参数：当前事务连接、可信请求 owner、分类摘要及服务端生成的结果/事件身份。返回：无。
    关键约束：本函数不提交事务，事件必须与同事务 result 的安全字段逐项一致。
    """

    connection.execute(
        """
        INSERT INTO intent_risk_events(
          intent_risk_event_id,tenant_id,intent_risk_result_id,event_type,
          source_request_id,source_trace_id,source_kind,classification_mode,
          risk_type,risk_score,risk_score_scale,action,reason_code,detector_version,
          ruleset_version,input_truncated,actor_type,actor_id,created_at
        ) VALUES(?,?,?,'security.intent_detected',?,?,'gateway',?,?,?,?,?,?,?,?,?,
                 'virtual_key',?,?)
        """,
        (
            event_id,
            context.tenant_id,
            result_id,
            context.request_id,
            context.trace_id,
            classification_mode,
            classification.risk_type,
            classification.risk_score,
            classification.risk_score_scale,
            action,
            classification.reason_code,
            classification.detector_version,
            classification.ruleset_version,
            1 if input_truncated else 0,
            context.virtual_key_id,
            completed_at,
        ),
    )


def _build_event(
    context: RequestLogContext,
    classification: IntentClassification,
    *,
    result_id: str,
    event_id: str,
    classification_mode: str,
    action: str,
    input_truncated: bool,
    completed_at: datetime,
) -> WebhookEvent:
    """构造与持久 source row 一致的 canonical Webhook 事件并执行 Shared 精确校验。"""

    try:
        return build_webhook_event(
            tenant_id=context.tenant_id,
            event_type="security.intent_detected",
            event_id=event_id,
            timestamp=completed_at,
            actor_id=context.virtual_key_id,
            object_id=result_id,
            trace_id=context.trace_id,
            source_request_id=context.request_id,
            payload={
                "intent_risk_result_id": result_id,
                "source_request_id": context.request_id,
                "source_kind": "gateway",
                "classification_mode": classification_mode,
                "risk_type": classification.risk_type,
                "risk_score": classification.risk_score,
                "risk_score_scale": classification.risk_score_scale,
                "action": action,
                "reason_code": classification.reason_code,
                "detector_version": classification.detector_version,
                "ruleset_version": classification.ruleset_version,
                "input_truncated": input_truncated,
                "completed_at": _to_db_time(completed_at),
            },
        )
    # Webhook exact-key/envelope 构造拒绝摘要时不能降级发送宽松 payload。
    except (TypeError, ValueError):
        # 合同映射损坏等同结果存储不可用，不能提交一条未来无法安全投递的 detected 事实。
        raise _ResultStoreUnavailable() from None


def _context_from_job(job: IntentClassificationJob) -> RequestLogContext:
    """从已冻结 job 恢复最小 tenant-qualified 写入上下文，不复制正文字段。"""

    return RequestLogContext(
        tenant_id=job.tenant_id,
        request_id=job.request_id,
        trace_id=job.trace_id,
        virtual_key_id=job.virtual_key_id,
    )


def _blocked_decision(
    result_id: str,
    classification: IntentClassification,
    input_truncated: bool,
) -> IntentRiskDecision:
    """构造已提交强规则阻断的 RequestLog 安全摘要。"""

    return IntentRiskDecision(
        intent_risk_result_id=result_id,
        schedule_status="blocked",
        classification_mode="sync_strong",
        risk_type=classification.risk_type,
        risk_score=classification.risk_score,
        action="block",
        reason_code=classification.reason_code,
        detector_version=classification.detector_version,
        input_truncated=input_truncated,
    )


def _failed_closed_decision(
    *,
    result_id: str | None,
    reason_code: str,
    input_truncated: bool,
    classification_mode: str = "sync_strong",
) -> IntentRiskDecision:
    """构造强规则分类或证据存储故障的失败关闭摘要。"""

    return IntentRiskDecision(
        intent_risk_result_id=result_id,
        schedule_status="failed_closed",
        classification_mode=classification_mode,  # type: ignore[arg-type]
        risk_type=None,
        risk_score=None,
        # failed_closed 必须显式冻结 none，避免消费者把空值误解为异步尚未产生动作。
        action="none",
        reason_code=reason_code,
        detector_version=DETECTOR_VERSION,
        input_truncated=input_truncated,
    )


def _async_decision(
    job: IntentClassificationJob,
    *,
    schedule_status: IntentScheduleStatus,
    reason_code: str,
) -> IntentRiskDecision:
    """构造 queued 或 skipped 的异步调度摘要，不伪造尚未持久化的分类终态。"""

    return IntentRiskDecision(
        intent_risk_result_id=job.intent_risk_result_id,
        schedule_status=schedule_status,
        classification_mode="async_light",
        risk_type=None,
        risk_score=None,
        action=None,
        reason_code=reason_code,
        detector_version=job.detector_version,
        input_truncated=job.input_truncated,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """打开启用外键的短连接，并把路径或驱动故障折叠为固定内部存储错误。"""

    try:
        return connect_database(config.database_url)
    # 连接失败也属于结果存储不可用，不能把底层路径或 SQLite 消息带入公开异常。
    except DbError:
        raise _ResultStoreUnavailable() from None


def _rollback_safely(connection: sqlite3.Connection) -> None:
    """结果事务失败后机会性回滚，二次故障不得覆盖固定存储错误语义。"""

    try:
        connection.rollback()
    # 回滚本身也可能因连接损坏失败；不能让二次异常覆盖原始固定存储故障。
    except sqlite3.Error:
        # 原事务已经失败，回滚异常只说明连接进一步损坏，不能向响应暴露底层消息。
        return


def _close_safely(connection: sqlite3.Connection) -> None:
    """释放短连接；close 异常不能把已提交的安全终态改写为未知失败。"""

    try:
        connection.close()
    # 关闭句柄失败不改变已完成 commit 的事实，也不向日志扩散驱动异常文本。
    except sqlite3.Error:
        # commit 成功后 close 故障不影响持久事实，记录层也不得输出数据库路径或驱动文本。
        return


def _duration_ms(started: float) -> int:
    """把本地分类耗时压到数据库允许的非负毫秒范围。"""

    return min(86_400_000, max(0, int((time.perf_counter() - started) * 1000)))


def _utcnow() -> datetime:
    """返回秒级 UTC 时间，确保 result、source event 与 Webhook 可逐字关联。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把 aware datetime 转为 SQLite 与 Webhook 共用的 canonical UTC 秒文本。

    参数：带时区的内部时间。返回：以 ``Z`` 结尾的秒级文本。统一格式使 result、event
    与 Webhook 可以逐字段复核，避免同一时刻因表示差异产生 owner 校验失败。
    """

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log_sync_failure(context: RequestLogContext, error_code: str, exc: Exception) -> None:
    """记录同步失败关闭事实，只包含固定码和异常类型。"""

    logger.warning(
        "security.intent_sync_failed",
        extra={
            "event": "security.intent_sync_failed",
            "tenant_id": context.tenant_id,
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "object_id": context.request_id,
            "error_code": error_code,
            "payload": {"exception_type": exc.__class__.__name__},
        },
    )


def _log_async_failure(job: IntentClassificationJob, error_code: str, exc: Exception) -> None:
    """记录异步分类或存储失败，不输出 job repr、正文或异常消息。"""

    logger.warning(
        "security.intent_async_failed",
        extra={
            "event": "security.intent_async_failed",
            "tenant_id": job.tenant_id,
            "request_id": job.request_id,
            "trace_id": job.trace_id,
            "object_id": job.intent_risk_result_id,
            "error_code": error_code,
            "payload": {"exception_type": exc.__class__.__name__},
        },
    )


def _log_enqueue_skipped(job: IntentClassificationJob, reason: str) -> None:
    """记录队列未接收任务的固定原因，不记录正文、队列内容或高基数租户标签。"""

    logger.warning(
        "security.intent_enqueue_skipped",
        extra={
            "event": "security.intent_enqueue_skipped",
            "tenant_id": job.tenant_id,
            "request_id": job.request_id,
            "trace_id": job.trace_id,
            "object_id": job.intent_risk_result_id,
            "payload": {"reason": reason},
        },
    )


__all__ = [
    "apply_intent_decision",
    "enforce_intent_guard",
    "enforce_session_intent_guard",
    "process_intent_job",
]
