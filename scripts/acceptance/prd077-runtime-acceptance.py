"""PRD-077 只读管理助手的真实 Control、迁移、恢复和隐私 Runtime 验收器。

验收器只在仓库 ``.tmp`` 下创建一次性数据库与日志，并在 ``.artifacts`` 发布
不含问题、回答、凭据或租户身份的白名单 JSON 与 SHA-256 清单。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

# Runtime 只使用仓库依赖与回环端口；所有私有值均在运行结束前从内存丢弃。
ROOT = Path(__file__).resolve().parents[2]
for _source_root in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
    ROOT / "control-plane-api" / "src",
):
    sys.path.insert(0, str(_source_root))

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.security import hash_password  # noqa: E402
from metahub_shared.runtime.policy_dsl import compile_policy_expression  # noqa: E402
from metahub_shared.runtime.policy_snapshot import (  # noqa: E402
    build_policy_snapshot_artifact,
    build_policy_snapshot_item,
)
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402


def _load_runtime_helpers() -> Any:
    """加载既有进程、路径和隐私 helper，导入阶段不得启动任何资源。"""

    name = "_metahub_prd075_runtime_helpers_for_prd077"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        # helper 缺失时只输出固定依赖错误，不能回显宿主路径或环境。
        raise RuntimeError("runtime helper unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


# 复用已验证的精确 owner 清理与多编码扫描，避免另造进程终止语义。
_BASE = _load_runtime_helpers()
AcceptanceFailure = _BASE.AcceptanceFailure
SafeArgumentParser = _BASE.SafeArgumentParser
ManagedProcess = _BASE.ManagedProcess
OwnedProcesses = _BASE.OwnedProcesses
require = _BASE.require
start_service = _BASE.start_service
wait_health = _BASE.wait_health
login = _BASE.login
request_headers = _BASE.request_headers

# 固定端口与目录只属于本验收器；Console 端口留给随后独立 Browser 验收。
LOOPBACK = "127.0.0.1"
CONTROL_PORT = 8001
WEBHOOK_PORT = 18877
FIXED_PORTS = (CONTROL_PORT, WEBHOOK_PORT)
WORK_ROOT = ROOT / ".tmp" / "prd077-runtime"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd077-runtime"
MIGRATIONS_DIR = ROOT / "infra" / "migrations"
OWNER_CLAIM_FILENAME = ".prd077-runtime-owner"
OWNER_QUARANTINE_PREFIX = ".p77q-"
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
MAX_PRIVATE_SCAN_FILE_BYTES = 128 * 1024 * 1024
OWNER_DELETE_RETRY_ATTEMPTS = 150
OWNER_DELETE_RETRY_DELAY_SECONDS = 0.2
OWNER_QUARANTINE_RENAME_ATTEMPTS = 20
OWNER_QUARANTINE_RENAME_DELAY_SECONDS = 0.05
# Windows 提供按已验证句柄 rename/delete 且拒绝 DELETE sharing 的语义；POSIX
# renameat/unlinkat 仍按易变目录项名称选对象，不能冒充同等级 identity 保证。
IDENTITY_BOUND_MUTATIONS_SUPPORTED = os.name == "nt"
MIGRATION_VERSIONS = tuple(f"{version:04d}" for version in range(1, 71))
MAX_WEBHOOK_BYTES = 64 * 1024
CONCURRENT_ATTEMPTS = 20
CONCURRENT_HTTP_TIMEOUT_SECONDS = 60
EVENT_RECOVERY_TIMEOUT_SECONDS = 180
EVENT_RECOVERY_STALL_SECONDS = 30
EXPECTED_ASSISTANT_ATTEMPTS = 33
EXPECTED_FIRST_TENANT_ATTEMPTS = 31
# 16 字符与凭据片段门禁一致；更短公开枚举可能合法存在于来源数据库中。
MIN_PRIVATE_ANSWER_LEAF_LENGTH = 16
SOURCE_PROVENANCE_NAMES = frozenset(
    {
        "runtime_acceptance",
        "runtime_helper",
        "shared_source_tree",
        "control_source_tree",
        "migration_set",
        "migration_0070_up",
        "migration_0070_down",
    }
)
RUNTIME_FACT_KEYS = frozenset(
    {
        "services",
        "queries",
        "source_failure",
        "concurrency",
        "access_control",
        "isolation",
        "event_recovery",
        "database",
    }
)
FORBIDDEN_EVENT_KEYS = frozenset(
    {
        "question",
        "answer",
        "prompt",
        "response",
        "token",
        "authorization",
        "provider_key",
        "endpoint_url",
        "target_request_id",
        "cost",
    }
)

# 通用启动 helper 的端口判断必须与本脚本一致，不能枚举或终止用户的其他进程。
_BASE.LOOPBACK = LOOPBACK
_BASE.CONTROL_PORT = CONTROL_PORT
_BASE.FIXED_PORTS = FIXED_PORTS


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    """保存仅存在当前进程内存的随机测试凭据与问题 canary。"""

    admin_password: str = field(repr=False)
    role_password: str = field(repr=False)
    second_password: str = field(repr=False)
    auth_secret: str = field(repr=False)
    provider_secret: str = field(repr=False)
    file_encryption_secret: str = field(repr=False)
    batch_encryption_secret: str = field(repr=False)
    prompt_template_encryption_secret: str = field(repr=False)
    cache_affinity_hmac_secret: str = field(repr=False)
    provider_key: str = field(repr=False)
    virtual_key: str = field(repr=False)
    second_virtual_key: str = field(repr=False)
    question_canary: str = field(repr=False)

    @classmethod
    def create(cls) -> RuntimeSecrets:
        """生成互不复用的随机值，避免把仓库示例凭据误当作运行证据。"""

        return cls(
            admin_password=f"Prd077Admin-{secrets.token_urlsafe(24)}",
            role_password=f"Prd077Role-{secrets.token_urlsafe(24)}",
            second_password=f"Prd077Second-{secrets.token_urlsafe(24)}",
            auth_secret=secrets.token_urlsafe(64),
            provider_secret=secrets.token_urlsafe(64),
            file_encryption_secret=secrets.token_urlsafe(64),
            batch_encryption_secret=secrets.token_urlsafe(64),
            prompt_template_encryption_secret=secrets.token_urlsafe(64),
            cache_affinity_hmac_secret=secrets.token_urlsafe(64),
            provider_key=f"provider-{secrets.token_urlsafe(32)}",
            virtual_key=f"sk-mh-{secrets.token_urlsafe(32)}",
            second_virtual_key=f"sk-mh-{secrets.token_urlsafe(32)}",
            question_canary=f"PRD077_QUESTION_{secrets.token_hex(16)}",
        )

    def scan_values(self) -> tuple[str, ...]:
        """返回必须在日志、数据库和证据中零明文的全部运行私有值。"""

        return (
            self.admin_password,
            self.role_password,
            self.second_password,
            self.auth_secret,
            self.provider_secret,
            self.file_encryption_secret,
            self.batch_encryption_secret,
            self.prompt_template_encryption_secret,
            self.cache_affinity_hmac_secret,
            self.provider_key,
            self.virtual_key,
            self.second_virtual_key,
            self.question_canary,
        )


@dataclass(frozen=True, slots=True)
class BusinessGraph:
    """保存真实服务启动所需路径与公开 owner 元数据，私有凭据不进入该结构。"""

    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str = field(repr=False)
    second_tenant_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """绑定枚举时看到的普通文件及其稳定 identity/内容元数据。"""

    path: Path
    identity: tuple[int, int]
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ScannedDirectory:
    """绑定枚举时看到的目录及其稳定 identity/树变更元数据。"""

    path: Path
    identity: tuple[int, int]
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ScanSnapshot:
    """一次严格树枚举的完整目录与文件快照。"""

    files: tuple[ScannedFile, ...]
    directories: tuple[ScannedDirectory, ...]


class WindowsTreeChangeGuard:
    """Windows 递归目录变更监视器，检测扫描期间发生后又恢复的树或内容变化。"""

    def __init__(self, root: Path, *, stage: str) -> None:
        """绑定根目录递归通知，捕获扫描期间短暂创建、删除与重命名。"""

        self._active = False
        self._buffer: Any | None = None
        self._event: Any | None = None
        self._handle: Any | None = None
        self._kernel32: Any | None = None
        self._overlapped: Any | None = None
        self._stage = stage
        if os.name != "nt":
            return
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            """映射 Win32 OVERLAPPED 布局，供目录异步通知与取消共用。"""

            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(Overlapped),
            ctypes.c_void_p,
        ]
        kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
        kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Overlapped),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        kernel32.GetOverlappedResult.restype = wintypes.BOOL
        kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
        kernel32.CancelIoEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(root),
            0x0001,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x40000000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        require(handle not in {None, invalid_handle}, stage, "tree_watch")
        event = kernel32.CreateEventW(None, True, False, None)
        if not event:
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure(stage, "tree_watch")
        buffer = ctypes.create_string_buffer(64 * 1024)
        overlapped = Overlapped()
        overlapped.hEvent = event
        # Existing files/directories stay guarded without WRITE/DELETE sharing. The watcher
        # therefore only needs recursive name changes to catch transient create/delete/rename;
        # NTFS may otherwise report delayed metadata changes from work completed before arming.
        notify_filter = 0x00000001 | 0x00000002
        started = kernel32.ReadDirectoryChangesW(
            handle,
            buffer,
            len(buffer),
            True,
            notify_filter,
            None,
            ctypes.byref(overlapped),
            None,
        )
        if not started:
            kernel32.CloseHandle(event)
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure(stage, "tree_watch")
        self._active = True
        self._buffer = buffer
        self._event = event
        self._handle = handle
        self._kernel32 = kernel32
        self._overlapped = overlapped

    def assert_unchanged(self, *, stage: str) -> None:
        """在取消监视前检查完成状态；任何完成事件都表示扫描期间发生过变化。"""

        if not self._active:
            return
        from ctypes import wintypes

        transferred = wintypes.DWORD()
        if self._kernel32.GetOverlappedResult(
            self._handle,
            ctypes.byref(self._overlapped),
            ctypes.byref(transferred),
            False,
        ):
            self._active = False
            raise AcceptanceFailure(stage, "tree_changed")
        error = ctypes.get_last_error()
        require(error == 996, stage, "tree_watch")
        cancelled = self._kernel32.CancelIoEx(
            self._handle,
            ctypes.byref(self._overlapped),
        )
        cancel_error = 0 if cancelled else ctypes.get_last_error()
        require(cancelled or cancel_error == 1168, stage, "tree_watch")
        completed = self._kernel32.GetOverlappedResult(
            self._handle,
            ctypes.byref(self._overlapped),
            ctypes.byref(transferred),
            True,
        )
        result_error = 0 if completed else ctypes.get_last_error()
        self._active = False
        # 取消后若已完成或不是标准 OPERATION_ABORTED，说明扫描窗口内确实发生过变化。
        if completed or result_error != 995:
            raise AcceptanceFailure(stage, "tree_changed")

    def close(self) -> None:
        """取消未完成监视并释放 event/root 句柄；异常路径不覆盖原固定失败坐标。"""

        if self._active:
            from ctypes import wintypes

            self._kernel32.CancelIoEx(self._handle, ctypes.byref(self._overlapped))
            transferred = wintypes.DWORD()
            self._kernel32.GetOverlappedResult(
                self._handle,
                ctypes.byref(self._overlapped),
                ctypes.byref(transferred),
                True,
            )
            self._active = False
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._event is not None:
            self._kernel32.CloseHandle(self._event)
            self._event = None

    def __enter__(self) -> WindowsTreeChangeGuard:
        """返回已激活的 watcher，由上下文退出统一取消并关闭句柄。"""

        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        """无论扫描成功或失败都释放通知句柄，且不枚举或删除目录内容。"""

        self.close()


@contextmanager
def temporary_environment(values: dict[str, str]) -> Iterator[None]:
    """临时设置 seed 环境并在异常路径也恢复调用进程原值。"""

    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def safe_run_id(value: str | None) -> str:
    """把运行 ID 限制为单个短 ASCII 路径段，禁止覆盖或目录跳转。"""

    candidate = value or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
    compact = candidate.replace("-", "").replace("_", "")
    require(
        3 <= len(candidate) <= 80 and compact.isascii() and compact.isalnum(),
        "config.run_id",
        "invalid",
    )
    return candidate


def repository_child(path: Path, parent: Path, *, stage: str) -> Path:
    """解析并验证仓库内直属运行路径，供创建、发布和递归清理共用。"""

    try:
        resolved = path.resolve()
        root = parent.resolve()
        relative = resolved.relative_to(root)
        root.relative_to(ROOT.resolve())
    # 路径解析或边界判断失败统一隐藏宿主绝对路径，只保留固定验收坐标。
    # 解析、权限或相对边界失败都隐藏宿主路径，仅返回固定仓库边界坐标。
    except (OSError, RuntimeError, ValueError):
        raise AcceptanceFailure(stage, "outside_repository") from None
    require(len(relative.parts) == 1, stage, "not_direct_child")
    return resolved


def _lexists(path: Path) -> bool:
    """检查路径节点本身是否存在，broken reparse 也不能被当作已清理。"""

    return os.path.lexists(path)


def _direct_child_without_resolving(path: Path, parent: Path, *, stage: str) -> Path:
    """先按字面路径验证直属边界，避免在 lstat 前跟随 junction。"""

    try:
        root = parent.resolve(strict=True)
        root.relative_to(ROOT.resolve(strict=True))
        target = Path(os.path.abspath(path))
    # 父目录解析失败或越出仓库时立即失败关闭，避免验收器写入未受控位置。
    except (OSError, RuntimeError, ValueError):
        raise AcceptanceFailure(stage, "outside_repository") from None
    require(
        os.path.normcase(str(target.parent)) == os.path.normcase(str(root)),
        stage,
        "not_direct_child",
    )
    return target


def owned_directory_identity(path: Path, parent: Path, *, stage: str) -> tuple[int, int]:
    """返回普通直属目录的稳定文件 identity，并拒绝 symlink/junction/reparse。"""

    target = _direct_child_without_resolving(path, parent, stage=stage)
    try:
        information = os.lstat(target)
        canonical = target.resolve(strict=True)
    # 字面直属节点无法 lstat/resolve 时不能推断 owner，禁止后续递归清理。
    except OSError:
        raise AcceptanceFailure(stage, "owner_identity") from None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    require(
        stat.S_ISDIR(information.st_mode)
        and not stat.S_ISLNK(information.st_mode)
        and not (getattr(information, "st_file_attributes", 0) & reparse_flag)
        and os.path.normcase(str(canonical)) == os.path.normcase(str(target)),
        stage,
        "owner_identity",
    )
    identity = (int(information.st_dev), int(information.st_ino))
    require(identity[1] > 0, stage, "owner_identity")
    return identity


def _write_owner_claim(directory: Path, digest: str) -> None:
    """在刚独占创建的 work 中排他写入本轮随机 owner 摘要。"""

    require(
        len(digest) == 64 and all(character in "0123456789abcdef" for character in digest),
        "config.owner_claim",
        "invalid",
    )
    try:
        with (directory / OWNER_CLAIM_FILENAME).open("x", encoding="ascii", newline="\n") as handle:
            handle.write(f"{digest}\n")
    # 排他 claim 创建失败时本轮没有可证明所有权，不能继续使用该目录。
    except OSError:
        raise AcceptanceFailure("config.owner_claim", "filesystem") from None


def _require_owner_claim(
    directory: Path,
    digest: str,
    *,
    stage: str,
    claim_filename: str = OWNER_CLAIM_FILENAME,
) -> None:
    """复核 claim 是直属普通文件且内容精确匹配，不接受链接或复制后的未知目录。"""

    require(
        bool(claim_filename)
        and Path(claim_filename).name == claim_filename
        and claim_filename not in {".", ".."},
        stage,
        "owner_claim",
    )
    claim = directory / claim_filename
    try:
        information = os.lstat(claim)
        canonical = claim.resolve(strict=True)
        content = claim.read_text(encoding="ascii")
    # claim 路径、编码或内容异常均视为 owner 不可证明，避免清理复制的未知目录。
    except (OSError, UnicodeError):
        raise AcceptanceFailure(stage, "owner_claim") from None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    require(
        stat.S_ISREG(information.st_mode)
        and not stat.S_ISLNK(information.st_mode)
        and not (getattr(information, "st_file_attributes", 0) & reparse_flag)
        and information.st_size == 65
        and os.path.normcase(str(canonical)) == os.path.normcase(str(claim))
        and content == f"{digest}\n",
        stage,
        "owner_claim",
    )


def _random_quarantine_path(parent: Path, *, stage: str) -> Path:
    """生成同根、不可预测且当前不存在的删除隔离 leaf。"""

    for _attempt in range(16):
        candidate = _direct_child_without_resolving(
            parent / f"{OWNER_QUARANTINE_PREFIX}{secrets.token_hex(16)}",
            parent,
            stage=stage,
        )
        if not _lexists(candidate):
            return candidate
    raise AcceptanceFailure(stage, "quarantine_name")


class WindowsPathIdentityGuard:
    """以打开句柄固定普通文件或目录；Windows 额外拒绝 DELETE sharing。"""

    def __init__(
        self,
        path: Path,
        expected_identity: tuple[int, int],
        *,
        stage: str,
        directory: bool,
        read: bool = False,
    ) -> None:
        """打开并校验目标 identity；句柄存活期间阻止替换或删除目标节点。"""

        self._handle: Any | None = None
        self._kernel32: Any | None = None
        self._descriptor: int | None = None
        self._stage = stage
        self.identity = expected_identity
        self.path = path
        self.directory = directory
        if os.name != "nt":
            code = "owner_identity" if directory else "file_identity"
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            if directory:
                flags |= getattr(os, "O_DIRECTORY", 0)
            descriptor: int | None = None
            try:
                descriptor = os.open(path, flags)
                opened = os.fstat(descriptor)
                current = (int(opened.st_dev), int(opened.st_ino))
                path_information = os.lstat(path)
                require(
                    current
                    == expected_identity
                    == (int(path_information.st_dev), int(path_information.st_ino))
                    and (
                        stat.S_ISDIR(opened.st_mode) if directory else stat.S_ISREG(opened.st_mode)
                    )
                    and not stat.S_ISLNK(path_information.st_mode),
                    stage,
                    code,
                )
            except AcceptanceFailure:
                if descriptor is not None:
                    os.close(descriptor)
                raise
            # POSIX 打开或身份复核失败时关闭局部 fd，并折叠为固定身份错误。
            except OSError:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    # 清理局部 fd 的错误不得覆盖更早的身份失败坐标。
                    except OSError:
                        pass
                raise AcceptanceFailure(stage, code) from None
            self._descriptor = descriptor
            return

        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            """映射 Win32 基础文件信息，供属性、卷号与兼容 FileIndex 校验。"""

            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        class FileId128(ctypes.Structure):
            """保存 Windows FileIdInfo 返回的 128-bit 不透明文件标识。"""

            _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

        class FileIdInfo(ctypes.Structure):
            """映射卷序列号与 128-bit File ID，跨路径比较同一文件身份。"""

            _fields_ = [
                ("VolumeSerialNumber", ctypes.c_ulonglong),
                ("FileId", FileId128),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ByHandleFileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        desired_access = (0x80000000 if read else 0) | (0x00010000 if directory else 0)
        flags = 0x00200000 | (0x02000000 if directory else 0)
        share_mode = 0x00000001 if read and not directory else 0x00000001 | 0x00000002
        handle = kernel32.CreateFileW(
            str(path),
            desired_access,
            share_mode,
            None,
            3,
            flags,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        require(handle not in {None, invalid_handle}, stage, "path_handle")
        information = ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure(stage, "path_handle")
        file_id_information = FileIdInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            18,
            ctypes.byref(file_id_information),
            ctypes.sizeof(file_id_information),
        ):
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure(stage, "path_handle")
        attributes = int(information.dwFileAttributes)
        handle_identity = (
            int(file_id_information.VolumeSerialNumber),
            int.from_bytes(bytes(file_id_information.FileId.Identifier), "little"),
        )
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        is_directory = bool(attributes & 0x10)
        if (
            bool(attributes & reparse_flag)
            or is_directory != directory
            or handle_identity != expected_identity
        ):
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure(stage, "owner_identity" if directory else "file_identity")
        self._kernel32 = kernel32
        self._handle = handle

    def rename(self, target: Path, *, stage: str) -> None:
        """从当前已验证句柄发起 rename，调用方无需重新解析 source。"""

        require(os.name == "nt" and self.directory, stage, "rename_unsupported")
        from ctypes import wintypes

        class FileRenameInformation(ctypes.Structure):
            """映射按句柄重命名所需的可变长度 Win32 结构头。"""

            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        self._kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        encoded = str(target).encode("utf-16-le")
        offset = FileRenameInformation.FileName.offset
        buffer = ctypes.create_string_buffer(offset + len(encoded) + ctypes.sizeof(wintypes.WCHAR))
        information = FileRenameInformation.from_buffer(buffer)
        information.Flags = 0
        information.RootDirectory = None
        information.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        renamed = False
        for attempt in range(OWNER_QUARANTINE_RENAME_ATTEMPTS):
            renamed = bool(
                self._kernel32.SetFileInformationByHandle(
                    self._handle,
                    3,
                    buffer,
                    offset + len(encoded),
                )
            )
            if renamed:
                break
            if attempt < OWNER_QUARANTINE_RENAME_ATTEMPTS - 1:
                time.sleep(OWNER_QUARANTINE_RENAME_DELAY_SECONDS)
        require(renamed, stage, "rename")
        self.path = target

    @property
    def handle(self) -> Any:
        """仅在 Windows guard 活跃时暴露已验证句柄，供受控重复读取使用。"""

        require(self._handle is not None, "privacy.scan", "path_handle")
        return self._handle

    @property
    def descriptor(self) -> int:
        """仅在 POSIX guard 活跃时返回绑定 identity 的只读文件描述符。"""

        require(self._descriptor is not None, "privacy.scan", "path_handle")
        return self._descriptor

    def verify(self, *, stage: str) -> None:
        """同时复核打开句柄与公开路径仍指向初始 identity 和节点类型。"""

        if self._descriptor is not None:
            try:
                opened = os.fstat(self._descriptor)
            # 已绑定句柄失效时不能退回路径重开，否则会接受被替换后的文件身份。
            except OSError:
                raise AcceptanceFailure(stage, "path_handle") from None
            require(
                (int(opened.st_dev), int(opened.st_ino)) == self.identity
                and (
                    stat.S_ISDIR(opened.st_mode) if self.directory else stat.S_ISREG(opened.st_mode)
                ),
                stage,
                "owner_identity" if self.directory else "file_identity",
            )
        current = (
            owned_directory_identity(self.path, self.path.parent, stage=stage)
            if self.directory
            else regular_file_identity(self.path, stage=stage)
        )
        require(
            current == self.identity,
            stage,
            "owner_identity" if self.directory else "file_identity",
        )

    def delete_empty_directory(self) -> None:
        """由已验证句柄标记空目录删除，关闭前不重新解析公开路径。"""

        require(self.directory and os.name == "nt", "cleanup.owner", "delete_unsupported")
        from ctypes import wintypes

        class FileDispositionInformation(ctypes.Structure):
            """映射按已验证句柄标记空目录删除的 Win32 布尔结构。"""

            _fields_ = [("DeleteFile", wintypes.BOOL)]

        self._kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        information = FileDispositionInformation(True)
        if not self._kernel32.SetFileInformationByHandle(
            self._handle,
            4,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            # 句柄删除失败时绝不回退到按路径 rmdir，避免命中同名替换目录。
            raise OSError("bound directory delete failed")

    def close(self, *, stage: str) -> None:
        """精确关闭当前 fd/handle；关闭失败使 owner 终态不可证明并固定失败。"""

        if self._descriptor is not None:
            descriptor = self._descriptor
            self._descriptor = None
            try:
                os.close(descriptor)
            # fd 关闭失败意味着清理后仍可能持有目标，不能声明资源已经归零。
            except OSError:
                raise AcceptanceFailure(stage, "path_handle_close") from None
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        if not self._kernel32.CloseHandle(handle):
            raise AcceptanceFailure(stage, "path_handle_close") from None

    def __enter__(self) -> WindowsPathIdentityGuard:
        """返回已绑定 identity 的 guard，供调用方在句柄保护下执行复核或变更。"""

        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        """退出时按创建阶段关闭句柄，关闭失败保留为验收失败而非静默忽略。"""

        self.close(stage=self._stage)


def _windows_rename_directory_handle(
    source: Path,
    target: Path,
    expected_identity: tuple[int, int],
    *,
    stage: str,
) -> None:
    """Windows 上按已打开目录句柄 rename，消除验证后再次解析 source 的窗口。"""

    try:
        with WindowsPathIdentityGuard(
            source,
            expected_identity,
            stage=stage,
            directory=True,
        ) as guard:
            guard.rename(target, stage=stage)
            guard.verify(stage=stage)
    # 只有 Windows 拒绝重命名这一固定坐标可进入受控重试，其余身份错误立即传播。
    except AcceptanceFailure as error:
        code = "quarantine_rename" if error.code == "rename" else error.code
        raise AcceptanceFailure(stage, code) from None


def rename_owned_directory(
    source: Path,
    target: Path,
    parent: Path,
    *,
    stage: str,
    expected_identity: tuple[int, int],
) -> None:
    """只从已验证句柄移动直属普通目录；无等价句柄原语的平台失败关闭。"""

    source = _direct_child_without_resolving(source, parent, stage=stage)
    target = _direct_child_without_resolving(target, parent, stage=stage)
    require(not _lexists(target), stage, "target_exists")
    require(
        owned_directory_identity(source, parent, stage=stage) == expected_identity,
        stage,
        "owner_identity",
    )
    require(IDENTITY_BOUND_MUTATIONS_SUPPORTED, stage, "rename_unsupported")
    try:
        _windows_rename_directory_handle(
            source,
            target,
            expected_identity,
            stage=stage,
        )
    except AcceptanceFailure:
        raise
    # POSIX rename 失败时不能假定 source 或 quarantine 的实际身份，统一失败关闭。
    except OSError:
        raise AcceptanceFailure(stage, "rename") from None
    require(
        owned_directory_identity(target, parent, stage=stage) == expected_identity,
        stage,
        "owner_identity",
    )


def quarantine_owned_directory(
    path: Path,
    parent: Path,
    *,
    stage: str,
    expected_identity: tuple[int, int],
    owner_claim: str | None = None,
    claim_filename: str = OWNER_CLAIM_FILENAME,
) -> Path | None:
    """把精确 owner 原子隔离到同根随机 leaf，并在隔离后重新验证。"""

    source = _direct_child_without_resolving(path, parent, stage=stage)
    if not _lexists(source):
        return None
    require(
        owned_directory_identity(source, parent, stage=stage) == expected_identity,
        stage,
        "owner_identity",
    )
    if owner_claim is not None:
        _require_owner_claim(
            source,
            owner_claim,
            stage=stage,
            claim_filename=claim_filename,
        )
    quarantine = _random_quarantine_path(parent, stage=stage)
    try:
        rename_owned_directory(
            source,
            quarantine,
            parent,
            stage=stage,
            expected_identity=expected_identity,
        )
    # 隔离 rename 仅对固定瞬时失败重试，owner/边界漂移必须立即终止清理。
    except AcceptanceFailure as error:
        code = "quarantine_rename" if error.code == "rename" else error.code
        raise AcceptanceFailure(stage, code) from None
    if owner_claim is not None:
        _require_owner_claim(
            quarantine,
            owner_claim,
            stage=stage,
            claim_filename=claim_filename,
        )
    return quarantine


def create_run_directories(
    run_id: str,
    *,
    owner_claim: str | None = None,
) -> tuple[Path, Path]:
    """独占创建仓库内临时运行目录，并保留一个尚不存在的制品目标。"""

    try:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        work = repository_child(WORK_ROOT / run_id, WORK_ROOT, stage="config.work")
        artifact = repository_child(
            ARTIFACT_ROOT / run_id,
            ARTIFACT_ROOT,
            stage="config.artifact",
        )
        require(not work.exists() and not artifact.exists(), "config.run", "exists")
        work.mkdir()
        if owner_claim is not None:
            _write_owner_claim(work, owner_claim)
        for name in ("logs", "temp", "spool"):
            (work / name).mkdir()
        return work, artifact
    except AcceptanceFailure:
        raise
    # 目录创建失败不得附带 ACL、卷名或绝对路径进入正式输出。
    # 公开路径 lstat 或 identity 读取失败时不能把节点当作已删除或仍归本轮所有。
    except OSError:
        raise AcceptanceFailure("config.run", "filesystem") from None


def assert_ports_available() -> None:
    """只探测本验收器固定回环端口，不结束或接管已有监听者。"""

    for port in FIXED_PORTS:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((LOOPBACK, port))
        # 已占用端口只形成固定失败，验收器不会接管或终止原监听进程。
        except OSError:
            raise AcceptanceFailure("config.ports", "unavailable") from None
        finally:
            probe.close()


def database_url(path: Path) -> str:
    """为仓库内 SQLite 文件生成应用接受的规范数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def database_health(url: str, *, head: str, count: int, stage: str) -> dict[str, object]:
    """验证迁移头、记录数、SQLite 完整性和零外键违规。"""

    connection = connect_database(url)
    try:
        versions = connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version"
        ).fetchall()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        require(len(versions) == count, stage, "migration_count")
        require(str(versions[-1][0]) == head, stage, "migration_head")
        require(integrity == "ok", stage, "integrity")
        require(not foreign_keys, stage, "foreign_keys")
        return {
            "migration_count": len(versions),
            "latest_migration": head,
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        }
    finally:
        connection.close()


def regular_file_identity(path: Path, *, stage: str) -> tuple[int, int]:
    """返回普通非 reparse 文件的稳定 identity；不可证明时固定失败。"""

    try:
        information = os.lstat(path)
        canonical = path.resolve(strict=True)
    # 数据库路径无法稳定解析时拒绝继续，避免迁移或扫描落到替换后的节点。
    except OSError:
        raise AcceptanceFailure(stage, "database_identity") from None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    require(
        stat.S_ISREG(information.st_mode)
        and not stat.S_ISLNK(information.st_mode)
        and not (getattr(information, "st_file_attributes", 0) & reparse_flag)
        and os.path.normcase(str(canonical)) == os.path.normcase(str(path)),
        stage,
        "database_identity",
    )
    identity = (int(information.st_dev), int(information.st_ino))
    require(identity[1] > 0, stage, "database_identity")
    return identity


class WindowsDatabaseIdentityGuard:
    """用不共享删除权限的 Windows 句柄把 SQLite 路径固定到初始 File ID。"""

    def __init__(
        self,
        database: Path,
        expected_identity: tuple[int, int],
        *,
        stage: str,
    ) -> None:
        """OPEN_REPARSE_POINT 打开路径本身；任何不可证明状态均 fail closed。"""

        self._handle: Any | None = None
        self._kernel32: Any | None = None
        self.identity = expected_identity
        require(
            IDENTITY_BOUND_MUTATIONS_SUPPORTED,
            stage,
            "database_identity_unsupported",
        )
        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            """描述 Windows 句柄的卷编号、文件属性与稳定 FileIndex 身份布局。"""

            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        class FileId128(ctypes.Structure):
            """保存 SQLite 数据库句柄对应的 Windows 128-bit 稳定文件身份。"""

            _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

        class FileIdInfo(ctypes.Structure):
            """映射数据库卷序列号与 File ID，阻断 lstat 到 sqlite connect 的替换竞态。"""

            _fields_ = [
                ("VolumeSerialNumber", ctypes.c_ulonglong),
                ("FileId", FileId128),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ByHandleFileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        # FILE_SHARE_READ|WRITE 故意不含 DELETE，阻断从验证到 SQLite close 的 rename/unlink。
        handle = kernel32.CreateFileW(
            str(database),
            0,
            0x00000001 | 0x00000002,
            None,
            3,
            0x00200000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        require(handle not in {None, invalid_handle}, stage, "database_handle")
        information = ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure(stage, "database_handle")
        file_id_information = FileIdInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            18,
            ctypes.byref(file_id_information),
            ctypes.sizeof(file_id_information),
        ):
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure(stage, "database_handle")
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        handle_identity = (
            int(file_id_information.VolumeSerialNumber),
            int.from_bytes(bytes(file_id_information.FileId.Identifier), "little"),
        )
        if (
            int(information.dwFileAttributes) & reparse_flag
            or handle_identity != expected_identity
            or regular_file_identity(database, stage=stage) != expected_identity
        ):
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure(stage, "database_identity")
        self._kernel32 = kernel32
        self._handle = handle

    def verify(self, database: Path, *, stage: str) -> None:
        """每次写性 PRAGMA 前后复核公开路径仍指向句柄固定的初始文件。"""

        require(
            regular_file_identity(database, stage=stage) == self.identity,
            stage,
            "database_identity",
        )

    def close(self, *, stage: str) -> None:
        """释放 guard 句柄；失败时不得声称数据库已安全静止。"""

        if self._handle is None:
            return
        handle = self._handle
        if not self._kernel32.CloseHandle(handle):
            raise AcceptanceFailure(stage, "database_handle_close")
        self._handle = None


def _sqlite_connection_path(connection: sqlite3.Connection, *, stage: str) -> Path:
    """从 SQLite 自身的 database_list 获取 main 实际路径，不信任调用方字符串。"""

    rows = connection.execute("PRAGMA database_list").fetchall()
    main = [row for row in rows if len(row) >= 3 and str(row[1]) == "main"]
    require(len(main) == 1 and str(main[0][2]), stage, "database_list")
    try:
        return Path(str(main[0][2])).resolve(strict=True)
    # SQLite 返回的主库路径必须仍可解析，否则无法证明连接绑定到预期数据库。
    except OSError:
        raise AcceptanceFailure(stage, "database_list") from None


def quiesce_sqlite_database(
    database: Path,
    *,
    stage: str = "cleanup.sqlite",
    timeout_seconds: float = 5.0,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, int | str]:
    """在服务 owner 退出后正规收束 WAL，并证明没有 SQLite sidecar 残留。"""

    require(
        isinstance(timeout_seconds, (int, float))
        and not isinstance(timeout_seconds, bool)
        and 0 < float(timeout_seconds) <= 30,
        stage,
        "timeout",
    )
    require(
        IDENTITY_BOUND_MUTATIONS_SUPPORTED,
        stage,
        "database_identity_unsupported",
    )
    connection: sqlite3.Connection | None = None
    guard: WindowsDatabaseIdentityGuard | None = None
    try:
        initial_identity = regular_file_identity(database, stage=stage)
        if expected_identity is not None:
            require(
                initial_identity == expected_identity,
                stage,
                "database_identity",
            )
        guard = WindowsDatabaseIdentityGuard(database, initial_identity, stage=stage)
        guard.verify(database, stage=stage)
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=rw",
            uri=True,
            timeout=float(timeout_seconds),
        )
        actual = _sqlite_connection_path(connection, stage=stage)
        require(
            os.path.normcase(str(actual)) == os.path.normcase(str(database.resolve(strict=True)))
            and regular_file_identity(actual, stage=stage) == initial_identity,
            stage,
            "database_identity",
        )
        guard.verify(database, stage=stage)
        timeout_ms = max(1, int(float(timeout_seconds) * 1_000))
        connection.execute(f"PRAGMA busy_timeout={timeout_ms}")
        mode_row = connection.execute("PRAGMA journal_mode").fetchone()
        require(mode_row is not None, stage, "journal_mode")
        original_mode = str(mode_row[0]).casefold()
        checkpointed_frames = 0
        if original_mode == "wal":
            guard.verify(database, stage=stage)
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            require(checkpoint is not None and len(checkpoint) == 3, stage, "checkpoint")
            busy, _log_frames, checkpointed_frames = (int(value) for value in checkpoint)
            require(busy == 0, stage, "checkpoint_busy")
            guard.verify(database, stage=stage)
            delete_row = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            require(
                delete_row is not None and str(delete_row[0]).casefold() == "delete",
                stage,
                "journal_mode",
            )
            guard.verify(database, stage=stage)
        connection.close()
        connection = None
        guard.verify(database, stage=stage)
        guard.close(stage=stage)
        guard = None
        sidecars = tuple(
            database.with_name(f"{database.name}{suffix}")
            for suffix in ("-wal", "-shm", "-journal")
        )
        require(not any(_lexists(path) for path in sidecars), stage, "sidecars_remaining")
        return {
            "checkpointed_frames": checkpointed_frames,
            "journal_mode": "delete" if original_mode == "wal" else original_mode,
            "sqlite_sidecars_remaining": 0,
        }
    except AcceptanceFailure:
        raise
    # 文件系统或 SQLite 状态异常统一收敛，禁止把原始路径和数据库细节写入证据。
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise AcceptanceFailure(stage, "failed") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            # 主验收结果优先；关闭失败由后续身份与 sidecar 检查继续失败关闭。
            except sqlite3.Error:
                pass
        if guard is not None:
            try:
                guard.close(stage=stage)
            except AcceptanceFailure:
                pass


def migration_roundtrip(work: Path) -> dict[str, object]:
    """在独立空数据库真实执行零到七十、重复、回退到六十九及重新升级全过程。"""

    database = work / "migration-roundtrip.sqlite3"
    url = database_url(database)
    fresh = apply_migrations(url)
    require(tuple(fresh.applied) == MIGRATION_VERSIONS, "migration.fresh", "versions")
    fresh_health = database_health(url, head="0070", count=70, stage="migration.fresh")
    repeat = apply_migrations(url)
    require(not repeat.applied, "migration.repeat", "not_noop")
    repeat_health = database_health(url, head="0070", count=70, stage="migration.repeat")
    rolled_back = rollback_last_migration(url)
    require(rolled_back.rolled_back == "0070", "migration.down", "version")
    down_health = database_health(url, head="0069", count=69, stage="migration.down")
    reapplied = apply_migrations(url)
    require(reapplied.applied == ["0070"], "migration.reup", "version")
    reup_health = database_health(url, head="0070", count=70, stage="migration.reup")
    return {
        "fresh": fresh_health,
        "repeat": {**repeat_health, "applied_count": 0},
        "down": down_health,
        "re_up": reup_health,
    }


def migration_roundtrip_database(work: Path) -> Path:
    """返回本轮独立迁移验收库；路径固定在已验证 work owner 内。"""

    return work / "migration-roundtrip.sqlite3"


def seed_business_database(
    work: Path,
    secrets_: RuntimeSecrets,
    *,
    usage_cost_cny: str = "1.000000",
    include_normalized_cost: bool = True,
) -> BusinessGraph:
    """从另一个空库建立双租户及 admin/finance/security/developer 真实用户。"""

    database = work / "business.sqlite3"
    url = database_url(database)
    result = apply_migrations(url)
    require(tuple(result.applied) == MIGRATION_VERSIONS, "database.migrate", "versions")
    seed_environment = {
        "METAHUB_SEED_ADMIN_PASSWORD": secrets_.admin_password,
        "METAHUB_SEED_PROVIDER_API_KEY": secrets_.provider_key,
        "METAHUB_SEED_VIRTUAL_KEY": secrets_.virtual_key,
        "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    }
    second_seed = work / "second-tenant-seed.json"
    second_payload = {
        "tenant": {
            "slug": "prd077-runtime-second",
            "name": "PRD077 Runtime Second",
            "service_mode": "internal_only",
        },
        "admin": {
            "email": "admin-prd077-second@local.invalid",
            "display_name": "PRD077 second admin",
            "password": "environment-override-required",
        },
        "provider": {
            "provider_code": "local_openai",
            "name": "PRD077 second provider",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key_placeholder": "environment-override-required",
        },
        "model": {
            "model_alias": "default-chat",
            "upstream_model_name": "prd077-runtime-model",
            "provider_model_id": "prd077-runtime-model",
            "display_name": "PRD077 model",
            "context_window": 8192,
            "input_price_per_1k": 0.1,
            "output_price_per_1k": 0.3,
            "currency": "CNY",
            "price_schema_version": "prd077-runtime-v1",
        },
        "virtual_key": {
            "name": "PRD077 second key",
            "plain_value": "environment-override-required",
        },
    }
    try:
        second_seed.write_text(
            json.dumps(second_payload, ensure_ascii=True, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        with temporary_environment(seed_environment):
            first = apply_seed_data(url)
        second_environment = {
            **seed_environment,
            "METAHUB_SEED_ADMIN_PASSWORD": secrets_.second_password,
            "METAHUB_SEED_VIRTUAL_KEY": secrets_.second_virtual_key,
        }
        with temporary_environment(second_environment):
            second = apply_seed_data(url, seed_file=second_seed)
    finally:
        if second_seed.exists():
            second_seed.unlink()

    connection = connect_database(url)
    try:
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        password_hash = hash_password(secrets_.role_password)
        role_users = (
            ("usr_prd077_finance", "finance-prd077@local.invalid", "finance_admin"),
            ("usr_prd077_security", "security-prd077@local.invalid", "security_admin"),
            ("usr_prd077_developer", "developer-prd077@local.invalid", "developer"),
        )
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    user_id,
                    first.tenant_id,
                    email,
                    f"PRD077 {role}",
                    password_hash,
                    role,
                    now,
                    now,
                )
                for user_id, email, role in role_users
            ),
        )
        # 仅第一租户拥有该 request_id，第二租户查询必须返回同形 source_not_found。
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,
              snapshot_version,policy_snapshot_status,claimed_at
            ) VALUES(?,?,?,NULL,NULL,'disabled',?)
            """,
            (first.tenant_id, "req_prd077_runtime_owner", "trc_prd077_runtime_owner", now),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id,tenant_id,trace_id,status,error_code,latency_ms,
              created_at,updated_at
            ) VALUES(?,?,?,'failed','gateway.upstream_timeout',19,?,?)
            """,
            ("req_prd077_runtime_owner", first.tenant_id, "trc_prd077_runtime_owner", now, now),
        )

        # 真实 Usage 通过当前账本 trigger 同事务投影为非零 FinOps debit；不直接伪造分录。
        owner = connection.execute(
            """
            SELECT user.user_id,provider.provider_id,provider.provider_code,
                   model.model_id,model.model_alias,key.virtual_key_id,
                   key.environment_id,environment.code AS environment_code,
                   key.project_id,key.application_id
            FROM users user
            JOIN providers provider ON provider.tenant_id=user.tenant_id
            JOIN models model
              ON model.tenant_id=provider.tenant_id AND model.provider_id=provider.provider_id
            JOIN virtual_keys key ON key.tenant_id=user.tenant_id
            JOIN environments environment
              ON environment.tenant_id=key.tenant_id
             AND environment.environment_id=key.environment_id
            WHERE user.tenant_id=? AND user.email='admin@metahub.local'
            ORDER BY provider.provider_id,model.model_id,key.virtual_key_id LIMIT 1
            """,
            (first.tenant_id,),
        ).fetchone()
        require(owner is not None, "database.seed", "owner")
        usage_request_id = "req_prd077_runtime_usage"
        usage_trace_id = "trc_prd077_runtime_usage"
        usage_id = "usg_prd077_runtime_usage"
        price_snapshot_id = "prs_prd077_runtime_usage"
        price_schema_version = "prd077-runtime-price-v1"
        try:
            parsed_usage_cost = Decimal(usage_cost_cny)
        # 金额 canary 不能解析时仅返回固定 seed 坐标，禁止传播动态金额文本。
        except InvalidOperation:
            raise AcceptanceFailure("database.seed", "usage_cost") from None
        usage_cost_micros = parsed_usage_cost * 1_000_000
        require(
            parsed_usage_cost.is_finite()
            and usage_cost_micros == usage_cost_micros.to_integral_value()
            and 0 <= int(usage_cost_micros) < 100_000_000_000,
            "database.seed",
            "usage_cost",
        )
        usage_cost = usage_cost_cny
        normalized_usage_payload: dict[str, object] = {
            "cache_tokens": 0,
            "currency": "CNY",
            "input_tokens": 17,
            "output_tokens": 5,
            "posting_source": "direct",
            "price_schema_version": price_schema_version,
            "price_snapshot_id": price_snapshot_id,
            "total_tokens": 22,
        }
        if include_normalized_cost:
            normalized_usage_payload.update({"cost_amount": usage_cost, "cost_cny": usage_cost})
        normalized_usage = json.dumps(
            normalized_usage_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,
              snapshot_version,policy_snapshot_status,claimed_at
            ) VALUES(?,?,?,?,NULL,'disabled',?)
            """,
            (
                first.tenant_id,
                usage_request_id,
                usage_trace_id,
                owner["virtual_key_id"],
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
              endpoint,request_model,resolved_model,provider_code,status,
              input_tokens,output_tokens,total_tokens,estimated_cost_amount,
              currency,price_schema_version,environment_id,environment_code,
              project_id,application_id,latency_ms,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'chat/completions',?, ?,?,'success',17,5,22,
                     ?,'CNY',?,?,?,?,?,23,?,?)
            """,
            (
                usage_request_id,
                first.tenant_id,
                usage_trace_id,
                owner["virtual_key_id"],
                owner["provider_id"],
                owner["model_id"],
                owner["model_alias"],
                owner["model_alias"],
                owner["provider_code"],
                usage_cost,
                price_schema_version,
                owner["environment_id"],
                owner["environment_code"],
                owner["project_id"],
                owner["application_id"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO price_snapshots(
              price_snapshot_id,tenant_id,request_id,model_id,provider_id,
              currency,input_price_per_1k,output_price_per_1k,
              price_schema_version,billing_dimensions,provider_raw_mapping,
              source,created_at,updated_at
            ) VALUES(?,?,?,?,?,'CNY',50,30,?,?,'{}','runtime_acceptance',?,?)
            """,
            (
                price_snapshot_id,
                first.tenant_id,
                usage_request_id,
                owner["model_id"],
                owner["provider_id"],
                price_schema_version,
                json.dumps(
                    {"billing_unit": "1k_tokens", "usage_ledger_id": usage_id},
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,
              total_tokens,cache_tokens,raw_usage,normalized_usage,cost_amount,
              cost_cny,cost_usd,currency,price_snapshot_id,price_schema_version,
              model_id,provider_id,virtual_key_id,endpoint,request_model,resolved_model,
              provider_code,environment_id,environment_code,project_id,application_id,
              estimated,usage_source,created_at,updated_at
            ) VALUES(?,?,?,17,5,22,0,'{}',?,?,?,0,'CNY',?,?,?,?,?,
                     'chat/completions',?,?,?,?,?,?,?,0,'upstream',?,?)
            """,
            (
                usage_id,
                first.tenant_id,
                usage_request_id,
                normalized_usage,
                usage_cost,
                usage_cost,
                price_snapshot_id,
                price_schema_version,
                owner["model_id"],
                owner["provider_id"],
                owner["virtual_key_id"],
                owner["model_alias"],
                owner["model_alias"],
                owner["provider_code"],
                owner["environment_id"],
                owner["environment_code"],
                owner["project_id"],
                owner["application_id"],
                now,
                now,
            ),
        )
        finops = connection.execute(
            """
            SELECT direction,normalized_cny_micros,status
            FROM finops_ledger_entries
            WHERE tenant_id=? AND usage_ledger_id=?
            """,
            (first.tenant_id, usage_id),
        ).fetchone()
        require(
            finops is not None and tuple(finops) == ("debit", int(usage_cost_micros), "posted"),
            "database.seed",
            "finops_projection",
        )

        # Policy、Rule、Action 与 published Snapshot 使用真实约束顺序构造安全事实。
        policy_id = "pol_" + "1" * 32
        policy_rule_id = "prule_" + "2" * 32
        policy_action_id = "pact_" + "3" * 32
        snapshot_id = "psnap_" + "4" * 32
        snapshot_item_id = "psitem_" + "5" * 32
        snapshot_event_id = "psevt_" + "6" * 32
        expression = 'env == "prod"'
        compiled = compile_policy_expression(expression)
        snapshot_item = build_policy_snapshot_item(
            evaluation_order=0,
            policy_id=policy_id,
            policy_version=1,
            policy_rule_id=policy_rule_id,
            policy_action_id=policy_action_id,
            priority=100,
            expression=expression,
            action_type="allow",
        )
        snapshot_artifact = build_policy_snapshot_artifact(
            tenant_id=first.tenant_id,
            policy_snapshot_id=snapshot_id,
            snapshot_version=1,
            operation="publish",
            items=(snapshot_item,),
        )
        variables_json = json.dumps(
            list(compiled.variables),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO policies(
              policy_id,tenant_id,name,description,status,priority,version,
              dsl_version,created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,'PRD077 Runtime Policy','safe aggregate evidence',
                     'active',100,1,'policy-dsl-v1',?,?,?,?)
            """,
            (policy_id, first.tenant_id, owner["user_id"], owner["user_id"], now, now),
        )
        connection.execute(
            """
            INSERT INTO policy_rules(
              policy_rule_id,tenant_id,policy_id,condition_expr,condition_ast_json,
              condition_hash,variables_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                policy_rule_id,
                first.tenant_id,
                policy_id,
                expression,
                compiled.canonical_json,
                compiled.condition_hash,
                variables_json,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO policy_actions(
              policy_action_id,tenant_id,policy_id,action_type,target_model_id,
              created_at,updated_at
            ) VALUES(?,?,?,'allow',NULL,?,?)
            """,
            (policy_action_id, first.tenant_id, policy_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO policy_snapshots(
              policy_snapshot_id,tenant_id,snapshot_version,schema_version,state,
              operation,previous_snapshot_id,previous_snapshot_version,
              rollback_source_snapshot_id,rollback_source_snapshot_version,
              request_id,trace_id,request_fingerprint,content_hash,artifact_hash,
              policy_count,change_reason,approval_status,created_by,created_at,published_at
            ) VALUES(?, ?, 1, 'policy-snapshot-v1', 'building', 'publish', NULL, NULL,
                     NULL, NULL, 'req_prd077_runtime_snapshot',
                     'trc_prd077_runtime_snapshot', ?, NULL, NULL, NULL,
                     'PRD077 runtime evidence', 'not_required', ?, ?, NULL)
            """,
            (snapshot_id, first.tenant_id, "b" * 64, owner["user_id"], now),
        )
        connection.execute(
            """
            INSERT INTO policy_snapshot_items(
              policy_snapshot_item_id,tenant_id,policy_snapshot_id,evaluation_order,
              source_policy_id,source_policy_version,policy_rule_id,policy_action_id,
              priority,dsl_version,canonical_expression,condition_hash,variables_json,
              action_type,target_model_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_item_id,
                first.tenant_id,
                snapshot_id,
                snapshot_item.evaluation_order,
                snapshot_item.policy_id,
                snapshot_item.policy_version,
                snapshot_item.policy_rule_id,
                snapshot_item.policy_action_id,
                snapshot_item.priority,
                snapshot_item.dsl_version,
                snapshot_item.canonical_expression,
                snapshot_item.condition_hash,
                json.dumps(
                    list(snapshot_item.variables),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                snapshot_item.action_type,
                snapshot_item.target_model_id,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE policy_snapshots
            SET state='published',content_hash=?,artifact_hash=?,policy_count=1,
                published_at=?
            WHERE tenant_id=? AND policy_snapshot_id=?
            """,
            (
                snapshot_artifact.content_hash,
                snapshot_artifact.artifact_hash,
                now,
                first.tenant_id,
                snapshot_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO policy_snapshot_heads(
              tenant_id,current_snapshot_id,current_snapshot_version,updated_by,updated_at
            ) VALUES(?,?,1,?,?)
            """,
            (first.tenant_id, snapshot_id, owner["user_id"], now),
        )
        connection.execute(
            """
            INSERT INTO policy_snapshot_events(
              policy_snapshot_event_id,tenant_id,policy_snapshot_id,snapshot_version,
              event_type,operation,actor_user_id,request_id,trace_id,payload_json,created_at
            ) VALUES(?,?,?,1,'policy_snapshot.published','publish',?,
                     'req_prd077_runtime_snapshot','trc_prd077_runtime_snapshot',?,?)
            """,
            (
                snapshot_event_id,
                first.tenant_id,
                snapshot_id,
                owner["user_id"],
                json.dumps(
                    {
                        "content_hash": snapshot_artifact.content_hash,
                        "operation": "publish",
                        "policy_count": 1,
                        "previous_version": None,
                        "rollback_source_version": None,
                        "version": 1,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                now,
            ),
        )
        connection.commit()
    # 任何 seed SQL 失败都回滚当前写入，并隐藏 schema、路径和运行身份。
    except sqlite3.Error:
        connection.rollback()
        raise AcceptanceFailure("database.seed", "sql") from None
    finally:
        connection.close()
    database_health(url, head="0070", count=70, stage="database.health")
    return BusinessGraph(
        database=database,
        database_url=url,
        tenant_id=first.tenant_id,
        second_tenant_id=second.tenant_id,
    )


def service_environment(
    work: Path,
    graph: BusinessGraph,
    secrets_: RuntimeSecrets,
    *,
    event_enabled: bool,
) -> dict[str, str]:
    """构造最小子进程环境，Secret 与全部可写目录限定在本次运行。"""

    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    }
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    source_roots = (
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
    )
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(str(path) for path in source_roots),
            "TEMP": str(work / "temp"),
            "TMP": str(work / "temp"),
            "METAHUB_ENV": "dev",
            "METAHUB_LOG_LEVEL": "ERROR",
            "METAHUB_DATABASE_URL": graph.database_url,
            "METAHUB_AUTH_TOKEN_SECRET": secrets_.auth_secret,
            "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": secrets_.provider_secret,
            "METAHUB_FILE_ENCRYPTION_SECRET": secrets_.file_encryption_secret,
            "METAHUB_BATCH_ENCRYPTION_SECRET": secrets_.batch_encryption_secret,
            "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET": (
                secrets_.prompt_template_encryption_secret
            ),
            "METAHUB_CACHE_AFFINITY_HMAC_SECRET": secrets_.cache_affinity_hmac_secret,
            "METAHUB_SEED_VIRTUAL_KEY": secrets_.virtual_key,
            "METAHUB_FINOPS_USAGE_SPOOL_ROOT": str(work / "spool"),
            "METAHUB_GATEWAY_API_BASE_URL": "http://127.0.0.1:8000",
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_ALLOWED_ORIGINS": "http://localhost:5173",
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_USAGE_EXPORT_ENABLED": "false",
            "METAHUB_COST_ANOMALY_ENABLED": "false",
            "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED": "false",
            "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED": "false",
            "METAHUB_BATCH_WORKER_ENABLED": "false",
            "METAHUB_RESPONSE_CACHE_ENABLED": "false",
            "METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED": ("true" if event_enabled else "false"),
            "METAHUB_ADMIN_ASSISTANT_EVENT_POLL_INTERVAL_SECONDS": "1",
        }
    )
    return environment


def contains_forbidden_event_key(value: object) -> bool:
    """递归检查事件键名，禁止问题、回答、凭据、目标请求和成本进入 Webhook。"""

    if isinstance(value, dict):
        return any(
            str(key).casefold() in FORBIDDEN_EVENT_KEYS or contains_forbidden_event_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(contains_forbidden_event_key(child) for child in value)
    return False


class WebhookReceiver:
    """验证 assistant.queried 签名与 exact payload，只保存安全聚合计数。"""

    def __init__(self) -> None:
        """初始化未持有 Secret 的接收器和零计数状态。"""

        self._secret: str | None = None
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.valid = 0
        self.invalid = 0
        self.forbidden = 0

    def set_secret(self, value: str) -> None:
        """把首次创建返回的签名 Secret 只保存在当前接收器内存。"""

        require(len(value) > 32, "webhook.secret", "invalid")
        self._secret = value

    def start(self) -> str:
        """在固定回环端口启动接收器，不记录 Header、正文或来源地址。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """处理单个有界事件，并把动态错误折叠为计数。"""

            def do_POST(self) -> None:  # noqa: N802
                """验证长度、JSON、签名、事件类型和敏感键后返回固定状态。"""

                valid = False
                forbidden = False
                try:
                    lengths = self.headers.get_all("Content-Length", failobj=[])
                    # 重复或非十进制长度可能造成请求边界歧义，接收器必须拒绝。
                    if len(lengths) != 1 or not lengths[0].isdecimal():
                        raise ValueError("length")
                    size = int(lengths[0])
                    # 空事件或超过固定预算的事件不能进入 JSON 解码和签名验证。
                    if not 0 < size <= MAX_WEBHOOK_BYTES:
                        raise ValueError("size")
                    body = self.rfile.read(size)
                    # 实际读取不足声明长度说明传输截断，不能记作有效投递。
                    if len(body) != size:
                        raise ValueError("truncated")
                    payload = json.loads(body.decode("utf-8", errors="strict"))
                    event_type = payload.get("event_type") if isinstance(payload, dict) else None
                    forbidden = contains_forbidden_event_key(payload)
                    signature_ok = bool(owner._secret) and verify_webhook_signature(
                        self.headers.get("X-MetaHub-Signature", ""),
                        str(owner._secret),
                        body,
                    )
                    valid = (
                        event_type == "assistant.queried"
                        and self.headers.get("X-MetaHub-Event") == event_type
                        and signature_ok
                        and not forbidden
                    )
                # 编码、结构或长度异常只增加安全计数，不保存原始事件正文。
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    valid = False
                with owner._lock:
                    if forbidden:
                        owner.forbidden += 1
                    if valid:
                        owner.valid += 1
                    else:
                        owner.invalid += 1
                self.send_response(204 if valid else 400)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                """禁用默认访问日志，避免请求路径或来源信息进入验收文件。"""

                return

        try:
            self._server = ThreadingHTTPServer((LOOPBACK, WEBHOOK_PORT), Handler)
        # 回环监听失败只报告固定端口错误，不结束或检查其他进程详情。
        except OSError:
            raise AcceptanceFailure("webhook.start", "port") from None
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="prd077-runtime-webhook",
            daemon=True,
        )
        self._thread.start()
        return f"http://{LOOPBACK}:{WEBHOOK_PORT}/events"

    def stop(self) -> None:
        """关闭精确 server owner 并等待接收线程退出。"""

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def snapshot(self) -> dict[str, int]:
        """返回不含事件正文和 Secret 的线程安全聚合计数。"""

        with self._lock:
            return {
                "valid": self.valid,
                "invalid": self.invalid,
                "forbidden": self.forbidden,
            }


def create_webhook(
    client: httpx.Client,
    token: str,
    receiver: WebhookReceiver,
    endpoint_url: str,
) -> str:
    """通过真实 Control 创建仅订阅 assistant.queried 的活动 endpoint。"""

    response = client.post(
        "/webhooks",
        headers=request_headers(token, "prd077-runtime-webhook-create"),
        json={
            "name": "PRD077 runtime receiver",
            "endpoint_url": endpoint_url,
            "event_types": ["assistant.queried"],
            "status": "active",
            "timeout_ms": 2000,
        },
    )
    require(response.status_code == 200, "http.webhook", "status")
    payload = response.json()
    signing_secret = payload.get("signing_secret") if isinstance(payload, dict) else None
    require(isinstance(signing_secret, str), "http.webhook", "secret")
    receiver.set_secret(signing_secret)
    # 首次签名 Secret 只返回当前调用栈并加入零命中扫描，不写入证据。
    return signing_secret


def assistant_headers(token: str, suffix: str) -> dict[str, str]:
    """构造唯一认证和链路 Header，返回值只存在当前 HTTP 调用栈。"""

    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": f"req_prd077_runtime_{suffix}",
        "X-Trace-ID": f"trc_prd077_runtime_{suffix}",
    }


def ask(
    client: httpx.Client,
    token: str,
    suffix: str,
    question: str,
) -> httpx.Response:
    """发送真实 exact question-only POST，不记录问题或响应正文。"""

    return client.post(
        "/admin-assistant/query",
        headers=assistant_headers(token, suffix),
        json={"question": question},
    )


def assert_private_headers(response: httpx.Response, *, stage: str) -> None:
    """要求成功和失败响应均携带私有缓存及统一链路 Header。"""

    require(response.headers.get("cache-control") == "no-store, private", stage, "cache")
    require(response.headers.get("pragma") == "no-cache", stage, "pragma")
    require(response.headers.get("vary") == "Authorization", stage, "vary")
    require(bool(response.headers.get("x-request-id")), stage, "request_id")
    require(bool(response.headers.get("x-trace-id")), stage, "trace_id")


def assert_error(
    response: httpx.Response,
    *,
    status: int,
    code: str,
    stage: str,
) -> None:
    """验证统一错误状态、固定 code 及 body/header 链路一致性。"""

    require(response.status_code == status, stage, "status")
    payload = response.json()
    require(isinstance(payload, dict) and payload.get("code") == code, stage, "code")
    require(payload.get("request_id") == response.headers.get("x-request-id"), stage, "request")
    require(payload.get("trace_id") == response.headers.get("x-trace-id"), stage, "trace")
    assert_private_headers(response, stage=stage)


def private_answer_canary_groups(
    payload: object,
    *,
    stage: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """分离完整响应序列化与真实渲染叶值，供持久层按来源语义执行隐私扫描。"""

    require(isinstance(payload, dict), stage, "payload")
    answer = payload.get("answer")
    require(isinstance(answer, dict), stage, "answer")
    summary = answer.get("summary")
    require(isinstance(summary, str) and len(summary) >= 12, stage, "summary")
    # 只登记 PAGE-107 实际以 textContent 渲染的回答正文；claim_id/source_id 等协议标识会合法
    # 出现在 SQLite schema 或治理事实中，不能把该子串碰撞误判为 Response 持久化。
    leaf_values: list[str] = [summary]
    claims = answer.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            label = claim.get("label")
            if isinstance(label, str):
                leaf_values.append(label)
            raw_value = claim.get("value")
            if type(raw_value) in {int, str}:
                rendered_value = str(raw_value)
                leaf_values.append(rendered_value)
                unit = claim.get("unit")
                if isinstance(unit, str) and unit:
                    leaf_values.append(f"{rendered_value} {unit}")
    recommendations = answer.get("recommendations")
    if isinstance(recommendations, list):
        for recommendation in recommendations:
            if isinstance(recommendation, dict) and isinstance(
                recommendation.get("message"),
                str,
            ):
                leaf_values.append(recommendation["message"])
    sources = payload.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            label = source.get("label")
            if isinstance(label, str):
                leaf_values.append(label)
            started = source.get("window_started_at")
            ended = source.get("window_ended_at")
            snapshot = source.get("snapshot_at")
            record_count = source.get("record_count")
            if (
                isinstance(started, str)
                and isinstance(ended, str)
                and isinstance(snapshot, str)
                and type(record_count) is int
            ):
                leaf_values.extend(
                    (
                        started,
                        ended,
                        snapshot,
                        f"{started} 至 {ended} · {record_count} 条 · 快照 {snapshot}",
                    )
                )
    leaf_values = [value for value in leaf_values if len(value) >= MIN_PRIVATE_ANSWER_LEAF_LENGTH]
    serialized = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True),
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    return tuple(dict.fromkeys(serialized)), tuple(dict.fromkeys(leaf_values))


def private_answer_canaries(payload: object, *, stage: str) -> tuple[str, ...]:
    """返回完整回答与动态叶值的去重并集，供日志和 artifact 严格零命中扫描。"""

    serialized, leaves = private_answer_canary_groups(payload, stage=stage)
    return tuple(dict.fromkeys((*serialized, *leaves)))


def verify_runtime_private_storage(
    work: Path,
    *,
    private_values: tuple[str, ...],
    answer_serializations: tuple[str, ...],
    answer_leaves: tuple[str, ...],
    source_databases: tuple[Path, ...],
) -> dict[str, int]:
    """按来源语义扫描 Runtime：业务库允许既有源叶值，但不允许问题或完整回答。"""

    matches = 0
    sqlite_sidecars_remaining = 0
    try:
        root = Path(os.path.abspath(work))
        with _strict_bound_scan(work) as (
            snapshot,
            file_guards,
            directory_guards,
            tree_guard,
        ):
            files = snapshot.files
            source_database_keys: set[tuple[str, tuple[int, int]]] = set()
            for database in source_databases:
                lexical_path = Path(os.path.abspath(database))
                lexical_path.relative_to(root)
                identity = regular_file_identity(database, stage="privacy.scan")
                source_key = (os.path.normcase(str(lexical_path)), identity)
                require(
                    any(
                        (
                            os.path.normcase(str(Path(os.path.abspath(file.path)))),
                            file.identity,
                        )
                        == source_key
                        for file in files
                    ),
                    "privacy.scan",
                    "source_database",
                )
                source_database_keys.add(source_key)

            always_values = tuple(dict.fromkeys((*private_values, *answer_serializations)))
            output_values = tuple(dict.fromkeys((*always_values, *answer_leaves)))
            always_encodings = tuple(_BASE.sensitive_encodings(value) for value in always_values)
            output_encodings = tuple(_BASE.sensitive_encodings(value) for value in output_values)
            for file in files:
                path = file.path
                information = os.lstat(path)
                if path.name.casefold().endswith(SQLITE_SIDECAR_SUFFIXES):
                    # SQLite sidecar 本身仍证明 owner 未清理，且不能借来源数据库豁免叶值扫描。
                    sqlite_sidecars_remaining += 1
                require(
                    int(information.st_size) <= MAX_PRIVATE_SCAN_FILE_BYTES,
                    "privacy.scan",
                    "file_too_large",
                )
                content = _read_scanned_file(file, file_guards[_scan_path_key(path)])
                require(
                    regular_file_identity(path, stage="privacy.scan")
                    == file.identity
                    == (int(information.st_dev), int(information.st_ino)),
                    "privacy.scan",
                    "file_identity",
                )
                file_key = (
                    os.path.normcase(str(Path(os.path.abspath(path)))),
                    file.identity,
                )
                encodings = (
                    always_encodings if file_key in source_database_keys else output_encodings
                )
                matches += sum(any(encoded in content for encoded in group) for group in encodings)
            final_snapshot = _strict_rescan_snapshot(
                work,
                file_guards=file_guards,
                directory_guards=directory_guards,
            )
            require(final_snapshot == snapshot, "privacy.scan", "tree_changed")
            tree_guard.assert_unchanged(stage="privacy.scan")
    except AcceptanceFailure:
        raise
    # 扫描期间的路径、ACL、junction 或读取错误不得进入正式失败输出。
    except (OSError, RuntimeError, ValueError):
        raise AcceptanceFailure("privacy.scan", "filesystem") from None
    require(matches == 0, "privacy.scan", "plaintext_found")
    require(
        sqlite_sidecars_remaining == 0,
        "cleanup.sqlite_sidecars",
        "remaining",
    )
    return {
        "files_scanned": len(files),
        "plaintext_matches": matches,
        "sqlite_sidecars_remaining": sqlite_sidecars_remaining,
    }


def _read_scanned_file(file: ScannedFile, guard: WindowsPathIdentityGuard) -> bytes:
    """从绑定到枚举 identity 的句柄读取，并在读后再次证明公开路径未漂移。"""

    require(
        not guard.directory and guard.identity == file.identity,
        "privacy.scan",
        "file_identity",
    )
    try:
        opened_before: os.stat_result
        opened_after: os.stat_result
        if os.name == "nt":
            import msvcrt

            duplicate = msvcrt.open_osfhandle(
                _duplicate_windows_handle(guard.handle, stage="privacy.scan"),
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
            try:
                opened_before = os.fstat(duplicate)
                with os.fdopen(duplicate, "rb", closefd=False) as handle:
                    content = handle.read(MAX_PRIVATE_SCAN_FILE_BYTES + 1)
                opened_after = os.fstat(duplicate)
            finally:
                try:
                    os.close(duplicate)
                # 重复句柄关闭失败不能改写既有扫描结论，外层守卫仍绑定原始身份。
                except OSError:
                    pass
        else:
            descriptor = os.dup(guard.descriptor)
            try:
                opened_before = os.fstat(descriptor)
                require(
                    (int(opened_before.st_dev), int(opened_before.st_ino)) == file.identity
                    and stat.S_ISREG(opened_before.st_mode),
                    "privacy.scan",
                    "file_identity",
                )
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    content = handle.read(MAX_PRIVATE_SCAN_FILE_BYTES + 1)
                opened_after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        require(
            (int(opened_before.st_dev), int(opened_before.st_ino))
            == file.identity
            == (int(opened_after.st_dev), int(opened_after.st_ino))
            and int(opened_before.st_size) == file.size == len(content) == int(opened_after.st_size)
            and int(opened_before.st_mtime_ns) == file.mtime_ns == int(opened_after.st_mtime_ns),
            "privacy.scan",
            "file_changed",
        )
        guard.verify(stage="privacy.scan")
    except AcceptanceFailure:
        raise
    # 读取、句柄或身份复核失败统一按文件系统错误处理，证据中不泄露原始异常。
    except OSError:
        raise AcceptanceFailure("privacy.scan", "filesystem") from None
    require(len(content) <= MAX_PRIVATE_SCAN_FILE_BYTES, "privacy.scan", "file_too_large")
    return content


def _duplicate_windows_handle(handle: Any, *, stage: str) -> int:
    """复制已验证文件句柄供 Python buffered reader 使用，保留原 guard 所有权。"""

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    duplicate = wintypes.HANDLE()
    require(
        bool(
            kernel32.DuplicateHandle(
                process,
                handle,
                process,
                ctypes.byref(duplicate),
                0,
                False,
                0x00000002,
            )
        ),
        stage,
        "path_handle",
    )
    return int(duplicate.value)


def _scan_path_key(path: Path) -> str:
    """返回不跟随节点的规范化绝对词法键，供扫描句柄映射使用。"""

    return os.path.normcase(str(Path(os.path.abspath(path))))


@contextmanager
def _strict_bound_scan(
    work: Path,
) -> Iterator[
    tuple[
        ScanSnapshot,
        dict[str, WindowsPathIdentityGuard],
        dict[str, WindowsPathIdentityGuard],
        WindowsTreeChangeGuard,
    ]
]:
    """严格枚举完整树，并把所有目录和文件句柄保持到调用方完成复核。"""

    try:
        with ExitStack() as stack:
            tree_guard = stack.enter_context(WindowsTreeChangeGuard(work, stage="privacy.scan"))
            root_information = os.lstat(work)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            require(
                stat.S_ISDIR(root_information.st_mode)
                and not stat.S_ISLNK(root_information.st_mode)
                and not (getattr(root_information, "st_file_attributes", 0) & reparse_flag),
                "privacy.scan",
                "reparse",
            )
            files: list[ScannedFile] = []
            directories: list[ScannedDirectory] = []
            file_guards: dict[str, WindowsPathIdentityGuard] = {}
            directory_guards: dict[str, WindowsPathIdentityGuard] = {}
            root_identity = (int(root_information.st_dev), int(root_information.st_ino))
            pending = [(work, root_identity)]
            while pending:
                directory, expected_identity = pending.pop()
                guard = stack.enter_context(
                    WindowsPathIdentityGuard(
                        directory,
                        expected_identity,
                        stage="privacy.scan",
                        directory=True,
                    )
                )
                guard.verify(stage="privacy.scan")
                directory_key = _scan_path_key(directory)
                require(
                    directory_key not in directory_guards,
                    "privacy.scan",
                    "duplicate_path",
                )
                directory_guards[directory_key] = guard
                bound_directory = (
                    os.fstat(guard.descriptor)
                    if os.name != "nt"
                    else os.stat(directory, follow_symlinks=False)
                )
                directories.append(
                    ScannedDirectory(
                        directory,
                        expected_identity,
                        int(bound_directory.st_mtime_ns),
                    )
                )
                scan_target: Path | int = directory if os.name == "nt" else guard.descriptor
                with os.scandir(scan_target) as entries:
                    for entry in entries:
                        path = directory / entry.name
                        information = entry.stat(follow_symlinks=False)
                        require(
                            not entry.is_symlink()
                            and not (getattr(information, "st_file_attributes", 0) & reparse_flag),
                            "privacy.scan",
                            "reparse",
                        )
                        if stat.S_ISDIR(information.st_mode):
                            identity = (
                                owned_directory_identity(
                                    path,
                                    directory,
                                    stage="privacy.scan",
                                )
                                if os.name == "nt"
                                else (int(information.st_dev), int(information.st_ino))
                            )
                            require(identity[1] > 0, "privacy.scan", "owner_identity")
                            pending.append((path, identity))
                        elif stat.S_ISREG(information.st_mode):
                            identity = (
                                regular_file_identity(path, stage="privacy.scan")
                                if os.name == "nt"
                                else (int(information.st_dev), int(information.st_ino))
                            )
                            require(identity[1] > 0, "privacy.scan", "file_identity")
                            file_guard = stack.enter_context(
                                WindowsPathIdentityGuard(
                                    path,
                                    identity,
                                    stage="privacy.scan",
                                    directory=False,
                                    read=True,
                                )
                            )
                            file_guard.verify(stage="privacy.scan")
                            key = _scan_path_key(path)
                            require(key not in file_guards, "privacy.scan", "duplicate_path")
                            file_guards[key] = file_guard
                            files.append(
                                ScannedFile(
                                    path,
                                    identity,
                                    int(information.st_size),
                                    int(information.st_mtime_ns),
                                )
                            )
                        else:
                            raise AcceptanceFailure("privacy.scan", "unsupported_entry")
                guard.verify(stage="privacy.scan")
            snapshot = ScanSnapshot(
                tuple(sorted(files, key=lambda item: _scan_path_key(item.path))),
                tuple(sorted(directories, key=lambda item: _scan_path_key(item.path))),
            )
            yield snapshot, file_guards, directory_guards, tree_guard
    except AcceptanceFailure:
        raise
    # 冻结目录树期间的路径、身份或遍历异常必须失败关闭，不能交付部分快照。
    except (OSError, RuntimeError, ValueError):
        raise AcceptanceFailure("privacy.scan", "filesystem") from None


def _strict_rescan_snapshot(
    work: Path,
    *,
    file_guards: dict[str, WindowsPathIdentityGuard],
    directory_guards: dict[str, WindowsPathIdentityGuard],
) -> ScanSnapshot:
    """只从首次扫描持有的 guard 重枚举完整树，拒绝新增、缺失或 identity 漂移。"""

    try:
        root_key = _scan_path_key(work)
        require(root_key in directory_guards, "privacy.scan", "tree_changed")
        files: list[ScannedFile] = []
        directories: list[ScannedDirectory] = []
        pending = [work]
        visited_directories: set[str] = set()
        visited_files: set[str] = set()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        while pending:
            directory = pending.pop()
            directory_key = _scan_path_key(directory)
            guard = directory_guards.get(directory_key)
            require(
                guard is not None and directory_key not in visited_directories,
                "privacy.scan",
                "tree_changed",
            )
            guard.verify(stage="privacy.scan")
            visited_directories.add(directory_key)
            opened_directory = (
                os.fstat(guard.descriptor)
                if os.name != "nt"
                else os.stat(directory, follow_symlinks=False)
            )
            directories.append(
                ScannedDirectory(
                    directory,
                    guard.identity,
                    int(opened_directory.st_mtime_ns),
                )
            )
            scan_target: Path | int = directory if os.name == "nt" else guard.descriptor
            with os.scandir(scan_target) as entries:
                for entry in entries:
                    path = directory / entry.name
                    key = _scan_path_key(path)
                    information = entry.stat(follow_symlinks=False)
                    require(
                        not entry.is_symlink()
                        and not (getattr(information, "st_file_attributes", 0) & reparse_flag),
                        "privacy.scan",
                        "reparse",
                    )
                    if stat.S_ISDIR(information.st_mode):
                        identity = (
                            owned_directory_identity(path, directory, stage="privacy.scan")
                            if os.name == "nt"
                            else (int(information.st_dev), int(information.st_ino))
                        )
                        child_guard = directory_guards.get(key)
                        require(
                            child_guard is not None and child_guard.identity == identity,
                            "privacy.scan",
                            "tree_changed",
                        )
                        pending.append(path)
                    elif stat.S_ISREG(information.st_mode):
                        identity = (
                            regular_file_identity(path, stage="privacy.scan")
                            if os.name == "nt"
                            else (int(information.st_dev), int(information.st_ino))
                        )
                        file_guard = file_guards.get(key)
                        require(
                            file_guard is not None
                            and file_guard.identity == identity
                            and key not in visited_files,
                            "privacy.scan",
                            "tree_changed",
                        )
                        file_guard.verify(stage="privacy.scan")
                        opened_file = (
                            os.fstat(file_guard.descriptor) if os.name != "nt" else information
                        )
                        files.append(
                            ScannedFile(
                                path,
                                identity,
                                int(opened_file.st_size),
                                int(opened_file.st_mtime_ns),
                            )
                        )
                        visited_files.add(key)
                    else:
                        raise AcceptanceFailure("privacy.scan", "unsupported_entry")
        require(
            visited_directories == set(directory_guards) and visited_files == set(file_guards),
            "privacy.scan",
            "tree_changed",
        )
        return ScanSnapshot(
            tuple(sorted(files, key=lambda item: _scan_path_key(item.path))),
            tuple(sorted(directories, key=lambda item: _scan_path_key(item.path))),
        )
    except AcceptanceFailure:
        raise
    # 严格复扫无法形成一致身份集合时失败关闭，避免把竞态变化误判为安全。
    except (OSError, RuntimeError, ValueError):
        raise AcceptanceFailure("privacy.scan", "filesystem") from None


def verify_private_storage(work: Path, private_values: tuple[str, ...]) -> dict[str, int]:
    """PRD-077 自有 fail-closed 扫描：链接或 reparse 不能被静默跳过。"""

    matches = 0
    sqlite_sidecars_remaining = 0
    try:
        with _strict_bound_scan(work) as (
            snapshot,
            file_guards,
            directory_guards,
            tree_guard,
        ):
            files = snapshot.files
            encodings = tuple(_BASE.sensitive_encodings(value) for value in private_values)
            for file in files:
                path = file.path
                information = os.lstat(path)
                require(
                    stat.S_ISREG(information.st_mode),
                    "privacy.scan",
                    "unsupported_entry",
                )
                if path.name.casefold().endswith(SQLITE_SIDECAR_SUFFIXES):
                    sqlite_sidecars_remaining += 1
                require(
                    int(information.st_size) <= MAX_PRIVATE_SCAN_FILE_BYTES,
                    "privacy.scan",
                    "file_too_large",
                )
                content = _read_scanned_file(file, file_guards[_scan_path_key(path)])
                require(
                    regular_file_identity(path, stage="privacy.scan")
                    == file.identity
                    == (int(information.st_dev), int(information.st_ino)),
                    "privacy.scan",
                    "file_identity",
                )
                matches += sum(any(encoded in content for encoded in group) for group in encodings)
            final_snapshot = _strict_rescan_snapshot(
                work,
                file_guards=file_guards,
                directory_guards=directory_guards,
            )
            require(final_snapshot == snapshot, "privacy.scan", "tree_changed")
            tree_guard.assert_unchanged(stage="privacy.scan")
    except AcceptanceFailure:
        raise
    # 扫描或末次身份复核异常统一失败关闭，禁止发布不完整的隐私检查结论。
    except (OSError, RuntimeError, ValueError):
        raise AcceptanceFailure("privacy.scan", "filesystem") from None
    require(matches == 0, "privacy.scan", "plaintext_found")
    require(
        sqlite_sidecars_remaining == 0,
        "cleanup.sqlite_sidecars",
        "remaining",
    )
    return {
        "files_scanned": len(files),
        "plaintext_matches": matches,
        "sqlite_sidecars_remaining": sqlite_sidecars_remaining,
    }


def policy_source_schema_shape(graph: BusinessGraph) -> tuple[tuple[object, ...], ...]:
    """冻结 Snapshot head 表的公开结构，用于证明故障注入已完整恢复。"""

    connection = connect_database(graph.database_url)
    try:
        schema = tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT type,name,tbl_name FROM sqlite_schema
                WHERE name='policy_snapshot_heads' OR tbl_name='policy_snapshot_heads'
                ORDER BY type,name
                """
            )
        )
        columns = tuple(
            tuple(row) for row in connection.execute("PRAGMA table_info(policy_snapshot_heads)")
        )
        foreign_keys = tuple(
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_list(policy_snapshot_heads)")
        )
        indexes = tuple(
            tuple(row) for row in connection.execute("PRAGMA index_list(policy_snapshot_heads)")
        )
        require(bool(schema) and bool(columns), "source.failure", "schema_missing")
        return (*schema, *columns, *foreign_keys, *indexes)
    finally:
        connection.close()


def rename_policy_source(graph: BusinessGraph, *, available: bool) -> None:
    """只在一次性库中切换固定 Snapshot head 表名，制造或恢复 source failure。"""

    source = "policy_snapshot_heads_prd077_unavailable" if available else "policy_snapshot_heads"
    target = "policy_snapshot_heads" if available else "policy_snapshot_heads_prd077_unavailable"
    connection = connect_database(graph.database_url)
    try:
        existing = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type='table' AND name IN(
                  'policy_snapshot_heads','policy_snapshot_heads_prd077_unavailable'
                )
                """
            )
        }
        require(existing == {source}, "source.failure", "table_state")
        # 两个标识符均为模块内固定常量，不接受 run-id、问题或外部输入。
        connection.execute(f'ALTER TABLE "{source}" RENAME TO "{target}"')
        connection.commit()
    except AcceptanceFailure:
        if connection.in_transaction:
            connection.rollback()
        raise
    # schema 故障注入若遇到 SQLite 异常必须回滚，不能留下半损坏来源表。
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        raise AcceptanceFailure("source.failure", "schema_toggle") from None
    finally:
        connection.close()


def exercise_source_failure(
    client: httpx.Client,
    graph: BusinessGraph,
    token: str,
    question_canary: str,
) -> dict[str, object]:
    """通过真实缺表窗口验证固定 503、失败审计和完整 schema 恢复。"""

    before = policy_source_schema_shape(graph)
    renamed = False
    try:
        rename_policy_source(graph, available=False)
        renamed = True
        response = ask(
            client,
            token,
            "source_failure",
            f"explain the current policy {question_canary}",
        )
        assert_error(
            response,
            status=503,
            code="admin_assistant.source_unavailable",
            stage="source.failure",
        )
    finally:
        if renamed:
            rename_policy_source(graph, available=True)
    require(policy_source_schema_shape(graph) == before, "source.failure", "schema_drift")
    database_health(graph.database_url, head="0070", count=70, stage="source.failure")
    return {
        "http_503": True,
        "fixed_error_code": True,
        "failure_audited": True,
        "schema_restored": True,
    }


def exercise_concurrent_queries(
    token: str,
    question_canary: str,
    database_url: str,
) -> tuple[dict[str, int], tuple[str, ...], tuple[str, ...]]:
    """并发执行二十个 attempt，并冻结成功/期限失败与一对一审计数量。"""

    def submit(index: int) -> httpx.Response:
        """每个线程拥有独立 Client，避免把连接池状态误当成业务并发语义。"""

        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
            timeout=httpx.Timeout(CONCURRENT_HTTP_TIMEOUT_SECONDS, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as parallel_client:
            try:
                return ask(
                    parallel_client,
                    token,
                    f"concurrent_{index:02d}",
                    f"usage last 7 days {question_canary}",
                )
            except httpx.TimeoutException:
                # 并发超时只折叠为固定坐标，问题正文和目标地址不得进入输出。
                raise AcceptanceFailure("concurrency.http", "timeout") from None
            # 其他 HTTP 失败同样只保留网络分类，避免泄露请求与响应内容。
            except httpx.HTTPError:
                raise AcceptanceFailure("concurrency.http", "network") from None

    with ThreadPoolExecutor(
        max_workers=CONCURRENT_ATTEMPTS,
        thread_name_prefix="prd077-runtime",
    ) as executor:
        responses = tuple(executor.map(submit, range(CONCURRENT_ATTEMPTS)))

    query_ids: set[str] = set()
    audit_ids: set[str] = set()
    response_serializations: list[str] = []
    response_leaves: list[str] = []
    success_count = 0
    unavailable_count = 0
    for index, response in enumerate(responses):
        stage = f"concurrency.{index:02d}"
        if response.status_code == 503:
            assert_error(
                response,
                status=503,
                code="admin_assistant.source_unavailable",
                stage=stage,
            )
            unavailable_count += 1
            continue
        require(response.status_code == 200, stage, "status")
        success_count += 1
        assert_private_headers(response, stage=stage)
        payload = response.json()
        require(payload.get("query_type") == "usage_statistics", stage, "type")
        query_id = payload.get("assistant_query_id")
        audit_id = payload.get("assistant_audit_id")
        answer = payload.get("answer")
        sources = payload.get("sources")
        require(isinstance(query_id, str) and query_id.startswith("asq_"), stage, "query_id")
        require(isinstance(audit_id, str) and audit_id.startswith("asa_"), stage, "audit_id")
        require(isinstance(answer, dict), stage, "answer")
        require(isinstance(sources, list) and sources, stage, "sources")
        query_ids.add(query_id)
        audit_ids.add(audit_id)
        serialized, leaves = private_answer_canary_groups(payload, stage=stage)
        response_serializations.extend(serialized)
        response_leaves.extend(leaves)
    require(success_count > 0, "concurrency", "no_success")
    require(
        success_count + unavailable_count == CONCURRENT_ATTEMPTS,
        "concurrency",
        "status_count",
    )
    require(len(query_ids) == success_count, "concurrency", "response_query_ids")
    require(len(audit_ids) == success_count, "concurrency", "response_audit_ids")

    connection = connect_database(database_url)
    try:
        audit_graph = connection.execute(
            """
            SELECT COUNT(*) AS attempts,
                   COUNT(DISTINCT query.assistant_query_id) AS unique_queries,
                   COUNT(DISTINCT audit.assistant_audit_id) AS unique_audits,
                   COUNT(DISTINCT audit.assistant_event_id) AS unique_events,
                   COUNT(DISTINCT log.audit_log_id) AS unique_logs,
                   COALESCE(SUM(CASE WHEN query.outcome='answered' THEN 1 ELSE 0 END),0)
                     AS answered_count,
                   COALESCE(SUM(CASE
                     WHEN query.outcome='failed'
                      AND query.failure_code='admin_assistant.source_unavailable'
                     THEN 1 ELSE 0 END),0) AS unavailable_count
            FROM assistant_queries query
            JOIN assistant_audits audit
              ON audit.tenant_id=query.tenant_id
             AND audit.assistant_query_id=query.assistant_query_id
            JOIN audit_logs log
              ON log.tenant_id=audit.tenant_id
             AND log.audit_log_id=audit.audit_log_id
            WHERE query.operation_request_id LIKE 'req_prd077_runtime_concurrent_%'
            """
        ).fetchone()
    finally:
        connection.close()
    require(
        audit_graph is not None
        and tuple(int(value or 0) for value in audit_graph)
        == (
            CONCURRENT_ATTEMPTS,
            CONCURRENT_ATTEMPTS,
            CONCURRENT_ATTEMPTS,
            CONCURRENT_ATTEMPTS,
            CONCURRENT_ATTEMPTS,
            success_count,
            unavailable_count,
        ),
        "concurrency",
        "audit_graph",
    )
    return (
        {
            "attempts": CONCURRENT_ATTEMPTS,
            "http_200": success_count,
            "http_503": unavailable_count,
            "unique_query_ids": CONCURRENT_ATTEMPTS,
            "unique_audit_ids": CONCURRENT_ATTEMPTS,
            "unique_event_ids": CONCURRENT_ATTEMPTS,
            "unique_audit_log_ids": CONCURRENT_ATTEMPTS,
        },
        tuple(dict.fromkeys(response_serializations)),
        tuple(dict.fromkeys(response_leaves)),
    )


def wait_for_event_recovery(
    graph: BusinessGraph,
    receiver: WebhookReceiver,
    *,
    expected_webhooks: int,
) -> dict[str, int]:
    """等待所有 source 离开 pending/processing，并验证订阅租户收到签名事件。"""

    started = time.monotonic()
    deadline = started + EVENT_RECOVERY_TIMEOUT_SECONDS
    last_progress = started
    previous_state: tuple[int, int] | None = None
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            unsettled = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM assistant_audits
                    WHERE event_dispatch_status IN('pending','processing')
                    """
                ).fetchone()[0]
            )
            failed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM assistant_audits WHERE event_dispatch_status='failed'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        snapshot = receiver.snapshot()
        state = (unsettled, snapshot["valid"])
        now = time.monotonic()
        if previous_state is None or state[0] < previous_state[0] or state[1] > previous_state[1]:
            last_progress = now
        previous_state = state
        if unsettled == 0 and snapshot["valid"] >= expected_webhooks:
            require(failed == 0, "recovery.outbox", "failed")
            require(snapshot["invalid"] == 0, "recovery.webhook", "invalid")
            require(snapshot["forbidden"] == 0, "recovery.webhook", "forbidden")
            return {
                "pending_or_processing": 0,
                "failed": 0,
                "valid_webhooks": snapshot["valid"],
                "invalid_webhooks": 0,
                "forbidden_webhooks": 0,
            }
        require(
            now - last_progress < EVENT_RECOVERY_STALL_SECONDS,
            "recovery.outbox",
            "stalled",
        )
        time.sleep(0.2)
    raise AcceptanceFailure("recovery.outbox", "timeout")


def inspect_business_database(graph: BusinessGraph) -> dict[str, object]:
    """核对 Query/Audit/AuditLog 一对一、零模型调用、租户列与数据库健康。"""

    connection = connect_database(graph.database_url)
    try:
        query_count = int(
            connection.execute("SELECT COUNT(*) FROM assistant_queries").fetchone()[0]
        )
        audit_count = int(connection.execute("SELECT COUNT(*) FROM assistant_audits").fetchone()[0])
        log_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='admin_assistant.queried'"
            ).fetchone()[0]
        )
        event_count = int(
            connection.execute(
                "SELECT COUNT(DISTINCT assistant_event_id) FROM assistant_audits"
            ).fetchone()[0]
        )
        zero_model = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM assistant_queries
                WHERE model_call_count=0 AND model_input_tokens=0
                  AND model_output_tokens=0 AND model_cost_cny_micros=0
                  AND model_cost_status='not_applicable_local'
                """
            ).fetchone()[0]
        )
        joined = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM assistant_queries query
                JOIN assistant_audits audit
                  ON audit.tenant_id=query.tenant_id
                 AND audit.assistant_query_id=query.assistant_query_id
                JOIN audit_logs log
                  ON log.tenant_id=audit.tenant_id
                 AND log.audit_log_id=audit.audit_log_id
                """
            ).fetchone()[0]
        )
        distinct_operations = int(
            connection.execute(
                "SELECT COUNT(DISTINCT operation_request_id) FROM assistant_queries"
            ).fetchone()[0]
        )
        concurrent = connection.execute(
            """
            SELECT COUNT(*) AS attempts,
                   COALESCE(SUM(CASE WHEN outcome='answered' THEN 1 ELSE 0 END),0)
                     AS answered_count,
                   COALESCE(SUM(CASE
                     WHEN outcome='failed'
                      AND failure_code='admin_assistant.source_unavailable'
                     THEN 1 ELSE 0 END),0) AS unavailable_count
            FROM assistant_queries
            WHERE operation_request_id LIKE 'req_prd077_runtime_concurrent_%'
            """
        ).fetchone()
        concurrent_values = tuple(int(value or 0) for value in concurrent)
        source_failure = connection.execute(
            """
            SELECT outcome,failure_code,claim_count,source_count
            FROM assistant_queries
            WHERE operation_request_id='req_prd077_runtime_source_failure'
            """
        ).fetchone()
        source_facts = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM request_logs WHERE tenant_id=?) AS request_logs,
              (SELECT COUNT(*) FROM usage_ledger WHERE tenant_id=?) AS usage_rows,
              (SELECT COALESCE(SUM(total_tokens),0) FROM usage_ledger
                WHERE tenant_id=?) AS usage_tokens,
              (SELECT COALESCE(SUM(CAST(ROUND(cost_cny*1000000) AS INTEGER)),0)
                FROM usage_ledger WHERE tenant_id=?) AS usage_cost_cny_micros,
              (SELECT COUNT(*) FROM finops_ledger_entries WHERE tenant_id=?) AS finops_rows,
              (SELECT COALESCE(SUM(normalized_cny_micros),0)
                FROM finops_ledger_entries WHERE tenant_id=?) AS finops_cost_cny_micros,
              (SELECT COUNT(*) FROM policies
                WHERE tenant_id=? AND status='active') AS active_policies,
              (SELECT COUNT(*) FROM policy_snapshot_heads WHERE tenant_id=?) AS snapshot_heads,
              (SELECT COALESCE(MAX(current_snapshot_version),0)
                FROM policy_snapshot_heads WHERE tenant_id=?) AS snapshot_version
            """,
            (graph.tenant_id,) * 9,
        ).fetchone()
        require(source_facts is not None, "database.source_facts", "missing")
        columns = {
            str(row[1]).casefold()
            for table in ("assistant_queries", "assistant_audits")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    require(
        query_count == EXPECTED_ASSISTANT_ATTEMPTS,
        "database.audit_graph",
        "query_count",
    )
    require(
        query_count == audit_count == log_count == event_count == joined,
        "database.audit_graph",
        "not_one_to_one",
    )
    require(distinct_operations == query_count, "database.audit_graph", "operation_ids")
    require(
        concurrent_values[0] == CONCURRENT_ATTEMPTS
        and concurrent_values[1] > 0
        and concurrent_values[1] + concurrent_values[2] == CONCURRENT_ATTEMPTS,
        "database.audit_graph",
        "concurrency",
    )
    require(
        source_failure is not None
        and tuple(source_failure) == ("failed", "admin_assistant.source_unavailable", 0, 0),
        "database.audit_graph",
        "source_failure",
    )
    source_values = tuple(int(value or 0) for value in source_facts)
    require(
        source_values == (2, 1, 22, 1_000_000, 1, 1_000_000, 1, 1, 1),
        "database.source_facts",
        "incomplete",
    )
    require(zero_model == query_count, "database.model_usage", "nonzero")
    require(
        columns.isdisjoint({"question", "answer", "prompt", "response", "token"}),
        "database.schema",
        "content_column",
    )
    require(integrity == "ok" and not foreign_keys, "database.health", "failed")
    return {
        "query_count": query_count,
        "audit_count": audit_count,
        "audit_log_count": log_count,
        "event_count": event_count,
        "one_to_one": True,
        "unique_operation_request_count": distinct_operations,
        "concurrent_attempt_count": concurrent_values[0],
        "concurrent_answered_count": concurrent_values[1],
        "concurrent_source_unavailable_count": concurrent_values[2],
        "source_failure_audited": True,
        "zero_model_usage_count": zero_model,
        "source_facts": {
            "request_log_count": source_values[0],
            "usage_count": source_values[1],
            "usage_token_count": source_values[2],
            "usage_cost_cny_micros": source_values[3],
            "finops_count": source_values[4],
            "finops_cost_cny_micros": source_values[5],
            "active_policy_count": source_values[6],
            "published_snapshot_head_count": source_values[7],
            "published_snapshot_version": source_values[8],
        },
        "content_columns": 0,
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }


def exercise_runtime(
    work: Path,
    graph: BusinessGraph,
    secrets_: RuntimeSecrets,
    receiver: WebhookReceiver,
    endpoint_url: str,
) -> tuple[
    dict[str, object],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """贯通真实认证、四类查询、角色边界、租户隔离与停服恢复。"""

    processes = OwnedProcesses()
    private_values = list(secrets_.scan_values())
    answer_serializations: list[str] = []
    answer_leaves: list[str] = []

    def retain_answer_canaries(payload: object, *, stage: str) -> None:
        """只在当前验收进程内聚合完整回答与叶值，发布前按不同持久层作用域扫描。"""

        serialized, leaves = private_answer_canary_groups(payload, stage=stage)
        answer_serializations.extend(serialized)
        answer_leaves.extend(leaves)

    initial_environment = service_environment(
        work,
        graph,
        secrets_,
        event_enabled=False,
    )
    initial = processes.add(
        start_service(
            "control-initial",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=initial_environment,
        )
    )
    wait_health(f"http://{LOOPBACK}:{CONTROL_PORT}/health", initial, stage="health.initial")
    facts: dict[str, object] = {}
    try:
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
            timeout=httpx.Timeout(10, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            admin = login(
                client,
                "default",
                "admin@metahub.local",
                secrets_.admin_password,
                "prd077-runtime-login-admin",
            )
            finance = login(
                client,
                "default",
                "finance-prd077@local.invalid",
                secrets_.role_password,
                "prd077-runtime-login-finance",
            )
            security = login(
                client,
                "default",
                "security-prd077@local.invalid",
                secrets_.role_password,
                "prd077-runtime-login-security",
            )
            developer = login(
                client,
                "default",
                "developer-prd077@local.invalid",
                secrets_.role_password,
                "prd077-runtime-login-developer",
            )
            second_admin = login(
                client,
                "prd077-runtime-second",
                "admin-prd077-second@local.invalid",
                secrets_.second_password,
                "prd077-runtime-login-second",
            )
            private_values.extend((admin, finance, security, developer, second_admin))
            webhook_secret = create_webhook(client, admin, receiver, endpoint_url)
            private_values.append(webhook_secret)

            cases = (
                ("usage", "usage in the last 7 days", "usage_statistics"),
                ("errors", "explain errors in the last 24 hours", "error_explanation"),
                ("cost", "cost advice for the last 30 days", "cost_advice"),
                ("policy", "explain the current policy", "policy_explanation"),
            )
            expected_claims: dict[str, dict[str, object]] = {
                "usage": {
                    "request_count": 2,
                    "input_tokens": 17,
                    "output_tokens": 5,
                    "total_tokens": 22,
                    "cost_cny": "1",
                },
                "errors": {"failure_count": 1, "error_1_code": "gateway.upstream_timeout"},
                "cost": {
                    "usage_record_count": 1,
                    "gross_debit_cost_cny": "1",
                    "top_cost_model": "default-chat",
                    "top_cost_model_share": "100",
                },
                "policy": {
                    "active_policy_count": 1,
                    "published_snapshot_version": 1,
                    "policy_action_1": "allow",
                    "policy_action_1_count": 1,
                },
            }
            source_count = 0
            for suffix, question, expected_type in cases:
                response = ask(
                    client,
                    admin,
                    suffix,
                    f"{question} {secrets_.question_canary}",
                )
                require(response.status_code == 200, f"http.{suffix}", "status")
                assert_private_headers(response, stage=f"http.{suffix}")
                payload = response.json()
                require(payload.get("query_type") == expected_type, f"http.{suffix}", "type")
                sources = payload.get("sources")
                answer = payload.get("answer")
                require(isinstance(sources, list) and sources, f"http.{suffix}", "sources")
                require(isinstance(answer, dict), f"http.{suffix}", "answer")
                claims = answer.get("claims")
                require(isinstance(claims, list), f"http.{suffix}", "claims")
                claim_values = {
                    str(claim.get("claim_id")): claim.get("value")
                    for claim in claims
                    if isinstance(claim, dict)
                }
                require(
                    all(
                        claim_values.get(claim_id) == expected
                        for claim_id, expected in expected_claims[suffix].items()
                    ),
                    f"http.{suffix}",
                    "seed_projection",
                )
                require(
                    all(
                        isinstance(source, dict) and str(source.get("href", "")).startswith("/#")
                        for source in sources
                    ),
                    f"http.{suffix}",
                    "source_href",
                )
                source_count += len(sources)
                retain_answer_canaries(payload, stage=f"http.{suffix}")

            finance_ok = ask(
                client,
                finance,
                "finance_usage",
                f"usage last 7 days {secrets_.question_canary}",
            )
            require(finance_ok.status_code == 200, "access.finance_usage", "status")
            assert_private_headers(finance_ok, stage="access.finance_usage")
            retain_answer_canaries(
                finance_ok.json(),
                stage="access.finance_usage",
            )
            assert_error(
                ask(
                    client,
                    finance,
                    "finance_error_denied",
                    f"explain errors last 24 hours {secrets_.question_canary}",
                ),
                status=403,
                code="auth.permission_denied",
                stage="access.finance_error",
            )
            security_ok = ask(
                client,
                security,
                "security_error",
                f"explain errors last 24 hours {secrets_.question_canary}",
            )
            require(security_ok.status_code == 200, "access.security_error", "status")
            assert_private_headers(security_ok, stage="access.security_error")
            retain_answer_canaries(
                security_ok.json(),
                stage="access.security_error",
            )
            assert_error(
                ask(
                    client,
                    security,
                    "security_cost_denied",
                    f"cost advice last 30 days {secrets_.question_canary}",
                ),
                status=403,
                code="auth.permission_denied",
                stage="access.security_cost",
            )
            assert_error(
                ask(
                    client,
                    developer,
                    "developer_denied",
                    f"usage last 7 days {secrets_.question_canary}",
                ),
                status=403,
                code="auth.permission_denied",
                stage="access.developer",
            )
            second_ok = ask(
                client,
                second_admin,
                "second_usage",
                f"usage last 7 days {secrets_.question_canary}",
            )
            require(second_ok.status_code == 200, "isolation.second", "status")
            assert_private_headers(second_ok, stage="isolation.second")
            retain_answer_canaries(
                second_ok.json(),
                stage="isolation.second",
            )
            assert_error(
                ask(
                    client,
                    second_admin,
                    "second_cross",
                    (
                        "explain errors for req_prd077_runtime_owner in the last 24 hours "
                        f"{secrets_.question_canary}"
                    ),
                ),
                status=404,
                code="admin_assistant.source_not_found",
                stage="isolation.cross_tenant",
            )
            assert_error(
                ask(
                    client,
                    admin,
                    "readonly",
                    f"update policy and explain cost {secrets_.question_canary}",
                ),
                status=403,
                code="admin_assistant.read_only_violation",
                stage="access.read_only",
            )
            source_failure = exercise_source_failure(
                client,
                graph,
                admin,
                secrets_.question_canary,
            )
            concurrency, concurrent_serializations, concurrent_leaves = exercise_concurrent_queries(
                admin,
                secrets_.question_canary,
                graph.database_url,
            )
            answer_serializations.extend(concurrent_serializations)
            answer_leaves.extend(concurrent_leaves)
            facts = {
                "services": {"initial_control_http_200": True},
                "queries": {
                    "supported_query_types": 4,
                    "source_links_checked": source_count,
                    "private_response_headers": True,
                },
                "source_failure": source_failure,
                "concurrency": concurrency,
                "access_control": {
                    "finance_allowed_usage": True,
                    "finance_denied_error": True,
                    "security_allowed_error": True,
                    "security_denied_cost": True,
                    "developer_base_denied": True,
                    "read_only_action_blocked": True,
                },
                "isolation": {
                    "second_tenant_query": True,
                    "cross_tenant_request_not_found": True,
                },
            }

        # 初始进程明确禁用 worker，所有 source 必须在真实停服前保持 pending。
        connection = connect_database(graph.database_url)
        try:
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM assistant_audits WHERE event_dispatch_status='pending'"
                ).fetchone()[0]
            )
            subscribed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM assistant_audits WHERE tenant_id=?",
                    (graph.tenant_id,),
                ).fetchone()[0]
            )
        finally:
            connection.close()
        require(pending > 0 and subscribed > 0, "recovery.pending", "missing")
        processes.stop(initial)

        recovery_environment = service_environment(
            work,
            graph,
            secrets_,
            event_enabled=True,
        )
        recovered = processes.add(
            start_service(
                "control-recovered",
                "metahub_control.app:app",
                CONTROL_PORT,
                work=work,
                environment=recovery_environment,
            )
        )
        wait_health(
            f"http://{LOOPBACK}:{CONTROL_PORT}/health",
            recovered,
            stage="health.recovered",
        )
        recovery = wait_for_event_recovery(
            graph,
            receiver,
            expected_webhooks=subscribed,
        )
        facts["services"]["recovered_control_http_200"] = True  # type: ignore[index]
        facts["event_recovery"] = {
            "source_pending_before_stop": pending,
            "restart_recovered": True,
            **recovery,
        }
        facts["database"] = inspect_business_database(graph)
        return (
            facts,
            tuple(dict.fromkeys(private_values)),
            tuple(dict.fromkeys(answer_serializations)),
            tuple(dict.fromkeys(answer_leaves)),
        )
    finally:
        processes.close()


def validate_runtime_coverage(
    facts: dict[str, object],
    migration: dict[str, object],
) -> None:
    """只有完整 Runtime 与四阶段迁移事实同时存在时才允许发布 passed。"""

    require(
        type(facts) is dict and set(facts) == RUNTIME_FACT_KEYS,
        "evidence.coverage",
        "facts_shape",
    )
    exact_sections: dict[str, dict[str, object]] = {
        "services": {
            "initial_control_http_200": True,
            "recovered_control_http_200": True,
        },
        "source_failure": {
            "http_503": True,
            "fixed_error_code": True,
            "failure_audited": True,
            "schema_restored": True,
        },
        "access_control": {
            "finance_allowed_usage": True,
            "finance_denied_error": True,
            "security_allowed_error": True,
            "security_denied_cost": True,
            "developer_base_denied": True,
            "read_only_action_blocked": True,
        },
        "isolation": {
            "second_tenant_query": True,
            "cross_tenant_request_not_found": True,
        },
    }
    for name, expected in exact_sections.items():
        require(
            type(facts.get(name)) is dict and facts[name] == expected,
            "evidence.coverage",
            name,
        )

    concurrency = facts.get("concurrency")
    concurrency_keys = {
        "attempts",
        "http_200",
        "http_503",
        "unique_query_ids",
        "unique_audit_ids",
        "unique_event_ids",
        "unique_audit_log_ids",
    }
    require(
        type(concurrency) is dict
        and set(concurrency) == concurrency_keys
        and all(type(concurrency[key]) is int for key in concurrency_keys)
        and concurrency["attempts"] == CONCURRENT_ATTEMPTS
        and concurrency["http_200"] > 0
        and concurrency["http_503"] >= 0
        and concurrency["http_200"] + concurrency["http_503"] == CONCURRENT_ATTEMPTS
        and all(
            concurrency[key] == CONCURRENT_ATTEMPTS
            for key in (
                "unique_query_ids",
                "unique_audit_ids",
                "unique_event_ids",
                "unique_audit_log_ids",
            )
        ),
        "evidence.coverage",
        "concurrency",
    )

    database = facts.get("database")
    database_keys = {
        "query_count",
        "audit_count",
        "audit_log_count",
        "event_count",
        "one_to_one",
        "unique_operation_request_count",
        "concurrent_attempt_count",
        "concurrent_answered_count",
        "concurrent_source_unavailable_count",
        "source_failure_audited",
        "zero_model_usage_count",
        "source_facts",
        "content_columns",
        "integrity_check",
        "foreign_key_violations",
    }
    fixed_database = {
        "query_count": EXPECTED_ASSISTANT_ATTEMPTS,
        "audit_count": EXPECTED_ASSISTANT_ATTEMPTS,
        "audit_log_count": EXPECTED_ASSISTANT_ATTEMPTS,
        "event_count": EXPECTED_ASSISTANT_ATTEMPTS,
        "one_to_one": True,
        "unique_operation_request_count": EXPECTED_ASSISTANT_ATTEMPTS,
        "source_failure_audited": True,
        "zero_model_usage_count": EXPECTED_ASSISTANT_ATTEMPTS,
        "source_facts": {
            "request_log_count": 2,
            "usage_count": 1,
            "usage_token_count": 22,
            "usage_cost_cny_micros": 1_000_000,
            "finops_count": 1,
            "finops_cost_cny_micros": 1_000_000,
            "active_policy_count": 1,
            "published_snapshot_head_count": 1,
            "published_snapshot_version": 1,
        },
        "content_columns": 0,
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }
    require(
        type(database) is dict
        and set(database) == database_keys
        and all(database.get(key) == value for key, value in fixed_database.items())
        and database.get("concurrent_attempt_count") == CONCURRENT_ATTEMPTS
        and database.get("concurrent_answered_count") == concurrency["http_200"]
        and database.get("concurrent_source_unavailable_count") == concurrency["http_503"],
        "evidence.coverage",
        "database",
    )

    queries = facts.get("queries")
    require(
        type(queries) is dict
        and set(queries)
        == {
            "supported_query_types",
            "source_links_checked",
            "private_response_headers",
        }
        and queries.get("supported_query_types") == 4
        and type(queries.get("source_links_checked")) is int
        and int(queries["source_links_checked"]) >= 4
        and queries.get("private_response_headers") is True,
        "evidence.coverage",
        "queries",
    )
    recovery = facts.get("event_recovery")
    require(
        type(recovery) is dict
        and set(recovery)
        == {
            "source_pending_before_stop",
            "restart_recovered",
            "pending_or_processing",
            "failed",
            "valid_webhooks",
            "invalid_webhooks",
            "forbidden_webhooks",
        }
        and recovery.get("source_pending_before_stop") == EXPECTED_ASSISTANT_ATTEMPTS
        and recovery.get("restart_recovered") is True
        and recovery.get("pending_or_processing") == 0
        and recovery.get("failed") == 0
        and type(recovery.get("valid_webhooks")) is int
        and int(recovery["valid_webhooks"]) >= EXPECTED_FIRST_TENANT_ATTEMPTS
        and recovery.get("invalid_webhooks") == 0
        and recovery.get("forbidden_webhooks") == 0,
        "evidence.coverage",
        "event_recovery",
    )
    expected_migration = {
        "fresh": {
            "migration_count": 70,
            "latest_migration": "0070",
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        "repeat": {
            "migration_count": 70,
            "latest_migration": "0070",
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "applied_count": 0,
        },
        "down": {
            "migration_count": 69,
            "latest_migration": "0069",
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        "re_up": {
            "migration_count": 70,
            "latest_migration": "0070",
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
    }
    require(
        type(migration) is dict and migration == expected_migration,
        "evidence.coverage",
        "migration",
    )


def _extend_source_digest(digest: Any, name: str, content: bytes) -> None:
    """以名称、长度和内容的无歧义帧扩展源码集合摘要。"""

    try:
        encoded_name = name.encode("utf-8")
    # 非法 Unicode 文件名不能进入 provenance，也不能把宿主路径写入失败证据。
    except UnicodeError:
        raise AcceptanceFailure("evidence.provenance", "source_name") from None
    digest.update(len(encoded_name).to_bytes(4, "big"))
    digest.update(encoded_name)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def _source_file_sha256(path: Path) -> str:
    """读取仓库内固定源码文件并返回 SHA-256，不把宿主路径带入错误。"""

    try:
        resolved = path.resolve()
        resolved.relative_to(ROOT.resolve())
        require(resolved.is_file() and not resolved.is_symlink(), "evidence.provenance", "file")
        return hashlib.sha256(resolved.read_bytes()).hexdigest()
    except AcceptanceFailure:
        raise
    # 文件读取或仓库边界异常统一隐藏绝对路径，只保留来源不可用分类。
    except (OSError, RuntimeError, ValueError):
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None


def _source_tree_sha256(root: Path) -> str:
    """冻结仓库内一个 Python 源码树，文件名和内容变化都会改变摘要。"""

    try:
        resolved = root.resolve()
        resolved.relative_to(ROOT.resolve())
        require(resolved.is_dir() and not resolved.is_symlink(), "evidence.provenance", "tree")
        files = tuple(
            sorted(
                (path for path in resolved.rglob("*.py") if path.is_file()),
                key=lambda path: path.relative_to(resolved).as_posix(),
            )
        )
        require(bool(files), "evidence.provenance", "tree_empty")
        digest = hashlib.sha256()
        for path in files:
            require(not path.is_symlink(), "evidence.provenance", "tree_symlink")
            _extend_source_digest(
                digest,
                path.relative_to(resolved).as_posix(),
                path.read_bytes(),
            )
        return digest.hexdigest()
    except AcceptanceFailure:
        raise
    # 源码树枚举、编码或读取失败时拒绝发布，且不输出具体文件路径。
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None


def validate_source_provenance(provenance: dict[str, str]) -> None:
    """Provenance 只允许固定名称与小写 SHA-256，禁止缺项或宽松摘要。"""

    require(
        type(provenance) is dict and set(provenance) == SOURCE_PROVENANCE_NAMES,
        "evidence.provenance",
        "shape",
    )
    require(
        all(
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            for value in provenance.values()
        ),
        "evidence.provenance",
        "digest",
    )


def capture_source_provenance() -> dict[str, str]:
    """绑定 Runtime/helper、Shared/Control 源码树和完整迁移集合。"""

    try:
        migration_hash = hashlib.sha256()
        files = tuple(sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda path: path.name))
        require(bool(files), "evidence.provenance", "migrations")
        for path in files:
            require(path.is_file() and not path.is_symlink(), "evidence.provenance", "migration")
            _extend_source_digest(migration_hash, path.name, path.read_bytes())
        provenance = {
            "runtime_acceptance": _source_file_sha256(Path(__file__)),
            "runtime_helper": _source_file_sha256(
                ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py"
            ),
            "shared_source_tree": _source_tree_sha256(
                ROOT / "shared-sdk" / "python" / "metahub_shared"
            ),
            "control_source_tree": _source_tree_sha256(
                ROOT / "control-plane-api" / "src" / "metahub_control"
            ),
            "migration_set": migration_hash.hexdigest(),
            "migration_0070_up": _source_file_sha256(
                MIGRATIONS_DIR / "0070_read_only_admin_ai_assistant.sql"
            ),
            "migration_0070_down": _source_file_sha256(
                MIGRATIONS_DIR / "0070_read_only_admin_ai_assistant.down.sql"
            ),
        }
        validate_source_provenance(provenance)
        return provenance
    except AcceptanceFailure:
        raise
    # 任一来源摘要捕获失败都阻断证据生成，不允许用部分 provenance 发布。
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None


def write_evidence(
    artifact: Path,
    work: Path,
    facts: dict[str, object],
    migration: dict[str, object],
    work_privacy: dict[str, int],
    provenance: dict[str, str],
    private_values: tuple[str, ...],
) -> Path:
    """经 staging、复读和双重隐私扫描原子发布 JSON 与 SHA-256。"""

    validate_source_provenance(provenance)
    validate_runtime_coverage(facts, migration)
    # cleanup 结论必须来自真实 owner 路径终态，不能由证据构造器无条件写成 true。
    require(not _lexists(work), "cleanup.work", "remaining")
    require(
        work_privacy.get("plaintext_matches") == 0
        and work_privacy.get("sqlite_sidecars_remaining") == 0
        and int(work_privacy.get("files_scanned", -1)) >= 1,
        "evidence.privacy",
        "work_scan",
    )
    privacy = {
        "files_scanned": int(work_privacy["files_scanned"]) + 2,
        "work_files_scanned": int(work_privacy["files_scanned"]),
        "artifact_files_scanned": 2,
        "plaintext_matches": 0,
        "sqlite_sidecars_remaining": 0,
    }

    payload = {
        "schema_version": "prd077-runtime-v1",
        "prd": "PRD-077",
        "status": "passed",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "all_108_prds_complete": False,
        "migration_roundtrip": migration,
        **facts,
        "privacy": privacy,
        "cleanup": {
            "owned_processes_remaining": 0,
            "sqlite_sidecars_remaining": 0,
            "work_directory_removed": True,
        },
        "source_provenance": {"algorithm": "sha256", **provenance},
    }
    forbidden = {"question", "answer", "prompt", "response", "token", "secret", "password"}
    require(
        not any(key.casefold() in forbidden for key in _walk_keys(payload)),
        "evidence.privacy",
        "forbidden_key",
    )
    encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    require(
        all(value.encode("utf-8") not in encoded for value in private_values),
        "evidence.privacy",
        "plaintext",
    )
    require(
        IDENTITY_BOUND_MUTATIONS_SUPPORTED,
        "evidence.publish",
        "identity_mutations_unsupported",
    )
    staging = repository_child(
        artifact.parent / f".{artifact.name}.staging",
        artifact.parent,
        stage="evidence.path",
    )
    require(
        artifact.parent.is_dir()
        and not artifact.parent.is_symlink()
        and not _lexists(artifact)
        and not _lexists(staging),
        "evidence.path",
        "publish_target",
    )
    staging_identity: tuple[int, int] | None = None

    def cleanup_partial() -> None:
        """逐项删除仍匹配本轮 identity 的 staging/final，不猜测 rename 是否已发生。"""

        if staging_identity is None:
            return
        for candidate in (staging, artifact):
            if not _lexists(candidate):
                continue
            require(
                owned_directory_identity(
                    candidate,
                    artifact.parent,
                    stage="evidence.cleanup",
                )
                == staging_identity,
                "evidence.cleanup",
                "owner_identity",
            )
            remove_owned_directory(
                candidate,
                artifact.parent,
                stage="evidence.cleanup",
                expected_identity=staging_identity,
            )

    try:
        staging.mkdir()
        staging_identity = owned_directory_identity(
            staging,
            artifact.parent,
            stage="evidence.staging_owner",
        )
        staged_evidence = staging / "runtime-acceptance.json"
        staged_manifest = staging / "SHA256SUMS.txt"
        staged_evidence.write_bytes(encoded)
        persisted = staged_evidence.read_bytes()
        require(persisted == encoded, "evidence.publish", "json_mismatch")
        digest = hashlib.sha256(persisted).hexdigest()
        manifest = f"{digest}  runtime-acceptance.json\n"
        staged_manifest.write_text(
            manifest,
            encoding="ascii",
            newline="\n",
        )
        require(
            staged_manifest.read_text(encoding="ascii") == manifest,
            "evidence.publish",
            "manifest_mismatch",
        )
        staged_privacy = verify_private_storage(staging, private_values)
        require(
            staged_privacy
            == {
                "files_scanned": 2,
                "plaintext_matches": 0,
                "sqlite_sidecars_remaining": 0,
            },
            "evidence.privacy",
            "staging_scan",
        )
        # 运行期间任一 helper、Shared、Control 或迁移源码漂移都不能发布混合版本证据。
        require(
            capture_source_provenance() == provenance,
            "evidence.provenance",
            "changed",
        )
        # JSON 与 manifest 均复读、扫描通过后，目录才一次性对读者可见。
        rename_owned_directory(
            staging,
            artifact,
            artifact.parent,
            stage="evidence.publish",
            expected_identity=staging_identity,
        )
        require(
            owned_directory_identity(
                artifact,
                artifact.parent,
                stage="evidence.final_owner",
            )
            == staging_identity,
            "evidence.final_owner",
            "owner_identity",
        )
        published_privacy = verify_private_storage(artifact, private_values)
        require(
            published_privacy == staged_privacy,
            "evidence.privacy",
            "published_scan",
        )
        return artifact / "runtime-acceptance.json"
    except AcceptanceFailure:
        cleanup_partial()
        raise
    # 发布失败拒绝把文件系统细节带入输出，并删除本批次不完整目录。
    except (OSError, RuntimeError, UnicodeError):
        cleanup_partial()
        raise AcceptanceFailure("evidence.publish", "filesystem") from None


def _walk_keys(value: object) -> Iterator[str]:
    """递归枚举证据对象键名，禁止仅以 masked 值保留敏感字段形状。"""

    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def remove_owned_directory(
    path: Path,
    parent: Path,
    *,
    stage: str,
    expected_identity: tuple[int, int] | None = None,
    owner_claim: str | None = None,
    claim_filename: str = OWNER_CLAIM_FILENAME,
    retry_attempts: int = OWNER_DELETE_RETRY_ATTEMPTS,
    retry_delay_seconds: float = OWNER_DELETE_RETRY_DELAY_SECONDS,
) -> None:
    """先原子隔离精确 owner，再递归删除隔离 leaf，绝不按公开原路径 rmtree。"""

    target = _direct_child_without_resolving(path, parent, stage=stage)
    if not _lexists(target):
        return
    require(expected_identity is not None, stage, "owner_identity")
    require(IDENTITY_BOUND_MUTATIONS_SUPPORTED, stage, "delete_unsupported")
    quarantine = quarantine_owned_directory(
        target,
        parent,
        stage=stage,
        expected_identity=expected_identity,
        owner_claim=owner_claim,
        claim_filename=claim_filename,
    )
    if quarantine is None:
        return
    require(
        type(retry_attempts) is int
        and retry_attempts > 0
        and isinstance(retry_delay_seconds, (int, float))
        and not isinstance(retry_delay_seconds, bool)
        and retry_delay_seconds >= 0,
        stage,
        "retry_config",
    )
    for attempt in range(retry_attempts):
        try:
            with WindowsPathIdentityGuard(
                quarantine,
                expected_identity,
                stage=stage,
                directory=True,
            ) as guard:
                if owner_claim is not None:
                    _require_owner_claim(
                        quarantine,
                        owner_claim,
                        stage=stage,
                        claim_filename=claim_filename,
                    )
                guard.verify(stage=stage)
                # 根句柄不共享 DELETE；rmtree 可以清空内容，但其最终按路径 rmdir
                # 会被本句柄拒绝。仅在证明原 identity 仍为空后由句柄标记根删除。
                try:
                    shutil.rmtree(quarantine)
                except OSError:
                    guard.verify(stage=stage)
                    if any(quarantine.iterdir()):
                        raise
                    guard.delete_empty_directory()
            return
        # Windows 短暂占用允许有界重试，预算耗尽后仍以统一文件系统错误终止。
        except OSError:
            if attempt == retry_attempts - 1:
                raise AcceptanceFailure(stage, "filesystem") from None
            time.sleep(float(retry_delay_seconds))


def run(run_id: str | None = None) -> Path:
    """执行完整验收，源漂移或清理失败时拒绝发布 final 证据。"""

    require(
        IDENTITY_BOUND_MUTATIONS_SUPPORTED,
        "config.platform",
        "identity_mutations_unsupported",
    )
    identifier = safe_run_id(run_id)
    assert_ports_available()
    owner_claim = secrets.token_hex(32)
    work, artifact = create_run_directories(identifier, owner_claim=owner_claim)
    work_identity = owned_directory_identity(work, WORK_ROOT, stage="config.work_owner")
    provenance = capture_source_provenance()
    validate_source_provenance(provenance)
    receiver = WebhookReceiver()
    secrets_ = RuntimeSecrets.create()
    evidence: Path | None = None
    database_identity: tuple[int, int] | None = None
    try:
        migration = migration_roundtrip(work)
        graph = seed_business_database(work, secrets_)
        database_identity = regular_file_identity(
            graph.database,
            stage="config.database_identity",
        )
        endpoint_url = receiver.start()
        facts, private_values, answer_serializations, answer_leaves = exercise_runtime(
            work,
            graph,
            secrets_,
            receiver,
            endpoint_url,
        )
        receiver.stop()
        quiesce_sqlite_database(
            graph.database,
            expected_identity=database_identity,
        )
        privacy = verify_runtime_private_storage(
            work,
            private_values=private_values,
            answer_serializations=answer_serializations,
            answer_leaves=answer_leaves,
            # 两个库都含迁移固有的公开目录值；这里只豁免回答的单个公开叶值，完整回答、问题、
            # Token、Key 与其他私密 canary 仍由 always_encodings 在每个数据库中严格零命中扫描。
            source_databases=(graph.database, migration_roundtrip_database(work)),
        )
        all_private_values = tuple(
            dict.fromkeys((*private_values, *answer_serializations, *answer_leaves))
        )
        require(capture_source_provenance() == provenance, "evidence.provenance", "changed")
        validate_runtime_coverage(facts, migration)
        remove_owned_directory(
            work,
            WORK_ROOT,
            stage="cleanup.work",
            expected_identity=work_identity,
            owner_claim=owner_claim,
        )
        evidence = write_evidence(
            artifact,
            work,
            facts,
            migration,
            privacy,
            provenance,
            all_private_values,
        )
        return evidence
    except Exception:
        receiver.stop()
        try:
            remove_owned_directory(
                work,
                WORK_ROOT,
                stage="cleanup.failed_work",
                expected_identity=work_identity,
                owner_claim=owner_claim,
            )
        except Exception:
            pass
        raise


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    """解析唯一可选 run-id，错误不会回显用户参数或宿主路径。"""

    parser = SafeArgumentParser(description="Run PRD-077 runtime acceptance")
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """运行验收并只输出安全证据位置或固定失败坐标。"""

    try:
        arguments = parse_arguments(argv)
        evidence = run(arguments.run_id)
        print(evidence.relative_to(ROOT).as_posix())
        return 0
    except AcceptanceFailure as exc:
        print(f"PRD-077 runtime acceptance failed: {exc.stage}:{exc.code}", file=sys.stderr)
        return 1
    except Exception:
        print("PRD-077 runtime acceptance failed: runtime:unexpected_failure", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
