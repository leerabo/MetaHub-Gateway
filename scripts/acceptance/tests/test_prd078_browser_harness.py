"""PRD-078 Browser harness 的边界、隐私、双视口与证据合同。"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from PIL import Image


def load_harness_module():
    """按真实脚本路径加载 harness，导入本身不得启动服务或创建目录。"""

    path = Path(__file__).resolve().parents[1] / "prd078-browser-harness.py"
    specification = importlib.util.spec_from_file_location(
        "prd078_browser_harness_contract",
        path,
    )
    if specification is None or specification.loader is None:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise AssertionError("unable to load PRD-078 browser harness")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 测试只调用已隔离加载的 harness，不得因导入启动浏览器、服务或固定端口监听。
HARNESS = load_harness_module()


def complete_browser_facts() -> dict[str, object]:
    """构造最终发布门禁接受的完整双视口交互事实。"""

    return {
        "status": "passed",
        "desktop": {
            "width": 1280,
            "height": 720,
            "nonblank": True,
            "horizontal_overflow": False,
            "text_overflow": False,
            "overlap_count": 0,
            "finance_budget_only": True,
            "native_modal": True,
            "background_blocked": True,
            "standard_initial_focus": True,
            "tab_forward_wrap": True,
            "tab_reverse_wrap": True,
            "idle_escape_closed": True,
            "focus_restored": True,
            "decision_completed": True,
        },
        "mobile": {
            "width": 390,
            "height": 844,
            "nonblank": True,
            "horizontal_overflow": False,
            "text_overflow": False,
            "overlap_count": 0,
            "security_risk_only": True,
            "native_modal": True,
            "high_risk_initial_focus": True,
            "idle_escape_closed": True,
            "idle_escape_password_cleared": True,
            "busy_native_modal": True,
            "busy_escape_blocked": True,
            "busy_cancel_blocked": True,
            "step_up_completed": True,
        },
        "lifecycle": {
            "owner_drift_closed": True,
            "owner_drift_password_cleared": True,
            "owner_drift_focus_not_restored": True,
            "late_success_discarded": True,
            "permission_revocation_closed": True,
            "permission_revocation_password_cleared": True,
            "permission_revocation_root_cleared": True,
            "late_denial_discarded": True,
            "logout_dialog_closed": True,
            "logout_password_cleared": True,
            "logout_root_cleared": True,
            "logout_storage_cleared": True,
        },
        "network": {
            "external_requests": 0,
            "unexpected_console_errors": 0,
            "runtime_exceptions": 0,
        },
        "screenshots": list(HARNESS.SCREENSHOTS),
    }


def test_browser_boundaries_are_repository_owned_and_loopback_only() -> None:
    """Harness 目录、端口、截图名与来源文件必须冻结在仓库和回环边界。"""

    root = Path(__file__).resolve().parents[3]
    assert HARNESS.ROOT == root
    assert HARNESS.WORK_ROOT == root / ".tmp" / "prd078-browser"
    assert HARNESS.ARTIFACT_ROOT == root / ".artifacts" / "prd078-browser"
    assert HARNESS.LOOPBACK == "127.0.0.1"
    assert HARNESS.GATEWAY_PORT == 8000
    assert HARNESS.GATEWAY_PORT not in HARNESS.RUNTIME.FIXED_PORTS
    assert HARNESS.HARNESS_PORT not in HARNESS.RUNTIME.FIXED_PORTS
    assert HARNESS.SCREENSHOTS == (
        "finance-standard-modal-desktop-1280x720.png",
        "security-high-risk-modal-mobile-390x844.png",
        "security-high-risk-busy-mobile-390x844.png",
    )
    assert all(path.resolve().is_relative_to(root) for path in HARNESS.SOURCE_FILES.values())


def test_browser_harness_owns_and_health_checks_real_gateway_before_control() -> None:
    """页面会在每次登录刷新 Gateway 健康；harness 必须真实启动而不能容忍连接拒绝。"""

    source = HARNESS.SOURCE_FILES["browser_harness"].read_text(encoding="utf-8")
    gateway_start = source.index('"metahub_gateway.app:app"')
    gateway_health = source.index('stage="health.gateway"')
    control_start = source.index('"metahub_control.app:app"')
    assert gateway_start < gateway_health < control_start
    assert '"gateway": "owned_and_stopped"' in source
    assert "for port in (GATEWAY_PORT, HARNESS_PORT):" in source


def test_browser_secrets_require_environment_and_never_expand_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """角色密码和停止 Token 必须显式提供，且所有私有值都有唯一扫描 owner。"""

    monkeypatch.delenv(HARNESS.ROLE_PASSWORD_ENV, raising=False)
    monkeypatch.delenv(HARNESS.STOP_TOKEN_ENV, raising=False)
    with pytest.raises(HARNESS.AcceptanceFailure) as missing:
        HARNESS.BrowserSecrets.from_environment()
    assert (missing.value.stage, missing.value.code) == (
        "config.role_password",
        "invalid",
    )

    role_password = "Prd078-Browser-Role-Password-" + "a" * 32
    stop_token = "Prd078-Browser-Stop-Token-" + "b" * 48
    monkeypatch.setenv(HARNESS.ROLE_PASSWORD_ENV, role_password)
    monkeypatch.setenv(HARNESS.STOP_TOKEN_ENV, stop_token)
    secrets_ = HARNESS.BrowserSecrets.from_environment()
    values = secrets_.private_values(("browser-extra-private",))
    assert role_password in values and stop_token in values
    assert len(values) == len(set(values)) == 15
    assert all(value not in repr(secrets_) for value in values)


@pytest.mark.parametrize(
    "unsafe",
    ("..", "../outside", "C:/outside", "a/b", "a\\b", "contains space", "x" * 81),
)
def test_browser_run_id_rejects_path_injection(unsafe: str) -> None:
    """Browser 批次名不得跨目录、使用绝对路径或携带宽松文本。"""

    with pytest.raises(HARNESS.AcceptanceFailure) as failure:
        HARNESS.RUNTIME.safe_run_id(unsafe)
    assert (failure.value.stage, failure.value.code) == ("config.run_id", "invalid")


def test_run_directories_are_exclusive_and_owner_claimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Work/staging 必须排他创建且 final 冲突不能被覆盖。"""

    work_root = tmp_path / "work"
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(HARNESS, "WORK_ROOT", work_root)
    monkeypatch.setattr(HARNESS, "ARTIFACT_ROOT", artifact_root)
    owner = "c" * 64
    work, staging, final = HARNESS.create_run_directories("prd078-browser-unit", owner)
    assert work.parent == work_root and staging.parent == artifact_root
    assert not final.exists()
    assert (work / HARNESS.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
    assert (staging / HARNESS.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
    with pytest.raises(HARNESS.AcceptanceFailure) as duplicate:
        HARNESS.create_run_directories("prd078-browser-unit", owner)
    assert (duplicate.value.stage, duplicate.value.code) == ("config.work", "exists")


def test_owned_cleanup_refuses_wrong_claim_then_removes_exact_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理必须保留未知目录，只删除直属且 owner claim 精确匹配的本轮 work。"""

    work_root = tmp_path / "work"
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(HARNESS, "WORK_ROOT", work_root)
    monkeypatch.setattr(HARNESS, "ARTIFACT_ROOT", artifact_root)
    owner = "d" * 64
    work, _staging, _final = HARNESS.create_run_directories("prd078-cleanup", owner)
    with pytest.raises(HARNESS.AcceptanceFailure) as wrong:
        HARNESS._remove_owned_directory(
            work,
            work_root,
            "e" * 64,
            stage="cleanup.unit",
        )
    assert (wrong.value.stage, wrong.value.code) == ("cleanup.unit", "claim")
    assert work.exists()
    HARNESS._remove_owned_directory(work, work_root, owner, stage="cleanup.unit")
    assert not work.exists()


def test_browser_facts_accept_only_exact_complete_dual_viewport_result() -> None:
    """最终证据不能用单视口、近似尺寸、非布尔布局或未完成动作冒充通过。"""

    facts = complete_browser_facts()
    assert HARNESS._validate_browser_facts(facts) is facts
    mutations = (
        ("desktop", "width", 1279),
        ("desktop", "horizontal_overflow", True),
        ("desktop", "overlap_count", 1),
        ("desktop", "decision_completed", False),
        ("mobile", "height", 843),
        ("mobile", "text_overflow", True),
        ("mobile", "step_up_completed", False),
        ("network", "external_requests", 1),
    )
    for section, name, value in mutations:
        changed = complete_browser_facts()
        changed[section][name] = value  # type: ignore[index]
        with pytest.raises(HARNESS.AcceptanceFailure):
            HARNESS._validate_browser_facts(changed)


def test_browser_facts_reject_extra_text_or_screenshot_name() -> None:
    """Browser 不能把页面正文、审批 ID 或任意本地路径写入完成信号。"""

    extra = complete_browser_facts()
    extra["page_text"] = "sensitive page body"
    with pytest.raises(HARNESS.AcceptanceFailure) as body:
        HARNESS._validate_browser_facts(extra)
    assert (body.value.stage, body.value.code) == ("browser.complete", "shape")

    wrong_screenshot = complete_browser_facts()
    wrong_screenshot["screenshots"] = [
        "C:/outside.png",
        HARNESS.SCREENSHOTS[1],
        HARNESS.SCREENSHOTS[2],
    ]
    with pytest.raises(HARNESS.AcceptanceFailure) as screenshot:
        HARNESS._validate_browser_facts(wrong_screenshot)
    assert (screenshot.value.stage, screenshot.value.code) == (
        "browser.complete",
        "screenshots",
    )


def _fake_png(width: int, height: int) -> bytes:
    """构造只满足旧 IHDR 检查的伪 PNG，证明完整解码门禁会拒绝。"""

    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x00" * 1024
    )


def test_png_gate_requires_full_decode_nonblank_pixels_and_exact_dimensions(
    tmp_path: Path,
) -> None:
    """截图必须可完整解码且非空；伪 IHDR、错视口和单色占位均失败关闭。"""

    valid = tmp_path / "valid.png"
    image = Image.new("RGB", (1280, 720))
    image.putdata(
        [
            ((x * 17) % 256, (y * 29) % 256, ((x + y) * 11) % 256)
            for y in range(720)
            for x in range(1280)
        ]
    )
    image.save(valid, format="PNG")
    HARNESS._validate_png(valid, 1280, 720)

    fake = tmp_path / "fake.png"
    fake.write_bytes(_fake_png(1280, 720))
    with pytest.raises(HARNESS.AcceptanceFailure) as fake_failure:
        HARNESS._validate_png(fake, 1280, 720)
    assert (fake_failure.value.stage, fake_failure.value.code) == (
        "browser.screenshot",
        "decode",
    )

    with pytest.raises(HARNESS.AcceptanceFailure) as dimensions:
        HARNESS._validate_png(valid, 390, 844)
    assert (dimensions.value.stage, dimensions.value.code) == (
        "browser.screenshot",
        "dimensions",
    )
    blank = tmp_path / "blank.png"
    Image.new("RGB", (1280, 720), (255, 255, 255)).save(blank, format="PNG")
    with pytest.raises(HARNESS.AcceptanceFailure) as blank_failure:
        HARNESS._validate_png(blank, 1280, 720)
    assert (blank_failure.value.stage, blank_failure.value.code) == (
        "browser.screenshot",
        "blank",
    )
    tiny = tmp_path / "tiny.png"
    tiny.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(HARNESS.AcceptanceFailure) as missing:
        HARNESS._validate_png(tiny, 1280, 720)
    assert (missing.value.stage, missing.value.code) == (
        "browser.screenshot",
        "missing",
    )


def test_source_provenance_binds_harness_runtime_and_page_implementation() -> None:
    """最终 SHA 集必须逐一绑定本轮 harness、Runtime 和审批页面四层实现。"""

    provenance = HARNESS._source_provenance()
    assert set(provenance) == set(HARNESS.SOURCE_FILES)
    for name, path in HARNESS.SOURCE_FILES.items():
        assert provenance[name] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(provenance[name]) == 64


def test_main_missing_private_environment_uses_fixed_error_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """配置失败只能输出固定坐标，不能回显参数或环境私有值。"""

    monkeypatch.delenv(HARNESS.ROLE_PASSWORD_ENV, raising=False)
    monkeypatch.delenv(HARNESS.STOP_TOKEN_ENV, raising=False)
    private_run_id = "private-run-id"
    assert HARNESS.main(["--run-id", private_run_id]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("PRD-078 browser harness failed: config.role_password:invalid\n")
    assert private_run_id not in captured.err


def test_browser_private_values_do_not_leak_to_environment_after_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harness 读取不会改写调用方环境；清理由启动者负责删除其临时键。"""

    role_password = "Prd078-Role-" + "f" * 40
    stop_token = "Prd078-Stop-" + "0" * 48
    monkeypatch.setenv(HARNESS.ROLE_PASSWORD_ENV, role_password)
    monkeypatch.setenv(HARNESS.STOP_TOKEN_ENV, stop_token)
    HARNESS.BrowserSecrets.from_environment()
    assert os.environ[HARNESS.ROLE_PASSWORD_ENV] == role_password
    assert os.environ[HARNESS.STOP_TOKEN_ENV] == stop_token
