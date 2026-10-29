"""导出 PRD-075 模型实验的确定性分桶、配额、路由指纹与整数统计共享合同。"""

from .contracts import (
    EXPERIMENT_ALLOCATION_ALGORITHM_VERSION,
    EXPERIMENT_BUCKET_COUNT,
    ExperimentAllocation,
    ExperimentAllocationPlan,
    ExperimentAssignmentReason,
    ExperimentBuckets,
    build_experiment_buckets,
    build_experiment_create_request_fingerprint,
    build_experiment_idempotency_key_hash,
    build_experiment_key_fingerprint,
    build_experiment_route_config_fingerprint,
    calculate_variant_targets,
    choose_experiment_variant,
)
from .statistics import (
    ExperimentArmStatistics,
    ExperimentRequestFact,
    aggregate_experiment_arm,
)

__all__ = [
    "EXPERIMENT_ALLOCATION_ALGORITHM_VERSION",
    "EXPERIMENT_BUCKET_COUNT",
    "ExperimentAllocation",
    "ExperimentAllocationPlan",
    "ExperimentAssignmentReason",
    "ExperimentArmStatistics",
    "ExperimentBuckets",
    "ExperimentRequestFact",
    "aggregate_experiment_arm",
    "build_experiment_buckets",
    "build_experiment_create_request_fingerprint",
    "build_experiment_idempotency_key_hash",
    "build_experiment_key_fingerprint",
    "build_experiment_route_config_fingerprint",
    "calculate_variant_targets",
    "choose_experiment_variant",
]
