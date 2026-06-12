"""App-token (kk5fd5Ce format) manager for TERRAINA integration.

Manages a separate token pair from the Ory OAuth2 tokens.  These tokens are
obtained from /user-center/token and are required for gRPC and the new REST API.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from .const import APP_TOKEN_URL, PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET
from .util import get_standard_headers

_LOGGER = logging.getLogger(__name__)

# Safety margin: refresh 5 minutes before actual expiry
_EXPIRY_MARGIN_SEC = 300


def _hmac_form_headers(region: str) -> dict[str, str]:
    h = get_standard_headers(PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    h["Accept-Language"] = "en"
    return h


def is_app_token_valid(app_token: dict[str, Any]) -> bool:
    """Return True if the app-token is still usable."""
    if not app_token or "access_token" not in app_token:
        return False
    expires_at = app_token.get("expires_at", 0)
    return time.time() < expires_at - _EXPIRY_MARGIN_SEC


def _stamp_expiry(token_data: dict[str, Any]) -> dict[str, Any]:
    """Add an absolute expires_at timestamp to a freshly-issued token dict."""
    expires_in = int(token_data.get("expires_in", 7200))
    return {**token_data, "expires_at": time.time() + expires_in}


async def acquire_app_token(
    session: aiohttp.ClientSession,
    region: str,
    ory_access_token: str,
) -> dict[str, Any] | None:
    """Try to get a fresh app-token by exchanging the Ory access-token.

    The endpoint is /user-center/token.  We send the Ory token as the
    Authorization Bearer header; the server introspects it and issues a
    kk5fd5Ce token pair.

    Returns the token dict on success, None on failure.
    """
    url = APP_TOKEN_URL.get(region)
    if not url:
        _LOGGER.warning("No app-token URL for region %r", region)
        return None

    headers = _hmac_form_headers(region)
    headers["Authorization"] = f"Bearer {ory_access_token}"

    try:
        async with session.post(
            url,
            headers=headers,
            data={},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            body = await resp.json(content_type=None)
            if body.get("code") == 200 and body.get("data"):
                _LOGGER.debug("App-token acquired via ory exchange")
                return _stamp_expiry(body["data"])
            _LOGGER.debug(
                "App-token ory exchange returned code=%s msg=%r",
                body.get("code"),
                body.get("msg"),
            )
    except Exception:
        _LOGGER.debug("App-token ory exchange failed", exc_info=True)

    return None


async def refresh_app_token(
    session: aiohttp.ClientSession,
    region: str,
    refresh_token: str,
) -> dict[str, Any] | None:
    """Refresh the app-token using its own refresh_token.

    POST /user-center/token  Content-Type: application/x-www-form-urlencoded
    Body: grant_type=refresh_token&refresh_token=<kk5fd5Ce_refresh>
    """
    url = APP_TOKEN_URL.get(region)
    if not url:
        return None

    headers = _hmac_form_headers(region)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        async with session.post(
            url,
            headers=headers,
            data=data,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            body = await resp.json(content_type=None)
            if body.get("code") == 200 and body.get("data"):
                _LOGGER.debug("App-token refreshed successfully")
                return _stamp_expiry(body["data"])
            _LOGGER.warning(
                "App-token refresh failed: code=%s msg=%r",
                body.get("code"),
                body.get("msg"),
            )
    except Exception:
        _LOGGER.warning("App-token refresh request failed", exc_info=True)

    return None


async def login_with_password(
    session: aiohttp.ClientSession,
    region: str,
    username: str,
    password: str,
) -> dict[str, Any] | None:
    """Obtain initial kk5fd5Ce app-token via username/password grant.

    POST /user-center/token
    Body: grant_type=password&username=<email>&password=<pass>
    Returns the token dict on success, None on failure.
    """
    url = APP_TOKEN_URL.get(region)
    if not url:
        return None

    headers = _hmac_form_headers(region)
    data = {
        "grant_type": "password",
        "username": username,
        "password": password,
    }

    try:
        async with session.post(
            url,
            headers=headers,
            data=data,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            body = await resp.json(content_type=None)
            if body.get("code") == 200 and body.get("data"):
                _LOGGER.debug("App-token obtained via password grant")
                return _stamp_expiry(body["data"])
            _LOGGER.warning(
                "App-token password grant failed: code=%s msg=%r",
                body.get("code"),
                body.get("msg"),
            )
            return None
    except Exception:
        _LOGGER.warning("App-token password grant request failed", exc_info=True)
        return None


async def get_valid_app_token(
    session: aiohttp.ClientSession,
    region: str,
    current_app_token: dict[str, Any] | None,
    ory_access_token: str,
) -> dict[str, Any] | None:
    """Return a valid app-token, refreshing or re-acquiring as needed."""
    if is_app_token_valid(current_app_token):
        return current_app_token

    # Try refresh first (cheaper than full re-acquisition)
    if current_app_token and current_app_token.get("refresh_token"):
        new_token = await refresh_app_token(
            session, region, current_app_token["refresh_token"]
        )
        if new_token:
            return new_token

    # Fall back to full ory exchange
    return await acquire_app_token(session, region, ory_access_token)
