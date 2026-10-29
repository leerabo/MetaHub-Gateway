"""Real native routing/evaluation HTTP checks, not full product release signoff."""

from contextlib import aclosing

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd147_workspace_routing_evaluation as evaluation
from test_prd147_workspace_files import workspace_http_subjects
from test_prd147_workspace_resource_writes import resource_subjects  # noqa: F401

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database
evaluation_subjects = evaluation.evaluation_subjects
evaluation_graph = evaluation.evaluation_graph


@pytest.fixture
async def file_subjects(native_business_database, tmp_path, monkeypatch):
    async with aclosing(
        workspace_http_subjects(native_business_database, tmp_path, monkeypatch)
    ) as subjects:
        async for app, fixtures in subjects:
            yield app, native_business_database, fixtures


@pytest.mark.anyio
async def test_native_evaluation_mutations_results_and_cross_workspace(evaluation_graph):
    await evaluation.test_workspace_evaluation_mutations_and_replay(evaluation_graph)


@pytest.mark.anyio
async def test_native_evaluation_terminal_expiry(evaluation_graph, monkeypatch, record_property):
    await evaluation.test_evaluation_terminal_expiry_rolls_back_and_closes(
        evaluation_graph,
        monkeypatch,
        record_property,
    )
