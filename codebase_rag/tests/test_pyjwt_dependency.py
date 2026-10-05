# GHSA-ffc3-869f-jxw9 (CVE-2026-102268): PyJWT <= 2.13.0 only refused an
# asymmetric key as an HMAC secret when its PEM regex recognised the key, and
# that regex misses PEM byte-forms cryptography still loads. With a mixed
# allow-list, anyone holding the public key could forge HS256 tokens. PyJWT
# reaches the runtime closure through mcp's auth extra.
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from importlib.metadata import version

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from packaging.version import Version

_MIXED_ALLOW_LIST = ["ES256", "HS256"]
_FORGED_CLAIMS = {"sub": "superadmin"}


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _hs256_token(secret: bytes, claims: dict[str, str]) -> str:
    # Signed by hand so the token does not depend on the guard under test.
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signing_input = header + b"." + payload
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + _b64url(signature)).decode()


def _public_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


_PEM_MUTATIONS: dict[str, Callable[[bytes], bytes]] = {
    "tab_before_end_marker": lambda pem: pem.replace(b"-----END", b"\t-----END"),
    "cr_only_terminators": lambda pem: pem.replace(b"\n", b"\r"),
    "folded_single_line": lambda pem: pem.replace(b"\n", b""),
}


def test_pyjwt_dependency_is_patched() -> None:
    assert Version(version("pyjwt")) >= Version("2.14.0"), version("pyjwt")


def test_hand_signed_hs256_token_verifies_with_a_plain_secret() -> None:
    # Control: proves _hs256_token builds a token PyJWT accepts, so the
    # rejection below is the key guard firing, not a malformed token.
    secret = b"a-genuine-shared-hmac-secret-of-32b"
    token = _hs256_token(secret, _FORGED_CLAIMS)
    assert jwt.decode(token, secret, algorithms=_MIXED_ALLOW_LIST) == _FORGED_CLAIMS


@pytest.mark.parametrize("mutate", _PEM_MUTATIONS.values(), ids=_PEM_MUTATIONS)
def test_mutated_public_pem_is_rejected_as_hmac_secret(
    mutate: Callable[[bytes], bytes],
) -> None:
    mutated = mutate(_public_pem())
    # The mutation must still be a real public key, or this is not the bypass.
    serialization.load_pem_public_key(mutated)
    forged = _hs256_token(mutated, _FORGED_CLAIMS)
    with pytest.raises(jwt.InvalidKeyError):
        jwt.decode(forged, mutated, algorithms=_MIXED_ALLOW_LIST)
