"""TERRAINA Community — Home Assistant integration."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, GRPC_HOST, SERVER_DOMAIN_NAME
from .coordinator import TerrainaCoordinator
from .grpc_stream import TerrainaGrpcStream
from .httpClient import TerrainaHttpClient
from .platform_token import get_valid_app_token, is_app_token_valid, login_with_password

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["lawn_mower", "sensor", "select", "switch", "time"]

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

    _register_services(hass)

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
            entities = data.get("entities", {}).get(_sn, [])
            _LOGGER.debug("gRPC _state_cb for %s: %d entities, keys=%s", _sn, len(entities), list(state_dict.keys()))
            for entity in entities:
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


_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Required("week"): vol.All(vol.Coerce(int), vol.Range(min=0, max=6)),
        vol.Optional("slot", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=9)),
        vol.Required("enabled"): bool,
        vol.Required("start_time"): str,
        vol.Required("end_time"): str,
    }
)


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "set_schedule_day"):
        return

    async def _handle_set_schedule_day(call: ServiceCall) -> None:
        from .lawn_mower import TerrainaLawnMower

        week = int(call.data["week"])
        slot_idx = int(call.data.get("slot", 0))
        enabled = 1 if call.data["enabled"] else 0
        entity_id_filter = call.data.get("entity_id")

        def _to_min(t: str) -> int:
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        start_min = _to_min(call.data["start_time"])
        end_min = _to_min(call.data["end_time"])

        found = False
        for _entry_id, data in hass.data.get(DOMAIN, {}).items():
            if not isinstance(data, dict):
                continue
            entity_map = data.get("entities", {})
            http_client = data.get("http_client")
            config_entry = hass.config_entries.async_get_entry(_entry_id)
            if not config_entry or not http_client:
                continue

            for sn, entities in entity_map.items():
                mower = next(
                    (e for e in entities if isinstance(e, TerrainaLawnMower)), None
                )
                if mower is None:
                    continue
                if entity_id_filter and mower.entity_id != entity_id_filter:
                    continue

                # Device rejects schedule changes while actively mowing/returning
                from homeassistant.components.lawn_mower import LawnMowerActivity
                if mower._attr_activity in (
                    LawnMowerActivity.MOWING,
                    LawnMowerActivity.RETURNING,
                ):
                    raise HomeAssistantError(
                        f"Cannot change the schedule while the mower is active "
                        f"({mower._attr_activity.value}). "
                        "Dock the mower first, then update the schedule."
                    )

                # Split current schedule into this day's slots and all other days
                all_slots = list(mower._schedule)
                day_slots = [d for d in all_slots if d.get("week") == week]
                other_days = [d for d in all_slots if d.get("week") != week]

                new_slot = {
                    "week": week, "enable": enabled,
                    "startTime": start_min, "endTime": end_min,
                    "mapId": (day_slots[slot_idx] if slot_idx < len(day_slots) else {}).get("mapId", 1),
                    "boundaryId": -1, "regionId": -1, "needEdge": 1,
                }
                if slot_idx < len(day_slots):
                    day_slots[slot_idx] = new_slot   # update existing slot
                else:
                    day_slots.append(new_slot)        # add new slot

                updated = sorted(other_days + day_slots, key=lambda d: d.get("week", 0))
                await http_client.set_schedule(config_entry, sn, updated)

                mower._schedule = updated
                mower.async_write_ha_state()
                found = True
                _LOGGER.debug(
                    "set_schedule_day: sn=%s week=%d slot=%d enabled=%d %s-%s",
                    sn, week, slot_idx, enabled, call.data["start_time"], call.data["end_time"],
                )

        if not found:
            raise HomeAssistantError(
                f"No TERRAINA lawn mower entity found"
                + (f" matching {entity_id_filter}" if entity_id_filter else "")
            )

    hass.services.async_register(
        DOMAIN,
        "set_schedule_day",
        _handle_set_schedule_day,
        schema=_SET_SCHEDULE_SCHEMA,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a TERRAINA Community config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    for stream in data.get("grpc_streams", {}).values():
        stream.stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
