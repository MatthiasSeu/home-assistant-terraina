"""TERRAINA Community select platform — working mode (auto / manual)."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TerrainaCoordinator
from .grpc_util import split_bits
from .httpClient import TerrainaHttpClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: TerrainaCoordinator = data["coordinator"]
    http_client: TerrainaHttpClient = data["http_client"]
    entity_map: dict[str, list] = data.setdefault("entities", {})

    entities = []
    for device in coordinator.data or []:
        sn = str(device["sn"])
        name = device.get("deviceName", f"TERRAINA {device['sn']}")
        model = device.get("modelName", "KDRM")

        select = TerrainarWorkingModeSelect(coordinator, entry, sn, name, model, http_client)
        entity_map.setdefault(sn, []).append(select)
        entities.append(select)

        rain_delay = TerrainaRainDelaySelect(coordinator, entry, sn, name, model, http_client)
        entity_map.setdefault(sn, []).append(rain_delay)
        entities.append(rain_delay)

    async_add_entities(entities)


class TerrainarWorkingModeSelect(CoordinatorEntity[TerrainaCoordinator], SelectEntity, RestoreEntity):
    """Select entity to read and change the mower's working mode (auto / manual)."""

    _attr_options = ["auto", "manual"]

    def __init__(
        self,
        coordinator: TerrainaCoordinator,
        entry: ConfigEntry,
        sn: str,
        device_name: str,
        model_name: str,
        http_client: TerrainaHttpClient,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._device_name = device_name
        self._model_name = model_name
        self._http_client = http_client
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{sn}_working_mode"
        self._attr_name = f"{device_name} Working Mode"
        self._attr_current_option: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            name=self._device_name,
            model=self._model_name.upper(),
            manufacturer="DCK / TERRAINA",
            serial_number=self._sn,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state in self._attr_options:
                self._attr_current_option = last.state

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def _extract_mode(self, state_dict: dict) -> str | None:
        info: dict = {}
        if "postDeviceDetail" in state_dict:
            info = state_dict["postDeviceDetail"].get("info") or {}
        elif "getDeviceDetail" in state_dict:
            data = state_dict["getDeviceDetail"].get("data") or {}
            info = data.get("info") or {}
        else:
            return None

        manual_mode_type = info.get("manualModeType")
        if manual_mode_type is not None:
            return "manual" if int(manual_mode_type) else "auto"

        raw = info.get("status")
        if raw is not None:
            try:
                return split_bits(int(raw)).get("working_mode", "auto")
            except (TypeError, ValueError):
                pass
        return None

    def update_from_grpc(self, state_dict: dict) -> None:
        mode = self._extract_mode(state_dict)
        if mode is None:
            return
        self._attr_current_option = mode
        _LOGGER.debug("Working mode for %s: %r", self._sn, mode)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        manual = 1 if option == "manual" else 0
        await self._http_client.set_work_mode(self._entry, self._sn, manual)
        self._attr_current_option = option
        self.async_write_ha_state()


_RAIN_DELAY_OPTIONS = ["1h", "2h", "3h"]
_RAIN_DELAY_MAP = {"1h": 60, "2h": 120, "3h": 180}
_RAIN_DELAY_REVERSE = {v: k for k, v in _RAIN_DELAY_MAP.items()}


class TerrainaRainDelaySelect(CoordinatorEntity[TerrainaCoordinator], SelectEntity, RestoreEntity):
    """Select entity for rain delay (1h / 2h / 3h after onboard sensor detects dry)."""

    _attr_options = _RAIN_DELAY_OPTIONS
    _attr_icon = "mdi:timer-sand"

    def __init__(
        self,
        coordinator: TerrainaCoordinator,
        entry: ConfigEntry,
        sn: str,
        device_name: str,
        model_name: str,
        http_client: TerrainaHttpClient,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._device_name = device_name
        self._model_name = model_name
        self._http_client = http_client
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{sn}_rain_delay"
        self._attr_name = f"{device_name} Rain Delay"
        self._attr_current_option: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            name=self._device_name,
            model=self._model_name.upper(),
            manufacturer="DCK / TERRAINA",
            serial_number=self._sn,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state in _RAIN_DELAY_OPTIONS:
                self._attr_current_option = last.state

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        settings: dict = {}
        if "getDeviceDetail" in state_dict:
            data = state_dict["getDeviceDetail"].get("data") or {}
            settings = data.get("settings") or {}
        elif "postDeviceDetail" in state_dict:
            settings = state_dict["postDeviceDetail"].get("settings") or {}
        else:
            return
        val = settings.get("rainDelay")
        if val is None:
            return
        option = _RAIN_DELAY_REVERSE.get(int(val))
        if option is None:
            _LOGGER.warning("Unknown rainDelay value %s for %s", val, self._sn)
            return
        self._attr_current_option = option
        _LOGGER.debug("Rain delay for %s: %s (%s min)", self._sn, option, val)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        minutes = _RAIN_DELAY_MAP[option]
        await self._http_client.set_rain_delay(self._entry, self._sn, minutes)
        self._attr_current_option = option
        self.async_write_ha_state()
