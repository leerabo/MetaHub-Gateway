"""FinOps Usage 本地持久恢复队列。

该模块只持久化账本恢复所需的安全白名单字段，不保存 raw usage、Prompt、Response、
Provider Endpoint、Key、Token 或人工调整原因。文件完整性使用独立域 HMAC；pending、
processing 和 quarantine 之间只通过同文件系统原子操作迁移，避免进程崩溃形成半条 Usage。
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import hmac
import json
import logging
import os
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.webhooks import WebhookEvent

from metahub_gateway.events import enqueue_runtime_event

logger = logging.getLogger("metahub.gateway.finops_spool")

# 单条记录硬上限同时约束内存、磁盘和恶意内部载荷；扩大上限必须重新审查敏感字段集合。
MAX_SPOOL_RECORD_BYTES = 16 * 1024
# 每轮最多恢复 64 条，防止数据库恢复后单个 Gateway worker 长时间占用 SQLite 写锁。
REPLAY_BATCH_SIZE = 64
# Windows 上安全软件或文件系统过滤驱动可能让刚完成原子发布的目录枚举短暂返回空；
# 仅在第一次空扫描时做极小的有界重试，避免把已落盘记录无故推迟到下一轮恢复。
_WINDOWS_EMPTY_SCAN_RETRIES = 2
_WINDOWS_EMPTY_SCAN_RETRY_SECONDS = 0.01
# 同进程已确认发布的 Windows hard-link 目录项偶尔会在精确 rename 时继续短暂返回
# FileNotFound；该路径只发生在数据库故障落盘恢复，不进入正常模型请求成功热路径。
_WINDOWS_PUBLISHED_RENAME_RETRIES = 20
_WINDOWS_PUBLISHED_RENAME_RETRY_SECONDS = 0.025
# 热路径写入和后台 replay 会构造不同 UsageSpool 实例；同进程登记必须跨实例共享，
# 才能只重试本进程刚发布的路径，同时避免把普通多 worker 竞争误判为可见性延迟。
_PUBLISHED_PENDING: set[Path] = set()
_PUBLISHED_PENDING_LOCK = Lock()
# HMAC 域和文件名域必须不同，避免同一密钥下的摘要被跨用途复用。
_CONTENT_HMAC_DOMAIN = b"metahub.finops.usage-spool.v1\x00content\x00"
_FILENAME_HMAC_DOMAIN = b"metahub.finops.usage-spool.v1\x00filename\x00"
_SCHEMA_VERSION = 1
# 文件信封版本与业务 payload 版本独立演进：v2 只为请求上下文增加不可逆的 mask
# 审计元数据。Gateway 升级期间必须继续接受已落盘的 v1，同时允许新进程持久化 v2；
# 未知未来版本仍失败关闭，避免旧进程宽松解释新增敏感字段。
# 文件信封版本保持 v1；内部 Usage payload 精确兼容 v1-v5。v5 只增加经过严格
# 校验的输出扫描 ID/状态/结算意图，不包含正文、pattern 或命中片段；未知未来版本仍
# 失败关闭，避免旧进程忽略尚未完成安全审查的新字段。
_SUPPORTED_PAYLOAD_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5, 6})
_LOWER_HEX = frozenset("0123456789abcdef")


class UsageSpoolError(RuntimeError):
    """表示 spool 安全、完整性或原子文件操作失败。

    对外调用方只能记录该异常类型和固定阶段，不得输出异常正文，因为底层异常可能包含路径。
    """


@dataclass(frozen=True)
class ClaimedUsageRecord:
    """已经由当前 worker 原子领取且通过 HMAC/文件名校验的一条记录。"""

    path: Path
    digest: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class UsageSpoolReplaySummary:
    """一次有界恢复扫描的安全计数摘要，不包含路径、租户或请求正文。"""

    claimed: int = 0
    committed: int = 0
    existing: int = 0
    deferred: int = 0
    events_enqueued: int = 0
    available: bool = True


@dataclass(frozen=True)
class UsageSpoolReplayBatch:
    """工作线程返回的恢复结果；事件必须回到所属 asyncio loop 后才能进入 Queue。"""

    summary: UsageSpoolReplaySummary
    events: tuple[WebhookEvent, ...] = ()


class UsageSpool:
    """管理单 Gateway 实例共享的 Usage spool 文件状态机。

    参数：运行配置提供专属本地目录、HMAC 密钥和 claim 租期。
    约束：每次公开操作都会重新检查目录非重解析且由当前服务账号持有，不能依赖启动时检查。
    """

    def __init__(self, config: AppConfig) -> None:
        """冻结配置中的绝对根目录和域隔离密钥，不创建文件。"""

        self.root = Path(config.finops_usage_spool_root)
        self.secret = config.auth_token_secret.encode("utf-8")
        self.claim_lease_seconds = config.finops_usage_spool_claim_lease_seconds
        self.worker_token = uuid4().hex
        # 没有 HMAC 密钥时禁止退化成普通 checksum，否则本地写权限可伪造跨租户 Usage。
        if not self.secret:
            raise UsageSpoolError("finops_spool_secret_missing")

    @property
    def pending(self) -> Path:
        """返回尚未领取记录目录；只有原子发布完成的正式 JSON 才能进入该目录。"""

        return self.root / "pending"

    @property
    def processing(self) -> Path:
        """返回已经原子领取记录目录。"""

        return self.root / "processing"

    @property
    def quarantine(self) -> Path:
        """返回真实性或 schema 校验失败的隔离目录。"""

        return self.root / "quarantine"

    def prepare(self) -> None:
        """创建并复核专属目录，拒绝网络路径、symlink/reparse 和其他账号 owner。"""

        _assert_local_absolute_root(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (self.root, self.pending, self.processing, self.quarantine):
            directory.mkdir(mode=0o700, exist_ok=True)
            _assert_private_directory(directory)
        _assert_no_reparse_ancestors(self.root)

    def store(self, payload: dict[str, Any]) -> bool:
        """原子持久化一条安全记录；同 tenant+Usage ID 同载荷重试返回 False。

        返回 True 表示本次发布了新 pending 文件，False 表示完全相同记录已存在。
        同标识不同载荷、超限、HMAC/路径或 fsync 失败均抛 ``UsageSpoolError``，禁止覆盖。
        """

        self.prepare()
        tenant_id, usage_ledger_id = _payload_identity(payload)
        digest = self._filename_digest(tenant_id, usage_ledger_id)
        payload_bytes = _canonical_json_bytes(payload)
        envelope = {
            "schema_version": _SCHEMA_VERSION,
            "payload": payload,
            "hmac_sha256": self._content_hmac(payload_bytes),
        }
        record_bytes = _canonical_json_bytes(envelope)
        # 单条恢复记录设置硬上限，防止故障路径被大对象放大为磁盘或内存压力。
        if len(record_bytes) > MAX_SPOOL_RECORD_BYTES:
            raise UsageSpoolError("finops_spool_record_too_large")
        temporary = self.pending / f".{digest}.{uuid4().hex}.tmp"
        final = self.pending / f"{digest}.json"
        _exclusive_write(temporary, record_bytes)
        try:
            try:
                # 同目录 hard-link 发布不会覆盖既有文件；创建成功后删临时名仍保留同一 inode。
                os.link(temporary, final)
                _fsync_directory(self.pending)
                self._remember_pending(final)
                return True
            # 同身份文件已经存在时必须读取并比较完整 canonical payload，不能直接视为幂等。
            except FileExistsError:
                existing = self._read_record(final, expected_digest=digest)
                if _canonical_json_bytes(existing) != payload_bytes:
                    # tenant+Usage 身份相同但载荷不同表示冲突，禁止覆盖既有恢复事实。
                    raise UsageSpoolError("finops_spool_identity_conflict") from None
                self._remember_pending(final)
                return False
            # 发布失败统一转为安全内部码，不把宿主机路径或系统错误正文写入日志。
            except OSError as exc:
                raise UsageSpoolError("finops_spool_publish_failed") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            # 临时文件清理失败不应删除或回滚已经 fsync 发布的正式记录。
            except OSError:
                # 临时名不参与扫描；清理失败由后续运维回收，不能删除已成功发布的正式记录。
                pass

    def claim_batch(self, *, limit: int = REPLAY_BATCH_SIZE) -> list[ClaimedUsageRecord]:
        """恢复超租期 claim，并按稳定文件名顺序原子领取有限 pending 记录。"""

        # 批量上限只接受真实整数并受常量约束，避免 bool 或超大值扩大恢复事务。
        if type(limit) is not int or not 1 <= limit <= REPLAY_BATCH_SIZE:
            raise ValueError("finops_spool_batch_invalid")
        self.prepare()
        self._recover_stale_claims()
        claims: list[ClaimedUsageRecord] = []
        for pending_path in self._pending_records():
            if len(claims) >= limit:
                break
            digest = pending_path.stem
            if not _is_lower_hex(digest, length=64):
                self._quarantine_path(pending_path, digest="invalid-name")
                continue
            claimed_at = int(time.time())
            claim_path = self.processing / (
                f"{digest}.{claimed_at}.{self.worker_token}.{uuid4().hex}.claim"
            )
            remembered = pending_path in self._remembered_pending()
            rename_retries = (
                _WINDOWS_PUBLISHED_RENAME_RETRIES
                if remembered and _is_windows_platform()
                else 0
            )
            claimed = False
            try:
                for rename_attempt in range(rename_retries + 1):
                    try:
                        os.rename(pending_path, claim_path)
                        claimed = True
                        break
                    # 文件消失通常表示另一 worker 已领取；仅同进程刚发布的 Windows 路径
                    # 允许等待目录项完成可见，且始终复用同一个不可覆盖 claim 目标。
                    except FileNotFoundError:
                        if rename_attempt >= rename_retries:
                            break
                        time.sleep(_WINDOWS_PUBLISHED_RENAME_RETRY_SECONDS)
                    # 其他 rename 错误可能破坏 claim 原子性，统一失败而不继续读取源文件。
                    except OSError as exc:
                        raise UsageSpoolError("finops_spool_claim_failed") from exc
            finally:
                self._forget_pending(pending_path)
            if not claimed:
                continue
            try:
                payload = self._read_record(claim_path, expected_digest=digest)
            # 已领取记录真实性失败时只能隔离，不能归还 pending 反复重试。
            except UsageSpoolError:
                self._quarantine_path(claim_path, digest=digest)
                continue
            claims.append(ClaimedUsageRecord(claim_path, digest, payload))
        return claims

    def _pending_records(self) -> list[Path]:
        """按稳定顺序读取 pending 记录，并收敛 Windows 的短暂空枚举窗口。"""

        records = set(self.pending.glob("*.json")) | self._remembered_pending()
        if records or not _is_windows_platform():
            return sorted(records)
        for _ in range(_WINDOWS_EMPTY_SCAN_RETRIES):
            time.sleep(_WINDOWS_EMPTY_SCAN_RETRY_SECONDS)
            records = set(self.pending.glob("*.json")) | self._remembered_pending()
            if records:
                return sorted(records)
        # 高 I/O 负载下 Windows 的通配枚举可能连续返回陈旧空结果；改用一次直接目录枚举，
        # 仍只接收固定后缀并由后续文件名、类型和 HMAC 校验决定是否领取或隔离。
        _assert_private_directory(self.pending)
        records = {
            path for path in self.pending.iterdir() if path.name.endswith(".json")
        }
        return sorted(records | self._remembered_pending())

    def _remember_pending(self, path: Path) -> None:
        """跨实例登记本进程已确认发布的精确路径，绕过 Windows 陈旧目录枚举。"""

        with _PUBLISHED_PENDING_LOCK:
            _PUBLISHED_PENDING.add(path)

    def _forget_pending(self, path: Path) -> None:
        """领取或隔离后清除进程内提示；持久文件状态仍是唯一恢复事实。"""

        with _PUBLISHED_PENDING_LOCK:
            _PUBLISHED_PENDING.discard(path)

    def _remembered_pending(self) -> set[Path]:
        """返回当前 spool 的提示快照，避免持锁执行文件系统操作。"""

        with _PUBLISHED_PENDING_LOCK:
            return {path for path in _PUBLISHED_PENDING if path.parent == self.pending}

    def complete(self, claim: ClaimedUsageRecord) -> None:
        """仅在数据库已提交或确认既有正式分录后删除当前 worker 的 claim。"""

        self.prepare()
        _assert_child_path(claim.path, self.processing)
        try:
            claim.path.unlink()
            _fsync_directory(self.processing)
        # 删除失败保留 claim 供下次 existing 分支确认，不能回滚已提交账本。
        except OSError as exc:
            # 删除失败会导致下次走 existing 幂等分支，但绝不能回滚已经提交的账本。
            raise UsageSpoolError("finops_spool_complete_failed") from exc

    def release(self, claim: ClaimedUsageRecord) -> None:
        """数据库暂不可用时把 claim 原子归还 pending；冲突载荷双边隔离。"""

        self.prepare()
        _assert_child_path(claim.path, self.processing)
        pending_path = self.pending / f"{claim.digest}.json"
        try:
            os.link(claim.path, pending_path)
            self._remember_pending(pending_path)
        # pending 同身份文件已存在时必须同时校验真实性和载荷一致性。
        except FileExistsError:
            try:
                existing = self._read_record(pending_path, expected_digest=claim.digest)
            # 任一现存记录损坏时双边隔离，不能选择其中一份继续入账。
            except UsageSpoolError:
                try:
                    self._quarantine_path(pending_path, digest=claim.digest)
                    self._quarantine_path(claim.path, digest=claim.digest)
                finally:
                    self._forget_pending(pending_path)
                return
            if _canonical_json_bytes(existing) != _canonical_json_bytes(claim.payload):
                try:
                    self._quarantine_path(pending_path, digest=claim.digest)
                    self._quarantine_path(claim.path, digest=claim.digest)
                finally:
                    self._forget_pending(pending_path)
                return
            self._remember_pending(pending_path)
        # hard-link 归还失败时保留原 claim，避免丢失唯一恢复副本。
        except OSError as exc:
            raise UsageSpoolError("finops_spool_release_failed") from exc
        try:
            claim.path.unlink(missing_ok=True)
            _fsync_directory(self.pending)
            _fsync_directory(self.processing)
        # 目录同步或 claim 清理失败必须显式报告，不能假装归还已持久完成。
        except OSError as exc:
            raise UsageSpoolError("finops_spool_release_cleanup_failed") from exc

    def _recover_stale_claims(self) -> None:
        """仅回收超过冻结租期的 processing 文件，避免抢占仍在提交数据库的 worker。"""

        threshold = time.time() - self.claim_lease_seconds
        for path in sorted(self.processing.glob("*.claim")):
            try:
                if path.stat().st_mtime > threshold:
                    continue
                digest = path.name.split(".", 1)[0]
                if not _is_lower_hex(digest, length=64):
                    self._quarantine_path(path, digest="invalid-name")
                    continue
                payload = self._read_record(path, expected_digest=digest)
                self.release(ClaimedUsageRecord(path, digest, payload))
            # 扫描期间文件被其他 worker 移走属于正常竞争，当前轮直接跳过。
            except FileNotFoundError:
                continue
            # 过期 claim 合同损坏时隔离，避免每轮恢复重复处理同一坏文件。
            except UsageSpoolError:
                if path.exists():
                    self._quarantine_path(path, digest="invalid-record")

    def _read_record(self, path: Path, *, expected_digest: str) -> dict[str, Any]:
        """按大小、文件类型、重复键、HMAC、schema 和文件名身份顺序读取记录。"""

        _assert_regular_private_file(path)
        try:
            record_bytes = path.read_bytes()
        # 文件系统读取异常只映射固定内部码，路径与系统正文不得穿透日志边界。
        except OSError as exc:
            raise UsageSpoolError("finops_spool_read_failed") from exc
        # 空记录和超上限记录都不进入 JSON 解析，避免含糊错误和资源放大。
        if not record_bytes or len(record_bytes) > MAX_SPOOL_RECORD_BYTES:
            raise UsageSpoolError("finops_spool_record_size_invalid")
        try:
            envelope = json.loads(
                record_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
        # UTF-8、JSON 或重复键失败统一视为不可信记录，绝不尝试宽松修复。
        except (UnicodeDecodeError, json.JSONDecodeError, UsageSpoolError) as exc:
            raise UsageSpoolError("finops_spool_json_invalid") from exc
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema_version",
            "payload",
            "hmac_sha256",
        }:
            # 信封字段必须精确相等，未知扩展不能被旧进程静默忽略。
            raise UsageSpoolError("finops_spool_envelope_invalid")
        # 信封版本不匹配时禁止猜测解释未来或历史格式。
        if envelope["schema_version"] != _SCHEMA_VERSION:
            raise UsageSpoolError("finops_spool_version_invalid")
        payload = envelope["payload"]
        # 业务正文必须为对象，标量或数组无法满足冻结字段合同。
        if not isinstance(payload, dict):
            raise UsageSpoolError("finops_spool_payload_invalid")
        payload_bytes = _canonical_json_bytes(payload)
        signature = envelope["hmac_sha256"]
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, self._content_hmac(payload_bytes)
        ):
            # 内容 HMAC 不匹配说明记录可能被修改，不能进入任何数据库查询。
            raise UsageSpoolError("finops_spool_hmac_invalid")
        tenant_id, usage_ledger_id = _payload_identity(payload)
        if not hmac.compare_digest(
            expected_digest, self._filename_digest(tenant_id, usage_ledger_id)
        ):
            # 文件名摘要必须绑定同一 tenant+Usage，防止跨身份替换合法信封。
            raise UsageSpoolError("finops_spool_filename_invalid")
        return payload

    def _content_hmac(self, payload_bytes: bytes) -> str:
        """生成内容真实性 HMAC；普通 checksum 不具备本地伪造防护能力。"""

        return hmac.new(
            self.secret, _CONTENT_HMAC_DOMAIN + payload_bytes, hashlib.sha256
        ).hexdigest()

    def _filename_digest(self, tenant_id: str, usage_ledger_id: str) -> str:
        """把租户和 Usage ID 隐藏在域隔离文件名摘要中，目录名不暴露业务标识。"""

        identity = tenant_id.encode("utf-8") + b"\x00" + usage_ledger_id.encode("utf-8")
        return hmac.new(
            self.secret, _FILENAME_HMAC_DOMAIN + identity, hashlib.sha256
        ).hexdigest()

    def _quarantine_path(self, path: Path, *, digest: str) -> None:
        """把未知或冲突文件原子移入隔离区，不解析正文也不输出原路径。"""

        if not path.exists():
            return
        safe_digest = digest if _is_lower_hex(digest, length=64) else "invalid"
        destination = self.quarantine / (
            f"{safe_digest}.{int(time.time())}.{uuid4().hex}.bad"
        )
        try:
            os.rename(path, destination)
            _fsync_directory(self.quarantine)
        # 隔离失败必须报告，不能让不可信文件留在可恢复目录后继续扫描。
        except OSError as exc:
            raise UsageSpoolError("finops_spool_quarantine_failed") from exc


def store_usage_spool_record(config: AppConfig, payload: dict[str, Any]) -> bool:
    """为 Gateway 热路径提供一次同步、原子且经过 fsync 的 spool 写入。"""

    return UsageSpool(config).store(payload)


async def replay_usage_spool_batch(
    config: AppConfig,
    app_state: object,
    *,
    limit: int = REPLAY_BATCH_SIZE,
) -> UsageSpoolReplaySummary:
    """在线程中恢复 SQLite/文件事实，再回到当前事件循环非阻塞入队。

    参数：运行配置、拥有 runtime queue 的 app state 和批上限。返回：安全计数摘要。
    约束：``asyncio.Queue`` 只能由其事件循环线程访问，工作线程只返回不可变事件对象。
    """

    batch = await asyncio.to_thread(_collect_usage_spool_batch, config, limit=limit)
    enqueued_count = sum(
        1 for event in batch.events if enqueue_runtime_event(app_state, event)
    )
    return replace(batch.summary, events_enqueued=enqueued_count)


def _collect_usage_spool_batch(
    config: AppConfig,
    *,
    limit: int = REPLAY_BATCH_SIZE,
) -> UsageSpoolReplayBatch:
    """在工作线程领取并提交有限 Usage，只收集事件而不触碰 asyncio Queue。"""

    try:
        spool = UsageSpool(config)
        claims = spool.claim_batch(limit=limit)
    # 领取阶段异常只生成安全不可用摘要，后台 worker 不能因单轮故障永久退出。
    except Exception as exc:
        _log_spool_unavailable(exc, stage="claim")
        return UsageSpoolReplayBatch(
            summary=UsageSpoolReplaySummary(available=False)
        )
    committed_count = 0
    existing_count = 0
    deferred_count = 0
    events: list[WebhookEvent] = []
    # 延迟导入打破 ledger -> spool 写入函数与 spool -> ledger 重放函数的模块环。
    from metahub_gateway.usage.ledger import replay_usage_spool_record

    for claim in claims:
        try:
            committed = replay_usage_spool_record(config, claim.payload)
        # 单条数据库恢复失败只延后该 claim，后续记录仍应继续处理。
        except Exception as exc:
            deferred_count += 1
            try:
                spool.release(claim)
            # 归还失败已经由 spool 自身安全记录，不能覆盖原重放异常或终止整批。
            except UsageSpoolError as release_exc:
                _log_spool_unavailable(release_exc, stage="release")
            _log_replay_deferred(claim.payload, exc)
            continue
        try:
            spool.complete(claim)
        # 数据库事实已经提交，claim 清理失败只能留待 existing 幂等分支处理。
        except UsageSpoolError as exc:
            # 数据库已经提交；保留 claim 让下次 existing 分支清理，不能重复制造新事件。
            _log_spool_unavailable(exc, stage="complete")
        if committed.created:
            committed_count += 1
            event = _build_replayed_event(committed)
            if event is not None:
                events.append(event)
        else:
            existing_count += 1
    return UsageSpoolReplayBatch(
        summary=UsageSpoolReplaySummary(
            claimed=len(claims),
            committed=committed_count,
            existing=existing_count,
            deferred=deferred_count,
        ),
        events=tuple(events),
    )


def _build_replayed_event(committed: object) -> WebhookEvent | None:
    """从本次新建正式分录构造安全事件；失败时保留账本并让 worker 继续处理后续 claim。"""

    try:
        return committed.to_webhook_event()
    except Exception as exc:
        # 不记录异常正文和事件 payload，避免数据库值或未来扩展字段进入日志。
        logger.warning(
            "finops.spool_event_build_failed",
            extra={
                "event": "finops.spool_event_build_failed",
                "tenant_id": getattr(committed, "tenant_id", None),
                "trace_id": getattr(committed, "operation_trace_id", None),
                "object_id": getattr(committed, "ledger_entry_id", None),
                "error_code": "webhook.delivery_unavailable",
                "payload": {"exception_type": exc.__class__.__name__},
            },
        )
        return None


def install_usage_spool_recovery(app: FastAPI) -> None:
    """组合 Gateway lifespan：事件队列就绪后先恢复一批，再启动有界周期 worker。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """在既有生命周期内管理恢复 worker，退出时先停 worker 再让事件队列排空。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            current_app.state.finops_usage_spool_summary = await replay_usage_spool_batch(
                current_app.state.config, current_app.state
            )
            worker = asyncio.create_task(
                _spool_worker(current_app, stop_event), name="gateway-finops-usage-spool"
            )
            try:
                yield
            finally:
                stop_event.set()
                await worker
                current_app.state.finops_usage_spool_summary = None

    app.router.lifespan_context = combined_lifespan


async def _spool_worker(app: FastAPI, stop_event: asyncio.Event) -> None:
    """按有界间隔在线程中执行同步 SQLite/文件恢复，避免阻塞 ASGI 事件循环。"""

    interval = app.state.config.finops_usage_spool_poll_interval_seconds
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        # 超时仅表示到达下一轮轮询时刻，不是 worker 故障或取消信号。
        except TimeoutError:
            app.state.finops_usage_spool_summary = await replay_usage_spool_batch(
                app.state.config, app.state
            )


def _payload_identity(payload: dict[str, Any]) -> tuple[str, str]:
    """执行 spool 顶层严格 schema，并返回用于域隔离文件名的 tenant/Usage 身份。"""

    # 顶层字段白名单精确冻结，防止新增数据绕过敏感字段筛选后落盘。
    if set(payload) != {"schema_version", "context", "usage", "estimate"}:
        raise UsageSpoolError("finops_spool_payload_schema_invalid")
    # payload 自身版本与信封版本独立校验；只接受显式审计过的历史/当前版本。
    if payload["schema_version"] not in _SUPPORTED_PAYLOAD_SCHEMA_VERSIONS:
        raise UsageSpoolError("finops_spool_payload_version_invalid")
    context = payload["context"]
    usage = payload["usage"]
    # 上下文和用量都必须是对象，避免宽松取值把损坏记录解释为空身份。
    if not isinstance(context, dict) or not isinstance(usage, dict):
        raise UsageSpoolError("finops_spool_payload_schema_invalid")
    tenant_id = context.get("tenant_id")
    request_id = context.get("request_id")
    trace_id = context.get("trace_id")
    usage_ledger_id = usage.get("usage_ledger_id")
    if not all(
        _is_bounded_safe_text(item, maximum=128)
        for item in (tenant_id, request_id, trace_id)
    ):
        # tenant/request/trace 任一非法都使所有权无法可靠核验，整条记录必须拒绝。
        raise UsageSpoolError("finops_spool_identity_invalid")
    if (
        not isinstance(usage_ledger_id, str)
        or not usage_ledger_id.startswith("usg_")
        or not _is_lower_hex(usage_ledger_id[4:], length=32)
    ):
        # Usage ID 必须符合内部对象格式，防止其他资源身份复用文件名域。
        raise UsageSpoolError("finops_spool_identity_invalid")
    # 返回值只供 HMAC 文件名域使用，后续数据库仍会复核 tenant owner。
    return tenant_id, usage_ledger_id


def _canonical_json_bytes(value: object) -> bytes:
    """生成禁止 NaN/Infinity 的稳定 UTF-8 JSON，供 HMAC、比较和磁盘正文共用。"""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    # 非 JSON 类型和非有限数字统一拒绝，避免签名与后续解析采用不同表示。
    except (TypeError, ValueError) as exc:
        raise UsageSpoolError("finops_spool_json_invalid") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝 JSON 重复键，防止 HMAC 校验与业务解析选择不同字段值。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # 重复键会让不同 JSON 实现选择 first/last 不同值，必须在验签后仍失败关闭。
            raise UsageSpoolError("finops_spool_duplicate_key")
        result[key] = value
    return result


def _exclusive_write(path: Path, content: bytes) -> None:
    """使用 exclusive-create、flush 和 fsync 写完整临时文件，不允许跟随链接。"""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    # exclusive-create、写入或 fsync 任一失败都不能留下可发布的半记录。
    except OSError as exc:
        raise UsageSpoolError("finops_spool_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_local_absolute_root(root: Path) -> None:
    """拒绝相对路径、UNC/网络共享和文件系统根，缩小误扫描与远端替换风险。"""

    text = str(root)
    if (
        not root.is_absolute()
        or text.startswith(("\\\\", "//"))
        or root == Path(root.anchor)
    ):
        # 相对、网络或卷根路径无法形成服务账号专属边界，启动时直接拒绝。
        raise UsageSpoolError("finops_spool_root_invalid")


def _assert_no_reparse_ancestors(path: Path) -> None:
    """检查从专属目录到卷根的所有既有父路径均不是 symlink/reparse point。"""

    current = path
    while True:
        if current.exists() and _is_reparse_or_symlink(current):
            # 任一祖先重解析点都可能把后续操作导向边界外目录，必须失败关闭。
            raise UsageSpoolError("finops_spool_reparse_rejected")
        if current == Path(current.anchor):
            return
        current = current.parent


def _is_windows_platform() -> bool:
    """隔离 Windows 枚举兼容分支，使跨平台测试无需修改全局 ``os.name``。"""

    return os.name == "nt"


def _assert_private_directory(path: Path) -> None:
    """确认目录是真实目录、当前账号 owner，POSIX 下还必须拒绝组/其他账号权限。"""

    try:
        metadata = path.lstat()
    # 无法读取安全元数据时不能假定目录可信，也不回显真实宿主机路径。
    except OSError as exc:
        raise UsageSpoolError("finops_spool_directory_unavailable") from exc
    # 目录类型或 reparse 检查失败意味着可能发生路径替换，禁止继续访问。
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_or_symlink(path, metadata=metadata):
        raise UsageSpoolError("finops_spool_directory_invalid")
    # owner 必须是当前服务账号，防止其他本地账号伪造跨租户恢复记录。
    if not _owner_is_current(path, metadata=metadata):
        raise UsageSpoolError("finops_spool_owner_invalid")
    # POSIX 目录禁止组和其他账号权限，Windows 则由 SID owner 与 ACL 边界负责。
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise UsageSpoolError("finops_spool_permissions_invalid")


def _assert_regular_private_file(path: Path) -> None:
    """读取前重验普通文件、owner、权限和 reparse，避免路径在启动检查后被替换。"""

    try:
        metadata = path.lstat()
    # 文件元数据不可读时拒绝恢复，不能在竞态窗口继续读取未知对象。
    except OSError as exc:
        raise UsageSpoolError("finops_spool_file_unavailable") from exc
    # 只接受普通且非 reparse 文件，FIFO、目录、链接和设备文件全部拒绝。
    if not stat.S_ISREG(metadata.st_mode) or _is_reparse_or_symlink(path, metadata=metadata):
        raise UsageSpoolError("finops_spool_file_invalid")
    # 每条文件也必须由当前账号持有，不能只信任父目录 owner。
    if not _owner_is_current(path, metadata=metadata):
        raise UsageSpoolError("finops_spool_owner_invalid")
    # POSIX 文件不得授予组或其他账号读取/写入权限，避免本地敏感账务扩散。
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise UsageSpoolError("finops_spool_permissions_invalid")


def _owner_is_current(path: Path, *, metadata: os.stat_result) -> bool:
    """按平台判断 owner；Windows 使用安全描述符 SID，不调用 shell 或修改 ACL。"""

    if os.name != "nt":
        get_effective_user = getattr(os, "geteuid", None)
        return callable(get_effective_user) and metadata.st_uid == get_effective_user()
    return _windows_owner_is_current(path)


def _windows_owner_is_current(path: Path) -> bool:
    """使用 Win32 owner SID 与当前进程 TokenUser SID 比较，不读取用户名文本。"""

    try:
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # ctypes 未声明签名时会把返回值按 32 位 int 解释；在 64 位 Windows 上这会截断
        # HANDLE/指针，既可能误判 owner，也可能访问无效地址。因此这里逐一冻结 ABI。
        advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        advapi32.EqualSid.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree.restype = ctypes.c_void_p
        owner_sid = ctypes.c_void_p()
        security_descriptor = ctypes.c_void_p()
        result = advapi32.GetNamedSecurityInfoW(
            str(path),
            1,  # SE_FILE_OBJECT
            1,  # OWNER_SECURITY_INFORMATION
            ctypes.byref(owner_sid),
            None,
            None,
            None,
            ctypes.byref(security_descriptor),
        )
        if result != 0:
            return False
        try:
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
            ):
                return False
            try:
                needed = wintypes.DWORD()
                advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
                if needed.value == 0:
                    return False
                buffer = ctypes.create_string_buffer(needed.value)
                if not advapi32.GetTokenInformation(
                    token,
                    1,
                    ctypes.cast(buffer, ctypes.c_void_p),
                    needed.value,
                    ctypes.byref(needed),
                ):
                    return False
                # TOKEN_USER 的首字段是 SID_AND_ATTRIBUTES，后者首字段即 PSID。
                current_sid = ctypes.c_void_p.from_buffer(buffer).value
                return bool(current_sid and advapi32.EqualSid(owner_sid, current_sid))
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.LocalFree(security_descriptor)
    # Win32 ABI 或安全描述符读取异常时按 owner 不可信处理，不尝试用户名文本回退。
    except (AttributeError, OSError, ValueError):
        return False


def _is_reparse_or_symlink(path: Path, *, metadata: os.stat_result | None = None) -> bool:
    """识别 POSIX 符号链接与 Windows 重解析点；目录 junction 同样必须拒绝。"""

    details = metadata if metadata is not None else path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def _assert_child_path(path: Path, parent: Path) -> None:
    """校验状态变更目标仍是预期目录的直接子项，不接受调用方拼接路径。"""

    # 只允许直接子项且拒绝特殊名称，防止 claim 操作逃逸状态目录。
    if path.parent != parent or path.name in {"", ".", ".."}:
        raise UsageSpoolError("finops_spool_path_invalid")


def _fsync_directory(directory: Path) -> None:
    """POSIX 上同步目录项；Windows 不支持目录 fsync 时依赖同卷原子文件操作。"""

    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_lower_hex(value: object, *, length: int) -> bool:
    """判断值是否为固定长度小写十六进制，不接受大写或 Unicode 数字。"""

    return isinstance(value, str) and len(value) == length and set(value) <= _LOWER_HEX


def _is_bounded_safe_text(value: object, *, maximum: int) -> bool:
    """校验 spool 身份字段有界、非空且不含空白或控制字符。"""

    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _log_replay_deferred(payload: dict[str, Any], exc: BaseException) -> None:
    """记录重放延期的安全身份和异常类型，不输出文件路径或载荷。"""

    context = payload.get("context") if isinstance(payload, dict) else None
    safe_context = context if isinstance(context, dict) else {}
    logger.warning(
        "finops.usage_spool_replay_deferred",
        extra={
            "event": "finops.usage_spool_replay_deferred",
            "tenant_id": safe_context.get("tenant_id"),
            "request_id": safe_context.get("request_id"),
            "trace_id": safe_context.get("trace_id"),
            "error_code": "finops.usage_record_unavailable",
            "payload": {"exception_type": exc.__class__.__name__},
        },
    )


def _log_spool_unavailable(exc: BaseException, *, stage: str) -> None:
    """记录 spool 固定失败阶段，不把可能含宿主机路径的异常正文写入日志。"""

    logger.error(
        "finops.usage_spool_unavailable",
        extra={
            "event": "finops.usage_spool_unavailable",
            "request_id": "runtime",
            "trace_id": "runtime",
            "error_code": "finops.usage_record_unavailable",
            "payload": {"stage": stage, "exception_type": exc.__class__.__name__},
        },
    )
