"""Run explanation ownership contracts against native PostgreSQL."""

import test_prd147_workspace_explanations as contracts
from test_prd147_postgresql_feedback import (  # noqa: F401
    feedback_subjects,
    file_subjects,
    native_business_database,
    native_business_template,
    pytestmark,
)

explanation_subjects = contracts.explanation_subjects
test_explanation_projection_uses_commit_permission_snapshot = (
    contracts.test_explanation_projection_uses_commit_permission_snapshot
)
test_workspace_explanation_http_scope_and_real_owner = (
    contracts.test_workspace_explanation_http_scope_and_real_owner
)
test_explanation_terminal_expiry_leaves_no_access_fact = (
    contracts.test_explanation_terminal_expiry_leaves_no_access_fact
)
test_explanation_read_rechecks_revocation_outside_snapshot = (
    contracts.test_explanation_read_rechecks_revocation_outside_snapshot
)
test_explanation_owner_xor_outbox_and_lossless_down_guard = (
    contracts.test_explanation_owner_xor_outbox_and_lossless_down_guard
)
test_explanation_legacy_history_roundtrip_and_drift_guard = (
    contracts.test_explanation_legacy_history_roundtrip_and_drift_guard
)
