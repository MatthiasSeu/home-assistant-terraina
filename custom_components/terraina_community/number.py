"""TERRAINA Community number platform — editable cutting height."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TerrainaCoordinator
from .httpClient import TerrainaHttpClient
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)

# EU range per KDRM210/220 manual (AU/NZ: 20-70 mm)
_HEIGHT_MIN_EU = 30
_HEIGHT_MAX_EU = 80
_HEIGHT_STEP = 5


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

        num = TerrainaCuttingHeightNumber(coordinator, entry, sn, name, model, http_client)
        entity_map.setdefault(sn, []).append(num)
        entities.append(num)

    async_add_entities(entities)


class TerrainaCuttingHeightNumber(CoordinatorEntity[TerrainaCoordinator], NumberEntity, RestoreEntity):
    """Slider for global cutting height (applies to all zones).

    Value source: getDeviceDetail.data.settings.aiHeight (mm).
    Write: setAiHeight command via REST API.
    EU range: 30-80 mm in 5 mm steps.
    """

    _attr_icon = "mdi:ruler"
    _attr_native_min_value = _HEIGHT_MIN_EU
    _attr_native_max_value = _HEIGHT_MAX_EU
    _attr_native_step = _HEIGHT_STEP
    _attr_native_unit_of_measurement = "mm"
    _attr_mode = NumberMode.SLIDER

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
        self._attr_unique_id = f"{DOMAIN}_{sn}_cutting_height_number"
        self._attr_name = f"{device_name} Cutting Height"
        self._attr_native_value: float | None = None

    @property
    def device_info(self):
        return _device_info(self._sn, self._device_name, self._model_name)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last.state)
            except (TypeError, ValueError):
                pass

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        info: dict = {}
        settings: dict = {}
        if "getDeviceDetail" in state_dict:
            data = state_dict["getDeviceDetail"].get("data") or {}
            info = data.get("info") or {}
            settings = data.get("settings") or {}
        elif "postDeviceDetail" in state_dict:
            info = state_dict["postDeviceDetail"].get("info") or {}
            settings = state_dict["postDeviceDetail"].get("settings") or {}
        else:
            return

        # aiHeight lives in info (not settings) in postDeviceDetail responses
        height = info.get("aiHeight") if "aiHeight" in info else settings.get("aiHeight")
        if height is None:
            return
        self._attr_native_value = float(int(height))
        _LOGGER.debug("Cutting height (slider) for %s: %s mm", self._sn, height)
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        height = int(value)
        await self._http_client.set_ai_height(self._entry, self._sn, height)
        self._attr_native_value = float(height)
        self.async_write_ha_state()
        _LOGGER.debug("Set cutting height for %s → %d mm", self._sn, height)
