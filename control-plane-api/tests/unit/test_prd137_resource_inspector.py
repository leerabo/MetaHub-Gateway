"""Controlled local-resource faults; real HTTP/TLS evidence is tested separately."""

import secrets
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace

import pytest
from metahub_assurance_runner.resources import (
    LocalResourceInspector,
    ResourceContractViolation,
    ResourceUnavailable,
)
from metahub_shared.files.crypto import FileBlobOwner, encrypt_file_blob, file_content_fingerprint
from metahub_shared.files.storage import EncryptedLocalBlobStore, file_object_key

TENANT = "ten_" + "1" * 32
FOREIGN = "ten_" + "2" * 32
FILE = "file_" + "3" * 32
EXTRACT = "fext_" + "4" * 32
WORKSPACE = "wsp_" + "5" * 32
BODY = b"resource-test-content-not-in-evidence"


@pytest.fixture
def inspector(tmp_path):
    database = tmp_path / "resource.sqlite3"
    secret = secrets.token_urlsafe(48)
    store = EncryptedLocalBlobStore(tmp_path / "file-storage")
    fingerprints = {}
    for kind in ("original", "extract"):
        fingerprint = file_content_fingerprint(
            BODY,
            secret=secret,
            tenant_id=TENANT,
            file_id=FILE,
            blob_kind=kind,
        )
        fingerprints[kind] = fingerprint
        owner = FileBlobOwner(
            tenant_id=TENANT,
            file_id=FILE,
            blob_kind=kind,
            content_fingerprint=fingerprint,
            file_extract_id=EXTRACT if kind == "extract" else None,
            extractor_version="txt-v1" if kind == "extract" else None,
        )
        store.publish(
            store.stage(
                encrypt_file_blob(BODY, secret=secret, owner=owner),
                object_key=file_object_key(FILE, kind),
            )
        )
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("""
            CREATE TABLE uploaded_files(tenant_id,file_id,status,original_object_key,
                                       content_fingerprint);
            CREATE TABLE file_extracts(tenant_id,file_id,file_extract_id,extractor_version,status,
                                      text_fingerprint,extracted_object_key);
            CREATE TABLE workspace_navigation_projection_heads(
                workspace_id,tenant_id,workspace_navigation_projection_id,generation);
            CREATE TABLE workspace_navigation_projection_versions(
                workspace_id,tenant_id,workspace_navigation_projection_id,generation,
                navigation_contract_snapshot_id,contract_generation,status);
            CREATE TABLE workspace_navigation_projection_items(
                workspace_id,tenant_id,workspace_navigation_projection_id,projection_generation,
                source_navigation_id,route_code);
            CREATE TABLE workspace_navigation_projection_sources(
                workspace_id,tenant_id,workspace_navigation_projection_id,projection_generation,
                source_code);
        """)
        connection.execute(
            "INSERT INTO uploaded_files VALUES(?,?,?,?,?)",
            (
                TENANT,
                FILE,
                "ready",
                file_object_key(FILE, "original"),
                fingerprints["original"],
            ),
        )
        connection.execute(
            "INSERT INTO file_extracts VALUES(?,?,?,?,?,?,?)",
            (
                TENANT,
                FILE,
                EXTRACT,
                "txt-v1",
                "ready",
                fingerprints["extract"],
                file_object_key(FILE, "extract"),
            ),
        )
        connection.execute(
            "INSERT INTO workspace_navigation_projection_heads VALUES(?,?,?,1)",
            (
                WORKSPACE,
                TENANT,
                "projection-test",
            ),
        )
        connection.execute(
            """INSERT INTO workspace_navigation_projection_versions
                              VALUES(?,?,?,1,'contract-test',1,'ready')""",
            (
                WORKSPACE,
                TENANT,
                "projection-test",
            ),
        )
        connection.execute(
            """INSERT INTO workspace_navigation_projection_items
                              VALUES(?,?,?,1,'source-test','workspace.files')""",
            (
                WORKSPACE,
                TENANT,
                "projection-test",
            ),
        )
        connection.execute(
            """INSERT INTO workspace_navigation_projection_sources
                              VALUES(?,?,?,1,'catalog')""",
            (WORKSPACE, TENANT, "projection-test"),
        )
        connection.commit()
    return LocalResourceInspector(database, tmp_path / "file-storage", secret)


def _public():
    return {
        "projection_generation": 1,
        "contract_snapshot_id": "contract-test",
        "contract_generation": 1,
        "projection_status": "ready",
        "groups": [{"items": [{"route_code": "workspace.files"}]}],
    }


def test_observer_reads_real_encrypted_bytes_and_returns_only_safe_facts(inspector):
    first = inspector.file(TENANT, FILE, FOREIGN, BODY)
    assert all(satisfied for _, satisfied in first.checks)
    assert first.state_sha256 == inspector.file(TENANT, FILE, FOREIGN, BODY).state_sha256
    assert not list((inspector.storage_root / ".staging").iterdir())
    evidence = repr(asdict(first)) + repr(inspector)
    for value in (BODY.decode(), inspector.file_secret, TENANT, FILE, str(inspector.database)):
        assert value not in evidence
    with closing(inspector._read()) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM uploaded_files")


@pytest.mark.parametrize("fault", ["wrong_key", "kind_swap", "bit_flip", "wrong_owner", "missing"])
def test_missing_or_corrupt_objects_cannot_pass(inspector, fault):
    original = inspector.storage_root / file_object_key(FILE, "original")
    if fault == "wrong_key":
        inspector = replace(inspector, file_secret=secrets.token_urlsafe(48))
    elif fault == "kind_swap":
        original.write_bytes(
            (inspector.storage_root / file_object_key(FILE, "extract")).read_bytes()
        )
    elif fault == "bit_flip":
        content = original.read_bytes()
        original.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    elif fault == "wrong_owner":
        with closing(sqlite3.connect(inspector.database)) as connection:
            connection.execute("UPDATE uploaded_files SET tenant_id=?", (FOREIGN,))
            connection.commit()
    else:
        original.unlink()
    with pytest.raises((ResourceContractViolation, ResourceUnavailable)):
        inspector.file(TENANT, FILE, FOREIGN, BODY)


@pytest.mark.parametrize(
    "fault", ["none", "owner", "version", "items", "public_items", "empty_public"]
)
def test_projection_binds_persisted_owner_version_and_items(inspector, fault):
    public = _public()
    with closing(sqlite3.connect(inspector.database)) as connection:
        if fault == "owner":
            connection.execute(
                "UPDATE workspace_navigation_projection_items SET tenant_id=?", (FOREIGN,)
            )
        elif fault == "version":
            public["projection_generation"] = 2
        elif fault == "items":
            connection.execute("DELETE FROM workspace_navigation_projection_items")
        elif fault == "public_items":
            public["groups"][0]["items"][0]["route_code"] = "foreign.route"
        elif fault == "empty_public":
            public["groups"] = []
        connection.commit()
    result = inspector.projection(WORKSPACE, TENANT, public)
    assert all(satisfied for _, satisfied in result.checks) is (fault == "none")


def test_mismatched_expected_content_is_not_success(inspector):
    result = inspector.file(TENANT, FILE, FOREIGN, b"wrong content")
    assert not dict(result.checks)["original.authenticated_content"]


def test_observer_rejects_outside_storage_and_invalid_subjects(inspector, tmp_path):
    with pytest.raises(ResourceUnavailable):
        replace(inspector, storage_root=tmp_path.parent / "outside")
    with pytest.raises(ResourceUnavailable):
        replace(inspector, file_secret="short")
    with pytest.raises(ResourceContractViolation):
        inspector.file(TENANT, FILE, TENANT, BODY)
    with pytest.raises(ResourceContractViolation):
        inspector.projection("../workspace", TENANT, _public())
