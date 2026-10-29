"""Response Cache 结算结果未知时的最小本地恢复队列。

记录只保存请求 owner、CachePolicy 版本、预生成结算 ID 和固定错误码。缓存正文、摘要、
标签、Token、金额、Provider/Endpoint 和任何 Secret 均不允许进入 payload 或日志。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import anyio
from fastapi import FastAPI
from metahub_shared.response_cache import VersionedResponseCacheSecret
from metahub_shared.runtime.config import AppConfig

from metahub_gateway.usage.spool import (
    UsageSpoolError,
    _assert_child_path,
    _assert_local_absolute_root,
    _assert_no_reparse_ancestors,
    _assert_private_directory,
    _assert_regular_private_file,
    _exclusive_write,
    _fsync_directory,
)

from .models import ResponseCacheSettlementIds

logger = logging.getLogger("metahub.gateway.response_cache_failure_spool")

# 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
MAX_FAILURE_RECORD_BYTES = 8 * 1024
RECOVERY_BATCH_SIZE = 64
_SCHEMA_VERSION = 1
_FIXED_ERROR_CODE = "gateway.response_cache_settlement_unavailable"
_CONTENT_DOMAIN = b"metahub.response-cache.failure-spool.v1\x00content\x00"
_FILENAME_DOMAIN = b"metahub.response-cache.failure-spool.v1\x00filename\x00"
_LOWER_HEX = frozenset("0123456789abcdef")


class ResponseCacheFailureSpoolError(RuntimeError):
    """本地恢复记录的 schema、真实性或原子文件操作失败。"""


@dataclass(frozen=True, slots=True)
class ResponseCacheFailureRecord:
    """结算未知恢复所需的完整白名单；所有可关联身份均从 repr 隐藏。"""

    tenant_id: str = field(repr=False)
    request_id: str = field(repr=False)
    trace_id: str = field(repr=False)
    cache_policy_id: str = field(repr=False)
    cache_policy_version: int
    ids: ResponseCacheSettlementIds = field(repr=False)
    error_code: str = _FIXED_ERROR_CODE

    def __post_init__(self) -> None:
        """构造时即执行严格字段合同，禁止调用方把扩展数据带入磁盘。"""

        _validate_record(self)


@dataclass(frozen=True, slots=True)
class ClaimedResponseCacheFailure:
    """当前 worker 已原子领取并完成验签的一条记录。"""

    path: Path = field(repr=False)
    digest: str = field(repr=False)
    record: ResponseCacheFailureRecord = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResponseCacheFailureReplaySummary:
    """一轮恢复的安全计数，不包含租户、请求、文件路径或结算金额。"""

    claimed: int = 0
    completed: int = 0
    recovered_failed: int = 0
    deferred: int = 0
    quarantined: int = 0
    available: bool = True


class ResponseCacheFailureSpool:
    """管理 pending、processing 与 quarantine 三态的 HMAC 文件队列。

    失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 ResponseCacheFailureSpool 对应步骤。"""

    def __init__(self, config: AppConfig) -> None:
        """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 __init__ 对应步骤。"""
        self.root = Path(config.response_cache_failure_spool_root)
        self.keyring = config.response_cache_failure_spool_keyring
        self.claim_lease_seconds = config.response_cache_failure_spool_claim_lease_seconds
        if not self.keyring:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError("response_cache_failure_spool_key_missing")

    @property
    def pending(self) -> Path:
        """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 pending 对应步骤。"""
        return self.root / "pending"

    @property
    def processing(self) -> Path:
        """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 processing 对应步骤。"""
        return self.root / "processing"

    @property
    def quarantine(self) -> Path:
        """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 quarantine 对应步骤。"""
        return self.root / "quarantine"

    def prepare(self) -> None:
        """创建并复核本地专属目录，拒绝根目录、网络路径和重解析边界。"""

        try:
            _assert_local_absolute_root(self.root)
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for directory in (self.root, self.pending, self.processing, self.quarantine):
                directory.mkdir(mode=0o700, exist_ok=True)
                _assert_private_directory(directory)
            _assert_no_reparse_ancestors(self.root)
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        except (OSError, UsageSpoolError) as exc:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_directory_unavailable"
            ) from exc

    def store(self, record: ResponseCacheFailureRecord) -> bool:
        """以 active key 签名并通过同目录 atomic replace + fsync 发布记录。"""

        _validate_record(record)
        self.prepare()
        key = self.keyring[0]
        payload = _record_payload(record)
        payload_bytes = _canonical_json_bytes(payload)
        digest = _filename_digest(key, record)
        envelope = {
            "schema_version": _SCHEMA_VERSION,
            "hmac_key_version": key.version,
            "payload": payload,
            "hmac_sha256": _content_hmac(key, payload_bytes),
        }
        content = _canonical_json_bytes(envelope)
        if not content or len(content) > MAX_FAILURE_RECORD_BYTES:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_record_size_invalid"
            )
        final = self.pending / f"{digest}.json"
        if final.exists():
            existing = self._read(final, expected_digest=digest)
            if existing != record:
                # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
                raise ResponseCacheFailureSpoolError(
                    "response_cache_failure_spool_identity_conflict"
                )
            return False
        temporary = self.pending / f".{digest}.{uuid4().hex}.tmp"
        try:
            _exclusive_write(temporary, content)
            os.replace(temporary, final)
            _fsync_directory(self.pending)
            return True
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        except (OSError, UsageSpoolError) as exc:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_publish_failed"
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            except OSError:
                pass

    def claim_batch(
        self, *, limit: int = RECOVERY_BATCH_SIZE
    ) -> tuple[list[ClaimedResponseCacheFailure], int]:
        """回收过期 claim 后按稳定顺序领取有限记录，并返回本轮隔离数。"""

        if type(limit) is not int or not 1 <= limit <= RECOVERY_BATCH_SIZE:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ValueError("response_cache_failure_spool_batch_invalid")
        self.prepare()
        quarantined = self._recover_stale_claims()
        claims: list[ClaimedResponseCacheFailure] = []
        for pending in sorted(self.pending.glob("*.json")):
            if len(claims) >= limit:
                break
            digest = pending.stem
            if not _lower_hex(digest, 64):
                self._quarantine(pending, digest="invalid")
                quarantined += 1
                continue
            # The lease uses file mtime, so a full UUID is sufficient for uniqueness.
            # Keeping the name bounded also avoids legacy Windows MAX_PATH failures.
            claim_path = self.processing / f"{digest}.{uuid4().hex}.claim"
            try:
                os.replace(pending, claim_path)
                _fsync_directory(self.pending)
                _fsync_directory(self.processing)
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            except FileNotFoundError as exc:
                # A missing source is a normal competing claim.  If it still
                # exists, Windows is reporting a destination/path failure.
                if pending.exists():
                    # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
                    raise ResponseCacheFailureSpoolError(
                        "response_cache_failure_spool_claim_failed"
                    ) from exc
                continue
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            except OSError as exc:
                # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
                raise ResponseCacheFailureSpoolError(
                    "response_cache_failure_spool_claim_failed"
                ) from exc
            try:
                record = self._read(claim_path, expected_digest=digest)
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            except ResponseCacheFailureSpoolError:
                self._quarantine(claim_path, digest=digest)
                quarantined += 1
                continue
            claims.append(ClaimedResponseCacheFailure(claim_path, digest, record))
        return claims, quarantined

    def complete(self, claim: ClaimedResponseCacheFailure) -> None:
        """数据库终态已确认后删除 claim 并同步目录项。"""

        self.prepare()
        _assert_claim_path(claim.path, self.processing)
        try:
            claim.path.unlink()
            _fsync_directory(self.processing)
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        except (OSError, UsageSpoolError) as exc:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_complete_failed"
            ) from exc

    def release(self, claim: ClaimedResponseCacheFailure) -> None:
        """数据库不可读时把 claim 原子归还 pending，绝不猜测 commit 结果。"""

        self.prepare()
        _assert_claim_path(claim.path, self.processing)
        pending = self.pending / f"{claim.digest}.json"
        if pending.exists():
            try:
                existing = self._read(pending, expected_digest=claim.digest)
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            except ResponseCacheFailureSpoolError:
                self._quarantine(pending, digest=claim.digest)
                self._quarantine(claim.path, digest=claim.digest)
                return
            if existing != claim.record:
                self._quarantine(pending, digest=claim.digest)
                self._quarantine(claim.path, digest=claim.digest)
                return
            self.complete(claim)
            return
        try:
            os.replace(claim.path, pending)
            _fsync_directory(self.processing)
            _fsync_directory(self.pending)
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        except OSError as exc:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_release_failed"
            ) from exc

    def quarantine_claim(self, claim: ClaimedResponseCacheFailure) -> None:
        """partial owner 必须保留为人工完整性告警，不能回到自动重试队列。"""

        self.prepare()
        _assert_claim_path(claim.path, self.processing)
        self._quarantine(claim.path, digest=claim.digest)

    def _read(self, path: Path, *, expected_digest: str) -> ResponseCacheFailureRecord:
        """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _read 对应步骤。"""
        try:
            _assert_regular_private_file(path)
            content = path.read_bytes()
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        except (OSError, UsageSpoolError) as exc:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_read_failed"
            ) from exc
        if not content or len(content) > MAX_FAILURE_RECORD_BYTES:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_record_size_invalid"
            )
        try:
            envelope = json.loads(
                content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        except (UnicodeError, json.JSONDecodeError, ResponseCacheFailureSpoolError) as exc:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_json_invalid"
            ) from exc
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema_version",
            "hmac_key_version",
            "payload",
            "hmac_sha256",
        }:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_envelope_invalid"
            )
        if envelope["schema_version"] != _SCHEMA_VERSION:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_version_invalid"
            )
        key = _key_for_version(self.keyring, envelope["hmac_key_version"])
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_payload_invalid"
            )
        payload_bytes = _canonical_json_bytes(payload)
        signature = envelope["hmac_sha256"]
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, _content_hmac(key, payload_bytes)
        ):
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_hmac_invalid"
            )
        record = _record_from_payload(payload)
        if not hmac.compare_digest(expected_digest, _filename_digest(key, record)):
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_filename_invalid"
            )
        return record

    def _recover_stale_claims(self) -> int:
        """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _recover_stale_claims 对应步骤。"""
        quarantined = 0
        threshold = time.time() - self.claim_lease_seconds
        for path in sorted(self.processing.glob("*.claim")):
            try:
                if path.stat().st_mtime > threshold:
                    continue
                digest = path.name.split(".", 1)[0]
                if not _lower_hex(digest, 64):
                    self._quarantine(path, digest="invalid")
                    quarantined += 1
                    continue
                record = self._read(path, expected_digest=digest)
                self.release(ClaimedResponseCacheFailure(path, digest, record))
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            except FileNotFoundError:
                continue
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            except ResponseCacheFailureSpoolError:
                if path.exists():
                    self._quarantine(path, digest="invalid")
                    quarantined += 1
        return quarantined

    def _quarantine(self, path: Path, *, digest: str) -> None:
        """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _quarantine 对应步骤。"""
        if not path.exists():
            return
        safe = digest if _lower_hex(digest, 64) else "invalid"
        destination = self.quarantine / f"{safe}.{uuid4().hex}.bad"
        try:
            os.replace(path, destination)
            _fsync_directory(path.parent)
            _fsync_directory(self.quarantine)
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        except OSError as exc:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_quarantine_failed"
            ) from exc


def store_response_cache_failure_record(
    config: AppConfig, record: ResponseCacheFailureRecord
) -> bool:
    """热路径在数据库结果不可判定时同步 fsync 一条最小恢复记录。"""

    return ResponseCacheFailureSpool(config).store(record)


async def replay_response_cache_failure_batch(
    config: AppConfig,
) -> ResponseCacheFailureReplaySummary:
    """在线程中执行一轮有界 exact read-back 与失败终态恢复。"""

    return await anyio.to_thread.run_sync(_replay_response_cache_failure_batch, config)


def _replay_response_cache_failure_batch(
    config: AppConfig,
) -> ResponseCacheFailureReplaySummary:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _replay_response_cache_failure_batch 对应步骤。"""
    from .settlement import (
        read_response_cache_settlement,
        recover_absent_response_cache_settlement,
    )

    try:
        spool = ResponseCacheFailureSpool(config)
        claims, quarantined = spool.claim_batch()
    # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
    except (ResponseCacheFailureSpoolError, ValueError) as exc:
        _log_runtime_failure(exc, stage="claim")
        return ResponseCacheFailureReplaySummary(available=False)
    completed = 0
    recovered = 0
    deferred = 0
    for claim in claims:
        record = claim.record
        try:
            readback = read_response_cache_settlement(
                config,
                tenant_id=record.tenant_id,
                request_id=record.request_id,
                trace_id=record.trace_id,
                cache_policy_id=record.cache_policy_id,
                cache_policy_version=record.cache_policy_version,
                ids=record.ids,
            )
            if readback.status == "complete":
                spool.complete(claim)
                completed += 1
                continue
            if readback.status == "partial":
                spool.quarantine_claim(claim)
                quarantined += 1
                _log_partial_owner(record)
                continue
            if readback.status == "unreadable":
                spool.release(claim)
                deferred += 1
                continue
            recovered_readback = recover_absent_response_cache_settlement(
                config,
                tenant_id=record.tenant_id,
                request_id=record.request_id,
                trace_id=record.trace_id,
                cache_policy_id=record.cache_policy_id,
                cache_policy_version=record.cache_policy_version,
                ids=record.ids,
                error_code=record.error_code,
            )
            if recovered_readback.status == "unreadable":
                spool.release(claim)
                deferred += 1
            elif recovered_readback.status == "partial":
                spool.quarantine_claim(claim)
                quarantined += 1
                _log_partial_owner(record)
            else:
                # recovered/already_complete 都证明唯一终态已提交，可以删除本地恢复记录。
                spool.complete(claim)
                recovered += 1
        except ResponseCacheFailureSpoolError as exc:
            deferred += 1
            _log_runtime_failure(exc, stage="replay")
        except Exception as exc:
            # Settlement 边界只应返回固定状态；未知异常仍保留 claim，不输出异常正文。
            try:
                spool.release(claim)
            except ResponseCacheFailureSpoolError:
                pass
            deferred += 1
            _log_runtime_failure(exc, stage="replay")
    return ResponseCacheFailureReplaySummary(
        claimed=len(claims),
        completed=completed,
        recovered_failed=recovered,
        deferred=deferred,
        quarantined=quarantined,
    )


def install_response_cache_failure_recovery(app: FastAPI) -> None:
    """组合 Gateway lifespan，在服务期间周期恢复 outcome-unknown 记录。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 combined_lifespan 对应步骤。"""
        async with original_lifespan(current_app):
            if not current_app.state.config.response_cache_failure_spool_keyring:
                # 能力默认关闭且从未持有 spool key 时不存在可验证记录，不能因此阻断 Gateway。
                current_app.state.response_cache_failure_spool_summary = (
                    ResponseCacheFailureReplaySummary()
                )
                try:
                    yield
                finally:
                    current_app.state.response_cache_failure_spool_summary = None
                return
            stop_event = asyncio.Event()
            current_app.state.response_cache_failure_spool_summary = (
                await replay_response_cache_failure_batch(current_app.state.config)
            )
            worker = asyncio.create_task(
                _failure_spool_worker(current_app, stop_event),
                name="gateway-response-cache-failure-spool",
            )
            try:
                yield
            finally:
                stop_event.set()
                await worker
                current_app.state.response_cache_failure_spool_summary = None

    app.router.lifespan_context = combined_lifespan


async def _failure_spool_worker(app: FastAPI, stop_event: asyncio.Event) -> None:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _failure_spool_worker 对应步骤。"""
    interval = app.state.config.response_cache_failure_spool_poll_interval_seconds
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        except TimeoutError:
            app.state.response_cache_failure_spool_summary = (
                await replay_response_cache_failure_batch(app.state.config)
            )


def _record_payload(record: ResponseCacheFailureRecord) -> dict[str, Any]:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _record_payload 对应步骤。"""
    return {
        "tenant_id": record.tenant_id,
        "request_id": record.request_id,
        "trace_id": record.trace_id,
        "cache_policy_id": record.cache_policy_id,
        "cache_policy_version": record.cache_policy_version,
        "settlement_ids": {
            "cache_hit_id": record.ids.cache_hit_id,
            "usage_ledger_id": record.ids.usage_ledger_id,
            "finops_ledger_entry_id": record.ids.finops_ledger_entry_id,
            "finops_event_id": record.ids.finops_event_id,
            "reference_price_snapshot_id": record.ids.reference_price_snapshot_id,
            "billed_price_snapshot_id": record.ids.billed_price_snapshot_id,
            "response_cache_event_id": record.ids.response_cache_event_id,
        },
        "error_code": record.error_code,
    }


def _record_from_payload(payload: dict[str, Any]) -> ResponseCacheFailureRecord:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _record_from_payload 对应步骤。"""
    if set(payload) != {
        "tenant_id",
        "request_id",
        "trace_id",
        "cache_policy_id",
        "cache_policy_version",
        "settlement_ids",
        "error_code",
    } or not isinstance(payload["settlement_ids"], dict):
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ResponseCacheFailureSpoolError(
            "response_cache_failure_spool_payload_schema_invalid"
        )
    ids = payload["settlement_ids"]
    if set(ids) != {
        "cache_hit_id",
        "usage_ledger_id",
        "finops_ledger_entry_id",
        "finops_event_id",
        "reference_price_snapshot_id",
        "billed_price_snapshot_id",
        "response_cache_event_id",
    }:
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ResponseCacheFailureSpoolError(
            "response_cache_failure_spool_payload_schema_invalid"
        )
    try:
        return ResponseCacheFailureRecord(
            tenant_id=payload["tenant_id"],
            request_id=payload["request_id"],
            trace_id=payload["trace_id"],
            cache_policy_id=payload["cache_policy_id"],
            cache_policy_version=payload["cache_policy_version"],
            ids=ResponseCacheSettlementIds(**ids),
            error_code=payload["error_code"],
        )
    # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
    except (TypeError, ValueError) as exc:
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ResponseCacheFailureSpoolError(
            "response_cache_failure_spool_payload_schema_invalid"
        ) from exc


def _validate_record(record: ResponseCacheFailureRecord) -> None:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _validate_record 对应步骤。"""
    if not isinstance(record, ResponseCacheFailureRecord) or not all(
        _safe_text(value, maximum=128)
        for value in (record.tenant_id, record.request_id, record.trace_id)
    ):
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ValueError("response_cache_failure_spool_identity_invalid")
    if not _prefixed_id(record.cache_policy_id, "rcp_") or (
        type(record.cache_policy_version) is not int or record.cache_policy_version < 1
    ):
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ValueError("response_cache_failure_spool_policy_invalid")
    # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
    if record.error_code != _FIXED_ERROR_CODE:
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ValueError("response_cache_failure_spool_error_code_invalid")
    if not isinstance(record.ids, ResponseCacheSettlementIds):
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ValueError("response_cache_failure_spool_settlement_ids_invalid")
    expected: tuple[tuple[str, str], ...] = (
        (record.ids.cache_hit_id, "rch_"),
        (record.ids.usage_ledger_id, "usg_"),
        (record.ids.finops_ledger_entry_id, "fle_"),
        (record.ids.finops_event_id, "flevt_"),
        (record.ids.reference_price_snapshot_id, "prs_"),
        (record.ids.billed_price_snapshot_id, "prs_"),
        (record.ids.response_cache_event_id, "rcev_"),
    )
    if not all(
        _prefixed_id(value, prefix) for value, prefix in expected
    ):
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ValueError("response_cache_failure_spool_settlement_ids_invalid")


def _canonical_json_bytes(value: object) -> bytes:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _canonical_json_bytes 对应步骤。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
    except (TypeError, ValueError) as exc:
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ResponseCacheFailureSpoolError(
            "response_cache_failure_spool_json_invalid"
        ) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _reject_duplicate_keys 对应步骤。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
            raise ResponseCacheFailureSpoolError(
                "response_cache_failure_spool_duplicate_key"
            )
        result[key] = value
    return result


def _content_hmac(key: VersionedResponseCacheSecret, payload: bytes) -> str:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _content_hmac 对应步骤。"""
    version = str(key.version).encode("ascii")
    return hmac.new(
        key.secret.encode("utf-8"),
        _CONTENT_DOMAIN + version + b"\x00" + payload,
        hashlib.sha256,
    ).hexdigest()


def _filename_digest(
    key: VersionedResponseCacheSecret, record: ResponseCacheFailureRecord
) -> str:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _filename_digest 对应步骤。"""
    identity = b"\x00".join(
        value.encode("utf-8")
        for value in (
            record.tenant_id,
            record.request_id,
            record.trace_id,
            record.ids.cache_hit_id,
            record.ids.usage_ledger_id,
        )
    )
    return hmac.new(
        key.secret.encode("utf-8"),
        _FILENAME_DOMAIN + str(key.version).encode("ascii") + b"\x00" + identity,
        hashlib.sha256,
    ).hexdigest()


def _key_for_version(
    keyring: tuple[VersionedResponseCacheSecret, ...], version: object
) -> VersionedResponseCacheSecret:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _key_for_version 对应步骤。"""
    if type(version) is not int:
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ResponseCacheFailureSpoolError(
            "response_cache_failure_spool_hmac_version_invalid"
        )
    for key in keyring:
        if key.version == version:
            return key
    # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
    raise ResponseCacheFailureSpoolError(
        "response_cache_failure_spool_hmac_version_unavailable"
    )


def _prefixed_id(value: object, prefix: str) -> bool:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _prefixed_id 对应步骤。"""
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and _lower_hex(value[len(prefix) :], 32)
    )


def _lower_hex(value: object, length: int) -> bool:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _lower_hex 对应步骤。"""
    return isinstance(value, str) and len(value) == length and set(value) <= _LOWER_HEX


def _safe_text(value: object, *, maximum: int) -> bool:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _safe_text 对应步骤。"""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _assert_claim_path(path: Path, processing: Path) -> None:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _assert_claim_path 对应步骤。"""
    try:
        _assert_child_path(path, processing)
    # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
    except UsageSpoolError as exc:
        # 失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。
        raise ResponseCacheFailureSpoolError(
            "response_cache_failure_spool_path_invalid"
        ) from exc


def _log_partial_owner(record: ResponseCacheFailureRecord) -> None:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _log_partial_owner 对应步骤。"""
    logger.error(
        "response_cache.failure_spool_partial_owner",
        extra={
            "event": "response_cache.failure_spool_partial_owner",
            "tenant_id": record.tenant_id,
            "request_id": record.request_id,
            "trace_id": record.trace_id,
            "error_code": "gateway.response_cache_integrity_failure",
        },
    )


def _log_runtime_failure(exc: BaseException, *, stage: Literal["claim", "replay"]) -> None:
    """失败恢复只持久化租户归属和安全枚举，敏感请求正文不得进入落盘队列。 当前定义负责 _log_runtime_failure 对应步骤。"""
    logger.error(
        "response_cache.failure_spool_unavailable",
        extra={
            "event": "response_cache.failure_spool_unavailable",
            "request_id": "runtime",
            "trace_id": "runtime",
            "error_code": _FIXED_ERROR_CODE,
            "payload": {"stage": stage, "exception_type": exc.__class__.__name__},
        },
    )


__all__ = [
    "ResponseCacheFailureRecord",
    "ResponseCacheFailureReplaySummary",
    "ResponseCacheFailureSpool",
    "ResponseCacheFailureSpoolError",
    "install_response_cache_failure_recovery",
    "replay_response_cache_failure_batch",
    "store_response_cache_failure_record",
]
