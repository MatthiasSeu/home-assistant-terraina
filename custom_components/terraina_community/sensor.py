"""TERRAINA Community sensor platform — battery and working mode."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TerrainaCoordinator
from .grpc_util import split_bits

_LOGGER = logging.getLogger(__name__)

_POWER_TO_PCT = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: TerrainaCoordinator = data["coordinator"]
    entity_map: dict[str, list] = data.setdefault("entities", {})

    entities = []
    for device in coordinator.data or []:
        sn = str(device["sn"])
        name = device.get("deviceName", f"TERRAINA {device['sn']}")
        model = device.get("modelName", "KDRM")

        battery = TerrainaBatterySensor(coordinator, entry, sn, name, model)
        mode = TerrainarWorkingModeSensor(coordinator, entry, sn, name, model)

        entity_map.setdefault(sn, []).extend([battery, mode])
        entities.extend([battery, mode])

    async_add_entities(entities)


class _TerrainaSensorBase(CoordinatorEntity[TerrainaCoordinator], SensorEntity):
    def __init__(
        self,
        coordinator: TerrainaCoordinator,
        entry: ConfigEntry,
        sn: str,
        device_name: str,
        model_name: str,
        unique_suffix: str,
        sensor_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._device_name = device_name
        self._model_name = model_name
        self._attr_unique_id = f"{DOMAIN}_{sn}_{unique_suffix}"
        self._attr_name = sensor_name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            name=self._device_name,
            model=self._model_name.upper(),
            manufacturer="DCK / TERRAINA",
            serial_number=self._sn,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        raise NotImplementedError

    def _extract_info(self, state_dict: dict) -> dict:
        if "postDeviceDetail" in state_dict:
            return state_dict["postDeviceDetail"].get("info") or {}
        if "getDeviceDetail" in state_dict:
            data = state_dict["getDeviceDetail"].get("data") or {}
            return data.get("info") or {}
        return {}


class TerrainaBatterySensor(_TerrainaSensorBase):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, entry, sn, device_name, model_name) -> None:
        super().__init__(
            coordinator, entry, sn, device_name, model_name,
            "battery", f"{device_name} Battery",
        )
        self._attr_native_value: int | None = None

    def update_from_grpc(self, state_dict: dict) -> None:
        info = self._extract_info(state_dict)
        power = info.get("power")
        if power is None:
            return
        self._attr_native_value = _POWER_TO_PCT.get(int(power))
        _LOGGER.debug("Battery for %s: power=%r → %s%%", self._sn, power, self._attr_native_value)
        self.async_write_ha_state()


class TerrainarWorkingModeSensor(_TerrainaSensorBase):
    def __init__(self, coordinator, entry, sn, device_name, model_name) -> None:
        super().__init__(
            coordinator, entry, sn, device_name, model_name,
            "working_mode", f"{device_name} Working Mode",
        )
        self._attr_native_value: str | None = None

    def update_from_grpc(self, state_dict: dict) -> None:
        info = self._extract_info(state_dict)
        raw = info.get("status")
        manual_mode_type = info.get("manualModeType")

        # Prefer explicit manualModeType field; fall back to bit 13
        if manual_mode_type is not None:
            self._attr_native_value = "manual" if int(manual_mode_type) else "auto"
        elif raw is not None:
            try:
                bits = split_bits(int(raw))
                self._attr_native_value = bits.get("working_mode", "auto")
            except (TypeError, ValueError):
                return
        else:
            return

        _LOGGER.debug("Working mode for %s: %r", self._sn, self._attr_native_value)
        self.async_write_ha_state()
