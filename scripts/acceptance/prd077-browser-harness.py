"""为 PRD-077 PAGE-107 Browser 验收提供真实 Control、Console 和临时数据库。

私有测试值只从进程环境读取。回环控制口仅允许撤销临时 finance 的用量来源权限和停止本 harness；
停止后扫描运行树、验证数据库健康并删除全部临时文件。
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
import re
import socket
import sqlite3
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

# Harness 的路径、端口和私有环境键全部集中定义，避免浏览器页面决定运行边界。
ROOT = Path(__file__).resolve().parents[2]
WORK_ROOT = ROOT / ".tmp" / "prd077-browser"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd077-browser"
GATEWAY_PORT = 8000
CONTROL_PORT = 8001
CONSOLE_PORT = 5173
HARNESS_PORT = 18878
LOOPBACK = "127.0.0.1"
PRIVATE_ENV_KEYS = (
    "PRD077_BROWSER_ADMIN_PASSWORD",
    "PRD077_BROWSER_ROLE_PASSWORD",
    "PRD077_BROWSER_SECOND_PASSWORD",
    "PRD077_BROWSER_AUTH_SECRET",
    "PRD077_BROWSER_PROVIDER_SECRET",
    "PRD077_BROWSER_FILE_ENCRYPTION_SECRET",
    "PRD077_BROWSER_BATCH_ENCRYPTION_SECRET",
    "PRD077_BROWSER_PROMPT_TEMPLATE_ENCRYPTION_SECRET",
    "PRD077_BROWSER_CACHE_AFFINITY_HMAC_SECRET",
    "PRD077_BROWSER_PROVIDER_KEY",
    "PRD077_BROWSER_VIRTUAL_KEY",
    "PRD077_BROWSER_SECOND_VIRTUAL_KEY",
    "PRD077_BROWSER_QUESTION_CANARY",
    "PRD077_BROWSER_ANSWER_CANARY",
)
CLEANUP_RETRY_ATTEMPTS = 150
CLEANUP_RETRY_DELAY_SECONDS = 0.2
OWNER_DRAIN_TIMEOUT_SECONDS = 10.0
OWNER_DRAIN_POLL_SECONDS = 0.05
SQLITE_QUIESCE_TIMEOUT_SECONDS = 10.0
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
OWNER_NONCE_ENV = "PRD077_BROWSER_OWNER_NONCE"
OWNER_CLAIM_FILENAME = ".prd077-owner-claim"
OWNER_NONCE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    """绑定独占创建的目录对象；复制 claim 不能复制文件系统 identity。"""

    device: int
    inode: int


def _windows_file_attributes(path: Path) -> int | None:
    """在 lstat 报不存在后辨认 Windows 仍可见的 broken reparse entry。"""

    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetFileAttributesW.restype = ctypes.c_ulong
    attributes = int(kernel32.GetFileAttributesW(str(path)))
    return None if attributes == 0xFFFFFFFF else attributes


def _lexical_direct_child(path: Path, parent: Path, *, stage: str) -> Path:
    """先做不解引用的直属路径判断，叶子 lstat 前绝不调用 resolve。"""

    try:
        target = Path(os.path.abspath(os.fspath(path)))
        boundary = Path(os.path.abspath(os.fspath(parent)))
        require(target.parent == boundary, stage, "not_direct_child")
        boundary.resolve().relative_to(ROOT.resolve())
    # 路径类型错误、解析失败或越出仓库边界时统一失败关闭，禁止触碰外部节点。
    except (OSError, RuntimeError, TypeError, ValueError):
        raise AcceptanceFailure(stage, "outside_repository") from None
    return target


def _lstat_direct_child(path: Path, parent: Path, *, stage: str) -> os.stat_result | None:
    """lstat 直属叶子并拒绝链接、junction 及 broken reparse，不跟随目标。"""

    target = _lexical_direct_child(path, parent, stage=stage)
    try:
        result = os.lstat(target)
    # 路径表面不存在时仍复核 Windows 属性，防止 broken reparse 被当成空位接管。
    except FileNotFoundError:
        attributes = _windows_file_attributes(target)
        if attributes is None:
            return None
        require(
            not (attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)),
            stage,
            "reparse",
        )
        raise AcceptanceFailure(stage, "filesystem") from None
    # 其他 lstat 失败同样检查 reparse 身份，并以固定错误坐标隐藏原始路径细节。
    except OSError:
        attributes = _windows_file_attributes(target)
        if attributes is not None:
            require(
                not (attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)),
                stage,
                "reparse",
            )
        raise AcceptanceFailure(stage, "filesystem") from None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    require(
        not stat.S_ISLNK(result.st_mode)
        and not (getattr(result, "st_file_attributes", 0) & reparse_flag),
        stage,
        "reparse",
    )
    # 只有叶子已被证明不是 reparse 后，才允许既有 helper 做 canonical 边界复核。
    RUNTIME.repository_child(target, parent, stage=stage)
    return result


def _capture_directory_identity(path: Path, parent: Path, *, stage: str) -> DirectoryIdentity:
    """捕获刚独占创建的普通目录 identity，拒绝不提供稳定 inode 的卷。"""

    result = _lstat_direct_child(path, parent, stage=stage)
    require(result is not None and stat.S_ISDIR(result.st_mode), stage, "not_directory")
    require(int(result.st_ino) != 0, stage, "identity_unavailable")
    return DirectoryIdentity(device=int(result.st_dev), inode=int(result.st_ino))


def _require_path_absent(path: Path, parent: Path, *, stage: str) -> None:
    """把普通冲突与 reparse 冲突都视为失败，且不跟随同名既有 entry。"""

    require(_lstat_direct_child(path, parent, stage=stage) is None, stage, "exists")


def _load_runtime_module() -> Any:
    """加载 PRD-077 Runtime 的迁移、seed、进程和隐私 helper，不启动资源。"""

    name = "_metahub_prd077_runtime_helpers_for_browser"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = ROOT / "scripts" / "acceptance" / "prd077-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    # 依赖加载失败只保留固定错误，不回显绝对脚本路径或 import 细节。
    if specification is None or specification.loader is None:
        raise RuntimeError("browser harness dependency unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


# 复用 Runtime 已通过的安全路径、seed、服务 owner 和多编码明文扫描实现。
RUNTIME = _load_runtime_module()
AcceptanceFailure = RUNTIME.AcceptanceFailure
require = RUNTIME.require


def _remove_owned_directory_with_retry(
    path: Path,
    parent: Path,
    digest: str,
    identity: DirectoryIdentity,
    *,
    stage: str,
) -> None:
    """把 Browser owner 原子隔离后删除；重试始终只作用于随机 quarantine。"""

    if _lstat_direct_child(path, parent, stage=stage) is None:
        return
    _require_owner_claim(path, parent, digest, identity, stage=stage)
    RUNTIME.remove_owned_directory(
        path,
        parent,
        stage=stage,
        expected_identity=(identity.device, identity.inode),
        owner_claim=digest,
        claim_filename=OWNER_CLAIM_FILENAME,
        retry_attempts=CLEANUP_RETRY_ATTEMPTS,
        retry_delay_seconds=CLEANUP_RETRY_DELAY_SECONDS,
    )
    require(
        _lstat_direct_child(path, parent, stage=stage) is None,
        stage,
        "filesystem",
    )


def _owner_claim_digest(nonce: str) -> str:
    """验证进程内 nonce，并只返回可落盘的固定长度摘要。"""

    require(OWNER_NONCE_PATTERN.fullmatch(nonce) is not None, "config.owner_claim", "invalid")
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


def _write_owner_claim(directory: Path, digest: str) -> None:
    """在刚独占创建的目录中以排他模式写入 owner claim。"""

    require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "config.owner_claim", "invalid")
    try:
        with (directory / OWNER_CLAIM_FILENAME).open("x", encoding="ascii", newline="\n") as handle:
            handle.write(f"{digest}\n")
    # claim 创建失败时不能接管同名目录，只返回固定文件系统坐标。
    except OSError:
        raise AcceptanceFailure("config.owner_claim", "filesystem") from None


def _require_owner_claim(
    path: Path,
    parent: Path,
    digest: str,
    identity: DirectoryIdentity,
    *,
    stage: str,
) -> None:
    """每次递归删除前复核直属普通目录、稳定 identity 与本轮精确 claim。"""

    target = _lexical_direct_child(path, parent, stage=stage)
    try:
        directory_stat = _lstat_direct_child(target, parent, stage=stage)
        require(directory_stat is not None, stage, "owner_claim")
        require(
            (int(directory_stat.st_dev), int(directory_stat.st_ino))
            == (identity.device, identity.inode),
            stage,
            "owner_identity",
        )
        claim = target / OWNER_CLAIM_FILENAME
        claim_stat = os.lstat(claim)
        content = claim.read_text(encoding="ascii")
    # 目录或 claim 不可读时拒绝递归操作，避免删除未知 owner 的路径。
    except (OSError, UnicodeError):
        raise AcceptanceFailure(stage, "owner_claim") from None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    require(
        directory_stat is not None
        and stat.S_ISDIR(directory_stat.st_mode)
        and not (getattr(directory_stat, "st_file_attributes", 0) & reparse_flag)
        and stat.S_ISREG(claim_stat.st_mode)
        and not (getattr(claim_stat, "st_file_attributes", 0) & reparse_flag)
        and claim_stat.st_size == 65
        and content == f"{digest}\n",
        stage,
        "owner_claim",
    )
    # claim 读取期间目录若被同名替换，即使复制了 claim 正文也必须拒绝。
    final_stat = _lstat_direct_child(target, parent, stage=stage)
    require(
        final_stat is not None
        and (int(final_stat.st_dev), int(final_stat.st_ino)) == (identity.device, identity.inode),
        stage,
        "owner_identity",
    )


def _sqlite_error_code(error: sqlite3.Error) -> str:
    """只按 SQLite 公开错误码分类，不把底层异常正文带入验收输出。"""

    numeric = getattr(error, "sqlite_errorcode", None)
    return "busy" if isinstance(numeric, int) and (numeric & 0xFF) in {5, 6} else "failed"


def _sidecars_remaining(database: Path) -> int:
    """用 lstat 统计 SQLite sidecar；broken link 也算残留，绝不主动删除。"""

    remaining = 0
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        candidate = Path(f"{database}{suffix}")
        try:
            os.lstat(candidate)
        # Windows 上 broken reparse 可能表现为不存在，必须再读属性后才能认定无残留。
        except FileNotFoundError:
            if _windows_file_attributes(candidate) is not None:
                remaining += 1
        # 无法读取 sidecar 身份时按残留计数，避免验收器错误宣称数据库已静止。
        except OSError:
            remaining += 1
        else:
            remaining += 1
    return remaining


def _quiesce_sqlite_database(
    database: Path,
    *,
    stage: str,
    timeout_seconds: float = SQLITE_QUIESCE_TIMEOUT_SECONDS,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """复用 PRD-077 已绑定 OS 句柄与 SQLite database_list 的正规收尾。"""

    try:
        result = RUNTIME.quiesce_sqlite_database(
            database,
            stage=stage,
            timeout_seconds=timeout_seconds,
            expected_identity=expected_identity,
        )
    # Runtime 的固定失败只映射 SQLite 忙状态，其他身份和文件系统坐标原样保留。
    except AcceptanceFailure as error:
        code = "busy" if error.code in {"checkpoint_busy", "failed"} else error.code
        raise AcceptanceFailure(stage, code) from None
    require(
        result.get("journal_mode") == "delete" and result.get("sqlite_sidecars_remaining") == 0,
        stage,
        "sidecars",
    )


class WindowsKillOnCloseJob:
    """在 Windows 上让 OS 于 harness 句柄关闭时回收全部服务进程。"""

    def __init__(self) -> None:
        """创建 kill-on-close Job；非 Windows 平台保持无副作用空实现。"""

        self._kernel32: Any | None = None
        self._handle: Any | None = None
        self._accounting_type: type[ctypes.Structure] | None = None
        self._last_active_processes: int | None = 0 if os.name != "nt" else None
        if os.name != "nt":
            return

        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            """镜像 Windows Job API 要求的进程树输入输出计数字段布局。"""

            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            """镜像 Job 基础限制结构，并承载关闭句柄即回收的标志位。"""

            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            """组合基础限制与计数结构，供 SetInformationJobObject 原样读取。"""

            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            """读取 Job 内已创建、活跃及已终止进程数，证明后代真实归零。"""

            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise AcceptanceFailure("process.job", "create")
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            kernel32.CloseHandle(handle)
            raise AcceptanceFailure("process.job", "configure")
        self._kernel32 = kernel32
        self._handle = handle
        self._accounting_type = BasicAccountingInformation

    def assign(self, process: Any) -> None:
        """把精确 Popen 句柄加入本 job；失败时由外层 owner 清理该进程。"""

        if os.name != "nt":
            return
        handle = getattr(process, "_handle", None)
        require(handle is not None and self._handle is not None, "process.job", "handle")
        if not self._kernel32.AssignProcessToJobObject(self._handle, ctypes.c_void_p(handle)):
            raise AcceptanceFailure("process.job", "assign")

    def close(self) -> None:
        """关闭唯一 job 句柄；正常路径已先停止子进程，崩溃路径由 OS 自动关闭。"""

        if self._handle is None:
            return
        handle = self._handle
        if not self._kernel32.CloseHandle(handle):
            raise AcceptanceFailure("process.job", "close")
        self._handle = None

    def active_processes(self) -> int:
        """查询 Windows Job 的真实 ActiveProcesses；查询失败不得假定为零。"""

        if os.name != "nt":
            self._last_active_processes = 0
            return 0
        require(
            self._handle is not None
            and self._kernel32 is not None
            and self._accounting_type is not None,
            "process.job",
            "handle",
        )
        information = self._accounting_type()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            raise AcceptanceFailure("process.job", "query")
        self._last_active_processes = int(information.ActiveProcesses)
        return self._last_active_processes

    def terminate_and_wait(self, timeout_seconds: float) -> int:
        """必要时终止整个 Job，并在共同 deadline 内证明 ActiveProcesses 为零。"""

        deadline = time.monotonic() + max(timeout_seconds, 0.001)
        active = self.active_processes()
        if active > 0:
            if not self._kernel32.TerminateJobObject(self._handle, 1):
                raise AcceptanceFailure("process.job", "terminate")
        while active > 0 and time.monotonic() < deadline:
            time.sleep(OWNER_DRAIN_POLL_SECONDS)
            active = self.active_processes()
        require(active == 0, "process.job", "timeout")
        return active


@dataclass(frozen=True, slots=True)
class BrowserSecrets:
    """保存仅存在 harness 内存和子进程环境中的浏览器测试私有值。"""

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
    answer_canary: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> BrowserSecrets:
        """从十四个必需环境变量构造私有值，缺失或过短时固定失败。"""

        values = [os.environ.get(name, "") for name in PRIVATE_ENV_KEYS]
        require(
            all(len(value) >= 24 for value in values[:-1]) and 12 <= len(values[-1]) <= 32,
            "config.private_values",
            "invalid",
        )
        return cls(*values)

    def runtime(self) -> Any:
        """转换为 Runtime seed 结构，避免在 Browser harness 重复数据库实现。"""

        # 字段逐一转交内存数据类，任何私有值都不会进入 repr 或 ready 输出。
        return RUNTIME.RuntimeSecrets(
            admin_password=self.admin_password,
            role_password=self.role_password,
            second_password=self.second_password,
            auth_secret=self.auth_secret,
            provider_secret=self.provider_secret,
            file_encryption_secret=self.file_encryption_secret,
            batch_encryption_secret=self.batch_encryption_secret,
            prompt_template_encryption_secret=self.prompt_template_encryption_secret,
            cache_affinity_hmac_secret=self.cache_affinity_hmac_secret,
            provider_key=self.provider_key,
            virtual_key=self.virtual_key,
            second_virtual_key=self.second_virtual_key,
            question_canary=self.question_canary,
        )

    def scan_values(self) -> tuple[str, ...]:
        """返回停止后必须在数据库和日志中零命中的全部私有明文。"""

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
            self.answer_canary,
        )


class BrowserHarness:
    """持有本轮真实服务、临时数据库、回环控制 server 与安全终态。"""

    def __init__(self, run_id: str, secrets_: BrowserSecrets, owner_nonce: str) -> None:
        """验证单段 run-id，并初始化尚未启动的精确资源 owner。"""

        require(RUNTIME.safe_run_id(run_id) == run_id, "config.run_id", "invalid")
        self.run_id = run_id
        self.secrets = secrets_
        self.owner_claim_digest = _owner_claim_digest(owner_nonce)
        # 叶子路径只做 absolute，不在 lstat 前 resolve，避免跟随同名 reparse。
        self.work = Path(os.path.abspath(os.fspath(WORK_ROOT / run_id)))
        self.staging = Path(os.path.abspath(os.fspath(ARTIFACT_ROOT / f".{run_id}.staging")))
        self.final_artifact = Path(os.path.abspath(os.fspath(ARTIFACT_ROOT / run_id)))
        self.graph: Any | None = None
        self.processes = RUNTIME.OwnedProcesses()
        self.direct_processes: list[Any] = []
        self.process_job: WindowsKillOnCloseJob | None = None
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.control_thread_started = False
        self.control_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.revoked = False
        self.base_permission_retained = False
        self.backend_denied = False
        self.control_failed = False
        self.quiesced = False
        self.control_owner_drained = False
        self.service_owners_drained = False
        self.database_quiesced = False
        self.owned_processes_remaining: int | None = None
        self.runtime_tokens: list[str] = []
        self.work_created = False
        self.staging_created = False
        self.work_identity: DirectoryIdentity | None = None
        self.staging_identity: DirectoryIdentity | None = None
        self.database_identity: tuple[int, int] | None = None

    def prepare(self) -> None:
        """创建独占目录、迁移 seed 数据库并启动真实 Control 与 Console。"""

        self._assert_paths_and_ports()
        try:
            WORK_ROOT.mkdir(parents=True, exist_ok=True)
            ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
            try:
                self.work.mkdir()
            # 本轮 work 必须独占创建，已存在即拒绝接管未知 owner 的目录。
            except FileExistsError:
                raise AcceptanceFailure("config.work", "exists") from None
            # work 创建失败收敛为固定坐标，证据和日志不得包含主机路径异常。
            except OSError:
                raise AcceptanceFailure("config.work", "filesystem") from None
            self.work_created = True
            self.work_identity = _capture_directory_identity(
                self.work,
                WORK_ROOT,
                stage="config.work_identity",
            )
            _write_owner_claim(self.work, self.owner_claim_digest)
            try:
                self.staging.mkdir()
            # staging 必须由本轮独占创建，禁止复用或覆盖任何既有验收证据目录。
            except FileExistsError:
                raise AcceptanceFailure("config.staging", "exists") from None
            # staging 创建失败固定为文件系统错误，不向调用端泄露原始异常文本。
            except OSError:
                raise AcceptanceFailure("config.staging", "filesystem") from None
            self.staging_created = True
            self.staging_identity = _capture_directory_identity(
                self.staging,
                ARTIFACT_ROOT,
                stage="config.staging_identity",
            )
            _write_owner_claim(self.staging, self.owner_claim_digest)
            # Browser profile 也由本轮 work owner 持有；Node 驱动必须在 stop 前删除，
            # 随后的双目录隐私扫描会证明没有 Session、问题或回答落盘残留。
            for name in ("browser", "logs", "temp", "spool"):
                (self.work / name).mkdir()
            runtime_secrets = self.secrets.runtime()
            self.graph = self._seed_business_graph(runtime_secrets)
            self.database_identity = RUNTIME.regular_file_identity(
                self.graph.database,
                stage="config.database_identity",
            )
            environment = RUNTIME.service_environment(
                self.work,
                self.graph,
                runtime_secrets,
                event_enabled=False,
            )
            environment["METAHUB_CONSOLE_PORT"] = str(CONSOLE_PORT)
            RUNTIME._BASE.CONSOLE_PORT = CONSOLE_PORT
            self.process_job = WindowsKillOnCloseJob()
            gateway = self._add_owned_process(
                RUNTIME.start_service(
                    "gateway",
                    "metahub_gateway.app:app",
                    GATEWAY_PORT,
                    work=self.work,
                    environment=environment,
                )
            )
            RUNTIME.wait_health(
                f"http://{LOOPBACK}:{GATEWAY_PORT}/health",
                gateway,
                stage="health.gateway",
            )
            control = self._add_owned_process(
                RUNTIME.start_service(
                    "control",
                    "metahub_control.app:app",
                    CONTROL_PORT,
                    work=self.work,
                    environment=environment,
                )
            )
            RUNTIME.wait_health(
                f"http://{LOOPBACK}:{CONTROL_PORT}/health",
                control,
                stage="health.control",
            )
            console = self._add_owned_process(
                RUNTIME._BASE.start_console(work=self.work, environment=environment)
            )
            RUNTIME.wait_health(
                f"http://{LOOPBACK}:{CONSOLE_PORT}/",
                console,
                stage="health.console",
            )
            self._start_control_server()
        except Exception:
            self.cleanup(success=False)
            raise

    def _add_owned_process(self, item: Any) -> Any:
        """先登记精确进程句柄，再加入 kill-on-close job，确保任一失败仍可回收。"""

        owned = self.processes.add(item)
        self.direct_processes.append(owned.process)
        require(self.process_job is not None, "process.job", "missing")
        self.process_job.assign(owned.process)
        return owned

    def _assert_paths_and_ports(self) -> None:
        """验证运行目标位于仓库直属边界且三个回环端口均未被占用。"""

        _require_path_absent(self.work, WORK_ROOT, stage="config.work")
        _require_path_absent(self.staging, ARTIFACT_ROOT, stage="config.staging")
        _require_path_absent(
            self.final_artifact,
            ARTIFACT_ROOT,
            stage="config.artifact",
        )
        for port in (GATEWAY_PORT, CONTROL_PORT, CONSOLE_PORT, HARNESS_PORT):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((LOOPBACK, port))
            # 已占用端口只阻断当前 harness，不接管或结束现有监听进程。
            except OSError:
                raise AcceptanceFailure("config.ports", "unavailable") from None
            finally:
                probe.close()

    def _start_control_server(self) -> None:
        """启动只接受 health、revoke、quiesce 和 stop 的固定回环控制接口。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """把控制请求映射到无参数 harness 动作，不读取任意正文。"""

            def do_GET(self) -> None:  # noqa: N802
                """health 只返回固定 ready 状态，其它路径统一 404。"""

                if self.path == "/health":
                    self._reply(200, {"status": "ready"})
                else:
                    self._reply(404, {"status": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                """执行撤权、稳定服务文件树或设置当前 owner 的退出事件。"""

                if self.headers.get("Content-Length") not in {None, "0"}:
                    self._reply(400, {"status": "invalid"})
                    return
                with owner.control_lock:
                    if self.path == "/revoke":
                        try:
                            owner.revoke_admin_assistant()
                        # 控制面异常只返回固定失败状态；数据库、HTTP 或凭据细节
                        # 不得进入 server traceback。
                        except Exception:
                            owner.control_failed = True
                            self._reply(500, {"status": "failed"})
                            return
                        self._reply(200, {"status": "revoked"})
                        return
                    if self.path == "/quiesce":
                        try:
                            owner.quiesce_services()
                        except Exception:
                            owner.control_failed = True
                            self._reply(500, {"status": "failed"})
                            return
                        self._reply(200, {"status": "quiesced"})
                        return
                    if self.path == "/stop":
                        owner.stop_event.set()
                        self._reply(
                            200 if not owner.control_failed else 409,
                            {"status": "stopping" if not owner.control_failed else "failed"},
                        )
                        return
                    self._reply(404, {"status": "not_found"})

            def _reply(self, status: int, value: dict[str, str]) -> None:
                """发送固定短 JSON，不回显路径、数据库结果或异常正文。"""

                encoded = json.dumps(value, separators=(",", ":")).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                """禁用控制口默认访问日志，避免运行路径进入文件。"""

                return

        try:
            self.server = ThreadingHTTPServer((LOOPBACK, HARNESS_PORT), Handler)
        # 控制口监听失败只报告固定坐标，不泄漏宿主网络配置。
        except OSError:
            raise AcceptanceFailure("harness.control", "port") from None
        try:
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                name="prd077-browser-harness",
                daemon=True,
            )
        except Exception:
            raise AcceptanceFailure("harness.control", "thread_create") from None
        try:
            self.thread.start()
        except Exception:
            raise AcceptanceFailure("harness.control", "thread_start") from None
        self.control_thread_started = True

    def _wait_direct_processes(self, timeout_seconds: float) -> int:
        """轮询本 harness 保存的精确 Popen 句柄，未知 poll 状态也按活跃处理。"""

        deadline = time.monotonic() + max(timeout_seconds, 0.001)
        remaining = len(self.direct_processes)
        while time.monotonic() < deadline:
            remaining = 0
            for process in self.direct_processes:
                try:
                    if process.poll() is None:
                        remaining += 1
                except Exception:
                    remaining += 1
            if remaining == 0:
                return 0
            time.sleep(OWNER_DRAIN_POLL_SECONDS)
        return remaining

    def _drain_service_owners(self, *, stage: str) -> int:
        """关闭直接 owner 与 Windows Job，并只在两层实测均归零后发布计数。"""

        if self.service_owners_drained:
            require(self.owned_processes_remaining == 0, stage, "active")
            return 0

        close_succeeded = True
        try:
            self.processes.close()
        except Exception:
            close_succeeded = False

        job_succeeded = True
        job_remaining: int | None = 0
        if self.process_job is not None:
            try:
                job_remaining = self.process_job.terminate_and_wait(OWNER_DRAIN_TIMEOUT_SECONDS)
                self.process_job.close()
                self.process_job = None
            except Exception:
                job_succeeded = False
                job_remaining = None

        direct_remaining = self._wait_direct_processes(OWNER_DRAIN_TIMEOUT_SECONDS)
        if not close_succeeded or not job_succeeded or job_remaining is None:
            # 任一 close/query 失败后，零不能作为已证明的发布事实。
            self.owned_processes_remaining = None
            self.service_owners_drained = False
            raise AcceptanceFailure(stage, "failed")
        measured = direct_remaining + job_remaining
        self.owned_processes_remaining = measured
        require(measured == 0, stage, "active")
        self.service_owners_drained = True
        return measured

    def _database_path(self, *, stage: str) -> Path:
        """在已验证 work identity 内定位真实普通 SQLite 文件，不从 URL 猜路径。"""

        require(
            self.graph is not None
            and self.work_identity is not None
            and self.database_identity is not None,
            stage,
            "missing",
        )
        _require_owner_claim(
            self.work,
            WORK_ROOT,
            self.owner_claim_digest,
            self.work_identity,
            stage=stage,
        )
        candidate = getattr(self.graph, "database", None)
        require(isinstance(candidate, Path), stage, "path")
        result = _lstat_direct_child(candidate, self.work, stage=stage)
        require(result is not None and stat.S_ISREG(result.st_mode), stage, "file")
        require(
            (int(result.st_dev), int(result.st_ino)) == self.database_identity,
            stage,
            "database_identity",
        )
        return _lexical_direct_child(candidate, self.work, stage=stage)

    def _quiesce_database(self, *, stage: str) -> None:
        """仅在全部服务 owner 归零后正规关闭 WAL 家族，并保存可复验状态。"""

        require(
            self.service_owners_drained and self.owned_processes_remaining == 0,
            stage,
            "owners_active",
        )
        if self.graph is None:
            self.database_quiesced = True
            return
        self.database_quiesced = False
        database = self._database_path(stage=stage)
        _quiesce_sqlite_database(
            database,
            stage=stage,
            timeout_seconds=SQLITE_QUIESCE_TIMEOUT_SECONDS,
            expected_identity=self.database_identity,
        )
        self.database_quiesced = True

    def quiesce_services(self) -> None:
        """关闭本轮服务 owner 并静止 SQLite，同时保留控制服务和 work 目录供验收。"""

        with self.control_lock:
            if self.quiesced:
                return
            self._drain_service_owners(stage="quiesce.owners")
            self._quiesce_database(stage="quiesce.sqlite")
            self.quiesced = True

    def _seed_business_graph(self, runtime_secrets: Any) -> Any:
        """在首次 append-only Usage 插入时注入随机成本回答，不执行事后 UPDATE。"""

        try:
            seed_cost = format(Decimal(self.secrets.answer_canary), "f")
        except InvalidOperation:
            raise AcceptanceFailure("database.answer_canary", "decimal") from None
        require(seed_cost == self.secrets.answer_canary, "database.answer_canary", "format")
        return RUNTIME.seed_business_database(
            self.work,
            runtime_secrets,
            usage_cost_cny=seed_cost,
            include_normalized_cost=False,
        )

    def revoke_admin_assistant(self) -> None:
        """仅撤销 finance 用量来源权限，并以真实 endpoint 403 证明后端执行新权限。"""

        require(self.graph is not None, "revoke.graph", "missing")
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
            timeout=httpx.Timeout(10, connect=3),
            trust_env=False,
        ) as client:
            admin_token = RUNTIME.login(
                client,
                "default",
                "admin@metahub.local",
                self.secrets.admin_password,
                "prd077-browser-revoke-admin-login",
            )
            self.runtime_tokens.append(admin_token)
            matrix = client.get(
                "/permissions",
                headers=self._headers(admin_token, "matrix_read"),
            )
            require(matrix.status_code == 200, "revoke.matrix", "status")
            try:
                matrix_payload = matrix.json()
            # 权限矩阵不是 JSON 时拒绝撤权流程，且不记录原始响应正文。
            except ValueError:
                raise AcceptanceFailure("revoke.matrix", "json") from None
            roles = matrix_payload.get("roles") if isinstance(matrix_payload, dict) else None
            require(isinstance(roles, list), "revoke.matrix", "roles")
            finance_role = next(
                (
                    role
                    for role in roles
                    if isinstance(role, dict) and role.get("code") == "finance_admin"
                ),
                None,
            )
            require(finance_role is not None, "revoke.role", "missing")
            current_codes = finance_role.get("permission_codes")
            require(isinstance(current_codes, list), "revoke.role", "permissions")
            require(
                "admin_assistant.query" in current_codes,
                "revoke.role",
                "permission_missing",
            )
            require(
                "billing.usage.read" in current_codes,
                "revoke.role",
                "source_permission_missing",
            )
            replacement = sorted(code for code in current_codes if code != "billing.usage.read")
            require(
                "admin_assistant.query" in replacement
                and len(replacement) == len(current_codes) - 1,
                "revoke.role",
                "scope",
            )
            patched = client.patch(
                f"/roles/{finance_role.get('role_id')}/permissions",
                headers=self._headers(admin_token, "matrix_patch"),
                json={
                    "expected_revision": finance_role.get("permission_revision"),
                    "permission_codes": replacement,
                },
            )
            require(patched.status_code == 200, "revoke.patch", "status")
            try:
                patched_payload = patched.json()
            # CAS 写后响应必须是规范 JSON，解析失败不能假定权限已经撤销。
            except ValueError:
                raise AcceptanceFailure("revoke.patch", "json") from None
            patched_role = (
                patched_payload.get("role") if isinstance(patched_payload, dict) else None
            )
            require(
                isinstance(patched_role, dict)
                and patched_payload.get("changed") is True
                and patched_role.get("code") == "finance_admin"
                and patched_role.get("permission_codes") == replacement,
                "revoke.patch",
                "contract",
            )
            self.revoked = True
            self.base_permission_retained = True
            finance_token = RUNTIME.login(
                client,
                "default",
                "finance-prd077@local.invalid",
                self.secrets.role_password,
                "prd077-browser-revoke-finance-login",
            )
            self.runtime_tokens.append(finance_token)
            response = client.post(
                "/admin-assistant/query",
                headers=self._headers(finance_token, "backend_denied"),
                json={
                    "question": f"usage last 7 days {self.secrets.question_canary}",
                },
            )
        try:
            payload = response.json()
        # 非 JSON 响应不能被当作权限拒绝成功，也不回显原始响应正文。
        except ValueError:
            raise AcceptanceFailure("revoke.backend", "json") from None
        # 后端必须返回统一权限码；前端导航变化本身不能替代该断言。
        self.backend_denied = (
            response.status_code == 403
            and isinstance(payload, dict)
            and payload.get("code") == "auth.permission_denied"
            and payload.get("request_id") == response.headers.get("x-request-id")
            and payload.get("trace_id") == response.headers.get("x-trace-id")
            and response.headers.get("cache-control") == "no-store, private"
            and response.headers.get("pragma") == "no-cache"
            and "authorization"
            in {item.strip().casefold() for item in response.headers.get("vary", "").split(",")}
        )
        require(self.backend_denied, "revoke.backend", "not_denied")

    @staticmethod
    def _headers(token: str, suffix: str) -> dict[str, str]:
        """构造仅存在当前 HTTP 调用栈的认证与公开链路 Header。"""

        return {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": f"req_prd077_browser_{suffix}",
            "X-Trace-ID": f"trc_prd077_browser_{suffix}",
        }

    def serve(self) -> None:
        """等待有界 stop 信号，超时也会进入同一清理路径。"""

        if not self.stop_event.wait(timeout=900):
            raise AcceptanceFailure("harness.wait", "timeout")

    def cleanup(self, *, success: bool) -> None:
        """关闭精确 owner；成功只留下 staging，失败删除本轮全部目录。"""

        failed = False
        first_failure: AcceptanceFailure | None = None

        def fixed_failure(
            error: Exception | None,
            stage: str,
            code: str,
        ) -> AcceptanceFailure:
            """仅保留白名单固定失败坐标，绝不把原始异常消息写入验收结果。"""

            # 已标准化的验收失败保留首个精确坐标，未知异常则使用调用方固定坐标。
            if isinstance(error, AcceptanceFailure):
                return error
            return AcceptanceFailure(stage, code)

        def mark_failure(error: Exception | None, stage: str, code: str) -> None:
            """按发生顺序保存首个固定坐标，同时允许后续独立 owner 继续清理。"""

            nonlocal failed, first_failure
            failed = True
            if first_failure is None:
                first_failure = fixed_failure(error, stage, code)

        # Thread 构造或 start 失败时 ident 为空；此时 shutdown/join 都可能死锁或抛错，
        # 只关闭已 bind 的 server socket。start 成功后则必须证明线程真实退出。
        server_closed = self.server is None
        thread_started = self.control_thread_started
        if self.thread is not None and not thread_started:
            try:
                thread_started = self.thread.ident is not None
            # 线程状态不可读取时无法证明其未启动，必须记录固定清理失败坐标。
            except Exception as error:
                mark_failure(error, "cleanup.control_thread", "state")
        if self.server is not None:
            if thread_started:
                try:
                    self.server.shutdown()
                # 控制服务 shutdown 失败不阻断其他 owner 清理，但最终验收必须失败。
                except Exception as error:
                    mark_failure(error, "cleanup.control_server", "shutdown")
            try:
                self.server.server_close()
                server_closed = True
            # socket 关闭失败意味着控制 owner 未证明释放，禁止删除仍可能活写的 work。
            except Exception as error:
                server_closed = False
                mark_failure(error, "cleanup.control_server", "close")
        thread_stopped = not thread_started
        if self.thread is not None and thread_started:
            try:
                self.thread.join(timeout=5)
                thread_stopped = not self.thread.is_alive()
            # join 或活跃状态检查异常都视为线程仍未证明退出，保持 work 不动。
            except Exception as error:
                thread_stopped = False
                mark_failure(error, "cleanup.control_thread", "join")
            if not thread_stopped:
                mark_failure(None, "cleanup.control_thread", "timeout")
        self.control_owner_drained = server_closed and thread_stopped
        if self.control_owner_drained:
            self.server = None
            self.thread = None
            self.control_thread_started = False

        try:
            self._drain_service_owners(stage="cleanup.owners")
        # 服务 owner 清理异常被固定化并继续回收独立 staging，最终仍以首错失败。
        except Exception as error:
            mark_failure(error, "cleanup.owners", "failed")

        if self.control_owner_drained and self.service_owners_drained:
            try:
                self._quiesce_database(stage="cleanup.sqlite")
            # SQLite 静止失败时禁止扫描或删除 work，避免与残留写入发生竞态。
            except Exception as error:
                mark_failure(error, "cleanup.sqlite", "failed")
        else:
            self.database_quiesced = False

        summary: dict[str, object] | None = None
        private_values: tuple[str, ...] = ()
        if success:
            try:
                require(self.graph is not None, "cleanup.database", "missing")
                require(
                    self.control_owner_drained,
                    "cleanup.control_owner",
                    "active",
                )
                require(
                    self.service_owners_drained and self.owned_processes_remaining == 0,
                    "cleanup.owners",
                    "active",
                )
                require(self.database_quiesced, "cleanup.sqlite", "not_quiesced")
                require(not self.control_failed, "cleanup.control", "failed")
                require(self.quiesced, "cleanup.quiesce", "missing")
                require(
                    _lstat_direct_child(
                        self.final_artifact,
                        ARTIFACT_ROOT,
                        stage="cleanup.publish",
                    )
                    is None,
                    "cleanup.publish",
                    "premature_final",
                )
                require(self.revoked, "cleanup.revoke", "missing")
                require(
                    self.base_permission_retained,
                    "cleanup.revoke",
                    "base_permission_missing",
                )
                require(self.backend_denied, "cleanup.backend", "missing")
                require(
                    self.work_identity is not None and self.staging_identity is not None,
                    "cleanup.owner_identity",
                    "missing",
                )
                _require_owner_claim(
                    self.work,
                    WORK_ROOT,
                    self.owner_claim_digest,
                    self.work_identity,
                    stage="cleanup.work",
                )
                _require_owner_claim(
                    self.staging,
                    ARTIFACT_ROOT,
                    self.owner_claim_digest,
                    self.staging_identity,
                    stage="cleanup.staging",
                )
                private_values = (
                    *self.secrets.scan_values(),
                    *tuple(self.runtime_tokens),
                )
                work_privacy = RUNTIME.verify_private_storage(
                    self.work,
                    private_values,
                )
                staging_privacy = RUNTIME.verify_private_storage(
                    self.staging,
                    private_values,
                )
                health = RUNTIME.database_health(
                    self.graph.database_url,
                    head="0070",
                    count=70,
                    stage="cleanup.database",
                )
                require(
                    _sidecars_remaining(self._database_path(stage="cleanup.database")) == 0,
                    "cleanup.sqlite_sidecars",
                    "remaining",
                )
                summary = {
                    "schema_version": "prd077-browser-harness-v2",
                    "status": "passed",
                    "gateway_http_200": True,
                    "control_http_200": True,
                    "console_http_200": True,
                    "finance_source_permission_revoked": self.revoked,
                    "base_permission_retained": self.base_permission_retained,
                    "backend_denied_after_source_revoke": self.backend_denied,
                    "privacy": {
                        "work": work_privacy,
                        "staging_before_summary": staging_privacy,
                    },
                    "database": health,
                    "owned_processes_remaining": self.owned_processes_remaining,
                    "all_108_prds_complete": False,
                }
            # 任何验证异常都固定为清理校验失败，且不得发布部分通过的摘要。
            except Exception as error:
                mark_failure(error, "cleanup.validation", "failed")

        # 只有 control、进程树和数据库三层都已证明静止，才允许扫描或删除 work。
        work_safe = (
            self.control_owner_drained and self.service_owners_drained and self.database_quiesced
        )
        if self.work_created and work_safe:
            try:
                require(self.work_identity is not None, "cleanup.work", "identity_missing")
                _remove_owned_directory_with_retry(
                    self.work,
                    WORK_ROOT,
                    self.owner_claim_digest,
                    self.work_identity,
                    stage="cleanup.work",
                )
            # work 回收失败必须保留首错并阻止 passed 证据发布，避免虚假验收成功。
            except Exception as error:
                mark_failure(error, "cleanup.work", "failed")

        # passed 摘要只能在 work 已真实删除后进入 staging；写后必须复读并再次扫描。
        work_absent = False
        if work_safe:
            try:
                work_absent = (
                    _lstat_direct_child(self.work, WORK_ROOT, stage="cleanup.work") is None
                )
            # 无法证明 work 已消失时按清理失败处理，摘要不能进入 staging。
            except Exception as error:
                mark_failure(error, "cleanup.work", "failed")
        if summary is not None and not failed and work_absent:
            encoded = (
                json.dumps(summary, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
            ).encode("utf-8")
            try:
                target = self.staging / "runtime-summary.json"
                target.write_bytes(encoded)
                require(
                    json.loads(target.read_text(encoding="utf-8")) == summary,
                    "evidence.runtime",
                    "readback",
                )
                RUNTIME.verify_private_storage(self.staging, private_values)
            # 摘要写入、复读或二次隐私扫描失败都必须删除整个批次。
            except Exception as error:
                mark_failure(error, "evidence.runtime", "failed")
        if failed or not success:
            # staging 不承载服务数据库；即使 work 仍可能活写，也可按独立 identity 回收。
            if self.staging_created:
                try:
                    require(
                        self.staging_identity is not None,
                        "cleanup.staging",
                        "identity_missing",
                    )
                    _remove_owned_directory_with_retry(
                        self.staging,
                        ARTIFACT_ROOT,
                        self.owner_claim_digest,
                        self.staging_identity,
                        stage="cleanup.staging",
                    )
                # staging 回收失败也必须上报固定坐标，不能留下貌似有效的半成品证据。
                except Exception as error:
                    mark_failure(error, "cleanup.staging", "failed")
        if first_failure is not None:
            raise first_failure
        if failed:
            raise AcceptanceFailure("cleanup", "failed")


def main(argv: list[str] | None = None) -> int:
    """启动 harness，并仅输出不含凭据的 ready 或固定失败 JSON。"""

    parser = RUNTIME.SafeArgumentParser(description="Serve PRD-077 browser harness")
    parser.add_argument("--run-id", required=True)
    try:
        arguments = parser.parse_args(argv)
        owner_nonce = os.environ.get(OWNER_NONCE_ENV, "")
        harness = BrowserHarness(
            arguments.run_id,
            BrowserSecrets.from_environment(),
            owner_nonce,
        )
        harness.prepare()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "work": harness.work.relative_to(ROOT).as_posix(),
                    "staging": harness.staging.relative_to(ROOT).as_posix(),
                    "owner_claim_sha256": harness.owner_claim_digest,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        try:
            harness.serve()
        except Exception:
            harness.cleanup(success=False)
            raise
        harness.cleanup(success=True)
        print(json.dumps({"status": "stopped"}, separators=(",", ":")), flush=True)
        return 0
    except AcceptanceFailure as exc:
        print(
            json.dumps({"status": "failed", "stage": exc.stage, "code": exc.code}),
            file=sys.stderr,
            flush=True,
        )
        return 1
    except Exception:
        print(
            json.dumps({"status": "failed", "stage": "harness", "code": "unexpected"}),
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
