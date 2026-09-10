"""Native parity for Workspace SDK downloads and authorization revalidation."""

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
from test_workspace_sdk_examples import (
    verify_sdk_download_revocation,
    verify_workspace_sdk_boundary,
)

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


@pytest.mark.anyio
async def test_native_workspace_sdk_boundary(native_business_database, tmp_path, monkeypatch):
    await verify_workspace_sdk_boundary(native_business_database, tmp_path, monkeypatch)


@pytest.mark.anyio
async def test_native_sdk_download_revocation(native_business_database, tmp_path, monkeypatch):
    await verify_sdk_download_revocation(native_business_database, tmp_path, monkeypatch)
