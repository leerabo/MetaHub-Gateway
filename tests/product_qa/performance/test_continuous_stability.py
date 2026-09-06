"""Observation arithmetic tests are not real elapsed-time acceptance evidence."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from scripts.quality.product_qa_continuous_stability import Coverage, observe


def _window(*, errors=0):
    return {
        "first_observation_offset_seconds": 0.1,
        "last_observation_offset_seconds": 59.8,
        "wall_seconds": 60.0, "max_observation_gap_seconds": 0.7,
        "requests": 300, "errors": errors,
        "targets": [{"target": "control", "requests": 300, "errors": errors}],
    }


def test_coverage_uses_sample_boundaries_not_wall_duration():
    coverage = Coverage({"control"})
    coverage.add(_window(), started=100, finished=161)
    assert coverage.observed_seconds == pytest.approx(58.698)
    coverage.add(_window(), started=162, finished=223)
    assert coverage.observed_seconds == pytest.approx(120.698)
    assert coverage.maximum_gap == pytest.approx(3.302)
    assert coverage.requests == 600


def test_unobserved_time_is_a_gap_not_success():
    coverage = Coverage({"control"})
    coverage.add(_window(), started=0, finished=60)
    coverage.add(_window(errors=1), started=100, finished=160)
    assert coverage.maximum_gap > 40
    assert coverage.errors == 1


@pytest.mark.parametrize("field,value", [
    ("first_observation_offset_seconds", None),
    ("last_observation_offset_seconds", float("nan")),
    ("wall_seconds", 70), ("requests", 500), ("errors", 1),
    ("targets", []),
])
def test_invalid_sample_evidence_is_rejected(field, value):
    report = _window()
    report[field] = value
    with pytest.raises(ValueError):
        Coverage({"control"}).add(report, started=0, finished=60)


def test_72_hour_mode_rejects_short_duration_without_writing(tmp_path):
    with pytest.raises(ValueError, match="requires_72_hours"):
        observe(raw_targets=["control=http://127.0.0.1:1/health"],
                output_dir=tmp_path / "invalid", revision="a" * 40, duration_seconds=60)
    assert not (tmp_path / "invalid").exists()


def test_failure_is_durable_and_secret_free(tmp_path, monkeypatch):
    from scripts.quality import product_qa_continuous_stability as module

    def interrupted(*args, **kwargs):
        raise RuntimeError("sensitive exception text must not be persisted")

    monkeypatch.setattr(module, "run_loopback_smoke", interrupted)
    with pytest.raises(RuntimeError):
        observe(raw_targets=["control=http://127.0.0.1:1/health"],
                output_dir=tmp_path / "run", revision="a" * 40)
    serialized = (tmp_path / "run/status.json").read_text()
    report = json.loads(serialized)
    assert report["status"] == "failed" and report["reason"] == "RuntimeError"
    assert not report["ga_approval"]
    assert "sensitive exception" not in serialized and "127.0.0.1" not in serialized
    with pytest.raises(FileExistsError):
        observe(raw_targets=["control=http://127.0.0.1:1/health"],
                output_dir=tmp_path / "run", revision="a" * 40)


@pytest.mark.parametrize("status,expected", [(200, "diagnostic_passed"), (503, "failed")])
def test_real_socket_diagnostics_preserve_failure_and_chain(tmp_path, status, expected):
    import hashlib

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler contract
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = tmp_path / "socket"
    try:
        result = observe(
            raw_targets=[f"control=http://127.0.0.1:{server.server_port}/health"],
            output_dir=output, revision="a" * 40, duration_seconds=1, diagnostic=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert result["status"] == expected
    assert result["ga_approval"] is False and result["diagnostic"] is True
    previous = "0" * 64
    for line in (output / "windows.jsonl").read_text().splitlines():
        record = json.loads(line)
        assert record["previous_sha256"] == previous
        previous = hashlib.sha256(line.encode()).hexdigest()
    assert result["journal_sha256"] == previous
    assert json.loads((output / "status.json").read_text()) == result
