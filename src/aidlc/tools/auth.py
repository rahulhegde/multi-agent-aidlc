from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.auth.provider import AccessToken


def validate_service_url(value: str) -> str:
    """Development HTTP is confined to loopback; credentials never belong in URLs."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise ValueError(
            "Service URLs require HTTPS or loopback HTTP without credentials or queries"
        )
    return value.rstrip("/")


async def discover_oidc(client: httpx.AsyncClient, issuer: str) -> dict[str, Any]:
    response = await client.get(f"{issuer}/.well-known/openid-configuration")
    response.raise_for_status()
    metadata = response.json()
    if not isinstance(metadata, dict) or metadata.get("issuer") != issuer:
        raise ValueError("OIDC discovery issuer mismatch")
    return metadata


class OIDCTokenVerifier:
    """Verify Keycloak access JWTs against discovered keys, never token-supplied URLs."""

    def __init__(
        self, issuer: str, resource: str, client: httpx.AsyncClient, *, cache_seconds: float = 300
    ) -> None:
        self.issuer = validate_service_url(issuer)
        self.resource = validate_service_url(resource)
        self.client = client
        self.cache_seconds = cache_seconds
        self._keys: dict[str, jwt.PyJWK] = {}
        self._refreshed_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> jwt.PyJWK | None:
        async with self._lock:
            age = time.monotonic() - self._refreshed_at
            # Refresh on rotation, with a cooldown for attacker-chosen unknown kids.
            if age >= self.cache_seconds or (kid not in self._keys and age >= 2):
                metadata = await discover_oidc(self.client, self.issuer)
                jwks_url = validate_service_url(metadata["jwks_uri"])
                response = await self.client.get(jwks_url)
                response.raise_for_status()
                keys = jwt.PyJWKSet.from_dict(response.json()).keys
                self._keys = {
                    key.key_id: key
                    for key in keys
                    if key.key_id and key.algorithm_name == "RS256" and key.public_key_use == "sig"
                }
                self._refreshed_at = time.monotonic()
            return self._keys.get(kid)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            if len(token) > 16_384:
                return None
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                return None
            key = await self._key(kid)
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.resource,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
            # Keycloak ID tokens must not be accepted as resource access tokens.
            if claims.get("typ") != "Bearer" and header.get("typ") != "at+jwt":
                return None
            for name in ("exp", "iat", "nbf"):
                if name in claims and type(claims[name]) is not int:
                    return None
            subject, client_id, scopes = claims["sub"], claims.get("azp"), claims.get("scope", "")
            if (
                not isinstance(subject, str)
                or not subject.strip()
                or not isinstance(client_id, str)
                or not client_id.strip()
                or not isinstance(scopes, str)
            ):
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                subject=subject,
                scopes=scopes.split(),
                expires_at=claims["exp"],
                resource=self.resource,
                claims={"iss": self.issuer},
            )
        except jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError:
            # Fail closed without logging JWTs, headers, or provider response bodies.
            return None


RUN_CLIENTS = frozenset(
    {
        "backend-agent",
        "frontend-agent",
        "test-agent",
        "integration-agent",
        "static-analysis-agent",
        "evaluation-agent",
    }
)


def permitted(token: AccessToken | None, scope: str) -> bool:
    return bool(
        token
        and token.subject
        and token.expires_at
        and token.expires_at > time.time()
        and scope in token.scopes
        and (scope != "analysis:run" or token.client_id in RUN_CLIENTS)
    )


def principal(token: AccessToken) -> str:
    import json

    return json.dumps([token.client_id, (token.claims or {}).get("iss"), token.subject])
