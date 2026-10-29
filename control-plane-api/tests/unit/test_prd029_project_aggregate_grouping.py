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
            CREATE TABLE users(user_id TEXT PRIMARY KEY,tenant_id TEXT,display_name TEXT,
                status TEXT DEFAULT 'active');
            CREATE TABLE applications(application_id TEXT PRIMARY KEY,tenant_id TEXT,
                project_id TEXT,application_code TEXT,name TEXT,description TEXT,
                status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE virtual_keys(virtual_key_id TEXT PRIMARY KEY,tenant_id TEXT,
                project_id TEXT,application_id TEXT);
            CREATE TABLE workspace_memberships(workspace_membership_id TEXT,tenant_id TEXT,
                account_id TEXT,role_code TEXT,status TEXT);
            CREATE TABLE global_accounts(account_id TEXT,display_name TEXT,email_normalized TEXT);
            CREATE TABLE application_memberships(application_membership_id TEXT,tenant_id TEXT,
                application_id TEXT,workspace_membership_id TEXT,status TEXT);
            INSERT INTO users(user_id,tenant_id,display_name) VALUES('owner','tenant','Owner');
            INSERT INTO users VALUES('inactive','tenant','Inactive','disabled'),
                ('other-owner','another','Foreign','active');
            INSERT INTO workspace_memberships VALUES
                ('member-one','tenant','account-one','developer','active'),
                ('member-two','tenant','account-two','developer','active'),
                ('member-inactive','tenant','account-inactive','developer','disabled'),
                ('member-service','tenant','account-service','service_account','active'),
                ('member-foreign','another','account-foreign','developer','active');
            INSERT INTO global_accounts VALUES
                ('account-one','One','one@example.com'),
                ('account-two','Two','two@example.com');
            INSERT INTO application_memberships VALUES
                ('am-one','tenant','app-one','member-one','active'),
                ('am-two','tenant','app-one','member-two','active'),
                ('am-inactive','tenant','app-one','member-inactive','active'),
                ('am-service','tenant','app-one','member-service','active'),
                ('am-foreign','another','app-one','member-foreign','active'),
                ('am-disabled','tenant','app-two','member-one','disabled');
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
            assert application.member_count == (2 if application_id == "app-one" else 0)
        with pytest.raises(ApiError) as rejected:
            service._load_application(connection, "tenant", "app-foreign")
        assert rejected.value.code == "application.not_found"


def test_owner_candidates_are_active_scoped_and_searchable(aggregate_database):
    context = SimpleNamespace(user=SimpleNamespace(tenant_id="tenant"))
    result = service.list_project_owner_candidates(
        aggregate_database, context, search=None, limit=50, request_id="test", trace_id="test",
    )
    assert result.total == 1
    assert [(row.user_id, row.display_name) for row in result.candidates] == [("owner", "Owner")]
    result = service.list_project_owner_candidates(
        aggregate_database, context, search="foreign", limit=50, request_id="test", trace_id="test",
    )
    assert result.total == 0


def test_member_roster_selected_only_matches_count_across_search_and_pages(aggregate_database):
    context = SimpleNamespace(user=SimpleNamespace(tenant_id="tenant"))
    result = service.list_application_members(
        aggregate_database, context, "app-one", search="Two", selected_only=True,
        limit=1, offset=0, request_id="test", trace_id="test",
    )
    assert result.total == 1
    assert [row.display_name for row in result.members] == ["Two"]
    assert result.selected_membership_ids == ["member-one", "member-two"]
    empty = service.list_application_members(
        aggregate_database, context, "app-two", search=None, selected_only=True,
        limit=1, offset=0, request_id="test", trace_id="test",
    )
    assert empty.total == 0
    assert empty.selected_membership_ids == []
