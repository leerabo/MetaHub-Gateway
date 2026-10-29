"""Real loopback SMTP/TLS acceptance; never connects to an external mail server.

The private test CA, credentials and messages exist only for this fixture. This
proves the transport, not deliverability through a customer's SMTP provider.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socketserver
import ssl
import sys
import tempfile
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
for directory in ("shared-sdk/python", "control-plane-api/src"):
    sys.path.insert(0, str(ROOT / directory))


class LocalSmtp:
    def __init__(self, directory: Path, mode="starttls", *, ip_sans=(), on_message=None):
        self.mode = mode
        self.username = "local-acceptance"
        self.password = secrets.token_urlsafe(36)
        self.messages = []
        self.authenticated = 0
        self.rejected_tls = 0
        self.tls_connections = 0
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MetaHub isolated SMTP acceptance")])
        now = datetime.now(UTC)
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .sign(key, hashes.SHA256()))
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
                .issuer_name(name).public_key(server_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                    *(x509.IPAddress(ipaddress.ip_address(value)) for value in ip_sans)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        self.ca_path = directory / "ca.pem"
        self.ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cert_path, key_path = directory / "server.pem", directory / "server.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(cert_path, key_path)
        fixture = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                channel = self.request
                channel.settimeout(15)
                secured = False
                authorized = False

                def line():
                    result = bytearray()
                    while not result.endswith(b"\n") and len(result) < 16384:
                        value = channel.recv(1)
                        if not value:
                            raise EOFError()
                        result.extend(value)
                    if len(result) >= 16384:
                        raise ValueError("Fixture line limit")
                    return bytes(result)

                def reply(value):
                    channel.sendall(value)

                try:
                    if fixture.mode == "tls":
                        channel = fixture.context.wrap_socket(channel, server_side=True)
                        secured = True
                        fixture.tls_connections += 1
                    reply(b"220 localhost acceptance SMTP\r\n")
                    while True:
                        command = line().strip()
                        verb = command.split(b" ", 1)[0].upper()
                        if verb in (b"EHLO", b"HELO"):
                            reply(b"250-localhost\r\n" + (b"250-AUTH PLAIN\r\n" if secured else b"250-STARTTLS\r\n") + b"250 SIZE 65536\r\n")
                        elif verb == b"STARTTLS" and not secured:
                            reply(b"220 Ready for TLS\r\n")
                            channel = fixture.context.wrap_socket(channel, server_side=True)
                            secured = True
                            fixture.tls_connections += 1
                        elif verb == b"AUTH" and secured:
                            parts = command.split()
                            credentials = base64.b64decode(parts[2], validate=True).split(b"\0")
                            authorized = (len(credentials) == 3 and
                                          hmac.compare_digest(credentials[1], fixture.username.encode()) and
                                          hmac.compare_digest(credentials[2], fixture.password.encode()))
                            fixture.authenticated += int(authorized)
                            reply(b"235 Authenticated\r\n" if authorized else b"535 Authentication failed\r\n")
                        elif verb in (b"MAIL", b"RCPT") and authorized:
                            reply(b"250 Accepted\r\n")
                        elif verb == b"DATA" and authorized:
                            reply(b"354 End with dot\r\n")
                            body = bytearray()
                            while True:
                                value = line()
                                if value == b".\r\n":
                                    break
                                body.extend(value[1:] if value.startswith(b"..") else value)
                                if len(body) > 65536:
                                    raise ValueError("Fixture message limit")
                            fixture.messages.append(bytes(body))
                            if on_message is not None:
                                on_message(bytes(body))
                            reply(b"250 Stored in local memory\r\n")
                        elif verb == b"QUIT":
                            reply(b"221 Goodbye\r\n")
                            return
                        else:
                            reply(b"530 TLS and authentication required\r\n")
                except ssl.SSLError:
                    fixture.rejected_tls += 1
                except (EOFError, ConnectionError, TimeoutError):
                    pass
                finally:
                    channel.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @contextmanager
    def trusted_ca(self):
        previous = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(self.ca_path)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = previous

    def settings(self):
        return dict(smtp_host="localhost", smtp_port=self.port, smtp_security=self.mode,
                    smtp_username=self.username, smtp_password=self.password,
                    smtp_sender="security@example.invalid", smtp_timeout_seconds=10,
                    identity_challenge_delivery_mode="smtp", environment="prod")


def main():
    from email import policy
    from email.parser import BytesParser
    from types import SimpleNamespace
    from metahub_control.identity.delivery import IdentityChallengeDeliveryError, identity_challenge_delivery
    observations = []
    for mode in ("starttls", "tls"):
        with tempfile.TemporaryDirectory(prefix="smtp-tls-", dir=ROOT / ".tmp") as temporary:
            with LocalSmtp(Path(temporary), mode) as fixture:
                delivery = identity_challenge_delivery(SimpleNamespace(**fixture.settings()))
                code = f"{secrets.randbelow(1000000):06d}"
                fields = dict(target="recipient@example.invalid", code=code, purpose="registration",
                              intent_id="private-intent", challenge_id="private-challenge", generation=1,
                              expires_at=datetime.now(UTC) + timedelta(minutes=10))
                try:
                    delivery.deliver(**fields)
                except IdentityChallengeDeliveryError as error:
                    assert str(error) == "identity challenge delivery failed"
                else:
                    raise AssertionError("Untrusted certificate was accepted")
                assert fixture.authenticated == 0 and not fixture.messages
                with fixture.trusted_ca():
                    delivery.deliver(**fields)
                assert fixture.authenticated == 1 and len(fixture.messages) == 1
                message = BytesParser(policy=policy.default).parsebytes(fixture.messages[0])
                assert message["To"] == fields["target"]
                assert code in message.get_content()
                assert "private-intent" not in message.get_content()
                observations.append(dict(mode=mode, untrusted_ca_rejected_before_credentials=True,
                                         trusted_tls_authenticated_delivery=True, message_content_verified=True))
    result = dict(status="passed", scope="Real loopback SMTP, production adapter; no external delivery",
                  observations=observations, public_network_used=False, fixed_verification_code=False)
    target = ROOT / "output/deployment-ubuntu-20261014/smtp-tls-validation.json"
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
