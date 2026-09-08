"""PRD-077 只读管理员助手的确定性、租户隔离执行流。"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anyio
from fastapi import BackgroundTasks, Request
from metahub_shared.admin_assistant import (
    ADMIN_ASSISTANT_ENDPOINT_DEADLINE_SECONDS,
    ADMIN_ASSISTANT_GOVERNANCE_DEADLINE_SECONDS,
    ADMIN_ASSISTANT_SOURCE_DEADLINE_SECONDS,
    AdminAssistantContractError,
    AnswerClaim,
    AnswerRecommendation,
    AnswerSource,
    ParsedAssistantQuery,
    SourceType,
    build_assistant_answer,
    parse_assistant_query,
    validate_assistant_answer_wire,
)
from metahub_shared.db import DbError, connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import AuditRequestMeta, build_audit_request_meta
from metahub_control.auth import AuthContext, require_permission

from .repository import (
    PersistedAttempt,
    SourceSnapshot,
    dispatch_generic_audit,
    persist_attempt,
    read_source_snapshot,
    required_permissions,
    safe_request_log_error_code,
)
from .wire import read_assistant_question

# base 权限、来源/决策/治理期限和 provisional ID 共同冻结一次 endpoint attempt 的执行边界；
# 限流表只按 tenant/actor 保存单调时钟，不保存问题、回答或链路正文。
BASE_PERMISSION = "admin_assistant.query"
DECISION_DEADLINE_SECONDS = ADMIN_ASSISTANT_ENDPOINT_DEADLINE_SECONDS
SOURCE_DEADLINE_SECONDS = ADMIN_ASSISTANT_SOURCE_DEADLINE_SECONDS
GOVERNANCE_DEADLINE_SECONDS = ADMIN_ASSISTANT_GOVERNANCE_DEADLINE_SECONDS
_DUMMY_QUERY_ID = "asq_" + "0" * 32
_DUMMY_AUDIT_ID = "asa_" + "0" * 32

# 认证与来源读取共用同一长期连接和单 slot；最终审计使用另一条长期连接和独立单 slot。
# 无限 dispatcher 只绕过 AnyIO 默认池队列，实际活跃线程仍严格受这两个有限 slot 控制。
_READ_WORKER_CAPACITY = 1
_WRITE_WORKER_CAPACITY = 1
# 已进入专用认证链的 attempt owner 必须有硬上限；shutdown 会先封住新登记，再等待这些
# owner 完成最终治理。额外调度宽限只用于释放 owner，不会刷新任何请求的业务期限。
_IN_FLIGHT_ATTEMPT_CAPACITY = 256
_SHUTDOWN_ATTEMPT_RELEASE_GRACE_SECONDS = 0.250
# 限流窗口内每个 key 的时间戳数量和 registry key 数都必须有硬上限；低频控制面在达到
# 活跃租户或 actor 容量时关闭式拒绝，不能通过持续创建可信主体让进程内状态无限增长。
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_ACTOR_RATE_LIMIT_PER_WINDOW = 30
_TENANT_RATE_LIMIT_PER_WINDOW = 120
_RATE_LIMIT_ACTOR_KEY_CAPACITY = 4_096
_RATE_LIMIT_TENANT_KEY_CAPACITY = 1_024
_EXECUTION_RUNTIMES_STATE = "admin_assistant_execution_runtimes"
_REQUEST_BUDGET_STATE = "admin_assistant_request_budget"
_REQUEST_RUNTIME_STATE = "admin_assistant_request_runtime"
_REQUEST_ATTEMPT_OWNER_STATE = "admin_assistant_request_attempt_owner"


@dataclass(frozen=True, slots=True)
class _RequestBudget:
    """冻结一次 HTTP attempt 的入口、来源、决策和治理绝对期限。"""

    started: float
    source_deadline: float
    decision_deadline: float
    governance_deadline: float


@dataclass(slots=True)
class _LoopRuntime:
    """保存一个 event loop 专属的锁、连接与 worker admission 原语。"""

    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop]
    tenant_locks: weakref.WeakValueDictionary[str, asyncio.Lock]
    read_slots: anyio.CapacityLimiter
    write_slots: anyio.CapacityLimiter
    dispatcher: anyio.CapacityLimiter
    attempt_owners: dict[object, float]
    attempts_drained: asyncio.Event
    read_connection: sqlite3.Connection | None = None
    write_connection: sqlite3.Connection | None = None
    closing: bool = False
    closed: bool = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """返回当前运行循环的弱引用目标，避免 runtime 值反向固定字典键。"""

        return self.loop_ref()

    @property
    def auth_slots(self) -> anyio.CapacityLimiter:
        """认证与来源必须在同一连接 owner 上串行。"""

        return self.read_slots

    @property
    def source_slots(self) -> anyio.CapacityLimiter:
        """保留来源 admission 名称，同时复用唯一 read-stage slot。"""

        return self.read_slots


_limiter_lock = threading.Lock()
_execution_runtime_lock = threading.Lock()
_actor_attempts: dict[tuple[str, str], deque[float]] = {}
_tenant_attempts: dict[str, deque[float]] = {}


def prepare_admin_assistant_request(
    request: Request,
) -> tuple[_RequestBudget, _LoopRuntime]:
    """从共享 middleware 入口冻结预算，并取得当前 loop 的专属执行原语。"""

    existing_budget = getattr(request.state, _REQUEST_BUDGET_STATE, None)
    existing_runtime = getattr(request.state, _REQUEST_RUNTIME_STATE, None)
    existing_owner = getattr(request.state, _REQUEST_ATTEMPT_OWNER_STATE, None)
    if existing_budget is not None or existing_runtime is not None or existing_owner is not None:
        # 同一 dependency 即使被框架复用也只能返回第一次冻结的对象，不能按当前时刻刷新期限。
        if not isinstance(existing_budget, _RequestBudget) or not isinstance(
            existing_runtime, _LoopRuntime
        ):
            # request.state 被错误对象污染时不能刷新预算或继续使用未知 runtime。
            raise ApiError("admin_assistant.source_unavailable")
        if (
            existing_runtime.loop is not asyncio.get_running_loop()
            or existing_runtime.closed
            or not _runtime_owns_attempt(existing_runtime, existing_owner)
        ):
            # 只有已登记 owner 可在 shutdown seal 后继续完成本 attempt，未知状态一律拒绝。
            raise ApiError("admin_assistant.source_unavailable")
        return existing_budget, existing_runtime

    now = time.monotonic()
    raw_started = getattr(request.state, "request_started_monotonic", None)
    if raw_started is None:
        # 仅直接调用依赖的测试/嵌入场景允许缺失 middleware；字段存在但非法时必须失败关闭。
        started = now
    elif (
        isinstance(raw_started, bool)
        or not isinstance(raw_started, (int, float))
        or not math.isfinite(float(raw_started))
    ):
        # 非法入口时钟无法证明期限来源，必须按固定来源错误关闭失败。
        raise ApiError("admin_assistant.source_unavailable")
    else:
        started = float(raw_started)

    decision_seconds = float(DECISION_DEADLINE_SECONDS)
    source_seconds = float(SOURCE_DEADLINE_SECONDS)
    governance_seconds = float(GOVERNANCE_DEADLINE_SECONDS)
    if (
        not math.isfinite(decision_seconds)
        or not math.isfinite(source_seconds)
        or not math.isfinite(governance_seconds)
        or decision_seconds <= 0
        or source_seconds <= 0
        or governance_seconds <= 0
        or source_seconds > decision_seconds
        or decision_seconds > governance_seconds
    ):
        # 三阶段服务端预算装配非法时禁止退回无期限执行，避免同步数据库工作失控。
        raise ApiError("admin_assistant.source_unavailable")
    budget = _RequestBudget(
        started=started,
        source_deadline=started + source_seconds,
        decision_deadline=started + decision_seconds,
        governance_deadline=started + governance_seconds,
    )
    if (
        budget.source_deadline <= now
        or budget.decision_deadline <= now
        or budget.governance_deadline <= now
    ):
        # middleware 已冻结但执行已迟到时禁止从 dependency 处重新开启任一阶段窗口。
        raise ApiError("admin_assistant.source_unavailable")

    runtime = _execution_runtime(request)
    owner = _register_attempt_owner(runtime, budget.governance_deadline)
    try:
        setattr(request.state, _REQUEST_BUDGET_STATE, budget)
        setattr(request.state, _REQUEST_RUNTIME_STATE, runtime)
        setattr(request.state, _REQUEST_ATTEMPT_OWNER_STATE, owner)
    except BaseException:
        _release_attempt_owner(runtime, owner)
        raise
    return budget, runtime


def admin_assistant_request_execution(
    request: Request,
) -> tuple[_RequestBudget, _LoopRuntime]:
    """读取认证阶段冻结的预算/runtime；缺失或跨 loop 使用一律失败关闭。"""

    budget = getattr(request.state, _REQUEST_BUDGET_STATE, None)
    runtime = getattr(request.state, _REQUEST_RUNTIME_STATE, None)
    owner = getattr(request.state, _REQUEST_ATTEMPT_OWNER_STATE, None)
    if not isinstance(budget, _RequestBudget) or not isinstance(runtime, _LoopRuntime):
        # 缺失认证阶段冻结对象说明执行链被绕过，不能在路由阶段重新创建。
        raise ApiError("admin_assistant.source_unavailable")
    if (
        runtime.loop is not asyncio.get_running_loop()
        or runtime.closed
        or not _runtime_owns_attempt(runtime, owner)
    ):
        # shutdown 只允许 seal 前已登记的 owner 继续；新请求与伪造 request.state 均失败关闭。
        raise ApiError("admin_assistant.source_unavailable")
    return budget, runtime


def admin_assistant_request_owner(request: Request) -> object:
    """返回当前请求已登记的治理 owner；未知或已释放 owner 不得借用长期连接。"""

    runtime = getattr(request.state, _REQUEST_RUNTIME_STATE, None)
    owner = getattr(request.state, _REQUEST_ATTEMPT_OWNER_STATE, None)
    if (
        not isinstance(runtime, _LoopRuntime)
        or runtime.loop is not asyncio.get_running_loop()
        or runtime.closed
        or not _runtime_owns_attempt(runtime, owner)
    ):
        # owner 不属于当前 loop/runtime 时禁止借用长期连接，避免跨请求或跨 loop 污染。
        raise ApiError("admin_assistant.source_unavailable")
    return owner


def release_admin_assistant_request(request: Request) -> None:
    """幂等释放认证链 attempt owner，并在最后一个 owner 退出时唤醒 teardown。"""

    runtime = getattr(request.state, _REQUEST_RUNTIME_STATE, None)
    owner = getattr(request.state, _REQUEST_ATTEMPT_OWNER_STATE, None)
    if isinstance(runtime, _LoopRuntime):
        _release_attempt_owner(runtime, owner)
    try:
        setattr(request.state, _REQUEST_ATTEMPT_OWNER_STATE, None)
    except BaseException:
        # request.state 清理失败不能阻止 runtime owner 计数归零。
        return


def _register_attempt_owner(runtime: _LoopRuntime, governance_deadline: float) -> object:
    """在当前 loop 同步登记有界 owner，使 shutdown seal 与请求 admission 不留 await 竞态。"""

    capacity = _IN_FLIGHT_ATTEMPT_CAPACITY
    if (
        runtime.loop is not asyncio.get_running_loop()
        or runtime.closing
        or runtime.closed
        or type(capacity) is not int
        or capacity <= 0
        or len(runtime.attempt_owners) >= capacity
        or not math.isfinite(governance_deadline)
    ):
        # loop、shutdown、容量或期限任一不可证明时禁止登记新 owner，避免无界或孤儿治理任务。
        raise ApiError("admin_assistant.source_unavailable")
    owner = object()
    runtime.attempt_owners[owner] = governance_deadline
    runtime.attempts_drained.clear()
    return owner


def _release_attempt_owner(runtime: _LoopRuntime, owner: object) -> None:
    """从有界 owner 表移除单个 attempt；重复清理保持幂等。"""

    try:
        runtime.attempt_owners.pop(owner, None)
    # request.state 可能被注入不可哈希对象；释放路径必须幂等且不能破坏 owner 表。
    except TypeError:
        return
    if not runtime.attempt_owners:
        runtime.attempts_drained.set()


def _runtime_owns_attempt(runtime: _LoopRuntime, owner: object) -> bool:
    """用对象身份表验证 owner，拒绝 request.state 中不可哈希或伪造值。"""

    try:
        return owner is not None and owner in runtime.attempt_owners
    # 不可哈希 owner 一律视为伪造身份，不能因类型异常绕过所有权校验。
    except TypeError:
        return False


def admin_assistant_runtime_read_connection(
    runtime: _LoopRuntime,
    config: AppConfig,
    *,
    deadline: float,
    attempt_owner: object | None = None,
) -> sqlite3.Connection:
    """在 read slot owner 内延迟建立并复用认证/来源连接。"""

    if runtime.closed or (runtime.closing and not _runtime_owns_attempt(runtime, attempt_owner)):
        # seal 后仅既有 attempt owner 可继续来源阶段；未知调用不能重开或借出连接。
        raise ApiError("admin_assistant.source_unavailable")
    connection = runtime.read_connection
    if connection is None:
        connection = _open_runtime_connection(
            config,
            error_code="admin_assistant.source_unavailable",
            deadline=deadline,
        )
        runtime.read_connection = connection
    return connection


def retire_admin_assistant_runtime_read_connection(
    runtime: _LoopRuntime,
    connection: sqlite3.Connection,
) -> None:
    """在 read slot owner 内关闭污染句柄，并仅按对象身份清除 runtime 引用。"""

    try:
        connection.close()
    # 污染连接即使无法正规关闭也必须摘除 runtime 引用，禁止下一请求继续复用。
    except sqlite3.Error:
        # close 失败也必须在 finally 摘除引用，后续请求只能建立新句柄。
        pass
    finally:
        if runtime.read_connection is connection:
            runtime.read_connection = None


def _admin_assistant_runtime_write_connection(
    runtime: _LoopRuntime,
    config: AppConfig,
    *,
    deadline: float,
) -> sqlite3.Connection:
    """在已登记的 write slot owner 内延迟建立并复用最终事实连接。"""

    if runtime.closed:
        # closing 之后只有已在 FIFO 登记的 persistence worker 能调用本函数；真正关闭后必须拒绝。
        raise ApiError("admin_assistant.audit_unavailable")
    connection = runtime.write_connection
    if connection is None:
        connection = _open_runtime_connection(
            config,
            error_code="admin_assistant.audit_unavailable",
            deadline=deadline,
        )
        runtime.write_connection = connection
    return connection


def _retire_admin_assistant_runtime_write_connection(
    runtime: _LoopRuntime,
    connection: sqlite3.Connection,
) -> None:
    """在 write slot owner 内关闭污染句柄，并仅按对象身份清除 runtime 引用。"""

    try:
        connection.close()
    # 写连接 close 失败不能阻止按对象身份退休，否则后续治理可能写入污染句柄。
    except sqlite3.Error:
        pass
    finally:
        if runtime.write_connection is connection:
            runtime.write_connection = None


def _open_runtime_connection(
    config: AppConfig,
    *,
    error_code: str,
    deadline: float,
) -> sqlite3.Connection:
    """在绝对期限内建立 WAL/FULL 长期连接，并隐藏路径与驱动细节。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url, check_same_thread=False)
        remaining_ms = max(1, int(_remaining_deadline(deadline, error_code) * 1_000))
        connection.execute(f"PRAGMA busy_timeout={remaining_ms}")
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
            # 长期读写连接缺少 WAL 时无法满足并发只读与短审计写入边界。
            raise sqlite3.OperationalError("WAL journal mode unavailable")
        # FULL 保留 SQLite 的提交持久性；WAL 只降低连续短事务的 journal 切换成本。
        connection.execute("PRAGMA synchronous=FULL")
        return connection
    except (DbError, sqlite3.Error, ApiError) as exc:
        if connection is not None:
            try:
                connection.close()
            # 初始化失败后的关闭异常只影响待丢弃句柄，不得覆盖固定领域错误。
            except sqlite3.Error:
                pass
        # 连接路径、SQLite 模式或期限错误都折叠为调用阶段的固定错误码。
        raise ApiError(error_code) from exc


def _execution_runtime_for_app(app: object) -> _LoopRuntime:
    """按 app 与 running loop 原子取得 runtime，绝不跨 loop 共享 asyncio 原语。"""

    loop = asyncio.get_running_loop()
    with _execution_runtime_lock:
        state = getattr(app, "state", None)
        if state is None:
            # 缺失 app.state 无法证明配置与 loop owner，禁止创建脱离 lifespan 的 runtime。
            raise ApiError("admin_assistant.source_unavailable")
        runtimes = getattr(state, _EXECUTION_RUNTIMES_STATE, None)
        if runtimes is None:
            runtimes = weakref.WeakKeyDictionary()
            setattr(state, _EXECUTION_RUNTIMES_STATE, runtimes)
        if not isinstance(runtimes, weakref.WeakKeyDictionary):
            # app.state 中未知容器不能被当作可信 loop/runtime owner 使用。
            raise ApiError("admin_assistant.source_unavailable")
        runtime = runtimes.get(loop)
        if runtime is None:
            read_slots = anyio.CapacityLimiter(_READ_WORKER_CAPACITY)
            runtime = _LoopRuntime(
                loop_ref=weakref.ref(loop),
                tenant_locks=weakref.WeakValueDictionary(),
                read_slots=read_slots,
                write_slots=anyio.CapacityLimiter(_WRITE_WORKER_CAPACITY),
                dispatcher=anyio.CapacityLimiter(math.inf),
                attempt_owners={},
                attempts_drained=asyncio.Event(),
            )
            runtime.attempts_drained.set()
            runtimes[loop] = runtime
        elif runtime.closing or runtime.closed:
            # 同一 loop 的旧 runtime 正在退出时禁止创建第二套孤儿连接。
            raise ApiError("admin_assistant.source_unavailable")
        return runtime


def _execution_runtime(request: Request) -> _LoopRuntime:
    """从请求 app 取得当前 loop runtime；保留入口便于路由依赖冻结预算。"""

    return _execution_runtime_for_app(request.app)


async def initialize_admin_assistant_execution_runtime(app: object) -> _LoopRuntime:
    """在 lifespan ready 前预建两条长期连接，避免首请求消耗业务 deadline。"""

    runtime = _execution_runtime_for_app(app)
    state = getattr(app, "state", None)
    config = getattr(state, "config", None)
    if config is None or runtime.closing or runtime.closed:
        # ready 前缺配置或 teardown 已封口时不得再预建连接，避免发布孤儿句柄。
        raise ApiError("admin_assistant.source_unavailable")
    deadline = time.monotonic() + float(GOVERNANCE_DEADLINE_SECONDS)
    await runtime.read_slots.acquire()
    try:
        await runtime.write_slots.acquire()
        try:
            if runtime.read_connection is not None and runtime.write_connection is not None:
                return runtime

            def open_connections() -> tuple[sqlite3.Connection, sqlite3.Connection]:
                """在线程中原子构造缺失连接；半成品必须在异常路径立即关闭。"""

                read_connection = runtime.read_connection
                write_connection = runtime.write_connection
                created: list[sqlite3.Connection] = []
                try:
                    if read_connection is None:
                        read_connection = _open_runtime_connection(
                            config,
                            error_code="admin_assistant.source_unavailable",
                            deadline=deadline,
                        )
                        created.append(read_connection)
                    if write_connection is None:
                        write_connection = _open_runtime_connection(
                            config,
                            error_code="admin_assistant.audit_unavailable",
                            deadline=deadline,
                        )
                        created.append(write_connection)
                    return read_connection, write_connection
                except BaseException:
                    for connection in created:
                        try:
                            connection.close()
                        # 半成品连接关闭失败不能覆盖原初始化异常，且该句柄绝不发布到 runtime。
                        except sqlite3.Error:
                            continue
                    raise

            read_connection, write_connection = await anyio.to_thread.run_sync(
                open_connections,
                abandon_on_cancel=False,
                limiter=runtime.dispatcher,
            )
            if runtime.closing or runtime.closed:
                # startup 被 teardown 接管时不能发布新句柄；局部连接仍由当前 owner 精确关闭。
                for connection in (read_connection, write_connection):
                    if (
                        connection is not runtime.read_connection
                        and connection is not runtime.write_connection
                    ):
                        try:
                            connection.close()
                        # teardown 竞态中的局部句柄只尽力关闭，固定 startup 错误保持优先。
                        except sqlite3.Error:
                            continue
                raise ApiError("admin_assistant.source_unavailable")
            runtime.read_connection = read_connection
            runtime.write_connection = write_connection
            return runtime
        finally:
            runtime.write_slots.release()
    finally:
        runtime.read_slots.release()


async def clear_admin_assistant_execution_runtime(
    app: object,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """在 lifespan 退出时移除 runtime，并在线程中回滚关闭两条长期连接。"""

    state = getattr(app, "state", None)
    if state is None:
        return
    runtime: _LoopRuntime | None = None
    with _execution_runtime_lock:
        runtimes = getattr(state, _EXECUTION_RUNTIMES_STATE, None)
        if isinstance(runtimes, weakref.WeakKeyDictionary):
            runtime = runtimes.get(loop)
            if runtime is not None:
                if runtime.closed:
                    return
                if runtime.closing:
                    # 同一 loop 只允许一个 teardown owner；失败的 closing runtime 不得伪装成功。
                    raise ApiError("admin_assistant.audit_unavailable")
                runtime.closing = True
    if runtime is None:
        return

    async def close_runtime_connections() -> None:
        """等待自身 attempt 与 connection owner 空闲，再关闭长期连接。"""

        await _wait_for_attempt_owners(runtime)
        await runtime.read_slots.acquire()
        try:
            await runtime.write_slots.acquire()
            try:
                connections = (runtime.read_connection, runtime.write_connection)
                await anyio.to_thread.run_sync(
                    lambda: _close_runtime_connection_bundle(*connections),
                    abandon_on_cancel=False,
                    limiter=runtime.dispatcher,
                )
                # 子系统只证明自身连接已关闭，不改变其他服务仍在使用的数据库日志模式。
                runtime.read_connection = None
                runtime.write_connection = None
                runtime.closed = True
            finally:
                runtime.write_slots.release()
        finally:
            runtime.read_slots.release()

    worker = asyncio.create_task(close_runtime_connections())
    cancelled: asyncio.CancelledError | None = None
    completed = False
    try:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            # 宿主取消只延迟传播，必须先等连接清理线程完整退出。
            except asyncio.CancelledError as exc:
                cancelled = exc
                continue
        worker.result()
        completed = True
    finally:
        if completed:
            with _execution_runtime_lock:
                runtimes = getattr(state, _EXECUTION_RUNTIMES_STATE, None)
                if (
                    isinstance(runtimes, weakref.WeakKeyDictionary)
                    and runtimes.get(loop) is runtime
                ):
                    runtimes.pop(loop, None)
    if cancelled is not None:
        raise cancelled


async def _wait_for_attempt_owners(runtime: _LoopRuntime) -> None:
    """按现有 owner 最晚治理期限有界等待；超时保留 runtime 并让 shutdown 失败关闭。"""

    if not runtime.attempt_owners:
        return
    latest_deadline = max(runtime.attempt_owners.values())
    remaining = latest_deadline - time.monotonic() + _SHUTDOWN_ATTEMPT_RELEASE_GRACE_SECONDS
    if not math.isfinite(remaining) or remaining <= 0:
        # owner 期限无效或已耗尽时无法证明在途治理会结束，停服必须失败关闭。
        raise ApiError("admin_assistant.audit_unavailable")
    try:
        async with asyncio.timeout(remaining):
            await runtime.attempts_drained.wait()
    # 等待超过所有 owner 的治理窗口说明连接仍可能被同步线程使用，禁止继续关闭。
    except TimeoutError:
        # 对外只公开固定审计依赖错误，不暴露 owner 数量或内部期限。
        raise ApiError("admin_assistant.audit_unavailable") from None
    if runtime.attempt_owners:
        # Event 与 owner 表不一致说明内部状态被破坏，不能继续关闭在途连接。
        raise ApiError("admin_assistant.audit_unavailable")


def _close_runtime_connection_bundle(
    read_connection: sqlite3.Connection | None,
    write_connection: sqlite3.Connection | None,
) -> None:
    """独立尽力关闭自身两条连接，保留共享数据库的 WAL 与其他 owner。"""

    connections: list[sqlite3.Connection] = []
    for connection in (read_connection, write_connection):
        if connection is not None and all(connection is not item for item in connections):
            connections.append(connection)
    first_error: sqlite3.Error | None = None

    def attempt(operation: Callable[[], object]) -> None:
        """保留最早 SQLite 失败，但绝不因此跳过另一条连接的清理。"""

        nonlocal first_error
        try:
            operation()
        # 两条连接的清理彼此独立；记录首错后仍须尝试其余资源收尾。
        except sqlite3.Error as exc:
            # 首个 SQLite 清理错误决定最终失败，后续异常不得改写原始安全坐标。
            if first_error is None:
                first_error = exc

    for connection in connections:
        attempt(lambda connection=connection: connection.set_progress_handler(None, 0))
        try:
            in_transaction = connection.in_transaction
        # 事务状态读取失败时仍继续清 handler 与连接，但整束连接最终不可判成功。
        except sqlite3.Error as exc:
            in_transaction = False
            first_error = first_error or exc
        if in_transaction:
            attempt(connection.rollback)
        # 清理连接级设置，避免暖连接归还后影响下一个 owner。
        attempt(lambda connection=connection: connection.execute("PRAGMA query_only=OFF"))

    for connection in connections:
        attempt(connection.close)

    # 任一自身连接清理失败都不能被其余成功步骤掩盖为完整 shutdown。
    if first_error is not None:
        # 不把 SQLite 路径、锁 owner 或 SQL 详情交给 lifespan/API 错误边界。
        raise ApiError("admin_assistant.audit_unavailable") from None


async def run_admin_assistant_worker[T](
    operation: Callable[[], T],
    *,
    slots: anyio.CapacityLimiter,
    dispatcher: anyio.CapacityLimiter,
    deadline: float,
    error_code: str,
) -> T:
    """在 deadline 前取得有限 slot；线程启动后等待其自行完成，绝不迟到后台提交。"""

    acquired = False
    try:
        try:
            with anyio.fail_after(_remaining_deadline(deadline, error_code)):
                await slots.acquire()
                acquired = True
        # admission 排队超时不得启动同步线程或占用长期连接。
        except TimeoutError:
            # 调用阶段决定公开固定码，不能暴露 AnyIO 排队实现细节。
            raise ApiError(error_code) from None

        def guarded_operation() -> T:
            """在真正占用 worker 时再次校验绝对期限，关闭 admission 边界竞态。"""

            _remaining_deadline(deadline, error_code)
            return operation()

        # admission 已保证活跃线程有界；无限 dispatcher 只移除默认 40-token 队列的未知等待。
        worker = asyncio.create_task(
            anyio.to_thread.run_sync(
                guarded_operation,
                abandon_on_cancel=False,
                limiter=dispatcher,
            )
        )
        try:
            return await asyncio.shield(worker)
        # 请求取消后仍等待已启动线程，避免释放 slot 后发生迟到数据库提交。
        except asyncio.CancelledError as cancelled:
            # 原生 asyncio 取消不会传给 shield 内 worker；slot 必须保持到线程真实退出，
            # 防止 HTTP 已结束后仍发生迟到提交。
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                # 重复取消仍只记录第一次异常，直到同步线程真实完成。
                except asyncio.CancelledError:
                    continue
            try:
                worker.result()
            except BaseException:
                # 请求已经取消，仍需取走 worker 异常以免泄露 SQL/路径到未处理任务日志。
                pass
            raise cancelled
    finally:
        if acquired:
            slots.release()


async def run_admin_assistant_persistence_worker[T](
    operation: Callable[[sqlite3.Connection], T],
    *,
    runtime: _LoopRuntime,
    config: AppConfig,
    deadline: float,
    attempt_owner: object | None = None,
) -> T:
    """登记最终治理任务，并在同一绝对期限内串行取得长期写连接。"""

    if (
        runtime.loop is not asyncio.get_running_loop()
        or runtime.closed
        or (runtime.closing and not _runtime_owns_attempt(runtime, attempt_owner))
    ):
        # seal 后只有现有 request owner 可新登记终态；seal 前已排队 waiter 仍由 FIFO 保证。
        raise ApiError("admin_assistant.audit_unavailable")

    # 本函数在检查 closing 后不经过其他 await，直接进入 limiter.acquire；因此 teardown 只能排在
    # 已登记 waiter 之后。AnyIO 当前 asyncio limiter 提供 FIFO，不需要再维护一套无界应用队列。
    return await run_admin_assistant_worker(
        lambda: operation(
            _admin_assistant_runtime_write_connection(
                runtime,
                config,
                deadline=deadline,
            )
        ),
        slots=runtime.write_slots,
        dispatcher=runtime.dispatcher,
        deadline=deadline,
        error_code="admin_assistant.audit_unavailable",
    )


def _remaining_deadline(deadline: float, error_code: str) -> float:
    """验证内部 monotonic deadline 并返回严格正的剩余秒数。"""

    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        # 非数值内部期限属于服务端装配错误，只公开调用阶段固定码。
        raise ApiError(error_code)
    remaining = float(deadline) - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        # 已耗尽期限不能通过重新起表继续执行受保护数据库工作。
        raise ApiError(error_code)
    return remaining


async def execute_admin_assistant_query(
    request: Request,
    context: AuthContext,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    """认证完成后即使客户端取消，也等待本次 attempt 形成权威终态。"""

    try:
        operation = asyncio.create_task(
            _execute_admin_assistant_query(request, context, background_tasks)
        )
        cancelled: asyncio.CancelledError | None = None
        while not operation.done():
            try:
                await asyncio.shield(operation)
            # 可信 actor 建立后，客户端取消不得跳过本次 attempt 的终态治理事实。
            except asyncio.CancelledError as exc:
                # 可信 actor 已建立后，断连/Browser Abort 只能取消响应，不能跳过治理事实。
                cancelled = exc
                continue
        if cancelled is not None:
            try:
                operation.result()
            except BaseException:
                # 客户端已离开；只消费固定领域异常，权威事实由内部 operation 决定。
                pass
            raise cancelled
        return operation.result()
    finally:
        # 成功、固定失败、未处理异常和客户端取消都必须释放 gate owner，teardown 才能继续。
        release_admin_assistant_request(request)


async def _execute_admin_assistant_query(
    request: Request,
    context: AuthContext,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    """执行并审计一次独立 HTTP attempt，问题与回答正文不进入持久层。"""

    # 认证依赖已从 middleware 入口冻结预算；路由和 tenant owner 后都不得重新起表。
    budget, runtime = admin_assistant_request_execution(request)
    started = budget.started
    governance_deadline = budget.governance_deadline
    request_id = str(getattr(request.state, "request_id", ""))
    trace_id = str(getattr(request.state, "trace_id", ""))
    request_meta = build_audit_request_meta(request)
    source_deadline = budget.source_deadline
    try:
        require_permission(context, BASE_PERMISSION)
    # 基础权限拒绝发生在问题正文读取前，但已认证 actor 仍要写零正文拒绝审计。
    except ApiError:
        await _persist_terminal(
            request,
            context,
            query_type="unknown",
            outcome="permission_denied",
            failure_code="auth.permission_denied",
            permission_codes=(BASE_PERMISSION,),
            started=started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=governance_deadline,
            background_tasks=background_tasks,
            enforce_permissions=False,
        )
        raise

    try:
        remaining = source_deadline - time.monotonic()
        if remaining <= 0:
            # 正文读取前已耗尽来源预算时直接进入可审计固定失败路径。
            raise TimeoutError
        # 合法 Content-Length 也不能让慢 ASGI stream 无限占用 endpoint；取消只作用于正文
        # coroutine，不用于不可取消的 SQLite 写线程。
        async with asyncio.timeout(remaining):
            question = await read_assistant_question(request)
    # 慢请求正文与来源读取共用 1.2 秒预算，超时后仍尝试写零事实审计。
    except TimeoutError:
        await _audit_and_raise(
            request,
            context,
            ApiError("admin_assistant.source_unavailable"),
            query_type="unknown",
            outcome="failed",
            permission_codes=(BASE_PERMISSION,),
            started=started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=governance_deadline,
            background_tasks=background_tasks,
        )
    # wire 错误只携带固定码；失败 attempt 仍以 unknown 类型落不可变审计。
    except ApiError as exc:
        await _audit_and_raise(
            request,
            context,
            exc,
            query_type="unknown",
            outcome="failed",
            permission_codes=(BASE_PERMISSION,),
            started=started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=governance_deadline,
            background_tasks=background_tasks,
        )

    try:
        parsed = parse_assistant_query(question)
    # 解析器异常不带问题正文，服务层只把固定合同码映射到有限审计终态。
    except AdminAssistantContractError as exc:
        error = ApiError(exc.code)
        outcome = {
            "admin_assistant.read_only_violation": "read_only_blocked",
            "admin_assistant.query_unsupported": "unsupported",
            "admin_assistant.query_ambiguous": "ambiguous",
        }.get(exc.code, "failed")
        await _audit_and_raise(
            request,
            context,
            error,
            query_type="unknown",
            outcome=outcome,
            permission_codes=(BASE_PERMISSION,),
            started=started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=governance_deadline,
            background_tasks=background_tasks,
        )

    permissions = required_permissions(
        parsed.query_type,
        target_request_id=parsed.target_request_id,
    )
    # base permission 不能替代真实来源权限，正文解析后但数据库读取前先做完整快照校验。
    if any(code not in context.permission_codes for code in permissions):
        await _audit_and_raise(
            request,
            context,
            ApiError("auth.permission_denied"),
            query_type=parsed.query_type,
            target_request_id=parsed.target_request_id,
            outcome="permission_denied",
            permission_codes=permissions,
            started=started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=governance_deadline,
            background_tasks=background_tasks,
            enforce_permissions=False,
        )
    if not _consume_rate_limit(context):
        await _audit_and_raise(
            request,
            context,
            ApiError("admin_assistant.rate_limited"),
            query_type=parsed.query_type,
            target_request_id=parsed.target_request_id,
            outcome="rate_limited",
            permission_codes=permissions,
            started=started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=governance_deadline,
            background_tasks=background_tasks,
        )

    # 只有入口权限、wire、意图、源权限和限流均通过后才进入 tenant 串行 owner。
    return await _execute_under_tenant_lock(
        request,
        context,
        background_tasks,
        runtime=runtime,
        budget=budget,
        parsed=parsed,
        permissions=permissions,
        request_id=request_id,
        trace_id=trace_id,
        request_meta=request_meta,
    )


async def _execute_under_tenant_lock(
    request: Request,
    context: AuthContext,
    background_tasks: BackgroundTasks,
    *,
    runtime: _LoopRuntime,
    budget: _RequestBudget,
    parsed: ParsedAssistantQuery,
    permissions: tuple[str, ...],
    request_id: str,
    trace_id: str,
    request_meta: AuditRequestMeta,
) -> dict[str, object]:
    """在来源期限前取得 tenant owner；排队超时仍用总期限写零正文审计。"""

    attempt_owner = admin_assistant_request_owner(request)
    lock = _tenant_execution_lock(
        runtime,
        context.tenant.tenant_id,
        attempt_owner=attempt_owner,
    )
    try:
        try:
            with anyio.fail_after(
                _remaining_deadline(
                    budget.source_deadline,
                    "admin_assistant.source_unavailable",
                )
            ):
                await lock.acquire()
        # tenant owner 排队使用来源期限，不能为拥塞请求刷新新的读取预算。
        except TimeoutError:
            # 未取得 tenant 锁时没有来源事实，只能返回固定来源不可用。
            raise ApiError("admin_assistant.source_unavailable") from None
    # 锁排队的固定错误仍需使用独立 write admission 写零正文终态。
    except ApiError as exc:
        await _audit_and_raise(
            request,
            context,
            exc,
            query_type=parsed.query_type,
            target_request_id=parsed.target_request_id,
            outcome="failed",
            permission_codes=permissions,
            started=budget.started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=budget.governance_deadline,
            background_tasks=background_tasks,
        )

    try:
        return await _execute_source_and_persist(
            request,
            context,
            background_tasks,
            runtime=runtime,
            budget=budget,
            parsed=parsed,
            permissions=permissions,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
        )
    finally:
        lock.release()


async def _execute_source_and_persist(
    request: Request,
    context: AuthContext,
    background_tasks: BackgroundTasks,
    *,
    runtime: _LoopRuntime,
    budget: _RequestBudget,
    parsed: ParsedAssistantQuery,
    permissions: tuple[str, ...],
    request_id: str,
    trace_id: str,
    request_meta: AuditRequestMeta,
) -> dict[str, object]:
    """在 tenant owner 内读取快照、构造回答并提交最终不可变审计。"""

    started = budget.started
    source_deadline = budget.source_deadline
    decision_deadline = budget.decision_deadline
    governance_deadline = budget.governance_deadline
    window_ended_at = datetime.now(UTC).replace(microsecond=0)
    window_started_at = window_ended_at - timedelta(hours=parsed.window_hours)
    attempt_owner = admin_assistant_request_owner(request)
    try:
        # 认证/source 共用 read-stage admission；排队和 progress handler 共用 1.2 秒绝对期限。
        snapshot = await run_admin_assistant_worker(
            lambda: read_source_snapshot(
                request.app.state.config,
                context,
                query_type=parsed.query_type,
                target_request_id=parsed.target_request_id,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
                deadline=source_deadline,
                connection=admin_assistant_runtime_read_connection(
                    runtime,
                    request.app.state.config,
                    deadline=source_deadline,
                    attempt_owner=attempt_owner,
                ),
                invalidate_connection=lambda connection: (
                    retire_admin_assistant_runtime_read_connection(runtime, connection)
                ),
            ),
            slots=runtime.source_slots,
            dispatcher=runtime.dispatcher,
            deadline=source_deadline,
            error_code="admin_assistant.source_unavailable",
        )
        if time.monotonic() >= source_deadline:
            # 已启动来源线程必须完成清理，但迟于 1.2 秒的快照不能进入回答图。
            raise ApiError("admin_assistant.source_unavailable")
        claims, recommendations, sources = _answer_graph(
            parsed,
            snapshot,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
        )
        # 成功审计前先校验完整 claim/source 图；临时 ID 只使用符合 wire 的固定哨兵，
        # 等 Query/Audit/AuditLog 原子提交后才替换为本次不可变真实 ID。
        provisional = build_assistant_answer(
            assistant_query_id=_DUMMY_QUERY_ID,
            assistant_audit_id=_DUMMY_AUDIT_ID,
            query_type=parsed.query_type,
            claims=claims,
            recommendations=recommendations,
            sources=sources,
            request_id=request_id,
            trace_id=trace_id,
        )
        validate_assistant_answer_wire(provisional.to_wire())
        if time.monotonic() >= decision_deadline:
            # 确定性映射与 exact wire 校验只可使用 1.5 秒决策预算；迟到图不得返回。
            raise ApiError("admin_assistant.source_unavailable")
    # typed claim/source 图不满足 Shared exact 合同时，按零事实失败审计并拒绝响应。
    except AdminAssistantContractError:
        await _audit_and_raise(
            request,
            context,
            ApiError("admin_assistant.answer_invalid"),
            query_type=parsed.query_type,
            target_request_id=parsed.target_request_id,
            outcome="failed",
            permission_codes=permissions,
            started=started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=governance_deadline,
            background_tasks=background_tasks,
        )
    # 来源读取期间的撤权保留权限错误，其余固定领域错误按失败终态审计。
    except ApiError as exc:
        outcome = "permission_denied" if exc.code == "auth.permission_denied" else "failed"
        await _audit_and_raise(
            request,
            context,
            exc,
            query_type=parsed.query_type,
            target_request_id=parsed.target_request_id,
            outcome=outcome,
            permission_codes=permissions,
            started=started,
            request_id=request_id,
            trace_id=trace_id,
            request_meta=request_meta,
            governance_deadline=governance_deadline,
            background_tasks=background_tasks,
            enforce_permissions=exc.code != "auth.permission_denied",
        )

    try:
        persisted = await run_admin_assistant_persistence_worker(
            lambda connection: persist_attempt(
                request.app.state.config,
                context,
                query_type=parsed.query_type,
                target_request_id=parsed.target_request_id,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
                outcome="answered",
                failure_code=None,
                required_permission_codes=permissions,
                source_types=tuple(sorted(source.source_type for source in sources)),
                claim_count=len(claims),
                source_count=len(sources),
                redaction_count=len(provisional.redactions),
                request_id=request_id,
                trace_id=trace_id,
                request_meta=request_meta,
                started_monotonic=started,
                deadline=governance_deadline,
                expected_authorization_fingerprint=snapshot.authorization_fingerprint,
                enforce_permissions=True,
                connection=connection,
                invalidate_connection=lambda current: (
                    _retire_admin_assistant_runtime_write_connection(runtime, current)
                ),
            ),
            runtime=runtime,
            config=request.app.state.config,
            deadline=governance_deadline,
            attempt_owner=admin_assistant_request_owner(request),
        )
    # 最终事务会刷新认证和权限；此处专门处理快照后撤权与审计存储失败。
    except ApiError as exc:
        # 快照后撤权必须另写零正文 permission_denied，绝不能返回已构造回答。
        if exc.code == "auth.permission_denied":
            await _persist_terminal(
                request,
                context,
                query_type=parsed.query_type,
                target_request_id=parsed.target_request_id,
                outcome="permission_denied",
                failure_code="auth.permission_denied",
                permission_codes=permissions,
                started=started,
                request_id=request_id,
                trace_id=trace_id,
                request_meta=request_meta,
                governance_deadline=governance_deadline,
                background_tasks=background_tasks,
                enforce_permissions=False,
            )
            # 对外保留统一权限码，不附带事务或用户上下文。
            raise ApiError("auth.permission_denied") from None
        # 其他持久化错误统一收敛为审计不可用，已读取事实不进入错误响应。
        raise ApiError("admin_assistant.audit_unavailable") from None

    answer = build_assistant_answer(
        assistant_query_id=persisted.assistant_query_id,
        assistant_audit_id=persisted.assistant_audit_id,
        query_type=parsed.query_type,
        claims=claims,
        recommendations=recommendations,
        sources=sources,
        request_id=request_id,
        trace_id=trace_id,
    )
    validate_assistant_answer_wire(answer.to_wire())
    _after_commit(
        request,
        persisted,
        background_tasks=background_tasks,
        request_id=request_id,
        trace_id=trace_id,
    )
    return answer.to_wire()


def _answer_graph(
    parsed: ParsedAssistantQuery,
    snapshot: SourceSnapshot,
    *,
    window_started_at: datetime,
    window_ended_at: datetime,
) -> tuple[list[AnswerClaim], list[AnswerRecommendation], list[AnswerSource]]:
    """把一个已授权租户快照投影为 typed claim/recommendation/source 完整图。"""

    values = snapshot.values
    source_types = tuple(sorted(snapshot.record_counts))
    sources = [
        AnswerSource(
            source_type=source_type,  # type: ignore[arg-type]
            record_count=snapshot.record_counts[source_type],
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            snapshot_at=snapshot.snapshot_at,
        )
        for source_type in source_types
    ]
    # 用量意图只投影概览与用量账本的固定 claim 集合。
    if parsed.query_type == "usage_statistics":
        return _usage_graph(values), [], sources
    # 错误意图只返回分类计数，原始 Provider 错误和请求正文不进入图。
    if parsed.query_type == "error_explanation":
        return _error_graph(values), [], sources
    # 成本意图按冻结阈值生成机器码建议，不输出自由节省承诺。
    if parsed.query_type == "cost_advice":
        claims = _cost_graph(values)
        recommendations = _cost_recommendations(values)
        return claims, recommendations, sources
    claims = _policy_graph(values)
    recommendations = (
        [AnswerRecommendation("priority_collision_review", ("src_policies",))]
        if int(values["priority_collision_count"]) > 0
        else []
    )
    return claims, recommendations, sources


def _usage_graph(values: dict[str, object]) -> list[AnswerClaim]:
    """把概览与 Usage 聚合转换为固定十个 typed claim，不生成自由解释正文。"""

    request_count = int(values["request_count"])
    success_count = int(values["success_count"])
    return [
        _integer("request_count", "调用次数", request_count, "requests", "overview"),
        _integer("success_count", "成功次数", success_count, "requests", "overview"),
        _integer("failure_count", "失败次数", int(values["failure_count"]), "requests", "overview"),
        _percentage("success_rate", "成功率", success_count, request_count, "overview"),
        _integer(
            "average_latency_ms", "平均延迟", int(values["average_latency_ms"]), "ms", "overview"
        ),
        _integer("input_tokens", "输入 Token", int(values["input_tokens"]), "tokens", "usage"),
        _integer("output_tokens", "输出 Token", int(values["output_tokens"]), "tokens", "usage"),
        _integer("total_tokens", "总 Token", int(values["total_tokens"]), "tokens", "usage"),
        _decimal("cost_cny", "用量成本", int(values["cost_cny_micros"]), "CNY", "usage"),
        _integer(
            "estimated_usage_count",
            "估算记录数",
            int(values["estimated_count"]),
            "records",
            "usage",
        ),
    ]


def _error_graph(values: dict[str, object]) -> list[AnswerClaim]:
    """生成单请求安全错误分类或时间窗 Top-N 计数，不返回 Provider 原始错误。"""

    if "request_status" in values:
        error_code = safe_request_log_error_code(values["error_code"])
        return [
            _text(
                "request_status",
                "请求状态",
                str(values["request_status"]),
                "status",
                "request_logs",
            ),
            _text("error_code", "安全错误分类", error_code, "code", "request_logs"),
            *_diagnosis_claims("error", "错误", error_code),
        ]
    claims = [
        _integer(
            "failure_count", "失败次数", int(values["failure_count"]), "requests", "request_logs"
        )
    ]
    for index, (code, count) in enumerate(values["top_errors"], start=1):  # type: ignore[assignment]
        safe_code = safe_request_log_error_code(code)
        claims.extend(
            (
                _text(
                    f"error_{index}_code", f"错误分类 {index}", safe_code, "code", "request_logs"
                ),
                _integer(
                    f"error_{index}_count",
                    f"错误次数 {index}",
                    int(count),
                    "requests",
                    "request_logs",
                ),
                *_diagnosis_claims(f"error_{index}", f"错误 {index}", safe_code),
            )
        )
    return claims


def _diagnosis_claims(prefix: str, label: str, error_code: str) -> tuple[AnswerClaim, ...]:
    """从共享固定规则派生安全原因和建议，不向诊断器传入 Provider 原始 message。"""

    diagnosis = diagnose_error(error_code)
    return (
        _text(f"{prefix}_reason", f"{label}原因", diagnosis.reason, "text", "request_logs"),
        _text(
            f"{prefix}_suggestion",
            f"{label}处理建议",
            diagnosis.suggestion,
            "text",
            "request_logs",
        ),
    )


def _cost_graph(values: dict[str, object]) -> list[AnswerClaim]:
    """把跨财务来源的有界计数和微元事实转换为固定成本 claim。"""

    request_count = int(values["request_count"])
    failure_count = int(values["failure_count"])
    usage_count = int(values["usage_count"])
    estimated_count = int(values["estimated_count"])
    return [
        _integer("request_count", "调用次数", request_count, "requests", "overview"),
        _percentage("failure_rate", "失败率", failure_count, request_count, "overview"),
        _integer("usage_record_count", "用量记录数", usage_count, "records", "usage"),
        _percentage("estimated_cost_share", "估算记录占比", estimated_count, usage_count, "usage"),
        _decimal(
            "gross_debit_cost_cny",
            "已入账借方成本",
            int(values["gross_debit_cost_cny_micros"]),
            "CNY",
            "finops_ledger",
        ),
        _text(
            "top_cost_model",
            "最高成本模型",
            str(values["top_cost_model_alias"]),
            "model",
            "finops_ledger",
        ),
        _percentage(
            "top_cost_model_share",
            "最高成本模型占比",
            int(values["top_cost_model_cny_micros"]),
            int(values["gross_debit_cost_cny_micros"]),
            "finops_ledger",
        ),
        _integer(
            "active_budget_count",
            "生效预算数",
            int(values["active_budget_count"]),
            "rules",
            "budgets",
        ),
        _integer(
            "open_anomaly_count",
            "待处理成本异常",
            int(values["open_anomaly_count"]),
            "anomalies",
            "cost_anomalies",
        ),
        _integer(
            "model_price_count",
            "模型价格记录数",
            int(values["model_price_count"]),
            "records",
            "model_prices",
        ),
    ]


def _cost_recommendations(values: dict[str, object]) -> list[AnswerRecommendation]:
    """只按冻结阈值和已有来源生成 machine-code 建议，不承诺节省金额。"""

    recommendations: list[AnswerRecommendation] = []
    if _ratio(int(values["failure_count"]), int(values["request_count"])) >= Decimal("10"):
        recommendations.append(AnswerRecommendation("high_failure_rate", ("src_overview",)))
    # 估算成本记录占比达到固定阈值时仅提示补齐正式证据，不给出金额承诺。
    if _ratio(int(values["estimated_count"]), int(values["usage_count"])) >= Decimal("20"):
        recommendations.append(AnswerRecommendation("high_estimated_cost_share", ("src_usage",)))
    # posted debit 的最高模型占比达到 60% 才提示集中度；零成本窗口不能触发比例建议。
    gross_debit = int(values["gross_debit_cost_cny_micros"])
    if gross_debit > 0 and _ratio(int(values["top_cost_model_cny_micros"]), gross_debit) >= Decimal(
        "60"
    ):
        recommendations.append(
            AnswerRecommendation("high_model_cost_concentration", ("src_finops_ledger",))
        )
    if not recommendations:
        recommendations.append(
            AnswerRecommendation(
                "insufficient_evidence",
                ("src_finops_ledger", "src_model_prices"),
            )
        )
    return recommendations


def _policy_graph(values: dict[str, object]) -> list[AnswerClaim]:
    """投影策略计数、动作和可选请求决定，禁止返回 DSL、名称或自由条件。"""

    claims = [
        _integer(
            "active_policy_count",
            "生效策略数",
            int(values["active_policy_count"]),
            "policies",
            "policies",
        ),
        _integer(
            "priority_collision_count",
            "同优先级候选组",
            int(values["priority_collision_count"]),
            "groups",
            "policies",
        ),
        _integer(
            "published_snapshot_version",
            "已发布快照版本",
            int(values["published_snapshot_version"]),
            "version",
            "policy_snapshots",
        ),
    ]
    for index, (action, count) in enumerate(values["action_counts"], start=1):  # type: ignore[assignment]
        claims.extend(
            (
                _text(
                    f"policy_action_{index}", f"策略动作 {index}", str(action), "action", "policies"
                ),
                _integer(
                    f"policy_action_{index}_count",
                    f"策略动作数量 {index}",
                    int(count),
                    "policies",
                    "policies",
                ),
            )
        )
    if "request_policy_decision" in values:
        claims.extend(
            (
                _text(
                    "request_policy_decision",
                    "请求策略决定",
                    str(values["request_policy_decision"]),
                    "decision",
                    "request_explanations",
                ),
                _text(
                    "request_policy_reason_code",
                    "请求策略原因",
                    str(values["request_policy_reason_code"]),
                    "code",
                    "request_explanations",
                ),
                _text(
                    "request_policy_snapshot_status",
                    "请求策略快照状态",
                    str(values["request_policy_snapshot_status"]),
                    "status",
                    "request_explanations",
                ),
                _integer(
                    "request_snapshot_version",
                    "请求策略快照版本",
                    int(values["request_snapshot_version"]),
                    "version",
                    "request_explanations",
                ),
            )
        )
    return claims


def _integer(claim_id: str, label: str, value: int, unit: str, source: SourceType) -> AnswerClaim:
    """构造来源绑定的非负整数 claim，负聚合值安全收敛为零。"""

    return AnswerClaim(claim_id, "integer", label, max(0, value), unit, (f"src_{source}",))


def _text(claim_id: str, label: str, value: str, unit: str, source: SourceType) -> AnswerClaim:
    """构造仅承载固定分类文本的 claim，值仍由 Shared 公共文本合同复核。"""

    return AnswerClaim(claim_id, "text", label, value, unit, (f"src_{source}",))


def _decimal(claim_id: str, label: str, micros: int, unit: str, source: SourceType) -> AnswerClaim:
    """把整数微元转换为 canonical 十进制字符串，避免浮点金额和精度漂移。"""

    value = Decimal(max(0, micros)) / Decimal(1_000_000)
    return AnswerClaim(
        claim_id, "decimal", label, _canonical_decimal(value), unit, (f"src_{source}",)
    )


def _percentage(
    claim_id: str,
    label: str,
    numerator: int,
    denominator: int,
    source: SourceType,
) -> AnswerClaim:
    """按非负整数分子分母生成两位以内 canonical 百分比 claim。"""

    return AnswerClaim(
        claim_id,
        "percentage",
        label,
        _canonical_decimal(_ratio(numerator, denominator)),
        "percent",
        (f"src_{source}",),
    )


def _ratio(numerator: int, denominator: int) -> Decimal:
    """计算有界百分比；零或负分母固定返回零而不制造除零异常。"""

    if denominator <= 0:
        return Decimal(0)
    return (Decimal(max(0, numerator)) * Decimal(100) / Decimal(denominator)).quantize(
        Decimal("0.01")
    )


def _canonical_decimal(value: Decimal) -> str:
    """移除十进制尾零并保留零值，得到 Shared wire 接受的唯一金额表示。"""

    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


async def _audit_and_raise(
    request: Request,
    context: AuthContext,
    error: ApiError,
    *,
    governance_deadline: float,
    background_tasks: BackgroundTasks,
    **kwargs: object,
) -> None:
    """先持久化零正文终态审计，再把原固定 ApiError 交给统一中间件。"""

    await _persist_terminal(
        request,
        context,
        failure_code=error.code,
        governance_deadline=governance_deadline,
        background_tasks=background_tasks,
        **kwargs,
    )
    # 只有零正文终态审计提交成功后才抛出原固定错误，保证每次失败请求可追踪。
    raise error


async def _persist_terminal(
    request: Request,
    context: AuthContext,
    *,
    query_type: str,
    target_request_id: str | None = None,
    outcome: str,
    failure_code: str,
    permission_codes: tuple[str, ...],
    started: float,
    request_id: str,
    trace_id: str,
    request_meta: AuditRequestMeta,
    governance_deadline: float,
    background_tasks: BackgroundTasks,
    enforce_permissions: bool = True,
) -> None:
    """为拒绝、阻断、限流或失败写入一组零 claim/source 的不可变治理事实。"""

    try:
        try:
            _, runtime = admin_assistant_request_execution(request)
        # 缺失或关闭 runtime 时无法证明终态写连接 owner，必须提升为审计不可用。
        except ApiError:
            # 原业务错误不能在治理事实缺失时先返回给调用方。
            raise ApiError("admin_assistant.audit_unavailable") from None
        persisted = await run_admin_assistant_persistence_worker(
            lambda connection: persist_attempt(
                request.app.state.config,
                context,
                query_type=query_type,
                target_request_id=target_request_id,
                window_started_at=None,
                window_ended_at=None,
                outcome=outcome,
                failure_code=failure_code,
                required_permission_codes=tuple(sorted(set(permission_codes))),
                source_types=(),
                claim_count=0,
                source_count=0,
                redaction_count=0,
                request_id=request_id,
                trace_id=trace_id,
                request_meta=request_meta,
                started_monotonic=started,
                deadline=governance_deadline,
                expected_authorization_fingerprint=None,
                enforce_permissions=enforce_permissions,
                connection=connection,
                invalidate_connection=lambda current: (
                    _retire_admin_assistant_runtime_write_connection(runtime, current)
                ),
            ),
            runtime=runtime,
            config=request.app.state.config,
            deadline=governance_deadline,
            attempt_owner=admin_assistant_request_owner(request),
        )
    # 终态审计本身失败时只允许一次撤权降级写入，其他错误必须阻断原响应。
    except ApiError as exc:
        # 最终事务发现撤权时，已认证 actor 仍需一条拒绝审计；第二次写入只刷新认证，
        # 不重新授予业务权限，也不携带已构造的回答或来源。
        if enforce_permissions and exc.code == "auth.permission_denied":
            return await _persist_terminal(
                request,
                context,
                query_type=query_type,
                target_request_id=target_request_id,
                outcome="permission_denied",
                failure_code="auth.permission_denied",
                permission_codes=permission_codes,
                started=started,
                request_id=request_id,
                trace_id=trace_id,
                request_meta=request_meta,
                governance_deadline=governance_deadline,
                background_tasks=background_tasks,
                enforce_permissions=False,
            )
        # 无法落下完整失败审计时返回固定审计错误，不暴露持久层异常。
        raise ApiError("admin_assistant.audit_unavailable") from None
    _after_commit(
        request,
        persisted,
        background_tasks=background_tasks,
        request_id=request_id,
        trace_id=trace_id,
    )


def _after_commit(
    request: Request,
    persisted: PersistedAttempt,
    *,
    background_tasks: BackgroundTasks,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后隔离发送通用审计，并唤醒只领取 AssistantAudit source 的调度器。"""

    from .scheduler import wake_admin_assistant_scheduler

    # 专用 durable source 已在事务内提交，先立即唤醒恢复 worker；通用 audit.recorded 是
    # 尽力副作用，交给响应后台线程，慢 Endpoint 不得阻塞 async event loop 或延迟 wake。
    wake_admin_assistant_scheduler(request.app)
    background_tasks.add_task(
        dispatch_generic_audit,
        request.app.state.config,
        persisted,
        request_id=request_id,
        trace_id=trace_id,
    )


def _consume_rate_limit(context: AuthContext) -> bool:
    """原子消费 tenant/actor 配额，并在 key registry 容量耗尽时关闭式拒绝。"""

    now = time.monotonic()
    actor_key = (context.tenant.tenant_id, context.user.user_id)
    with _limiter_lock:
        _reclaim_expired_rate_limit_keys(_actor_attempts, now)
        _reclaim_expired_rate_limit_keys(_tenant_attempts, now)
        actor = _actor_attempts.get(actor_key)
        tenant = _tenant_attempts.get(context.tenant.tenant_id)

        if (
            type(_RATE_LIMIT_ACTOR_KEY_CAPACITY) is not int
            or type(_RATE_LIMIT_TENANT_KEY_CAPACITY) is not int
            or _RATE_LIMIT_ACTOR_KEY_CAPACITY <= 0
            or _RATE_LIMIT_TENANT_KEY_CAPACITY <= 0
        ):
            # 非法容量配置会破坏内存上界，必须在创建任何 tenant/actor key 前拒绝。
            return False
        if actor is None and len(_actor_attempts) >= _RATE_LIMIT_ACTOR_KEY_CAPACITY:
            # 新 actor 不能挤掉窗口内活跃主体，否则可绕过每主体配额。
            return False
        if tenant is None and len(_tenant_attempts) >= _RATE_LIMIT_TENANT_KEY_CAPACITY:
            # 新 tenant 达到 registry 上限时关闭式拒绝，且不得留下半个 actor key。
            return False
        actor_size = len(actor) if actor is not None else 0
        tenant_size = len(tenant) if tenant is not None else 0
        # actor 与 tenant 任一滑动窗口达到上限都拒绝本次尝试，两个计数仍不保存正文。
        if (
            actor_size >= _ACTOR_RATE_LIMIT_PER_WINDOW
            or tenant_size >= _TENANT_RATE_LIMIT_PER_WINDOW
        ):
            return False
        if actor is None:
            actor = deque()
            _actor_attempts[actor_key] = actor
        # tenant 队列只在两类容量检查均通过后创建，确保 registry 原子一致。
        if tenant is None:
            tenant = deque()
            _tenant_attempts[context.tenant.tenant_id] = tenant
        actor.append(now)
        tenant.append(now)
        return True


def _reclaim_expired_rate_limit_keys[K](
    registry: dict[K, deque[float]],
    now: float,
) -> None:
    """回收窗口内已无 attempt 的 key，确保有界 registry 容量可被后续主体复用。"""

    for key, values in tuple(registry.items()):
        _expire(values, now)
        if not values:
            # 仅删除完整过期的空队列，活跃主体的滑动窗口顺序保持不变。
            del registry[key]


def _expire(values: deque[float], now: float) -> None:
    """从单调时钟队列移除满一分钟的 attempt，保持固定滑动窗口边界。"""

    while values and now - values[0] >= _RATE_LIMIT_WINDOW_SECONDS:
        values.popleft()


def _tenant_execution_lock(
    runtime: _LoopRuntime,
    tenant_id: str,
    *,
    attempt_owner: object | None = None,
) -> asyncio.Lock:
    """在当前 loop runtime 内按 tenant 弱引用复用执行 owner。"""

    if (
        runtime.loop is not asyncio.get_running_loop()
        or runtime.closed
        or (runtime.closing and not _runtime_owns_attempt(runtime, attempt_owner))
    ):
        # seal 后只允许已登记 attempt 继续取得 tenant owner，未知工作不得进入来源域。
        raise ApiError("admin_assistant.source_unavailable")
    lock = runtime.tenant_locks.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        runtime.tenant_locks[tenant_id] = lock
    return lock


__all__ = [
    "admin_assistant_runtime_read_connection",
    "admin_assistant_request_execution",
    "retire_admin_assistant_runtime_read_connection",
    "clear_admin_assistant_execution_runtime",
    "execute_admin_assistant_query",
    "initialize_admin_assistant_execution_runtime",
    "prepare_admin_assistant_request",
    "run_admin_assistant_persistence_worker",
    "run_admin_assistant_worker",
]
