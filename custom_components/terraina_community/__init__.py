"""TERRAINA Community — Home Assistant integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

from .const import DOMAIN, GRPC_HOST, SERVER_DOMAIN_NAME
from .coordinator import TerrainaCoordinator
from .grpc_stream import TerrainaGrpcStream
from .httpClient import TerrainaHttpClient
from .platform_token import get_valid_app_token, is_app_token_valid, login_with_password

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["lawn_mower", "sensor", "select"]

_APP_TOKEN_CHECK_INTERVAL = timedelta(minutes=30)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TERRAINA Community from a config entry."""
    region = entry.data.get("region", "eu")
    session = async_get_clientsession(hass)

    http_client = TerrainaHttpClient(
        hass=hass,
        base_url=SERVER_DOMAIN_NAME[region],
        session=session,
        region=region,
    )
    coordinator = TerrainaCoordinator(hass, http_client, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "http_client": http_client,
        "grpc_streams": {},
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start gRPC streams if we have an app_token
    if entry.data.get("app_token"):
        await _start_grpc_streams(hass, entry)
    else:
        _LOGGER.warning("No app_token — triggering reauth to collect platform credentials")
        entry.async_start_reauth(hass)

    # Periodically check and refresh app_token
    async def _refresh_app_token(_now=None) -> None:
        current = entry.data.get("app_token")
        if is_app_token_valid(current):
            return
        new_token = await get_valid_app_token(session, region, current)
        if new_token and new_token != current:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "app_token": new_token}
            )
            _LOGGER.debug("App-token refreshed proactively")
        elif not new_token:
            _LOGGER.warning("App-token refresh failed — gRPC may disconnect")

    entry.async_on_unload(
        async_track_time_interval(hass, _refresh_app_token, _APP_TOKEN_CHECK_INTERVAL)
    )

    return True


async def _start_grpc_streams(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start one gRPC stream per device."""
    region = entry.data.get("region", "eu")
    grpc_host = GRPC_HOST.get(region)
    if not grpc_host:
        _LOGGER.error("No gRPC host for region %r", region)
        return

    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: TerrainaCoordinator = data["coordinator"]
    entity_map: dict[str, list] = data.get("entities", {})

    region = entry.data.get("region", "eu")

    async def _relogin() -> bool:
        """Re-obtain kk5fd5Ce tokens via password grant and update the entry."""
        email = entry.data.get("platform_email", "")
        password = entry.data.get("platform_password", "")
        if not email or not password:
            _LOGGER.warning("No platform credentials stored — triggering reauth")
            entry.async_start_reauth(hass)
            return False
        new_token = await login_with_password(
            async_get_clientsession(hass), region, email, password
        )
        if new_token:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "app_token": new_token}
            )
            _LOGGER.info("App-token refreshed via password re-login")
            return True
        _LOGGER.warning("Password re-login failed for app-token refresh")
        return False

    for device in coordinator.data or []:
        sn = str(device["sn"])

        def _state_cb(state_dict, _sn=sn) -> None:
            for entity in entity_map.get(_sn, []):
                entity.update_from_grpc(state_dict)

        stream = TerrainaGrpcStream(
            grpc_host=grpc_host,
            sn=sn,
            get_ory_token=lambda e=entry: (e.data.get("token") or {}).get("access_token", ""),
            get_app_token=lambda e=entry: (e.data.get("app_token") or {}).get("access_token", ""),
            state_callback=_state_cb,
            on_unauthenticated=_relogin,
        )
        stream.start()
        data["grpc_streams"][sn] = stream
        _LOGGER.debug("gRPC stream started for device %s", sn)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a TERRAINA Community config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    for stream in data.get("grpc_streams", {}).values():
        stream.stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
