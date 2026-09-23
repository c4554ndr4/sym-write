"""Google identity verification and server-side checks for the public beta."""

import asyncio
import base64
import hashlib
import hmac
import math
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException

from .providers import api_key


def google_url(settings, state, nonce, verifier):
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.origin + "/auth/google/callback",
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )


def verify_google_id_token(encoded, audience):
    # Delegate signature, issuer, audience and expiry verification to Google's library.
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    transport = Request()

    def bounded_transport(*args, **kwargs):
        kwargs["timeout"] = 10
        return transport(*args, **kwargs)

    return id_token.verify_oauth2_token(encoded, bounded_transport, audience=audience)


async def google_identity(http, settings, code, nonce, verifier):
    if not code or len(code) > 4096:
        raise ValueError("Invalid authorization code")
    response = await http.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.origin + "/auth/google/callback",
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout=15,
    )
    response.raise_for_status()
    claims = await asyncio.to_thread(
        verify_google_id_token, response.json()["id_token"], settings.google_client_id
    )
    if (
        claims.get("email_verified") is not True
        or not isinstance(claims.get("nonce"), str)
        or not hmac.compare_digest(claims["nonce"], nonce)
        or not isinstance(claims.get("sub"), str)
        or not 1 <= len(claims["sub"]) <= 255
        or not claims["sub"].isascii()
        or claims.get("azp", settings.google_client_id) != settings.google_client_id
    ):
        raise ValueError("Identity claims were not verified")
    # No email, name, picture, access token or refresh token is retained.
    return claims["sub"]


async def verify_challenge(http, settings, token):
    if not settings.public:
        return
    if not token or len(token) > 2048:
        raise HTTPException(403, "Complete the verification before continuing.")
    try:
        response = await http.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": settings.turnstile_secret,
                "response": token,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if (
            data.get("success") is not True
            or data.get("hostname") != urlsplit(settings.origin).hostname
            or data.get("action") != "continue"
        ):
            raise ValueError()
    except Exception:
        raise HTTPException(
            403, "Verification expired or failed. Please try again."
        ) from None


async def verify_spending_cap(http, settings, stage):
    if not settings.public:
        return
    try:
        response = await http.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": "Bearer " + api_key(stage, public=True)},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()["data"]
        limit, remaining = data.get("limit"), data.get("limit_remaining")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, (int, float))
            or not math.isfinite(limit)
            or not 0 < limit <= settings.key_limit_usd
            or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining)
            or remaining <= 0
            or data.get("is_management_key") is not False
            or data.get("limit_reset") not in (None, "daily", "weekly", "monthly")
        ):
            raise ValueError()
    except Exception:
        raise HTTPException(
            503, "The beta's provider budget is unavailable. Generation is paused."
        ) from None
