"""模块职责：允许 `python -m metahub_gateway.provider_contracts` 运行独立 CI 门禁。

维护边界：本入口只转交固定 CLI，不读取 Provider Key、客户正文或任意上游地址。
"""

from .cli import main


raise SystemExit(main())
