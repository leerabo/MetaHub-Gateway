"""运行 PRD-070 previous HMAC 移除前的全局只读验证。

脚本只从 MetaHub 正式启动配置读取数据库、Redis 和 current/previous 版本；不得通过命令行
传递 URL 或 Secret，避免进程列表泄密。调用前必须停止所有 Gateway 新写入，并保持两把密钥
仍配置。成功 stdout 只输出安全 JSON 计数；失败 stderr 只输出固定错误码。
"""

from __future__ import annotations

import asyncio
import json
import sys

from metahub_gateway.session_risk import (
    RedisSessionRiskStore,
    SessionRiskHmacRotationVerifierUnavailable,
    SessionRiskStoreUnavailable,
    verify_session_risk_hmac_removal,
)
from metahub_shared.runtime.config import ConfigError, load_app_config

# 退出码区分“引用尚未归零”和“无法完成验证”，自动化不能把基础设施故障当成待等待引用。
EXIT_READY = 0
EXIT_UNAVAILABLE = 2
EXIT_BLOCKED = 3


async def _run() -> int:
    """加载正式配置、运行 verifier，并输出不含敏感字段的 canonical JSON。"""

    config = load_app_config("gateway-data-plane")
    previous_version = config.session_risk_previous_hmac_version
    previous_secret = config.session_risk_previous_hmac_secret
    redis_url = config.redis_url
    if (
        not config.session_risk_enabled
        or type(previous_version) is not int
        or not previous_secret
        or not redis_url
    ):
        # verifier 必须在删配置前运行；缺少任一 previous 字段时无法证明旧 HMAC 引用。
        raise SessionRiskHmacRotationVerifierUnavailable()
    store = RedisSessionRiskStore(redis_url)
    report = await verify_session_risk_hmac_removal(
        config.database_url,
        store,
        current_version=config.session_risk_hmac_version,
        removing_version=previous_version,
    )
    print(
        json.dumps(
            report.as_safe_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return EXIT_READY if report.ready else EXIT_BLOCKED


def main() -> int:
    """把配置、数据库、Redis 或协议故障折叠为固定 stderr 和非零退出码。"""

    try:
        return asyncio.run(_run())
    # 配置错误文本可能包含变量状态；维护证据只保留固定错误码，不展开异常或 cause。
    except (
        ConfigError,
        SessionRiskHmacRotationVerifierUnavailable,
        SessionRiskStoreUnavailable,
    ):
        # Redis URL 在 Store 构造阶段就可能被拒绝，此时尚未进入 verifier 的异常转换层。
        print("session_risk.hmac_rotation_verifier_unavailable", file=sys.stderr)
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
