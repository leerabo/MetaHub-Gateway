"""Focused OIDC, SAML, and outbound-network contracts for PRD-114."""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from metahub_control.enterprise_identity import protocols
from metahub_control.enterprise_identity.service import _saml_authorization_url
from metahub_shared.runtime.errors import ApiError

NOW = datetime.now(UTC).replace(microsecond=0)
ISSUER = "https://idp.example.com"
AUDIENCE = "metahub-prd114"
DESTINATION = "https://app.example.com/identity/sso/callback"
REQUEST_ID = "_request_prd114_1234567890"
DS = "http://www.w3.org/2000/09/xmldsig#"
SAML_ASSERTION = "urn:oasis:names:tc:SAML:2.0:assertion"
SAML_PROTOCOL = "urn:oasis:names:tc:SAML:2.0:protocol"


def _api_error(expected_code: str, function, *args, **kwargs) -> None:
    with pytest.raises(ApiError) as captured:
        function(*args, **kwargs)
    assert captured.value.code == expected_code


def _oidc_material() -> tuple[rsa.RSAPrivateKey, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "prd114-key", "alg": "RS256", "use": "sig"})
    return private_key, jwk


def _certificate(private_key: rsa.RSAPrivateKey) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PRD-114 Test IdP")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .sign(private_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _signed_saml_response(
    private_key: rsa.RSAPrivateKey,
    *,
    status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    assertion_id: str = "_assertion_prd114_1234567890",
    confirmation_method: str = "urn:oasis:names:tc:SAML:2.0:cm:bearer",
) -> str:
    nsmap = {"samlp": SAML_PROTOCOL, "saml": SAML_ASSERTION, "ds": DS}
    response = etree.Element(
        f"{{{SAML_PROTOCOL}}}Response",
        nsmap=nsmap,
        ID="_response_prd114_1234567890",
        Destination=DESTINATION,
        InResponseTo=REQUEST_ID,
    )
    status_element = etree.SubElement(response, f"{{{SAML_PROTOCOL}}}Status")
    etree.SubElement(status_element, f"{{{SAML_PROTOCOL}}}StatusCode", Value=status)
    assertion = etree.SubElement(response, f"{{{SAML_ASSERTION}}}Assertion", ID=assertion_id)
    etree.SubElement(assertion, f"{{{SAML_ASSERTION}}}Issuer").text = ISSUER
    subject = etree.SubElement(assertion, f"{{{SAML_ASSERTION}}}Subject")
    etree.SubElement(subject, f"{{{SAML_ASSERTION}}}NameID").text = "subject-prd114"
    confirmation = etree.SubElement(
        subject,
        f"{{{SAML_ASSERTION}}}SubjectConfirmation",
        Method=confirmation_method,
    )
    etree.SubElement(
        confirmation,
        f"{{{SAML_ASSERTION}}}SubjectConfirmationData",
        Recipient=DESTINATION,
        InResponseTo=REQUEST_ID,
        NotOnOrAfter=(NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    )
    conditions = etree.SubElement(
        assertion,
        f"{{{SAML_ASSERTION}}}Conditions",
        NotBefore=(NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        NotOnOrAfter=(NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    )
    restriction = etree.SubElement(conditions, f"{{{SAML_ASSERTION}}}AudienceRestriction")
    etree.SubElement(restriction, f"{{{SAML_ASSERTION}}}Audience").text = AUDIENCE
    statement = etree.SubElement(assertion, f"{{{SAML_ASSERTION}}}AttributeStatement")
    attribute = etree.SubElement(statement, f"{{{SAML_ASSERTION}}}Attribute", Name="email")
    etree.SubElement(attribute, f"{{{SAML_ASSERTION}}}AttributeValue").text = (
        "member@example.com"
    )

    digest = base64.b64encode(
        hashlib.sha256(
            etree.tostring(assertion, method="c14n", exclusive=True, with_comments=False)
        ).digest()
    ).decode("ascii")
    signature = etree.Element(f"{{{DS}}}Signature")
    signed_info = etree.SubElement(signature, f"{{{DS}}}SignedInfo")
    etree.SubElement(
        signed_info,
        f"{{{DS}}}CanonicalizationMethod",
        Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    )
    etree.SubElement(
        signed_info,
        f"{{{DS}}}SignatureMethod",
        Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
    )
    reference = etree.SubElement(signed_info, f"{{{DS}}}Reference", URI=f"#{assertion_id}")
    transforms = etree.SubElement(reference, f"{{{DS}}}Transforms")
    etree.SubElement(
        transforms,
        f"{{{DS}}}Transform",
        Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature",
    )
    etree.SubElement(
        transforms,
        f"{{{DS}}}Transform",
        Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    )
    etree.SubElement(
        reference,
        f"{{{DS}}}DigestMethod",
        Algorithm="http://www.w3.org/2001/04/xmlenc#sha256",
    )
    etree.SubElement(reference, f"{{{DS}}}DigestValue").text = digest
    assertion.insert(1, signature)
    canonical = etree.tostring(
        signed_info, method="c14n", exclusive=True, with_comments=False
    )
    signature_value = private_key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    etree.SubElement(signature, f"{{{DS}}}SignatureValue").text = base64.b64encode(
        signature_value
    ).decode("ascii")
    return base64.b64encode(etree.tostring(response)).decode("ascii")


def test_domain_and_endpoint_normalization_reject_private_shapes() -> None:
    assert protocols.normalize_enterprise_domain("Example.COM.") == "example.com"
    assert (
        protocols.validate_fixed_https_endpoint("https://IDP.Example.COM/oauth/token")
        == "https://idp.example.com/oauth/token"
    )
    for value in ("*.example.com", "localhost", "127.0.0.1", "service.internal"):
        _api_error("sso.domain_invalid", protocols.normalize_enterprise_domain, value)
    for value in (
        "http://idp.example.com/token",
        "https://user@idp.example.com/token",
        "https://127.0.0.1/token",
        "https://service.internal/token",
        "https://com/token",
    ):
        _api_error("sso.connection_invalid", protocols.validate_fixed_https_endpoint, value)


def test_oidc_static_jwks_signature_nonce_and_azp() -> None:
    private_key, jwk = _oidc_material()
    validated_jwks = protocols.validate_oidc_jwks({"keys": [jwk]})
    claims = {
        "iss": ISSUER,
        "sub": "subject-prd114",
        "aud": AUDIENCE,
        "azp": AUDIENCE,
        "exp": NOW + timedelta(minutes=5),
        "iat": NOW,
        "nonce": "nonce-prd114-1234567890",
        "email": "member@example.com",
        "email_verified": True,
        "jti": "assertion-prd114",
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": jwk["kid"]})
    identity = protocols.validate_oidc_id_token(
        token,
        issuer=ISSUER,
        audience=AUDIENCE,
        nonce=claims["nonce"],
        jwks=validated_jwks,
    )
    assert (identity.subject, identity.email, identity.assertion_key) == (
        claims["sub"],
        claims["email"],
        claims["jti"],
    )

    for updates in (
        {"nonce": "different-nonce-1234567890"},
        {"azp": "different-client"},
        {"jti": ["not", "scalar"]},
    ):
        rejected = jwt.encode(
            claims | updates,
            private_key,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )
        _api_error(
            "sso.login_failed",
            protocols.validate_oidc_id_token,
            rejected,
            issuer=ISSUER,
            audience=AUDIENCE,
            nonce=claims["nonce"],
            jwks=validated_jwks,
        )


def test_saml_signature_status_owner_and_replay_identifiers() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate_pem = _certificate(private_key)
    encoded = _signed_saml_response(private_key)
    assert protocols.saml_response_request_id(encoded) == REQUEST_ID
    identity = protocols.validate_saml_response(
        encoded,
        certificate_pem=certificate_pem,
        issuer=ISSUER,
        audience=AUDIENCE,
        destination=DESTINATION,
        request_id=REQUEST_ID,
        now=NOW,
    )
    assert (identity.subject, identity.email) == ("subject-prd114", "member@example.com")

    for rejected in (
        _signed_saml_response(
            private_key, status="urn:oasis:names:tc:SAML:2.0:status:Requester"
        ),
        _signed_saml_response(
            private_key,
            confirmation_method="urn:oasis:names:tc:SAML:2.0:cm:sender-vouches",
        ),
        _signed_saml_response(private_key, assertion_id="_" + "a" * 300),
    ):
        _api_error(
            "sso.login_failed",
            protocols.validate_saml_response,
            rejected,
            certificate_pem=certificate_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
            destination=DESTINATION,
            request_id=REQUEST_ID,
            now=NOW,
        )

    raw = base64.b64decode(encoded)
    root = etree.fromstring(raw)
    assertion = root.find(f"{{{SAML_ASSERTION}}}Assertion")
    assert assertion is not None
    root.set("ID", assertion.get("ID"))
    duplicate_id = base64.b64encode(etree.tostring(root)).decode("ascii")
    _api_error(
        "sso.login_failed",
        protocols.validate_saml_response,
        duplicate_id,
        certificate_pem=certificate_pem,
        issuer=ISSUER,
        audience=AUDIENCE,
        destination=DESTINATION,
        request_id=REQUEST_ID,
        now=NOW,
    )


def test_saml_authn_request_uses_structured_xml_escaping() -> None:
    url = _saml_authorization_url(
        endpoint="https://idp.example.com/saml",
        request_id=REQUEST_ID,
        issuer="MetaHub & <tenant>",
        destination="https://idp.example.com/saml?tenant=a&mode=b",
        callback=DESTINATION,
        state="state-prd114",
        now=NOW,
    )
    query = parse_qs(urlsplit(url).query)
    xml = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), wbits=-15)
    root = etree.fromstring(xml)
    issuer = root.find(f"{{{SAML_ASSERTION}}}Issuer")
    assert issuer is not None and issuer.text == "MetaHub & <tenant>"
    assert root.get("Destination") == "https://idp.example.com/saml?tenant=a&mode=b"
    assert query["RelayState"] == ["state-prd114"]


def test_oidc_exchange_rejects_private_dns_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        protocols.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )

    class UnexpectedConnection:
        def __init__(self, *args, **kwargs):
            raise AssertionError("TLS connection must not open for a private DNS result")

    monkeypatch.setattr(protocols, "_PinnedHttpsConnection", UnexpectedConnection)
    _api_error(
        "sso.login_failed",
        protocols.exchange_oidc_code,
        token_endpoint="https://idp.example.com/token",
        client_id="client-prd114",
        client_secret="synthetic-client-secret",
        redirect_uri=DESTINATION,
        code="code-prd114",
        verifier="verifier-prd114",
    )


def test_oidc_exchange_pins_the_validated_dns_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        protocols.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    class TokenResponse:
        status = 200

        @staticmethod
        def getheader(name: str) -> str | None:
            return None

        @staticmethod
        def read(limit: int) -> bytes:
            assert limit == 65_537
            return json.dumps({"id_token": "t" * 32}).encode("ascii")

    class PinnedConnection:
        def __init__(self, host: str, port: int, address: str) -> None:
            observed.update(host=host, port=port, address=address)

        def request(self, method: str, target: str, **kwargs) -> None:
            observed.update(method=method, target=target)

        @staticmethod
        def getresponse() -> TokenResponse:
            return TokenResponse()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(protocols, "_PinnedHttpsConnection", PinnedConnection)
    token = protocols.exchange_oidc_code(
        token_endpoint="https://idp.example.com:8443/oauth/token?tenant=one",
        client_id="client-prd114",
        client_secret="synthetic-client-secret",
        redirect_uri=DESTINATION,
        code="code-prd114",
        verifier="verifier-prd114",
    )
    assert token == "t" * 32
    assert observed == {
        "host": "idp.example.com",
        "port": 8443,
        "address": "93.184.216.34",
        "method": "POST",
        "target": "/oauth/token?tenant=one",
    }
