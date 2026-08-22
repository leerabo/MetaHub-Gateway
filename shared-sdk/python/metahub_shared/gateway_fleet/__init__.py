"""导出 Gateway placement 签名快照与本地容量准入能力，边界是不暴露密钥或可变 owner。"""

from .runtime import (
    GatewayAdmission,
    GatewayCapacityController,
    PlacementSnapshot,
    admit_gateway_request,
    create_placement_snapshot,
    decode_placement_snapshot,
    encode_placement_snapshot,
    install_placement_snapshot,
    remove_placement_snapshot,
    verify_placement_snapshot,
)

__all__ = [
    "GatewayAdmission",
    "GatewayCapacityController",
    "PlacementSnapshot",
    "admit_gateway_request",
    "create_placement_snapshot",
    "decode_placement_snapshot",
    "encode_placement_snapshot",
    "install_placement_snapshot",
    "remove_placement_snapshot",
    "verify_placement_snapshot",
]
