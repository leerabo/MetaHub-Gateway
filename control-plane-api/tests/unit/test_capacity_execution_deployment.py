"""Static deployment contract; this is not a Docker runtime acceptance test."""

from pathlib import Path

import yaml


def test_execution_overlay_preserves_internal_network_and_owns_only_runner_state():
    root = Path(__file__).resolve().parents[3]
    base = yaml.safe_load((root / "infra/compose/docker-compose.yml").read_text(encoding="utf-8"))
    overlay = yaml.safe_load(
        (root / "infra/compose/docker-compose.capacity-execution.yml").read_text(encoding="utf-8")
    )
    runner = overlay["services"]["capacity-runner"]
    assert set(overlay["services"]) == {"capacity-runner"}
    assert base["services"]["capacity-runner"]["read_only"] is True
    assert "ports" not in runner
    assert "ports" not in base["services"]["capacity-runner"]
    assert base["networks"]["capacity-runner-control"]["internal"] is True
    assert overlay["networks"]["capacity-test-targets"]["internal"] is True
    assert runner["volumes"][0] == "capacity-execution-state:/var/lib/metahub-capacity"
    credentials = runner["volumes"][1]
    assert credentials["type"] == "bind"
    assert credentials["read_only"] is True
    assert credentials["bind"]["create_host_path"] is False
    assert credentials["source"].startswith("${METAHUB_CAPACITY_CONFIGURATION_DIRECTORY:?")
    assert runner["environment"]["METAHUB_CAPACITY_RUNNER_SETTINGS_FILE"] == (
        credentials["target"] + "/settings.json"
    )
    serialized = str(runner).lower()
    for forbidden in (
        "docker.sock",
        "pid: host",
        "privileged",
        "metahub-sqlite",
        "metahub-postgres",
    ):
        assert forbidden not in serialized
    assert "/ready" in runner["healthcheck"]["test"][-1]


def test_docker_image_initializes_state_volume_for_unprivileged_owner():
    root = Path(__file__).resolve().parents[3]
    dockerfile = (root / "infra/docker/capacity-runner.Dockerfile").read_text(encoding="utf-8")
    assert "mkdir -p /var/lib/metahub-capacity" in dockerfile
    assert "chown 65532:65532 /var/lib/metahub-capacity" in dockerfile
    assert "USER 65532:65532" in dockerfile
