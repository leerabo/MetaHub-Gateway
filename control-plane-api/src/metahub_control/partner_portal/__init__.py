"""PRD-087 伙伴门户 API、分润物化与 EVT-099 恢复入口。"""

from .routes import router
from .scheduler import install_partner_portal_scheduler

__all__ = ["install_partner_portal_scheduler", "router"]
