"""验证 PRD-050 one-shot 静态证据、源码快照与候选脚本的一致性。

本模块只读取 ``artifacts/prd050/<generation>`` 内已经生成的证据，不执行测试，也不修改
工作区或候选目录。验证范围包括两份 manifest 的严格结构、工作区源码逐文件摘要、十一条
命令的原始输出、四份 JUnit、由完整 Python JUnit 派生的摘要，以及十二个固定候选快照。

安全边界采用失败关闭策略：JSON 重复键、未知字段、路径穿越、符号链接、哈希漂移、跳过
测试或统计不精确都会返回非零。成功时 stdout 只输出不含路径和命令内容的 JSON；失败时
stderr 只输出固定错误码，避免 Prompt、Token、Provider Key 或管理员口令借异常泄露。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


class EvidenceRejected(Exception):
    """表示证据不满足固定合同且只能向调用方暴露稳定错误码。

    ``code`` 必须来自本模块常量或固定字面量，绝不能拼接路径、JSON 值、命令输出或异常文本。
    这样 runner 即使把 stderr 纳入最终审计，也不会把候选环境中的敏感信息带入日志。
    """


# CLI、摘要与时间格式均采用固定上限，防止候选文件借超大输入消耗封账进程资源。
GENERATION_RE = re.compile(r"^\d{8}T\d{6}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_MILLISECONDS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
UTC_SECONDS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SUMMARY_BYTES = 128 * 1024
MAX_JUNIT_BYTES = 64 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_HASHED_EVIDENCE_BYTES = 512 * 1024 * 1024
REPARSE_POINT_ATTRIBUTE = 0x400

# 静态证据必须按此顺序完整执行；改变顺序会破坏“全量回归先于专项复核”的审计语义。
COMMAND_IDS = (
    "python-full",
    "control-affinity-final",
    "runtime-contract",
    "prd049-history",
    "console-test",
    "browser-unit",
    "node-examples",
    "console-lint",
    "ruff",
    "compileall",
    "comments",
)

# 四个 JUnit 的精确计数是本批次封账基线；921 来自新增持久测试后的全仓 collect-only。
EXPECTED_PYTHON_TESTS = 921
JUNIT_CONTRACTS = {
    "python-full": ("python-full-junit.xml", EXPECTED_PYTHON_TESTS),
    "control-affinity-final": ("control-affinity-final-junit.xml", 4),
    "runtime-contract": ("runtime-contract-junit.xml", 19),
    "prd049-history": ("prd049-history-junit.xml", 33),
}
EXPECTED_JUNIT_FILES = 4
EXPECTED_JUNIT_TESTS = 977

# 三组 Node 回归必须独立解析实际汇总；仅依赖 exitcode 和文件哈希无法证明测试数量未缩减。
NODE_TEST_CONTRACTS = {
    "console-test": 354,
    "browser-unit": 71,
    "node-examples": 33,
}

# 非测试质量门禁使用精确字节合同，避免重算 manifest 后以任意“成功”文本替代真实工具输出。
QUALITY_OUTPUT_CONTRACTS = {
    "console-lint": b"console lint ok (118 files)\n",
    "ruff": b"All checks passed!\n",
    # Python 在本项目固定 Windows runner 的原生重定向中输出 CRLF，必须与真实字节一致。
    "comments": b"comment coverage ok\r\n",
}

# 静态 verifier 不参与任何管理员认证；出现任一兼容口令变量都说明调用方扩大了明文暴露面。
ADMIN_PASSWORD_ENVIRONMENT_NAMES = (
    "METAHUB_PRD050_ADMIN_PASSWORD",
    "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD050_PASSWORD_VERIFY",
)

# Node 可能使用 TAP 或 spec reporter；两种格式的最终统计都使用固定前缀和六个字段。
NODE_SUMMARY_RE = re.compile(
    r"^(?:#|ℹ) (tests|pass|fail|cancelled|skipped|todo) (0|[1-9]\d*)$"
)

# static 脚本固定使用这些本机运行时；校验可阻断 manifest 把命令替换为无关的成功程序。
PYTHON_EXECUTABLE = (
    r"C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime"
    r"\dependencies\python\python.exe"
)
NODE_EXECUTABLE = (
    r"C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime"
    r"\dependencies\node\bin\node.exe"
)
RUFF_EXECUTABLE = r"C:\Users\rabol\AppData\Local\hermes\hermes-agent\venv\Scripts\ruff.exe"

# Ruff 文件集与 static runner 保持逐项一致，避免只校验命令前缀而遗漏关键实现或测试文件。
RUFF_TARGETS = (
    "shared-sdk/python/metahub_shared/db/security.py",
    "shared-sdk/python/metahub_shared/runtime/cache_affinity.py",
    "shared-sdk/python/metahub_shared/runtime/config.py",
    "shared-sdk/python/metahub_shared/runtime/errors.py",
    "shared-sdk/python/metahub_shared/runtime/fastapi_app.py",
    "shared-sdk/python/metahub_shared/runtime/webhooks.py",
    "shared-sdk/python/tests/test_cache_affinity.py",
    "shared-sdk/python/tests/test_security.py",
    "control-plane-api/src/metahub_control/audit/service.py",
    "control-plane-api/src/metahub_control/model_aliases/cache_affinity_service.py",
    "control-plane-api/src/metahub_control/model_aliases/models.py",
    "control-plane-api/src/metahub_control/model_aliases/routes.py",
    "control-plane-api/src/metahub_control/model_aliases/service.py",
    "control-plane-api/src/metahub_control/request_logs/models.py",
    "control-plane-api/src/metahub_control/request_logs/service.py",
    "control-plane-api/tests/integration/test_cache_affinity_control.py",
    "control-plane-api/tests/integration/test_db_migrations.py",
    "control-plane-api/tests/integration/test_prd050_db_migrations.py",
    "gateway-data-plane/src/metahub_gateway/api/openai_compatible.py",
    "gateway-data-plane/src/metahub_gateway/app.py",
    "gateway-data-plane/src/metahub_gateway/auth/virtual_key.py",
    "gateway-data-plane/src/metahub_gateway/cache_affinity/__init__.py",
    "gateway-data-plane/src/metahub_gateway/cache_affinity/keypool.py",
    "gateway-data-plane/src/metahub_gateway/cache_affinity/lifecycle.py",
    "gateway-data-plane/src/metahub_gateway/cache_affinity/models.py",
    "gateway-data-plane/src/metahub_gateway/cache_affinity/routing.py",
    "gateway-data-plane/src/metahub_gateway/providers/base.py",
    "gateway-data-plane/src/metahub_gateway/providers/keypool.py",
    "gateway-data-plane/src/metahub_gateway/providers/openai_compatible.py",
    "gateway-data-plane/src/metahub_gateway/routing/models.py",
    "gateway-data-plane/tests/integration/test_cache_affinity_routing.py",
    "gateway-data-plane/tests/integration/test_cache_affinity_service_boundaries.py",
    "gateway-data-plane/tests/unit/test_cache_affinity_cancellation.py",
    "gateway-data-plane/tests/unit/test_cache_affinity_state_machine.py",
    "gateway-data-plane/tests/unit/test_provider_adapters.py",
    "gateway-data-plane/tests/unit/test_provider_keypool.py",
    "gateway-data-plane/tests/unit/test_runtime_config.py",
    "gateway-data-plane/tests/unit/test_sensitive_logging.py",
    "scripts/acceptance/prd048-runtime-acceptance.py",
    "scripts/acceptance/prd050-runtime-acceptance.py",
    "scripts/acceptance/prd050-verify-static-evidence.py",
    "scripts/acceptance/tests/test_prd050_runtime_acceptance.py",
    "scripts/acceptance/tests/test_prd050_verify_static_evidence.py",
)

# source manifest 的完整闭集独立固化在验证器中；新增持久测试也属于封账输入，不能被省略。
SOURCE_ADDITIONAL_PATHS = (
    ".env.example",
    ".tmp/prd050-assemble-candidate.py",
    ".tmp/prd050-final-closeout-audit.ps1",
    ".tmp/prd050-generate-runner-process-contract.ps1",
    ".tmp/prd050-launch-real-acceptance.ps1",
    ".tmp/prd050-prepare-candidate.py",
    ".tmp/prd050-rerun-static-evidence.ps1",
    ".tmp/prd050-run-real-acceptance.ps1",
    "control-plane-api/src/metahub_control/main.py",
    "control-plane-api/tests/integration/test_prd049_migration.py",
    "console-web/scripts/dev-server.mjs",
    "console-web/src/app/main.js",
    "console-web/src/features/request-logs/renderRequestLogPanel.js",
    "console-web/src/features/routing-policies/cacheAffinityControls.js",
    "console-web/src/features/routing-policies/renderRoutingPolicyPanel.js",
    "console-web/src/lib/modelAliases.js",
    "console-web/src/styles/main.css",
    "console-web/tests/unit/cacheAffinity.test.mjs",
    "console-web/tests/unit/cacheAffinityRequestLogs.test.mjs",
    "console-web/tests/unit/prd050BrowserAcceptance.test.mjs",
    "console-web/tests/unit/routingPolicies.test.mjs",
    "infra/migrations/0040_prompt_cache_affinity_routing.down.sql",
    "infra/migrations/0040_prompt_cache_affinity_routing.sql",
    "gateway-data-plane/src/metahub_gateway/main.py",
    "scripts/acceptance/prd047-browser-acceptance.mjs",
    "scripts/acceptance/prd050-browser-acceptance.mjs",
    "scripts/acceptance/prd050-verify-static-evidence.py",
    "scripts/acceptance/tests/test_prd050_verify_static_evidence.py",
)
SOURCE_PATH_CONTRACT = frozenset((*RUFF_TARGETS, *SOURCE_ADDITIONAL_PATHS))
SOURCE_PATH_ORDER = tuple(sorted(SOURCE_PATH_CONTRACT, key=lambda value: value.casefold()))
EXPECTED_SOURCE_FILE_COUNT = 69

# 候选批次在任一验证阶段都只能包含四份根级 JUnit，runtime/browser 不得注入额外 XML。
EXPECTED_XML_PATHS = frozenset(name for name, _ in JUNIT_CONTRACTS.values())

# 候选目录使用固定文件名，源路径来自 manifest；禁止调用方替换待核对的封账脚本。
SNAPSHOT_CONTRACTS = {
    ".tmp/prd050-assemble-candidate.py": "prd050-assemble-candidate.py",
    ".tmp/prd050-prepare-candidate.py": "prd050-prepare-candidate.py",
    ".tmp/prd050-run-real-acceptance.ps1": "prd050-run-real-acceptance.ps1",
    ".tmp/prd050-final-closeout-audit.ps1": "prd050-final-closeout-audit.ps1",
    ".tmp/prd050-rerun-static-evidence.ps1": "prd050-rerun-static-evidence.ps1",
    ".tmp/prd050-generate-runner-process-contract.ps1": (
        "prd050-generate-runner-process-contract.ps1"
    ),
    ".tmp/prd050-launch-real-acceptance.ps1": "prd050-launch-real-acceptance.ps1",
    "scripts/acceptance/prd047-browser-acceptance.mjs": (
        "prd047-browser-acceptance.mjs"
    ),
    "scripts/acceptance/prd048-runtime-acceptance.py": (
        "prd048-runtime-acceptance.py"
    ),
    "scripts/acceptance/prd050-runtime-acceptance.py": (
        "prd050-runtime-acceptance.py"
    ),
    "scripts/acceptance/prd050-browser-acceptance.mjs": (
        "prd050-browser-acceptance.mjs"
    ),
    "scripts/acceptance/prd050-verify-static-evidence.py": (
        "prd050-verify-static-evidence.py"
    ),
}

# 两份 manifest 及嵌套对象只接受白名单字段，防止冲突字段被不同消费者作不同解释。
SOURCE_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "evidence_kind",
        "prd_id",
        "generation",
        "generated_at_utc",
        "file_count",
        "files_fingerprint_sha256",
        "files",
    }
)
STATIC_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "evidence_kind",
        "prd_id",
        "generation",
        "generated_at_utc",
        "source_manifest",
        "command_count",
        "commands",
        "junit_aggregate",
        "derived_artifacts",
    }
)
FILE_RECORD_KEYS = frozenset({"path", "bytes", "sha256"})
SOURCE_REFERENCE_KEYS = frozenset(
    {
        "path",
        "bytes",
        "sha256",
        "generation",
        "file_count",
        "files_fingerprint_sha256",
    }
)
COMMAND_RECORD_KEYS = frozenset(
    {
        "id",
        "status",
        "command",
        "started_at_utc",
        "finished_at_utc",
        "duration_ms",
        "exit_code",
        "stdout",
        "stderr",
        "exitcode_file",
        "junit",
    }
)
COMMAND_DESCRIPTOR_KEYS = frozenset({"executable", "arguments", "working_directory"})
JUNIT_RECORD_KEYS = frozenset({"file", "summary"})
JUNIT_SUMMARY_KEYS = frozenset(
    {"suites", "tests", "failures", "errors", "skipped", "time_seconds"}
)
JUNIT_AGGREGATE_KEYS = frozenset({"files", "tests", "failures", "errors", "skipped"})
PYTHON_SUMMARY_KEYS = frozenset(
    {
        "status",
        "evidence_kind",
        "source_junit",
        "tests",
        "failures",
        "errors",
        "skipped",
        "time_seconds",
        "exit_code",
        "generated_at",
    }
)


def reject(code: str) -> None:
    """以固定错误码终止当前验证流程，不附带原始异常或候选数据。

    参数 ``code`` 只能是代码中静态定义的 ASCII 字面量；调用处不得拼接用户输入。
    """

    raise EvidenceRejected(code)


def is_exact_int(value: Any) -> bool:
    """判断 JSON 值是否为真正整数，并排除 Python 中属于整数子类的布尔值。"""

    return type(value) is int


def assert_exact_keys(value: Any, expected: frozenset[str], code: str) -> dict[str, Any]:
    """要求对象类型和字段集合完全匹配白名单，并返回已收窄类型的对象。

    未知字段与缺失字段使用同一稳定错误码；这样既维持严格 schema，也不向日志回显攻击者
    放入的字段名或敏感值。
    """

    if type(value) is not dict or frozenset(value) != expected:
        reject(code)
    return value


def parse_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 JSON 对象并拒绝重复键，避免不同解析器产生前值或后值语义分歧。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            reject("prd050_static_evidence_json_duplicate_key")
        result[key] = value
    return result


def reject_nonstandard_number(_: str) -> None:
    """拒绝 NaN 与 Infinity 等不属于标准 JSON 的数值，防止比较逻辑被绕过。"""

    reject("prd050_static_evidence_json_number_invalid")


def is_reparse_point(path: Path) -> bool:
    """检查路径是否为符号链接、junction 或其他 Windows 重解析点。

    ``Path.is_symlink`` 覆盖通用平台；``st_file_attributes`` 额外覆盖 Windows junction，避免
    校验完成后候选目录被切换到工作区之外。
    """

    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & REPARSE_POINT_ATTRIBUTE)


def assert_no_reparse_chain(path: Path, anchor: Path, code: str) -> None:
    """从可信锚点到目标逐级拒绝重解析点，阻断中间目录跳转与替换攻击。"""

    try:
        relative = path.relative_to(anchor)
    # 锚点边界解析失败必须关闭验证，且不能把宿主绝对路径写入审计错误。
    except ValueError:
        reject(code)
    current = anchor
    for part in relative.parts:
        current = current / part
        try:
            if is_reparse_point(current):
                reject(code)
        # 路径元数据读取异常按不可信对象处理，不能跳过当前目录继续验证。
        except OSError:
            reject(code)


def same_path(left: Path, right: Path) -> bool:
    """按宿主平台的大小写规则比较两个绝对路径，不读取或输出路径文本。"""

    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    """提取已打开文件与路径终点都可比较的设备号和文件身份号。

    Windows 的 ``st_ino`` 对应文件索引；把它与设备号组合，可发现路径在打开后被原子替换
    到另一个文件，即使大小和修改时间被刻意保持不变也会失败关闭。
    """

    return int(metadata.st_dev), int(metadata.st_ino)


def normalized_resolved_path(path: Path, code: str) -> str:
    """返回解析后按 Windows 规则归一化的路径身份，不把该值写入错误输出。"""

    try:
        return os.path.normcase(str(path.resolve(strict=True)))
    # 解析后的路径身份无法稳定取得时，不允许退回到仅比较 manifest 文本路径。
    except (OSError, RuntimeError):
        reject(code)


def resolve_directory(raw_path: str, code: str) -> Path:
    """把 CLI 目录参数解析为存在的普通目录，并把所有失败收敛为固定错误码。"""

    try:
        lexical = Path(raw_path)
        if is_reparse_point(lexical):
            reject(code)
        resolved = lexical.resolve(strict=True)
        if not resolved.is_dir():
            reject(code)
    except EvidenceRejected:
        raise
    # CLI 目录解析的系统异常统一脱敏，禁止泄露用户输入或宿主文件系统细节。
    except (OSError, RuntimeError, ValueError):
        reject(code)
    return resolved


def validate_relative_file_path(value: Any, code: str) -> str:
    """验证 manifest 文件路径为规范 POSIX 相对路径，禁止反斜杠、空段和父目录跳转。"""

    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        reject(code)
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or parsed.as_posix() != value:
        reject(code)
    if any(part in {"", ".", ".."} or ":" in part for part in parsed.parts):
        reject(code)
    return value


def resolve_relative_file(base: Path, value: Any, code: str) -> tuple[str, Path]:
    """在指定根目录内解析 manifest 相对文件，并拒绝逃逸、链接及非普通文件。

    返回规范相对路径和已解析绝对路径；错误信息不包含任一原始路径。
    """

    relative = validate_relative_file_path(value, code)
    lexical = base.joinpath(*PurePosixPath(relative).parts)
    try:
        assert_no_reparse_chain(lexical, base, code)
        resolved = lexical.resolve(strict=True)
        if not resolved.is_file():
            reject(code)
        if Path(os.path.commonpath((str(base), str(resolved)))) != base:
            reject(code)
        if not stat.S_ISREG(resolved.stat().st_mode):
            reject(code)
    except EvidenceRejected:
        raise
    # 相对路径解析失败表示候选边界无法证明，必须使用固定错误码失败关闭。
    except (OSError, RuntimeError, ValueError):
        reject(code)
    return relative, resolved


def read_bytes_stable(path: Path, maximum: int, code: str) -> tuple[bytes, str]:
    """有界读取文件并同时计算摘要，且拒绝读取期间发生的元数据变化。

    返回原始字节和小写 SHA-256；调用者随后从同一字节解析 JSON 或 XML，避免“先哈希、
    后解析”读取到两个版本。
    """

    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            path_before = path.stat()
            if metadata_identity(before) != metadata_identity(path_before):
                reject(code)
            if before.st_size < 0 or before.st_size > maximum or not stat.S_ISREG(before.st_mode):
                reject(code)
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
        path_after = path.stat()
    except EvidenceRejected:
        raise
    # 文件读取异常不能携带路径传播到 stderr，统一映射为调用方提供的固定错误码。
    except OSError:
        reject(code)
    stable_fields = (
        metadata_identity(before) == metadata_identity(after) == metadata_identity(path_after)
        and before.st_size == after.st_size == path_after.st_size == len(data)
        and before.st_mtime_ns == after.st_mtime_ns
        and after.st_mtime_ns == path_after.st_mtime_ns
    )
    if not stable_fields:
        reject(code)
    return data, hashlib.sha256(data).hexdigest()


def hash_file_stable(path: Path, code: str) -> tuple[int, str]:
    """流式计算普通证据文件摘要，并以大小和元数据复核阻断并发替换。

    stdout/stderr 可能较大，因此不整体载入内存；固定总大小上限仍可防止恶意候选耗尽磁盘
    读取时间。
    """

    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            path_before = path.stat()
            if metadata_identity(before) != metadata_identity(path_before):
                reject(code)
            if before.st_size < 0 or before.st_size > MAX_HASHED_EVIDENCE_BYTES:
                reject(code)
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_HASHED_EVIDENCE_BYTES:
                    reject(code)
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        path_after = path.stat()
    except EvidenceRejected:
        raise
    # 流式摘要期间的系统异常按证据不可验证处理，不能返回部分哈希或文件内容。
    except OSError:
        reject(code)
    if (
        metadata_identity(before) != metadata_identity(after)
        or metadata_identity(after) != metadata_identity(path_after)
        or before.st_size != after.st_size
        or after.st_size != path_after.st_size
        or before.st_size != total
        or before.st_mtime_ns != after.st_mtime_ns
        or after.st_mtime_ns != path_after.st_mtime_ns
    ):
        reject(code)
    return total, digest.hexdigest()


def load_json_document(path: Path, maximum: int, code: str) -> tuple[dict[str, Any], int, str]:
    """从同一份稳定字节解析严格 UTF-8 JSON，并返回对象、大小与摘要。

    重复键和非标准数字由专用回调拒绝；任何解码或结构异常都转换为固定错误码，不传播库
    异常中的内容或文件位置。
    """

    data, digest = read_bytes_stable(path, maximum, code)
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=parse_object_pairs,
            parse_constant=reject_nonstandard_number,
        )
    except EvidenceRejected:
        raise
    # JSON 解码异常可能包含候选片段和偏移，必须在此转换为不含原文的固定错误码。
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        reject(code)
    if type(value) is not dict:
        reject(code)
    return value, len(data), digest


def validate_sha256(value: Any, code: str) -> str:
    """要求摘要为小写、固定 64 位的 SHA-256 十六进制文本。"""

    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        reject(code)
    return value


def parse_timestamp(value: Any, milliseconds: bool, code: str) -> datetime:
    """解析固定 UTC 审计时间，拒绝本地时区、缺失 Z 后缀和宽松日期格式。"""

    pattern = UTC_MILLISECONDS_RE if milliseconds else UTC_SECONDS_RE
    date_format = "%Y-%m-%dT%H:%M:%S.%fZ" if milliseconds else "%Y-%m-%dT%H:%M:%SZ"
    if type(value) is not str or pattern.fullmatch(value) is None:
        reject(code)
    try:
        return datetime.strptime(value, date_format).replace(tzinfo=UTC)
    # 日期库的原始异常不参与审计输出，宽松或越界日期一律视为合同不符。
    except ValueError:
        reject(code)


def validate_nonnegative_number(value: Any, code: str) -> float:
    """把 JSON 有限非负数转换为浮点值，并明确排除布尔值与非数值类型。"""

    if type(value) not in {int, float}:
        reject(code)
    number = float(value)
    if not math.isfinite(number) or number < 0:
        reject(code)
    return number


def validate_file_record(
    value: Any,
    base: Path,
    expected_path: str | None,
    code: str,
) -> tuple[str, Path, int, str]:
    """验证文件记录 schema、受限路径、声明大小、声明摘要和当前文件字节。

    ``expected_path`` 非空时还要求固定文件名，防止命令记录互换 stdout、stderr、JUnit 或
    exitcode 后仍借有效哈希通过。
    """

    record = assert_exact_keys(value, FILE_RECORD_KEYS, code)
    relative, path = resolve_relative_file(base, record["path"], code)
    if expected_path is not None and relative != expected_path:
        reject(code)
    if not is_exact_int(record["bytes"]) or record["bytes"] < 0:
        reject(code)
    declared_sha = validate_sha256(record["sha256"], code)
    actual_bytes, actual_sha = hash_file_stable(path, code)
    if record["bytes"] != actual_bytes or declared_sha != actual_sha:
        reject(code)
    return relative, path, actual_bytes, actual_sha


def compute_source_fingerprint(records: list[dict[str, Any]]) -> str:
    """按 static runner 的 ``path|bytes|sha256`` 行协议计算源码集合指纹。"""

    lines = [f"{item['path']}|{item['bytes']}|{item['sha256']}" for item in records]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_source_manifest(
    workspace: Path,
    batch: Path,
    generation: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], int, str]:
    """验证 source manifest 及每个工作区源码文件，并返回后续交叉核对数据。

    文件列表必须唯一且按 ASCII 大小写不敏感顺序排列；每个记录都与当前工作区普通文件的
    大小和摘要一致，十二个候选快照源也必须全部纳入清单。
    """

    _, manifest_path = resolve_relative_file(
        batch, "source-manifest.json", "prd050_static_evidence_source_manifest_missing"
    )
    manifest, manifest_bytes, manifest_sha = load_json_document(
        manifest_path, MAX_MANIFEST_BYTES, "prd050_static_evidence_source_manifest_invalid"
    )
    source = assert_exact_keys(
        manifest, SOURCE_MANIFEST_KEYS, "prd050_static_evidence_source_schema_invalid"
    )
    if (
        not is_exact_int(source["schema_version"])
        or source["schema_version"] != 1
        or source["status"] != "passed"
        or source["evidence_kind"] != "prd050_source_snapshot"
        or source["prd_id"] != "PRD-050"
        or source["generation"] != generation
    ):
        reject("prd050_static_evidence_source_identity_invalid")
    parse_timestamp(
        source["generated_at_utc"], True, "prd050_static_evidence_source_timestamp_invalid"
    )
    files = source["files"]
    if type(files) is not list or not files or len(files) > 4096:
        reject("prd050_static_evidence_source_files_invalid")
    if not is_exact_int(source["file_count"]) or source["file_count"] != len(files):
        reject("prd050_static_evidence_source_count_invalid")
    fingerprint = validate_sha256(
        source["files_fingerprint_sha256"],
        "prd050_static_evidence_source_fingerprint_invalid",
    )

    records: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    normalized_paths: set[str] = set()
    resolved_paths: set[str] = set()
    filesystem_identities: set[tuple[int, int]] = set()
    for raw_record in files:
        record = assert_exact_keys(
            raw_record, FILE_RECORD_KEYS, "prd050_static_evidence_source_record_invalid"
        )
        relative, source_path, actual_bytes, actual_sha = validate_file_record(
            record,
            workspace,
            None,
            "prd050_static_evidence_source_file_invalid",
        )
        if relative in by_path:
            reject("prd050_static_evidence_source_path_duplicate")
        windows_path = os.path.normcase(relative.replace("/", os.sep)).casefold()
        resolved_path = normalized_resolved_path(
            source_path, "prd050_static_evidence_source_identity_invalid"
        )
        try:
            filesystem_identity = metadata_identity(source_path.stat())
        # 文件身份复核失败表示同一路径可能已被替换，不能只信任此前计算的摘要。
        except OSError:
            reject("prd050_static_evidence_source_identity_invalid")
        if (
            windows_path in normalized_paths
            or resolved_path in resolved_paths
            or filesystem_identity in filesystem_identities
        ):
            reject("prd050_static_evidence_source_identity_duplicate")
        normalized_paths.add(windows_path)
        resolved_paths.add(resolved_path)
        filesystem_identities.add(filesystem_identity)
        normalized = {"path": relative, "bytes": actual_bytes, "sha256": actual_sha}
        records.append(normalized)
        by_path[relative] = normalized
    paths = [item["path"] for item in records]
    if (
        len(SOURCE_PATH_CONTRACT) != EXPECTED_SOURCE_FILE_COUNT
        or len(SOURCE_PATH_ORDER) != EXPECTED_SOURCE_FILE_COUNT
        or tuple(paths) != SOURCE_PATH_ORDER
        or frozenset(paths) != SOURCE_PATH_CONTRACT
    ):
        reject("prd050_static_evidence_source_contract_mismatch")
    if compute_source_fingerprint(records) != fingerprint:
        reject("prd050_static_evidence_source_fingerprint_mismatch")
    if not set(SNAPSHOT_CONTRACTS).issubset(by_path):
        reject("prd050_static_evidence_snapshot_source_missing")
    return source, by_path, manifest_bytes, manifest_sha


def expected_command_arguments(
    command_id: str,
    workspace: Path,
    batch: Path,
    generation: str,
) -> tuple[str, tuple[str, ...], str]:
    """构造 static runner 中一条命令的精确可执行文件、参数与工作目录合同。

    参数中的 JUnit、pytest 临时目录和 cache 目录都绑定当前 batch/generation，因而旧批次
    即使复制 manifest 也无法通过。
    """

    batch_text = str(batch)
    temporary = str(workspace / ".tmp" / f"prd050-static-{generation}")
    if command_id == "python-full":
        return (
            PYTHON_EXECUTABLE,
            (
                "-m",
                "pytest",
                "-q",
                f"--junitxml={batch_text}\\python-full-junit.xml",
                f"--basetemp={temporary}\\pytest-python-full",
                "-o",
                f"cache_dir={temporary}\\cache-python-full",
            ),
            ".",
        )
    if command_id == "control-affinity-final":
        return (
            PYTHON_EXECUTABLE,
            (
                "-m",
                "pytest",
                "-q",
                "control-plane-api/tests/integration/test_cache_affinity_control.py",
                f"--junitxml={batch_text}\\control-affinity-final-junit.xml",
                f"--basetemp={temporary}\\pytest-control-affinity",
                "-o",
                f"cache_dir={temporary}\\cache-control-affinity",
            ),
            ".",
        )
    if command_id == "runtime-contract":
        return (
            PYTHON_EXECUTABLE,
            (
                "-m",
                "pytest",
                "-q",
                "scripts/acceptance/tests/test_prd050_runtime_acceptance.py",
                f"--junitxml={batch_text}\\runtime-contract-junit.xml",
                f"--basetemp={temporary}\\pytest-runtime-contract",
                "-o",
                f"cache_dir={temporary}\\cache-runtime-contract",
            ),
            ".",
        )
    if command_id == "prd049-history":
        return (
            PYTHON_EXECUTABLE,
            (
                "-m",
                "pytest",
                "-q",
                "control-plane-api/tests/integration/test_prd049_migration.py",
                f"--junitxml={batch_text}\\prd049-history-junit.xml",
                f"--basetemp={temporary}\\pytest-prd049-history",
                "-o",
                f"cache_dir={temporary}\\cache-prd049-history",
            ),
            ".",
        )
    if command_id == "console-test":
        return NODE_EXECUTABLE, ("--test", "tests/unit/*.test.mjs"), "console-web"
    if command_id == "browser-unit":
        return (
            NODE_EXECUTABLE,
            (
                "--test",
                "tests/unit/prd050BrowserAcceptance.test.mjs",
                "tests/unit/routingPolicies.test.mjs",
            ),
            "console-web",
        )
    if command_id == "node-examples":
        return NODE_EXECUTABLE, ("--test", "tests/*.test.mjs"), "examples/js"
    if command_id == "console-lint":
        return NODE_EXECUTABLE, ("scripts/lint.mjs",), "console-web"
    if command_id == "ruff":
        return (
            RUFF_EXECUTABLE,
            ("check", "--no-cache", "--ignore", "E501,I001", *RUFF_TARGETS),
            ".",
        )
    if command_id == "compileall":
        return (
            PYTHON_EXECUTABLE,
            (
                "-m",
                "compileall",
                "-q",
                "shared-sdk/python",
                "control-plane-api/src",
                "gateway-data-plane/src",
                "examples/python/src",
                "scripts/acceptance",
            ),
            ".",
        )
    if command_id == "comments":
        return PYTHON_EXECUTABLE, ("scripts/ci/check-comments.py",), "."
    reject("prd050_static_evidence_command_id_invalid")


def local_name(tag: str) -> str:
    """返回 XML 标签去除命名空间后的本地名，供安全的 JUnit 结构遍历使用。"""

    return tag.rsplit("}", 1)[-1]


def parse_nonnegative_xml_int(value: str | None) -> int:
    """解析 JUnit 非负十进制整数字段，拒绝空值、符号、空格及其他宽松表示。"""

    if value is None or re.fullmatch(r"0|[1-9]\d*", value) is None:
        reject("prd050_static_evidence_junit_integer_invalid")
    return int(value)


def verify_junit_bytes(
    data: bytes,
    expected_tests: int,
) -> dict[str, int | float]:
    """独立解析 JUnit 叶子 suite，并核对精确用例数及零失败、错误、跳过。

    除汇总属性外还统计直接 ``testcase`` 数量及失败子节点，避免攻击者只修改 suite 属性伪造
    通过。DOCTYPE/ENTITY 被显式拒绝，阻断实体展开类 XML 输入。
    """

    # JUnit 上限已经受控，因此扫描完整字节而非前缀，阻断把实体声明藏到大段注释后的绕过。
    upper_document = data.upper()
    if b"<!DOCTYPE" in upper_document or b"<!ENTITY" in upper_document:
        reject("prd050_static_evidence_junit_unsafe_xml")
    try:
        root = ET.fromstring(data)
    # XML 解析器异常可能回显候选字节位置，必须收敛为固定且脱敏的错误码。
    except ET.ParseError:
        reject("prd050_static_evidence_junit_xml_invalid")
    if local_name(root.tag) not in {"testsuites", "testsuite"}:
        reject("prd050_static_evidence_junit_root_invalid")
    suites = [element for element in root.iter() if local_name(element.tag) == "testsuite"]
    leaf_suites = [
        suite
        for suite in suites
        if not any(local_name(child.tag) == "testsuite" for child in list(suite))
    ]
    if not leaf_suites:
        reject("prd050_static_evidence_junit_suite_missing")

    tests = failures = errors = skipped = 0
    time_seconds = 0.0
    testcase_count = 0
    observed_failure_nodes = observed_error_nodes = observed_skipped_nodes = 0
    for suite in leaf_suites:
        tests += parse_nonnegative_xml_int(suite.get("tests"))
        failures += parse_nonnegative_xml_int(suite.get("failures"))
        errors += parse_nonnegative_xml_int(suite.get("errors"))
        skipped += parse_nonnegative_xml_int(suite.get("skipped"))
        raw_time = suite.get("time")
        try:
            parsed_time = 0.0 if raw_time in {None, ""} else float(raw_time)
        # 非法时间文本不得进入浮点汇总，也不能通过异常消息泄露 XML 属性内容。
        except ValueError:
            reject("prd050_static_evidence_junit_time_invalid")
        if not math.isfinite(parsed_time) or parsed_time < 0:
            reject("prd050_static_evidence_junit_time_invalid")
        time_seconds += parsed_time
        direct_cases = [child for child in list(suite) if local_name(child.tag) == "testcase"]
        testcase_count += len(direct_cases)
        for case in direct_cases:
            observed_failure_nodes += sum(
                1 for child in list(case) if local_name(child.tag) == "failure"
            )
            observed_error_nodes += sum(
                1 for child in list(case) if local_name(child.tag) == "error"
            )
            observed_skipped_nodes += sum(
                1 for child in list(case) if local_name(child.tag) == "skipped"
            )
    if tests != expected_tests or testcase_count != tests:
        reject("prd050_static_evidence_junit_test_count_invalid")
    # 四组专项必须实际全部通过，任何 skipped 都不能作为“已执行”计入验收数量。
    if (
        failures != 0
        or errors != 0
        or skipped != 0
        or observed_failure_nodes != 0
        or observed_error_nodes != 0
        or observed_skipped_nodes != 0
    ):
        reject("prd050_static_evidence_junit_not_clean")
    return {
        "suites": len(leaf_suites),
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "time_seconds": time_seconds,
    }


def verify_junit_summary(
    value: Any,
    observed: dict[str, int | float],
) -> None:
    """把 manifest 的 JUnit 汇总逐字段绑定到独立 XML 解析结果。"""

    summary = assert_exact_keys(
        value, JUNIT_SUMMARY_KEYS, "prd050_static_evidence_junit_summary_schema_invalid"
    )
    for field in ("suites", "tests", "failures", "errors", "skipped"):
        if not is_exact_int(summary[field]) or summary[field] != observed[field]:
            reject("prd050_static_evidence_junit_summary_mismatch")
    declared_time = validate_nonnegative_number(
        summary["time_seconds"], "prd050_static_evidence_junit_summary_time_invalid"
    )
    if not math.isclose(
        declared_time, float(observed["time_seconds"]), rel_tol=1e-12, abs_tol=1e-9
    ):
        reject("prd050_static_evidence_junit_summary_time_mismatch")


def verify_node_test_output(command_id: str, data: bytes) -> None:
    """解析 Node 测试输出的最终统计，并验证精确 tests/pass 与全部零异常计数。

    既支持 Node TAP 的 ``#`` 前缀，也支持当前运行时 spec reporter 的信息符号前缀；同一字段
    重复、缺失或出现宽松数字表示都会失败，不能靠测试名称中的相似文本伪造汇总。
    """

    try:
        text = data.decode("utf-8", errors="strict")
    # Node 输出解码异常不得回显原始测试文本或潜在敏感 fixture 内容。
    except UnicodeDecodeError:
        reject("prd050_static_evidence_node_output_encoding_invalid")
    if not text.endswith("\n") or "\x00" in text:
        reject("prd050_static_evidence_node_output_format_invalid")
    summaries: dict[str, int] = {}
    for line in text.splitlines():
        match = NODE_SUMMARY_RE.fullmatch(line)
        if match is None:
            continue
        field, raw_value = match.groups()
        if field in summaries:
            reject("prd050_static_evidence_node_summary_duplicate")
        summaries[field] = int(raw_value)
    expected_tests = NODE_TEST_CONTRACTS[command_id]
    expected = {
        "tests": expected_tests,
        "pass": expected_tests,
        "fail": 0,
        "cancelled": 0,
        "skipped": 0,
        "todo": 0,
    }
    if summaries != expected:
        reject("prd050_static_evidence_node_summary_mismatch")


def verify_non_junit_output(
    command_id: str,
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    """独立验证三组 Node 统计与四项质量门禁的真实输出语义。

    所有非 pytest 命令都必须保持 stderr 为空；compileall 的 stdout 也必须为空，其余质量
    命令则逐字节匹配固定成功文本，避免仅凭零退出码和可重算哈希放行伪造证据。
    """

    stdout, _ = read_bytes_stable(
        stdout_path,
        MAX_COMMAND_OUTPUT_BYTES,
        "prd050_static_evidence_command_stdout_semantics_invalid",
    )
    stderr, _ = read_bytes_stable(
        stderr_path,
        MAX_COMMAND_OUTPUT_BYTES,
        "prd050_static_evidence_command_stderr_semantics_invalid",
    )
    if stderr != b"":
        reject("prd050_static_evidence_non_junit_stderr_not_empty")
    if command_id in NODE_TEST_CONTRACTS:
        verify_node_test_output(command_id, stdout)
        return
    if command_id in QUALITY_OUTPUT_CONTRACTS:
        if stdout != QUALITY_OUTPUT_CONTRACTS[command_id]:
            reject("prd050_static_evidence_quality_output_mismatch")
        return
    if command_id == "compileall":
        if stdout != b"":
            reject("prd050_static_evidence_compileall_output_not_empty")
        return
    reject("prd050_static_evidence_non_junit_command_invalid")


def verify_command_descriptor(
    value: Any,
    command_id: str,
    workspace: Path,
    batch: Path,
    generation: str,
) -> None:
    """验证命令描述与当前 generation 的固定可执行文件、参数和工作目录完全一致。"""

    descriptor = assert_exact_keys(
        value,
        COMMAND_DESCRIPTOR_KEYS,
        "prd050_static_evidence_command_descriptor_schema_invalid",
    )
    executable, arguments, working_directory = expected_command_arguments(
        command_id, workspace, batch, generation
    )
    if (
        descriptor["executable"] != executable
        or type(descriptor["arguments"]) is not list
        or tuple(descriptor["arguments"]) != arguments
        or descriptor["working_directory"] != working_directory
    ):
        reject("prd050_static_evidence_command_descriptor_mismatch")


def verify_command_record(
    value: Any,
    command_id: str,
    workspace: Path,
    batch: Path,
    generation: str,
) -> dict[str, int | float] | None:
    """验证单条命令的身份、时序、零退出码、三份原始输出及可选 JUnit。

    返回独立解析后的 JUnit 汇总；非 pytest 命令必须把 ``junit`` 明确写为 null。
    """

    record = assert_exact_keys(
        value, COMMAND_RECORD_KEYS, "prd050_static_evidence_command_schema_invalid"
    )
    if record["id"] != command_id or record["status"] != "passed":
        reject("prd050_static_evidence_command_identity_invalid")
    verify_command_descriptor(record["command"], command_id, workspace, batch, generation)
    started = parse_timestamp(
        record["started_at_utc"], True, "prd050_static_evidence_command_timestamp_invalid"
    )
    finished = parse_timestamp(
        record["finished_at_utc"], True, "prd050_static_evidence_command_timestamp_invalid"
    )
    if finished < started:
        reject("prd050_static_evidence_command_timestamp_order_invalid")
    if not is_exact_int(record["duration_ms"]) or record["duration_ms"] < 0:
        reject("prd050_static_evidence_command_duration_invalid")
    if not is_exact_int(record["exit_code"]) or record["exit_code"] != 0:
        reject("prd050_static_evidence_command_exitcode_invalid")

    _, stdout_path, _, _ = validate_file_record(
        record["stdout"],
        batch,
        f"{command_id}.stdout.log",
        "prd050_static_evidence_command_stdout_invalid",
    )
    _, stderr_path, _, _ = validate_file_record(
        record["stderr"],
        batch,
        f"{command_id}.stderr.log",
        "prd050_static_evidence_command_stderr_invalid",
    )
    stderr_bytes, _ = read_bytes_stable(
        stderr_path,
        MAX_COMMAND_OUTPUT_BYTES,
        "prd050_static_evidence_command_stderr_semantics_invalid",
    )
    # stderr 对全部十一条命令都是禁止面；JUnit 成功不代表原始 stderr 没有 traceback、
    # 路径或秘密，必须在分支前统一拒绝，不能只约束非 pytest 质量门禁。
    if stderr_bytes != b"":
        reject("prd050_static_evidence_command_stderr_not_empty")
    _, exitcode_path, _, _ = validate_file_record(
        record["exitcode_file"],
        batch,
        f"{command_id}.exitcode",
        "prd050_static_evidence_command_exitcode_file_invalid",
    )
    exitcode_bytes, _ = read_bytes_stable(
        exitcode_path, 32, "prd050_static_evidence_command_exitcode_file_invalid"
    )
    if exitcode_bytes != b"0\n":
        reject("prd050_static_evidence_command_exitcode_content_invalid")

    contract = JUNIT_CONTRACTS.get(command_id)
    if contract is None:
        if record["junit"] is not None:
            reject("prd050_static_evidence_unexpected_junit")
        verify_non_junit_output(command_id, stdout_path, stderr_path)
        return None
    junit = assert_exact_keys(
        record["junit"], JUNIT_RECORD_KEYS, "prd050_static_evidence_junit_schema_invalid"
    )
    junit_name, expected_tests = contract
    _, junit_path, _, _ = validate_file_record(
        junit["file"], batch, junit_name, "prd050_static_evidence_junit_file_invalid"
    )
    junit_bytes, _ = read_bytes_stable(
        junit_path, MAX_JUNIT_BYTES, "prd050_static_evidence_junit_file_invalid"
    )
    observed = verify_junit_bytes(junit_bytes, expected_tests)
    verify_junit_summary(junit["summary"], observed)
    return observed


def verify_python_summary(
    static_manifest: dict[str, Any],
    batch: Path,
    python_junit: dict[str, int | float],
) -> None:
    """验证完整 Python JUnit 派生摘要的文件记录、严格 schema 与精确统计。

    摘要不能只凭文件名存在；它必须是 static manifest 唯一派生产物，且内容与本轮 921 条
    Python JUnit 的独立解析结果一致。
    """

    derived = static_manifest["derived_artifacts"]
    if type(derived) is not list or len(derived) != 1:
        reject("prd050_static_evidence_derived_artifacts_invalid")
    _, summary_path, _, _ = validate_file_record(
        derived[0],
        batch,
        "python-full-summary.json",
        "prd050_static_evidence_python_summary_file_invalid",
    )
    summary, _, _ = load_json_document(
        summary_path, MAX_SUMMARY_BYTES, "prd050_static_evidence_python_summary_invalid"
    )
    summary = assert_exact_keys(
        summary, PYTHON_SUMMARY_KEYS, "prd050_static_evidence_python_summary_schema_invalid"
    )
    if (
        summary["status"] != "passed"
        or summary["evidence_kind"] != "junit_derived_summary"
        or summary["source_junit"] != "python-full-junit.xml"
        or not is_exact_int(summary["tests"])
        or summary["tests"] != EXPECTED_PYTHON_TESTS
        or not is_exact_int(summary["failures"])
        or summary["failures"] != 0
        or not is_exact_int(summary["errors"])
        or summary["errors"] != 0
        or not is_exact_int(summary["skipped"])
        or summary["skipped"] != 0
        or not is_exact_int(summary["exit_code"])
        or summary["exit_code"] != 0
    ):
        reject("prd050_static_evidence_python_summary_values_invalid")
    declared_time = validate_nonnegative_number(
        summary["time_seconds"], "prd050_static_evidence_python_summary_time_invalid"
    )
    if not math.isclose(
        declared_time, float(python_junit["time_seconds"]), rel_tol=1e-12, abs_tol=1e-9
    ):
        reject("prd050_static_evidence_python_summary_time_mismatch")
    parse_timestamp(
        summary["generated_at"], False, "prd050_static_evidence_python_summary_timestamp_invalid"
    )


def verify_snapshots(
    batch: Path,
    source_records: dict[str, dict[str, Any]],
) -> None:
    """核对十二个固定候选快照与 source manifest 中对应工作区源文件完全相同。

    快照本身必须是候选根目录下的普通文件，不能通过子目录、链接或调用参数改变核对对象。
    """

    for source_relative, candidate_name in SNAPSHOT_CONTRACTS.items():
        source = source_records[source_relative]
        _, candidate_path = resolve_relative_file(
            batch, candidate_name, "prd050_static_evidence_snapshot_missing"
        )
        candidate_bytes, candidate_sha = hash_file_stable(
            candidate_path, "prd050_static_evidence_snapshot_invalid"
        )
        if candidate_bytes != source["bytes"] or candidate_sha != source["sha256"]:
            reject("prd050_static_evidence_snapshot_mismatch")


def verify_candidate_xml_set(batch: Path) -> None:
    """递归验证候选目录恰好包含四份根级 JUnit XML，拒绝任何额外 XML。

    runtime 和 Browser 执行后仍会再次调用本函数；目录遍历明确禁止跟随重解析点，避免额外
    XML 藏在 junction 后绕过候选文件集合审计。
    """

    observed: set[str] = set()
    normalized: set[str] = set()
    try:
        walker = os.walk(
            batch,
            topdown=True,
            followlinks=False,
            onerror=lambda _: reject("prd050_static_evidence_xml_walk_invalid"),
        )
        for root_text, directories, files in walker:
            root = Path(root_text)
            for directory in directories:
                candidate_directory = root / directory
                if is_reparse_point(candidate_directory):
                    reject("prd050_static_evidence_xml_reparse_rejected")
            for filename in files:
                candidate = root / filename
                if is_reparse_point(candidate):
                    reject("prd050_static_evidence_xml_reparse_rejected")
                if candidate.suffix.lower() != ".xml":
                    continue
                relative = candidate.relative_to(batch).as_posix()
                identity = os.path.normcase(relative.replace("/", os.sep)).casefold()
                if identity in normalized:
                    reject("prd050_static_evidence_xml_identity_duplicate")
                normalized.add(identity)
                observed.add(relative)
    # 遍历错误可能包含宿主路径，统一转换后才能进入 runner 的脱敏 stderr。
    except OSError:
        reject("prd050_static_evidence_xml_walk_invalid")
    if frozenset(observed) != EXPECTED_XML_PATHS or len(observed) != len(EXPECTED_XML_PATHS):
        reject("prd050_static_evidence_xml_set_invalid")


def verify_static_manifest(
    workspace: Path,
    batch: Path,
    generation: str,
    source_manifest: dict[str, Any],
    source_manifest_bytes: int,
    source_manifest_sha: str,
) -> tuple[dict[str, Any], int, str]:
    """验证 static manifest、源码引用、十一条命令、四份 JUnit 与派生摘要。

    static manifest 对 source manifest 的大小、SHA、generation、文件数和集合指纹必须逐字段
    一致；这项交叉引用阻断从不同批次拼接源码清单与测试输出。
    """

    _, static_path = resolve_relative_file(
        batch,
        "static-evidence-manifest.json",
        "prd050_static_evidence_static_manifest_missing",
    )
    static_manifest, static_bytes, static_sha = load_json_document(
        static_path, MAX_MANIFEST_BYTES, "prd050_static_evidence_static_manifest_invalid"
    )
    static_manifest = assert_exact_keys(
        static_manifest,
        STATIC_MANIFEST_KEYS,
        "prd050_static_evidence_static_schema_invalid",
    )
    if (
        not is_exact_int(static_manifest["schema_version"])
        or static_manifest["schema_version"] != 1
        or static_manifest["status"] != "passed"
        or static_manifest["evidence_kind"] != "prd050_static_one_shot"
        or static_manifest["prd_id"] != "PRD-050"
        or static_manifest["generation"] != generation
    ):
        reject("prd050_static_evidence_static_identity_invalid")
    parse_timestamp(
        static_manifest["generated_at_utc"],
        True,
        "prd050_static_evidence_static_timestamp_invalid",
    )

    source_reference = assert_exact_keys(
        static_manifest["source_manifest"],
        SOURCE_REFERENCE_KEYS,
        "prd050_static_evidence_source_reference_schema_invalid",
    )
    if (
        source_reference["path"] != "source-manifest.json"
        or not is_exact_int(source_reference["bytes"])
        or source_reference["bytes"] != source_manifest_bytes
        or validate_sha256(
            source_reference["sha256"],
            "prd050_static_evidence_source_reference_sha_invalid",
        )
        != source_manifest_sha
        or source_reference["generation"] != generation
        or not is_exact_int(source_reference["file_count"])
        or source_reference["file_count"] != source_manifest["file_count"]
        or source_reference["files_fingerprint_sha256"]
        != source_manifest["files_fingerprint_sha256"]
    ):
        reject("prd050_static_evidence_source_reference_mismatch")

    commands = static_manifest["commands"]
    if type(commands) is not list or len(commands) != len(COMMAND_IDS):
        reject("prd050_static_evidence_command_list_invalid")
    if (
        not is_exact_int(static_manifest["command_count"])
        or static_manifest["command_count"] != len(COMMAND_IDS)
    ):
        reject("prd050_static_evidence_command_count_invalid")
    observed_ids = [item.get("id") if type(item) is dict else None for item in commands]
    if tuple(observed_ids) != COMMAND_IDS:
        reject("prd050_static_evidence_command_order_invalid")

    junit_summaries: dict[str, dict[str, int | float]] = {}
    for command_id, record in zip(COMMAND_IDS, commands, strict=True):
        observed = verify_command_record(record, command_id, workspace, batch, generation)
        if observed is not None:
            junit_summaries[command_id] = observed
    if set(junit_summaries) != set(JUNIT_CONTRACTS):
        reject("prd050_static_evidence_junit_set_invalid")

    aggregate = assert_exact_keys(
        static_manifest["junit_aggregate"],
        JUNIT_AGGREGATE_KEYS,
        "prd050_static_evidence_junit_aggregate_schema_invalid",
    )
    expected_aggregate = {
        "files": EXPECTED_JUNIT_FILES,
        "tests": EXPECTED_JUNIT_TESTS,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    for field, expected in expected_aggregate.items():
        if not is_exact_int(aggregate[field]) or aggregate[field] != expected:
            reject("prd050_static_evidence_junit_aggregate_mismatch")
    verify_python_summary(static_manifest, batch, junit_summaries["python-full"])
    return static_manifest, static_bytes, static_sha


def verify_workspace_batch(
    workspace_raw: str,
    batch_raw: str,
    generation: str,
) -> tuple[Path, Path]:
    """验证工作区、批次父目录和 generation 的一一对应关系。

    batch 必须是 ``workspace/artifacts/prd050`` 的直接子目录，且叶子名精确等于 generation；
    该约束禁止把失败归档或其他 PRD 目录伪装成本轮候选。
    """

    if GENERATION_RE.fullmatch(generation) is None:
        reject("prd050_static_evidence_generation_invalid")
    workspace = resolve_directory(
        workspace_raw, "prd050_static_evidence_workspace_invalid"
    )
    batch = resolve_directory(batch_raw, "prd050_static_evidence_batch_invalid")
    expected_parent_lexical = workspace / "artifacts" / "prd050"
    try:
        assert_no_reparse_chain(
            expected_parent_lexical,
            workspace,
            "prd050_static_evidence_batch_invalid",
        )
        expected_parent = expected_parent_lexical.resolve(strict=True)
    except EvidenceRejected:
        raise
    # 批次父目录解析异常无法证明候选归属，必须失败关闭并隐藏宿主路径细节。
    except (OSError, RuntimeError):
        reject("prd050_static_evidence_batch_invalid")
    if not same_path(batch.parent, expected_parent) or batch.name != generation:
        reject("prd050_static_evidence_batch_identity_invalid")
    assert_no_reparse_chain(batch, workspace, "prd050_static_evidence_batch_invalid")
    return workspace, batch


def verify_all(workspace_raw: str, batch_raw: str, generation: str) -> dict[str, Any]:
    """执行完整只读验证，并生成仅含固定统计和摘要的安全成功结果。

    返回值不会包含工作区路径、候选路径、命令、输出文本或 generation，调用方可以直接把它
    写入 runner/closeout 日志而不扩大敏感信息暴露面。
    """

    workspace, batch = verify_workspace_batch(workspace_raw, batch_raw, generation)
    verify_candidate_xml_set(batch)
    source, source_records, source_bytes, source_sha = verify_source_manifest(
        workspace, batch, generation
    )
    _, static_bytes, static_sha = verify_static_manifest(
        workspace,
        batch,
        generation,
        source,
        source_bytes,
        source_sha,
    )
    verify_snapshots(batch, source_records)

    # 发布结果前完整重验源码、所有命令输出、JUnit、摘要与快照，关闭首次校验后的 TOCTOU 窗口。
    final_source, final_records, final_source_bytes, final_source_sha = (
        verify_source_manifest(workspace, batch, generation)
    )
    verify_candidate_xml_set(batch)
    _, final_static_bytes, final_static_sha = verify_static_manifest(
        workspace,
        batch,
        generation,
        final_source,
        final_source_bytes,
        final_source_sha,
    )
    verify_snapshots(batch, final_records)
    verify_candidate_xml_set(batch)
    if (
        final_source_bytes != source_bytes
        or final_source_sha != source_sha
        or final_static_bytes != static_bytes
        or final_static_sha != static_sha
    ):
        reject("prd050_static_evidence_manifest_changed_during_verification")
    return {
        "status": "passed",
        "evidence_kind": "prd050_static_evidence_verification",
        "source_manifest_sha256": source_sha,
        "static_manifest_sha256": static_sha,
        "files_fingerprint_sha256": source["files_fingerprint_sha256"],
        "source_file_count": source["file_count"],
        "command_count": len(COMMAND_IDS),
        "junit_files": EXPECTED_JUNIT_FILES,
        "junit_tests": EXPECTED_JUNIT_TESTS,
        "snapshot_count": len(SNAPSHOT_CONTRACTS),
    }


def parse_cli(arguments: list[str]) -> tuple[str, str, str]:
    """解析三个必需命名参数并拒绝重复项、未知项和位置参数。

    不使用 argparse 的自动错误输出，因为其 usage 可能回显用户传入的路径；所有无效调用只
    返回一个固定合同错误码。
    """

    if len(arguments) != 6:
        reject("prd050_static_evidence_cli_invalid")
    values: dict[str, str] = {}
    allowed = {"--workspace", "--batch", "--generation"}
    for offset in range(0, len(arguments), 2):
        option = arguments[offset]
        value = arguments[offset + 1]
        if option not in allowed or option in values or not value:
            reject("prd050_static_evidence_cli_invalid")
        values[option] = value
    if set(values) != allowed:
        reject("prd050_static_evidence_cli_invalid")
    return values["--workspace"], values["--batch"], values["--generation"]


def main(arguments: list[str] | None = None) -> int:
    """运行 CLI 验证并保证成功 stdout 与失败 stderr 均遵守脱敏输出合同。

    参数为空时读取 ``sys.argv``；返回 0 表示完整通过，返回 1 表示证据拒绝。未知内部异常
    也只映射为固定错误码，不打印 traceback 或异常消息。
    """

    raw_arguments = sys.argv[1:] if arguments is None else arguments
    try:
        # 在解析路径或读取候选前先拒绝管理员口令环境，保证 launcher、runner pre/post、
        # process-contract 与 closeout 五个调用点都能以可执行门禁证明无口令继承。
        if any(name in os.environ for name in ADMIN_PASSWORD_ENVIRONMENT_NAMES):
            reject("prd050_static_evidence_admin_password_environment_forbidden")
        workspace, batch, generation = parse_cli(raw_arguments)
        result = verify_all(workspace, batch, generation)
    except EvidenceRejected as rejection:
        print(str(rejection), file=sys.stderr)
        return 1
    except Exception:
        print("prd050_static_evidence_internal_failure", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
