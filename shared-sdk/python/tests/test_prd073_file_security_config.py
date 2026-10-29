"""PRD-073 文件安全扫描部署配置专项。"""

from __future__ import annotations

from pathlib import Path

import pytest
from metahub_shared.runtime.config import ConfigError, load_app_config


def test_file_security_config_defaults_and_supported_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认硬预算可运行，且受支持的部署覆盖会进入公开非敏感配置。

    参数：pytest 环境替换器。返回值：无。关键约束：这些值只能由部署配置提供，业务请求没有
    对应字段；扫描和 OCR 分开限流，避免图片任务占满普通 TXT 的全部容量。
    """

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    defaults = load_app_config("control-plane-api")
    assert defaults.file_security_scan_concurrency == 2
    assert defaults.file_security_ocr_concurrency == 1
    assert defaults.file_security_max_ocr_pages == 20
    assert defaults.file_security_max_images == 50
    assert defaults.file_security_max_image_pixels == 12_000_000
    assert defaults.file_security_max_total_pixels == 50_000_000

    monkeypatch.setenv("METAHUB_FILE_SECURITY_SCAN_CONCURRENCY", "4")
    monkeypatch.setenv("METAHUB_FILE_SECURITY_OCR_CONCURRENCY", "2")
    monkeypatch.setenv("METAHUB_FILE_SECURITY_MAX_OCR_PAGES", "80")
    monkeypatch.setenv("METAHUB_FILE_SECURITY_MAX_IMAGES", "120")
    monkeypatch.setenv("METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS", "20000000")
    monkeypatch.setenv("METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS", "90000000")
    configured = load_app_config("control-plane-api")
    assert configured.file_security_scan_concurrency == 4
    assert configured.file_security_ocr_concurrency == 2
    assert configured.file_security_max_ocr_pages == 80
    assert configured.file_security_max_images == 120
    assert configured.file_security_max_image_pixels == 20_000_000
    assert configured.file_security_max_total_pixels == 90_000_000


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("METAHUB_FILE_SECURITY_SCAN_CONCURRENCY", "5"),
        ("METAHUB_FILE_SECURITY_OCR_CONCURRENCY", "3"),
        ("METAHUB_FILE_SECURITY_MAX_OCR_PAGES", "101"),
        ("METAHUB_FILE_SECURITY_MAX_IMAGES", "201"),
        ("METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS", "50000001"),
        ("METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS", "200000001"),
    ],
)
def test_file_security_config_rejects_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """任一资源预算越界都在应用启动阶段失败，不能静默夹断或运行期扩容。

    参数：pytest 环境替换器以及待验证的变量和值。返回值：无。关键约束：错误只暴露统一
    配置分类，不启动一个与运维声明不一致的扫描器。
    """

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"


def test_file_security_config_rejects_total_pixel_budget_below_single_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总像素预算小于单图预算时拒绝启动，避免同一图片得到矛盾覆盖结论。

    参数：pytest 环境替换器。返回值：无。关键约束：关系约束必须在构建 OCR pool 前验证，
    不能等企业图片已经解密后才发现部署预算不可满足。
    """

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    monkeypatch.setenv("METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS", "12000000")
    monkeypatch.setenv("METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS", "11999999")
    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"


def test_file_security_runtime_dependency_pins_rapidocr_release() -> None:
    """运行镜像必须精确固定已验收的 OCR 版本，避免升级后仍伪报旧检测器版本。

    参数：无。返回值：无。关键约束：RapidOCR 的代码、模型和输出合同共同参与安全结论，
    因此不能使用允许后续版本漂移的范围依赖。
    """

    requirements_path = (
        Path(__file__).resolve().parents[3] / "infra" / "docker" / "requirements.runtime.txt"
    )
    requirements = requirements_path.read_text(encoding="utf-8").splitlines()
    assert requirements.count("rapidocr-onnxruntime==1.4.4") == 1
    assert not any(
        line.startswith("rapidocr-onnxruntime") and line != "rapidocr-onnxruntime==1.4.4"
        for line in requirements
    )
