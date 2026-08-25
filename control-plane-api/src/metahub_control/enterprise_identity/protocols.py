"""Bounded OIDC/SAML validation and fixed-endpoint network policy for PRD-114."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree
from metahub_shared.runtime.errors import ApiError

_DS = "http://www.w3.org/2000/09/xmldsig#"
_SAML_ASSERTION = "urn:oasis:names:tc:SAML:2.0:assertion"
_SAML_PROTOCOL = "urn:oasis:names:tc:SAML:2.0:protocol"
_EXCLUSIVE_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
_ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
_SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
_EMAIL_NAMES = frozenset(
    {
        "email",
        "mail",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "urn:oid:0.9.2342.19200300.100.1.3",
    }
)
_BLOCKED_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".home",
    ".lan",
    ".corp",
)
_TOKEN_RESPONSE_LIMIT = 65_536


@dataclass(frozen=True, slots=True)
class CertificateMaterial:
    fingerprint_sha256: str
    public_pem: str
    not_before: datetime
    not_after: datetime


@dataclass(frozen=True, slots=True)
class ProtocolIdentity:
    subject: str
    email: str
    assertion_key: str
    expires_at: datetime
    nonce: str | None = None


def normalize_enterprise_domain(value: object) -> str:
    if type(value) is not str or value != value.strip() or value.startswith("*."):
        raise ApiError("sso.domain_invalid")
    try:
        normalized = value.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise ApiError("sso.domain_invalid") from None
    if (
        not 3 <= len(normalized) <= 253
        or normalized.count(".") < 1
        or any(normalized.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)
    ):
        raise ApiError("sso.domain_invalid")
    try:
        ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        pass
    else:
        raise ApiError("sso.domain_invalid") from None
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label)
        for label in labels
    ):
        raise ApiError("sso.domain_invalid")
    return normalized


def validate_fixed_https_endpoint(value: object) -> str:
    if type(value) is not str or value != value.strip():
        raise ApiError("sso.connection_invalid")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or "." not in host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)
    ):
        raise ApiError("sso.connection_invalid")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise ApiError("sso.connection_invalid") from None
    try:
        port = parsed.port
    except ValueError:
        raise ApiError("sso.connection_invalid") from None
    if port is not None and not 1 <= port <= 65535:
        raise ApiError("sso.connection_invalid")
    path = parsed.path or "/"
    return urlunsplit(("https", parsed.netloc.lower(), path, parsed.query, ""))


def validate_oidc_jwks(value: object) -> tuple[dict[str, Any], ...]:
    if type(value) is not dict or set(value) != {"keys"} or type(value.get("keys")) is not list:
        raise ApiError("sso.connection_invalid")
    keys = value["keys"]
    if not 1 <= len(keys) <= 10:
        raise ApiError("sso.connection_invalid")
    result: list[dict[str, Any]] = []
    kids: set[str] = set()
    for item in keys:
        if type(item) is not dict or not set(item) <= {
            "kty",
            "kid",
            "use",
            "alg",
            "key_ops",
            "n",
            "e",
            "crv",
            "x",
            "y",
        }:
            raise ApiError("sso.connection_invalid")
        kid = item.get("kid")
        alg = item.get("alg")
        if (
            type(kid) is not str
            or not 1 <= len(kid) <= 128
            or kid in kids
            or alg not in {"RS256", "ES256"}
            or item.get("use", "sig") != "sig"
            or item.get("key_ops", ["verify"]) != ["verify"]
            or item.get("kty") not in {"RSA", "EC"}
        ):
            raise ApiError("sso.connection_invalid")
        try:
            jwt.PyJWK.from_dict(item)
        except (jwt.PyJWKError, ValueError, TypeError):
            raise ApiError("sso.connection_invalid") from None
        kids.add(kid)
        result.append(dict(item))
    return tuple(result)


def parse_saml_certificate(value: object) -> CertificateMaterial:
    if type(value) is not str or not 64 <= len(value) <= 32768:
        raise ApiError("sso.connection_invalid")
    try:
        certificate = x509.load_pem_x509_certificate(value.encode("ascii"))
        public_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    except (ValueError, UnicodeError, TypeError):
        raise ApiError("sso.connection_invalid") from None
    return CertificateMaterial(
        fingerprint_sha256=certificate.fingerprint(hashes.SHA256()).hex(),
        public_pem=public_pem,
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
    )


def oidc_authorization_url(
    *,
    endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return endpoint + ("&" if "?" in endpoint else "?") + query


def exchange_oidc_code(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    verifier: str,
) -> str:
    try:
        status_code, content = _post_oidc_token(
            token_endpoint,
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": verifier,
            },
        )
        if status_code != 200:
            raise ApiError("sso.login_failed")
        payload = json.loads(content)
        if type(payload) is not dict or set(payload) - {
            "access_token",
            "token_type",
            "expires_in",
            "id_token",
            "scope",
            "refresh_token",
        }:
            raise ApiError("sso.login_failed")
        id_token = payload.get("id_token")
        if type(id_token) is not str or not 32 <= len(id_token) <= 32768:
            raise ApiError("sso.login_failed")
        return id_token
    except ApiError:
        raise
    except (
        http.client.HTTPException,
        json.JSONDecodeError,
        OSError,
        ssl.SSLError,
        UnicodeError,
        ValueError,
        TypeError,
    ):
        raise ApiError("sso.login_failed") from None


def validate_oidc_id_token(
    value: str,
    *,
    issuer: str,
    audience: str,
    nonce: str | None,
    jwks: tuple[dict[str, Any], ...],
) -> ProtocolIdentity:
    try:
        header = jwt.get_unverified_header(value)
        if set(header) - {"alg", "kid", "typ"}:
            raise ApiError("sso.login_failed")
        kid = header.get("kid")
        alg = header.get("alg")
        matches = [item for item in jwks if item.get("kid") == kid and item.get("alg") == alg]
        if len(matches) != 1 or alg not in {"RS256", "ES256"}:
            raise ApiError("sso.login_failed")
        key = jwt.PyJWK.from_dict(matches[0]).key
        claims = jwt.decode(
            value,
            key=key,
            algorithms=[alg],
            audience=audience,
            issuer=issuer,
            leeway=60,
            options={
                "require": ["iss", "sub", "aud", "exp", "iat", "nonce", "email", "email_verified"]
            },
        )
        subject = claims.get("sub")
        email = claims.get("email")
        if (
            type(subject) is not str
            or not 1 <= len(subject) <= 512
            or type(email) is not str
            or not 3 <= len(email) <= 320
            or claims.get("email_verified") is not True
            or type(claims.get("nonce")) is not str
            or not 16 <= len(claims["nonce"]) <= 256
            or (nonce is not None and claims.get("nonce") != nonce)
        ):
            raise ApiError("sso.login_failed")
        audience_claim = claims.get("aud")
        azp_claim = claims.get("azp")
        assertion_claim = claims.get("jti")
        if (
            (azp_claim is not None and (type(azp_claim) is not str or azp_claim != audience))
            or (isinstance(audience_claim, list) and len(audience_claim) > 1 and azp_claim is None)
            or (
                assertion_claim is not None
                and (type(assertion_claim) is not str or not 1 <= len(assertion_claim) <= 256)
            )
        ):
            raise ApiError("sso.login_failed")
        expires_at = datetime.fromtimestamp(int(claims["exp"]), UTC)
        assertion_key = assertion_claim or hashlib.sha256(value.encode("ascii")).hexdigest()
        return ProtocolIdentity(subject, email, assertion_key, expires_at, claims["nonce"])
    except ApiError:
        raise
    except (jwt.PyJWTError, UnicodeError, ValueError, TypeError, KeyError):
        raise ApiError("sso.login_failed") from None


def validate_saml_response(
    value: str,
    *,
    certificate_pem: str,
    issuer: str,
    audience: str,
    destination: str,
    request_id: str,
    now: datetime,
) -> ProtocolIdentity:
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeError, ValueError):
        raise ApiError("sso.login_failed") from None
    if not raw or len(raw) > 131_072 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ApiError("sso.login_failed")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        recover=False,
        huge_tree=False,
        remove_comments=False,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        raise ApiError("sso.login_failed") from None
    if root.tag != f"{{{_SAML_PROTOCOL}}}Response" or root.get("Destination") != destination:
        raise ApiError("sso.login_failed")
    if root.get("InResponseTo") != request_id:
        raise ApiError("sso.login_failed")
    statuses = root.findall(f"{{{_SAML_PROTOCOL}}}Status")
    if len(statuses) != 1 or len(statuses[0]) != 1:
        raise ApiError("sso.login_failed")
    status_code = statuses[0][0]
    if (
        status_code.tag != f"{{{_SAML_PROTOCOL}}}StatusCode"
        or status_code.attrib
        != {"Value": "urn:oasis:names:tc:SAML:2.0:status:Success"}
        or len(status_code) != 0
    ):
        raise ApiError("sso.login_failed")
    assertions = root.xpath("./saml:Assertion", namespaces={"saml": _SAML_ASSERTION})
    if len(assertions) != 1:
        raise ApiError("sso.login_failed")
    assertion = assertions[0]
    response_id = root.get("ID")
    assertion_id = assertion.get("ID")
    if (
        type(response_id) is not str
        or not 8 <= len(response_id) <= 256
        or type(assertion_id) is not str
        or not 8 <= len(assertion_id) <= 256
    ):
        raise ApiError("sso.login_failed")
    ids = [item.get("ID") for item in root.iter() if item.get("ID") is not None]
    if not ids or len(ids) != len(set(ids)) or assertion.get("ID") not in ids:
        raise ApiError("sso.login_failed")
    _verify_saml_signature(assertion, certificate_pem)
    issuer_nodes = assertion.xpath("./saml:Issuer", namespaces={"saml": _SAML_ASSERTION})
    if len(issuer_nodes) != 1 or (issuer_nodes[0].text or "").strip() != issuer:
        raise ApiError("sso.login_failed")
    conditions = assertion.xpath("./saml:Conditions", namespaces={"saml": _SAML_ASSERTION})
    if len(conditions) != 1:
        raise ApiError("sso.login_failed")
    not_before = _saml_time(conditions[0].get("NotBefore"))
    not_after = _saml_time(conditions[0].get("NotOnOrAfter"))
    skew = timedelta(seconds=120)
    if now + skew < not_before or now - skew >= not_after or not_after - now > timedelta(hours=1):
        raise ApiError("sso.login_failed")
    audiences = assertion.xpath(
        "./saml:Conditions/saml:AudienceRestriction/saml:Audience/text()",
        namespaces={"saml": _SAML_ASSERTION},
    )
    if audiences != [audience]:
        raise ApiError("sso.login_failed")
    confirmations = assertion.xpath(
        "./saml:Subject/saml:SubjectConfirmation",
        namespaces={"saml": _SAML_ASSERTION},
    )
    if (
        len(confirmations) != 1
        or confirmations[0].get("Method")
        != "urn:oasis:names:tc:SAML:2.0:cm:bearer"
    ):
        raise ApiError("sso.login_failed")
    confirmation_data = confirmations[0].findall(
        f"{{{_SAML_ASSERTION}}}SubjectConfirmationData"
    )
    if (
        len(confirmation_data) != 1
        or confirmation_data[0].get("Recipient") != destination
        or confirmation_data[0].get("InResponseTo") != request_id
        or now - skew >= _saml_time(confirmation_data[0].get("NotOnOrAfter"))
    ):
        raise ApiError("sso.login_failed")
    names = assertion.xpath(
        "./saml:Subject/saml:NameID/text()", namespaces={"saml": _SAML_ASSERTION}
    )
    if len(names) != 1 or not names[0].strip() or len(names[0]) > 512:
        raise ApiError("sso.login_failed")
    emails: list[str] = []
    for attribute in assertion.xpath(
        "./saml:AttributeStatement/saml:Attribute", namespaces={"saml": _SAML_ASSERTION}
    ):
        if (attribute.get("Name") or "").lower() in _EMAIL_NAMES:
            values = attribute.xpath(
                "./saml:AttributeValue/text()", namespaces={"saml": _SAML_ASSERTION}
            )
            emails.extend(value.strip() for value in values if value.strip())
    if len(emails) != 1 or not 3 <= len(emails[0]) <= 320:
        raise ApiError("sso.login_failed")
    return ProtocolIdentity(names[0].strip(), emails[0], assertion.get("ID"), not_after)


def saml_response_request_id(value: str) -> str:
    """Read the bounded outer request correlation before full signed-assertion validation."""

    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeError, ValueError):
        raise ApiError("sso.login_failed") from None
    if not raw or len(raw) > 131_072 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ApiError("sso.login_failed")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        raise ApiError("sso.login_failed") from None
    request_id = root.get("InResponseTo")
    if (
        root.tag != f"{{{_SAML_PROTOCOL}}}Response"
        or type(request_id) is not str
        or not 8 <= len(request_id) <= 256
    ):
        raise ApiError("sso.login_failed")
    return request_id


def _verify_saml_signature(assertion: etree._Element, certificate_pem: str) -> None:
    signatures = assertion.xpath("./ds:Signature", namespaces={"ds": _DS})
    if len(signatures) != 1:
        raise ApiError("sso.login_failed")
    signature = signatures[0]
    signed_info = signature.find(f"{{{_DS}}}SignedInfo")
    signature_value = signature.findtext(f"{{{_DS}}}SignatureValue")
    if signed_info is None or signature_value is None:
        raise ApiError("sso.login_failed")
    canonicalization = signed_info.find(f"{{{_DS}}}CanonicalizationMethod")
    signature_method = signed_info.find(f"{{{_DS}}}SignatureMethod")
    references = signed_info.findall(f"{{{_DS}}}Reference")
    if (
        canonicalization is None
        or canonicalization.get("Algorithm") != _EXCLUSIVE_C14N
        or signature_method is None
        or signature_method.get("Algorithm") != _RSA_SHA256
        or len(references) != 1
        or references[0].get("URI") != f"#{assertion.get('ID')}"
    ):
        raise ApiError("sso.login_failed")
    transforms = references[0].findall(f"{{{_DS}}}Transforms/{{{_DS}}}Transform")
    if [item.get("Algorithm") for item in transforms] != [_ENVELOPED, _EXCLUSIVE_C14N]:
        raise ApiError("sso.login_failed")
    digest_method = references[0].find(f"{{{_DS}}}DigestMethod")
    digest_value = references[0].findtext(f"{{{_DS}}}DigestValue")
    if digest_method is None or digest_method.get("Algorithm") != _SHA256 or digest_value is None:
        raise ApiError("sso.login_failed")
    unsigned = copy.deepcopy(assertion)
    copied_signatures = unsigned.xpath("./ds:Signature", namespaces={"ds": _DS})
    if len(copied_signatures) != 1:
        raise ApiError("sso.login_failed")
    unsigned.remove(copied_signatures[0])
    canonical_assertion = etree.tostring(
        unsigned, method="c14n", exclusive=True, with_comments=False
    )
    expected_digest = base64.b64encode(hashlib.sha256(canonical_assertion).digest()).decode("ascii")
    if not __import__("hmac").compare_digest(expected_digest, "".join(digest_value.split())):
        raise ApiError("sso.login_failed")
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        signature_bytes = base64.b64decode("".join(signature_value.split()), validate=True)
        canonical_signed_info = etree.tostring(
            signed_info, method="c14n", exclusive=True, with_comments=False
        )
        certificate.public_key().verify(
            signature_bytes,
            canonical_signed_info,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception:
        raise ApiError("sso.login_failed") from None


def _saml_time(value: str | None) -> datetime:
    if value is None or len(value) > 40:
        raise ApiError("sso.login_failed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("sso.login_failed") from None
    if parsed.tzinfo is None:
        raise ApiError("sso.login_failed")
    return parsed.astimezone(UTC)


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(host, port, timeout=5.0, context=ssl.create_default_context())
        self._pinned_address = address

    def _create_connection(
        self,
        _address: tuple[str, int],
        timeout: object = socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        return socket.create_connection(
            (self._pinned_address, self.port), timeout, source_address
        )


def _post_oidc_token(endpoint: str, form: dict[str, str]) -> tuple[int, bytes]:
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    if host is None:
        raise ApiError("sso.login_failed")
    port = parsed.port or 443
    addresses = _public_resolutions(host, port)
    connection = _PinnedHttpsConnection(host, port, addresses[0])
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request(
            "POST",
            target,
            body=urlencode(form).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = connection.getresponse()
        declared = response.getheader("Content-Length")
        if declared is not None and (
            not declared.isdigit() or int(declared) > _TOKEN_RESPONSE_LIMIT
        ):
            raise ApiError("sso.login_failed")
        content = response.read(_TOKEN_RESPONSE_LIMIT + 1)
        if len(content) > _TOKEN_RESPONSE_LIMIT:
            raise ApiError("sso.login_failed")
        return response.status, content
    finally:
        connection.close()


def _public_resolutions(host: str, port: int) -> tuple[str, ...]:
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    }
    if not addresses:
        raise ApiError("sso.login_failed")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ApiError("sso.login_failed")
    return tuple(sorted(addresses))


__all__ = [
    "CertificateMaterial",
    "ProtocolIdentity",
    "exchange_oidc_code",
    "normalize_enterprise_domain",
    "oidc_authorization_url",
    "parse_saml_certificate",
    "saml_response_request_id",
    "validate_fixed_https_endpoint",
    "validate_oidc_id_token",
    "validate_oidc_jwks",
    "validate_saml_response",
]
