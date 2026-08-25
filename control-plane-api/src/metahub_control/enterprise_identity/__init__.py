"""Enterprise SSO and SCIM directory transports."""

from fastapi import APIRouter

from .routes import router as enterprise_identity_router
from .scim_routes import router as scim_router

router = APIRouter()
router.include_router(enterprise_identity_router)
router.include_router(scim_router)

__all__ = ["router"]
