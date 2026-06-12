"""Config flow for TERRAINA Community integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, GLOBAL_DOMAIN, SERVER_DOMAIN_NAME
from .httpClient import TerrainaHttpClient
from .oauth2Client import create_auth_implementation
from .platform_token import login_with_password

_LOGGER = logging.getLogger(__name__)


class TerrainaCommunityConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Config flow for TERRAINA Community."""

    VERSION = 1
    MINOR_VERSION = 1
    DOMAIN = DOMAIN

    def __init__(self) -> None:
        super().__init__()
        self._tc_user_data: dict | None = None
        self._tc_reauth_entry_id: str | None = None
        self._oauth_data: dict | None = None

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(__name__)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: select country / region."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        http_client = TerrainaHttpClient(
            hass=self.hass,
            base_url=GLOBAL_DOMAIN,
            session=async_get_clientsession(self.hass),
            region="",
        )
        countries = await http_client.get_countries()

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {vol.Required("country"): vol.In(countries.keys())}
                ),
            )

        region = countries[user_input["country"]]
        self._tc_user_data = {"region": region}
        config_entry_oauth2_flow.async_register_implementation(
            self.hass,
            DOMAIN,
            create_auth_implementation(
                self.hass,
                authorize_url=f"{SERVER_DOMAIN_NAME[region]}/user-center/oauth2/auth",
                token_url=f"{SERVER_DOMAIN_NAME[region]}/user-center/oauth2/token",
            ),
        )
        return await self.async_step_pick_implementation()

    async def async_oauth_create_entry(self, data: dict) -> config_entries.ConfigFlowResult:
        """After OAuth2 — save ory tokens, then ask for platform password."""
        if self._tc_user_data:
            data.update(self._tc_user_data)
        self._oauth_data = data
        return await self.async_step_platform_auth()

    async def async_step_platform_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step: enter TERRAINA app credentials to obtain gRPC app-token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            region = (self._oauth_data or {}).get("region", "eu")
            session = async_get_clientsession(self.hass)
            email = user_input.get("email", "").strip()
            password = user_input.get("password", "")

            app_token = await login_with_password(session, region, email, password)
            if app_token:
                final_data = {
                    **(self._oauth_data or {}),
                    "app_token": app_token,
                    "platform_email": email,
                    "platform_password": password,
                }
                if self._tc_reauth_entry_id:
                    return self.async_update_reload_and_abort(
                        self.hass.config_entries.async_get_entry(
                            self._tc_reauth_entry_id
                        ),
                        data_updates=final_data,
                        reason="reauth_successful",
                    )
                return self.async_create_entry(title="TERRAINA Community", data=final_data)

            errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="platform_auth",
            data_schema=vol.Schema(
                {
                    vol.Required("email"): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, user_input: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication."""
        self._tc_reauth_entry_id = self.context.get("entry_id")
        region = user_input.get("region", "eu")
        self._tc_user_data = {"region": region}
        config_entry_oauth2_flow.async_register_implementation(
            self.hass,
            DOMAIN,
            create_auth_implementation(
                self.hass,
                authorize_url=f"{SERVER_DOMAIN_NAME[region]}/user-center/oauth2/auth",
                token_url=f"{SERVER_DOMAIN_NAME[region]}/user-center/oauth2/token",
            ),
        )
        return await self.async_step_pick_implementation()
