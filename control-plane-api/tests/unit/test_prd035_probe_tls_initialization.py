"""Pinned probes share trust initialization only within one isolated attempt."""

import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from metahub_assurance_runner.guest_runtime import _certificate
from metahub_control.provider_health import service


def _target(hostname):
    return service._ValidatedProbeUrl(
        f"https://{hostname}/v1/models", hostname=hostname, port=443,
        addresses=("192.0.2.10",),
    )


def test_pinned_transport_initializes_trust_once_per_attempt(monkeypatch):
    original_context = ssl.create_default_context
    original_pool = service.httpcore.ConnectionPool
    contexts = []
    pools = []

    def create_context(*args, **kwargs):
        context = original_context(*args, **kwargs)
        contexts.append(context)
        return context

    def create_pool(*args, **kwargs):
        pool = original_pool(*args, **kwargs)
        pools.append((kwargs["ssl_context"], pool))
        return pool

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    monkeypatch.setattr(service.httpcore, "ConnectionPool", create_pool)
    with service._PinnedHTTPTransport(_target("first.example")) as first:
        with service._PinnedHTTPTransport(_target("second.example")) as second:
            assert first._pool is not second._pool
            assert first._pool._network_backend._hostname == "first.example"
            assert second._pool._network_backend._hostname == "second.example"
    assert len(contexts) == 2
    assert len(pools) == 4
    assert pools[0][0] is pools[1][0] is contexts[0]
    assert pools[2][0] is pools[3][0] is contexts[1]
    assert contexts[0] is not contexts[1]
    for context in contexts:
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True


@pytest.fixture
def tls_endpoint(tmp_path):
    certificate, key = _certificate(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield certificate, server.server_port
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize("trusted,hostname", [
    (True, "127.0.0.1"), (True, "wrong.example"), (False, "127.0.0.1"),
])
def test_pinned_transport_preserves_real_tls_verification(tls_endpoint, monkeypatch,
                                                        trusted, hostname):
    certificate, port = tls_endpoint
    if trusted:
        context = ssl.create_default_context(cafile=str(certificate))
        monkeypatch.setattr(service.httpx, "create_ssl_context", lambda **_kwargs: context)
    target = service._ValidatedProbeUrl(
        f"https://{hostname}:{port}/v1/models", hostname=hostname, port=port,
        addresses=("127.0.0.1",),
    )
    with httpx.Client(transport=service._PinnedHTTPTransport(target), timeout=2,
                      follow_redirects=False, trust_env=False) as client:
        if trusted and hostname == "127.0.0.1":
            assert client.get(target).status_code == 204
        else:
            with pytest.raises(httpx.ConnectError):
                client.get(target)
