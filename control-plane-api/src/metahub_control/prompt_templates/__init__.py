"""PRD-067 企业 Prompt 模板控制面领域。

公开路由、严格 DTO 和事务服务集中在本包。模板正文只在授权请求、独立 AES-GCM 加解密
和单次 Gateway 渲染调用栈中存在，不得进入普通日志、审计摘要或 Webhook payload。
"""

# 路由由 ``metahub_control.app`` 显式从 ``routes`` 导入，包入口不主动装配 FastAPI，避免
# 测试仅导入 DTO 或共享服务时触发数据库/路由循环依赖。
__all__: list[str] = []
