from __future__ import annotations

import uvicorn


def main() -> None:
    # 本地开发默认监听 8001，与数据面 8000 分开，方便新员工确认模块边界。
    """启动 控制面 API 服务进程，保持 ASGI 应用装配和命令行入口分离。
    
    边界：这里不写业务分支，业务权限、租户隔离和错误码仍由下层模块负责。
    """


    uvicorn.run(
        "metahub_control.app:app",
        host="0.0.0.0",
        port=8001,
        reload=False,
        # 高级筛选使用有界 Base64URL query。Uvicorn 默认 access log 会原样落完整 URL，
        # 因此控制面关闭该旁路日志，业务观测统一走携带 request_id 的脱敏结构化事件。
        access_log=False,
        # PRD-034 的 client_ip 只认 TCP 直接对端；未建立可信代理白名单前禁止 Uvicorn
        # 用 X-Forwarded-For/Forwarded 改写 request.client，避免客户端伪造审计来源。
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
