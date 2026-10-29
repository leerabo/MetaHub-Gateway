"""Provider Adapter 契约测试框架。

该包只供研发 CI 和受控验收使用，不进入 Gateway 请求热路径。公开对象均只携带租户、
稳定标识、数值断言和固定错误码，不保存 Provider Key、Endpoint、Prompt 或响应正文。
"""

from .fixtures import CONTRACT_FIXTURES
from .matrix import build_contract_matrix, validate_fixture_coverage
from .models import (
    AdapterContractFixture,
    ContractCaseDefinition,
    ContractCaseResult,
    ContractRunReport,
)
from .probe import GatewayProbeConfig, ProbeConfigError
from .upload import ControlUploadConfig, ControlUploadError

__all__ = [
    "AdapterContractFixture",
    "CONTRACT_FIXTURES",
    "ContractCaseDefinition",
    "ContractCaseResult",
    "ContractRunReport",
    "ControlUploadConfig",
    "ControlUploadError",
    "GatewayProbeConfig",
    "ProbeConfigError",
    "build_contract_matrix",
    "validate_fixture_coverage",
]
