"""全仓中文注释质量门禁。

扫描正式源码、测试与迁移的职责说明和关键治理分支；生成证据和外部依赖不参与当前代码基线。
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path


# 注释门禁的扫描范围、源码后缀和关键治理关键词集中定义，避免后续 PRD 重新降低注释标准。
ROOT = Path(__file__).resolve().parents[2]
# 临时目录和验收证据会保存冻结源码副本，其中还可能包含目录联接；它们不是当前维护源码，
# 注释门禁不得递归扫描这些生成副本或外部依赖，否则失败归档会反向污染当前代码基线。
EXCLUDED_PARTS = {
    "node_modules",
    ".data",
    ".tmp",
    ".venv",
    ".artifacts",
    "artifacts",
    "dist",
    "__pycache__",
}
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".sql", ".css"}
# 只从正式源码根进入递归，避免先触碰仓库级 .tmp/.artifacts 再做无效的后置排除。
SOURCE_ROOT_NAMES = (
    "console-web",
    "control-plane-api",
    "examples",
    "gateway-data-plane",
    "infra",
    "scripts",
    "shared-sdk",
)
# 仓库根目录当前只有该 Python 文件属于注释门禁范围；显式列举可避免枚举临时目录节点。
ROOT_SOURCE_FILES = ("conftest.py",)
MIN_HAN_CHARS = 12
MAX_CSS_SECTION_GAP = 500
HEAD_CONTEXT_LINES = 8
FLOW_COMMENT_LOOKBACK = 3
HAN_RE = re.compile(r"[\u4e00-\u9fff]")
MOJIBAKE_COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*).*?\?{3,}.*$")
JS_DEFINITION_RE = re.compile(
    r"^(\s*)(export\s+)?(class|async\s+function|function)\s+([A-Za-z_][\w]*)"
    r"|^(\s*)constructor\s*\("
    r"|^(\s*)(export\s+)?(const|let|var)\s+[A-Za-z_][\w]*\s*=\s*(?:async\s*)?"
    r"(?:\([^)]*\)|[A-Za-z_][\w]*)\s*=>"
    r"|^(\s*)(export\s+)?(const|let|var)\s+[A-Za-z_][\w]*\s*=\s*(?:async\s*)?function\b"
)
JS_CONSTANT_RE = re.compile(r"^const\s+([A-Z][A-Z0-9_]*)\b")
BANNED_COMMENT_PATTERNS = (
    "说明该定义的职责",
    "说明该函数的页面职责",
    "页面职责、状态边界",
)
PY_CRITICAL_FLOW_RE = re.compile(
    r"^\s*((?:if|elif|except)\b.*:\s*$|raise\b|return\b|continue\b|break\b)"
)
JS_CRITICAL_FLOW_RE = re.compile(r"^\s*(if|else if|else|catch|throw|return|continue|break)\b")
CRITICAL_BRANCH_TERMS = tuple(
    term.lower()
    for term in (
    "ApiError(",
    "ConfigError(",
    "permission",
    "role",
    "tenant_id",
    "tenant",
    "auth",
    "token_hash",
    "key_hash",
    "encrypted_key",
    "provider_key",
    "authorization",
    "token",
    "session",
    "usage_ledger",
    "usage",
    "request_logs",
    "estimated_cost",
    "cost_",
    "currency",
    "error",
    "sqlite3.Error",
    "json.JSONDecodeError",
    "ValueError",
    "TypeError",
    "resolved_model",
    "model_alias",
    "model_id",
    "provider_code",
    "provider",
    "upstream",
    "route",
    "request_id",
    "trace_id",
    "Virtual Key",
    "Provider Key",
    "secret",
    )
)
CRITICAL_JUMP_TERMS = tuple(
    term.lower()
    for term in (
        "permission",
        "tenant_id",
        "tenant",
        "auth",
        "authorization",
        "token",
        "session",
        "token_hash",
        "key_hash",
        "encrypted_key",
        "provider_key",
        "Virtual Key",
        "Provider Key",
        "secret",
        "usage_ledger",
        "estimated_cost",
        "cost_",
        "request_logs",
    )
)
SQL_OPERATION_RE = re.compile(
    r"^\s*(CREATE TABLE IF NOT EXISTS|CREATE INDEX IF NOT EXISTS|ALTER TABLE|"
    r"DROP TABLE IF EXISTS|DROP INDEX IF EXISTS|UPDATE|INSERT INTO|DELETE FROM)\b",
    re.IGNORECASE,
)
# 已应用迁移连一条说明注释都不能补写；仅当规范化 SHA-256 精确匹配数据库登记值时，允许这些
# 已知历史操作沿用冻结前的注释基线。哈希或操作文本任一变化都会自动撤销豁免并重新触发门禁。
FROZEN_SQL_COMMENT_EXEMPTIONS: dict[str, tuple[str, frozenset[str]]] = {
    "infra/migrations/0051_user_invitation_join_flow.sql": (
        "4bc2793f8d825591195562415761375ba2881343e8b30b3099cfab01712a1c11",
        frozenset(
            {
                "INSERT INTO prd060_up_guard(invalid_count)",
                "INSERT INTO audit_logs_prd060_new(",
                "INSERT INTO prd060_audit_copy_guard(",
                "ALTER TABLE audit_logs_prd060_new RENAME TO audit_logs;",
                "INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)",
                "INSERT INTO prd060_final_guard(invalid_count)",
            }
        ),
    ),
}


def main() -> int:
    """检查全仓代码注释是否达到企业交付基线。

    门槛包含定义级说明、SQL 迁移说明、关键安全分支说明和模板化注释禁用，避免后续 PRD
    回到“看起来有中文但读不出边界”的状态。
    """

    missing: list[str] = []
    for path in iter_code_files():
        missing.extend(check_no_mojibake_comments(path))
        missing.extend(check_file_has_chinese_context(path))
        if path.suffix == ".py":
            missing.extend(check_python_file(path))
            missing.extend(check_python_constants(path))
            missing.extend(check_critical_flow(path, PY_CRITICAL_FLOW_RE))
        elif path.suffix in {".js", ".mjs"}:
            missing.extend(check_javascript_file(path))
            missing.extend(check_javascript_constants(path))
            missing.extend(check_critical_flow(path, JS_CRITICAL_FLOW_RE))
        elif path.suffix == ".sql":
            missing.extend(check_sql_file(path))
        elif path.suffix == ".css":
            missing.extend(check_css_file(path))

    if missing:
        print("Missing Chinese comments for definitions:")
        for item in missing:
            print(f"- {item}")
        return 1
    print("comment coverage ok")
    return 0


def iter_code_files() -> list[Path]:
    """列出需要执行注释检查的源码和迁移文件。

    生成目录、依赖目录和缓存目录不参与检查；测试、脚本和 SQL 迁移属于项目交付物，也会纳入。
    扫描从显式正式源码根开始，并在 ``os.walk`` 下钻前剪枝，保证不会打开仓库级 ``.tmp``，
    也不会遍历各模块内的依赖或验收证据目录。
    """

    files = [
        ROOT / filename
        for filename in ROOT_SOURCE_FILES
        if (ROOT / filename).is_file() and (ROOT / filename).suffix in SOURCE_SUFFIXES
    ]
    for root_name in SOURCE_ROOT_NAMES:
        source_root = ROOT / root_name
        if not source_root.is_dir():
            continue
        for current_root, directory_names, file_names in os.walk(source_root, topdown=True):
            # 必须在 walk 读取子目录前原地剪枝；结果阶段再过滤无法阻止目录联接或临时树被访问。
            directory_names[:] = [
                name for name in directory_names if name not in EXCLUDED_PARTS
            ]
            for filename in file_names:
                path = Path(current_root) / filename
                if path.suffix in SOURCE_SUFFIXES:
                    files.append(path)
    return sorted(files)


def check_file_has_chinese_context(path: Path) -> list[str]:
    """检查每个源码文件是否至少保留一段中文语境说明。
    这个门禁用于覆盖空包入口、纯导入文件和测试脚本，避免文件没有定义时绕过定义级注释检查。
    """

    text = path.read_text(encoding="utf-8")
    if has_head_chinese_context(text):
        return []
    return [f"{relative(path)}:1: file header lacks Chinese responsibility/boundary comment"]


def check_no_mojibake_comments(path: Path) -> list[str]:
    """检查注释中是否残留 Windows 编码污染产生的问号乱码。

    这类注释可能仍然让文件级中文数量达标，但对维护者没有信息价值，所以必须单独拦截。
    """

    missing: list[str] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if MOJIBAKE_COMMENT_RE.match(line):
            missing.append(f"{relative(path)}:{index}: mojibake comment must be rewritten")
    return missing


def check_python_file(path: Path) -> list[str]:
    """检查 Python class/function/async function 的中文 docstring 质量。

    使用 AST 读取真实 docstring，要求说明足够具体，并拒绝前期批量生成的模板化占位语句。
    """

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    # 源码语法错误会阻断 AST 检查，必须先以清晰的门禁失败返回。
    except SyntaxError as exc:
        return [f"{relative(path)}:{exc.lineno}: Python syntax error before comment check"]

    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        docstring = ast.get_docstring(node) or ""
        if is_useful_chinese_comment(docstring):
            continue
        missing.append(f"{relative(path)}:{node.lineno}: {node.name}")
    return missing


def check_javascript_file(path: Path) -> list[str]:
    """检查 JavaScript class/function/constructor 前是否有可读中文 JSDoc。

    前端代码以 JSDoc 作为定义说明载体；行内回调不强制要求，但顶层定义和构造函数必须说明职责和边界。
    """

    lines = path.read_text(encoding="utf-8").splitlines()
    missing: list[str] = []
    for index, line in enumerate(lines):
        if not JS_DEFINITION_RE.match(line):
            continue
        if has_useful_chinese_jsdoc(lines, index):
            continue
        missing.append(f"{relative(path)}:{index + 1}: {line.strip()}")
    return missing


def check_python_constants(path: Path) -> list[str]:
    """检查 Python 顶层常量组是否有中文业务说明。

    角色集合、错误码表、敏感字段名单和限额常量都会直接影响权限、审计或治理行为；这些定义本身也要有
    中文注释，不能只依赖使用处的函数 docstring。"""

    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    # 源码语法错误会阻断常量组扫描，必须先以清晰的门禁失败返回。
    except SyntaxError as exc:
        return [f"{relative(path)}:{exc.lineno}: Python syntax error before constant comment check"]

    lines = text.splitlines()
    groups: list[tuple[int, list[str]]] = []
    current_start: int | None = None
    current_end: int | None = None
    current_names: list[str] = []
    for node in tree.body:
        names = python_constant_names(node)
        if not names:
            if current_start is not None:
                groups.append((current_start, current_names))
            current_start = None
            current_end = None
            current_names = []
            continue
        node_start = int(getattr(node, "lineno", 1))
        node_end = int(getattr(node, "end_lineno", node_start))
        if current_start is None or (current_end is not None and node_start > current_end + 3):
            if current_start is not None:
                groups.append((current_start, current_names))
            current_start = node_start
            current_names = []
        current_names.extend(names)
        current_end = node_end
    if current_start is not None:
        groups.append((current_start, current_names))

    missing: list[str] = []
    for line_no, names in groups:
        if has_nearby_definition_comment(lines, line_no - 1):
            continue
        missing.append(f"{relative(path)}:{line_no}: constants {', '.join(names)}")
    return missing


def python_constant_names(node: ast.AST) -> list[str]:
    """提取顶层常量赋值里的全大写名称。

    只约束模块级常量，不检查函数内部临时变量，避免把局部实现细节误判为需要文档化的业务定义。"""

    names: list[str] = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and is_constant_name(target.id):
                names.append(target.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and is_constant_name(node.target.id):
        names.append(node.target.id)
    return names


def check_javascript_constants(path: Path) -> list[str]:
    """检查 JavaScript 顶层全大写常量组是否有中文说明。

    控制台里的角色集合、状态标签和选项列表是前端权限体验与业务展示定义，必须在定义处说明用途和后端边界。"""

    lines = path.read_text(encoding="utf-8").splitlines()
    groups: list[tuple[int, list[str]]] = []
    current_start: int | None = None
    current_line: int | None = None
    current_names: list[str] = []
    for index, line in enumerate(lines):
        match = JS_CONSTANT_RE.match(line)
        if not match:
            continue
        line_no = index + 1
        if current_start is None or (current_line is not None and line_no > current_line + 5):
            if current_start is not None:
                groups.append((current_start, current_names))
            current_start = line_no
            current_names = []
        current_names.append(match.group(1))
        current_line = line_no
    if current_start is not None:
        groups.append((current_start, current_names))

    missing: list[str] = []
    for line_no, names in groups:
        if has_nearby_definition_comment(lines, line_no - 1):
            continue
        missing.append(f"{relative(path)}:{line_no}: constants {', '.join(names)}")
    return missing


def check_sql_file(path: Path) -> list[str]:
    """检查数据库迁移操作前是否有中文说明。

    表、索引、字段变更和种子数据操作都会影响后续排障与回滚，因此每个 SQL 操作块都要说明意图。
    """

    lines = path.read_text(encoding="utf-8").splitlines()
    frozen_exemptions = frozen_sql_comment_exemptions(path)
    missing: list[str] = []
    for index, line in enumerate(lines):
        if not SQL_OPERATION_RE.match(line):
            continue
        if has_nearby_sql_comment(lines, index):
            continue
        # 豁免只覆盖哈希绑定的已应用迁移操作；不能按文件名宽松跳过后续内容变化。
        if line.strip() in frozen_exemptions:
            continue
        missing.append(f"{relative(path)}:{index + 1}: {line.strip()}")
    return missing


def frozen_sql_comment_exemptions(path: Path) -> frozenset[str]:
    """返回哈希精确匹配时允许沿用历史注释基线的 SQL 操作。

    参数：path 必须是仓库内迁移文件。返回值：只读操作文本集合。
    关键约束：使用与迁移器相同的 UTF-8 文本规范化哈希；任何字节级业务文本变化都失败关闭。
    """

    entry = FROZEN_SQL_COMMENT_EXEMPTIONS.get(relative(path))
    if entry is None:
        return frozenset()
    expected_checksum, operations = entry
    normalized = path.read_text(encoding="utf-8").encode("utf-8")
    if hashlib.sha256(normalized).hexdigest() != expected_checksum:
        return frozenset()
    return operations


def check_critical_flow(path: Path, flow_re: re.Pattern[str]) -> list[str]:
    """检查安全和治理关键分支附近是否有中文说明。

    分支类语句覆盖租户、权限、密钥、日志、成本、路由、错误和链路标识等高风险关键词；
    return/continue/break 只检查更窄的安全与审计关键词，避免给普通响应构造塞无意义注释。
    """

    lines = path.read_text(encoding="utf-8").splitlines()
    missing: list[str] = []
    for index, line in enumerate(lines):
        match = flow_re.match(line)
        if not match:
            continue
        # 跳转语句只检查真正影响安全、租户、密钥、用量和审计的关键词，避免注释噪音。
        terms = terms_for_flow_keyword(match.group(1))
        lower_line = line.lower()
        if not any(term in lower_line for term in terms):
            continue
        if has_nearby_flow_comment(lines, index):
            continue
        missing.append(f"{relative(path)}:{index + 1}: {line.strip()}")
    return missing


def check_css_file(path: Path) -> list[str]:
    """检查大型 CSS 文件是否有足够密度的中文分段说明。

    CSS 没有函数定义，但控制台样式会承载大量业务面板和响应式边界；要求分段说明能让维护者快速定位模块，
    同时避免为每个选择器写机械注释造成噪音。
    """

    lines = path.read_text(encoding="utf-8").splitlines()
    chinese_comment_lines = [
        index
        for index, line in enumerate(lines, start=1)
        if "/*" in line and "*/" in line and is_useful_chinese_comment(line)
    ]
    if not chinese_comment_lines:
        return [f"{relative(path)}:1: css lacks Chinese section comments"]

    missing: list[str] = []
    previous = 1
    for current in chinese_comment_lines:
        if current - previous > MAX_CSS_SECTION_GAP:
            missing.append(
                f"{relative(path)}:{previous}: css section gap exceeds {MAX_CSS_SECTION_GAP} lines before line {current}"
            )
        previous = current
    if len(lines) - previous > MAX_CSS_SECTION_GAP:
        missing.append(
            f"{relative(path)}:{previous}: css section gap exceeds {MAX_CSS_SECTION_GAP} lines before file end"
        )
    return missing


def terms_for_flow_keyword(keyword: str) -> tuple[str, ...]:
    """根据控制流关键字选择对应的高风险关键词集合。

    return/continue/break 使用更窄集合，if/elif/except/raise/catch/throw 使用完整治理集合，
    这样既能覆盖关键判断和跳出路径，也避免把普通 UI 或响应返回注释成噪音。
    """

    if keyword.startswith(("return", "continue", "break")):
        return CRITICAL_JUMP_TERMS
    return CRITICAL_BRANCH_TERMS


def has_nearby_flow_comment(lines: list[str], flow_index: int) -> bool:
    """检查关键控制流附近是否存在直接解释该判断原因的中文注释。

    只承认分支前方小范围内的注释行，避免文件头、字符串文案或距离很远的 JSDoc 被误判成当前判断的说明。
    """

    for probe in range(max(0, flow_index - FLOW_COMMENT_LOOKBACK), flow_index + 1):
        stripped = lines[probe].strip()
        if not stripped.startswith(("#", "//", "/*", "*")):
            continue
        if is_useful_chinese_comment(stripped):
            return True
    return False


def has_useful_chinese_jsdoc(lines: list[str], definition_index: int) -> bool:
    """判断定义前紧邻 JSDoc 是否达到中文说明门槛。

    只检查定义前的小块注释，避免文件顶部说明被误认为每个函数的注释。
    """

    start = max(0, definition_index - 12)
    text = "\n".join(lines[start:definition_index])
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return any(token in text for token in ("/**", "*/", "//")) and is_useful_chinese_comment(text)


def has_nearby_sql_comment(lines: list[str], operation_index: int) -> bool:
    """判断 SQL 操作前最近的有效行是否为中文注释。"""

    for probe in range(operation_index - 1, max(-1, operation_index - 4), -1):
        stripped = lines[probe].strip()
        if not stripped:
            continue
        return stripped.startswith("--") and is_useful_chinese_comment(stripped)
    return False


def has_nearby_definition_comment(lines: list[str], definition_index: int) -> bool:
    """判断定义前最近几行是否有足够中文说明。

    允许注释和空行夹在定义前，但不会把文件顶部很远的说明当作当前常量组说明。"""

    for probe in range(definition_index - 1, max(-1, definition_index - 6), -1):
        stripped = lines[probe].strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "//", "/*", "*")):
            return is_useful_chinese_comment(stripped)
        return False
    return False


def is_constant_name(name: str) -> bool:
    """判断名称是否是需要说明的模块级常量。"""

    return name.isupper() and not name.startswith("__")


def is_useful_chinese_comment(text: str) -> bool:
    """判断注释是否包含足量中文且不是模板占位。"""

    if any(pattern in text for pattern in BANNED_COMMENT_PATTERNS):
        return False
    return has_enough_chinese(text)


def has_head_chinese_context(text: str) -> bool:
    """检查文件开头是否提供维护者能立即看到的中文职责说明。

    只扫描前几行，防止文件靠很靠后的函数注释通过门禁，导致用户打开文件时看不到模块职责和安全边界。
    """

    head = "\n".join(text.splitlines()[:HEAD_CONTEXT_LINES])
    return has_enough_chinese(head)


def has_enough_chinese(text: str) -> bool:
    """只判断文本里是否有足量中文，供文件级语境检查避开禁用模板列表自身。"""

    return len(HAN_RE.findall(text)) >= MIN_HAN_CHARS


def relative(path: Path) -> str:
    """返回便于 CI 输出阅读的相对路径。"""

    return path.relative_to(ROOT).as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
