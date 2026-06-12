"""App-token (kk5fd5Ce format) manager for TERRAINA Community integration."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from .const import APP_TOKEN_URL, PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET
from .util import get_standard_headers

_LOGGER = logging.getLogger(__name__)

_EXPIRY_MARGIN_SEC = 300


def _hmac_form_headers(region: str) -> dict[str, str]:
    h = get_standard_headers(PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    h["Accept-Language"] = "en"
    return h


def is_app_token_valid(app_token: dict[str, Any]) -> bool:
    if not app_token or "access_token" not in app_token:
        return False
    expires_at = app_token.get("expires_at", 0)
    return time.time() < expires_at - _EXPIRY_MARGIN_SEC


def _stamp_expiry(token_data: dict[str, Any]) -> dict[str, Any]:
    expires_in = int(token_data.get("expires_in", 7200))
    return {**token_data, "expires_at": time.time() + expires_in}


async def login_with_password(
    session: aiohttp.ClientSession,
    region: str,
    username: str,
    password: str,
) -> dict[str, Any] | None:
    """Obtain initial kk5fd5Ce app-token via username/password grant."""
    url = APP_TOKEN_URL.get(region)
    if not url:
        return None

    headers = _hmac_form_headers(region)
    data = {"grant_type": "password", "username": username, "password": password}

    try:
        async with session.post(
            url, headers=headers, data=data, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            body = await resp.json(content_type=None)
            if body.get("code") == 200 and body.get("data"):
                _LOGGER.debug("App-token obtained via password grant")
                return _stamp_expiry(body["data"])
            _LOGGER.warning(
                "App-token password grant failed: code=%s msg=%r",
                body.get("code"), body.get("msg"),
            )
    except Exception:
        _LOGGER.warning("App-token password grant request failed", exc_info=True)
    return None


async def refresh_app_token(
    session: aiohttp.ClientSession,
    region: str,
    refresh_token: str,
) -> dict[str, Any] | None:
    """Refresh the kk5fd5Ce app-token using its own refresh_token."""
    url = APP_TOKEN_URL.get(region)
    if not url:
        return None

    headers = _hmac_form_headers(region)
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}

    try:
        async with session.post(
            url, headers=headers, data=data, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            body = await resp.json(content_type=None)
            if body.get("code") == 200 and body.get("data"):
                _LOGGER.debug("App-token refreshed successfully")
                return _stamp_expiry(body["data"])
            _LOGGER.warning(
                "App-token refresh failed: code=%s msg=%r",
                body.get("code"), body.get("msg"),
            )
    except Exception:
        _LOGGER.warning("App-token refresh request failed", exc_info=True)
    return None


async def get_valid_app_token(
    session: aiohttp.ClientSession,
    region: str,
    current_app_token: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a valid app-token, refreshing as needed. Returns None if refresh fails."""
    if is_app_token_valid(current_app_token):
        return current_app_token

    if current_app_token and current_app_token.get("refresh_token"):
        new_token = await refresh_app_token(
            session, region, current_app_token["refresh_token"]
        )
        if new_token:
            return new_token

    _LOGGER.warning("App-token expired and refresh failed — reauth required")
    return None
