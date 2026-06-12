"""TERRAINA sensor."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DOMAIN,
    GRPC_HOST,
    NAME,
    SERVER_DOMAIN_NAME,
    WORKING_STATUS,
)
from .grpc_stream import TerrainaGrpcStream
from .grpc_util import split_bits
from .httpClient import RefreshFailedException, TerrainaHttpClient
from .platform_token import get_valid_app_token, is_app_token_valid

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up TERRAINA sensors."""
    region = entry.data.get("region")
    session = async_get_clientsession(hass)
    http_client = TerrainaHttpClient(
        hass=hass,
        base_url=SERVER_DOMAIN_NAME[region],
        session=session,
        region=region,
    )

    devices = await http_client.get_serial_numbers(config_entry=entry)

    sensors: list[SensorEntity] = []
    sensor_map: dict[str, LawnmowerSensor] = {}
    service_map: dict[str, str] = {}
    registered_services: list[str] = []

    # Ensure we have a valid app-token before starting gRPC streams
    ory_at = entry.data.get("token", {}).get("access_token", "")
    current_app_token = entry.data.get("app_token")
    app_token_data = await get_valid_app_token(
        session, region, current_app_token, ory_at
    )
    if app_token_data and app_token_data != current_app_token:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "app_token": app_token_data}
        )

    for device in devices:
        sn = str(device["sn"])
        device_name = device["deviceName"]
        model_name = device["modelName"]
        back_service_name = f"back_{model_name}_{sn[-6:]}"

        sensor = LawnmowerSensor(
            hass=hass,
            entry=entry,
            sn=sn,
            device_name=device_name,
        )
        sensors.append(sensor)
        sensor_map[sn] = sensor
        service_map[sn] = back_service_name

        async def handle_back(call: ServiceCall, *, serial: str = sn) -> None:
            await http_client.go_home(config_entry=entry, serial_number=serial)

        hass.services.async_register(DOMAIN, back_service_name, handle_back)
        registered_services.append(back_service_name)

        # Start gRPC stream for this device
        grpc_host = GRPC_HOST.get(region, GRPC_HOST["eu"])
        stream = TerrainaGrpcStream(
            grpc_host=grpc_host,
            sn=sn,
            get_ory_token=lambda e=entry: e.data.get("token", {}).get(
                "access_token", ""
            ),
            get_app_token=lambda e=entry: (
                e.data.get("app_token") or {}
            ).get("access_token", ""),
            state_callback=sensor.update_state,
        )
        sensor.set_grpc_stream(stream)
        stream.start()

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    if entry.entry_id not in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN][entry.entry_id]["services"] = registered_services

    sensors.append(
        LawnmowerControllerSensor(hass, entry, http_client, sensor_map, service_map)
    )
    async_add_entities(sensors)


class LawnmowerSensor(SensorEntity):
    """Represents a single TERRAINA mower with real-time gRPC state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        sn: str,
        device_name: str,
    ) -> None:
        """Init."""
        self._hass = hass
        self._entry = entry
        self._attr_has_entity_name = True
        self._sn = sn
        self._device_name = device_name
        self._attr_name = f"{device_name} ({sn})"
        self._attr_unique_id = f"{entry.entry_id}_{sn}_state"
        self._attr_native_value = "connecting"
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._stream: TerrainaGrpcStream | None = None

    def set_grpc_stream(self, stream: TerrainaGrpcStream) -> None:
        """Store a reference to the gRPC stream (for cleanup)."""
        self._stream = stream

    def update_state(self, state_dict: dict[str, Any]) -> None:
        """Called by the gRPC stream with parsed device state."""
        self._attr_extra_state_attributes = state_dict

        # The device state is delivered as a bit-packed integer under various keys
        status_int = None
        for key in ("sm", "status", "deviceStatus", "state"):
            val = state_dict.get(key)
            if isinstance(val, int):
                status_int = val
                break

        if status_int is not None:
            try:
                bits = split_bits(None, status_int)
                ws_idx = (status_int >> 6) & 0b1111
                ws = WORKING_STATUS[ws_idx] if ws_idx < len(WORKING_STATUS) else str(ws_idx)
                self._attr_native_value = ws or "idle"
                self._attr_extra_state_attributes.update(bits)
            except Exception:
                _LOGGER.debug("Could not decode status int %d", status_int)
                self._attr_native_value = str(status_int)
        else:
            self._attr_native_value = "connected"

        self.schedule_update_ha_state()

    @property
    def should_poll(self) -> bool:
        """gRPC push; no polling needed."""
        return False

    @property
    def device_info(self) -> DeviceInfo:
        """Device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            name=self._device_name,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop the gRPC stream when entity is removed."""
        if self._stream:
            self._stream.stop()


class LawnmowerControllerSensor(SensorEntity):
    """Polling sensor that keeps the device list up to date and refreshes tokens."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        http_client: TerrainaHttpClient,
        sensor_map: dict[str, LawnmowerSensor],
        service_map: dict[str, str],
    ) -> None:
        """Initialize."""
        self._attr_unique_id = f"{entry.data['region']}_devices"
        self._hass = hass
        self._attr_has_entity_name = True
        self._entry = entry
        self._disabled = False
        self._http_client = http_client
        self._attr_available = True
        self._attr_native_value = "Connected"
        self._sensor_map = sensor_map
        self._service_map = service_map
        self._attr_name = "Controller"

    @property
    def should_poll(self) -> bool:
        """Enable polling."""
        return True

    @property
    def device_info(self) -> DeviceInfo:
        """Device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, DOMAIN)},
            name=NAME,
        )

    async def async_update(self) -> None:
        """Refresh device list and renew app-token when needed."""
        if self._disabled:
            return

        # Proactively refresh app-token before it expires
        region = self._entry.data.get("region")
        current_app_token = self._entry.data.get("app_token")
        if not is_app_token_valid(current_app_token):
            session = async_get_clientsession(self._hass)
            ory_at = self._entry.data.get("token", {}).get("access_token", "")
            new_app_token = await get_valid_app_token(
                session, region, current_app_token, ory_at
            )
            if new_app_token:
                self._hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, "app_token": new_app_token},
                )

        try:
            new_devices = await self._http_client.get_serial_numbers(
                config_entry=self._entry
            )
            self._attr_available = True
            current_serials = set(self._sensor_map.keys())
            new_serials = {str(d["sn"]) for d in new_devices}

            added = new_serials - current_serials
            removed = current_serials - new_serials
            for sn in removed:
                svc = self._service_map.pop(sn, None)
                if svc and self.hass.services.has_service(DOMAIN, svc):
                    self.hass.services.async_remove(DOMAIN, svc)
            if added or removed:
                _LOGGER.info(
                    "Device list changed. Added: %s, Removed: %s", added, removed
                )
                self._hass.async_create_task(
                    self._hass.config_entries.async_reload(self._entry.entry_id)
                )
        except RefreshFailedException:
            _LOGGER.warning("Token refresh failed; marking controller unavailable")
            self._attr_available = False
            self._disabled = True
            self._attr_native_value = "Disconnected"
        except Exception:
            _LOGGER.exception("Failed to update device list")
            self._attr_available = False
