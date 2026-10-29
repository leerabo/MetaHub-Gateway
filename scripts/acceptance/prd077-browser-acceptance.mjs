/**
 * PRD-077 PAGE-107 只读管理员 AI 助手真实 Browser 验收器。
 *
 * 本驱动只使用仓库内 Python、Chrome 与既有 CDP helper。问题、回答、Session、租户身份和
 * 凭据只存在进程内存；持久证据仅包含清空后的双视口 PNG、脱敏计数与 SHA-256。
 */

import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import {
  lstat,
  mkdir,
  open,
  readFile,
  readdir,
  realpath,
  unlink,
  writeFile,
} from "node:fs/promises";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  CdpClient,
  createPageSession,
  evaluate,
  launchEdge,
  runAndWaitForPageLoad,
  setViewport,
  waitForDebugger,
  waitForDom as waitForDomBase,
  waitForTrackerCount,
} from "./prd066-browser-acceptance.mjs";
import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";


// 固定仓库路径与受控工具位置，验收器不接受外部路径覆盖。
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const WORK_ROOT = path.join(ROOT, ".tmp", "prd077-browser");
const ARTIFACT_ROOT = path.join(ROOT, ".artifacts", "prd077-browser");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const HARNESS = path.join(SCRIPT_DIRECTORY, "prd077-browser-harness.py");
const FRONTEND_SOURCE = path.join(ROOT, "console-web", "src");
const CONTROL_SOURCE = path.join(ROOT, "control-plane-api", "src");
const GATEWAY_SOURCE = path.join(ROOT, "gateway-data-plane", "src");
const SHARED_SOURCE = path.join(ROOT, "shared-sdk", "python", "metahub_shared");
const MIGRATIONS = path.join(ROOT, "infra", "migrations");
const CHROME = path.join(
  ROOT,
  ".tools",
  "chrome-portable",
  "chrome.exe",
);
// PE Machine 必须与当前 Node 架构精确对应，避免误启同名但架构不兼容的浏览器。
const WINDOWS_PE_MACHINE_BY_ARCHITECTURE = Object.freeze({
  ia32: 0x014c,
  x64: 0x8664,
  arm64: 0xaa64,
});
// 固定本机服务来源与调试端口，网络门禁只认可这些受控入口。
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const GATEWAY_ORIGIN = "http://localhost:8000";
const HARNESS_ORIGIN = "http://127.0.0.1:18878";
const DEBUG_PORT = 19077;
const DOM_WAIT_TIMEOUT_MS = 60_000;
const HARNESS_CONTROL_TIMEOUT_MS = 30_000;
const HARNESS_QUIESCE_TIMEOUT_MS = 60_000;
const INSPECTION_TIMEOUT_MS = 30_000;
const INSPECTION_STABLE_MS = 250;
const ALLOWED_PAGE_ORIGINS = new Set([
  CONSOLE_ORIGIN,
  CONTROL_ORIGIN,
  GATEWAY_ORIGIN,
  "http://127.0.0.1:8000",
  "http://127.0.0.1:5173",
  "http://127.0.0.1:8001",
]);
// 视口、路径段和路由族共同限定证据形态及允许访问的控制面边界。
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_COORDINATE = /^[a-z][a-z0-9_.-]{1,100}$/;
const OWNER_CLAIM_FILENAME = ".prd077-owner-claim";
const CHILD_DRAIN_STATES = new WeakMap();
const HANDLE_LOCK_TIMEOUT_MS = 10_000;
const HANDLE_CALLBACK_TIMEOUT_MS = 300_000;
const HANDLE_COMPLETION_TIMEOUT_MS = 60_000;
const HANDLE_CLOSE_TIMEOUT_MS = 5_000;
const HANDLE_DRAIN_TIMEOUT_MS = 5_000;
const DELETE_LOCK_RETRY_DELAYS_MS = Object.freeze([100, 250, 500, 1_000, 2_000, 4_000]);
const WINDOWS_DIRECTORY_MOVE_HELPER = String.raw`
import ctypes
import sys
from ctypes import wintypes

DELETE = 0x00010000
FILE_READ_ATTRIBUTES = 0x00000080
FILE_TRAVERSE = 0x00000020
SYNCHRONIZE = 0x00100000
FILE_SHARE_STABLE = 0x00000001 | 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_RENAME_INFORMATION = 10
WINDOWS_EPOCH_TICKS = 116444736000000000

# // 描述 Windows 目录句柄的卷号、FileIndex 与时间戳身份，供改名前后精确复核。
class ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]

# // 对齐 SetFileInformationByHandle 的改名缓冲区 ABI，禁止路径级替换绕过句柄身份。
class FileRenameInformation(ctypes.Structure):
    _fields_ = [
        ("replace", wintypes.BOOLEAN),
        ("root", wintypes.HANDLE),
        ("name_length", wintypes.ULONG),
        ("name", wintypes.WCHAR * 1),
    ]

# // 承接 NtSetInformationFile 的固定状态与完成字节数，不把原始系统错误写入证据。
class IoStatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

def fail():
    print("FAILED", flush=True)
    raise SystemExit(2)

def identity(handle):
    value = ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(value)):
        fail()
    creation_ticks = (int(value.creation.dwHighDateTime) << 32) | int(value.creation.dwLowDateTime)
    return (
        int(value.volume),
        (int(value.index_high) << 32) | int(value.index_low),
        (creation_ticks - WINDOWS_EPOCH_TICKS) * 100,
        int(value.attributes),
    )

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
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
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
ntdll.NtSetInformationFile.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(IoStatusBlock),
    ctypes.c_void_p,
    wintypes.ULONG,
    ctypes.c_int,
]
ntdll.NtSetInformationFile.restype = ctypes.c_long

source, destination_parent, destination_leaf = sys.argv[1:4]
expected_source = tuple(int(value) for value in sys.argv[4:7])
expected_parent = tuple(int(value) for value in sys.argv[7:10])
flags = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
source_handle = kernel32.CreateFileW(
    source,
    DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
    FILE_SHARE_STABLE,
    None,
    OPEN_EXISTING,
    flags,
    None,
)
parent_handle = kernel32.CreateFileW(
    destination_parent,
    FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
    FILE_SHARE_STABLE,
    None,
    OPEN_EXISTING,
    flags,
    None,
)
invalid = ctypes.c_void_p(-1).value
if source_handle in {None, invalid} or parent_handle in {None, invalid}:
    fail()
try:
    source_identity = identity(source_handle)
    parent_identity = identity(parent_handle)
    if (
        source_identity[:3] != expected_source
        or parent_identity[:3] != expected_parent
        or not (source_identity[3] & FILE_ATTRIBUTE_DIRECTORY)
        or not (parent_identity[3] & FILE_ATTRIBUTE_DIRECTORY)
        or source_identity[3] & FILE_ATTRIBUTE_REPARSE_POINT
        or parent_identity[3] & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        fail()
    print("LOCKED", flush=True)
    if sys.stdin.readline() != "MOVE\n":
        fail()
    encoded = destination_leaf.encode("utf-16-le")
    offset = FileRenameInformation.name.offset
    buffer_size = ctypes.sizeof(FileRenameInformation) + len(encoded)
    buffer = ctypes.create_string_buffer(buffer_size)
    information = FileRenameInformation.from_buffer(buffer)
    information.replace = 0
    information.root = parent_handle
    information.name_length = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    status = IoStatusBlock()
    result = ntdll.NtSetInformationFile(
        source_handle,
        ctypes.byref(status),
        buffer,
        buffer_size,
        FILE_RENAME_INFORMATION,
    )
    if result != 0:
        fail()
    print("MOVED", flush=True)
finally:
    parent_closed = bool(kernel32.CloseHandle(parent_handle))
    source_closed = bool(kernel32.CloseHandle(source_handle))
    if not parent_closed or not source_closed:
        fail()
`;
// 删除 helper 绑定完整目录树句柄后再执行删除，避免公开路径被替换时误删新节点。
const WINDOWS_BOUND_DELETE_HELPER = String.raw`
import ctypes
import os
import stat
import sys
from ctypes import wintypes

DELETE = 0x00010000
FILE_READ_ATTRIBUTES = 0x00000080
SYNCHRONIZE = 0x00100000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_DISPOSITION_INFO = 4
WINDOWS_EPOCH_TICKS = 116444736000000000

# // 描述待删除节点的卷号、FileIndex 与时间戳身份，确保递归树始终绑定原节点。
class ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]

# // 对齐 SetFileInformationByHandle 的删除标记 ABI，仅允许句柄绑定节点进入删除态。
class FileDispositionInformation(ctypes.Structure):
    _fields_ = [("delete", wintypes.BOOL)]

def fail():
    print("FAILED", flush=True)
    raise SystemExit(2)

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
kernel32.SetFileInformationByHandle.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
invalid = ctypes.c_void_p(-1).value

def extended_path(target):
    absolute = os.path.abspath(target)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute

def identity(handle):
    value = ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(value)):
        fail()
    creation_ticks = (int(value.creation.dwHighDateTime) << 32) | int(value.creation.dwLowDateTime)
    return (
        int(value.volume),
        (int(value.index_high) << 32) | int(value.index_low),
        (creation_ticks - WINDOWS_EPOCH_TICKS) * 100,
        int(value.attributes),
    )

def open_entry(target, expect_directory, expected=None):
    flags = FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if expect_directory else 0)
    handle = kernel32.CreateFileW(
        target,
        DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    if handle in {None, invalid}:
        fail()
    value = identity(handle)
    is_directory = bool(value[3] & FILE_ATTRIBUTE_DIRECTORY)
    if (
        is_directory != expect_directory
        or value[3] & FILE_ATTRIBUTE_REPARSE_POINT
        or (expected is not None and value[:3] != expected)
    ):
        kernel32.CloseHandle(handle)
        fail()
    return handle, value

def mark_delete(handle):
    information = FileDispositionInformation(True)
    if not kernel32.SetFileInformationByHandle(
        handle,
        FILE_DISPOSITION_INFO,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        fail()

def collect_tree(target, expect_directory, expected_handle=None, expected_path=None):
    information = os.lstat(target)
    attributes = int(getattr(information, "st_file_attributes", 0))
    if stat.S_ISLNK(information.st_mode) or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        fail()
    actual_directory = stat.S_ISDIR(information.st_mode)
    if actual_directory != expect_directory or (not actual_directory and not stat.S_ISREG(information.st_mode)):
        fail()
    path_expected = (int(information.st_ino), int(information.st_birthtime_ns))
    if expected_path is not None and path_expected != expected_path:
        fail()
    handle, value = open_entry(target, expect_directory)
    if (
        (expected_handle is not None and value[:3] != expected_handle)
        or value[1:3] != path_expected
    ):
        kernel32.CloseHandle(handle)
        fail()
    children = []
    if expect_directory:
        with os.scandir(target) as iterator:
            entries = list(iterator)
        for entry in entries:
            child_information = os.lstat(entry.path)
            child_attributes = int(getattr(child_information, "st_file_attributes", 0))
            if entry.is_symlink() or child_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                fail()
            child_directory = stat.S_ISDIR(child_information.st_mode)
            if not child_directory and not stat.S_ISREG(child_information.st_mode):
                fail()
            child_expected = (
                int(child_information.st_ino),
                int(child_information.st_birthtime_ns),
            )
            children.append(collect_tree(
                entry.path,
                child_directory,
                expected_path=child_expected,
            ))
    return [handle, value[:3], children]

def dispose_tree(node):
    handle, expected, children = node
    for child in children:
        dispose_tree(child)
    if identity(handle)[:3] != expected:
        fail()
    mark_delete(handle)
    if not kernel32.CloseHandle(handle):
        fail()

source = extended_path(sys.argv[1])
expected_source = tuple(int(value) for value in sys.argv[2:5])
root_node = collect_tree(source, True, expected_handle=expected_source)
print("LOCKED", flush=True)
if sys.stdin.readline() != "DELETE\n":
    fail()
dispose_tree(root_node)
print("DELETED", flush=True)
`;
// 稳定树 helper 在隐私扫描全程锁定目录和文件身份，并在释放前完成同域复扫。
const WINDOWS_TREE_LOCK_HELPER = String.raw`
import ctypes
import os
import stat
import sys
from ctypes import wintypes

FILE_READ_ATTRIBUTES = 0x00000080
FILE_READ_DATA = 0x00000001
FILE_LIST_DIRECTORY = 0x00000001
SYNCHRONIZE = 0x00100000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
WINDOWS_EPOCH_TICKS = 116444736000000000

# // 描述隐私扫描节点的卷号、FileIndex 与时间戳身份，支持锁定前后稳定性比较。
class ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]

def fail():
    print("FAILED", flush=True)
    raise SystemExit(2)

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
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
invalid = ctypes.c_void_p(-1).value

def extended_path(target):
    absolute = os.path.abspath(target)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute

def identity(handle):
    value = ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(value)):
        fail()
    creation_ticks = (int(value.creation.dwHighDateTime) << 32) | int(value.creation.dwLowDateTime)
    write_ticks = (int(value.write.dwHighDateTime) << 32) | int(value.write.dwLowDateTime)
    return (
        int(value.volume),
        (int(value.index_high) << 32) | int(value.index_low),
        (creation_ticks - WINDOWS_EPOCH_TICKS) * 100,
        int(value.attributes),
        (int(value.size_high) << 32) | int(value.size_low),
        (write_ticks - WINDOWS_EPOCH_TICKS) * 100,
    )

handles = []
handle_snapshot = {}

def path_identity(information, expected_directory):
    attributes = int(getattr(information, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(information.st_mode)
        or attributes & FILE_ATTRIBUTE_REPARSE_POINT
        or stat.S_ISDIR(information.st_mode) != expected_directory
        or (not expected_directory and not stat.S_ISREG(information.st_mode))
    ):
        fail()
    return (int(information.st_ino), int(information.st_birthtime_ns), expected_directory)

def open_locked(target, relative, expected_directory, expected_path):
    flags = FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if expected_directory else 0)
    access = FILE_READ_ATTRIBUTES | SYNCHRONIZE | (
        FILE_LIST_DIRECTORY if expected_directory else FILE_READ_DATA
    )
    handle = kernel32.CreateFileW(
        target,
        access,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    if handle in {None, invalid}:
        fail()
    value = identity(handle)
    if (
        bool(value[3] & FILE_ATTRIBUTE_DIRECTORY) != expected_directory
        or value[3] & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        kernel32.CloseHandle(handle)
        fail()
    if value[1:3] != expected_path[:2]:
        kernel32.CloseHandle(handle)
        fail()
    handles.append((handle, value))
    handle_snapshot[relative] = value[:3] + (expected_directory,)
    return value

def walk(target, relative="", expected_path=None):
    information = os.lstat(target)
    observed_path = path_identity(information, True)
    if expected_path is not None and observed_path != expected_path:
        fail()
    root_value = open_locked(target, relative, True, observed_path)
    with os.scandir(target) as iterator:
        entries = list(iterator)
    for entry in entries:
        information = os.lstat(entry.path)
        child_relative = os.path.join(relative, entry.name) if relative else entry.name
        if stat.S_ISDIR(information.st_mode):
            child_path = path_identity(information, True)
            walk(entry.path, child_relative, child_path)
        elif stat.S_ISREG(information.st_mode):
            child_path = path_identity(information, False)
            opened_information = os.lstat(entry.path)
            if path_identity(opened_information, False) != child_path:
                fail()
            open_locked(entry.path, child_relative, False, child_path)
        else:
            fail()
    return root_value

root = extended_path(sys.argv[1])
expected_root = tuple(int(value) for value in sys.argv[2:5])
root_value = walk(root)
if root_value[:3] != expected_root:
    fail()
print("LOCKED", flush=True)
if sys.stdin.readline() != "RELEASE\n":
    fail()

# File ID、size 与 write time 只在同一个 Win32 handle 域内比较，避免跨 API 时间表示差异。
for handle, before in handles:
    after = identity(handle)
    if before != after:
        fail()

current = {}
def current_handle_identity(target, expected_directory):
    flags = FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if expected_directory else 0)
    handle = kernel32.CreateFileW(
        target,
        FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    if handle in {None, invalid}:
        fail()
    try:
        value = identity(handle)
        if (
            bool(value[3] & FILE_ATTRIBUTE_DIRECTORY) != expected_directory
            or value[3] & FILE_ATTRIBUTE_REPARSE_POINT
        ):
            fail()
        return value[:3] + (expected_directory,)
    finally:
        if not kernel32.CloseHandle(handle):
            fail()

def rescan(target, relative=""):
    information = os.lstat(target)
    is_directory = stat.S_ISDIR(information.st_mode)
    path_identity(information, is_directory)
    current[relative] = current_handle_identity(target, is_directory)
    if is_directory:
        with os.scandir(target) as iterator:
            entries = list(iterator)
        for entry in entries:
            child_relative = os.path.join(relative, entry.name) if relative else entry.name
            rescan(entry.path, child_relative)
    elif not stat.S_ISREG(information.st_mode):
        fail()
rescan(root)
if current != handle_snapshot:
    fail()

close_failed = False
for handle, _ in reversed(handles):
    if not kernel32.CloseHandle(handle):
        close_failed = True
if close_failed:
    fail()
print("VERIFIED", flush=True)
`;
// 路由族映射只用于断言后端访问范围，前端分类不能替代服务端权限校验。
const CONTROL_ROUTE_FAMILIES = Object.freeze({
  adapter: "adapter",
  applications: "applications",
  audit: "audit",
  batch: "batch",
  budget: "budget",
  budgets: "budget",
  cache: "cache",
  chat: "chat",
  console: "console",
  cost: "cost",
  debug: "debug",
  developer: "developer",
  dlp: "dlp",
  error: "error",
  experiments: "experiments",
  feedback: "feedback",
  files: "files",
  finops: "finops",
  health: "health",
  internal: "internal",
  invitations: "invitations",
  model: "model",
  policies: "policies",
  policy: "policy",
  projects: "projects",
  prompt: "prompt",
  provider: "provider",
  providers: "provider",
  request: "request",
  roles: "roles",
  security: "security",
  status: "status",
  usage: "usage",
  user: "user",
  virtual: "virtual",
  webhook: "webhook",
  webhooks: "webhook",
});
// 健康探测类别单独列入可恢复网络请求，其他失败仍按异常流量处理。
const HEALTH_REQUEST_CATEGORIES = new Set([
  "control_health_live",
  "control_health_ready",
  "control_health_summary",
  "control_health_version",
  "gateway_health_live",
  "gateway_health_ready",
  "gateway_health_summary",
  "gateway_health_version",
]);
// 链路格式、扫描上限与禁止字段定义隐私门禁的固定资源边界。
const HEALTH_ATTEMPT_REQUEST_ID = /^([A-Za-z0-9][A-Za-z0-9._:-]{0,239})_attempt_([12])$/;
const LOWER_SHA256 = /^[0-9a-f]{64}$/;
const CONSOLE_OBJECT_LIMIT = 100;
const CONSOLE_DEPTH_LIMIT = 3;
const ASSISTANT_RESPONSE_MAX_BYTES = 256 * 1024;
const ASSISTANT_RESPONSE_MAX_DEPTH = 8;
const ASSISTANT_RESPONSE_MAX_NODES = 4096;
const ASSISTANT_RESPONSE_MAX_OBJECT_KEYS = 64;
const ASSISTANT_RESPONSE_MAX_ARRAY_ITEMS = 256;
const ANSWER_LEAF_MIN_LENGTH = 16;
const ANSWER_LEAF_MAX_LENGTH = 4096;
const ANSWER_LEAF_INPUT_MAX_COUNT = 260;
const ANSWER_LEAF_MAX_COUNT = 2048;
const RECENT_INSPECTION_TEXT_LIMIT = ASSISTANT_RESPONSE_MAX_BYTES + (64 * 1024);
const FORBIDDEN_QUERY_KEY = /tenant|password|token|secret|authorization|prompt|response|provider_key/i;
const FORBIDDEN_EVIDENCE_KEYS = new Set([
  "authorization",
  "token",
  "access_token",
  "password",
  "secret",
  "api_key",
  "provider_key",
  "prompt",
  "question",
  "answer",
  "response_body",
  "body",
  "payload",
  "tenant_id",
  "user_id",
  "email",
  "database_url",
  "endpoint_url",
]);
// 私有配置标签只用于内存扫描计数，标签对应的值不得进入持久证据。
const PRIVATE_CONFIGURATION_LABELS = Object.freeze([
  "admin_password",
  "role_password",
  "second_password",
  "auth_secret",
  "provider_secret",
  "file_encryption_secret",
  "batch_encryption_secret",
  "prompt_template_encryption_secret",
  "cache_affinity_hmac_secret",
  "provider_key",
  "virtual_key",
  "second_virtual_key",
  "question_canary",
  "answer_canary",
]);
// Python harness 只继承启动解释器所需的宿主键；其余登录态或云凭据不得进入子进程。
const HARNESS_HOST_ENVIRONMENT_ALLOWLIST = Object.freeze(new Set([
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
]));
// 验收账号固定覆盖具备助手权限的角色，不允许通过外部参数替换身份。
const ROLE_ACCOUNTS = Object.freeze({
  admin: Object.freeze({ email: "admin@metahub.local", passwordKey: "adminPassword" }),
  finance: Object.freeze({
    email: "finance-prd077@local.invalid",
    passwordKey: "rolePassword",
  }),
  security: Object.freeze({
    email: "security-prd077@local.invalid",
    passwordKey: "rolePassword",
  }),
});
// 四类只读问题绑定固定展示标签，确保回答来源和权限路径可精确断言。
const QUERY_CASES = Object.freeze({
  usage: Object.freeze({
    text: "过去 7 天调用情况如何？",
    label: "调用统计",
  }),
  errors: Object.freeze({
    text: "解释最近 24 小时的主要错误。",
    label: "错误解释",
  }),
  cost: Object.freeze({
    text: "过去 30 天有哪些成本优化建议？",
    label: "成本建议",
  }),
  policy: Object.freeze({
    text: "解释当前策略状态和需要人工复核的优先级碰撞。",
    label: "策略解释",
  }),
});
// 响应键白名单冻结浏览器可接受的只读助手契约，意外字段一律拒绝进入证据。
const ASSISTANT_RESPONSE_KEYS = Object.freeze([
  "answer",
  "assistant_audit_id",
  "assistant_query_id",
  "query_type",
  "redactions",
  "request_id",
  "sources",
  "trace_id",
]);
// 每种只读查询的期望响应次数用于证明完整流程，不允许缺失或额外后台请求。
const EXPECTED_RESPONSE_QUERY_COUNTS = Object.freeze({
  usage_statistics: 2,
  error_explanation: 2,
  cost_advice: 1,
  policy_explanation: 1,
});
// 来源清单冻结最终证据所依赖的代码与迁移，发布前必须重新核对摘要。
const SOURCE_PROVENANCE_NAMES = Object.freeze([
  "browser_driver",
  "browser_harness",
  "browser_helper_prd056",
  "browser_helper_prd066",
  "console_dev_server",
  "console_entry",
  "control_plane_source_tree",
  "frontend_source_tree",
  "gateway_source_tree",
  "migration_0070_down",
  "migration_0070_up",
  "migration_set",
  "runtime_acceptance",
  "runtime_helper_prd075",
  "shared_sdk_source_tree",
]);
// Browser 汇总键限制最终 JSON 只保存流程、网络、隐私与来源的安全事实。
const BROWSER_SUMMARY_KEYS = Object.freeze([
  "schema_version",
  "status",
  "prd_id",
  "page_id",
  "workflow",
  "viewports",
  "network",
  "privacy",
  "source_provenance",
  "all_108_prds_complete",
]);
// Runtime 汇总键用于交叉核验服务、撤权、数据库和进程回收结果。
const RUNTIME_SUMMARY_KEYS = Object.freeze([
  "schema_version",
  "status",
  "gateway_http_200",
  "control_http_200",
  "console_http_200",
  "finance_source_permission_revoked",
  "base_permission_retained",
  "backend_denied_after_source_revoke",
  "privacy",
  "database",
  "owned_processes_remaining",
  "all_108_prds_complete",
]);
// 数据库健康字段只允许迁移版本、完整性和外键违规等非敏感事实。
const DATABASE_HEALTH_KEYS = Object.freeze([
  "migration_count",
  "latest_migration",
  "integrity_check",
  "foreign_key_violations",
]);
// 隐私扫描与清单键共同约束发布制品的精确字段集合和文件集合。
const PRIVATE_SCAN_KEYS = Object.freeze([
  "files_scanned",
  "plaintext_matches",
  "sqlite_sidecars_remaining",
]);
const MANIFEST_KEYS = Object.freeze([
  "schema_version",
  "status",
  "prd_id",
  "page_id",
  "files",
  "source_provenance",
  "all_108_prds_complete",
]);


/** 失败只保留固定 stage/code；动态 URL、正文、Header 与底层异常永不进入终端。 */
class BrowserAcceptanceFailure extends Error {
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 统一失败关闭断言，stage/code 必须来自源码中的固定坐标。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** PRD-077 页面在新机器冷启动时仍使用有界等待，不改变任何业务 deadline。 */
async function waitForDom(client, sessionId, expression, stage) {
  return waitForDomBase(client, sessionId, expression, stage, DOM_WAIT_TIMEOUT_MS);
}


/** 把共享 CDP helper 的错误折叠成本驱动固定坐标，禁止传播原始 message 或 stack。 */
function safeFailure(error, stage = "browser.acceptance") {
  if (error instanceof BrowserAcceptanceFailure) return error;
  if (
    error?.name === "BrowserAcceptanceFailure"
    && SAFE_COORDINATE.test(String(error.stage ?? ""))
    && SAFE_COORDINATE.test(String(error.code ?? ""))
  ) {
    return new BrowserAcceptanceFailure(error.stage, error.code);
  }
  return new BrowserAcceptanceFailure(stage, "unexpected");
}


/** 清理失败必须阻止成功发布，但不得覆盖更早产生的安全失败坐标。 */
function assertCleanupFailurePriority(cleanupFailed, primaryFailure) {
  if (cleanupFailed && primaryFailure === null) {
    throw new BrowserAcceptanceFailure("cleanup", "failed");
  }
  return primaryFailure;
}


/** run-id 只能是单个短 ASCII 路径段，禁止覆盖、绝对路径和父目录跳转。 */
function safeRunId(value) {
  const fallback = `prd077-${new Date().toISOString().replaceAll(/[-:.]/g, "")}`;
  const candidate = String(value || fallback);
  requireCondition(
    SAFE_RUN_ID.test(candidate) && !candidate.includes(".."),
    "config.run_id",
    "invalid",
  );
  return candidate;
}


/** 规范化页面提供的数组索引，不假设来源项必定位于第零项。 */
function canonicalSourceIndex(value) {
  const candidate = String(value ?? "");
  return /^(?:0|[1-9][0-9]*)$/.test(candidate) ? candidate : null;
}


/** CLI 只允许一个可选 --run-id；端口、路径、Origin 和账号不能由外部覆盖。 */
function parseArguments(argv = process.argv.slice(2)) {
  let runId = "";
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] !== "--run-id" || runId || index + 1 >= argv.length) {
      throw new BrowserAcceptanceFailure("config.arguments", "invalid");
    }
    runId = argv[index + 1];
    index += 1;
  }
  return Object.freeze({ runId: safeRunId(runId) });
}


/** 将 run-id 绑定为固定根的直属子目录，避免清理时枚举或命中其它批次。 */
function ownedRunDirectory(root, runId) {
  const target = path.resolve(root, safeRunId(runId));
  const relative = path.relative(path.resolve(root), target);
  requireCondition(
    relative === runId && !path.isAbsolute(relative) && !relative.includes(path.sep),
    "config.run_directory",
    "outside_root",
  );
  return target;
}


/** staging 名称由 run-id 单向派生，不能通过 CLI 选择隐藏目录或覆盖最终制品。 */
function ownedStagingDirectory(root, runId) {
  const safe = safeRunId(runId);
  const name = `.${safe}.staging`;
  const target = path.resolve(root, name);
  requireCondition(
    path.relative(path.resolve(root), target) === name,
    "config.staging_directory",
    "outside_root",
  );
  return target;
}


/** 生成仅存在当前进程和 harness 子进程环境中的随机私有值。 */
function createPrivateConfiguration() {
  const value = (prefix, bytes = 24) => `${prefix}-${randomBytes(bytes).toString("base64url")}`;
  let answerMicros = 10_000_000_001
    + (randomBytes(6).readUIntBE(0, 6) % 89_999_999_999);
  // 后端 canonical decimal 会裁掉尾零；末位非零可保证 Browser 针仍是固定六位且原样渲染。
  if (answerMicros % 10 === 0) answerMicros += 1;
  const fraction = String(answerMicros % 1_000_000).padStart(6, "0");
  return Object.freeze({
    adminPassword: value("Prd077Admin"),
    rolePassword: value("Prd077Role"),
    secondPassword: value("Prd077Second"),
    authSecret: value("prd077-auth", 48),
    providerSecret: value("prd077-provider-secret", 48),
    fileEncryptionSecret: value("prd077-file-secret", 48),
    batchEncryptionSecret: value("prd077-batch-secret", 48),
    promptTemplateEncryptionSecret: value("prd077-prompt-secret", 48),
    cacheAffinityHmacSecret: value("prd077-cache-secret", 48),
    providerKey: value("prd077-provider-key", 32),
    virtualKey: value("sk-mh-prd077", 32),
    secondVirtualKey: value("sk-mh-prd077-second", 32),
    questionCanary: `PRD077_QUESTION_${randomBytes(20).toString("hex")}`,
    answerCanary: `${Math.floor(answerMicros / 1_000_000)}.${fraction}`,
  });
}


/** 生成仅用于本轮目录所有权的一次性 nonce；制品只短暂保存其 SHA-256。 */
function createOwnershipClaim() {
  const nonce = randomBytes(32).toString("hex");
  return Object.freeze({
    nonce,
    digest: createHash("sha256").update(nonce, "ascii").digest("hex"),
  });
}


/** 为金额回答冻结精确 claim 与来源 oracle；其他查询必须完全不渲染金额针。 */
function answerOracleForQuery(name, answerCanary) {
  if (name === "usage") {
    return Object.freeze({
      claimLabel: "用量成本",
      claimValue: `${answerCanary} CNY`,
      sourceLabel: "用量账本",
      sourceHref: "/#usage",
    });
  }
  if (name === "cost") {
    return Object.freeze({
      claimLabel: "已入账借方成本",
      claimValue: `${answerCanary} CNY`,
      sourceLabel: "FinOps 账本",
      sourceHref: "/#finops-ledger",
    });
  }
  return null;
}


/** 把最小宿主环境和私有配置映射到 harness 固定键，不透传宿主登录态或云凭据。 */
function harnessEnvironment(privateConfig, ownershipClaim, hostEnvironment = process.env) {
  const environment = {};
  for (const [name, value] of Object.entries(hostEnvironment)) {
    const normalized = name.toUpperCase();
    if (HARNESS_HOST_ENVIRONMENT_ALLOWLIST.has(normalized) && typeof value === "string") {
      environment[normalized] = value;
    }
  }
  return {
    ...environment,
    PYTHONDONTWRITEBYTECODE: "1",
    TEMP: path.join(ROOT, ".tmp"),
    TMP: path.join(ROOT, ".tmp"),
    PRD077_BROWSER_ADMIN_PASSWORD: privateConfig.adminPassword,
    PRD077_BROWSER_ROLE_PASSWORD: privateConfig.rolePassword,
    PRD077_BROWSER_SECOND_PASSWORD: privateConfig.secondPassword,
    PRD077_BROWSER_AUTH_SECRET: privateConfig.authSecret,
    PRD077_BROWSER_PROVIDER_SECRET: privateConfig.providerSecret,
    PRD077_BROWSER_FILE_ENCRYPTION_SECRET: privateConfig.fileEncryptionSecret,
    PRD077_BROWSER_BATCH_ENCRYPTION_SECRET: privateConfig.batchEncryptionSecret,
    PRD077_BROWSER_PROMPT_TEMPLATE_ENCRYPTION_SECRET: (
      privateConfig.promptTemplateEncryptionSecret
    ),
    PRD077_BROWSER_CACHE_AFFINITY_HMAC_SECRET: privateConfig.cacheAffinityHmacSecret,
    PRD077_BROWSER_PROVIDER_KEY: privateConfig.providerKey,
    PRD077_BROWSER_VIRTUAL_KEY: privateConfig.virtualKey,
    PRD077_BROWSER_SECOND_VIRTUAL_KEY: privateConfig.secondVirtualKey,
    PRD077_BROWSER_QUESTION_CANARY: privateConfig.questionCanary,
    PRD077_BROWSER_ANSWER_CANARY: privateConfig.answerCanary,
    PRD077_BROWSER_OWNER_NONCE: ownershipClaim.nonce,
  };
}


/** 检查固定回环端口未被既有进程占用；本驱动绝不接管或终止未知监听者。 */
async function assertPortFree(port) {
  const free = await new Promise((resolve) => {
    const server = createServer();
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => server.close(() => resolve(true)));
  });
  requireCondition(free, "config.debug_port", "unavailable");
}


/**
 * 浏览器必须是与固定 Node 运行时同架构的有效 Windows PE，避免 ARM64 主机误启 x64
 * Chrome 后只留下 Crashpad 进程、却永远不开放调试端口。
 */
function assertBrowserExecutableImage(image, runtimeArchitecture = process.arch) {
  const expectedMachine = WINDOWS_PE_MACHINE_BY_ARCHITECTURE[runtimeArchitecture];
  requireCondition(
    Buffer.isBuffer(image) && image.length >= 64 && expectedMachine !== undefined,
    "config.chrome",
    "architecture",
  );
  const peOffset = image.readUInt32LE(0x3c);
  requireCondition(
    peOffset >= 0x40
      && peOffset <= image.length - 6
      && image.readUInt32LE(peOffset) === 0x00004550
      && image.readUInt16LE(peOffset + 4) === expectedMachine,
    "config.chrome",
    "architecture",
  );
  return true;
}


/** 读取固定浏览器映像并在启动服务前完成架构门禁，底层路径错误不进入终端。 */
async function assertBrowserExecutable(binary = CHROME, runtimeArchitecture = process.arch) {
  try {
    return assertBrowserExecutableImage(await readFile(binary), runtimeArchitecture);
  } catch (error) {
    // 已归一化的安全坐标必须原样上抛，不能被底层 unreadable 分类覆盖。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure("config.chrome", "unreadable");
  }
}


/** 路径存在性必须由 lstat 判定，避免 existsSync 跟随未解析的 junction 或符号链接。 */
async function pathEntryExists(target, stage = "filesystem.entry") {
  try {
    await lstat(target);
    return true;
  } catch (error) {
    // 只有明确的 ENOENT 可以表示节点不存在，其他 lstat 错误必须固定失败关闭。
    if (error?.code === "ENOENT") return false;
    throw new BrowserAcceptanceFailure(stage, "lstat");
  }
}


/** 将平台文件标识冻结为纯内存字符串；目录内容变化不会改变这些字段。 */
function directoryIdentityFromStat(information) {
  return Object.freeze({
    device: String(information.dev),
    inode: String(information.ino),
    birthtime_ns: String(information.birthtimeNs),
  });
}


/** 验证目录是精确普通目录而非链接或 Windows reparse point，并返回稳定身份。 */
async function captureDirectoryIdentity(directory, stage) {
  try {
    // 必须先 lstat 未解析路径；只有确认叶节点不是 symlink/junction 后才能调用 realpath。
    const information = await lstat(directory, { bigint: true });
    requireCondition(
      information.isDirectory() && !information.isSymbolicLink(),
      stage,
      "not_plain",
    );
    const canonical = await realpath(directory);
    const expected = path.normalize(path.resolve(directory));
    const actual = path.normalize(path.resolve(canonical));
    requireCondition(
      process.platform === "win32"
        ? actual.toLowerCase() === expected.toLowerCase()
        : actual === expected,
      stage,
      "reparse_point",
    );
    return directoryIdentityFromStat(information);
  } catch (error) {
    // 已归一化的安全坐标应原样保留，文件系统原始错误则折叠为固定缺失坐标。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "missing");
  }
}


/** 目录必须仍是最初取得 owner 时的同一个文件系统对象。 */
async function assertDirectoryIdentity(directory, expectedIdentity, stage) {
  requireCondition(
    expectedIdentity
      && typeof expectedIdentity === "object"
      && ["device", "inode", "birthtime_ns"].every(
        (key) => typeof expectedIdentity[key] === "string" && expectedIdentity[key].length > 0,
      ),
    stage,
    "identity",
  );
  const actual = await captureDirectoryIdentity(directory, stage);
  requireCondition(
    actual.device === expectedIdentity.device
      && actual.inode === expectedIdentity.inode
      && actual.birthtime_ns === expectedIdentity.birthtime_ns,
    stage,
    "identity",
  );
  return true;
}


/** 清理分支只需要布尔结果；任何动态文件系统错误都折叠为“不再拥有”。 */
async function directoryIdentityMatches(directory, expectedIdentity) {
  try {
    await assertDirectoryIdentity(directory, expectedIdentity, "cleanup.identity");
    return true;
  } catch {
    return false;
  }
}


/**
 * 运行只输出固定控制词的 Windows 句柄 helper，并为锁定与完成阶段分别设置硬超时。
 * `whileLocked` 只在 helper 已经取得全部目标句柄后执行，异常时只回收该精确 child PID。
 */
async function runWindowsHandleHelper({
  script,
  args,
  stage,
  command,
  successWord,
  whileLocked = async () => undefined,
  lockTimeoutMs = HANDLE_LOCK_TIMEOUT_MS,
  callbackTimeoutMs = HANDLE_CALLBACK_TIMEOUT_MS,
  completionTimeoutMs = HANDLE_COMPLETION_TIMEOUT_MS,
  closeTimeoutMs = HANDLE_CLOSE_TIMEOUT_MS,
  drainTimeoutMs = HANDLE_DRAIN_TIMEOUT_MS,
  spawnOperation = spawn,
}) {
  requireCondition(process.platform === "win32", stage, "handle_unsupported");
  for (const [value, maximum] of [
    [lockTimeoutMs, HANDLE_LOCK_TIMEOUT_MS],
    [callbackTimeoutMs, HANDLE_CALLBACK_TIMEOUT_MS],
    [completionTimeoutMs, HANDLE_COMPLETION_TIMEOUT_MS],
    [closeTimeoutMs, HANDLE_CLOSE_TIMEOUT_MS],
    [drainTimeoutMs, HANDLE_DRAIN_TIMEOUT_MS],
  ]) {
    requireCondition(
      Number.isInteger(value) && value > 0 && value <= maximum,
      stage,
      "handle_timeout",
    );
  }
  requireCondition(
    typeof script === "string"
      && Array.isArray(args)
      && args.every((value) => typeof value === "string")
      && /^[A-Z_]{2,32}$/.test(command)
      && /^[A-Z_]{2,32}$/.test(successWord)
      && typeof whileLocked === "function"
      && typeof spawnOperation === "function",
    stage,
    "handle_contract",
  );
  const child = spawnOperation(PYTHON, ["-I", "-c", script, ...args], {
    cwd: ROOT,
    env: Object.freeze({
      SYSTEMROOT: process.env.SYSTEMROOT ?? "C:\\Windows",
      WINDIR: process.env.WINDIR ?? "C:\\Windows",
    }),
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  child.stdin.setDefaultEncoding("ascii");
  const drainState = observeChildDrain(child);
  let stdout = "";
  let stderrBytes = 0;
  let locked = false;
  let lockSettled = false;
  let completionSettled = false;
  let resolveLock;
  let rejectLock;
  let resolveCompletion;
  let rejectCompletion;
  const lockPromise = new Promise((resolve, reject) => {
    resolveLock = resolve;
    rejectLock = reject;
  });
  const completionPromise = new Promise((resolve, reject) => {
    resolveCompletion = resolve;
    rejectCompletion = reject;
  });
  // 两个 promise 从 spawn 时就受事件驱动；提前失败也必须视为已被观察，避免延迟 await 产生告警。
  lockPromise.catch(() => undefined);
  completionPromise.catch(() => undefined);
  const rejectPending = () => {
    if (lockSettled) return;
    lockSettled = true;
    rejectLock(new BrowserAcceptanceFailure(stage, "handle_lock"));
  };
  const rejectPendingCompletion = () => {
    if (completionSettled) return;
    completionSettled = true;
    rejectCompletion(new BrowserAcceptanceFailure(stage, "handle_operation"));
  };
  child.stdout.on("data", (chunk) => {
    stdout = `${stdout}${String(chunk)}`.slice(-4096);
    if (!lockSettled && /(?:^|\r?\n)LOCKED\r?\n/.test(stdout)) {
      locked = true;
      lockSettled = true;
      resolveLock();
    }
    if (!completionSettled && new RegExp(`(?:^|\\r?\\n)${successWord}\\r?\\n`).test(stdout)) {
      completionSettled = true;
      resolveCompletion();
    }
  });
  child.stderr.on("data", (chunk) => {
    stderrBytes = Math.min(4097, stderrBytes + Buffer.byteLength(chunk));
  });
  child.once("error", () => {
    rejectPending();
    rejectPendingCompletion();
  });
  child.once("close", () => {
    rejectPending();
    rejectPendingCompletion();
  });

  let primaryFailure = null;
  let result;
  /**
   * 用单一有界 deadline 包装 helper 阶段，并把超时收敛为固定验收错误码。
   */
  const withTimeout = async (operation, timeoutMs, code) => {
    let timer;
    try {
      return await Promise.race([
        operation,
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(new BrowserAcceptanceFailure(stage, code)),
            timeoutMs,
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  };
  try {
    await withTimeout(lockPromise, lockTimeoutMs, "handle_lock_timeout");
    result = await withTimeout(
      Promise.resolve().then(() => whileLocked(Object.freeze({ pid: child.pid }))),
      callbackTimeoutMs,
      "handle_callback_timeout",
    );
    child.stdin.end(`${command}\n`);
    await withTimeout(completionPromise, completionTimeoutMs, "handle_completion_timeout");
    const exitCode = await withTimeout(
      drainState.promise,
      closeTimeoutMs,
      "handle_close_timeout",
    );
    requireCondition(
      locked
        && exitCode === 0
        && stderrBytes === 0,
      stage,
      "handle_operation",
    );
    return result;
  } catch (error) {
    primaryFailure = error instanceof BrowserAcceptanceFailure
      ? error
      : new BrowserAcceptanceFailure(stage, "handle_operation");
    throw primaryFailure;
  } finally {
    if (!drainState.closeObserved) {
      try { if (!child.stdin.destroyed) child.stdin.destroy(); } catch {}
      try { if (child.exitCode === null) child.kill(); } catch {}
      try {
        await withTimeout(
          drainState.promise,
          drainTimeoutMs,
          "handle_drain_timeout",
        );
      } catch (error) {
        // 句柄排空超时保留既有固定错误，其他异常也折叠到同一安全坐标。
        throw error instanceof BrowserAcceptanceFailure
          ? error
          : new BrowserAcceptanceFailure(stage, "handle_drain_timeout");
      }
    }
  }
}


/**
 * Windows 上先由 helper 打开 source 与目标 parent 句柄并复核 File ID，再按句柄移动。
 * `afterLocked` 只用于本地竞态合同；它发生在句柄锁定后、实际 move 前。
 */
async function moveVerifiedDirectory(
  source,
  destination,
  expectedIdentity,
  stage,
  {
    destinationParentIdentity = null,
    afterLocked = async () => undefined,
  } = {},
) {
  const resolvedSource = path.resolve(source);
  const resolvedDestination = path.resolve(destination);
  const destinationParent = path.dirname(resolvedDestination);
  const destinationLeaf = path.basename(resolvedDestination);
  requireCondition(
    destinationLeaf && destinationLeaf !== "." && destinationLeaf !== "..",
    stage,
    "rename_target",
  );
  const parentIdentity = destinationParentIdentity
    ?? await captureDirectoryIdentity(destinationParent, `${stage}_parent`);
  await assertDirectoryIdentity(resolvedSource, expectedIdentity, stage);
  await assertDirectoryIdentity(destinationParent, parentIdentity, `${stage}_parent`);

  // Node 的路径 rename 会在校验后重新解析 source；Windows 验收只能使用 File ID 绑定句柄。
  await runWindowsHandleHelper({
    script: WINDOWS_DIRECTORY_MOVE_HELPER,
    args: [
      resolvedSource,
      destinationParent,
      destinationLeaf,
      expectedIdentity.device,
      expectedIdentity.inode,
      expectedIdentity.birthtime_ns,
      parentIdentity.device,
      parentIdentity.inode,
      parentIdentity.birthtime_ns,
    ],
    stage,
    command: "MOVE",
    successWord: "MOVED",
    whileLocked: afterLocked,
    completionTimeoutMs: 30_000,
  });
}


/** 由已核对 File ID 且禁止 delete-sharing 的根句柄递归删除 exact quarantine 对象。 */
async function deleteVerifiedDirectoryTree(
  directory,
  expectedIdentity,
  stage,
  { afterLocked = async () => undefined } = {},
) {
  await assertDirectoryIdentity(directory, expectedIdentity, stage);
  await runWindowsHandleHelper({
    script: WINDOWS_BOUND_DELETE_HELPER,
    args: [
      path.resolve(directory),
      expectedIdentity.device,
      expectedIdentity.inode,
      expectedIdentity.birthtime_ns,
    ],
    stage,
    command: "DELETE",
    successWord: "DELETED",
    whileLocked: afterLocked,
    completionTimeoutMs: 60_000,
  });
}


/**
 * Windows 可能在 Chrome 父进程与 stdio 已排空后短暂保留 Crashpad/杀毒扫描句柄。
 * 只允许重试 helper 尚未输出 LOCKED、也尚未收到 DELETE 指令的前置失败；每次重试都由
 * 调用方重新证明 quarantine inventory、目录身份与 owner claim，禁止重放任何可能已开始
 * 删除的 handle_operation 或未知失败。
 */
async function deleteVerifiedDirectoryTreeAfterRelease(
  directory,
  expectedIdentity,
  stage,
  {
    afterLocked = async () => undefined,
    verifyOwner = async () => assertDirectoryIdentity(directory, expectedIdentity, stage),
    deleteOperation = deleteVerifiedDirectoryTree,
    delayOperation = (delayMs) => new Promise((resolve) => setTimeout(resolve, delayMs)),
    retryDelays = DELETE_LOCK_RETRY_DELAYS_MS,
  } = {},
) {
  requireCondition(
    typeof verifyOwner === "function"
      && typeof deleteOperation === "function"
      && typeof delayOperation === "function"
      && Array.isArray(retryDelays)
      && retryDelays.length <= DELETE_LOCK_RETRY_DELAYS_MS.length
      && retryDelays.every(
        (delayMs, index) => Number.isInteger(delayMs)
          && delayMs > 0
          && delayMs <= DELETE_LOCK_RETRY_DELAYS_MS[index],
      ),
    stage,
    "delete_retry_contract",
  );
  for (let attempt = 0; ; attempt += 1) {
    await verifyOwner();
    try {
      return await deleteOperation(directory, expectedIdentity, stage, { afterLocked });
    } catch (error) {
      const retryable = error instanceof BrowserAcceptanceFailure
        && error.stage === stage
        && ["handle_lock", "handle_lock_timeout"].includes(error.code)
        && attempt < retryDelays.length;
      // 身份、claim 或删除阶段错误均不可重试，避免触碰已被替换的目录树。
      if (!retryable) throw error;
      // helper 未取得全树句柄，因此没有收到 DELETE；再次证明 owner 后才允许有界等待。
      await verifyOwner();
      await delayOperation(retryDelays[attempt]);
    }
  }
}


/** 在 Windows helper 持有整棵树的禁止写入/删除句柄期间执行只读门禁并复核 exact inventory。 */
async function withStableDirectoryTree(directory, expectedIdentity, stage, operation) {
  return runWindowsHandleHelper({
    script: WINDOWS_TREE_LOCK_HELPER,
    args: [
      path.resolve(directory),
      expectedIdentity.device,
      expectedIdentity.inode,
      expectedIdentity.birthtime_ns,
    ],
    stage,
    command: "RELEASE",
    successWord: "VERIFIED",
    whileLocked: operation,
    completionTimeoutMs: 60_000,
  });
}


/** 兼容只需验证普通目录的调用；owner 路径应改用 capture/assert identity。 */
async function assertPlainDirectory(directory, stage) {
  await captureDirectoryIdentity(directory, stage);
  return directory;
}


/** 验证本轮 owner claim 是直属普通文件且摘要精确匹配，不信任同名目录本身。 */
async function assertOwnershipClaim(
  directory,
  ownershipClaim,
  stage,
  expectedIdentity = null,
) {
  requireCondition(
    ownershipClaim && LOWER_SHA256.test(String(ownershipClaim.digest ?? "")),
    stage,
    "owner_claim",
  );
  if (expectedIdentity) {
    await assertDirectoryIdentity(directory, expectedIdentity, stage);
  } else {
    await assertPlainDirectory(directory, stage);
  }
  const target = path.join(directory, OWNER_CLAIM_FILENAME);
  try {
    const information = await lstat(target);
    const canonical = await realpath(target);
    requireCondition(
      information.isFile()
        && !information.isSymbolicLink()
        && information.size === 65
        && path.normalize(canonical).toLowerCase() === path.normalize(target).toLowerCase()
        && await readFile(target, "ascii") === `${ownershipClaim.digest}\n`,
      stage,
      "owner_claim",
    );
  } catch (error) {
    // 所有权校验失败不得泄露文件系统细节，既有安全坐标仍保持最高优先级。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "owner_claim");
  }
  return true;
}


/** 只在完成全部发布校验后移除内部 claim，使最终制品保持 exact inventory。 */
async function removeOwnershipClaim(
  directory,
  ownershipClaim,
  stage,
  expectedIdentity = null,
) {
  await assertOwnershipClaim(directory, ownershipClaim, stage, expectedIdentity);
  try {
    await unlink(path.join(directory, OWNER_CLAIM_FILENAME));
  } catch {
    throw new BrowserAcceptanceFailure(stage, "owner_claim_remove");
  }
  requireCondition(
    !(await pathEntryExists(path.join(directory, OWNER_CLAIM_FILENAME), stage)),
    stage,
    "owner_claim_remove",
  );
}


/** 清理异常时只返回 claim 是否仍匹配，不传播动态文件系统错误。 */
async function ownershipClaimMatches(directory, ownershipClaim, expectedIdentity = null) {
  if (!directory || !(await pathEntryExists(directory, "cleanup.owner_claim"))) return false;
  try {
    await assertOwnershipClaim(
      directory,
      ownershipClaim,
      "cleanup.owner_claim",
      expectedIdentity,
    );
    return true;
  } catch {
    return false;
  }
}


/** 单文件 provenance 只接受普通文件，并输出规范小写 SHA-256。 */
async function sourceFileDigest(filename, stage = "provenance.file") {
  const information = await lstat(filename);
  requireCondition(
    information.isFile() && !information.isSymbolicLink(),
    stage,
    "not_plain",
  );
  return createHash("sha256").update(await readFile(filename)).digest("hex");
}


/** 对源码树按相对路径和原始字节排序哈希，链接或空集合均失败关闭。 */
async function sourceTreeDigest(directory, accept, stage) {
  const root = path.resolve(directory);
  const files = [];
  async function visit(current) {
    for (const entry of await readdir(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name);
      const information = await lstat(target);
      requireCondition(!information.isSymbolicLink(), stage, "link");
      if (information.isDirectory()) {
        await visit(target);
      } else if (information.isFile() && accept(target)) {
        const relative = path.relative(root, target).split(path.sep).join("/");
        requireCondition(relative && !relative.startsWith("../"), stage, "outside_root");
        files.push(Object.freeze({ relative, target }));
      } else if (!information.isFile()) {
        throw new BrowserAcceptanceFailure(stage, "unexpected_type");
      }
    }
  }
  await visit(root);
  files.sort((left, right) => left.relative.localeCompare(right.relative, "en"));
  requireCondition(files.length > 0, stage, "empty");
  const digest = createHash("sha256");
  for (const file of files) {
    digest.update(file.relative, "utf8");
    digest.update(Buffer.from([0]));
    digest.update(await readFile(file.target));
    digest.update(Buffer.from([0]));
  }
  return digest.digest("hex");
}


/** 冻结 Browser/Harness、PAGE-107 前端边界及完整迁移集合的源码来源。 */
async function captureSourceProvenance() {
  const values = {
    browser_driver: await sourceFileDigest(fileURLToPath(import.meta.url)),
    browser_harness: await sourceFileDigest(HARNESS),
    browser_helper_prd056: await sourceFileDigest(
      path.join(SCRIPT_DIRECTORY, "prd056-browser-acceptance.mjs"),
    ),
    browser_helper_prd066: await sourceFileDigest(
      path.join(SCRIPT_DIRECTORY, "prd066-browser-acceptance.mjs"),
    ),
    console_dev_server: await sourceFileDigest(
      path.join(ROOT, "console-web", "scripts", "dev-server.mjs"),
    ),
    console_entry: await sourceFileDigest(path.join(ROOT, "console-web", "index.html")),
    control_plane_source_tree: await sourceTreeDigest(
      CONTROL_SOURCE,
      (filename) => filename.endsWith(".py"),
      "provenance.control_plane_tree",
    ),
    frontend_source_tree: await sourceTreeDigest(
      FRONTEND_SOURCE,
      (filename) => /\.(?:css|js|json)$/i.test(filename),
      "provenance.frontend_tree",
    ),
    gateway_source_tree: await sourceTreeDigest(
      GATEWAY_SOURCE,
      (filename) => filename.endsWith(".py"),
      "provenance.gateway_tree",
    ),
    migration_0070_down: await sourceFileDigest(
      path.join(MIGRATIONS, "0070_read_only_admin_ai_assistant.down.sql"),
    ),
    migration_0070_up: await sourceFileDigest(
      path.join(MIGRATIONS, "0070_read_only_admin_ai_assistant.sql"),
    ),
    migration_set: await sourceTreeDigest(
      MIGRATIONS,
      (filename) => filename.endsWith(".sql"),
      "provenance.migrations",
    ),
    runtime_acceptance: await sourceFileDigest(
      path.join(SCRIPT_DIRECTORY, "prd077-runtime-acceptance.py"),
    ),
    runtime_helper_prd075: await sourceFileDigest(
      path.join(SCRIPT_DIRECTORY, "prd075-runtime-acceptance.py"),
    ),
    shared_sdk_source_tree: await sourceTreeDigest(
      SHARED_SOURCE,
      (filename) => filename.endsWith(".py"),
      "provenance.shared_sdk_tree",
    ),
  };
  assertSourceProvenance(values);
  return Object.freeze(values);
}


/** provenance 必须是 exact key 集及规范小写 SHA-256，禁止缺项或扩展。 */
function assertSourceProvenance(value, stage = "evidence.provenance") {
  requireCondition(
    value && typeof value === "object" && !Array.isArray(value),
    stage,
    "shape",
  );
  requireCondition(
    JSON.stringify(Object.keys(value).sort())
      === JSON.stringify([...SOURCE_PROVENANCE_NAMES].sort()),
    stage,
    "keys",
  );
  requireCondition(
    Object.values(value).every((digest) => LOWER_SHA256.test(String(digest))),
    stage,
    "digest",
  );
  return true;
}


/** 运行前后 provenance 必须逐键相同；任何源码漂移都拒绝发布。 */
function assertSourceProvenanceUnchanged(expected, actual) {
  assertSourceProvenance(expected);
  assertSourceProvenance(actual);
  requireCondition(
    SOURCE_PROVENANCE_NAMES.every((name) => expected[name] === actual[name]),
    "evidence.provenance",
    "changed",
  );
  return true;
}


/**
 * 把已证明 owner 原子移入同根随机 quarantine 后再递归删除。
 *
 * rename 成功后绝不再读取 source 路径：即使其它进程立即创建同名目录并复制 claim，本函数的
 * 所有递归操作也只指向不可预测的私有 quarantine leaf。每次重试仍复核 root、quarantine、
 * leaf identity、唯一 inventory 与 exact claim，避免已隔离目标本身在重试期间被替换。
 */
async function removeOwnedDirectory(
  directory,
  root,
  stage,
  {
    deleteOperation = deleteVerifiedDirectoryTreeAfterRelease,
    moveOperation = moveVerifiedDirectory,
    afterMoveLocked = async () => undefined,
    afterDeleteLocked = async () => undefined,
    mkdirOperation = mkdir,
    expectedIdentity,
    ownershipClaim = null,
    claimDirectory = directory,
    claimIdentity = expectedIdentity,
  } = {},
) {
  if (!directory || !(await pathEntryExists(directory, stage))) return;
  const resolvedRoot = path.resolve(root);
  const resolvedDirectory = path.resolve(directory);
  const relative = path.relative(resolvedRoot, resolvedDirectory);
  requireCondition(
    relative && !path.isAbsolute(relative) && !relative.includes(path.sep),
    stage,
    "outside_root",
  );
  const rootIdentity = await captureDirectoryIdentity(resolvedRoot, `${stage}_root`);
  const claimMovesWithDirectory = path.resolve(claimDirectory) === resolvedDirectory;

  /** source 接管前仍需最后一次确认；rename 之后不再触碰该路径。 */
  await assertDirectoryIdentity(resolvedDirectory, expectedIdentity, stage);
  if (ownershipClaim) {
    await assertOwnershipClaim(
      claimDirectory,
      ownershipClaim,
      `${stage}_owner`,
      claimIdentity,
    );
  }

  let quarantineRoot = "";
  let quarantineRootIdentity = null;
  let quarantineLeaf = "";
  let primaryFailure = null;
  for (let attempt = 0; attempt < 8 && !quarantineRootIdentity; attempt += 1) {
    quarantineRoot = path.join(
      resolvedRoot,
      `.prd077-quarantine-${randomBytes(24).toString("hex")}`,
    );
    try {
      await assertDirectoryIdentity(resolvedRoot, rootIdentity, `${stage}_root`);
      await mkdirOperation(quarantineRoot, { recursive: false });
      quarantineRootIdentity = await captureDirectoryIdentity(
        quarantineRoot,
        `${stage}_quarantine`,
      );
    } catch (error) {
      // 随机隔离根只有明确 EEXIST 可重试，其他异常必须立即折叠为固定创建失败。
      if (error?.code !== "EEXIST") {
        // 已标准化身份错误原样保留，未知创建异常不能暴露主机路径或系统正文。
        if (error instanceof BrowserAcceptanceFailure) throw error;
        throw new BrowserAcceptanceFailure(stage, "quarantine_create");
      }
    }
  }
  requireCondition(quarantineRootIdentity, stage, "quarantine_create");
  quarantineLeaf = path.join(
    quarantineRoot,
    `owned-${randomBytes(24).toString("hex")}`,
  );

  /** quarantine 必须始终是本轮创建的唯一 leaf；任何额外目录项都使清理失败关闭。 */
  const assertQuarantineOwner = async () => {
    await assertDirectoryIdentity(resolvedRoot, rootIdentity, `${stage}_root`);
    await assertDirectoryIdentity(
      quarantineRoot,
      quarantineRootIdentity,
      `${stage}_quarantine`,
    );
    const inventory = await readdir(quarantineRoot);
    requireCondition(
      inventory.length === 1 && inventory[0] === path.basename(quarantineLeaf),
      stage,
      "quarantine_inventory",
    );
    await assertDirectoryIdentity(quarantineLeaf, expectedIdentity, stage);
    if (ownershipClaim) {
      await assertOwnershipClaim(
        claimMovesWithDirectory ? quarantineLeaf : claimDirectory,
        ownershipClaim,
        `${stage}_owner`,
        claimMovesWithDirectory ? expectedIdentity : claimIdentity,
      );
    }
  };

  try {
    await assertDirectoryIdentity(resolvedRoot, rootIdentity, `${stage}_root`);
    await assertDirectoryIdentity(
      quarantineRoot,
      quarantineRootIdentity,
      `${stage}_quarantine`,
    );
    requireCondition(
      (await readdir(quarantineRoot)).length === 0,
      stage,
      "quarantine_inventory",
    );
    // helper 持有已按 File ID 校验的 source 与 quarantine parent 句柄；路径替换不会被带走。
    try {
      await moveOperation(
        resolvedDirectory,
        quarantineLeaf,
        expectedIdentity,
        stage,
        {
          destinationParentIdentity: quarantineRootIdentity,
          afterLocked: afterMoveLocked,
        },
      );
    } catch {
      throw new BrowserAcceptanceFailure(stage, "quarantine_rename");
    }
    await assertQuarantineOwner();
    // 最终递归删除由 helper 从 exact root handle 出发；禁止重新解析 quarantineLeaf 后调用 rm。
    await deleteOperation(quarantineLeaf, expectedIdentity, stage, {
      afterLocked: afterDeleteLocked,
      verifyOwner: assertQuarantineOwner,
    });
    requireCondition(
      !(await pathEntryExists(quarantineLeaf, stage)),
      stage,
      "remove_failed",
    );
  } catch (error) {
    primaryFailure = error instanceof BrowserAcceptanceFailure
      ? error
      : new BrowserAcceptanceFailure(stage, "remove_failed");
    throw primaryFailure;
  } finally {
    // quarantine root 同样按 exact handle 删除；绝不在校验后重新解析公开路径调用 rmdir。
    try {
      if (
        quarantineRootIdentity
        && await directoryIdentityMatches(quarantineRoot, quarantineRootIdentity)
        && (await readdir(quarantineRoot)).length === 0
      ) {
        await assertDirectoryIdentity(resolvedRoot, rootIdentity, `${stage}_root`);
        await deleteVerifiedDirectoryTree(
          quarantineRoot,
          quarantineRootIdentity,
          `${stage}_quarantine`,
        );
      }
    } catch {
      if (!primaryFailure) {
        throw new BrowserAcceptanceFailure(stage, "quarantine_remove");
      }
    }
  }
}


/** 删除 harness work 内固定 browser 子目录，必须在 harness 最终隐私扫描前完成。 */
async function removeBrowserDirectory(
  browserDirectory,
  workDirectory,
  {
    ownershipClaim = null,
    workIdentity,
    browserIdentity,
  } = {},
) {
  if (!(await pathEntryExists(browserDirectory, "cleanup.browser"))) return;
  requireCondition(
    path.relative(workDirectory, browserDirectory) === "browser",
    "cleanup.browser",
    "outside_work",
  );
  if (ownershipClaim) {
    await assertOwnershipClaim(
      workDirectory,
      ownershipClaim,
      "cleanup.browser_owner",
      workIdentity,
    );
  }
  await removeOwnedDirectory(browserDirectory, workDirectory, "cleanup.browser", {
    expectedIdentity: browserIdentity,
    ownershipClaim,
    claimDirectory: workDirectory,
    claimIdentity: workIdentity,
  });
}


/** 返回生成配置、动态 Browser Token 与四类回答叶值的当前只读扫描集合。 */
function trackerPrivateValues(tracker) {
  return [
    ...tracker.privateValues,
    ...tracker.browserTokens,
    ...(tracker.answerLeafValues ?? []),
    ...(tracker.answerSerializations ?? []),
  ];
}


/** 在内存中保留有界近期输出，使回答叶值在 DOM 出现后仍能回溯检查更早日志。 */
function rememberInspectionText(tracker, channel, value) {
  if (!(tracker.recentInspectionText instanceof Map)) tracker.recentInspectionText = new Map();
  const previous = tracker.recentInspectionText.get(channel) ?? "";
  tracker.recentInspectionText.set(
    channel,
    `${previous}${String(value ?? "")}`.slice(-RECENT_INSPECTION_TEXT_LIMIT),
  );
}


/** 注册单类真实回答的长叶值，并立即回溯扫描已观察到的进程与 Console 输出。 */
function registerAnswerLeafValues(tracker, queryName, values) {
  requireCondition(
    Object.hasOwn(QUERY_CASES, queryName),
    "browser.answer_leaf",
    "query_type",
  );
  requireCondition(
    Array.isArray(values) && values.length > 0 && values.length <= ANSWER_LEAF_INPUT_MAX_COUNT,
    "browser.answer_leaf",
    "shape",
  );
  if (!(tracker.answerLeafValues instanceof Set)) tracker.answerLeafValues = new Set();
  if (!(tracker.answerLeavesByQuery instanceof Map)) tracker.answerLeavesByQuery = new Map();
  const current = new Set();
  for (const rawValue of values) {
    requireCondition(typeof rawValue === "string", "browser.answer_leaf", "type");
    const value = rawValue.trim();
    requireCondition(value.length <= ANSWER_LEAF_MAX_LENGTH, "browser.answer_leaf", "length");
    if (value.length >= ANSWER_LEAF_MIN_LENGTH) current.add(value);
  }
  requireCondition(current.size > 0, "browser.answer_leaf", "missing");
  const registered = tracker.answerLeavesByQuery.get(queryName) ?? new Set();
  const recentText = [...(tracker.recentInspectionText?.values?.() ?? [])];
  for (const value of current) {
    registered.add(value);
    if (!tracker.answerLeafValues.has(value)) {
      if (recentText.some((text) => text.includes(value))) {
        tracker.processPrivacyViolationCount += 1;
      }
      tracker.answerLeafValues.add(value);
    }
  }
  requireCondition(
    tracker.answerLeafValues.size <= ANSWER_LEAF_MAX_COUNT,
    "browser.answer_leaf",
    "count",
  );
  tracker.answerLeavesByQuery.set(queryName, registered);
  return Object.freeze([...current]);
}


/** 按固定资源上限复制并排序 JSON 图，避免规范化时被超深或超宽响应拖垮。 */
function boundedSortedJson(value, state = { nodes: 0 }, depth = 0) {
  state.nodes += 1;
  requireCondition(
    state.nodes <= ASSISTANT_RESPONSE_MAX_NODES,
    "browser.answer_response",
    "nodes",
  );
  requireCondition(
    depth <= ASSISTANT_RESPONSE_MAX_DEPTH,
    "browser.answer_response",
    "depth",
  );
  if (value === null || ["string", "boolean"].includes(typeof value)) return value;
  if (typeof value === "number") {
    requireCondition(Number.isFinite(value), "browser.answer_response", "number");
    return value;
  }
  if (Array.isArray(value)) {
    requireCondition(
      value.length <= ASSISTANT_RESPONSE_MAX_ARRAY_ITEMS,
      "browser.answer_response",
      "array",
    );
    return value.map((item) => boundedSortedJson(item, state, depth + 1));
  }
  requireCondition(
    typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype,
    "browser.answer_response",
    "object",
  );
  const keys = Object.keys(value).sort();
  requireCondition(
    keys.length <= ASSISTANT_RESPONSE_MAX_OBJECT_KEYS,
    "browser.answer_response",
    "object_keys",
  );
  return Object.fromEntries(
    keys.map((key) => [key, boundedSortedJson(value[key], state, depth + 1)]),
  );
}


/** 将 JSON 文本中的非 ASCII UTF-16 code unit 转为 Python ensure_ascii 同形转义。 */
function ensureAsciiJson(text) {
  return text.replace(
    /[^\x00-\x7f]/g,
    (value) => `\\u${value.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}


/** 生成 Python json.dumps 默认分隔符同形文本；对象键已经在上一阶段排序。 */
function pythonDefaultJson(value) {
  if (Array.isArray(value)) return `[${value.map(pythonDefaultJson).join(", ")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value).map(([key, item]) => (
      `${ensureAsciiJson(JSON.stringify(key))}: ${pythonDefaultJson(item)}`
    )).join(", ")}}`;
  }
  return ensureAsciiJson(JSON.stringify(value));
}


/** 从一次真实 200 CDP 响应构造 raw 与跨 Python/JS 的完整响应扫描串。 */
function assistantResponseSerializations(responseBody) {
  requireCondition(
    responseBody?.base64Encoded === false && typeof responseBody.body === "string",
    "browser.answer_response",
    "encoding",
  );
  const raw = responseBody.body.trim();
  requireCondition(
    raw.length > 0 && Buffer.byteLength(raw, "utf8") <= ASSISTANT_RESPONSE_MAX_BYTES,
    "browser.answer_response",
    "size",
  );
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new BrowserAcceptanceFailure("browser.answer_response", "json");
  }
  const sorted = boundedSortedJson(payload);
  requireCondition(
    JSON.stringify(Object.keys(sorted)) === JSON.stringify(ASSISTANT_RESPONSE_KEYS)
      && typeof sorted.answer === "object"
      && sorted.answer !== null
      && !Array.isArray(sorted.answer)
      && Array.isArray(sorted.sources)
      && Array.isArray(sorted.redactions)
      && Object.hasOwn(EXPECTED_RESPONSE_QUERY_COUNTS, sorted.query_type),
    "browser.answer_response",
    "schema",
  );
  const compact = JSON.stringify(sorted);
  const serializations = Object.freeze([
    ...new Set([
      raw,
      compact,
      ensureAsciiJson(compact),
      pythonDefaultJson(sorted),
    ]),
  ]);
  requireCondition(
    serializations.length > 0
      && serializations.every((value) => Buffer.byteLength(value, "utf8") <= ASSISTANT_RESPONSE_MAX_BYTES),
    "browser.answer_response",
    "serialization",
  );
  return Object.freeze({ queryType: sorted.query_type, serializations });
}


/** 登记完整响应，并立即回溯检查在 CDP body 可读前已经观察到的进程与 Console 输出。 */
function registerAssistantResponseBody(tracker, responseBody) {
  const { queryType, serializations } = assistantResponseSerializations(responseBody);
  if (!(tracker.answerSerializations instanceof Set)) tracker.answerSerializations = new Set();
  if (!(tracker.answerResponsesByQuery instanceof Map)) tracker.answerResponsesByQuery = new Map();
  const recentText = [...(tracker.recentInspectionText?.values?.() ?? [])];
  for (const serialization of serializations) {
    if (!tracker.answerSerializations.has(serialization)) {
      if (recentText.some((text) => text.includes(serialization))) {
        tracker.processPrivacyViolationCount += 1;
      }
      tracker.answerSerializations.add(serialization);
    }
  }
  tracker.answerResponseCount = Number(tracker.answerResponseCount ?? 0) + 1;
  tracker.answerResponsesByQuery.set(
    queryType,
    Number(tracker.answerResponsesByQuery.get(queryType) ?? 0) + 1,
  );
  return Object.freeze({
    query_type: queryType,
    serialization_count: serializations.length,
  });
}


/** 读取一次成功 assistant body；任何 CDP 失败只转换为固定坐标。 */
async function inspectAssistantResponseBody(client, tracker, requestId) {
  let responseBody;
  try {
    responseBody = await client.send("Network.getResponseBody", { requestId });
  } catch {
    throw new BrowserAcceptanceFailure("browser.answer_response", "read");
  }
  return registerAssistantResponseBody(tracker, responseBody);
}


/** 跨 chunk 扫描进程输出，只累计命中数，绝不保存或打印原始输出块。 */
function inspectProcessChunk(chunk, tracker, channel = "process") {
  const privateValues = trackerPrivateValues(tracker);
  const previous = tracker.processTails.get(channel) ?? "";
  const text = `${previous}${String(chunk ?? "")}`;
  rememberInspectionText(tracker, `process.${channel}`, chunk);
  if (privateValues.some((value) => value && text.includes(value))) {
    tracker.processPrivacyViolationCount += 1;
  }
  const tailLength = Math.max(0, ...privateValues.map((value) => value.length - 1));
  tracker.processTails.set(channel, tailLength > 0 ? text.slice(-tailLength) : "");
}


/** 只接受 harness 自身固定 schema 的失败坐标；任意额外字段或动态正文都拒绝传播。 */
function parseHarnessFailureLine(line) {
  let payload;
  try { payload = JSON.parse(String(line)); } catch { return null; }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  if (
    JSON.stringify(Object.keys(payload).sort()) !== JSON.stringify(["code", "stage", "status"])
    || payload.status !== "failed"
    || !SAFE_COORDINATE.test(String(payload.stage ?? ""))
    || !SAFE_COORDINATE.test(String(payload.code ?? ""))
  ) return null;
  return new BrowserAcceptanceFailure(payload.stage, payload.code);
}


/** 从 spawn 当刻观察 close；Node 的 close 事件同时证明进程退出与其 stdio 已到 EOF。 */
function observeChildDrain(child) {
  requireCondition(
    child && typeof child.once === "function",
    "cleanup.process_tree",
    "child",
  );
  const existing = CHILD_DRAIN_STATES.get(child);
  if (existing) return existing;
  let resolveClose;
  const state = {
    closeObserved: false,
    closeCode: null,
    promise: new Promise((resolve) => { resolveClose = resolve; }),
  };
  CHILD_DRAIN_STATES.set(child, state);
  child.once("close", (code) => {
    state.closeObserved = true;
    state.closeCode = code;
    resolveClose(code);
  });
  // 极短命子进程可能在观察前完成；只有 exitCode 与可读流 EOF 同时成立才可补证 close。
  const streams = [child.stdout, child.stderr].filter(Boolean);
  if (
    child.exitCode !== null
    && streams.every((stream) => stream.readableEnded === true || stream.closed === true)
  ) {
    state.closeObserved = true;
    state.closeCode = child.exitCode;
    resolveClose(child.exitCode);
  }
  return state;
}


/** 有界等待 child close/stdio EOF；只看到 exit 不足以允许 Profile 或 run 目录清理。 */
async function waitForChildDrain(child, timeoutMs = 10000) {
  const state = observeChildDrain(child);
  if (state.closeObserved) return state.closeCode;
  let timer;
  try {
    return await Promise.race([
      state.promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          reject(new BrowserAcceptanceFailure("cleanup.process_tree", "close_timeout"));
        }, timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}


/** 启动真实 Python harness，并只解析其固定 ready/stopped/failed JSON 控制行。 */
function startHarness(runId, privateConfig, ownershipClaim, tracker) {
  const child = spawn(PYTHON, [HARNESS, "--run-id", runId], {
    cwd: ROOT,
    env: harnessEnvironment(privateConfig, ownershipClaim),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  let buffer = "";
  let stderrBuffer = "";
  let reportedFailure = null;
  let settled = false;
  const drainState = observeChildDrain(child);
  const closed = drainState.promise;
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new BrowserAcceptanceFailure("harness.ready", "timeout"));
      }
    }, 120000);
    child.stdout.on("data", (chunk) => {
      inspectProcessChunk(chunk, tracker, "harness_stdout");
      buffer = `${buffer}${String(chunk)}`.slice(-64 * 1024);
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        let payload;
        try { payload = JSON.parse(line); } catch { payload = null; }
        if (!settled && payload?.status === "ready") {
          settled = true;
          clearTimeout(timer);
          resolve(payload);
        }
      }
    });
    child.stderr.on("data", (chunk) => {
      inspectProcessChunk(chunk, tracker, "harness_stderr");
      stderrBuffer = `${stderrBuffer}${String(chunk)}`.slice(-8 * 1024);
      const lines = stderrBuffer.split(/\r?\n/);
      stderrBuffer = lines.pop() ?? "";
      for (const line of lines) {
        const failure = parseHarnessFailureLine(line);
        if (failure) reportedFailure = failure;
      }
    });
    child.once("error", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new BrowserAcceptanceFailure("harness.start", "failed"));
      }
    });
    child.once("close", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(
          reportedFailure
          ?? parseHarnessFailureLine(stderrBuffer)
          ?? new BrowserAcceptanceFailure("harness.ready", "exited"),
        );
      }
    });
  });
  return Object.freeze({
    child,
    closed,
    ready,
    failure: () => reportedFailure ?? parseHarnessFailureLine(stderrBuffer),
  });
}


/** 有界等待 harness 退出并返回 exit code；不读取或回显 stderr。 */
async function waitForHarnessExit(child, timeoutMs = 60000) {
  if (child.exitCode !== null) return child.exitCode;
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer = null;
    const finish = (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener("exit", onExit);
      resolve(code);
    };
    /** 将子进程退出事件归并到单次完成回调，避免监听竞态重复结算。 */
    const onExit = (code) => finish(code);
    child.once("exit", onExit);
    timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.removeListener("exit", onExit);
      reject(new BrowserAcceptanceFailure("harness.exit", "timeout"));
    }, timeoutMs);
    if (child.exitCode !== null) finish(child.exitCode);
  });
}


/** 等待 harness 进程和全部 stdio 关闭，确保尾部隐私扫描与固定失败行已消费。 */
async function waitForHarnessClose(harness, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new BrowserAcceptanceFailure("harness.close", "timeout"));
    }, timeoutMs);
    harness.closed.then((code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(code);
    });
  });
}


/** 显式创建 off-the-record Context，禁止 CDP 新建页面落入默认持久 Profile。 */
async function createPrivatePageSession(
  browserClient,
  debugPort,
  pageSessionFactory = createPageSession,
) {
  const { browserContextId } = await browserClient.send(
    "Target.createBrowserContext",
    { disposeOnDetach: true },
  );
  requireCondition(
    typeof browserContextId === "string" && browserContextId.length > 0,
    "browser.context",
    "missing",
  );
  try {
    const session = await pageSessionFactory(browserClient, debugPort, browserContextId);
    requireCondition(
      session?.browserContextId === browserContextId,
      "browser.context",
      "target_mismatch",
    );
    // 仅返回与一次性 Context 精确绑定的页面会话，避免回落到持久 Profile。
    return session;
  } catch (error) {
    try {
      await browserClient.send("Target.disposeBrowserContext", { browserContextId });
    } catch {
      // 原始固定失败坐标优先；Browser.close/进程树回收仍会销毁该一次性 Context。
    }
    throw error;
  }
}


/** 正常收口私有 Page target 和 Context；任一协议步骤失败均交由外层强制回收。 */
async function disposePrivatePageSession(browserClient, pageSession) {
  requireCondition(
    typeof pageSession?.targetId === "string" && pageSession.targetId.length > 0,
    "browser.context.dispose",
    "target_missing",
  );
  requireCondition(
    typeof pageSession?.browserContextId === "string"
      && pageSession.browserContextId.length > 0,
    "browser.context.dispose",
    "context_missing",
  );
  pageSession.client?.close();
  const closed = await browserClient.send(
    "Target.closeTarget",
    { targetId: pageSession.targetId },
  );
  requireCondition(closed?.success === true, "browser.context.dispose", "target_close");
  await browserClient.send(
    "Target.disposeBrowserContext",
    { browserContextId: pageSession.browserContextId },
  );
  return true;
}


/** 只等待 Chrome 自然退出，不在超时分支内提前杀父进程。 */
async function waitForBrowserExit(child, timeoutMs = 10000) {
  if (child.exitCode !== null) return true;
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const finish = (exited) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener("exit", onExit);
      resolve(exited);
    };
    /** 将浏览器退出事件归并到有界等待结果，防止退出与计时器重复完成。 */
    const onExit = () => finish(true);
    child.once("exit", onExit);
    timer = setTimeout(() => finish(false), timeoutMs);
    if (child.exitCode !== null) finish(true);
  });
}


/** 优先 Browser.close 正常退出；有界等待超时后才精确强杀本驱动创建的进程树。 */
async function closeBrowserGracefully(
  browserClient,
  child,
  {
    waitOperation = waitForBrowserExit,
    terminateOperation = terminateBrowserTree,
    closeWaitOperation = waitForChildDrain,
  } = {},
) {
  observeChildDrain(child);
  try {
    await browserClient.send("Browser.close", {}, undefined, 5000);
  } catch {
    // Chrome 常在 Browser.close 响应送达前关闭 WebSocket；以实际进程退出为准。
  }
  browserClient.close();
  if (!(await waitOperation(child, 10000))) {
    await terminateOperation(child);
    await closeWaitOperation(child, 10000);
    return Object.freeze({ forced: true, treeDrained: true, stdioEof: true });
  }
  // exit 只说明浏览器父进程结束；close 才覆盖 renderer 继承的 stdout/stderr 句柄。
  await closeWaitOperation(child, 10000);
  child.unref();
  return Object.freeze({ forced: false, treeDrained: true, stdioEof: true });
}


/**
 * 终止本驱动创建的隔离 Chrome 子树。Windows renderer 会继承 stdio 句柄，仅结束父进程会让
 * Node 永久等待并锁住一次性 Profile，因此必须使用 launchEdge 返回的精确父 PID 回收子树。
 */
async function terminateWindowsProcessTree(
  pid,
  { spawnOperation = spawn, timeoutMs = 10000 } = {},
) {
  requireCondition(
    Number.isInteger(pid) && pid > 0,
    "cleanup.process_tree",
    "pid",
  );
  await new Promise((resolve, reject) => {
    let killer;
    try {
      killer = spawnOperation("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
    } catch {
      reject(new BrowserAcceptanceFailure("cleanup.process_tree", "spawn"));
      return;
    }
    let settled = false;
    /** 统一结束进程树回收等待，并确保计时器与 Promise 只结算一次。 */
    const finish = (error = null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      // 回收工具错误必须拒绝完成，只有零错误结果才能确认进程树已处理。
      if (error) reject(error); else resolve();
    };
    const timer = setTimeout(() => {
      try { if (killer.exitCode === null) killer.kill(); } catch {}
      finish(new BrowserAcceptanceFailure("cleanup.process_tree", "timeout"));
    }, timeoutMs);
    killer.once("error", () => {
      finish(new BrowserAcceptanceFailure("cleanup.process_tree", "spawn"));
    });
    killer.once("exit", (code) => {
      finish(
        code === 0
          ? null
          : new BrowserAcceptanceFailure("cleanup.process_tree", "exit"),
      );
    });
  });
}


/** 回收驱动拥有的浏览器进程树，并保留最早发生的安全失败坐标。 */
async function terminateBrowserTree(child, treeOptions = {}) {
  if (!child) return;
  observeChildDrain(child);
  const pid = Number(child.pid);
  let treeFailure = null;
  if (child.exitCode === null && process.platform === "win32") {
    try {
      await terminateWindowsProcessTree(pid, treeOptions);
    } catch (error) {
      treeFailure = error;
      try { if (child.exitCode === null) child.kill(); } catch {}
    }
  } else if (child.exitCode === null) {
    child.kill("SIGKILL");
  }
  try {
    await waitForChildDrain(child, 5000);
  } catch (error) {
    // 若进程树强制回收已失败则保留该根因，否则传播 close/stdio EOF 的有界失败。
    if (!treeFailure) throw error;
  }
  child.unref();
  if (treeFailure) throw treeFailure;
  return Object.freeze({ treeDrained: true, stdioEof: true });
}


/** Harness 同样拥有 Gateway/Control/Console 子进程，异常回收必须结束精确父 PID 的整棵树。 */
async function terminateHarnessTree(child) {
  await terminateBrowserTree(child);
}


/** 请求 harness 执行真实 finance 撤权；响应正文只能是固定短状态。 */
async function revokeFinancePermission(
  { fetchOperation = fetch, timeoutMs = HARNESS_CONTROL_TIMEOUT_MS } = {},
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  let payload;
  try {
    try {
      response = await fetchOperation(`${HARNESS_ORIGIN}/revoke`, {
        method: "POST",
        signal: controller.signal,
      });
    } catch {
      throw new BrowserAcceptanceFailure(
        "harness.revoke",
        controller.signal.aborted ? "timeout" : "network",
      );
    }
    requireCondition(response.status === 200, "harness.revoke", "status");
    try {
      // 同一个 deadline 必须覆盖 headers 和响应体；只限制 fetch 连接阶段会让损坏 harness 永久挂起。
      payload = await response.json();
    } catch {
      throw new BrowserAcceptanceFailure(
        "harness.revoke",
        controller.signal.aborted ? "timeout" : "contract",
      );
    }
  } finally {
    clearTimeout(timer);
  }
  requireCondition(payload?.status === "revoked", "harness.revoke", "contract");
}


/** 先停止本轮服务 owner，使 work 隐私扫描面对稳定文件树；控制 server 保持存活。 */
async function quiesceHarness(
  { fetchOperation = fetch, timeoutMs = HARNESS_QUIESCE_TIMEOUT_MS } = {},
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  let payload;
  try {
    try {
      response = await fetchOperation(`${HARNESS_ORIGIN}/quiesce`, {
        method: "POST",
        signal: controller.signal,
      });
    } catch {
      throw new BrowserAcceptanceFailure(
        "harness.quiesce",
        controller.signal.aborted ? "timeout" : "network",
      );
    }
    requireCondition(response.status === 200, "harness.quiesce", "status");
    try {
      payload = await response.json();
    } catch {
      throw new BrowserAcceptanceFailure(
        "harness.quiesce",
        controller.signal.aborted ? "timeout" : "contract",
      );
    }
  } finally {
    clearTimeout(timer);
  }
  requireCondition(payload?.status === "quiesced", "harness.quiesce", "contract");
}


/** 请求 harness 停服和执行最终门禁；成功路径必须以 exit 0 结束。 */
async function stopHarness(
  harness,
  {
    requireSuccess,
    fetchOperation = fetch,
    waitOperation = waitForHarnessClose,
    timeoutMs = HARNESS_CONTROL_TIMEOUT_MS,
  },
) {
  const { child } = harness;
  let response = null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    response = await fetchOperation(`${HARNESS_ORIGIN}/stop`, {
      method: "POST",
      signal: controller.signal,
    });
  } catch {
    // 未 ready 或已经退出的失败路径交给精确 PID 和 owner 目录回收。
  } finally {
    clearTimeout(timer);
  }
  if (requireSuccess) {
    requireCondition(response?.status === 200, "harness.stop", "status");
  }
  if (!response && child.exitCode === null) {
    throw new BrowserAcceptanceFailure("harness.stop", "network");
  }
  let exitCode;
  try {
    exitCode = await waitOperation(harness);
  } catch (error) {
    // Harness 退出超时已经是脱敏固定坐标，必须原样交给外层失败收口。
    throw error;
  }
  if (requireSuccess && exitCode !== 0) {
    const failure = harness.failure?.();
    if (failure) throw failure;
    throw new BrowserAcceptanceFailure("harness.stop", "exit");
  }
  return exitCode;
}


/** 构造只保存状态码和计数的浏览器追踪器，不保存 Header、URL、正文或身份。 */
function createTracker(privateValues) {
  return {
    // 既有 launchEdge helper 使用 secrets 字段检查进程输出；两者引用同一只读内存数组。
    secrets: privateValues,
    privateValues,
    processPrivacyViolationCount: 0,
    edgeProcessErrorCount: 0,
    externalRequestCount: 0,
    forbiddenQueryCount: 0,
    requestPrivacyViolationCount: 0,
    unexpectedConsoleErrorCount: 0,
    consoleNetworkFailureCount: 0,
    unexpectedConsoleFailure: null,
    expectedConsoleErrorCount: 0,
    exceptionCount: 0,
    unexpectedNetworkFailureCount: 0,
    unexpectedNetworkFailure: null,
    networkActivityRevision: 0,
    canceledAssistantRequestCount: 0,
    assistantHeaderFailureCount: 0,
    assistantStatuses: [],
    loginStatuses: [],
    logoutStatuses: [],
    navigationStatuses: [],
    healthProbeFailureCount: 0,
    healthProbeRecoveryCount: 0,
    pendingHealthRecoveries: new Map(),
    earlyHealthRecoveries: new Map(),
    requestOwners: new Map(),
    expectedCanceledRequestIds: new Set(),
    browserTokens: new Set(),
    answerLeafValues: new Set(),
    answerLeavesByQuery: new Map(),
    answerSerializations: new Set(),
    answerResponseCount: 0,
    answerResponsesByQuery: new Map(),
    pendingAnswerInspections: new Set(),
    answerInspectionFailureCount: 0,
    processTails: new Map(),
    recentInspectionText: new Map(),
    pendingConsoleInspections: new Set(),
    consoleInspectionFailureCount: 0,
    inspectionActivityRevision: 0,
    inspectionsSealed: false,
    lateInspectionFailureCount: 0,
    phase: "setup",
    delayNextAssistant: false,
    delayedRequestId: "",
    delayedLoadingFailureObserved: false,
  };
}


/** 网络事件每次进入归因账本都递增 revision，覆盖在轮询间隙内发生并结束的短请求。 */
function recordNetworkActivity(tracker) {
  tracker.networkActivityRevision = Number(tracker.networkActivityRevision ?? 0) + 1;
}


/** inspection 注册和拒绝都进入独立 revision，供 sealed 稳定窗口识别迟到事件。 */
function recordInspectionActivity(tracker) {
  tracker.inspectionActivityRevision = Number(tracker.inspectionActivityRevision ?? 0) + 1;
}


/** 将页面 URL 映射为固定分类；返回值不含 query、身份或动态资源名。 */
function classifyPageRequest(value) {
  let target;
  try { target = new URL(String(value)); } catch { return "invalid"; }
  if (["about:", "data:", "blob:"].includes(target.protocol)) return "browser_internal";
  if (!ALLOWED_PAGE_ORIGINS.has(target.origin)) return "external";
  // 认证端点分开计数，确保登录、会话读取和登出都能独立核对网络账本。
  if (target.pathname === "/auth/login") return "auth_login";
  if (target.pathname === "/auth/me") return "auth_me";
  if (target.pathname === "/auth/logout") return "auth_logout";
  if (target.pathname === "/console/navigation") return "navigation";
  if (target.pathname === "/console/preferences") return "preferences";
  if (target.pathname === "/internal/config/summary") return "config_summary";
  // 租户权限与角色概览端点按业务域归类，禁止将其混入普通控制面流量。
  if (target.pathname === "/permissions") return "permissions";
  if (target.pathname === "/dashboard/role-view") return "dashboard";
  if (target.pathname === "/tenant/current") return "tenant";
  if (target.pathname === "/users") return "users";
  if (target.pathname === "/environments") return "environments";
  // Provider 与模型配置端点保持独立类别，便于确认助手未越界读取敏感配置。
  if (target.pathname === "/providers") return "providers";
  if (target.pathname === "/models") return "models";
  if (target.pathname === "/model-prices") return "model_prices";
  // Alias 与助手请求各自独立归类，来源导航不能伪装成助手查询流量。
  if (target.pathname === "/model-aliases") return "model_aliases";
  if (target.pathname === "/admin-assistant/query") return "assistant";
  const healthRoute = {
    "/health": "health_summary",
    "/health/live": "health_live",
    "/health/ready": "health_ready",
    "/version": "health_version",
  }[target.pathname];
  if (target.origin.endsWith(":8000")) {
    return healthRoute ? `gateway_${healthRoute}` : "gateway";
  }
  if (target.origin.endsWith(":5173")) return "console";
  // 控制面健康端点保留细分类别，使一次可恢复失败能与精确 attempt 对账。
  if (healthRoute) return `control_${healthRoute}`;
  const firstSegment = target.pathname.split("/").filter(Boolean)[0] ?? "";
  const staticPrefix = firstSegment.split("-")[0];
  const family = CONTROL_ROUTE_FAMILIES[staticPrefix];
  return family ? `control_${family}` : "control";
}


/** 页面请求只能指向固定 Console/Control 或无网络的浏览器内部协议。 */
function browserRequestAllowed(value) {
  let target;
  try { target = new URL(String(value)); } catch { return false; }
  if (["about:", "data:", "blob:"].includes(target.protocol)) return true;
  return ALLOWED_PAGE_ORIGINS.has(target.origin)
    && !target.username
    && !target.password
    && ![...target.searchParams.keys()].some((key) => FORBIDDEN_QUERY_KEY.test(key));
}


/** 大小写无关读取 CDP Header 对象，不把值复制进持久证据。 */
function headerValue(headers, name) {
  const expected = String(name).toLowerCase();
  const key = Object.keys(headers ?? {}).find((candidate) => candidate.toLowerCase() === expected);
  return key ? String(headers[key]) : "";
}


/** 把健康探测 request_id 拆为同一逻辑调用和精确 attempt；其他 Header 一律不参与恢复。 */
function parseHealthAttemptRequestId(value) {
  const match = String(value ?? "").match(HEALTH_ATTEMPT_REQUEST_ID);
  return match
    ? Object.freeze({ logicalId: match[1], attempt: Number(match[2]) })
    : null;
}


/** 验证 PAGE-107 成功和错误响应共享 exact 私有 Header 与链路标识。 */
function privateAssistantHeadersValid(headers) {
  const cacheControl = headerValue(headers, "Cache-Control").trim().toLowerCase();
  const pragma = headerValue(headers, "Pragma").trim().toLowerCase();
  const vary = headerValue(headers, "Vary")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
  const unique = new Set(vary);
  return cacheControl === "no-store, private"
    && pragma === "no-cache"
    && Boolean(headerValue(headers, "X-Request-ID"))
    && Boolean(headerValue(headers, "X-Trace-ID"))
    && vary.length === unique.size
    && unique.has("authorization")
    && [...unique].every((item) => ["authorization", "origin"].includes(item));
}


/**
 * 归类 CDP 加载失败。预期取消必须按唯一 request id 登记，不能依赖 Chromium 不稳定的
 * canceled/errorText 字段，也不能吞掉其他 assistant 或本地服务加载失败。
 */
function recordNetworkLoadingFailure(tracker, event) {
  recordNetworkActivity(tracker);
  const owner = tracker.requestOwners.get(event.requestId);
  tracker.requestOwners.delete(event.requestId);
  const expectedCancellation = tracker.expectedCanceledRequestIds.delete(event.requestId);
  const errorText = String(event.errorText ?? "");
  const canceled = event.canceled === true
    || /(?:^|:)ERR_(?:ABORTED|FAILED)(?:$|\b)/i.test(errorText);
  if (
    owner?.category === "assistant"
    && owner.method === "POST"
    && expectedCancellation
    && canceled
  ) {
    tracker.canceledAssistantRequestCount += 1;
    tracker.delayedLoadingFailureObserved = true;
  } else if (
    owner?.method === "GET"
    && HEALTH_REQUEST_CATEGORIES.has(owner.category)
    && owner.healthAttempt?.attempt === 1
  ) {
    const key = owner.healthAttempt.logicalId;
    tracker.healthProbeFailureCount += 1;
    const early = tracker.earlyHealthRecoveries.get(key);
    if (early?.category === owner.category) {
      tracker.healthProbeRecoveryCount += 1;
      tracker.earlyHealthRecoveries.delete(key);
    } else {
      tracker.pendingHealthRecoveries.set(key, Object.freeze({
        category: owner.category,
        phase: owner.phase,
      }));
    }
  } else {
    tracker.unexpectedNetworkFailureCount += 1;
    if (!tracker.unexpectedNetworkFailure) {
      const allowedCategories = new Set([
        "assistant", "auth_login", "auth_logout", "auth_me", "browser_internal", "console",
        "control", "dashboard", "environments", "external", "gateway", "invalid",
        "model_aliases", "model_prices", "models", "navigation", "permissions",
        "config_summary", "preferences", "providers", "tenant", "unowned", "users",
        "control_health_live", "control_health_ready", "control_health_summary",
        "control_health_version", "gateway_health_live", "gateway_health_ready",
        "gateway_health_summary", "gateway_health_version",
        ...Object.values(CONTROL_ROUTE_FAMILIES).map((family) => `control_${family}`),
      ]);
      const category = allowedCategories.has(owner?.category) ? owner.category : "unowned";
      let reason = canceled ? "canceled" : "other";
      // 将常见连接故障折叠为有限类别，证据不得保存浏览器原始错误文本。
      if (/ERR_CONNECTION_REFUSED/i.test(errorText)) reason = "connection_refused";
      else if (/ERR_BLOCKED_BY_CLIENT/i.test(errorText)) reason = "blocked";
      else if (/ERR_NAME_NOT_RESOLVED/i.test(errorText)) reason = "dns";
      // 离线和通用失败继续使用固定枚举，避免 URL 或扩展详情进入失败证据。
      else if (/ERR_INTERNET_DISCONNECTED/i.test(errorText)) reason = "disconnected";
      else if (/ERR_FAILED/i.test(errorText)) reason = "failed";
      tracker.unexpectedNetworkFailure = Object.freeze({
        phase: String(owner?.phase || tracker.phase || "unknown"),
        category,
        reason,
      });
    }
  }
}


/** 记录响应状态；健康重试只有同一 logical owner 与端点随后 200 才能消费失败。 */
function recordNetworkResponse(tracker, event) {
  recordNetworkActivity(tracker);
  const owner = tracker.requestOwners.get(event.requestId);
  if (!owner) return;
  const status = Number(event.response?.status ?? 0);
  tracker.requestOwners.set(event.requestId, { ...owner, responseStatus: status });
  if (
    HEALTH_REQUEST_CATEGORIES.has(owner.category)
    && owner.method === "GET"
    && owner.healthAttempt?.attempt === 2
    && status === 200
  ) {
    const key = owner.healthAttempt.logicalId;
    const pending = tracker.pendingHealthRecoveries.get(key);
    if (pending?.category === owner.category) {
      tracker.healthProbeRecoveryCount += 1;
      tracker.pendingHealthRecoveries.delete(key);
    } else {
      // 不同 request 的 CDP 事件可交错；先到的 attempt_2 仅暂存精确逻辑 owner，不能抵消其它成功。
      tracker.earlyHealthRecoveries.set(key, Object.freeze({
        category: owner.category,
        phase: owner.phase,
      }));
    }
  }
  if (owner.category === "assistant" && owner.method === "POST") {
    tracker.assistantStatuses.push(status);
    if (!privateAssistantHeadersValid(event.response?.headers)) {
      tracker.assistantHeaderFailureCount += 1;
    }
  } else if (owner.category === "auth_login" && owner.method === "POST") {
    tracker.loginStatuses.push(status);
  } else if (owner.category === "auth_logout" && owner.method === "POST") {
    tracker.logoutStatuses.push(status);
  } else if (owner.category === "navigation" && owner.method === "GET") {
    tracker.navigationStatuses.push(status);
  }
}


/** Fetch 与 Network 域使用不同 request id；暂停时分别保存控制 id 与归因 id。 */
function registerDelayedAssistantRequest(tracker, event) {
  tracker.delayedRequestId = String(event.requestId ?? "");
  tracker.expectedCanceledRequestIds.add(String(event.networkId ?? event.requestId ?? ""));
}


/** 用同一绝对截止时间约束异步 CDP 读取，超时后不再扩展新的对象或正文检查。 */
async function awaitBeforeDeadline(promise, deadline, stage, code = "timeout") {
  const remaining = deadline - Date.now();
  requireCondition(remaining > 0, stage, code);
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new BrowserAcceptanceFailure(stage, code)),
          remaining,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}


/** sealed 后禁止再创建异步检查；迟到事件只登记固定失败，不执行未受控 CDP 工作。 */
function scheduleInspection(tracker, setName, failureName, operation) {
  recordInspectionActivity(tracker);
  if (tracker.inspectionsSealed === true) {
    tracker.lateInspectionFailureCount = Number(tracker.lateInspectionFailureCount ?? 0) + 1;
    return null;
  }
  const deadline = Date.now() + INSPECTION_TIMEOUT_MS;
  const task = awaitBeforeDeadline(
    Promise.resolve().then(() => operation(deadline)),
    deadline,
    failureName === "consoleInspectionFailureCount"
      ? "browser.console"
      : "browser.answer_response",
  ).catch(() => {
    tracker[failureName] = Number(tracker[failureName] ?? 0) + 1;
  });
  tracker[setName].add(task);
  void task.finally(() => tracker[setName].delete(task));
  return task;
}


/** 绑定 Fetch/Network/Console 门禁；事件处理只累计分类和计数。 */
function bindBrowserGuards(client, tracker) {
  client.on("Fetch.requestPaused", (event) => {
    recordNetworkActivity(tracker);
    const request = event.request ?? {};
    const url = String(request.url ?? "");
    const method = String(request.method ?? "").toUpperCase();
    const authorization = headerValue(request.headers, "Authorization");
    const bearer = authorization.match(/^Bearer ([^\s]+)$/)?.[1] ?? "";
    // 浏览器令牌只驻留内存用于反向隐私扫描，不写入任何日志或验收制品。
    if (bearer) tracker.browserTokens.add(bearer);
    let target;
    try { target = new URL(url); } catch { target = null; }
    const suppliedPrivate = tracker.privateValues.some(
      (value) => value && url.includes(value),
    );
    if (suppliedPrivate || target?.username || target?.password) {
      tracker.requestPrivacyViolationCount += 1;
    }
    if (target && [...target.searchParams.keys()].some((key) => FORBIDDEN_QUERY_KEY.test(key))) {
      tracker.forbiddenQueryCount += 1;
    }
    if (!browserRequestAllowed(url)) {
      if (classifyPageRequest(url) === "external") tracker.externalRequestCount += 1;
      client.send("Fetch.failRequest", {
        requestId: event.requestId,
        errorReason: "BlockedByClient",
      }).catch(() => { tracker.edgeProcessErrorCount += 1; });
      return;
    }
    if (
      tracker.delayNextAssistant
      && classifyPageRequest(url) === "assistant"
      && method === "POST"
      && !tracker.delayedRequestId
    ) {
      // 唯一迟到请求在发送前暂停；hashchange 先销毁 owner 后才允许该请求继续。
      registerDelayedAssistantRequest(tracker, event);
      return;
    }
    client.send("Fetch.continueRequest", { requestId: event.requestId })
      .catch(() => { tracker.edgeProcessErrorCount += 1; });
  });

  client.on("Network.requestWillBeSent", (event) => {
    recordNetworkActivity(tracker);
    const category = classifyPageRequest(event.request?.url);
    tracker.requestOwners.set(event.requestId, {
      category,
      method: String(event.request?.method ?? "").toUpperCase(),
      phase: String(tracker.phase || "unknown"),
      healthAttempt: HEALTH_REQUEST_CATEGORIES.has(category)
        ? parseHealthAttemptRequestId(headerValue(event.request?.headers, "X-Request-ID"))
        : null,
    });
  });
  client.on("Network.responseReceived", (event) => {
    recordNetworkResponse(tracker, event);
  });
  client.on("Network.loadingFailed", (event) => {
    recordNetworkLoadingFailure(tracker, event);
  });
  client.on("Network.loadingFinished", (event) => {
    recordNetworkActivity(tracker);
    const owner = tracker.requestOwners.get(event.requestId);
    if (
      owner?.category === "assistant"
      && owner.method === "POST"
      && owner.responseStatus === 200
    ) {
      scheduleInspection(
        tracker,
        "pendingAnswerInspections",
        "answerInspectionFailureCount",
        () => inspectAssistantResponseBody(client, tracker, event.requestId),
      );
    }
    tracker.requestOwners.delete(event.requestId);
    if (tracker.expectedCanceledRequestIds.delete(event.requestId)) {
      tracker.unexpectedNetworkFailureCount += 1;
      if (!tracker.unexpectedNetworkFailure) {
        tracker.unexpectedNetworkFailure = Object.freeze({
          phase: String(tracker.phase || "unknown"),
          category: "assistant",
          reason: "finished",
        });
      }
    }
  });

  /** 检查控制台值是否包含本轮私有配置，仅累计违规数量而不保存原文。 */
  const inspectConsolePrivacy = (value) => {
    const text = String(value ?? "");
    rememberInspectionText(tracker, "browser.console", text);
    // 任一私有值命中只增加计数，禁止记录命中的值、字段或控制台正文。
    if (trackerPrivateValues(tracker).some((secret) => secret && text.includes(secret))) {
      tracker.processPrivacyViolationCount += 1;
    }
  };
  /** 将控制台错误折叠为固定类别，并同步执行内存中的隐私值扫描。 */
  const inspectConsole = (value, level, source) => {
    const text = String(value ?? "");
    inspectConsolePrivacy(text);
    // 非错误级别消息无需进入错误账本，但已经完成隐私扫描。
    if (!/[Ee]rror|failed|exception/.test(level) && level !== "error") return;
    if (/status (?:401|403|404|422)|\b(?:401|403|404|422)\b/.test(text)) {
      tracker.expectedConsoleErrorCount += 1;
    } else {
      tracker.unexpectedConsoleErrorCount += 1;
      let reason = "other";
      if (/ERR_FAILED/i.test(text)) reason = "net_failed";
      else if (/Failed to fetch/i.test(text)) reason = "fetch_failed";
      else if (/abort|cancel/i.test(text)) reason = "aborted";
      else if (/ERR_CACHE_MISS/i.test(text)) reason = "cache_miss";
      else if (/Content Security Policy|CSP/i.test(text)) reason = "csp";
      if (reason === "net_failed") tracker.consoleNetworkFailureCount += 1;
      if (!tracker.unexpectedConsoleFailure) {
        tracker.unexpectedConsoleFailure = Object.freeze({
          phase: String(tracker.phase || "unknown"),
          source: source === "runtime" ? "runtime" : "log",
          reason,
        });
      }
    }
  };
  client.on("Runtime.consoleAPICalled", (event) => {
    const args = event.args ?? [];
    inspectConsole(
      args.map((item) => item.value ?? item.description ?? "").join(" "),
      String(event.type ?? ""),
      "runtime",
    );
    /** 异步展开控制台对象图执行隐私扫描，并将遍历失败单独计数。 */
    scheduleInspection(
      tracker,
      "pendingConsoleInspections",
      "consoleInspectionFailureCount",
      async (deadline) => {
        for (const item of args) {
          if (item.objectId) {
            await inspectConsoleObjectGraph(
              client,
              item.objectId,
              inspectConsolePrivacy,
              { deadline },
            );
          }
        }
      },
    );
  });
  client.on("Runtime.exceptionThrown", (event) => {
    const description = String(event.exceptionDetails?.exception?.description ?? "");
    inspectConsolePrivacy(description);
    tracker.exceptionCount += 1;
    if (!tracker.unexpectedConsoleFailure) {
      tracker.unexpectedConsoleFailure = Object.freeze({
        phase: String(tracker.phase || "unknown"),
        source: "runtime",
        reason: "exception",
      });
    }
  });
  client.on("Log.entryAdded", (event) => {
    inspectConsole(event.entry?.text, String(event.entry?.level ?? ""), "log");
  });
}


/** Console 复合参数使用有界 BFS；达到边界必须失败关闭，不能静默漏扫。 */
async function inspectConsoleObjectGraph(
  client,
  objectId,
  inspectValue,
  {
    objectLimit = CONSOLE_OBJECT_LIMIT,
    depthLimit = CONSOLE_DEPTH_LIMIT,
    deadline = Date.now() + INSPECTION_TIMEOUT_MS,
  } = {},
) {
  const queue = [{ objectId, depth: 0 }];
  const seen = new Set();
  while (queue.length > 0) {
    requireCondition(seen.size < objectLimit, "browser.console", "object_limit");
    const current = queue.shift();
    if (!current?.objectId || seen.has(current.objectId)) continue;
    seen.add(current.objectId);
    const result = await awaitBeforeDeadline(
      client.send("Runtime.getProperties", {
        objectId: current.objectId,
        ownProperties: true,
        accessorPropertiesOnly: false,
        generatePreview: false,
      }),
      deadline,
      "browser.console",
    );
    requireCondition(!result?.exceptionDetails, "browser.console", "properties");
    const properties = [
      ...(result?.result ?? []),
      ...(result?.internalProperties ?? []),
      ...(result?.privateProperties ?? []),
    ];
    for (const property of properties) {
      inspectValue(property.name ?? "");
      for (const remote of [property.value, property.get, property.set]) {
        if (!remote) continue;
        inspectValue(remote.value ?? remote.description ?? "");
        if (!remote.objectId || seen.has(remote.objectId)) continue;
        requireCondition(
          current.depth < depthLimit,
          "browser.console",
          "depth_limit",
        );
        queue.push({ objectId: remote.objectId, depth: current.depth + 1 });
      }
    }
  }
  return Object.freeze({ objects_inspected: seen.size });
}


/** 注销后等待全部已登记网络 owner 稳定归零，避免 teardown 掩盖迟到失败。 */
async function waitForRequestOwnersDrain(
  tracker,
  { timeoutMs = 30000, stableMs = 250, pollMs = 25 } = {},
) {
  const deadline = Date.now() + timeoutMs;
  let emptySince = null;
  let emptyRevision = null;
  while (Date.now() < deadline) {
    if (tracker.requestOwners.size === 0) {
      const revision = Number(tracker.networkActivityRevision ?? 0);
      if (emptySince === null || emptyRevision !== revision) {
        emptySince = Date.now();
        emptyRevision = revision;
      }
      if (Date.now() - emptySince >= stableMs) return true;
    } else {
      emptySince = null;
      emptyRevision = null;
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
  throw new BrowserAcceptanceFailure("browser.network", "owners_timeout");
}


/** 等待 Console 对象与成功响应正文扫描结束；任一 CDP 读取失败都必须阻断发布。 */
async function waitForConsoleInspections(
  tracker,
  {
    timeoutMs = INSPECTION_TIMEOUT_MS,
    stableMs = INSPECTION_STABLE_MS,
    pollMs = 25,
  } = {},
) {
  const deadline = Date.now() + timeoutMs;
  tracker.inspectionsSealed = true;
  let stableSince = null;
  let stableRevision = null;
  while (Date.now() < deadline) {
    const pending = [
      ...tracker.pendingConsoleInspections,
      ...tracker.pendingAnswerInspections,
    ];
    if (pending.length > 0) {
      stableSince = null;
      stableRevision = null;
      await awaitBeforeDeadline(
        Promise.all(pending),
        deadline,
        "browser.inspection",
      );
      continue;
    }
    const revision = Number(tracker.inspectionActivityRevision ?? 0);
    if (stableSince === null || stableRevision !== revision) {
      stableSince = Date.now();
      stableRevision = revision;
    }
    if (Number(tracker.lateInspectionFailureCount ?? 0) > 0) {
      throw new BrowserAcceptanceFailure("browser.inspection", "late_task");
    }
    if (Date.now() - stableSince >= stableMs) break;
    await awaitBeforeDeadline(
      new Promise((resolve) => setTimeout(resolve, pollMs)),
      deadline,
      "browser.inspection",
    );
  }
  requireCondition(Date.now() < deadline, "browser.inspection", "timeout");
  requireCondition(
    Number(tracker.lateInspectionFailureCount ?? 0) === 0,
    "browser.inspection",
    "late_task",
  );
  requireCondition(
    tracker.consoleInspectionFailureCount === 0,
    "browser.console",
    "inspection_failed",
  );
  requireCondition(
    tracker.answerInspectionFailureCount === 0,
    "browser.answer_response",
    "inspection_failed",
  );
}


/** 页面销毁后再次核对 sealed 状态，封住 drain 返回到 target close 之间的迟到事件窗口。 */
function assertSealedInspectionState(tracker) {
  requireCondition(tracker.inspectionsSealed === true, "browser.inspection", "not_sealed");
  requireCondition(
    Number(tracker.lateInspectionFailureCount ?? 0) === 0,
    "browser.inspection",
    "late_task",
  );
  requireCondition(
    tracker.pendingConsoleInspections.size === 0
      && tracker.pendingAnswerInspections.size === 0,
    "browser.inspection",
    "pending",
  );
  return true;
}


/** 导航到真实 Console，并等待登录表单完成绑定。 */
async function loadConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/` }, sessionId),
    "browser.console.load",
  );
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('#login-form')",
    "browser.console.login_form",
  );
}


/** 通过真实登录表单建立指定角色 Session；账号与口令只进入该次浏览器 POST。 */
async function loginRole(client, sessionId, role, privateConfig, tracker) {
  const account = ROLE_ACCOUNTS[role];
  requireCondition(account, "browser.login", "role");
  const expectedLoginCount = tracker.loginStatuses.length + 1;
  const email = JSON.stringify(account.email);
  const password = JSON.stringify(privateConfig[account.passwordKey]);
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector('#login-form');
    if (!form) return false;
    const tenant = form.elements.namedItem('tenantSlug');
    const email = form.elements.namedItem('email');
    const password = form.elements.namedItem('password');
    tenant.value = 'default';
    email.value = ${email};
    password.value = ${password};
    for (const element of [tenant, email, password]) {
      element.dispatchEvent(new Event('input', { bubbles: true }));
    }
    form.requestSubmit();
    return true;
  })()`, `browser.login.${role}`);
  requireCondition(submitted === true, `browser.login.${role}`, "submit");
  await waitForTrackerCount(
    () => tracker.loginStatuses.length,
    expectedLoginCount,
    `browser.login.${role}.network`,
  );
  requireCondition(
    tracker.loginStatuses.at(-1) === 200,
    `browser.login.${role}`,
    "status",
  );
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('#logout-button') && document.querySelector('.console-shell')",
    `browser.login.${role}.shell`,
  );
}


/** 点击真实登出按钮，并证明 PAGE-107 私有 DOM 在网络完成前后均不会残留。 */
async function logoutRole(client, sessionId, tracker, stage) {
  const expectedLogoutCount = tracker.logoutStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('#logout-button');
    if (!button) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('#login-form') && !document.querySelector('[data-admin-assistant-root]')",
    `${stage}.login_form`,
  );
  await waitForTrackerCount(
    () => tracker.logoutStatuses.length,
    expectedLogoutCount,
    `${stage}.network`,
  );
  requireCondition(tracker.logoutStatuses.at(-1) === 200, stage, "status");
}


/** 打开后端授权的 PAGE-107；不存在导航项时失败，不能通过手写 hash 绕过。 */
async function openAssistant(client, sessionId, stage) {
  const opened = await evaluate(client, sessionId, `(() => {
    if (document.querySelector('[data-admin-assistant-root]')) return true;
    const button = document.querySelector('[data-console-nav="admin-assistant"]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(opened === true, stage, "navigation");
  // 先证明真实导航按钮或已存在授权 root 对应同一合法 hash，再等待 PAGE-107；编码等价形式
  // 由产品 parseNavigationHash 接受，验收不能为追求字面量自行改写 URL 或制造第二次导航。
  await waitForDom(
    client,
    sessionId,
    `(() => {
      try { return decodeURIComponent(location.hash.slice(1)) === 'admin-assistant'; }
      catch { return false; }
    })()`,
    `${stage}.hash`,
  );
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-admin-assistant-root]')",
    `${stage}.root`,
  );
}


/** 清空当前问题、回答、来源和链路字段，并保持当前授权页面 owner。 */
async function clearAssistant(client, sessionId, stage) {
  const cleared = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-admin-assistant-clear]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(cleared === true, stage, "button");
  await waitForDom(
    client,
    sessionId,
    `(() => {
      const root = document.querySelector('[data-admin-assistant-root]');
      return root?.dataset.adminAssistantState === 'idle'
        && (root.querySelector('[data-admin-assistant-question]')?.value || '') === ''
        && !root.querySelector('[data-admin-assistant-sensitive]');
    })()`,
    `${stage}.empty`,
  );
}


/** 提交一次真实问题，仅返回 DOM 结构计数和固定合同判断，不返回问题或回答正文。 */
async function submitAssistantQuery(
  client,
  sessionId,
  tracker,
  privateConfig,
  queryCase,
  {
    expectedStatus,
    expectedCode = "",
    expectedAnswerOracle = null,
    queryName = "",
    stage,
  },
) {
  const expectedNetworkCount = tracker.assistantStatuses.length + 1;
  const question = JSON.stringify(`${queryCase.text} ${privateConfig.questionCanary}`);
  const submitted = await evaluate(client, sessionId, `(() => {
    const root = document.querySelector('[data-admin-assistant-root]');
    const textarea = root?.querySelector('[data-admin-assistant-question]');
    const form = root?.querySelector('[data-admin-assistant-form]');
    if (!textarea || !form) return false;
    textarea.value = ${question};
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    form.requestSubmit();
    return true;
  })()`, stage);
  requireCondition(submitted === true, stage, "submit");
  await waitForTrackerCount(
    () => tracker.assistantStatuses.length,
    expectedNetworkCount,
    `${stage}.network`,
  );
  const observedStatus = tracker.assistantStatuses.at(-1);
  if (observedStatus !== expectedStatus) {
    const coordinate = Number.isInteger(observedStatus)
      && observedStatus >= 100
      && observedStatus <= 599
      ? `status_${observedStatus}`
      : "status_invalid";
    throw new BrowserAcceptanceFailure(stage, coordinate);
  }

  if (expectedStatus === 200) {
    await waitForDom(
      client,
      sessionId,
      "document.querySelector('[data-admin-assistant-root]')?.dataset.adminAssistantState === 'answered'",
      `${stage}.answered`,
    );
    const label = JSON.stringify(queryCase.label);
    const answerCanary = JSON.stringify(privateConfig.answerCanary);
    const answerOracle = JSON.stringify(expectedAnswerOracle);
    const facts = await evaluate(client, sessionId, `(() => {
      const root = document.querySelector('[data-admin-assistant-root]');
      const claims = [...(root?.querySelectorAll('[data-admin-assistant-claim-index]') || [])];
      const recommendations = [
        ...(root?.querySelectorAll('[data-admin-assistant-recommendation-index]') || []),
      ];
      const sources = [...(root?.querySelectorAll('[data-admin-assistant-source-index]') || [])];
      const links = [...(root?.querySelectorAll('a[data-admin-assistant-source-index]') || [])];
      const oracle = ${answerOracle};
      const claimLeafValues = claims.flatMap((claim) => {
        const label = claim.querySelector('[data-admin-assistant-claim-label]')?.textContent || '';
        const formatted = claim.querySelector('[data-admin-assistant-claim-value]')?.textContent || '';
        const unitSeparator = formatted.lastIndexOf(' ');
        const rawValue = unitSeparator > 0 ? formatted.slice(0, unitSeparator) : formatted;
        return [label, formatted, rawValue];
      });
      const sourceLeafValues = sources.flatMap((source) => {
        const label = source.querySelector('[data-admin-assistant-source-label]')?.textContent || '';
        const detail = source.querySelector('[data-admin-assistant-source-detail]')?.textContent || '';
        const timestamps = detail.match(
          /[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z/g,
        ) || [];
        return [label, detail, ...timestamps];
      });
      const answerLeafValues = [
        root?.querySelector('[data-admin-assistant-summary]')?.textContent || '',
        ...claimLeafValues,
        ...recommendations.map((item) => (
          item.querySelector('[data-admin-assistant-recommendation-message]')?.textContent || ''
        )),
        ...sourceLeafValues,
      ];
      return {
        claim_count: claims.length,
        source_count: root?.querySelectorAll('[data-admin-assistant-source-index]').length || 0,
        link_count: links.length,
        links_internal: links.every((link) => new URL(link.href).origin === location.origin),
        label_match: [...root.querySelectorAll('.admin-assistant-section-heading span')]
          .some((element) => element.textContent === ${label}),
        answer_canary_hits: document.body.textContent.includes(${answerCanary}) ? 1 : 0,
        oracle_claim_matches: oracle === null ? 0 : claims.filter((claim) => (
          claim.querySelector('[data-admin-assistant-claim-label]')?.textContent === oracle.claimLabel
          && claim.querySelector('[data-admin-assistant-claim-value]')?.textContent === oracle.claimValue
        )).length,
        oracle_source_matches: oracle === null ? 0 : links.filter((link) => (
          link.querySelector('[data-admin-assistant-source-label]')?.textContent === oracle.sourceLabel
          && link.getAttribute('href') === oracle.sourceHref
        )).length,
        answer_leaf_values: answerLeafValues,
      };
    })()`, `${stage}.facts`);
    const oracleExpected = expectedAnswerOracle !== null;
    requireCondition(
      facts?.claim_count > 0
      && facts.source_count > 0
      && facts.link_count === facts.source_count
      && facts.links_internal === true
      && facts.label_match === true
      && facts.answer_canary_hits === (oracleExpected ? 1 : 0)
      && facts.oracle_claim_matches === (oracleExpected ? 1 : 0)
      && facts.oracle_source_matches === (oracleExpected ? 1 : 0),
      stage,
      "answer_contract",
    );
    const answerLeafValues = registerAnswerLeafValues(
      tracker,
      queryName,
      facts.answer_leaf_values,
    );
    await inspectBrowserPrivacy(client, sessionId, tracker, privateConfig, {
      expectRoot: true,
      activeAnswer: true,
      activeAnswerValues: answerLeafValues,
      expectAnswerCanary: oracleExpected,
    });
    return Object.freeze({
      answer_verified: true,
      answer_leaf_count: answerLeafValues.length,
    });
  }

  await waitForDom(
    client,
    sessionId,
    "document.querySelector('[data-admin-assistant-root]')?.dataset.adminAssistantState === 'error'",
    `${stage}.error`,
  );
  const code = JSON.stringify(expectedCode);
  const errorValid = await evaluate(client, sessionId, `(() => {
    const root = document.querySelector('[data-admin-assistant-root]');
    return root?.querySelector('[data-admin-assistant-error-code]')?.textContent === ${code}
      && !root.querySelector('.admin-assistant-answer');
  })()`, `${stage}.error_contract`);
  requireCondition(errorValid === true, stage, "error_contract");
  return Object.freeze({ error_verified: true });
}


/** 对真实回答来源执行一次 href/index 篡改拒绝或 exact 导航，并验证同步 owner 清理。 */
async function exerciseSourceAction(
  client,
  sessionId,
  privateConfig,
  { action, expectedHref },
) {
  const contract = await evaluate(client, sessionId, `(() => {
    const matches = Array.from(document.querySelectorAll('a[data-admin-assistant-source-index]'))
      .filter((candidate) => candidate.getAttribute('href') === ${JSON.stringify(expectedHref)});
    const link = matches.length === 1 ? matches[0] : null;
    return link ? {
      href: link.getAttribute('href'),
      index: link.getAttribute('data-admin-assistant-source-index'),
      hash: location.hash,
      matchCount: matches.length,
    } : null;
  })()`, "browser.admin.source.contract");
  const sourceIndex = canonicalSourceIndex(contract?.index);
  requireCondition(
    contract?.href === expectedHref
      && contract.matchCount === 1
      && sourceIndex !== null
      && contract.hash === "#admin-assistant",
    "browser.admin.source",
    "contract",
  );
  if (action === "href_tamper" || action === "index_tamper") {
    const tampered = await evaluate(client, sessionId, `(() => {
      const link = document.querySelector('a[data-admin-assistant-source-index="${sourceIndex}"]');
      if (!link) return false;
      if (${JSON.stringify(action)} === 'href_tamper') {
        link.setAttribute('href', ${JSON.stringify(expectedHref === "/#overview" ? "/#usage" : "/#overview")});
      } else {
        link.setAttribute('data-admin-assistant-source-index', '01');
      }
      link.click();
      return true;
    })()`, `browser.admin.source.${action}`);
    requireCondition(tampered === true, "browser.admin.source", action);
    await waitForDom(
      client,
      sessionId,
      `(() => {
        const root = document.querySelector('[data-admin-assistant-root]');
        return location.hash === '#admin-assistant'
          && root?.dataset.adminAssistantState === 'idle'
          && (root.querySelector('[data-admin-assistant-question]')?.value || '') === ''
          && !root.querySelector('[data-admin-assistant-sensitive]')
          && !root.querySelector('[data-admin-assistant-source-index]')
          && !document.body.textContent.includes(${JSON.stringify(privateConfig.questionCanary)})
          && !document.body.textContent.includes(${JSON.stringify(privateConfig.answerCanary)});
      })()`,
      `browser.admin.source.${action}.cleanup`,
    );
    return Object.freeze({ action, rejected: true, cleanup: true });
  }
  requireCondition(action === "navigate", "browser.admin.source", "action");
  const clicked = await evaluate(client, sessionId, `(() => {
    const link = document.querySelector('a[data-admin-assistant-source-index="${sourceIndex}"]');
    if (link?.getAttribute('href') !== ${JSON.stringify(expectedHref)}) return false;
    link.click();
    return true;
  })()`, "browser.admin.source.navigate");
  requireCondition(clicked === true, "browser.admin.source", "link");
  const expectedTarget = new URL(expectedHref, CONSOLE_ORIGIN);
  const expectedNavigationItem = expectedTarget.hash.slice(1);
  requireCondition(
    expectedTarget.origin === CONSOLE_ORIGIN
      && expectedTarget.pathname === "/"
      && expectedTarget.search === ""
      && expectedNavigationItem.length > 0,
    "browser.admin.source",
    "target",
  );
  await waitForDom(
    client,
    sessionId,
    `(() => {
      const expected = ${JSON.stringify({
        origin: CONSOLE_ORIGIN,
        pathname: expectedTarget.pathname,
        search: expectedTarget.search,
        hash: expectedTarget.hash,
      })};
      const navigationItem = ${JSON.stringify(expectedNavigationItem)};
      const activeButton = document.querySelector('[data-console-nav="' + navigationItem + '"]');
      return location.origin === expected.origin
        && location.pathname === expected.pathname
        && location.search === expected.search
        && location.hash === expected.hash
        && document.querySelector('.console-shell')?.dataset.activeNavigation === navigationItem
        && activeButton?.getAttribute('aria-current') === 'page'
        && !document.querySelector('[data-admin-assistant-root]')
        && !document.querySelector('[data-admin-assistant-sensitive]')
        && !document.body.textContent.includes(${JSON.stringify(privateConfig.questionCanary)})
        && !document.body.textContent.includes(${JSON.stringify(privateConfig.answerCanary)});
    })()`,
    "browser.admin.source.cleanup",
  );
  await openAssistant(client, sessionId, "browser.admin.source.return");
  return Object.freeze({
    action,
    exact_target: Object.freeze({
      origin: expectedTarget.origin,
      pathname: expectedTarget.pathname,
      search: expectedTarget.search,
      hash: expectedTarget.hash,
      navigation_item: expectedNavigationItem,
    }),
    cleanup: true,
  });
}


/** 暂停一次 POST，先触发规范等价 hashchange，再证明迟到结果不能恢复问题或回答。 */
async function exerciseLateResponse(client, sessionId, tracker, privateConfig) {
  tracker.delayNextAssistant = true;
  const question = JSON.stringify(`${QUERY_CASES.usage.text} ${privateConfig.questionCanary}`);
  const submitted = await evaluate(client, sessionId, `(() => {
    const root = document.querySelector('[data-admin-assistant-root]');
    const textarea = root?.querySelector('[data-admin-assistant-question]');
    const form = root?.querySelector('[data-admin-assistant-form]');
    if (!textarea || !form) return false;
    textarea.value = ${question};
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    form.requestSubmit();
    return true;
  })()`, "browser.admin.late.submit");
  requireCondition(submitted === true, "browser.admin.late", "submit");
  await waitForTrackerCount(
    () => tracker.delayedRequestId ? 1 : 0,
    1,
    "browser.admin.late.paused",
  );
  const delayedRequestId = tracker.delayedRequestId;
  await evaluate(client, sessionId, `(() => {
    location.hash = location.hash === '#admin-assistant'
      ? '#%61dmin-assistant'
      : '#admin-assistant';
    return true;
  })()`, "browser.admin.late.hashchange");
  await evaluate(
    client,
    sessionId,
    "new Promise((resolve) => setTimeout(() => resolve(true), 100))",
    "browser.admin.late.cleanup_window",
  );
  tracker.delayedRequestId = "";
  tracker.delayNextAssistant = false;
  try {
    await client.send("Fetch.continueRequest", { requestId: delayedRequestId });
  } catch {
    // hashchange 已 abort 时 CDP 可能先释放 request owner；两种时序都必须保持 DOM 已清空。
  }
  await waitForTrackerCount(
    () => tracker.canceledAssistantRequestCount,
    1,
    "browser.admin.late.loading_failed",
  );
  requireCondition(
    tracker.delayedLoadingFailureObserved === true
      && tracker.expectedCanceledRequestIds.size === 0,
    "browser.admin.late",
    "loading_failed_contract",
  );
  await waitForDom(
    client,
    sessionId,
    `(() => {
      const root = document.querySelector('[data-admin-assistant-root]');
      return root?.dataset.adminAssistantState === 'idle'
        && (root.querySelector('[data-admin-assistant-question]')?.value || '') === ''
        && !root.querySelector('[data-admin-assistant-sensitive]')
        && !document.body.textContent.includes(${JSON.stringify(privateConfig.questionCanary)})
        && !document.body.textContent.includes(${JSON.stringify(privateConfig.answerCanary)});
    })()`,
    "browser.admin.late.cleaned",
  );
  await evaluate(
    client,
    sessionId,
    "new Promise((resolve) => setTimeout(() => resolve(true), 800))",
    "browser.admin.late.settle",
  );
  const stillClean = await evaluate(client, sessionId, `(() => {
    const root = document.querySelector('[data-admin-assistant-root]');
    return root?.dataset.adminAssistantState === 'idle'
      && !root.querySelector('[data-admin-assistant-sensitive]')
      && !document.body.textContent.includes(${JSON.stringify(privateConfig.questionCanary)})
      && !document.body.textContent.includes(${JSON.stringify(privateConfig.answerCanary)});
  })()`, "browser.admin.late.final");
  requireCondition(stillClean === true, "browser.admin.late", "revived");
}


/** admin 覆盖四类回答、来源、只读阻断、等价 hash 与登出清理。 */
async function exerciseAdmin(client, sessionId, tracker, privateConfig) {
  await loginRole(client, sessionId, "admin", privateConfig, tracker);
  await openAssistant(client, sessionId, "browser.admin.open");
  let answered = 0;
  const sourceNavigation = {};
  for (const [name, queryCase] of Object.entries(QUERY_CASES)) {
    tracker.phase = `admin.${name}.query`;
    await submitAssistantQuery(
      client,
      sessionId,
      tracker,
      privateConfig,
      queryCase,
      {
        expectedStatus: 200,
        expectedAnswerOracle: answerOracleForQuery(name, privateConfig.answerCanary),
        queryName: name,
        stage: `browser.admin.${name}`,
      },
    );
    answered += 1;
    tracker.phase = `admin.${name}.source`;
    // 每类回答只允许跳转到对应只读来源，篡改检查必须按问题类型精确执行。
    if (name === "usage") {
      sourceNavigation.href_tamper = await exerciseSourceAction(
        client,
        sessionId,
        privateConfig,
        { action: "href_tamper", expectedHref: "/#usage" },
      );
    } else if (name === "errors") {
      sourceNavigation.index_tamper = await exerciseSourceAction(
        client,
        sessionId,
        privateConfig,
        { action: "index_tamper", expectedHref: "/#request-logs" },
      );
    } else if (name === "cost") {
      sourceNavigation.exact_navigation = await exerciseSourceAction(
        client,
        sessionId,
        privateConfig,
        { action: "navigate", expectedHref: "/#overview" },
      );
    } else {
      await clearAssistant(client, sessionId, `browser.admin.${name}.clear`);
    }
  }
  tracker.phase = "admin.read_only";
  await submitAssistantQuery(
    client,
    sessionId,
    tracker,
    privateConfig,
    { text: "请修改策略并解释成本", label: "" },
    {
      expectedStatus: 403,
      expectedCode: "admin_assistant.read_only_violation",
      stage: "browser.admin.read_only",
    },
  );
  await clearAssistant(client, sessionId, "browser.admin.read_only.clear");
  tracker.phase = "admin.late_response";
  await exerciseLateResponse(client, sessionId, tracker, privateConfig);
  tracker.phase = "admin.logout";
  await logoutRole(client, sessionId, tracker, "browser.admin.logout");
  return Object.freeze({
    answered,
    source_navigation: sourceNavigation,
    read_only_denied: true,
    late_cleanup: true,
    answer_canary_rendered_and_cleared: true,
  });
}


/** finance 保留助手 base permission，仅撤用量来源权限并立即清除旧回答与来源。 */
async function exerciseFinance(client, sessionId, tracker, privateConfig) {
  tracker.phase = "finance.login";
  await loginRole(client, sessionId, "finance", privateConfig, tracker);
  tracker.phase = "finance.open";
  await openAssistant(client, sessionId, "browser.finance.open");
  tracker.phase = "finance.denied";
  await submitAssistantQuery(
    client,
    sessionId,
    tracker,
    privateConfig,
    QUERY_CASES.errors,
    {
      expectedStatus: 403,
      expectedCode: "auth.permission_denied",
      stage: "browser.finance.denied",
    },
  );
  await clearAssistant(client, sessionId, "browser.finance.denied.clear");
  tracker.phase = "finance.allowed";
  await submitAssistantQuery(
    client,
    sessionId,
    tracker,
    privateConfig,
    QUERY_CASES.usage,
    {
      expectedStatus: 200,
      expectedAnswerOracle: answerOracleForQuery("usage", privateConfig.answerCanary),
      queryName: "usage",
      stage: "browser.finance.allowed",
    },
  );
  const beforeRevoke = await evaluate(client, sessionId, `(() => {
    const root = document.querySelector('[data-admin-assistant-root]');
    return root?.dataset.adminAssistantState === 'answered'
      && root.querySelectorAll('[data-admin-assistant-sensitive]').length > 0
      && root.querySelectorAll('[data-admin-assistant-source-index]').length > 0;
  })()`, "browser.finance.revoke.before");
  requireCondition(beforeRevoke === true, "browser.finance.revoke", "missing_old_answer");

  tracker.phase = "finance.revoke";
  await revokeFinancePermission();
  await evaluate(client, sessionId, `(() => {
    location.hash = location.hash === '#admin-assistant'
      ? '#%61dmin-assistant'
      : '#admin-assistant';
    return true;
  })()`, "browser.finance.revoke.hashchange");
  await waitForDom(
    client,
    sessionId,
    `(() => {
      const root = document.querySelector('[data-admin-assistant-root]');
      return Boolean(document.querySelector('[data-console-nav="admin-assistant"]'))
        && root?.dataset.adminAssistantState === 'idle'
        && (root.querySelector('[data-admin-assistant-question]')?.value || '') === ''
        && !root.querySelector('[data-admin-assistant-sensitive]')
        && !root.querySelector('[data-admin-assistant-source-index]')
        && !document.body.textContent.includes(${JSON.stringify(privateConfig.questionCanary)})
        && !document.body.textContent.includes(${JSON.stringify(privateConfig.answerCanary)});
    })()`,
    "browser.finance.revoke.cleanup",
  );
  tracker.phase = "finance.logout";
  await logoutRole(client, sessionId, tracker, "browser.finance.logout");
  return Object.freeze({
    allowed: true,
    source_denied: true,
    base_permission_retained: true,
    source_permission_revoked: true,
    dynamic_revoke_cleanup: true,
  });
}


/** security 验证允许错误解释但拒绝用量域，权限结论始终来自后端 403。 */
async function exerciseSecurity(client, sessionId, tracker, privateConfig) {
  tracker.phase = "security.login";
  await loginRole(client, sessionId, "security", privateConfig, tracker);
  tracker.phase = "security.open";
  await openAssistant(client, sessionId, "browser.security.open");
  tracker.phase = "security.allowed";
  await submitAssistantQuery(
    client,
    sessionId,
    tracker,
    privateConfig,
    QUERY_CASES.errors,
    {
      expectedStatus: 200,
      queryName: "errors",
      stage: "browser.security.allowed",
    },
  );
  tracker.phase = "security.allowed_clear";
  await clearAssistant(client, sessionId, "browser.security.allowed.clear");
  tracker.phase = "security.denied";
  await submitAssistantQuery(
    client,
    sessionId,
    tracker,
    privateConfig,
    QUERY_CASES.usage,
    {
      expectedStatus: 403,
      expectedCode: "auth.permission_denied",
      stage: "browser.security.denied",
    },
  );
  tracker.phase = "security.denied_clear";
  await clearAssistant(client, sessionId, "browser.security.denied.clear");
  tracker.phase = "security.logout";
  await logoutRole(client, sessionId, tracker, "browser.security.logout");
  return Object.freeze({ allowed: true, source_denied: true });
}


/** 读取空状态 PAGE-107 的几何事实，不返回任何 DOM 文本或身份值。 */
async function readLayoutMetrics(client, sessionId) {
  return evaluate(client, sessionId, `(() => {
    const visible = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden'
        && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const root = document.querySelector('[data-admin-assistant-root]');
    const rootRect = root?.getBoundingClientRect();
    const controls = [...(root?.querySelectorAll('button,textarea,a[href]') || [])].filter(visible);
    const textNodes = [...(root?.querySelectorAll('h2,h3,h4,p,dt,dd,strong,small,code,button') || [])]
      .filter(visible);
    let overlapCount = 0;
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        const a = controls[left];
        const b = controls[right];
        if (a.contains(b) || b.contains(a)) continue;
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        if (Math.min(ar.right, br.right) - Math.max(ar.left, br.left) > 1
          && Math.min(ar.bottom, br.bottom) - Math.max(ar.top, br.top) > 1) overlapCount += 1;
      }
    }
    const sidebar = document.querySelector('.console-sidebar')?.getBoundingClientRect();
    const main = document.querySelector('.console-main')?.getBoundingClientRect();
    const shellOverlap = sidebar && main
      ? Math.max(0, Math.min(sidebar.right, main.right) - Math.max(sidebar.left, main.left))
        * Math.max(0, Math.min(sidebar.bottom, main.bottom) - Math.max(sidebar.top, main.top))
      : 0;
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      root_visible: visible(root),
      root_inside_horizontal: Boolean(rootRect)
        && rootRect.left >= -1 && rootRect.right <= innerWidth + 1,
      controls_outside_horizontal: controls.filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.left < -1 || rect.right > innerWidth + 1;
      }).length,
      text_overflow_count: textNodes.filter(
        (element) => element.scrollWidth > element.clientWidth + 1,
      ).length,
      control_overlap_count: overlapCount,
      shell_overlap_area: shellOverlap,
      sensitive_node_count: root?.querySelectorAll('[data-admin-assistant-sensitive]').length || 0,
      question_length: root?.querySelector('[data-admin-assistant-question]')?.value.length || 0,
    };
  })()`, "browser.layout.read");
}


/** 双视口均要求无横向溢出、控件越界、文本裁切、重叠或敏感正文节点。 */
function assertLayoutMetrics(metrics, viewport) {
  const stage = `browser.layout.${viewport.name}`;
  requireCondition(
    metrics?.viewport_width === viewport.width && metrics.viewport_height === viewport.height,
    stage,
    "viewport",
  );
  requireCondition(
    metrics.document_scroll_width <= viewport.width + 1
      && metrics.body_scroll_width <= viewport.width + 1,
    stage,
    "horizontal_overflow",
  );
  requireCondition(metrics.root_visible && metrics.root_inside_horizontal, stage, "root_bounds");
  requireCondition(metrics.controls_outside_horizontal === 0, stage, "control_bounds");
  requireCondition(metrics.text_overflow_count === 0, stage, "text_overflow");
  requireCondition(metrics.control_overlap_count === 0, stage, "control_overlap");
  requireCondition(metrics.shell_overlap_area === 0, stage, "shell_overlap");
  requireCondition(
    metrics.sensitive_node_count === 0 && metrics.question_length === 0,
    stage,
    "private_state",
  );
  return metrics;
}


/** 截图前隐藏用户菜单身份但保留几何尺寸；PAGE-107 本身必须已经为空状态。 */
async function maskIdentity(client, sessionId) {
  const masked = await evaluate(client, sessionId, `(() => {
    const menu = document.querySelector('.user-menu');
    if (!menu) return false;
    menu.dataset.prd077Mask = 'true';
    menu.style.visibility = 'hidden';
    return true;
  })()`, "browser.screenshot.mask");
  requireCondition(masked === true, "browser.screenshot.mask", "missing");
}


/** 截图后恢复验收器添加的临时 mask，不修改业务状态或 DOM 正文。 */
async function restoreIdentity(client, sessionId) {
  await evaluate(client, sessionId, `(() => {
    const menu = document.querySelector('[data-prd077-mask="true"]');
    if (!menu) return false;
    menu.style.visibility = '';
    delete menu.dataset.prd077Mask;
    return true;
  })()`, "browser.screenshot.restore");
}


/** 捕获精确视口 PNG 并执行像素非空检查；文件名不含身份或运行私有值。 */
async function captureEmptyScreenshot(client, sessionId, artifactDirectory, viewport) {
  await setViewport(client, sessionId, viewport);
  await evaluate(client, sessionId, `new Promise((resolve) => {
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
  })`, `browser.screenshot.${viewport.name}.frame`);
  const layout = assertLayoutMetrics(await readLayoutMetrics(client, sessionId), viewport);
  await maskIdentity(client, sessionId);
  let result;
  try {
    result = await client.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false,
    }, sessionId, 30000);
  } finally {
    await restoreIdentity(client, sessionId);
  }
  const buffer = Buffer.from(String(result?.data ?? ""), "base64");
  const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${viewport.name}`);
  const filename = `empty-${viewport.name}.png`;
  await writeFile(path.join(artifactDirectory, filename), buffer);
  return Object.freeze({ filename, layout, pixels });
}


/** 登录态只允许两个认证槽位且 Token 必须来自本轮网络；登出态要求两个 Storage 都精确为空。 */
function browserStorageFactsValid(facts, expectAuthenticated) {
  if (!facts || facts.local_storage_count !== 0) return false;
  return expectAuthenticated
    ? facts.session_storage_count === 2
      && facts.session_keys_exact === true
      && facts.auth_token_known === true
      && facts.auth_context_valid === true
    : facts.session_storage_count === 0
      && facts.session_keys_exact === false
      && facts.auth_token_known === false
      && facts.auth_context_valid === false;
}


/** 检查活动回答或清空终态的 DOM、URL、Storage、Cache 与 IndexedDB 精确边界。 */
async function inspectBrowserPrivacy(
  client,
  sessionId,
  tracker,
  privateConfig,
  {
    expectRoot,
    activeAnswer = false,
    activeAnswerValues = [],
    expectAnswerCanary = false,
    expectAuthenticated = expectRoot,
  },
) {
  const fixedCanaries = [
    privateConfig.questionCanary,
    privateConfig.answerCanary,
  ];
  const registeredAnswerValues = [...(tracker.answerLeafValues ?? [])];
  const currentAnswerValues = [...new Set(activeAnswerValues)];
  requireCondition(
    !activeAnswer || currentAnswerValues.length > 0,
    "browser.privacy",
    "answer_leaf",
  );
  const canaries = JSON.stringify(fixedCanaries);
  const registeredAnswers = JSON.stringify(registeredAnswerValues);
  const currentAnswers = JSON.stringify(currentAnswerValues);
  const browserTokens = JSON.stringify([...tracker.browserTokens]);
  const facts = await evaluate(client, sessionId, `(async () => {
    const canaries = ${canaries};
    const registeredAnswers = ${registeredAnswers};
    const currentAnswers = ${currentAnswers};
    const currentAnswerSet = new Set(currentAnswers);
    const staleAnswers = registeredAnswers.filter((value) => !currentAnswerSet.has(value));
    const privateValues = [...new Set([...canaries, ...registeredAnswers])];
    const browserTokens = ${browserTokens};
    const storageValues = [];
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        storageValues.push(key || '', storage.getItem(key) || '');
      }
    }
    const sessionKeys = Object.keys(sessionStorage).sort();
    const token = sessionStorage.getItem('metahub.auth_token') || '';
    const rawContext = sessionStorage.getItem('metahub.auth_context') || '';
    let contextValid = false;
    try {
      const context = JSON.parse(rawContext);
      const permissionCodes = context?.permission_codes;
      const tenantId = String(context?.tenant?.tenant_id || '');
      const userTenantId = String(context?.user?.tenant_id || '');
      contextValid = Boolean(context)
        && typeof context === 'object'
        && !Array.isArray(context)
        && /^[A-Za-z0-9_-]{1,128}$/.test(tenantId)
        && userTenantId === tenantId
        && /^[A-Za-z0-9_-]{1,160}$/.test(String(context?.user?.user_id || ''))
        && /^[A-Za-z0-9_-]{1,160}$/.test(String(context?.session?.session_id || ''))
        && Array.isArray(permissionCodes)
        && permissionCodes.length > 0
        && permissionCodes.every((code) => /^[a-z][a-z0-9_.]{1,127}$/.test(String(code)))
        && new Set(permissionCodes).size === permissionCodes.length
        && Number.isSafeInteger(Number(context?.permission_revision))
        && Number(context.permission_revision) >= 0
        && /^[A-Za-z0-9_.:-]{1,256}$/.test(String(context?.authorization_fingerprint || ''));
    } catch {
      contextValid = false;
    }
    const cacheNames = typeof caches === 'undefined' ? [] : await caches.keys();
    const databases = typeof indexedDB.databases === 'function' ? await indexedDB.databases() : [];
    const root = document.querySelector('[data-admin-assistant-root]');
    const text = document.documentElement.textContent || '';
    const attributeValues = [...document.querySelectorAll('*')].flatMap(
      (element) => [...element.attributes].map((attribute) => attribute.value),
    );
    const formValues = [...document.querySelectorAll('input,textarea,select')].map(
      (element) => String(element.value || ''),
    );
    return {
      root_count: root ? 1 : 0,
      sensitive_node_count: document.querySelectorAll('[data-admin-assistant-sensitive]').length,
      question_length: root?.querySelector('[data-admin-assistant-question]')?.value.length || 0,
      question_text_hits: text.includes(canaries[0]) ? 1 : 0,
      answer_text_hits: text.includes(canaries[1]) ? 1 : 0,
      registered_answer_text_hits: registeredAnswers.filter(
        (value) => text.includes(value),
      ).length,
      active_answer_text_hits: currentAnswers.filter((value) => text.includes(value)).length,
      stale_answer_text_hits: staleAnswers.filter((value) => text.includes(value)).length,
      answer_leaf_count: registeredAnswers.length,
      attribute_canary_hits: privateValues.filter(
        (canary) => attributeValues.some((value) => value.includes(canary)),
      ).length,
      question_form_hits: formValues.some((value) => value.includes(canaries[0])) ? 1 : 0,
      answer_form_hits: [canaries[1], ...registeredAnswers].filter(
        (canary) => formValues.some((value) => value.includes(canary)),
      ).length,
      url_canary_hits: privateValues.filter((value) => location.href.includes(value)).length,
      storage_canary_hits: privateValues.filter(
        (canary) => storageValues.some((value) => value.includes(canary)),
      ).length,
      local_storage_count: localStorage.length,
      session_storage_count: sessionStorage.length,
      session_keys_exact: JSON.stringify(sessionKeys)
        === JSON.stringify(['metahub.auth_context', 'metahub.auth_token']),
      auth_token_known: browserTokens.includes(token),
      auth_context_valid: contextValid,
      cache_count: cacheNames.length,
      indexeddb_count: databases.length,
    };
  })()`, "browser.privacy");
  requireCondition(facts?.root_count === (expectRoot ? 1 : 0), "browser.privacy", "root");
  const domValid = activeAnswer
    ? facts.question_text_hits === 0
      && facts.answer_text_hits === (expectAnswerCanary ? 1 : 0)
      && facts.active_answer_text_hits === currentAnswerValues.length
      && facts.stale_answer_text_hits === 0
      && facts.attribute_canary_hits === 0
      && facts.question_form_hits === 1
      && facts.answer_form_hits === 0
    : facts.question_text_hits === 0
      && facts.answer_text_hits === 0
      && facts.registered_answer_text_hits === 0
      && facts.active_answer_text_hits === 0
      && facts.stale_answer_text_hits === 0
      && facts.attribute_canary_hits === 0
      && facts.question_form_hits === 0
      && facts.answer_form_hits === 0;
  const storageValid = browserStorageFactsValid(facts, expectAuthenticated);
  requireCondition(
    domValid
      && storageValid
      && (activeAnswer || facts.sensitive_node_count === 0)
      && (activeAnswer || facts.question_length === 0)
      && facts.url_canary_hits === 0
      && facts.storage_canary_hits === 0
      && facts.cache_count === 0
      && facts.indexeddb_count === 0,
    "browser.privacy",
    "private_state",
  );
  return facts;
}


/** 最终截图使用 fresh admin 空页面；截图完成后登出并再次证明全局正文清理。 */
async function captureFinalEvidence(
  client,
  sessionId,
  tracker,
  privateConfig,
  artifactDirectory,
) {
  tracker.phase = "final.login";
  await loginRole(client, sessionId, "admin", privateConfig, tracker);
  tracker.phase = "final.open";
  await openAssistant(client, sessionId, "browser.final.open");
  tracker.phase = "final.active_privacy";
  const activePrivacy = await inspectBrowserPrivacy(
    client,
    sessionId,
    tracker,
    privateConfig,
    { expectRoot: true },
  );
  const screenshots = [];
  for (const viewport of Object.values(VIEWPORTS)) {
    tracker.phase = `final.screenshot.${viewport.name}`;
    screenshots.push(
      await captureEmptyScreenshot(client, sessionId, artifactDirectory, viewport),
    );
  }
  tracker.phase = "final.logout";
  await logoutRole(client, sessionId, tracker, "browser.final.logout");
  tracker.phase = "final.logout_privacy";
  const logoutPrivacy = await inspectBrowserPrivacy(
    client,
    sessionId,
    tracker,
    privateConfig,
    { expectRoot: false },
  );
  return Object.freeze({ screenshots, activePrivacy, logoutPrivacy });
}


/** Evidence 对象键使用严格黑名单递归审计，避免后续扩展误放正文或身份。 */
function assertEvidenceKeys(value, stage = "evidence.keys") {
  if (Array.isArray(value)) {
    for (const item of value) assertEvidenceKeys(item, stage);
    return true;
  }
  if (!value || typeof value !== "object") return true;
  for (const [key, item] of Object.entries(value)) {
    requireCondition(!FORBIDDEN_EVIDENCE_KEYS.has(key.toLowerCase()), stage, "forbidden_key");
    assertEvidenceKeys(item, stage);
  }
  return true;
}


/** 证据顶层必须使用冻结的 exact key 集，防止缺项或静默扩展绕过合同。 */
function assertExactObjectKeys(value, expected, stage) {
  requireCondition(
    value && typeof value === "object" && !Array.isArray(value),
    stage,
    "shape",
  );
  requireCondition(
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort()),
    stage,
    "keys",
  );
  return true;
}


/** 验证 Python harness 摘要及嵌套数据库/隐私字段的 exact 跨语言合同。 */
function assertRuntimeSummary(value) {
  assertExactObjectKeys(value, RUNTIME_SUMMARY_KEYS, "evidence.runtime");
  assertExactObjectKeys(value.database, DATABASE_HEALTH_KEYS, "evidence.runtime.database");
  assertExactObjectKeys(
    value.privacy,
    ["work", "staging_before_summary"],
    "evidence.runtime.privacy",
  );
  for (const scan of [value.privacy.work, value.privacy.staging_before_summary]) {
    assertExactObjectKeys(scan, PRIVATE_SCAN_KEYS, "evidence.runtime.privacy_scan");
    requireCondition(
      Number.isInteger(scan.files_scanned)
        && scan.files_scanned > 0
        && scan.plaintext_matches === 0
        && scan.sqlite_sidecars_remaining === 0,
      "evidence.runtime.privacy_scan",
      "contract",
    );
  }
  requireCondition(
    value.schema_version === "prd077-browser-harness-v2"
      && value.status === "passed"
      && value.gateway_http_200 === true
      && value.control_http_200 === true
      && value.console_http_200 === true
      && value.finance_source_permission_revoked === true
      && value.base_permission_retained === true
      && value.backend_denied_after_source_revoke === true
      && value.owned_processes_remaining === 0
      && value.database.latest_migration === "0070"
      && value.database.migration_count === 70
      && value.database.integrity_check === "ok"
      && value.database.foreign_key_violations === 0
      && value.all_108_prds_complete === false,
    "evidence.runtime",
    "contract",
  );
  return true;
}


/** JSON 证据不得包含本轮私有值、角色账号或问题固定文案。 */
function assertSafeEvidence(value, privateValues) {
  assertEvidenceKeys(value);
  const serialized = JSON.stringify(value);
  const forbiddenValues = [
    ...privateValues,
    ...Object.values(ROLE_ACCOUNTS).map((account) => account.email),
    ...Object.values(QUERY_CASES).map((item) => item.text),
  ];
  requireCondition(
    forbiddenValues.every((item) => !item || !serialized.includes(item)),
    "evidence.values",
    "private_value",
  );
  return true;
}


/** 以 UTF-8、UTF-16、URL、Base64 和 hex 扫描制品，不返回命中内容或文件名。 */
function privateRepresentations(value) {
  const text = String(value ?? "");
  const utf8 = Buffer.from(text, "utf8");
  const utf16le = Buffer.from(text, "utf16le");
  const base64 = utf8.toString("base64");
  return [
    utf8,
    utf16le,
    Buffer.from(utf16le).swap16(),
    Buffer.from(encodeURIComponent(text), "ascii"),
    Buffer.from(base64, "ascii"),
    Buffer.from(base64.replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, ""), "ascii"),
    Buffer.from(utf8.toString("hex"), "ascii"),
  ];
}


/** 为 live work 冻结严格值、来源叶值和精确 SQLite 文件族，供生产与合同共用。 */
function buildLiveWorkPrivacyScope(privateConfig, tracker, workDirectory) {
  const configurationValues = Object.values(privateConfig);
  requireCondition(
    configurationValues.length === PRIVATE_CONFIGURATION_LABELS.length,
    "privacy.work_live",
    "configuration",
  );
  const strictEntries = [
    ...configurationValues.map((value, index) => ({
      value,
      label: PRIVATE_CONFIGURATION_LABELS[index],
    })),
    ...[...tracker.browserTokens].map((value) => ({ value, label: "browser_token" })),
    ...[...tracker.answerSerializations].map(
      (value) => ({ value, label: "answer_response" }),
    ),
  ];
  const sourceEntries = [...tracker.answerLeafValues]
    .map((value) => ({ value, label: "answer_leaf" }));
  const businessDatabase = path.join(workDirectory, "business.sqlite3");
  requireCondition(existsSync(businessDatabase), "privacy.work_live", "source_database");
  const authoritativeSourceFiles = [
    businessDatabase,
    ...["-wal", "-shm", "-journal"].map((suffix) => `${businessDatabase}${suffix}`),
  ];
  return Object.freeze({
    privateValues: Object.freeze(strictEntries.map(({ value }) => value)),
    privateLabels: Object.freeze(strictEntries.map(({ label }) => label)),
    authoritativeSourceFiles: Object.freeze(authoritativeSourceFiles),
    sourceScopedValues: Object.freeze(sourceEntries.map(({ value }) => value)),
    sourceScopedLabels: Object.freeze(sourceEntries.map(({ label }) => label)),
  });
}


/**
 * 递归扫描私有目录；只有精确权威源文件可豁免来源叶值，其他值和文件始终零容忍。
 */
async function scanPrivateTree(
  directory,
  privateValues,
  stage,
  privateLabels = [],
  {
    authoritativeSourceFiles = [],
    sourceScopedValues = [],
    sourceScopedLabels = [],
    scanHooks = {},
    treeAlreadyStable = false,
  } = {},
) {
  const root = path.resolve(directory);
  const rootIdentity = await captureDirectoryIdentity(root, stage);
  if (!treeAlreadyStable) {
    return withStableDirectoryTree(root, rootIdentity, stage, () => scanPrivateTree(
      root,
      privateValues,
      stage,
      privateLabels,
      {
        authoritativeSourceFiles,
        sourceScopedValues,
        sourceScopedLabels,
        scanHooks,
        treeAlreadyStable: true,
      },
    ));
  }
  /** 将路径规范化为平台稳定坐标，Windows 比较时消除大小写差异。 */
  const sourceCoordinate = (value) => {
    const normalized = path.normalize(path.resolve(value));
    return process.platform === "win32" ? normalized.toLowerCase() : normalized;
  };
  requireCondition(
    sourceScopedLabels.length === 0 || sourceScopedLabels.length === sourceScopedValues.length,
    stage,
    "source_scope",
  );
  const authoritativeSources = new Set();
  for (const sourceFile of authoritativeSourceFiles) {
    const target = path.resolve(sourceFile);
    const relative = path.relative(root, target);
    requireCondition(
      relative && !path.isAbsolute(relative) && !relative.startsWith(`..${path.sep}`),
      stage,
      "source_scope",
    );
    authoritativeSources.add(sourceCoordinate(target));
  }
  const strictEntries = privateValues.map((value, index) => ({
    label: privateLabels[index] ?? "",
    representations: privateRepresentations(value),
  }));
  const sourceScopedEntries = sourceScopedValues.map((value, index) => ({
    label: sourceScopedLabels[index] ?? "",
    representations: privateRepresentations(value),
  }));
  let filesScanned = 0;
  let plaintextMatches = 0;
  let firstMatchLabel = "";
  /** 执行单次扫描 I/O，并将平台异常折叠为调用方提供的固定错误码。 */
  async function scanIo(operation, code) {
    try {
      return await operation();
    } catch {
      throw new BrowserAcceptanceFailure(stage, code);
    }
  }
  /** 执行只读竞态钩子，钩子失败时按身份不稳定拒绝继续扫描。 */
  const scanHook = async (name, context) => {
    const operation = scanHooks?.[name];
    if (typeof operation !== "function") return;
    try {
      await operation(Object.freeze(context));
    } catch {
      // Windows 稳定树锁拒绝竞态注入也属于预期的失败关闭，不暴露平台错误正文。
      throw new BrowserAcceptanceFailure(stage, "identity");
    }
  };
  async function regularFileIdentity(target, coordinate) {
    const information = await scanIo(
      () => lstat(target, { bigint: true }),
      `${coordinate}_lstat`,
    );
    requireCondition(
      information.isFile() && !information.isSymbolicLink(),
      stage,
      "unexpected_type",
    );
    const canonical = await scanIo(() => realpath(target), `${coordinate}_realpath`);
    const relative = path.relative(root, canonical);
    requireCondition(
      relative && !path.isAbsolute(relative) && !relative.startsWith(`..${path.sep}`),
      stage,
      "outside_root",
    );
    return Object.freeze({
      ...directoryIdentityFromStat(information),
      size: String(information.size),
      mtime_ns: String(information.mtimeNs),
      ctime_ns: String(information.ctimeNs),
    });
  }
  /** 比较文件读取前后的完整身份和内容元数据，任何漂移都视为竞态。 */
  const sameIdentity = (left, right) => [
    "device",
    "inode",
    "birthtime_ns",
    "size",
    "mtime_ns",
    "ctime_ns",
  ].every(
    (key) => left?.[key] === right?.[key],
  );
  /** 递归访问稳定目录树，逐节点绑定身份并扫描敏感值的所有编码表示。 */
  async function visit(current, expectedIdentity) {
    await scanHook("beforeDirectoryRead", { current, expectedIdentity });
    await assertDirectoryIdentity(current, expectedIdentity, stage);
    const children = await scanIo(
      () => readdir(current, { withFileTypes: true }),
      "directory_read",
    );
    await assertDirectoryIdentity(current, expectedIdentity, stage);
    await scanHook("afterDirectoryRead", { current, expectedIdentity, children });
    for (const entry of children) {
      const target = path.join(current, entry.name);
      const information = await scanIo(
        () => lstat(target, { bigint: true }),
        "entry_lstat",
      );
      requireCondition(!information.isSymbolicLink(), stage, "link");
      const canonical = await scanIo(() => realpath(target), "entry_realpath");
      const relative = path.relative(root, canonical);
      requireCondition(
        relative && !path.isAbsolute(relative) && !relative.startsWith(`..${path.sep}`),
        stage,
        "outside_root",
      );
      if (information.isDirectory()) {
        const childIdentity = directoryIdentityFromStat(information);
        await scanHook("beforeDirectoryOpen", { target, expectedIdentity: childIdentity });
        await assertDirectoryIdentity(target, childIdentity, stage);
        await visit(target, childIdentity);
        await assertDirectoryIdentity(target, childIdentity, stage);
        continue;
      }
      requireCondition(information.isFile(), stage, "unexpected_type");
      const initialIdentity = Object.freeze({
        ...directoryIdentityFromStat(information),
        size: String(information.size),
        mtime_ns: String(information.mtimeNs),
        ctime_ns: String(information.ctimeNs),
      });
      requireCondition(information.size <= 64n * 1024n * 1024n, stage, "file_too_large");
      await scanHook("beforeFileOpen", { target, expectedIdentity: initialIdentity });
      const handle = await scanIo(() => open(target, "r"), "file_open");
      let content;
      try {
        const openedInformation = await scanIo(
          () => handle.stat({ bigint: true }),
          "file_fstat",
        );
        const opened = Object.freeze({
          ...directoryIdentityFromStat(openedInformation),
          size: String(openedInformation.size),
          mtime_ns: String(openedInformation.mtimeNs),
          ctime_ns: String(openedInformation.ctimeNs),
        });
        requireCondition(sameIdentity(opened, initialIdentity), stage, "identity");
        content = await scanIo(() => handle.readFile(), "file_read");
        await scanHook("afterFileRead", { target, expectedIdentity: initialIdentity });
        const readInformation = await scanIo(
          () => handle.stat({ bigint: true }),
          "file_fstat",
        );
        const readIdentity = Object.freeze({
          ...directoryIdentityFromStat(readInformation),
          size: String(readInformation.size),
          mtime_ns: String(readInformation.mtimeNs),
          ctime_ns: String(readInformation.ctimeNs),
        });
        requireCondition(sameIdentity(readIdentity, initialIdentity), stage, "identity");
        const pathIdentity = await regularFileIdentity(target, "file_after_read");
        requireCondition(sameIdentity(pathIdentity, initialIdentity), stage, "identity");
      } finally {
        await scanIo(() => handle.close(), "file_close");
      }
      filesScanned += 1;
      const entries = authoritativeSources.has(sourceCoordinate(target))
        ? strictEntries
        : [...strictEntries, ...sourceScopedEntries];
      for (const { representations, label } of entries) {
        if (representations.some((needle) => content.includes(needle))) {
          plaintextMatches += 1;
          if (!firstMatchLabel && SAFE_COORDINATE.test(String(label))) {
            firstMatchLabel = String(label);
          }
        }
      }
    }
    await assertDirectoryIdentity(current, expectedIdentity, stage);
  }
  await visit(root, rootIdentity);
  await assertDirectoryIdentity(root, rootIdentity, stage);
  requireCondition(
    plaintextMatches === 0,
    firstMatchLabel ? `${stage}.${firstMatchLabel}` : stage,
    "plaintext_found",
  );
  return Object.freeze({
    files_scanned: filesScanned,
    plaintext_matches: plaintextMatches,
  });
}


/** 扫描最终 artifact 普通文件并返回纯计数；链接、超大文件或任一明文命中均失败。 */
async function scanArtifacts(
  artifactDirectory,
  privateValues,
  ownershipClaim = null,
  { treeAlreadyStable = false, scanHooks = {} } = {},
) {
  const identity = await captureDirectoryIdentity(artifactDirectory, "privacy.artifact");
  if (!treeAlreadyStable) {
    return withStableDirectoryTree(
      artifactDirectory,
      identity,
      "privacy.artifact",
      () => scanArtifacts(artifactDirectory, privateValues, ownershipClaim, {
        treeAlreadyStable: true,
        scanHooks,
      }),
    );
  }
  if (ownershipClaim) {
    await assertOwnershipClaim(
      artifactDirectory,
      ownershipClaim,
      "privacy.artifact_owner",
      identity,
    );
  }
  const files = [];
  for (const entry of await readdir(artifactDirectory, { withFileTypes: true })) {
    if (ownershipClaim && entry.name === OWNER_CLAIM_FILENAME) continue;
    const target = path.join(artifactDirectory, entry.name);
    const information = await lstat(target);
    requireCondition(!information.isSymbolicLink(), "privacy.artifact", "link");
    requireCondition(information.isFile(), "privacy.artifact", "unexpected_directory");
    requireCondition(information.size <= 32 * 1024 * 1024, "privacy.artifact", "file_too_large");
    files.push(target);
  }
  let matches = 0;
  for (const file of files) {
    try {
      if (typeof scanHooks.beforeFileRead === "function") {
        await scanHooks.beforeFileRead(Object.freeze({ target: file }));
      }
    } catch {
      throw new BrowserAcceptanceFailure("privacy.artifact", "identity");
    }
    let content;
    try {
      const handle = await open(file, "r");
      try {
        const before = await handle.stat({ bigint: true });
        requireCondition(
          before.isFile() && !before.isSymbolicLink(),
          "privacy.artifact",
          "unexpected_type",
        );
        content = await handle.readFile();
        const after = await handle.stat({ bigint: true });
        requireCondition(
          before.dev === after.dev
            && before.ino === after.ino
            && before.birthtimeNs === after.birthtimeNs
            && before.size === after.size
            && before.mtimeNs === after.mtimeNs
            && before.ctimeNs === after.ctimeNs,
          "privacy.artifact",
          "identity",
        );
      } finally {
        await handle.close();
      }
    } catch (error) {
      // 已标准化隐私错误保留精确坐标，未知读取异常统一隐藏文件系统正文。
      if (error instanceof BrowserAcceptanceFailure) throw error;
      throw new BrowserAcceptanceFailure("privacy.artifact", "file_read");
    }
    for (const value of privateValues) {
      if (privateRepresentations(value).some((needle) => content.includes(needle))) matches += 1;
    }
  }
  requireCondition(matches === 0, "privacy.artifact", "plaintext_found");
  return Object.freeze({ files_scanned: files.length, plaintext_matches: matches });
}


/** 写入稳定排序的 UTF-8 JSON；调用前必须完成键和值审计。 */
async function writeSafeJson(target, value, privateValues) {
  assertSafeEvidence(value, privateValues);
  await writeFile(target, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}


/** 计算单个已发布普通文件 SHA-256，不接受目录、链接或动态路径。 */
async function fileDigest(artifactDirectory, filename) {
  requireCondition(/^[a-z0-9][a-z0-9.-]{2,80}$/.test(filename), "manifest.file", "name");
  const target = path.join(artifactDirectory, filename);
  const information = await lstat(target);
  requireCondition(information.isFile() && !information.isSymbolicLink(), "manifest.file", "type");
  const content = await readFile(target);
  return Object.freeze({
    path: filename,
    bytes: content.length,
    sha256: createHash("sha256").update(content).digest("hex"),
  });
}


/** 生成覆盖两张 PNG 与 Browser/Runtime 摘要的稳定 SHA-256 manifest。 */
async function buildManifest(artifactDirectory, filenames, sourceProvenance) {
  assertSourceProvenance(sourceProvenance);
  const files = [];
  for (const filename of [...filenames].sort()) {
    files.push(await fileDigest(artifactDirectory, filename));
  }
  return Object.freeze({
    schema_version: "prd077-browser-sha256-v1",
    status: "passed",
    prd_id: "PRD-077",
    page_id: "PAGE-107",
    files,
    source_provenance: sourceProvenance,
    all_108_prds_complete: false,
  });
}


/** 复读 manifest、核对 exact inventory，并对 staging/final 中每个发布文件重算摘要。 */
async function verifyPublishedManifest(
  artifactDirectory,
  filenames,
  sourceProvenance,
  ownershipClaim = null,
  { treeAlreadyStable = false, scanHooks = {} } = {},
) {
  const identity = await captureDirectoryIdentity(artifactDirectory, "manifest.directory");
  if (!treeAlreadyStable) {
    return withStableDirectoryTree(
      artifactDirectory,
      identity,
      "manifest.directory",
      () => verifyPublishedManifest(
        artifactDirectory,
        filenames,
        sourceProvenance,
        ownershipClaim,
        { treeAlreadyStable: true, scanHooks },
      ),
    );
  }
  if (ownershipClaim) {
    await assertOwnershipClaim(
      artifactDirectory,
      ownershipClaim,
      "manifest.owner_claim",
      identity,
    );
  }
  const expectedFiles = [...filenames].sort();
  const expectedInventory = [
    ...expectedFiles,
    "sha256-manifest.json",
    ...(ownershipClaim ? [OWNER_CLAIM_FILENAME] : []),
  ].sort();
  const entries = await readdir(artifactDirectory, { withFileTypes: true });
  requireCondition(
    entries.every((entry) => entry.isFile() && !entry.isSymbolicLink())
      && JSON.stringify(entries.map((entry) => entry.name).sort())
        === JSON.stringify(expectedInventory),
    "manifest.inventory",
    "files",
  );
  let manifest;
  try {
    if (typeof scanHooks.beforeManifestRead === "function") {
      await scanHooks.beforeManifestRead(Object.freeze({ artifactDirectory }));
    }
    manifest = JSON.parse(
      await readFile(path.join(artifactDirectory, "sha256-manifest.json"), "utf8"),
    );
  } catch {
    throw new BrowserAcceptanceFailure("manifest.readback", "json");
  }
  assertExactObjectKeys(manifest, MANIFEST_KEYS, "manifest.readback");
  assertSourceProvenance(manifest.source_provenance, "manifest.provenance");
  requireCondition(
    manifest.schema_version === "prd077-browser-sha256-v1"
      && manifest.status === "passed"
      && manifest.prd_id === "PRD-077"
      && manifest.page_id === "PAGE-107"
      && manifest.all_108_prds_complete === false
      && SOURCE_PROVENANCE_NAMES.every(
        (name) => manifest.source_provenance[name] === sourceProvenance[name],
      )
      && Array.isArray(manifest.files)
      && manifest.files.length === expectedFiles.length,
    "manifest.readback",
    "contract",
  );
  const actualPaths = [];
  for (const item of manifest.files) {
    assertExactObjectKeys(item, ["path", "bytes", "sha256"], "manifest.file");
    try {
      if (typeof scanHooks.beforeFileDigest === "function") {
        await scanHooks.beforeFileDigest(Object.freeze({
          target: path.join(artifactDirectory, item.path),
          filename: item.path,
        }));
      }
    } catch {
      throw new BrowserAcceptanceFailure("manifest.file", "identity");
    }
    const actual = await fileDigest(artifactDirectory, item.path);
    requireCondition(
      item.path === actual.path
        && item.bytes === actual.bytes
        && item.sha256 === actual.sha256,
      "manifest.file",
      "digest",
    );
    actualPaths.push(item.path);
  }
  requireCondition(
    JSON.stringify(actualPaths) === JSON.stringify(expectedFiles),
    "manifest.file",
    "paths",
  );
  return Object.freeze({ files_verified: expectedInventory.length });
}


/** 在同一 artifact 根内原子发布 staging；rename 失败不得生成 final。 */
async function publishStagingAtomically(
  stagingDirectory,
  artifactDirectory,
  {
    moveOperation = moveVerifiedDirectory,
    afterMoveLocked = async () => undefined,
    validateFinal = async () => undefined,
    ownershipClaim = null,
    expectedIdentity = null,
  } = {},
) {
  const observedStagingIdentity = await captureDirectoryIdentity(
    stagingDirectory,
    "evidence.staging",
  );
  if (expectedIdentity) {
    requireCondition(
      observedStagingIdentity.device === expectedIdentity.device
        && observedStagingIdentity.inode === expectedIdentity.inode
        && observedStagingIdentity.birthtime_ns === expectedIdentity.birthtime_ns,
      "evidence.staging",
      "identity",
    );
  }
  const stagingIdentity = expectedIdentity ?? observedStagingIdentity;
  if (ownershipClaim) {
    await assertOwnershipClaim(
      stagingDirectory,
      ownershipClaim,
      "evidence.staging_owner",
      stagingIdentity,
    );
  }
  requireCondition(
    !(await pathEntryExists(artifactDirectory, "evidence.final")),
    "evidence.final",
    "exists",
  );
  const artifactRootIdentity = await captureDirectoryIdentity(
    path.dirname(artifactDirectory),
    "evidence.artifact_root",
  );
  let moved = false;
  try {
    await moveOperation(
      stagingDirectory,
      artifactDirectory,
      stagingIdentity,
      "evidence.publish",
      {
        destinationParentIdentity: artifactRootIdentity,
        afterLocked: afterMoveLocked,
      },
    );
    moved = true;
    requireCondition(
      await pathEntryExists(artifactDirectory, "evidence.publish")
        && !(await pathEntryExists(stagingDirectory, "evidence.publish")),
      "evidence.publish",
      "rename",
    );
    await assertDirectoryIdentity(artifactDirectory, stagingIdentity, "evidence.final");
    if (ownershipClaim) {
      await assertOwnershipClaim(
        artifactDirectory,
        ownershipClaim,
        "evidence.final_owner",
        stagingIdentity,
      );
    }
    const result = await validateFinal(artifactDirectory);
    if (ownershipClaim) {
      await removeOwnershipClaim(
        artifactDirectory,
        ownershipClaim,
        "evidence.publish_owner",
        stagingIdentity,
      );
      await assertDirectoryIdentity(
        artifactDirectory,
        stagingIdentity,
        "evidence.published",
      );
    }
    return result;
  } catch (error) {
    const finalExists = await pathEntryExists(artifactDirectory, "evidence.publish");
    const ownsFinal = finalExists
      && await directoryIdentityMatches(artifactDirectory, stagingIdentity)
      && (
        !ownershipClaim
        || await ownershipClaimMatches(
          artifactDirectory,
          ownershipClaim,
          stagingIdentity,
        )
      );
    if (ownsFinal) {
      try {
        if (ownershipClaim) {
          await assertOwnershipClaim(
            artifactDirectory,
            ownershipClaim,
            "evidence.publish.cleanup_owner",
            stagingIdentity,
          );
        }
        await removeOwnedDirectory(
          artifactDirectory,
          path.dirname(artifactDirectory),
          "evidence.publish.cleanup",
          {
            expectedIdentity: stagingIdentity,
            ownershipClaim,
            claimIdentity: stagingIdentity,
          },
        );
      } catch {
        throw new BrowserAcceptanceFailure("evidence.publish", "cleanup");
      }
    } else if (ownershipClaim && moved && finalExists) {
      // 同名 final 已被替换或 claim 已漂移，宁可保留并失败，也不能递归删除未知 owner。
      throw new BrowserAcceptanceFailure("evidence.publish", "owner_claim");
    }
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(
      "evidence.publish",
      ownsFinal ? "validation" : "rename",
    );
  }
}


/** 失败路径只删除已经由本轮 harness/rename 明确取得所有权的目录。 */
async function cleanupFailedRunDirectories({
  workDirectory,
  workRoot = WORK_ROOT,
  stagingDirectory,
  artifactDirectory,
  artifactRoot = ARTIFACT_ROOT,
  runDirectoriesOwned,
  artifactOwned,
  ownershipClaim = null,
  workIdentity = null,
  stagingIdentity = null,
  artifactIdentity = null,
  takeoverAfterDrain = false,
  processTreeDrained = false,
  browserTreeDrained = true,
}) {
  let firstFailure = null;
  let removed = 0;
  const candidates = [
    {
      directory: workDirectory,
      root: workRoot,
      stage: "cleanup.failed_work",
      identity: runDirectoriesOwned ? workIdentity : null,
      eligible: runDirectoriesOwned || takeoverAfterDrain,
      treeDrained: browserTreeDrained,
    },
    {
      directory: stagingDirectory,
      root: artifactRoot,
      stage: "cleanup.failed_staging",
      identity: runDirectoriesOwned ? stagingIdentity : null,
      eligible: runDirectoriesOwned || takeoverAfterDrain,
      treeDrained: true,
    },
    {
      directory: artifactDirectory,
      root: artifactRoot,
      stage: "cleanup.failed_artifact",
      identity: artifactOwned ? artifactIdentity : null,
      eligible: artifactOwned,
      treeDrained: true,
    },
  ];
  if (!candidates.some((candidate) => candidate.eligible)) {
    return Object.freeze({ directories_removed: 0 });
  }
  requireCondition(processTreeDrained, "cleanup.process_tree", "not_drained");
  for (const candidate of candidates) {
    if (!candidate.eligible) continue;
    try {
      if (!(await pathEntryExists(candidate.directory, candidate.stage))) continue;
      requireCondition(candidate.treeDrained, candidate.stage, "tree_not_drained");
      let identity = candidate.identity;
      if (!identity && takeoverAfterDrain && !runDirectoriesOwned) {
        // ready 前失败只能在 child close/tree drain 后逐目录重新取得 exact claim 所有权。
        identity = await captureDirectoryIdentity(candidate.directory, `${candidate.stage}_owner`);
        await assertOwnershipClaim(
          candidate.directory,
          ownershipClaim,
          `${candidate.stage}_owner`,
          identity,
        );
      }
      requireCondition(identity, `${candidate.stage}_owner`, "identity");
      await removeOwnedDirectory(candidate.directory, candidate.root, candidate.stage, {
        expectedIdentity: identity,
        ownershipClaim,
        claimIdentity: identity,
      });
      removed += 1;
    } catch (error) {
      // 一个 owner 失效不能阻止其它独立目录做 best-effort 安全清理；最终保留最早固定坐标。
      firstFailure ??= safeFailure(error, candidate.stage);
    }
  }
  if (firstFailure) throw firstFailure;
  return Object.freeze({ directories_removed: removed });
}


/** 汇总网络、Console 与隐私终态；任何计数漂移都在写证据前失败关闭。 */
function assertTracker(tracker) {
  const successes = tracker.assistantStatuses.filter((status) => status === 200).length;
  const denials = tracker.assistantStatuses.filter((status) => status === 403).length;
  requireCondition(
    successes === 6 && tracker.assistantStatuses.length === 9,
    "browser.network",
    "assistant_success_coverage",
  );
  requireCondition(denials === 3, "browser.network", "assistant_denial_coverage");
  requireCondition(
    tracker.canceledAssistantRequestCount === 1
      && tracker.delayedLoadingFailureObserved === true
      && tracker.expectedCanceledRequestIds.size === 0,
    "browser.network",
    "assistant_cancel_coverage",
  );
  requireCondition(tracker.loginStatuses.length === 4, "browser.network", "login_count");
  requireCondition(tracker.logoutStatuses.length === 4, "browser.network", "logout_count");
  requireCondition(
    tracker.loginStatuses.every((status) => status === 200)
      && tracker.logoutStatuses.every((status) => status === 200)
      && tracker.browserTokens.size === tracker.loginStatuses.length
      && tracker.navigationStatuses.length > 0
      && tracker.navigationStatuses.every((status) => status === 200),
    "browser.network",
    "status",
  );
  requireCondition(tracker.externalRequestCount === 0, "browser.network", "external");
  requireCondition(tracker.forbiddenQueryCount === 0, "browser.network", "query_private");
  requireCondition(tracker.requestPrivacyViolationCount === 0, "browser.network", "url_private");
  requireCondition(tracker.assistantHeaderFailureCount === 0, "browser.network", "headers");
  requireCondition(tracker.requestOwners.size === 0, "browser.network", "owners_remaining");
  if (tracker.unexpectedNetworkFailureCount !== 0) {
    const failure = tracker.unexpectedNetworkFailure ?? {
      phase: "unknown",
      category: "unowned",
      reason: "other",
    };
    throw new BrowserAcceptanceFailure(
      `browser.network.${failure.phase}.${failure.category}`,
      `loading_${failure.reason}`,
    );
  }
  requireCondition(
    tracker.healthProbeFailureCount === tracker.healthProbeRecoveryCount
      && tracker.pendingHealthRecoveries.size === 0
      && tracker.earlyHealthRecoveries.size === 0,
    "browser.network",
    "health_recovery",
  );
  const consoleNetworkFailureCount = Number(tracker.consoleNetworkFailureCount ?? 0);
  const expectedNetworkFailureCeiling = tracker.canceledAssistantRequestCount
    + tracker.healthProbeFailureCount;
  const consoleFailuresReconciled = tracker.exceptionCount === 0
    && tracker.unexpectedConsoleErrorCount === consoleNetworkFailureCount
    && consoleNetworkFailureCount <= expectedNetworkFailureCeiling;
  if (!consoleFailuresReconciled) {
    const failure = tracker.unexpectedConsoleFailure ?? {
      phase: "unknown",
      source: "log",
      reason: "other",
    };
    throw new BrowserAcceptanceFailure(
      `browser.console.${failure.phase}.${failure.source}.${failure.reason}`,
      "unexpected",
    );
  }
  requireCondition(
    tracker.pendingConsoleInspections.size === 0
      && tracker.consoleInspectionFailureCount === 0
      && tracker.inspectionsSealed === true
      && Number(tracker.lateInspectionFailureCount ?? 0) === 0,
    "browser.console",
    "inspection",
  );
  requireCondition(
    tracker.pendingAnswerInspections.size === 0
      && tracker.answerInspectionFailureCount === 0
      && tracker.answerResponseCount === successes
      && tracker.answerSerializations instanceof Set
      && tracker.answerSerializations.size >= successes
      && tracker.answerSerializations.size <= successes * 4
      && tracker.answerResponsesByQuery instanceof Map
      && tracker.answerResponsesByQuery.size === Object.keys(EXPECTED_RESPONSE_QUERY_COUNTS).length
      && Object.entries(EXPECTED_RESPONSE_QUERY_COUNTS).every(
        ([queryType, count]) => tracker.answerResponsesByQuery.get(queryType) === count,
      ),
    "browser.answer_response",
    "coverage",
  );
  const expectedAnswerQueries = Object.keys(QUERY_CASES).sort();
  const observedAnswerQueries = tracker.answerLeavesByQuery instanceof Map
    ? [...tracker.answerLeavesByQuery.keys()].sort()
    : [];
  requireCondition(
    JSON.stringify(observedAnswerQueries) === JSON.stringify(expectedAnswerQueries)
      && tracker.answerLeafValues instanceof Set
      && tracker.answerLeafValues.size >= expectedAnswerQueries.length
      && tracker.answerLeafValues.size <= ANSWER_LEAF_MAX_COUNT
      && expectedAnswerQueries.every((name) => {
        const leaves = tracker.answerLeavesByQuery.get(name);
        return leaves instanceof Set
          && leaves.size > 0
          && [...leaves].every(
            (value) => value.length >= ANSWER_LEAF_MIN_LENGTH
              && value.length <= ANSWER_LEAF_MAX_LENGTH
              && tracker.answerLeafValues.has(value),
          );
      }),
    "browser.privacy",
    "answer_leaf_coverage",
  );
  requireCondition(
    tracker.processPrivacyViolationCount === 0 && tracker.edgeProcessErrorCount === 0,
    "browser.process",
    "error",
  );
  return Object.freeze({
    assistant_http_200: successes,
    assistant_http_403: denials,
    canceled_assistant_requests: tracker.canceledAssistantRequestCount,
    login_http_200: tracker.loginStatuses.length,
    logout_http_200: tracker.logoutStatuses.length,
    navigation_http_200: tracker.navigationStatuses.length,
    expected_console_errors: tracker.expectedConsoleErrorCount + consoleNetworkFailureCount,
    unexpected_console_errors: 0,
    external_requests: tracker.externalRequestCount,
    private_url_violations: tracker.requestPrivacyViolationCount,
    private_header_failures: tracker.assistantHeaderFailureCount,
    recovered_health_probe_failures: tracker.healthProbeRecoveryCount,
    answer_query_types_scanned: observedAnswerQueries.length,
    answer_leaf_values_scanned: tracker.answerLeafValues.size,
    answer_response_bodies_scanned: tracker.answerResponseCount,
    answer_response_serializations_scanned: tracker.answerSerializations.size,
  });
}


/** 执行完整 Browser 验收并在成功时保留 artifact、失败时精确清理本轮 owner。 */
async function executeAcceptance(options) {
  const runId = safeRunId(options.runId);
  const workDirectory = ownedRunDirectory(WORK_ROOT, runId);
  const stagingDirectory = ownedStagingDirectory(ARTIFACT_ROOT, runId);
  const artifactDirectory = ownedRunDirectory(ARTIFACT_ROOT, runId);
  const browserDirectory = path.join(workDirectory, "browser");
  requireCondition(existsSync(PYTHON), "config.python", "missing");
  requireCondition(existsSync(CHROME), "config.chrome", "missing");
  requireCondition(existsSync(HARNESS), "config.harness", "missing");
  await assertBrowserExecutable();
  const [workExists, stagingExists, artifactExists] = await Promise.all([
    pathEntryExists(workDirectory, "config.run_directory"),
    pathEntryExists(stagingDirectory, "config.run_directory"),
    pathEntryExists(artifactDirectory, "config.run_directory"),
  ]);
  requireCondition(
    !workExists && !stagingExists && !artifactExists,
    "config.run_directory",
    "exists",
  );
  await assertPortFree(DEBUG_PORT);

  const privateConfig = createPrivateConfiguration();
  const ownershipClaim = createOwnershipClaim();
  const privateValues = Object.freeze(Object.values(privateConfig));
  const initialSourceProvenance = await captureSourceProvenance();
  const tracker = createTracker(privateValues);
  const harness = startHarness(runId, privateConfig, ownershipClaim, tracker);
  let harnessReady = false;
  let harnessStopped = false;
  let harnessTreeDrained = false;
  let browserChild = null;
  let browserLaunched = false;
  let browserTreeDrained = true;
  let browserClient = null;
  let pageClient = null;
  let pageSession = null;
  let browserProfilePrivacy = null;
  let liveWorkPrivacy = null;
  let acceptancePrivateValues = privateValues;
  let browserProfileScanAttempted = false;
  let runDirectoriesOwned = false;
  let artifactOwned = false;
  let workIdentity = null;
  let stagingIdentity = null;
  let browserIdentity = null;
  let artifactIdentity = null;
  let completed = false;
  let primaryFailure = null;
  try {
    const readyPayload = await harness.ready;
    harnessReady = true;
    requireCondition(
      readyPayload?.work
        === path.relative(ROOT, workDirectory).split(path.sep).join("/")
        && readyPayload?.owner_claim_sha256 === ownershipClaim.digest
        && readyPayload?.staging
          === path.relative(ROOT, stagingDirectory).split(path.sep).join("/"),
      "harness.ready",
      "ownership",
    );
    requireCondition(
      readyPayload?.staging
        === path.relative(ROOT, stagingDirectory).split(path.sep).join("/"),
      "harness.ready",
      "staging",
    );
    workIdentity = await captureDirectoryIdentity(workDirectory, "config.work_directory");
    stagingIdentity = await captureDirectoryIdentity(
      stagingDirectory,
      "config.staging_directory",
    );
    browserIdentity = await captureDirectoryIdentity(
      browserDirectory,
      "config.browser_directory",
    );
    await assertOwnershipClaim(
      workDirectory,
      ownershipClaim,
      "config.work_owner",
      workIdentity,
    );
    await assertOwnershipClaim(
      stagingDirectory,
      ownershipClaim,
      "config.staging_owner",
      stagingIdentity,
    );
    requireCondition(
      !(await pathEntryExists(artifactDirectory, "config.artifact_directory")),
      "config.artifact_directory",
      "exists",
    );
    // 只有 ready 合同、三个普通目录和双 claim 全部复核后才能取得递归清理资格。
    runDirectoriesOwned = true;

    browserChild = launchEdge({
      edgePath: CHROME,
      debugPort: DEBUG_PORT,
      artifactDirectory: browserDirectory,
      incognito: true,
      // Chrome 151/Windows ARM64 会把自定义 USERPROFILE 与显式 Profile 误判为默认目录。
      // 仅省略该键；HOME、AppData、Temp 与 Profile 仍全部固定在本轮 work 内。
      omitUserProfile: true,
    }, tracker);
    observeChildDrain(browserChild);
    browserLaunched = true;
    browserTreeDrained = false;
    browserChild.stdout?.on(
      "data",
      (chunk) => inspectProcessChunk(chunk, tracker, "browser_stdout"),
    );
    browserChild.stderr?.on(
      "data",
      (chunk) => inspectProcessChunk(chunk, tracker, "browser_stderr"),
    );
    const debuggerUrl = await waitForDebugger(DEBUG_PORT);
    browserClient = new CdpClient(debuggerUrl);
    await browserClient.connect();
    pageSession = await createPrivatePageSession(browserClient, DEBUG_PORT);
    pageClient = pageSession.client;
    bindBrowserGuards(pageClient, tracker);
    await setViewport(pageClient, pageSession.sessionId, VIEWPORTS.desktop);
    tracker.phase = "load";
    await loadConsole(pageClient, pageSession.sessionId);

    tracker.phase = "admin";
    const admin = await exerciseAdmin(
      pageClient,
      pageSession.sessionId,
      tracker,
      privateConfig,
    );
    tracker.phase = "finance";
    const finance = await exerciseFinance(
      pageClient,
      pageSession.sessionId,
      tracker,
      privateConfig,
    );
    tracker.phase = "security";
    const security = await exerciseSecurity(
      pageClient,
      pageSession.sessionId,
      tracker,
      privateConfig,
    );
    tracker.phase = "final.capture";
    const final = await captureFinalEvidence(
      pageClient,
      pageSession.sessionId,
      tracker,
      privateConfig,
      stagingDirectory,
    );
    tracker.phase = "final.network_drain";
    await waitForRequestOwnersDrain(tracker);
    tracker.phase = "final.console_inspections";
    await waitForConsoleInspections(tracker);
    tracker.phase = "final.tracker_assert";
    const network = assertTracker(tracker);
    acceptancePrivateValues = Object.freeze(trackerPrivateValues(tracker));

    tracker.phase = "final.page_dispose";
    await disposePrivatePageSession(browserClient, pageSession);
    pageSession = null;
    pageClient = null;
    assertSealedInspectionState(tracker);
    tracker.phase = "final.browser_close";
    const browserClose = await closeBrowserGracefully(browserClient, browserChild);
    requireCondition(
      browserClose.treeDrained === true && browserClose.stdioEof === true,
      "browser.close",
      "not_drained",
    );
    browserTreeDrained = true;
    browserChild = null;
    browserClient = null;
    browserProfileScanAttempted = true;
    tracker.phase = "final.profile_scan";
    browserProfilePrivacy = await scanPrivateTree(
      browserDirectory,
      acceptancePrivateValues,
      "privacy.browser_profile",
      [
        ...PRIVATE_CONFIGURATION_LABELS,
        ...[...tracker.browserTokens].map(() => "browser_token"),
        ...[...tracker.answerLeafValues].map(() => "answer_leaf"),
      ],
    );
    requireCondition(
      browserProfilePrivacy.files_scanned > 0,
      "privacy.browser_profile",
      "empty",
    );
    tracker.phase = "final.browser_remove";
    await removeBrowserDirectory(browserDirectory, workDirectory, {
      ownershipClaim,
      workIdentity,
      browserIdentity,
    });
    tracker.phase = "final.harness_quiesce";
    await quiesceHarness();
    const workPrivacyScope = buildLiveWorkPrivacyScope(
      privateConfig,
      tracker,
      workDirectory,
    );
    tracker.phase = "final.work_scan";
    await assertOwnershipClaim(
      workDirectory,
      ownershipClaim,
      "privacy.work_live_owner",
      workIdentity,
    );
    liveWorkPrivacy = await scanPrivateTree(
      workDirectory,
      workPrivacyScope.privateValues,
      "privacy.work_live",
      workPrivacyScope.privateLabels,
      {
        authoritativeSourceFiles: workPrivacyScope.authoritativeSourceFiles,
        sourceScopedValues: workPrivacyScope.sourceScopedValues,
        sourceScopedLabels: workPrivacyScope.sourceScopedLabels,
      },
    );
    requireCondition(liveWorkPrivacy.files_scanned > 0, "privacy.work_live", "empty");

    tracker.phase = "final.harness_stop";
    await stopHarness(harness, { requireSuccess: true });
    harnessStopped = true;
    harnessTreeDrained = true;
    tracker.phase = "final.process_scan";
    requireCondition(
      tracker.processPrivacyViolationCount === 0 && tracker.edgeProcessErrorCount === 0,
      "browser.process",
      "error",
    );
    tracker.phase = "final.staging_read";
    requireCondition(
      !(await pathEntryExists(workDirectory, "cleanup.work")),
      "cleanup.work",
      "remaining",
    );
    await assertDirectoryIdentity(stagingDirectory, stagingIdentity, "evidence.staging");
    await assertOwnershipClaim(
      stagingDirectory,
      ownershipClaim,
      "evidence.staging_owner",
      stagingIdentity,
    );
    requireCondition(
      !(await pathEntryExists(artifactDirectory, "evidence.final")),
      "evidence.final",
      "exists",
    );
    let runtimeSummary;
    try {
      runtimeSummary = JSON.parse(
        await readFile(path.join(stagingDirectory, "runtime-summary.json"), "utf8"),
      );
    } catch {
      throw new BrowserAcceptanceFailure("evidence.runtime", "json");
    }
    tracker.phase = "final.runtime_validate";
    assertRuntimeSummary(runtimeSummary);
    assertSafeEvidence(runtimeSummary, acceptancePrivateValues);

    const browserSummary = Object.freeze({
      schema_version: "prd077-browser-evidence-v2",
      status: "passed",
      prd_id: "PRD-077",
      page_id: "PAGE-107",
      workflow: Object.freeze({
        admin_query_types_answered: admin.answered,
        answer_canary_rendered_and_cleared: admin.answer_canary_rendered_and_cleared,
        source_navigation_verified: admin.source_navigation,
        read_only_denied: admin.read_only_denied,
        late_result_cleanup_verified: admin.late_cleanup,
        finance_allowed_and_denied: finance.allowed && finance.source_denied,
        security_allowed_and_denied: security.allowed && security.source_denied,
        base_permission_retained: finance.base_permission_retained,
        source_permission_revoked: finance.source_permission_revoked,
        backend_denied_after_source_revoke:
          runtimeSummary.backend_denied_after_source_revoke,
        dynamic_revoke_cleanup_verified: finance.dynamic_revoke_cleanup,
        logout_cleanup_verified:
          final.logoutPrivacy.root_count === 0
          && final.logoutPrivacy.local_storage_count === 0
          && final.logoutPrivacy.session_storage_count === 0
          && final.logoutPrivacy.session_keys_exact === false
          && final.logoutPrivacy.auth_token_known === false
          && final.logoutPrivacy.auth_context_valid === false
          && final.logoutPrivacy.question_text_hits === 0
          && final.logoutPrivacy.answer_text_hits === 0
          && final.logoutPrivacy.attribute_canary_hits === 0
          && final.logoutPrivacy.question_form_hits === 0
          && final.logoutPrivacy.answer_form_hits === 0,
      }),
      viewports: final.screenshots.map((item) => ({
        name: item.filename,
        layout: item.layout,
        pixels: item.pixels,
      })),
      network,
      privacy: Object.freeze({
        active_empty_page: final.activePrivacy,
        logged_out: final.logoutPrivacy,
        browser_profile: browserProfilePrivacy,
        work_live: liveWorkPrivacy,
        process_plaintext_matches: tracker.processPrivacyViolationCount,
      }),
      source_provenance: initialSourceProvenance,
      all_108_prds_complete: false,
    });
    assertExactObjectKeys(browserSummary, BROWSER_SUMMARY_KEYS, "evidence.browser");
    tracker.phase = "final.browser_summary_write";
    await writeSafeJson(
      path.join(stagingDirectory, "browser-summary.json"),
      browserSummary,
      acceptancePrivateValues,
    );

    const published = [
      "browser-summary.json",
      "runtime-summary.json",
      ...final.screenshots.map((item) => item.filename),
    ];
    tracker.phase = "final.manifest_build";
    const manifest = await buildManifest(
      stagingDirectory,
      published,
      initialSourceProvenance,
    );
    assertExactObjectKeys(manifest, MANIFEST_KEYS, "evidence.manifest");
    tracker.phase = "final.manifest_write";
    await writeSafeJson(
      path.join(stagingDirectory, "sha256-manifest.json"),
      manifest,
      acceptancePrivateValues,
    );
    tracker.phase = "final.manifest_verify";
    const stagedManifest = await verifyPublishedManifest(
      stagingDirectory,
      published,
      initialSourceProvenance,
      ownershipClaim,
    );
    tracker.phase = "final.artifact_scan";
    const artifactPrivacy = await scanArtifacts(
      stagingDirectory,
      acceptancePrivateValues,
      ownershipClaim,
    );
    requireCondition(
      artifactPrivacy.files_scanned === published.length + 1
        && stagedManifest.files_verified === published.length + 2,
      "privacy.artifact",
      "coverage",
    );
    tracker.phase = "final.provenance";
    const finalSourceProvenance = await captureSourceProvenance();
    assertSourceProvenanceUnchanged(initialSourceProvenance, finalSourceProvenance);
    tracker.phase = "final.publish";
    const { finalPrivacy } = await publishStagingAtomically(
      stagingDirectory,
      artifactDirectory,
      {
        ownershipClaim,
        expectedIdentity: stagingIdentity,
        validateFinal: async () => {
          const finalManifest = await verifyPublishedManifest(
            artifactDirectory,
            published,
            initialSourceProvenance,
            ownershipClaim,
          );
          const finalPrivacy = await scanArtifacts(
            artifactDirectory,
            acceptancePrivateValues,
            ownershipClaim,
          );
          requireCondition(
            finalPrivacy.files_scanned === artifactPrivacy.files_scanned
              && finalPrivacy.plaintext_matches === 0
              && finalManifest.files_verified === stagedManifest.files_verified,
            "privacy.final",
            "coverage",
          );
          return Object.freeze({ finalPrivacy });
        },
      },
    );
    artifactOwned = true;
    artifactIdentity = stagingIdentity;
    completed = true;
    return Object.freeze({
      status: "passed",
      prd_id: "PRD-077",
      page_id: "PAGE-107",
      screenshot_count: final.screenshots.length,
      artifact_file_count: finalPrivacy.files_scanned,
    });
  } catch (error) {
    const phaseStage = `browser.${String(tracker.phase ?? "acceptance")}`;
    primaryFailure = safeFailure(
      error,
      SAFE_COORDINATE.test(phaseStage) ? phaseStage : "browser.acceptance",
    );
    throw primaryFailure;
  } finally {
    let cleanupFailed = false;
    // 先销毁一次性页面与 Context，再关闭其余客户端和驱动拥有的进程。
    if (pageSession && browserClient) {
      try {
        await disposePrivatePageSession(browserClient, pageSession);
        pageClient = null;
      } catch {
        cleanupFailed = true;
      }
      pageSession = null;
    }
    if (pageClient) {
      try { pageClient.close(); } catch { cleanupFailed = true; }
      pageClient = null;
    }
    if (browserClient && browserChild) {
      try {
        const result = await closeBrowserGracefully(browserClient, browserChild);
        browserTreeDrained = result.treeDrained === true && result.stdioEof === true;
      } catch {
        cleanupFailed = true;
        try { browserClient.close(); } catch { cleanupFailed = true; }
        try {
          await terminateBrowserTree(browserChild);
          browserTreeDrained = true;
        } catch {
          cleanupFailed = true;
        }
      }
      browserClient = null;
      browserChild = null;
    } else if (browserClient) {
      try { browserClient.close(); } catch { cleanupFailed = true; }
      browserClient = null;
    } else if (browserChild) {
      try {
        await terminateBrowserTree(browserChild);
        browserTreeDrained = true;
      } catch {
        cleanupFailed = true;
      }
      browserChild = null;
    }
    let browserDirectoryExists = false;
    try {
      browserDirectoryExists = await pathEntryExists(browserDirectory, "cleanup.browser");
    } catch {
      cleanupFailed = true;
    }
    if (runDirectoriesOwned && browserDirectoryExists) {
      if (!browserTreeDrained) {
        // 未观察到 child close/stdio EOF 时，Profile 仍可能被后代写入，禁止扫描或递归删除。
        cleanupFailed = true;
      } else if (!browserProfileScanAttempted) {
        browserProfileScanAttempted = true;
        try {
          await assertOwnershipClaim(
            workDirectory,
            ownershipClaim,
            "privacy.browser_profile_owner",
            workIdentity,
          );
          await assertDirectoryIdentity(
            browserDirectory,
            browserIdentity,
            "privacy.browser_profile_identity",
          );
          await scanPrivateTree(
            browserDirectory,
            trackerPrivateValues(tracker),
            "privacy.browser_profile",
            [
              ...PRIVATE_CONFIGURATION_LABELS,
              ...[...tracker.browserTokens].map(() => "browser_token"),
              ...[...tracker.answerLeafValues].map(() => "answer_leaf"),
            ],
          );
        } catch {
          cleanupFailed = true;
        }
      }
      if (browserTreeDrained) {
        try {
          await removeBrowserDirectory(browserDirectory, workDirectory, {
            ownershipClaim,
            workIdentity,
            browserIdentity,
          });
        } catch {
          cleanupFailed = true;
        }
      }
    }
    if (!harnessStopped && browserLaunched && !browserTreeDrained) {
      // Browser 后代未归零时不得调用 /stop：harness 的成功 cleanup 会间接递归删除 Profile。
      try {
        await terminateHarnessTree(harness.child);
        harnessTreeDrained = true;
      } catch {
        cleanupFailed = true;
      }
    } else if (harnessReady && !harnessStopped) {
      try {
        await stopHarness(harness, { requireSuccess: false });
        harnessStopped = true;
        harnessTreeDrained = true;
      } catch {
        cleanupFailed = true;
        try {
          await waitForHarnessClose(harness, 1000);
          harnessTreeDrained = true;
        } catch {
          try {
            await terminateHarnessTree(harness.child);
            harnessTreeDrained = true;
          } catch {
            cleanupFailed = true;
          }
        }
      }
    } else if (!harnessReady) {
      try {
        if (harness.child.exitCode === null) {
          await terminateHarnessTree(harness.child);
        } else {
          await waitForHarnessClose(harness);
        }
        harnessTreeDrained = true;
      } catch {
        cleanupFailed = true;
      }
    }
    if (!completed) {
      if (!harnessTreeDrained) {
        cleanupFailed = true;
      } else {
        try {
          await cleanupFailedRunDirectories({
            workDirectory,
            stagingDirectory,
            artifactDirectory,
            runDirectoriesOwned,
            artifactOwned,
            ownershipClaim,
            workIdentity,
            stagingIdentity,
            artifactIdentity,
            takeoverAfterDrain: !runDirectoriesOwned,
            processTreeDrained: true,
            browserTreeDrained: !browserLaunched || browserTreeDrained,
          });
        } catch {
          cleanupFailed = true;
        }
      }
    }
    assertCleanupFailurePriority(cleanupFailed, primaryFailure);
  }
}


/** CLI 只输出固定成功计数或固定失败坐标，不输出动态上下文。 */
async function main() {
  try {
    const result = await executeAcceptance(parseArguments());
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return 0;
  } catch (error) {
    const failure = safeFailure(error);
    process.stderr.write(`${JSON.stringify({
      status: "failed",
      stage: failure.stage,
      code: failure.code,
    })}\n`);
    return 1;
  }
}


export {
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  CHROME,
  OWNER_CLAIM_FILENAME,
  SOURCE_PROVENANCE_NAMES,
  VIEWPORTS,
  assistantResponseSerializations,
  assertCleanupFailurePriority,
  assertBrowserExecutable,
  assertBrowserExecutableImage,
  answerOracleForQuery,
  assertDirectoryIdentity,
  assertEvidenceKeys,
  assertLayoutMetrics,
  assertOwnershipClaim,
  assertSafeEvidence,
  assertRuntimeSummary,
  assertSourceProvenance,
  assertSourceProvenanceUnchanged,
  assertTracker,
  browserRequestAllowed,
  browserStorageFactsValid,
  bindBrowserGuards,
  buildLiveWorkPrivacyScope,
  buildManifest,
  canonicalSourceIndex,
  classifyPageRequest,
  closeBrowserGracefully,
  cleanupFailedRunDirectories,
  captureDirectoryIdentity,
  createOwnershipClaim,
  createPrivatePageSession,
  createPrivateConfiguration,
  createTracker,
  disposePrivatePageSession,
  deleteVerifiedDirectoryTreeAfterRelease,
  deleteVerifiedDirectoryTree,
  harnessEnvironment,
  inspectConsoleObjectGraph,
  inspectProcessChunk,
  moveVerifiedDirectory,
  ownedRunDirectory,
  ownedStagingDirectory,
  parseHealthAttemptRequestId,
  privateAssistantHeadersValid,
  parseHarnessFailureLine,
  publishStagingAtomically,
  quiesceHarness,
  recordNetworkLoadingFailure,
  recordNetworkResponse,
  registerAssistantResponseBody,
  registerAnswerLeafValues,
  registerDelayedAssistantRequest,
  removeOwnedDirectory,
  revokeFinancePermission,
  runWindowsHandleHelper,
  safeRunId,
  scanArtifacts,
  scanPrivateTree,
  stopHarness,
  terminateBrowserTree,
  terminateHarnessTree,
  terminateWindowsProcessTree,
  verifyPublishedManifest,
  waitForBrowserExit,
  waitForChildDrain,
  waitForConsoleInspections,
  waitForHarnessClose,
  waitForHarnessExit,
  waitForRequestOwnersDrain,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
