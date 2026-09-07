"""Docker SDK contract tests only; no daemon or isolation attestation is involved."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from docker import DockerClient
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from metahub_assurance_runner import sandbox
from metahub_assurance_runner.sandbox import DockerSandbox, SandboxDeployment, SandboxError

IMAGE = "sha256:" + "a" * 64
CONTAINER = "b" * 64


def _attrs(labels):
    return {
        "Id": CONTAINER,
        "Image": IMAGE,
        "State": {"Status": "created", "Running": False},
        "Config": {
            "Labels": labels,
            "User": "10001:10001",
            "Entrypoint": ["/usr/local/bin/python"],
            "Cmd": ["-m", "metahub_assurance_runner.guest"],
            "Volumes": None,
            "Healthcheck": {"Test": ["NONE"]},
            "NetworkDisabled": True,
            "OpenStdin": False,
            "Tty": False,
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "Memory": 536870912,
            "MemorySwap": 536870912,
            "NanoCpus": 1000000000,
            "PidsLimit": 128,
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "LogConfig": {"Type": "none", "Config": {}},
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,nodev,size=268435456,uid=10001,gid=10001,mode=0700"
            },
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {"none": {}}},
    }


@pytest.fixture
def runtime():
    client = Mock(spec=DockerClient)
    client.info.return_value = {
        "OSType": "linux",
        "ID": "dedicated-test-daemon",
        "SecurityOptions": ["name=rootless", "name=seccomp,profile=builtin"],
        "Labels": ["metahub.assurance=dedicated-v1"],
        "CgroupVersion": "2",
        "MemoryLimit": True,
        "SwapLimit": True,
        "PidsLimit": True,
        "CpuCfsPeriod": True,
        "CpuCfsQuota": True,
    }
    client.containers.list.return_value = []
    client.images.get.return_value.id = IMAGE
    client.images.get.return_value.attrs = {"Config": {"Volumes": None}}
    container = Mock(spec=Container)
    container.id = CONTAINER

    def create(**kwargs):
        container.attrs = _attrs(kwargs["labels"])
        return container

    def start():
        container.attrs["State"] = {"Status": "running", "Running": True}

    container.start.side_effect = start
    client.containers.create.side_effect = create
    client.containers.get.return_value = container
    manager = DockerSandbox(
        SandboxDeployment(
            "unix:///run/user/1000/metahub-assurance/docker.sock", "dedicated-test-daemon", IMAGE
        ),
        client,
        lambda: True,
        datetime.now(UTC) + timedelta(minutes=1),
    )
    return manager, client, container


def test_create_start_and_exact_cleanup(runtime):
    manager, client, container = runtime
    handle = manager.create()
    options = client.containers.create.call_args.kwargs
    assert options["image"] == IMAGE and options["use_config_proxy"] is False
    assert options["network_mode"] == "none" and options["privileged"] is False
    assert not {"volumes", "mounts", "environment", "ports", "devices", "cap_add"} & options.keys()
    manager.start(handle)
    client.containers.get.side_effect = [container, NotFound("gone")]
    result = manager.cleanup(handle)
    assert result.removed and result.reason_code == "cleanup.deleted_and_absent"
    assert manager.cleanup(handle) is result
    container.stop.assert_called_once_with(timeout=5)
    container.remove.assert_called_once_with(v=False, force=False)
    assert all(call.args == (CONTAINER,) for call in client.containers.get.call_args_list)
    assert handle.owner_marker not in repr(handle) + repr(manager) + repr(result)
    with pytest.raises(SandboxError):
        manager.start(handle)
    container.start.assert_called_once()


@pytest.mark.parametrize(
    "change",
    [
        {"socket_url": "unix:///var/run/docker.sock"},
        {"socket_url": "tcp://localhost:2375"},
        {"socket_url": "unix:///run/user/0/metahub-assurance/docker.sock"},
        {"socket_url": "unix:///run/user/1000/../docker.sock"},
        {"image_id": "metahub:latest"},
        {"daemon_id": "invalid value"},
    ],
)
def test_deployment_boundary(runtime, change):
    manager, client, _ = runtime
    with pytest.raises(ValueError):
        replace(manager.deployment, **change)
    client.containers.create.assert_not_called()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OSType", "windows"),
        ("ID", "foreign-daemon"),
        ("Labels", []),
        ("SecurityOptions", []),
        ("SecurityOptions", ["name=rootless"]),
        ("CgroupVersion", "1"),
        ("MemoryLimit", False),
        ("SwapLimit", False),
        ("PidsLimit", False),
        ("CpuCfsPeriod", False),
        ("CpuCfsQuota", False),
    ],
)
def test_daemon_identity_and_rootless_must_match(runtime, key, value):
    manager, client, _ = runtime
    client.info.return_value[key] = value
    with pytest.raises(SandboxError, match="creation_failed"):
        manager.create()
    client.containers.create.assert_not_called()


@pytest.mark.parametrize("case", ["shared", "capacity", "image", "volumes"])
def test_preflight_rejects_shared_daemon_or_implicit_volumes(runtime, case):
    manager, client, _ = runtime
    if case in {"shared", "capacity"}:
        foreign = Mock()
        foreign.attrs = {"Config": {"Labels": {}}}
        client.containers.list.return_value = [foreign] * (101 if case == "capacity" else 1)
    elif case == "image":
        client.images.get.return_value.id = "sha256:" + "f" * 64
    else:
        client.images.get.return_value.attrs["Config"]["Volumes"] = {"/data": {}}
    with pytest.raises(SandboxError):
        manager.create()
    client.containers.create.assert_not_called()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NetworkMode", "host"),
        ("ReadonlyRootfs", False),
        ("Privileged", True),
        ("Memory", 0),
        ("MemorySwap", -1),
        ("NanoCpus", 0),
        ("PidsLimit", 0),
        ("IpcMode", "host"),
        ("CgroupnsMode", "host"),
        ("RestartPolicy", {"Name": "always"}),
        ("LogConfig", {"Type": "json-file"}),
        ("Tmpfs", {"/tmp": "rw,exec"}),
        ("CapDrop", []),
        ("SecurityOpt", ["seccomp=unconfined"]),
        ("Binds", ["/host:/guest"]),
        ("Mounts", [{"Type": "bind"}]),
        ("VolumesFrom", ["foreign"]),
        ("Devices", [{"PathOnHost": "/dev/kvm"}]),
        ("DeviceRequests", [{"Count": -1}]),
        ("CapAdd", ["SYS_ADMIN"]),
        ("PortBindings", {"80/tcp": [{"HostPort": "80"}]}),
        ("PublishAllPorts", True),
        ("ExtraHosts", ["external:1.2.3.4"]),
        ("Links", ["foreign"]),
        ("GroupAdd", ["0"]),
        ("PidMode", "host"),
        ("UsernsMode", "host"),
        ("AutoRemove", True),
        ("OomKillDisable", True),
    ],
)
def test_effective_host_policy_is_read_from_inspect(runtime, key, value):
    manager, client, container = runtime
    original = client.containers.create.side_effect

    def create(**kwargs):
        result = original(**kwargs)
        result.attrs["HostConfig"][key] = value
        return result

    client.containers.create.side_effect = create
    with pytest.raises(SandboxError, match="creation_failed"):
        manager.create()
    assert manager.handle is not None
    container.start.assert_not_called()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("User", "0"),
        ("Entrypoint", ["/bin/sh"]),
        ("Cmd", ["malicious"]),
        ("Volumes", {"/data": {}}),
        ("Healthcheck", {"Test": ["CMD", "curl", "outside"]}),
        ("NetworkDisabled", False),
        ("OpenStdin", True),
        ("Tty", True),
    ],
)
def test_modified_config_prevents_start(runtime, key, value):
    manager, _, container = runtime
    handle = manager.create()
    container.attrs["Config"][key] = value
    with pytest.raises(SandboxError, match="start_failed"):
        manager.start(handle)
    container.start.assert_not_called()


@pytest.mark.parametrize("case", ["network", "mount", "label", "image", "id"])
def test_foreign_resources_prevent_start(runtime, case):
    manager, _, container = runtime
    handle = manager.create()
    if case == "network":
        container.attrs["NetworkSettings"]["Networks"]["production"] = {}
    elif case == "mount":
        container.attrs["Mounts"] = [{"Type": "bind", "Destination": "/tmp"}]
    elif case == "label":
        container.attrs["Config"]["Labels"][sandbox.LABEL_OWNER] = "f" * 64
    else:
        container.attrs["Image" if case == "image" else "Id"] = "f" * 64
    with pytest.raises(SandboxError):
        manager.start(handle)
    container.start.assert_not_called()


@pytest.mark.parametrize("case", ["before", "after_create", "before_start"])
def test_revocation_prevents_execution_and_preserves_handle(runtime, case):
    manager, client, container = runtime
    if case == "before":
        manager.check_lease = lambda: False
    elif case == "after_create":
        container.reload.side_effect = lambda: None
        original = client.containers.create.side_effect

        def create(**kwargs):
            result = original(**kwargs)
            manager.check_lease = lambda: False
            return result

        client.containers.create.side_effect = create
    if case == "before_start":
        handle = manager.create()
        manager.check_lease = lambda: False
        with pytest.raises(SandboxError):
            manager.start(handle)
    else:
        with pytest.raises(SandboxError):
            manager.create()
    assert (manager.handle is None) == (case == "before")
    container.start.assert_not_called()
    if manager.handle:
        client.containers.get.side_effect = [container, NotFound("gone")]
        assert manager.cleanup(manager.handle).removed


@pytest.mark.parametrize(
    "case",
    [
        "stop",
        "remove",
        "query",
        "still_present",
        "missing_before",
        "owner_before",
        "owner_after",
        "daemon",
        "unknown",
    ],
)
def test_cleanup_never_assumes_success_or_deletes_foreign_resources(runtime, case):
    manager, client, container = runtime
    handle = manager.create()
    if case in {"stop", "remove"}:
        getattr(container, case).side_effect = APIError("private-daemon-secret")
    elif case == "query":
        client.containers.get.side_effect = [container, APIError("private-daemon-secret")]
    elif case == "missing_before":
        client.containers.get.side_effect = NotFound("gone")
    elif case == "daemon":
        client.info.return_value["ID"] = "foreign-daemon"
    elif case in {"owner_before", "owner_after"}:

        def alter():
            container.attrs["Config"]["Labels"][sandbox.LABEL_OWNER] = "f" * 64

        if case == "owner_before":
            alter()
        else:
            container.stop.side_effect = lambda **_: alter()
    elif case == "unknown":
        handle = replace(handle)
    result = manager.cleanup(handle)
    assert not result.removed
    assert "private-daemon-secret" not in repr(result)
    if case in {"owner_before", "owner_after", "daemon", "unknown", "missing_before", "stop"}:
        container.remove.assert_not_called()


def test_create_response_loss_cannot_be_cleaned_by_prefix(runtime):
    manager, client, _ = runtime
    client.containers.create.side_effect = APIError("private-daemon-secret")
    with pytest.raises(SandboxError, match="creation_failed"):
        manager.create()
    assert not manager.cleanup(None).removed
    client.containers.get.assert_not_called()
    assert client.containers.list.call_count == 1


def test_start_failure_retains_handle_for_cleanup(runtime):
    manager, client, container = runtime
    handle = manager.create()
    container.start.side_effect = APIError("private-daemon-secret")
    with pytest.raises(SandboxError, match="start_failed"):
        manager.start(handle)
    assert manager.handle is handle
    client.containers.get.side_effect = [container, NotFound("gone")]
    assert manager.cleanup(handle).removed


def test_real_sdk_serializes_restricted_creation_without_connecting(runtime, monkeypatch):
    manager, fake, _ = runtime
    client = DockerClient(base_url="tcp://127.0.0.1:9", version="1.44")
    captured = {}

    def create_container_from_config(config, name=None, platform=None):
        captured.update(config)
        return {"Id": CONTAINER}

    monkeypatch.setattr(client.api, "create_container_from_config", create_container_from_config)
    monkeypatch.setattr(client.api, "inspect_container", lambda _: _attrs(captured["Labels"]))
    monkeypatch.setattr(client, "info", fake.info)
    monkeypatch.setattr(client.api, "containers", lambda **_: [])
    monkeypatch.setattr(client.api, "inspect_image", lambda _: {"Id": IMAGE, "Config": {}})
    monkeypatch.setattr(client.api, "request", lambda *_, **__: pytest.fail("unmocked SDK I/O"))
    manager.client = client
    try:
        manager.create()
        expected_host = _attrs({})["HostConfig"]
        expected_host.pop("Privileged")
        assert captured["HostConfig"] == expected_host
        assert captured["Healthcheck"]["Test"] == ["NONE"]
        assert all(value is None for key, value in captured["Healthcheck"].items() if key != "Test")
        assert captured["NetworkDisabled"] is True
        assert not captured["HostConfig"].get("Privileged")
        assert not captured.get("Env")
    finally:
        client.close()


def test_connection_uses_explicit_socket_despite_host_environment(runtime, monkeypatch):
    manager, client, _ = runtime
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setenv("DOCKER_HOST", "tcp://production.invalid:2375")
    factory = Mock(return_value=client)
    monkeypatch.setattr(sandbox, "DockerClient", factory)
    connected = DockerSandbox.connect(manager.deployment, lambda: True, manager.deadline_at)
    factory.assert_called_once_with(
        base_url=manager.deployment.socket_url, timeout=10, version="1.44"
    )
    connected.close()
    client.close.assert_called_once()
