from __future__ import annotations

import pytest
from metahub_control.launch_governance import service


class _Response:
    def raise_for_status(self) -> None:
        return None


class _Client:
    calls: list[tuple[str, dict[str, object], dict[str, str]]] = []
    should_fail = False

    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, *, json: dict[str, object], headers: dict[str, str]) -> _Response:
        self.calls.append((url, json, headers))
        if self.should_fail:
            raise RuntimeError("sink unavailable")
        return _Response()


def _event() -> dict[str, str]:
    return {
        "ga_event_id": "gev_" + "a" * 32,
        "event_type": "ga.launched",
        "payload_sha256": "b" * 64,
        "occurred_at": "2026-09-12T00:00:00.000Z",
    }


def test_ga_event_sink_posts_minimal_signed_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    _Client.calls = []
    monkeypatch.setattr(service.httpx, "Client", _Client)
    config = type("Config", (), {
        "ga_event_sink_url": "https://deploy.example.test/ga-events",
        "ga_event_sink_token": "sink-token",
        "ga_event_sink_timeout_seconds": 3.0,
    })()

    service._deliver_ga_event(config, _event())

    assert _Client.calls == [
        (
            "https://deploy.example.test/ga-events",
            {
                "event_id": "gev_" + "a" * 32,
                "event_type": "ga.launched",
                "payload_sha256": "b" * 64,
                "occurred_at": "2026-09-12T00:00:00.000Z",
            },
            {"content-type": "application/json", "authorization": "Bearer sink-token"},
        )
    ]


def test_ga_event_sink_is_disabled_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _Client.calls = []
    monkeypatch.setattr(service.httpx, "Client", _Client)
    config = type("Config", (), {"ga_event_sink_url": None})()

    service._deliver_ga_event(config, _event())

    assert _Client.calls == []


def test_ga_event_sink_rejects_invalid_endpoint() -> None:
    config = type("Config", (), {"ga_event_sink_url": "file:///tmp/events"})()

    with pytest.raises(ValueError, match="event_sink_url_invalid"):
        service._deliver_ga_event(config, _event())
