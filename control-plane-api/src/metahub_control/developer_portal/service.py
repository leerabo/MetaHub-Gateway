"""开发者门户业务服务。

本模块生成租户内快速开始数据和 Python/JavaScript SDK 示例元数据，并以固定白名单构建 ZIP。
租户只取自认证上下文；归档路径校验 fail-closed，低频下载事件写入 fail-open，日志不得包含密钥。
"""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import stat
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext

from .models import (
    JavaScriptSDKExampleResponse,
    PythonSDKExampleResponse,
    QuickstartCommonError,
    QuickstartDataResponse,
    QuickstartStep,
    SDKCommand,
    SDKEnvironmentVariable,
    SDKExample,
)

logger = logging.getLogger("metahub.developer_portal")

# 开发者引导默认环境、模型别名和事件白名单定义安全示例，不返回任何真实密钥明文。
VIRTUAL_KEY_ENV_NAME = "METAHUB_VIRTUAL_KEY"
DEFAULT_MODEL_ALIAS = "default-chat"
DEFAULT_EMBEDDING_MODEL_ALIAS = "default-embedding"
EVENT_ALLOWED_KEYS = {"model_alias", "has_active_virtual_key", "example_ids"}
EVENT_SENSITIVE_KEYS = {
    "authorization",
    "provider_key",
    "providerkey",
    "virtual_key",
    "virtualkey",
    "token",
    "secret",
    "password",
    "prompt",
    "response",
    "message",
    "messages",
    "input",
}

# Python 示例制品标识与版本由后端固定，下载接口不接受客户端覆盖，避免任意文件名和路径注入。
PYTHON_EXAMPLE_ID = "python-openai-example"
PYTHON_EXAMPLE_PACKAGE = "metahub-gateway-examples"
PYTHON_EXAMPLE_VERSION = "0.1.0"
PYTHON_EXAMPLE_REPOSITORY_PATH = "examples/python"
PYTHON_EXAMPLE_DOWNLOAD_URL = "/developer/sdk-examples/python/download"
PYTHON_EXAMPLE_ARCHIVE_NAME = f"metahub-python-examples-{PYTHON_EXAMPLE_VERSION}.zip"
PYTHON_EXAMPLE_ARCHIVE_ROOT = f"metahub-python-examples-{PYTHON_EXAMPLE_VERSION}"

# 示例源码根目录只由服务端源码位置推导；前端不能通过查询参数或请求体指定目录。
PYTHON_EXAMPLE_ROOT = Path(__file__).resolve().parents[4] / "examples" / "python"

# ZIP 只允许包含这些仓库文件，不递归扫描目录，也不把测试缓存、.env、日志或数据库带入制品。
PYTHON_EXAMPLE_FILE_ALLOWLIST = (
    ".env.example",
    "README.md",
    "pyproject.toml",
    "src/metahub_gateway_examples/__init__.py",
    "src/metahub_gateway_examples/chat.py",
    "src/metahub_gateway_examples/client.py",
    "src/metahub_gateway_examples/config.py",
    "src/metahub_gateway_examples/embeddings.py",
    "src/metahub_gateway_examples/errors.py",
)

# 单文件和总源文件上限限制意外大文件占用控制面内存；正常示例远低于该阈值。
PYTHON_EXAMPLE_MAX_FILE_BYTES = 512 * 1024
PYTHON_EXAMPLE_MAX_TOTAL_BYTES = 2 * 1024 * 1024

# JavaScript/Node 制品与 Python 制品使用独立常量；路由不接受任何参数覆盖这些安全边界。
JAVASCRIPT_EXAMPLE_ID = "javascript-openai-example"
JAVASCRIPT_EXAMPLE_PACKAGE = "@metahub/gateway-node-examples"
JAVASCRIPT_EXAMPLE_VERSION = "0.1.0"
JAVASCRIPT_EXAMPLE_RUNTIME = "Node.js >= 22"
JAVASCRIPT_EXAMPLE_REPOSITORY_PATH = "examples/js"
JAVASCRIPT_EXAMPLE_DOWNLOAD_URL = "/developer/sdk-examples/javascript/download"
JAVASCRIPT_EXAMPLE_ARCHIVE_NAME = (
    f"metahub-javascript-examples-{JAVASCRIPT_EXAMPLE_VERSION}.zip"
)
JAVASCRIPT_EXAMPLE_ARCHIVE_ROOT = (
    f"metahub-javascript-examples-{JAVASCRIPT_EXAMPLE_VERSION}"
)

# 源码根目录仅由仓库布局推导，测试可 monkeypatch 此常量构造隔离制品树。
JAVASCRIPT_EXAMPLE_ROOT = Path(__file__).resolve().parents[4] / "examples" / "js"

# 下载包固定为九个运行所需文件，不包含 tests、.env、node_modules、缓存或本机日志。
JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST = (
    ".env.example",
    "README.md",
    "package.json",
    "package-lock.json",
    "src/chat.js",
    "src/client.js",
    "src/config.js",
    "src/errors.js",
    "src/stream.js",
)
JAVASCRIPT_EXAMPLE_MAX_FILE_BYTES = 512 * 1024
JAVASCRIPT_EXAMPLE_MAX_TOTAL_BYTES = 2 * 1024 * 1024

# SDK 下载事件使用独立白名单，不能复用并扩大 quickstart 事件允许字段。
SDK_DOWNLOAD_EVENT_ALLOWED_KEYS = {"language", "artifact_version", "file_count", "example_id"}

# 事件版本必须是短 SemVer；即使值来自内部调用，也不能把自由文本带入分析事件。
SDK_ARTIFACT_VERSION_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


@dataclass(frozen=True)
class SDKArtifactSpec:
    """描述单个语言制品的完整受控边界。

    该规范不定义为模块级冻结实例，而由每次元数据或下载调用从当前模块常量重新构造。
    这样既能让两种语言复用同一安全归档器，也保留 PRD-038 测试对 Python 常量的 monkeypatch
    注入点，不改变既有路由、DTO、命令、ZIP 名称或事件内容。
    """

    language: str
    example_id: str
    package_name: str
    version: str
    repository_path: str
    download_url: str
    archive_name: str
    archive_root: str
    source_root: Path
    file_allowlist: tuple[str, ...]
    max_file_bytes: int
    max_total_bytes: int


@dataclass(frozen=True)
class ControlledSDKFile:
    """保存通过路径预检的普通文件及其读取前可信大小。"""

    relative_name: str
    source_path: Path
    expected_size: int


def _python_sdk_artifact_spec() -> SDKArtifactSpec:
    """在调用时读取 Python 常量并构造制品规范，保留既有 monkeypatch 注入点。"""

    return SDKArtifactSpec(
        language="python",
        example_id=PYTHON_EXAMPLE_ID,
        package_name=PYTHON_EXAMPLE_PACKAGE,
        version=PYTHON_EXAMPLE_VERSION,
        repository_path=PYTHON_EXAMPLE_REPOSITORY_PATH,
        download_url=PYTHON_EXAMPLE_DOWNLOAD_URL,
        archive_name=PYTHON_EXAMPLE_ARCHIVE_NAME,
        archive_root=PYTHON_EXAMPLE_ARCHIVE_ROOT,
        source_root=PYTHON_EXAMPLE_ROOT,
        file_allowlist=tuple(PYTHON_EXAMPLE_FILE_ALLOWLIST),
        max_file_bytes=PYTHON_EXAMPLE_MAX_FILE_BYTES,
        max_total_bytes=PYTHON_EXAMPLE_MAX_TOTAL_BYTES,
    )


def _javascript_sdk_artifact_spec() -> SDKArtifactSpec:
    """在调用时读取 JavaScript 常量并构造受控 Node 制品规范。"""

    return SDKArtifactSpec(
        language="javascript",
        example_id=JAVASCRIPT_EXAMPLE_ID,
        package_name=JAVASCRIPT_EXAMPLE_PACKAGE,
        version=JAVASCRIPT_EXAMPLE_VERSION,
        repository_path=JAVASCRIPT_EXAMPLE_REPOSITORY_PATH,
        download_url=JAVASCRIPT_EXAMPLE_DOWNLOAD_URL,
        archive_name=JAVASCRIPT_EXAMPLE_ARCHIVE_NAME,
        archive_root=JAVASCRIPT_EXAMPLE_ARCHIVE_ROOT,
        source_root=JAVASCRIPT_EXAMPLE_ROOT,
        file_allowlist=tuple(JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST),
        max_file_bytes=JAVASCRIPT_EXAMPLE_MAX_FILE_BYTES,
        max_total_bytes=JAVASCRIPT_EXAMPLE_MAX_TOTAL_BYTES,
    )


@dataclass(frozen=True)
class SDKModelAliases:
    """保存当前租户的 Chat 与 Embeddings 推荐别名。

    两个值都来自携带 ``tenant_id`` 的模型目录查询；缺少匹配能力时使用公开占位，
    绝不回退读取其他租户的模型别名。
    """

    chat: str
    embeddings: str


def get_python_sdk_example_metadata(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> PythonSDKExampleResponse:
    """生成当前认证租户的 Python 示例下载元数据。

    租户标识只取自 ``AuthContext``；服务在返回元数据前校验固定文件清单，避免页面展示
    一个实际无法下载的制品。数据库或源码异常统一折叠为公开错误码，不暴露本机路径。
    """

    # 每次调用都从 Python 常量构造规范，既执行共享安全校验，也保留 PRD-038 monkeypatch。
    artifact_spec = _python_sdk_artifact_spec()
    _resolve_sdk_example_files(
        artifact_spec,
        error_code="developer.sdk_example_unavailable",
    )
    try:
        connection = _connect(config)
        aliases = _select_sdk_model_aliases(
            connection,
            context.user.tenant_id,
            config.environment,
        )
    # 数据库连接或模型目录查询失败时统一收敛为 SDK 元数据不可用，禁止泄露底层位置与 SQL。
    except (ApiError, sqlite3.Error) as exc:
        # 模型目录不可读时不能猜测其他租户数据，也不能把 SQL 或数据库路径返回给客户端。
        raise ApiError("developer.sdk_example_unavailable") from exc

    gateway_base_url = _gateway_base_url(config)
    return PythonSDKExampleResponse(
        id=artifact_spec.example_id,
        tenant_id=context.user.tenant_id,
        language="python",
        title="Python OpenAI-compatible 示例",
        description="使用官方 OpenAI Python 客户端调用 MetaHub Chat Completions 与 Embeddings。",
        package_name=artifact_spec.package_name,
        version=artifact_spec.version,
        repository_path=artifact_spec.repository_path,
        download_url=artifact_spec.download_url,
        gateway_base_url=gateway_base_url,
        chat_model_alias=aliases.chat,
        embedding_model_alias=aliases.embeddings,
        environment_variables=_python_sdk_environment_variables(
            gateway_base_url,
            aliases,
        ),
        commands=_python_sdk_commands(),
        files=list(artifact_spec.file_allowlist),
        capabilities=[
            "chat",
            "embeddings",
            "timeout",
            "retries",
            "request_id_error_handling",
        ],
        request_id=request_id,
        trace_id=trace_id,
    )


def get_javascript_sdk_example_metadata(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> JavaScriptSDKExampleResponse:
    """生成当前认证租户的 JavaScript/Node 示例下载元数据。

    元数据返回前先验证九个固定文件的路径、类型、符号链接和声明大小；模型别名查询始终
    携带认证上下文中的 tenant_id，数据库故障只暴露稳定的语言无关错误码。
    """

    artifact_spec = _javascript_sdk_artifact_spec()
    _resolve_sdk_example_files(
        artifact_spec,
        error_code="developer.sdk_example_unavailable",
    )
    try:
        connection = _connect(config)
        aliases = _select_sdk_model_aliases(
            connection,
            context.user.tenant_id,
            config.environment,
        )
    # 数据库连接与租户模型目录异常统一折叠，禁止向响应泄露 SQL 或数据库绝对路径。
    except (ApiError, sqlite3.Error) as exc:
        # 失败时不回退读取其他租户模型；前端只得到可重试的公开制品不可用错误。
        raise ApiError("developer.sdk_example_unavailable") from exc

    gateway_base_url = _gateway_base_url(config)
    return JavaScriptSDKExampleResponse(
        id=artifact_spec.example_id,
        tenant_id=context.user.tenant_id,
        language="javascript",
        title="JavaScript/Node OpenAI-compatible 示例",
        description=(
            "使用官方 OpenAI JavaScript SDK 调用 MetaHub Chat Completions，"
            "支持非流式、SSE 流式、超时、重试与安全错误处理。"
        ),
        package_name=artifact_spec.package_name,
        version=artifact_spec.version,
        runtime=JAVASCRIPT_EXAMPLE_RUNTIME,
        repository_path=artifact_spec.repository_path,
        download_url=artifact_spec.download_url,
        gateway_base_url=gateway_base_url,
        chat_model_alias=aliases.chat,
        environment_variables=_javascript_sdk_environment_variables(
            gateway_base_url,
            aliases.chat,
        ),
        commands=_javascript_sdk_commands(),
        files=list(artifact_spec.file_allowlist),
        capabilities=["chat", "stream", "timeout", "retries", "request_id"],
        browser_direct_supported=False,
        security_notice=(
            "仅用于 Node.js 服务端；不要在浏览器代码中保存或发送长期 Virtual Key。"
        ),
        request_id=request_id,
        trace_id=trace_id,
    )


def download_python_sdk_example(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> bytes:
    """构建受控 Python 示例 ZIP，并在制品成功后尝试记录下载事件。

    归档安全校验采用 fail-closed；只有完整 ZIP 已在内存生成后才写事件。事件属于低频产品
    分析，写入失败采用 fail-open 并记录结构化日志，不能让已经可用的制品下载失败。
    """

    artifact_spec = _python_sdk_artifact_spec()
    archive = _build_sdk_example_archive(artifact_spec)
    _record_sdk_download_event_fail_open(
        config=config,
        context=context,
        request_id=request_id,
        trace_id=trace_id,
        artifact_spec=artifact_spec,
    )
    return archive


def download_javascript_sdk_example(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> bytes:
    """构建受控 JavaScript ZIP，并在制品完成后以 fail-open 方式写下载事件。

    路径、符号链接、大小、受限读取或压缩任一异常都会 fail-closed；只有完整 ZIP 已生成后
    才进入低频产品事件边界，因此事件系统故障不能降低制品下载可用性。
    """

    artifact_spec = _javascript_sdk_artifact_spec()
    archive = _build_sdk_example_archive(artifact_spec)
    _record_sdk_download_event_fail_open(
        config=config,
        context=context,
        request_id=request_id,
        trace_id=trace_id,
        artifact_spec=artifact_spec,
    )
    return archive


def _select_sdk_model_aliases(
    connection: sqlite3.Connection,
    tenant_id: str,
    environment: str,
) -> SDKModelAliases:
    """按当前租户、环境优先级和模型能力选择 SDK 示例别名。

    Chat 优先选择声明 ``chat=true`` 的 active 模型，随后回退当前租户首个 active 别名；
    Embeddings 没有能力匹配时只返回公开占位，不把 Chat 模型错误推荐为向量模型。
    """

    rows = connection.execute(
        """
        SELECT ma.alias, ma.environment, ma.created_at, m.capabilities, p.health_status
        FROM model_aliases ma
        JOIN models m
          ON m.model_id=ma.default_model_id
         AND m.tenant_id=ma.tenant_id
        JOIN providers p
          ON p.provider_id=m.provider_id
         AND p.tenant_id=m.tenant_id
        WHERE ma.tenant_id=?
          AND ma.status='active'
          AND m.status='active'
          AND p.status='active'
        ORDER BY
          CASE ma.environment WHEN ? THEN 0 WHEN 'dev' THEN 1 WHEN 'test' THEN 2 ELSE 3 END,
          -- 已明确判定为不健康的 Provider 只能作为最后占位，避免生成开箱即失败的示例配置。
          CASE p.health_status WHEN 'unhealthy' THEN 1 ELSE 0 END,
          ma.created_at ASC,
          ma.alias ASC
        """,
        (tenant_id, environment),
    ).fetchall()

    parsed_rows = [
        (str(row["alias"]), _parse_sdk_capabilities(row["capabilities"])) for row in rows
    ]
    chat_alias = next(
        (alias for alias, capabilities in parsed_rows if capabilities.get("chat") is True),
        parsed_rows[0][0] if parsed_rows else DEFAULT_MODEL_ALIAS,
    )
    embedding_alias = next(
        (
            alias
            for alias, capabilities in parsed_rows
            if capabilities.get("embeddings") is True
        ),
        DEFAULT_EMBEDDING_MODEL_ALIAS,
    )
    return SDKModelAliases(chat=chat_alias, embeddings=embedding_alias)


def _parse_sdk_capabilities(raw: object) -> dict[str, bool]:
    """把模型能力 JSON 解析为布尔映射，非法历史数据按无能力声明处理。

    该容错只影响推荐排序，不扩大模型访问权限；真正调用仍由 Gateway 的租户路由与模型
    能力校验负责，因此不能因单行旧数据格式异常让整个下载区不可用。
    """

    try:
        payload = json.loads(str(raw or "{}"))
    # 历史能力值不是合法 JSON 时按空能力处理，不能把解析异常升级成跨租户回退。
    except (TypeError, ValueError):
        # 非法 JSON 不能作为能力证明，按空能力处理最符合最小权限原则。
        return {}
    if not isinstance(payload, dict):
        # 数组或标量同样不是合法能力对象，不能据此推荐 Chat 或 Embeddings 别名。
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, bool)}


def _python_sdk_environment_variables(
    gateway_base_url: str,
    aliases: SDKModelAliases,
) -> list[SDKEnvironmentVariable]:
    """构建下载页展示的固定环境变量说明。

    Virtual Key 仅展示变量名且 ``default_value`` 固定为空；其余默认值均为公开运行配置或
    安全占位，不读取 Provider Key、Token、进程环境中的真实 Virtual Key。
    """

    return [
        SDKEnvironmentVariable(
            name="METAHUB_BASE_URL",
            required=False,
            default_value=gateway_base_url,
            description="Gateway 的 OpenAI-compatible /v1 地址。",
        ),
        SDKEnvironmentVariable(
            name=VIRTUAL_KEY_ENV_NAME,
            required=True,
            default_value=None,
            description="控制台签发的 Virtual Key；只保存在本地环境变量中。",
        ),
        SDKEnvironmentVariable(
            name="METAHUB_CHAT_MODEL",
            required=False,
            default_value=aliases.chat,
            description="当前租户推荐的 Chat 模型别名。",
        ),
        SDKEnvironmentVariable(
            name="METAHUB_EMBEDDING_MODEL",
            required=False,
            default_value=aliases.embeddings,
            description="当前租户推荐的 Embeddings 模型别名或安全占位。",
        ),
        SDKEnvironmentVariable(
            name="METAHUB_TIMEOUT_SECONDS",
            required=False,
            default_value="30",
            description="单次请求总超时秒数。",
        ),
        SDKEnvironmentVariable(
            name="METAHUB_MAX_RETRIES",
            required=False,
            default_value="2",
            description="官方客户端自动重试次数。",
        ),
    ]


def _python_sdk_commands() -> list[SDKCommand]:
    """返回与示例项目模块入口一致的三条固定命令。"""

    return [
        SDKCommand(id="install", label="安装依赖", command="python -m pip install -e ."),
        SDKCommand(
            id="chat",
            label="运行 Chat",
            command=(
                'python -m metahub_gateway_examples.chat '
                '--prompt "用一句话介绍企业级 AI Gateway"'
            ),
        ),
        SDKCommand(
            id="embeddings",
            label="运行 Embeddings",
            command=(
                'python -m metahub_gateway_examples.embeddings '
                '--input "企业级 AI Gateway"'
            ),
        ),
    ]


def _javascript_sdk_environment_variables(
    gateway_base_url: str,
    chat_model_alias: str,
) -> list[SDKEnvironmentVariable]:
    """构建 Node 示例的五个固定环境变量说明。

    Virtual Key 的默认值始终为空；其余值只来自公开控制面配置、当前租户推荐别名或固定
    数字默认值，不读取进程中可能存在的真实 Key，也不把凭据拼入命令参数。
    """

    return [
        SDKEnvironmentVariable(
            name="METAHUB_BASE_URL",
            required=False,
            default_value=gateway_base_url,
            description="Gateway 的 OpenAI-compatible /v1 地址。",
        ),
        SDKEnvironmentVariable(
            name=VIRTUAL_KEY_ENV_NAME,
            required=True,
            default_value=None,
            description="控制台签发的 Virtual Key；只保存在 Node 服务端进程环境中。",
        ),
        SDKEnvironmentVariable(
            name="METAHUB_CHAT_MODEL",
            required=False,
            default_value=chat_model_alias,
            description="当前租户推荐的 Chat 模型别名。",
        ),
        SDKEnvironmentVariable(
            name="METAHUB_TIMEOUT_SECONDS",
            required=False,
            default_value="30",
            description="单次请求及流式示例总时限秒数。",
        ),
        SDKEnvironmentVariable(
            name="METAHUB_MAX_RETRIES",
            required=False,
            default_value="2",
            description="官方客户端在请求建立阶段的有限自动重试次数。",
        ),
    ]


def _javascript_sdk_commands() -> list[SDKCommand]:
    """返回与 Node ESM 示例入口一致的四步安装、配置和运行命令。"""

    return [
        SDKCommand(id="install", label="安装锁定依赖", command="npm ci"),
        SDKCommand(
            id="configure",
            label="创建本地环境文件",
            command="Copy-Item .env.example .env",
        ),
        SDKCommand(
            id="chat",
            label="运行非流式 Chat",
            command=(
                'node --env-file=.env src/chat.js --prompt "用一句话介绍企业级 AI Gateway"'
            ),
        ),
        SDKCommand(
            id="stream",
            label="运行流式 Chat",
            command=(
                'node --env-file=.env src/stream.js --prompt "流式介绍企业级 AI Gateway"'
            ),
        ),
    ]


def _resolve_python_example_files(*, error_code: str) -> list[tuple[str, Path]]:
    """兼容 PRD-038 的 Python 解析入口，并在调用时读取当前 Python 常量。"""

    return [
        (item.relative_name, item.source_path)
        for item in _resolve_sdk_example_files(
            _python_sdk_artifact_spec(),
            error_code=error_code,
        )
    ]


def _resolve_sdk_example_files(
    artifact_spec: SDKArtifactSpec,
    *,
    error_code: str,
) -> list[ControlledSDKFile]:
    """预检受控制品根目录、白名单路径、符号链接、普通文件类型和声明大小。

    根目录以及相对路径中的每一级父目录和叶子都先用 ``lstat`` 检查，任何符号链接即使
    仍指向根内文件也会被拒绝；随后再做真实路径归属校验。全部文件的 ``stat().st_size``
    会在读取前完成单文件和总量校验，避免先把误提交的大文件完整读入 API 进程内存。
    """

    try:
        root_status = artifact_spec.source_root.lstat()
        if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
            # 根本身不能是链接或普通文件，否则真实路径归属检查会错误信任被重定向的目录。
            raise ApiError(error_code)
        resolved_root = artifact_spec.source_root.resolve(strict=True)
    # 根目录缺失、权限不足或解析失败时只返回固定错误码，不泄露仓库绝对路径。
    except ApiError:
        # 已归一的根对象类型错误保持调用方选择的公开错误码。
        raise
    except OSError as exc:
        # OSError 文本可能包含服务器路径，因此只能作为异常链保留给内部诊断。
        raise ApiError(error_code) from exc

    controlled_files: list[ControlledSDKFile] = []
    declared_total = 0
    for relative_name in artifact_spec.file_allowlist:
        relative_path = PurePosixPath(relative_name)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or not relative_path.parts
            or relative_path.as_posix() in {"", "."}
        ):
            # 即使内部白名单被误改，也不能读取根外对象、根目录本身或绝对路径。
            raise ApiError(error_code)

        candidate = artifact_spec.source_root
        try:
            for index, part in enumerate(relative_path.parts):
                candidate = candidate / part
                candidate_status = candidate.lstat()
                if stat.S_ISLNK(candidate_status.st_mode):
                    # 任意链接都 fail-closed，包括链接到根内普通文件或根内目录的情形。
                    raise ApiError(error_code)
                is_leaf = index == len(relative_path.parts) - 1
                if not is_leaf and not stat.S_ISDIR(candidate_status.st_mode):
                    # 中间成员必须是真实目录，文件、设备和其他特殊对象都不能继续拼接。
                    raise ApiError(error_code)
                if is_leaf and not stat.S_ISREG(candidate_status.st_mode):
                    # ZIP 成员只允许真实普通文件，不接受目录、设备、管道或 socket。
                    raise ApiError(error_code)

            resolved_source = candidate.resolve(strict=True)
            resolved_source.relative_to(resolved_root)
            source_status = resolved_source.stat()
        # 缺失成员、真实路径逃逸和文件系统检查失败全部映射为稳定公开错误。
        except ApiError:
            # 主动拒绝的链接或对象类型错误已经使用正确公开错误码，直接上抛。
            raise
        except (OSError, ValueError) as exc:
            # ``relative_to`` 的 ValueError 表示真实路径不属于制品根，不能读取任何字节。
            raise ApiError(error_code) from exc

        declared_size = source_status.st_size
        declared_total += declared_size
        if (
            declared_size < 0
            or declared_size > artifact_spec.max_file_bytes
            or declared_total > artifact_spec.max_total_bytes
        ):
            # 读取前先拒绝超限声明，确保攻击性文件不会因 read_bytes 耗尽控制面内存。
            raise ApiError(error_code)
        controlled_files.append(
            ControlledSDKFile(
                relative_name=relative_path.as_posix(),
                source_path=resolved_source,
                expected_size=declared_size,
            )
        )
    return controlled_files


def _read_controlled_sdk_file(
    controlled_file: ControlledSDKFile,
    artifact_spec: SDKArtifactSpec,
) -> bytes:
    """按“单文件上限 + 1”受限读取，并复核读取前后对象与大小没有变化。"""

    try:
        before = controlled_file.source_path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            # 预检后若成员被替换成链接或特殊对象，必须在打开前再次 fail-closed。
            raise ApiError("developer.sdk_example_archive_failed")
        if (
            before.st_size != controlled_file.expected_size
            or before.st_size > artifact_spec.max_file_bytes
        ):
            # 预检到打开之间的尺寸变化视为制品不稳定，不能生成内容不确定的 ZIP。
            raise ApiError("developer.sdk_example_archive_failed")
        with controlled_file.source_path.open("rb") as source:
            content = source.read(artifact_spec.max_file_bytes + 1)
        after = controlled_file.source_path.lstat()
    # 文件打开、受限读取或读取后复检故障统一折叠，响应中不能出现本机路径。
    except ApiError:
        # 安全分支已归一为归档失败，交由统一异常处理器补链路标识。
        raise
    except OSError as exc:
        # 底层异常只作为内部异常链，不能把路径或系统消息返回给调用方。
        raise ApiError("developer.sdk_example_archive_failed") from exc

    if (
        len(content) > artifact_spec.max_file_bytes
        or len(content) != controlled_file.expected_size
        or stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or after.st_size != controlled_file.expected_size
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        # 限长结果、对象身份、修改时间或最终大小不一致都表示读取期间发生竞态或越界。
        raise ApiError("developer.sdk_example_archive_failed")
    return content


def _build_sdk_example_archive(artifact_spec: SDKArtifactSpec) -> bytes:
    """从受控制品规范构建只含固定 POSIX 成员名的内存 ZIP。"""

    controlled_files = _resolve_sdk_example_files(
        artifact_spec,
        error_code="developer.sdk_example_archive_failed",
    )
    buffer = io.BytesIO()
    actual_total = 0
    try:
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for controlled_file in controlled_files:
                content = _read_controlled_sdk_file(controlled_file, artifact_spec)
                actual_total += len(content)
                if actual_total > artifact_spec.max_total_bytes:
                    # 读取后再次执行总量边界，防止任何预检与读取间竞态绕过总上限。
                    raise ApiError("developer.sdk_example_archive_failed")
                archive_name = f"{artifact_spec.archive_root}/{controlled_file.relative_name}"
                archive.writestr(archive_name, content)
    # 安全校验已产生的 ApiError 保持稳定错误码，不能被压缩库异常分支覆盖。
    except ApiError:
        # 路径、体积或读取竞态错误全部 fail-closed，不会进入下载事件写入阶段。
        raise
    # 压缩器和内存缓冲故障统一映射为归档失败，禁止回传原始异常对象。
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        # 原始错误仅保留在异常链，公开响应不包含路径、源码或压缩器内部状态。
        raise ApiError("developer.sdk_example_archive_failed") from exc
    return buffer.getvalue()


def _build_python_example_archive() -> bytes:
    """兼容 PRD-038 的 Python 归档入口，并在调用时读取 monkeypatch 后的常量。"""

    return _build_sdk_example_archive(_python_sdk_artifact_spec())


def _build_javascript_example_archive() -> bytes:
    """构建 JavaScript/Node 固定九成员归档，供路由和安全测试复用。"""

    return _build_sdk_example_archive(_javascript_sdk_artifact_spec())


def _record_sdk_download_event_fail_open(
    *,
    config: AppConfig,
    context: AuthContext,
    request_id: str,
    trace_id: str,
    artifact_spec: SDKArtifactSpec,
) -> None:
    """在 ZIP 完成后的调用边界捕获所有事件异常并安全降级。

    这里故意捕获 ``Exception``，而不只捕获数据库异常：事件校验、连接工厂、INSERT、commit、
    monkeypatch 注入或额外运行时故障都属于低频分析链路，不能让已经生成的制品下载失败。
    """

    try:
        _record_sdk_example_downloaded(
            config,
            context,
            request_id=request_id,
            trace_id=trace_id,
            artifact_spec=artifact_spec,
        )
    # ZIP 已完成后，任何普通事件异常都 fail-open；SystemExit/KeyboardInterrupt 不在业务降级范围。
    except Exception:
        # 日志自身也属于可失败依赖，二次保护确保自定义 handler 异常不会反向阻断 ZIP。
        try:
            logger.warning(
                "sdk.example_download_event_write_failed",
                extra={
                    "event": "sdk.example_download_event_write_failed",
                    "tenant_id": context.user.tenant_id,
                    "user_id": context.user.user_id,
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "error_code": "developer.sdk_example_event_write_failed",
                },
            )
        # 日志处理器异常同样不能改变已生成制品的成功结果，也不能输出原始异常或敏感值。
        except Exception:
            # 事件和日志均不可用时保持静默 fail-open，外部健康检查负责暴露基础设施故障。
            pass


def _record_sdk_example_downloaded(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
    artifact_spec: SDKArtifactSpec,
) -> None:
    """在单个事务中写入与制品规范一致的 sdk.example_downloaded 事件。

    记录器负责在校验、连接、写入或提交异常时尝试回滚并重新抛出；外层下载边界统一执行
    fail-open 和安全日志。事件只含四个短标量，不含源码、Prompt、环境变量值或任何 Key。
    """

    connection: sqlite3.Connection | None = None
    try:
        safe_payload = _safe_sdk_download_event_payload(
            {
                "language": artifact_spec.language,
                "artifact_version": artifact_spec.version,
                "file_count": len(artifact_spec.file_allowlist),
                "example_id": artifact_spec.example_id,
            },
            artifact_spec=artifact_spec,
        )
        connection = _connect(config)
        connection.execute(
            """
            INSERT INTO developer_portal_events(
              event_id, tenant_id, user_id, session_id, event_type, object_id,
              path, request_id, trace_id, payload_json, created_at
            )
            VALUES(?, ?, ?, ?, 'sdk.example_downloaded', ?, ?, ?, ?, ?, ?)
            """,
            (
                f"dpe_{uuid4().hex}",
                context.user.tenant_id,
                context.user.user_id,
                context.session.session_id,
                artifact_spec.example_id,
                "/#sdk-examples",
                request_id,
                trace_id,
                json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                _to_db_time(_utcnow()),
            ),
        )
        connection.commit()
    # 记录器必须覆盖校验、连接、INSERT、commit 及额外 RuntimeError，并把事务清理留在内部。
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            # rollback 自身异常由外层 fail-open 边界统一吞掉，不能覆盖原始事件失败原因。
            except Exception:
                # 数据库健康检查负责报告不可回滚连接；下载流程仍返回已完成的 ZIP。
                pass
        raise


def _safe_sdk_download_event_payload(
    payload: dict[str, object],
    *,
    artifact_spec: SDKArtifactSpec | None = None,
) -> dict[str, object]:
    """严格校验 SDK 下载事件字段和值均与当前语言制品规范一致。

    兼容 PRD-038 直接调用方式：未显式传规范时先按 ``language`` 选择调用时构造的 Python
    或 JavaScript 规范。键名不做宽松归一化，防止拼写变体绕过白名单或产生不可解释事件。
    """

    if set(payload) != SDK_DOWNLOAD_EVENT_ALLOWED_KEYS or any(
        not isinstance(key, str) or key in EVENT_SENSITIVE_KEYS for key in payload
    ):
        # 四个精确字段缺一不可，未知字段和敏感字段即使值为空也必须拒绝。
        raise ApiError("developer.sdk_example_event_invalid")

    language = payload.get("language")
    if artifact_spec is None:
        if language == "python":
            artifact_spec = _python_sdk_artifact_spec()
        elif language == "javascript":
            artifact_spec = _javascript_sdk_artifact_spec()
        else:
            # language 只允许两种受控制品；curl 或自由文本不能写入下载事件。
            raise ApiError("developer.sdk_example_event_invalid")

    artifact_version = payload.get("artifact_version")
    file_count = payload.get("file_count")
    example_id = payload.get("example_id")
    if not isinstance(language, str) or language != artifact_spec.language:
        # 事件语言必须与当前制品规范完全一致，不能用 Python 规范记录 JavaScript 下载。
        raise ApiError("developer.sdk_example_event_invalid")
    if (
        not isinstance(artifact_version, str)
        or len(artifact_version) > 120
        or SDK_ARTIFACT_VERSION_PATTERN.fullmatch(artifact_version) is None
        or artifact_version != artifact_spec.version
    ):
        # 版本既要满足短 SemVer，也必须等于实际归档版本，避免分析账本与文件名漂移。
        raise ApiError("developer.sdk_example_event_invalid")
    if (
        isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or file_count <= 0
        or file_count != len(artifact_spec.file_allowlist)
    ):
        # bool 是 int 子类，必须显式拒绝；文件数还必须等于当前固定白名单长度。
        raise ApiError("developer.sdk_example_event_invalid")
    if (
        not isinstance(example_id, str)
        or len(example_id) > 120
        or example_id != artifact_spec.example_id
    ):
        # 示例 ID 必须精确匹配当前制品，防止事件对象和 payload 指向不同语言或产品。
        raise ApiError("developer.sdk_example_event_invalid")
    return {
        "language": language,
        "artifact_version": artifact_version,
        "file_count": file_count,
        "example_id": example_id,
    }


@dataclass(frozen=True)
class QuickstartConfigState:
    """快速开始配置状态。

    服务层用该结构集中保存推荐模型别名和 Key 状态，避免示例生成时散落数据库 row 访问逻辑。
    """

    model_alias: str
    model_alias_ready: bool
    virtual_key_prefix: str | None
    virtual_key_ready: bool


def get_quickstart_data(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> QuickstartDataResponse:
    """生成当前租户的 5 分钟快速接入数据。

    所有业务数据均从 AuthContext 的 tenant_id 查询；示例中只使用 Virtual Key 环境变量，不回显明文 Key。
    """

    connection = _connect(config)
    state = _load_quickstart_state(connection, config, context)
    gateway_base_url = _gateway_base_url(config)
    steps = _build_steps(context.user.tenant_id, state)
    examples = _build_examples(context.user.tenant_id, gateway_base_url, state.model_alias)
    common_errors = _common_errors()
    _record_quickstart_viewed(
        connection,
        context,
        state,
        examples,
        request_id=request_id,
        trace_id=trace_id,
    )
    return QuickstartDataResponse(
        tenant_id=context.user.tenant_id,
        gateway_base_url=gateway_base_url,
        model_alias=state.model_alias,
        virtual_key_placeholder=VIRTUAL_KEY_ENV_NAME,
        has_active_virtual_key=state.virtual_key_ready,
        virtual_key_prefix=state.virtual_key_prefix,
        steps=steps,
        examples=examples,
        common_errors=common_errors,
        request_id=request_id,
        trace_id=trace_id,
    )


def _load_quickstart_state(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: AuthContext,
) -> QuickstartConfigState:
    """读取当前租户的推荐模型别名和 Virtual Key 安全状态。"""

    model_alias = _select_model_alias(connection, context.user.tenant_id, config.environment)
    virtual_key_prefix = _select_virtual_key_prefix(connection, context.user.tenant_id, config.environment)
    return QuickstartConfigState(
        model_alias=model_alias or DEFAULT_MODEL_ALIAS,
        model_alias_ready=bool(model_alias),
        virtual_key_prefix=virtual_key_prefix,
        virtual_key_ready=bool(virtual_key_prefix),
    )


def _select_model_alias(connection: sqlite3.Connection, tenant_id: str, environment: str) -> str | None:
    """按环境优先级选择当前租户可用于快速开始的 active 模型别名。"""

    row = connection.execute(
        """
        SELECT alias
        FROM model_aliases
        WHERE tenant_id=? AND status='active'
        ORDER BY CASE environment WHEN ? THEN 0 WHEN 'dev' THEN 1 WHEN 'test' THEN 2 ELSE 3 END,
                 created_at ASC,
                 alias ASC
        LIMIT 1
        """,
        (tenant_id, environment),
    ).fetchone()
    return str(row["alias"]) if row else None


def _select_virtual_key_prefix(connection: sqlite3.Connection, tenant_id: str, environment: str) -> str | None:
    """读取当前租户 active Virtual Key 的脱敏前缀，绝不读取或返回 key_hash。"""

    row = connection.execute(
        """
        SELECT key_prefix
        FROM virtual_keys
        WHERE tenant_id=? AND status='active'
        ORDER BY CASE environment WHEN ? THEN 0 WHEN 'dev' THEN 1 WHEN 'test' THEN 2 ELSE 3 END,
                 created_at DESC
        LIMIT 1
        """,
        (tenant_id, environment),
    ).fetchone()
    return str(row["key_prefix"]) if row and row["key_prefix"] else None


def _gateway_base_url(config: AppConfig) -> str:
    """生成 OpenAI-compatible SDK 使用的 `/v1` base_url。

    该值来自运行配置，不能让前端自行拼接，避免本地、测试和生产环境示例指向错误网关。
    """

    base = config.gateway_api_base_url.strip().rstrip("/") or "http://localhost:8000"
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _build_steps(tenant_id: str, state: QuickstartConfigState) -> list[QuickstartStep]:
    """构建快速开始步骤，缺失 Key 或模型别名时返回可跳转的 action_required 状态。"""

    return [
        QuickstartStep(
            id="prepare-key",
            tenant_id=tenant_id,
            order=1,
            title="准备 Virtual Key",
            description=(
                "当前租户已有 active Key，可使用创建时复制的一次性明文。"
                if state.virtual_key_ready
                else "先创建一把 dev Virtual Key，并在创建成功时复制一次性明文。"
            ),
            status="ready" if state.virtual_key_ready else "action_required",
            action_label="查看 Virtual Key",
            navigation_item_id="virtual-keys",
            target_hash="#virtual-keys",
        ),
        QuickstartStep(
            id="choose-model",
            tenant_id=tenant_id,
            order=2,
            title="确认模型别名",
            description=(
                f"推荐使用模型别名 {state.model_alias} 发起第一次调用。"
                if state.model_alias_ready
                else "尚未配置 active 模型别名，示例先使用 default-chat 占位。"
            ),
            status="ready" if state.model_alias_ready else "action_required",
            action_label="查看模型别名",
            navigation_item_id="model-aliases",
            target_hash="#model-aliases",
        ),
        QuickstartStep(
            id="copy-example",
            tenant_id=tenant_id,
            order=3,
            title="复制示例代码",
            description="选择 Python、JavaScript 或 curl 示例，设置本地环境变量后执行非流式 chat 调用。",
            status="ready",
            action_label="复制示例",
            navigation_item_id="quickstart",
            target_hash="#quickstart",
        ),
        QuickstartStep(
            id="inspect-request",
            tenant_id=tenant_id,
            order=4,
            title="用 request_id 排查",
            description="调用成功或失败后，可到调用日志按 request_id 查看路由、错误码和用量摘要。",
            status="ready",
            action_label="查看调用日志",
            navigation_item_id="request-logs",
            target_hash="#request-logs",
        ),
    ]


def _build_examples(tenant_id: str, gateway_base_url: str, model_alias: str) -> list[SDKExample]:
    """生成 Python、JavaScript 和 curl 三类可复制示例。

    示例代码只使用环境变量引用 Virtual Key，不把明文 Key、Provider Key 或 Authorization 写进响应。
    """

    python_code = f"""import os
from openai import OpenAI

client = OpenAI(
    base_url="{gateway_base_url}",
    api_key=os.environ["{VIRTUAL_KEY_ENV_NAME}"],
)

response = client.chat.completions.create(
    model="{model_alias}",
    messages=[{{"role": "user", "content": "你好，MetaHub"}}],
    stream=False,
)

print(response.choices[0].message.content)
"""
    javascript_code = f"""import OpenAI from "openai";

const client = new OpenAI({{
  baseURL: "{gateway_base_url}",
  apiKey: process.env.{VIRTUAL_KEY_ENV_NAME},
}});

const response = await client.chat.completions.create({{
  model: "{model_alias}",
  messages: [{{ role: "user", content: "你好，MetaHub" }}],
  stream: false,
}});

console.log(response.choices[0].message.content);
"""
    curl_code = f"""curl -sS "{gateway_base_url}/chat/completions" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${{{VIRTUAL_KEY_ENV_NAME}}}" \\
  -d '{{"model":"{model_alias}","messages":[{{"role":"user","content":"你好，MetaHub"}}],"stream":false}}'
"""
    return [
        SDKExample(
            id="python-openai",
            tenant_id=tenant_id,
            language="python",
            title="Python",
            description="使用 OpenAI Python SDK 发起一次非流式 Chat Completions 调用。",
            install_command="pip install openai",
            code=python_code,
            copy_label="复制 Python",
            base_url=gateway_base_url,
            model_alias=model_alias,
            uses_env_var=True,
        ),
        SDKExample(
            id="javascript-openai",
            tenant_id=tenant_id,
            language="javascript",
            title="JavaScript",
            description="使用 OpenAI Node SDK 发起一次非流式 Chat Completions 调用。",
            install_command="npm install openai",
            code=javascript_code,
            copy_label="复制 JavaScript",
            base_url=gateway_base_url,
            model_alias=model_alias,
            uses_env_var=True,
        ),
        SDKExample(
            id="curl-chat",
            tenant_id=tenant_id,
            language="curl",
            title="curl",
            description="使用 curl 直接调用 Gateway 的 OpenAI-compatible Chat Completions 接口。",
            install_command=None,
            code=curl_code,
            copy_label="复制 curl",
            base_url=gateway_base_url,
            model_alias=model_alias,
            uses_env_var=True,
        ),
    ]


def _common_errors() -> list[QuickstartCommonError]:
    """返回快速开始页展示的 M0 常见错误和修复建议。"""

    return [
        QuickstartCommonError(
            code="gateway.auth_missing",
            title="缺少 Virtual Key",
            cause="请求没有携带 Authorization Bearer 头。",
            fix=f"把创建时复制的明文 Key 写入本地环境变量 {VIRTUAL_KEY_ENV_NAME}。",
            link_target="#virtual-keys",
        ),
        QuickstartCommonError(
            code="gateway.auth_invalid",
            title="Virtual Key 无效",
            cause="Key 被禁用、复制不完整，或不属于当前租户。",
            fix="重新创建 dev Key，并只在本地环境变量中保存明文。",
            link_target="#virtual-keys",
        ),
        QuickstartCommonError(
            code="gateway.model_missing",
            title="缺少 model",
            cause="请求体没有传模型别名。",
            fix="复制示例中的 model 字段，或到模型别名页确认当前租户可用别名。",
            link_target="#model-aliases",
        ),
        QuickstartCommonError(
            code="gateway.messages_missing",
            title="缺少 messages",
            cause="Chat Completions 请求缺少非空 messages 数组。",
            fix="保留示例里的 messages 结构，再替换业务消息内容。",
            link_target="#quickstart",
        ),
        QuickstartCommonError(
            code="gateway.model_not_found",
            title="模型别名不存在",
            cause="当前租户没有该 model alias，或别名已被删除。",
            fix="到模型别名页创建 active alias，或把示例 model 改成已有别名。",
            link_target="#model-aliases",
        ),
        QuickstartCommonError(
            code="gateway.upstream_unavailable",
            title="上游不可用",
            cause="Provider Endpoint 网络不可达、超时或未正确配置。",
            fix="到 Provider 页面执行连接测试，并查看 request_id 对应调用日志。",
            link_target="#providers",
        ),
        QuickstartCommonError(
            code="gateway.upstream_error",
            title="上游返回错误",
            cause="模型厂商或云端 Endpoint 返回非成功响应。",
            fix="使用 request_id 查看调用日志中的上游错误摘要。",
            link_target="#request-logs",
        ),
    ]


def _record_quickstart_viewed(
    connection: sqlite3.Connection,
    context: AuthContext,
    state: QuickstartConfigState,
    examples: list[SDKExample],
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """记录 developer.quickstart_viewed 事件，事件 payload 只包含安全元数据。"""

    safe_payload = _safe_event_payload(
        {
            "model_alias": state.model_alias,
            "has_active_virtual_key": state.virtual_key_ready,
            "example_ids": ",".join(example.id for example in examples),
        }
    )
    try:
        connection.execute(
            """
            INSERT INTO developer_portal_events(
              event_id, tenant_id, user_id, session_id, event_type, object_id,
              path, request_id, trace_id, payload_json, created_at
            )
            VALUES(?, ?, ?, ?, 'developer.quickstart_viewed', 'quickstart', ?, ?, ?, ?, ?)
            """,
            (
                f"dpe_{uuid4().hex}",
                context.user.tenant_id,
                context.user.user_id,
                context.session.session_id,
                "/#quickstart",
                request_id,
                trace_id,
                json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                _to_db_time(_utcnow()),
            ),
        )
        connection.commit()
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error:
        connection.rollback()
        logger.warning(
            "developer.quickstart_event_write_failed",
            extra={
                "event": "developer.quickstart_event_write_failed",
                "tenant_id": context.user.tenant_id,
                "user_id": context.user.user_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "developer.quickstart_event_write_failed",
            },
        )
        # 页面主数据已经生成，事件写入失败不能阻断开发者完成首次接入。


def _safe_event_payload(payload: dict[str, object]) -> dict[str, object]:
    """过滤开发者门户事件 payload，只允许安全短字段进入数据库。"""

    safe: dict[str, object] = {}
    for key, value in payload.items():
        normalized_key = str(key).strip().lower()
        if normalized_key in EVENT_SENSITIVE_KEYS or normalized_key not in EVENT_ALLOWED_KEYS:
            # 事件 payload 白名单比黑名单更稳妥，避免把密钥、Prompt 或响应正文夹带进审计表。
            raise ApiError("developer.quickstart_event_invalid")
        if not isinstance(value, (str, int, float, bool)):
            # 事件只保存简单标量，复杂对象容易夹带请求正文或密钥片段。
            raise ApiError("developer.quickstart_event_invalid")
        if isinstance(value, str) and len(value) > 240:
            # 快速开始事件只需要短元数据，过长字符串按潜在敏感内容拒绝。
            raise ApiError("developer.quickstart_event_invalid")
        safe[normalized_key] = value
    return safe


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，并把底层连接错误映射为统一错误结构。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 数据库连接错误继续使用统一错误码，避免把路径或 SQL 细节暴露给开发者门户页面。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _utcnow() -> datetime:
    """返回 UTC 当前时间，统一开发者门户事件的审计时间口径。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把 UTC datetime 转换为数据库保存的 ISO 字符串。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
