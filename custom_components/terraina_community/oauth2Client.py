"""OAuth2 client for the TERRAINA Community integration."""

from typing import Any

from aiohttp import BasicAuth

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    LocalOAuth2ImplementationWithPkce,
)

from .const import CLIENT_ID, CLIENT_SECRET, DOMAIN


class OAuth2Impl(LocalOAuth2ImplementationWithPkce):
    """OAuth2 PKCE implementation for the Dongcheng IoT platform."""

    async def _token_request(self, data: dict) -> dict:
        session = async_get_clientsession(self.hass)
        auth = BasicAuth(self.client_id, self.client_secret)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        async with session.post(
            self.token_url, data=data, headers=headers, auth=auth
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        request_data: dict = {
            "grant_type": "authorization_code",
            "code": external_data["code"],
            "client_id": self.client_id,
            "redirect_uri": external_data["state"]["redirect_uri"],
            "code_verifier": self.generate_code_verifier(),
        }
        request_data.update(self.extra_token_resolve_data)
        return await self._token_request(request_data)

    async def _async_refresh_token(self, token: dict) -> dict:
        new_token = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
            }
        )
        return {**token, **new_token}


def create_auth_implementation(
    hass: HomeAssistant, authorize_url: str, token_url: str
) -> OAuth2Impl:
    """Create the OAuth2 implementation instance."""
    return OAuth2Impl(
        hass=hass,
        domain=DOMAIN,
        client_id=CLIENT_ID,
        authorize_url=authorize_url,
        token_url=token_url,
        client_secret=CLIENT_SECRET,
    )
