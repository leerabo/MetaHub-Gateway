"""PRD-066 浏览器运行编排器的纯沙箱合同测试。

测试只使用随机回环端口和 pytest 已重定向到 ``D:/MetaHub/.artifacts`` 的临时目录；不启动
Edge、不读取开发数据库、不访问外网，也不写入或枚举废弃临时目录。
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest


# 脚本文件名包含连字符，按工作区物理路径加载可避免误执行安装环境中的同名模块。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd066-browser-runtime.py"
MODULE_SPEC = importlib.util.spec_from_file_location("prd066_browser_runtime", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块只注册供测试调用；``if __name__`` 入口不会启动服务、Provider 或浏览器。
RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME
MODULE_SPEC.loader.exec_module(RUNTIME)


def _free_port() -> int:
    """由内核分配一个当前空闲回环端口，随后立即交给单测 Provider。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def test_provider_endpoint_accepts_only_plain_loopback_http() -> None:
    """验证 Provider 只能使用无凭据、无查询参数的本机 HTTP，拒绝外网和敏感 URL。"""

    valid = RUNTIME.BrowserMockProvider(f"http://127.0.0.1:{_free_port()}/v1")
    assert valid.call_count == 0
    for value in (
        "https://127.0.0.1:19080/v1",
        "http://example.com:19080/v1",
        "http://user:password@127.0.0.1:19080/v1",
        "http://127.0.0.1:19080/v1?token=hidden",
        "http://127.0.0.1:19080/v1#fragment",
    ):
        with pytest.raises(RUNTIME.BrowserRuntimeFailure) as rejected:
            RUNTIME.BrowserMockProvider(value)
        assert rejected.value.stage == "provider.endpoint"


def test_mock_provider_validates_shape_and_keeps_only_counts() -> None:
    """合法 Chat 返回规范 Usage，非法路径只增加计数且对象不保存请求正文。"""

    port = _free_port()
    provider = RUNTIME.BrowserMockProvider(f"http://127.0.0.1:{port}/v1")
    prompt_canary = "PRD066_BROWSER_UNIT_PROMPT_MEMORY_ONLY"
    provider.start()
    try:
        success = httpx.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            headers={"Authorization": "Bearer unit-only-key"},
            json={
                "model": "local-chat-model",
                "messages": [{"role": "user", "content": prompt_canary}],
                "stream": False,
            },
            timeout=5.0,
        )
        invalid = httpx.post(
            f"http://127.0.0.1:{port}/wrong",
            headers={"Authorization": "Bearer unit-only-key"},
            json={
                "model": "local-chat-model",
                "messages": [{"role": "user", "content": prompt_canary}],
                "stream": False,
            },
            timeout=5.0,
        )
    finally:
        # Provider 由本测试创建，正常和断言失败分支都必须释放监听线程。
        provider.stop()

    assert success.status_code == 200
    assert success.json()["usage"] == {
        "prompt_tokens": 13,
        "completion_tokens": 5,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    assert invalid.status_code == 400
    assert provider.call_count == 1
    assert provider.invalid_count == 1
    # 正文只能存在于当前测试栈和 HTTP 调用栈，Provider 对象不得持久保留来访 payload。
    assert prompt_canary not in repr(vars(provider))


def test_runtime_evidence_contains_only_fixed_counts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """编排证据只允许浏览器/服务/Provider/清理计数，不包含本机身份或正文。"""

    cleanup = {
        "active_chat_sessions": 0,
        "issued_grants": 0,
        "in_flight_messages": 0,
        "integrity": "ok",
        "foreign_key_violations": 0,
    }
    RUNTIME._write_runtime_evidence(
        tmp_path,
        provider_calls=2,
        invalid_provider_calls=0,
        cleanup=cleanup,
    )
    evidence = json.loads(
        (tmp_path / "browser-runtime-evidence.json").read_text(encoding="utf-8")
    )
    assert set(evidence) == {"status", "prd_id", "browser", "services", "provider", "closeout"}
    assert evidence["provider"] == {"calls": 2, "invalid_requests": 0}
    assert evidence["closeout"] == cleanup
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "passed"
    assert len(output["browser_evidence_sha256"]) == 64


def test_source_has_no_forbidden_temporary_or_escalation_path() -> None:
    """源码门禁禁止回退到旧临时目录、patch 中转或任何提权请求。"""

    source = SCRIPT_PATH.read_text(encoding="utf-8")
    # 拆分禁用目录字面量，避免门禁测试自身被误认为在声明可访问路径。
    forbidden_temp = "." + "tmp"
    assert forbidden_temp not in source
    assert "C:" + "\\tmp" not in source
    assert "." + "patch" not in source
    assert "require_" + "escalated" not in source
    assert 'REPOSITORY_ROOT / ".artifacts" / "prd066-browser"' in source
    assert "subprocess.run(" in source
    assert "Start-Process" not in source
