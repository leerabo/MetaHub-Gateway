"""验证 README、环境模板、项目元数据和源码入口保持一致。"""

from __future__ import annotations

import ast
from pathlib import Path

# 项目根目录和包目录用于一致性扫描，确保测试不会越过 examples/python 边界。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "metahub_gateway_examples"


def test_required_project_files_and_module_entries_exist() -> None:
    """独立分发所需文件、Chat 和 Embeddings 模块必须全部存在。"""

    expected_paths = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / ".env.example",
        PROJECT_ROOT / "pyproject.toml",
        PACKAGE_ROOT / "__init__.py",
        PACKAGE_ROOT / "chat.py",
        PACKAGE_ROOT / "embeddings.py",
    ]

    assert all(path.is_file() for path in expected_paths)


def test_readme_commands_environment_names_and_modules_match_project() -> None:
    """README 命令与环境变量必须和实际模块、模板及项目脚本一致。"""

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    variables = [
        "METAHUB_BASE_URL",
        "METAHUB_VIRTUAL_KEY",
        "METAHUB_CHAT_MODEL",
        "METAHUB_EMBEDDING_MODEL",
        "METAHUB_TIMEOUT_SECONDS",
        "METAHUB_MAX_RETRIES",
    ]

    assert "python -m metahub_gateway_examples.chat" in readme
    assert "python -m metahub_gateway_examples.embeddings" in readme
    assert "metahub-chat-example" in pyproject
    assert "metahub-embeddings-example" in pyproject
    assert all(variable in readme for variable in variables)
    assert all(variable in env_example for variable in variables)


def test_environment_template_contains_placeholder_instead_of_real_key() -> None:
    """分发模板必须只含明显占位 Virtual Key，不能包含可用凭据格式。"""

    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "METAHUB_VIRTUAL_KEY=your-virtual-key" in env_example
    assert "Authorization: Bearer" not in env_example
    assert "sk-" not in env_example
    assert "vk_" not in env_example


def test_runtime_source_uses_no_openai_private_module_imports() -> None:
    """运行时代码只能惰性加载官方顶层包，不能依赖下划线私有模块。"""

    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_ROOT.glob("*.py"))
    )

    assert "openai._" not in source_text
    assert "from openai" not in source_text
    assert 'import_module("openai")' in source_text


def test_every_python_definition_has_a_concrete_chinese_docstring() -> None:
    """源码与测试中的每个类和函数定义都必须具备包含中文的具体 docstring。"""

    python_files = sorted((PROJECT_ROOT / "src").rglob("*.py")) + sorted(
        (PROJECT_ROOT / "tests").rglob("*.py")
    )
    missing: list[str] = []
    for path in python_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(node, clean=True)
            has_chinese = bool(docstring) and any(
                "\u4e00" <= char <= "\u9fff" for char in docstring
            )
            if not has_chinese:
                missing.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{node.name}")

    assert missing == []
