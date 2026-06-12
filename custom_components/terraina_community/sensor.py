"""TERRAINA Community sensor platform — battery + weekly schedule."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
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

_LOGGER = logging.getLogger(__name__)

_POWER_TO_PCT = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}

_WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _fmt_min(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


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
        entity_map.setdefault(sn, []).append(battery)
        entities.append(battery)

        for week in range(7):
            sched = TerrainaScheduleSensor(coordinator, entry, sn, name, model, week)
            entity_map.setdefault(sn, []).append(sched)
            entities.append(sched)

    async_add_entities(entities)


def _device_info(sn: str, device_name: str, model_name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=device_name,
        model=model_name.upper(),
        manufacturer="DCK / TERRAINA",
        serial_number=sn,
    )


class TerrainaBatterySensor(CoordinatorEntity[TerrainaCoordinator], RestoreSensor):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self,
        coordinator: TerrainaCoordinator,
        entry: ConfigEntry,
        sn: str,
        device_name: str,
        model_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._device_name = device_name
        self._model_name = model_name
        self._attr_unique_id = f"{DOMAIN}_{sn}_battery"
        self._attr_name = f"{device_name} Battery"
        self._attr_native_value: int | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._sn, self._device_name, self._model_name)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        info: dict = {}
        if "postDeviceDetail" in state_dict:
            info = state_dict["postDeviceDetail"].get("info") or {}
        elif "getDeviceDetail" in state_dict:
            data = state_dict["getDeviceDetail"].get("data") or {}
            info = data.get("info") or {}
        else:
            return

        power = info.get("power")
        if power is None:
            return
        self._attr_native_value = _POWER_TO_PCT.get(int(power))
        _LOGGER.debug("Battery for %s: power=%r → %s%%", self._sn, power, self._attr_native_value)
        self.async_write_ha_state()


class TerrainaScheduleSensor(CoordinatorEntity[TerrainaCoordinator], RestoreSensor):
    """One sensor per weekday showing the mowing time window for that day."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: TerrainaCoordinator,
        entry: ConfigEntry,
        sn: str,
        device_name: str,
        model_name: str,
        week: int,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._device_name = device_name
        self._model_name = model_name
        self._week = week
        self._attr_unique_id = f"{DOMAIN}_{sn}_schedule_{week}"
        self._attr_name = f"{device_name} Schedule {_WEEKDAY_NAMES[week]}"
        self._attr_native_value: str | None = None
        self._start_time: str | None = None
        self._end_time: str | None = None
        self._enabled: bool | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._sn, self._device_name, self._model_name)

    @property
    def extra_state_attributes(self) -> dict:
        if self._start_time is None:
            return {}
        return {
            "start_time": self._start_time,
            "end_time": self._end_time,
            "enabled": self._enabled,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        if "getSchedule" not in state_dict:
            return
        data = state_dict["getSchedule"].get("data") or {}
        global_sche = data.get("globalSche") or {}
        days = global_sche.get("schedule") or []
        if isinstance(days, dict):
            days = [days]

        day = next((d for d in days if d.get("week") == self._week), None)
        if day is None:
            return

        enabled = bool(day.get("enable", 1))
        start = _fmt_min(int(day["startTime"]))
        end = _fmt_min(int(day["endTime"]))

        self._enabled = enabled
        self._start_time = start
        self._end_time = end
        self._attr_native_value = f"{start} - {end}" if enabled else f"{start} - {end} (off)"

        _LOGGER.debug(
            "Schedule for %s week=%d (%s): %s-%s enabled=%s",
            self._sn, self._week, _WEEKDAY_NAMES[self._week], start, end, enabled,
        )
        self.async_write_ha_state()
