"""Isolated native database baseline for the PRD-131..133 business batch."""

from __future__ import annotations

import secrets

import pytest
import test_prd147_postgresql_runtime as native_helpers
from metahub_shared.db import apply_migrations, apply_seed_data, close_database_pools


@pytest.fixture(scope="session")
def native_business_template():
    name, database_url = native_helpers._create_database()
    try:
        assert {"0123", "0124", "0125", "0140"} <= set(apply_migrations(database_url).applied)
        assert not apply_migrations(database_url).applied
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(48))
            apply_seed_data(database_url)
        close_database_pools()
        yield name
    finally:
        native_helpers._drop_database(name)


@pytest.fixture
def native_business_database(native_business_template):
    name, database_url = native_helpers._create_database(template=native_business_template)
    try:
        yield database_url
    finally:
        native_helpers._drop_database(name)
