from __future__ import annotations

import uvicorn


def main() -> None:
    # 本地开发默认监听 8000；生产部署端口后续由容器/部署配置覆盖。
    """启动 网关数据面 服务进程，保持 ASGI 应用装配和命令行入口分离。
    
    边界：这里不写业务分支，业务权限、租户隔离和错误码仍由下层模块负责。
    """


    uvicorn.run(
        "metahub_gateway.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
