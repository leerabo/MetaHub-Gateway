"""PRD-120 shared CSR and registration proof contracts."""

from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID
from metahub_shared.private_gateway import (
    build_registration_challenge_proof,
    parse_edge_csr,
    verify_registration_challenge_proof,
)


def _csr(
    key,
    *,
    subject: x509.Name | None = None,
    san: x509.SubjectAlternativeName | None = None,
) -> str:
    builder = x509.CertificateSigningRequestBuilder().subject_name(subject or x509.Name([]))
    if san is not None:
        builder = builder.add_extension(san, critical=False)
    algorithm = None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    return builder.sign(key, algorithm).public_bytes(serialization.Encoding.PEM).decode("ascii")


def test_ed25519_csr_and_challenge_proof_bind_all_registration_coordinates() -> None:
    parsed = parse_edge_csr(_csr(ed25519.Ed25519PrivateKey.generate()))
    token = "erg_" + "A" * 43
    challenge = "ech_" + "B" * 43
    deployment_id = "edp_" + "1" * 32
    proof = build_registration_challenge_proof(
        token,
        deployment_id=deployment_id,
        challenge=challenge,
        csr_sha256=parsed.csr_sha256,
    )

    assert verify_registration_challenge_proof(
        proof,
        token,
        deployment_id=deployment_id,
        challenge=challenge,
        csr_sha256=parsed.csr_sha256,
    )
    assert not verify_registration_challenge_proof(
        proof,
        token,
        deployment_id="edp_" + "2" * 32,
        challenge=challenge,
        csr_sha256=parsed.csr_sha256,
    )
    assert not verify_registration_challenge_proof(
        proof,
        token,
        deployment_id=deployment_id,
        challenge="ech_" + "C" * 43,
        csr_sha256=parsed.csr_sha256,
    )


def test_csr_rejects_non_ed25519_subject_and_requested_extensions() -> None:
    invalid = (
        _csr(rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        _csr(
            ed25519.Ed25519PrivateKey.generate(),
            subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "attacker")]),
        ),
        _csr(
            ed25519.Ed25519PrivateKey.generate(),
            san=x509.SubjectAlternativeName([x509.DNSName("attacker.invalid")]),
        ),
    )
    for csr_pem in invalid:
        try:
            parse_edge_csr(csr_pem)
        except ValueError as exc:
            assert str(exc) == "edge.csr_invalid"
        else:
            raise AssertionError("unsafe CSR accepted")
