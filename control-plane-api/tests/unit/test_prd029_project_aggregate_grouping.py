"""Project/Application SQL counts and tenant joins, not authentication evidence."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from metahub_control.projects import service
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.errors import ApiError


@pytest.fixture
def aggregate_database(tmp_path):
    database = tmp_path / "aggregates.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("""
            CREATE TABLE projects(project_id TEXT PRIMARY KEY,tenant_id TEXT,project_code TEXT,
                name TEXT,description TEXT,owner_user_id TEXT,status TEXT,
                created_at TEXT,updated_at TEXT);
            CREATE TABLE users(user_id TEXT PRIMARY KEY,tenant_id TEXT,display_name TEXT);
            CREATE TABLE applications(application_id TEXT PRIMARY KEY,tenant_id TEXT,
                project_id TEXT,application_code TEXT,name TEXT,description TEXT,
                status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE virtual_keys(virtual_key_id TEXT PRIMARY KEY,tenant_id TEXT,
                project_id TEXT,application_id TEXT);
            INSERT INTO users VALUES('owner','tenant','Owner');
            INSERT INTO projects VALUES
                ('project','tenant','main','Main',NULL,'owner','active',
                 '2026-09-08T00:00:00Z','2026-09-08T00:00:00Z'),
                ('empty','tenant','empty','Empty',NULL,NULL,'active',
                 '2026-09-08T00:00:00Z','2026-09-08T00:00:00Z'),
                ('foreign','another','main','Foreign',NULL,NULL,'active',
                 '2026-09-08T00:00:00Z','2026-09-08T00:00:00Z');
            INSERT INTO applications VALUES
                ('app-one','tenant','project','one','One',NULL,'active',
                 '2026-09-08T00:00:00Z','2026-09-08T00:00:00Z'),
                ('app-two','tenant','project','two','Two',NULL,'active',
                 '2026-09-08T00:00:00Z','2026-09-08T00:00:00Z'),
                ('app-foreign','another','project','three','Foreign',NULL,'active',
                 '2026-09-08T00:00:00Z','2026-09-08T00:00:00Z');
            INSERT INTO virtual_keys VALUES
                ('key-one','tenant','project','app-one'),
                ('key-two','tenant','project','app-one'),
                ('key-three','tenant','project','app-two'),
                ('key-foreign','another','project','app-one');
        """)
    return SimpleNamespace(database_url=f"sqlite:///{database.as_posix()}")


def test_project_list_and_detail_keep_owner_and_distinct_counts(aggregate_database):
    config = aggregate_database
    context = SimpleNamespace(user=SimpleNamespace(tenant_id="tenant"))
    rows = service.list_projects(
        config,
        context,
        status="all",
        search=None,
        request_id="test",
        trace_id="test",
    ).projects
    assert {row.project_id for row in rows} == {"project", "empty"}
    with closing(connect_database(config.database_url)) as connection:
        project = service._load_project(connection, "tenant", "project")
        empty = service._load_project(connection, "tenant", "empty")
        assert project in rows and empty in rows
        assert (
            project.owner_display_name,
            project.application_count,
            project.virtual_key_count,
        ) == (
            "Owner",
            2,
            3,
        )
        assert (empty.owner_display_name, empty.application_count, empty.virtual_key_count) == (
            None,
            0,
            0,
        )
        with pytest.raises(ApiError) as rejected:
            service._load_project(connection, "tenant", "foreign")
        assert rejected.value.code == "project.not_found"


def test_application_list_and_detail_keep_project_and_distinct_counts(aggregate_database):
    config = aggregate_database
    context = SimpleNamespace(user=SimpleNamespace(tenant_id="tenant"))
    rows = service.list_applications(
        config,
        context,
        project_id="project",
        status="all",
        search=None,
        request_id="test",
        trace_id="test",
    ).applications
    assert {row.application_id for row in rows} == {"app-one", "app-two"}
    with closing(connect_database(config.database_url)) as connection:
        for application_id, count in (("app-one", 2), ("app-two", 1)):
            application = service._load_application(connection, "tenant", application_id)
            assert application in rows
            assert (application.project_code, application.project_name) == ("main", "Main")
            assert application.virtual_key_count == count
        with pytest.raises(ApiError) as rejected:
            service._load_application(connection, "tenant", "app-foreign")
        assert rejected.value.code == "application.not_found"
