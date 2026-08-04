"""PRD-055 控制面访问日志隐私门禁。

该测试只检查启动参数，不启动网络服务；高级筛选值不得被 Uvicorn 默认 access log
原样持久化，业务追踪改由携带 request_id 的脱敏结构化事件承担。
"""

from __future__ import annotations

from metahub_control import main as control_main


def test_control_plane_disables_raw_url_access_log(monkeypatch) -> None:
    """验证官方控制面启动入口关闭原始 URL access log，同时保留代理来源防伪约束。"""

    captured: dict[str, object] = {}

    def fake_run(target: str, **options: object) -> None:
        """记录 Uvicorn 启动合同，不执行真实监听或写入任何日志文件。"""

        captured["target"] = target
        captured.update(options)

    monkeypatch.setattr(control_main.uvicorn, "run", fake_run)
    control_main.main()

    assert captured["target"] == "metahub_control.app:app"
    assert captured["access_log"] is False
    assert captured["proxy_headers"] is False
