"""Verify an OIDC identity token against an explicitly configured issuer.

This is a building block, NOT a complete MCP OAuth authorization server.
Never accept an email from an MCP tool argument or arbitrary HTTP header.
"""
from dataclasses import dataclass
import jwt
from jwt import PyJWKClient

@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    email: str

def verify_identity_token(token: str, *, issuer: str, audience: str, jwks_url: str, allowed_email: str) -> VerifiedIdentity:
    if not all((token, issuer, audience, jwks_url, allowed_email)):
        raise ValueError("identity verification is not configured")
    if not jwks_url.startswith("https://"):
        raise ValueError("JWKS URL must use HTTPS")
    key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token, key, algorithms=["RS256", "ES256"],
        audience=audience, issuer=issuer,
        options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        leeway=30,
    )
    email = claims.get("email")
    if claims.get("email_verified") is not True or not isinstance(email, str):
        raise PermissionError("verified email claim required")
    if email.casefold() != allowed_email.casefold():
        raise PermissionError("identity not on local allowlist")
    return VerifiedIdentity(issuer=claims["iss"], subject=claims["sub"], email=email)
