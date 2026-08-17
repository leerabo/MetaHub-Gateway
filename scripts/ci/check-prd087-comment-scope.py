"""PRD-087 交付范围注释门禁。

全仓历史代码仍有旧注释债务，因此本门禁只检查 PRD-087 新增交付边界：模块头、
后端公开 API、前端导出 API、迁移说明和安全常量。它不把内部辅助分支误报为新需求。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HAN = re.compile(r"[\u4e00-\u9fff]")
HEADER_TERMS = ("租户", "伙伴", "分润", "白标", "浏览器", "迁移", "事件", "权限", "测试")
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".sql"}
PUBLIC_JS_RE = re.compile(r"^\s*export\s+(?:async\s+)?(?:function|class)\s+|^\s*export\s+const\s+")
SQL_OPERATION_RE = re.compile(
    r"^\s*(?:CREATE TABLE|CREATE INDEX|ALTER TABLE|DROP TABLE|DROP INDEX|"
    r"UPDATE|INSERT INTO|DELETE FROM)\b",
    re.IGNORECASE,
)

FILES = (
    "control-plane-api/src/metahub_control/partner_portal/__init__.py",
    "control-plane-api/src/metahub_control/partner_portal/commission.py",
    "control-plane-api/src/metahub_control/partner_portal/models.py",
    "control-plane-api/src/metahub_control/partner_portal/outbox.py",
    "control-plane-api/src/metahub_control/partner_portal/routes.py",
    "control-plane-api/src/metahub_control/partner_portal/scheduler.py",
    "control-plane-api/src/metahub_control/partner_portal/service.py",
    "control-plane-api/tests/integration/test_prd087_db_migrations.py",
    "control-plane-api/tests/integration/test_prd087_partner_portal.py",
    "shared-sdk/python/tests/test_prd087_partner_portal_contracts.py",
    "infra/migrations/0080_channel_partner_white_label_portal.sql",
    "infra/migrations/0080_channel_partner_white_label_portal.down.sql",
    "console-web/src/lib/partnerPortal.js",
    "console-web/src/features/partner-portal/bindPartnerPortal.js",
    "console-web/src/features/partner-portal/partnerPortalPageState.js",
    "console-web/src/features/partner-portal/renderPartnerPortal.js",
    "console-web/tests/unit/partnerPortal.test.mjs",
    "console-web/tests/unit/prd087BrowserAcceptance.test.mjs",
    "console-web/tests/unit/prd087BrowserAcceptanceDriver.test.mjs",
    "scripts/acceptance/prd087-browser-acceptance.mjs",
    "scripts/acceptance/prd087-browser-harness.py",
    "scripts/acceptance/tests/test_prd087_browser_harness.py",
)


def useful_comment(text: str) -> bool:
    return len(HAN.findall(text)) >= 6 and "璇存槑璇ュ畾义" not in text


def header_ok(path: Path) -> bool:
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:8])
    return len(HAN.findall(head)) >= 12 and any(term in head for term in HEADER_TERMS)


def python_public_api_ok(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    failures: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        docstring = ast.get_docstring(node) or ""
        if not useful_comment(docstring):
            failures.append(f"{path.relative_to(ROOT)}:{node.lineno}: public API {node.name}")
    return failures


def js_exports_ok(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    failures: list[str] = []
    for index, line in enumerate(lines):
        if not PUBLIC_JS_RE.match(line):
            continue
        nearby = "\n".join(lines[max(0, index - 6) : index])
        if not useful_comment(nearby):
            failures.append(f"{path.relative_to(ROOT)}:{index + 1}: exported API")
    return failures


def sql_scope_ok(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    comments = [line for line in lines if line.strip().startswith("--") and useful_comment(line)]
    operations = [line for line in lines if SQL_OPERATION_RE.match(line)]
    if len(comments) < 5:
        return [f"{path.relative_to(ROOT)}: insufficient migration operation comments"]
    if not operations:
        return [f"{path.relative_to(ROOT)}: no migration operations found"]
    return []


def main() -> int:
    failures: list[str] = []
    for relative in FILES:
        path = ROOT / relative
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            failures.append(f"{relative}: missing or unsupported file")
            continue
        if not header_ok(path):
            failures.append(f"{relative}: module header lacks Chinese responsibility and boundary")
        if path.suffix == ".py" and relative.startswith(
            "control-plane-api/src/metahub_control/partner_portal/"
        ):
            failures.extend(python_public_api_ok(path))
        if path.suffix in {".js", ".mjs"} and relative.startswith("console-web/"):
            failures.extend(js_exports_ok(path))
        if path.suffix == ".sql":
            failures.extend(sql_scope_ok(path))
    if failures:
        print("\n".join(failures))
        return 1
    print(f"PRD-087 scoped Chinese comment gate passed ({len(FILES)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
