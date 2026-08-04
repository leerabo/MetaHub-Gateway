"""Gateway 后台治理事件队列入口，隔离模型热路径与 Webhook 网络 I/O。"""

from .dispatcher import enqueue_runtime_event, install_runtime_event_dispatcher

__all__ = ["enqueue_runtime_event", "install_runtime_event_dispatcher"]
