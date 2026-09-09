"""Identical native metering contracts through real Workspace subscription creation."""

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd099_projection_atomicity as contracts

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database
projection_subject = contracts.projection_subject


@pytest.fixture
def projection_database(native_business_database):
    return native_business_database


test_projection_uses_usage_time_after_trial_ends = (
    contracts.test_projection_uses_usage_time_after_trial_ends
)
test_projection_does_not_borrow_entitlement_from_enqueue_time = (
    contracts.test_projection_does_not_borrow_entitlement_from_enqueue_time
)
test_projection_failure_rolls_back_new_cycle_and_allocations = (
    contracts.test_projection_failure_rolls_back_new_cycle_and_allocations
)
test_late_projection_failure_keeps_frozen_facts = (
    contracts.test_late_projection_failure_keeps_frozen_facts
)
