"""Exact-ID lifecycle on a deployment-pinned, dedicated rootless Docker daemon.

Only the isolated supervisor uses this module. It neither attests test outcomes
nor grants Control or the guest access to Docker. Guest packaging is separate.
"""

from __future__ import annotations

import re
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from docker import DockerClient
from docker.errors import NotFound
from docker.types import LogConfig

DAEMON_LABEL = "metahub.assurance=dedicated-v1"
LABEL_KIND = "metahub.assurance.kind"
LABEL_ENVIRONMENT = "metahub.assurance.environment"
LABEL_OWNER = "metahub.assurance.owner"
USER = "10001:10001"
ENTRYPOINT = ["/usr/local/bin/python"]
COMMAND = ["-m", "metahub_assurance_runner.guest"]
MEMORY = 536_870_912
NANO_CPUS = 1_000_000_000
TMPFS = {"/tmp": "rw,noexec,nosuid,nodev,size=268435456,uid=10001,gid=10001,mode=0700"}


class SandboxError(RuntimeError):
    """Sanitized supervisor failure without daemon output or secrets."""


@dataclass(frozen=True, slots=True)
class SandboxDeployment:
    socket_url: str
    daemon_id: str
    image_id: str

    def __post_init__(self):
        if (
            re.fullmatch(
                r"unix:///run/user/[1-9][0-9]*/metahub-assurance/docker.sock", self.socket_url
            )
            is None
            or re.fullmatch(r"[A-Za-z0-9:-]{8,128}", self.daemon_id) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id) is None
        ):
            raise ValueError("invalid dedicated sandbox deployment")


@dataclass(frozen=True, slots=True, repr=False)
class SandboxHandle:
    container_id: str
    environment_id: str
    owner_marker: str
    image_id: str
    created_at: datetime

    @property
    def labels(self):
        return {
            LABEL_KIND: "dedicated-v1",
            LABEL_ENVIRONMENT: self.environment_id,
            LABEL_OWNER: self.owner_marker,
        }


@dataclass(frozen=True, slots=True)
class CleanupObservation:
    environment_id: str | None
    removed: bool
    reason_code: str
    observed_at: datetime


@dataclass(slots=True, repr=False)
class DockerSandbox:
    deployment: SandboxDeployment
    client: DockerClient
    check_lease: Callable[[], bool]
    deadline_at: datetime
    handle: SandboxHandle | None = field(default=None, init=False)
    _attempted: bool = field(default=False, init=False)
    _fenced: bool = field(default=False, init=False)
    _cleanup_result: CleanupObservation | None = field(default=None, init=False)

    def __post_init__(self):
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("sandbox deadline must be timezone aware")

    @classmethod
    def connect(cls, deployment, check_lease, deadline_at):
        if sys.platform != "linux":
            raise SandboxError("sandbox.linux_required")
        try:
            # Explicit socket; never inherit DOCKER_HOST, TLS or context configuration.
            client = DockerClient(base_url=deployment.socket_url, timeout=10, version="1.44")
            return cls(deployment, client, check_lease, deadline_at)
        except Exception:
            raise SandboxError("sandbox.connection_unavailable") from None

    def close(self):
        self.client.close()

    def _guard(self):
        if self._fenced or datetime.now(UTC) >= self.deadline_at:
            self._fenced = True
            raise SandboxError("sandbox.lease_unavailable")
        try:
            allowed = self.check_lease()
        except Exception:
            allowed = False
        if allowed is not True or datetime.now(UTC) >= self.deadline_at:
            self._fenced = True
            raise SandboxError("sandbox.lease_unavailable")

    def _daemon(self):
        info = self.client.info()
        if (
            info.get("OSType") != "linux"
            or info.get("ID") != self.deployment.daemon_id
            or "name=rootless" not in (info.get("SecurityOptions") or [])
            or "name=seccomp,profile=builtin" not in (info.get("SecurityOptions") or [])
            or DAEMON_LABEL not in (info.get("Labels") or [])
            or info.get("CgroupVersion") != "2"
            or any(
                info.get(key) is not True
                for key in (
                    "MemoryLimit",
                    "SwapLimit",
                    "PidsLimit",
                    "CpuCfsPeriod",
                    "CpuCfsQuota",
                )
            )
        ):
            raise SandboxError("sandbox.daemon_boundary")

    def _preflight(self):
        self._daemon()
        self._guard()
        containers = self.client.containers.list(all=True, limit=101)
        if len(containers) > 100:
            raise SandboxError("sandbox.daemon_capacity")
        for container in containers:
            self._guard()
            container.reload()
            labels = container.attrs.get("Config", {}).get("Labels") or {}
            if (
                labels.get(LABEL_KIND) != "dedicated-v1"
                or re.fullmatch(r"aenv_[0-9a-f]{32}", labels.get(LABEL_ENVIRONMENT, "")) is None
                or re.fullmatch(r"[0-9a-f]{64}", labels.get(LABEL_OWNER, "")) is None
            ):
                raise SandboxError("sandbox.daemon_shared")
        self._guard()
        image = self.client.images.get(self.deployment.image_id)
        image.reload()
        if image.id != self.deployment.image_id or image.attrs.get("Config", {}).get("Volumes"):
            raise SandboxError("sandbox.image_boundary")

    def _owned(self, container, handle):
        attrs = container.attrs
        if (
            attrs.get("Id") != handle.container_id
            or attrs.get("Image") != handle.image_id
            or attrs.get("Config", {}).get("Labels") != handle.labels
        ):
            raise SandboxError("sandbox.owner_mismatch")

    def _inspect(self, container, handle):
        container.reload()
        self._owned(container, handle)
        attrs = container.attrs
        config, host = attrs["Config"], attrs["HostConfig"]
        expected = {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "Memory": MEMORY,
            "MemorySwap": MEMORY,
            "NanoCpus": NANO_CPUS,
            "PidsLimit": 128,
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "RestartPolicy": {
                "Name": "no",
                "MaximumRetryCount": 0,
            },
            "LogConfig": {"Type": "none", "Config": {}},
            "Tmpfs": TMPFS,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
        }
        if any(host.get(key) != value for key, value in expected.items()):
            raise SandboxError("sandbox.effective_policy_mismatch")
        if any(
            host.get(key)
            for key in (
                "Binds",
                "Mounts",
                "VolumesFrom",
                "Devices",
                "DeviceRequests",
                "CapAdd",
                "PortBindings",
                "PublishAllPorts",
                "ExtraHosts",
                "Links",
                "GroupAdd",
                "PidMode",
                "UsernsMode",
                "AutoRemove",
                "OomKillDisable",
            )
        ):
            raise SandboxError("sandbox.effective_policy_mismatch")
        if (
            config.get("User") != USER
            or config.get("Entrypoint") != ENTRYPOINT
            or config.get("Cmd") != COMMAND
            or config.get("Volumes")
            or config.get("Healthcheck", {}).get("Test") != ["NONE"]
            or not config.get("NetworkDisabled")
            or config.get("OpenStdin")
            or config.get("Tty")
        ):
            raise SandboxError("sandbox.effective_policy_mismatch")
        for mount in attrs.get("Mounts", []):
            if mount.get("Type") != "tmpfs" or mount.get("Destination") != "/tmp":
                raise SandboxError("sandbox.effective_policy_mismatch")
        networks = attrs.get("NetworkSettings", {}).get("Networks") or {}
        if set(networks) - {"none"}:
            raise SandboxError("sandbox.effective_policy_mismatch")

    def create(self) -> SandboxHandle:
        if self._attempted:
            raise SandboxError("sandbox.already_attempted")
        self._attempted = True
        try:
            self._guard()
            self._preflight()
            self._guard()
            environment_id = "aenv_" + uuid4().hex
            owner_marker = secrets.token_hex(32)
            labels = {
                LABEL_KIND: "dedicated-v1",
                LABEL_ENVIRONMENT: environment_id,
                LABEL_OWNER: owner_marker,
            }
            container = self.client.containers.create(
                image=self.deployment.image_id,
                entrypoint=ENTRYPOINT,
                command=COMMAND,
                name=environment_id,
                labels=labels,
                user=USER,
                network_mode="none",
                network_disabled=True,
                read_only=True,
                privileged=False,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                ipc_mode="private",
                cgroupns="private",
                mem_limit=MEMORY,
                memswap_limit=MEMORY,
                nano_cpus=NANO_CPUS,
                pids_limit=128,
                tmpfs=dict(TMPFS),
                log_config=LogConfig(type="none"),
                restart_policy={"Name": "no", "MaximumRetryCount": 0},
                healthcheck={"test": ["NONE"]},
                use_config_proxy=False,
                auto_remove=False,
                stdin_open=False,
                tty=False,
            )
            if re.fullmatch(r"[0-9a-f]{64}", container.id) is None:
                raise SandboxError("sandbox.unidentified_creation")
            self.handle = SandboxHandle(
                container.id,
                environment_id,
                owner_marker,
                self.deployment.image_id,
                datetime.now(UTC),
            )
            self._guard()
            self._inspect(container, self.handle)
            self._guard()
            return self.handle
        except Exception:
            self._fenced = True
            raise SandboxError("sandbox.creation_failed") from None

    def start(self, handle: SandboxHandle):
        if handle is not self.handle or handle is None:
            raise SandboxError("sandbox.unknown_handle")
        try:
            self._guard()
            self._daemon()
            container = self.client.containers.get(handle.container_id)
            self._inspect(container, handle)
            if container.attrs.get("State", {}).get("Status") != "created":
                raise SandboxError("sandbox.invalid_state")
            self._guard()
            container.start()
            self._guard()
            self._inspect(container, handle)
            self._guard()
            if container.attrs.get("State", {}).get("Running") is not True:
                raise SandboxError("sandbox.not_running")
        except Exception:
            self._fenced = True
            raise SandboxError("sandbox.start_failed") from None

    def cleanup(self, handle: SandboxHandle | None) -> CleanupObservation:
        self._fenced = True
        environment_id = self.handle.environment_id if self.handle else None

        def result(removed, reason):
            observation = CleanupObservation(environment_id, removed, reason, datetime.now(UTC))
            if removed:
                self._cleanup_result = observation
            return observation

        if handle is None or handle is not self.handle:
            return result(False, "cleanup.unidentified")
        if self._cleanup_result is not None:
            return self._cleanup_result
        try:
            # Cleanup remains allowed after revocation; it never resumes guest execution.
            self._daemon()
            try:
                container = self.client.containers.get(handle.container_id)
            except NotFound:
                return result(False, "cleanup.absent_without_observed_delete")
            container.reload()
            self._owned(container, handle)
            container.stop(timeout=5)
            container.reload()
            self._owned(container, handle)
            container.remove(v=False, force=False)
            try:
                self.client.containers.get(handle.container_id)
            except NotFound:
                return result(True, "cleanup.deleted_and_absent")
            return result(False, "cleanup.residue")
        except Exception:
            return result(False, "cleanup.unverified")
