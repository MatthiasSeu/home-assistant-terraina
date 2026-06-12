"""Config flow for TERRAINA integration."""

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


class TerrainaConfigFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Config flow for TERRAINA."""

    VERSION = 1
    MINOR_VERSION = 1
    DOMAIN = DOMAIN

    def __init__(self) -> None:
        """Initialize."""
        super().__init__()
        self.data: object | None = None
        self._http_client: TerrainaHttpClient | None = None
        self._user_data: dict | None = None
        self._my_reauth_entry_id: str | None = None
        self._oauth_data: dict | None = None  # ory tokens from OAuth2 step

    @property
    def logger(self):
        """Return logger."""
        return logging.getLogger(__name__)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """First step: select region."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(error="Only one TERRAINA account allowed.")
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
        self._user_data = {"region": region}
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
        """After OAuth2 completes — save ory tokens and ask for platform password."""
        if self._user_data:
            data.update(self._user_data)
        self._oauth_data = data
        return await self.async_step_platform_auth()

    async def async_step_platform_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step: enter TERRAINA platform password to obtain gRPC app-token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            region = self._oauth_data.get("region", "eu") if self._oauth_data else "eu"
            session = async_get_clientsession(self.hass)

            # Derive email from user input or fall back to empty string
            email = user_input.get("email", "").strip()
            password = user_input.get("password", "")

            app_token = await login_with_password(session, region, email, password)
            if app_token:
                final_data = {
                    **(self._oauth_data or {}),
                    "app_token": app_token,
                    "platform_email": email,
                }
                if self._my_reauth_entry_id:
                    return self.async_update_reload_and_abort(
                        self.hass.config_entries.async_get_entry(
                            self._my_reauth_entry_id
                        ),
                        data_updates=final_data,
                        reason="reauth successful",
                    )
                return self.async_create_entry(title=DOMAIN, data=final_data)

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
        self._my_reauth_entry_id = self.context.get("entry_id")
        region = user_input.get("region", "eu")
        self._user_data = {"region": region}
        if region:
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
