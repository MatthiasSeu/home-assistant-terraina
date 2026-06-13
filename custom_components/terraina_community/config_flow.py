"""Config flow for TERRAINA Community integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "TerrainaOptionsFlow":
        return TerrainaOptionsFlow()

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


class TerrainaOptionsFlow(config_entries.OptionsFlow):
    """Options flow — configure smart weather protection sensors."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            # Store empty strings as absent so SmartProtectionManager treats them as "not configured"
            cleaned = {k: v for k, v in user_input.items() if v != ""}
            for k in ("rain_factor_mm", "max_temperature", "auto_dock_unsafe"):
                if k in user_input:
                    cleaned[k] = user_input[k]
            return self.async_create_entry(title="", data=cleaned)

        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "precipitation_sensor",
                        default=opts.get("precipitation_sensor", ""),
                    ): str,
                    vol.Optional(
                        "rain_hold_base_hours",
                        default=opts.get("rain_hold_base_hours", 0),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=12)),
                    vol.Optional(
                        "rain_factor_mm",
                        default=opts.get("rain_factor_mm", 5),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
                    vol.Optional(
                        "rain_hold_step_hours",
                        default=opts.get("rain_hold_step_hours", 1),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
                    vol.Optional(
                        "rain_hold_max_hours",
                        default=opts.get("rain_hold_max_hours", 24),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=72)),
                    vol.Optional(
                        "forecast_entity",
                        default=opts.get("forecast_entity", ""),
                    ): str,
                    vol.Optional(
                        "forecast_hours_ahead",
                        default=opts.get("forecast_hours_ahead", 2),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
                    vol.Optional(
                        "temperature_sensor",
                        default=opts.get("temperature_sensor", ""),
                    ): str,
                    vol.Optional(
                        "max_temperature",
                        default=opts.get("max_temperature", 32),
                    ): vol.All(vol.Coerce(int), vol.Range(min=20, max=40)),
                    vol.Optional(
                        "auto_dock_unsafe",
                        default=opts.get("auto_dock_unsafe", False),
                    ): bool,
                }
            ),
        )
