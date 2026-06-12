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

# Protocol: 0=Sun, 1=Mon, …, 6=Sat.  Display order: Mon first.
_WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_WEEK_DISPLAY_ORDER = [1, 2, 3, 4, 5, 6, 0]   # Mon → … → Sat → Sun


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

        height = TerrainaCuttingHeightSensor(coordinator, entry, sn, name, model)
        entity_map.setdefault(sn, []).append(height)
        entities.append(height)

        # Create one sensor per weekday, Monday first
        for week in _WEEK_DISPLAY_ORDER:
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


class TerrainaCuttingHeightSensor(CoordinatorEntity[TerrainaCoordinator], RestoreSensor):
    """Sensor showing the current AI cutting height level."""

    _attr_icon = "mdi:ruler"
    _attr_state_class = SensorStateClass.MEASUREMENT

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
        self._attr_unique_id = f"{DOMAIN}_{sn}_cutting_height"
        self._attr_name = f"{device_name} Cutting Height"
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

        height = info.get("aiHeight")
        if height is None:
            return
        self._attr_native_value = int(height)
        _LOGGER.debug("Cutting height for %s: %s", self._sn, height)
        self.async_write_ha_state()


class TerrainaScheduleSensor(CoordinatorEntity[TerrainaCoordinator], RestoreSensor):
    """One sensor per weekday — supports multiple time slots per day.

    State shows all slots separated by ' / '.
    Each slot can be individually enabled or disabled.
    Example states:
      "10:00 - 20:30"                        (1 slot, enabled)
      "10:00 - 20:30 (off)"                  (1 slot, disabled)
      "08:00 - 12:00 / 15:00 - 20:00"        (2 slots, both on)
      "08:00 - 12:00 / 15:00 - 20:00 (off)"  (2 slots, second off)
    """

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
        # unique_id uses the protocol week number (0=Sun … 6=Sat)
        self._attr_unique_id = f"{DOMAIN}_{sn}_schedule_{week}"
        # Name: device name is the prefix — entity_id will be sensor.<device>_schedule_<day>
        self._attr_name = f"{device_name} Schedule {_WEEKDAY_NAMES[week]}"
        self._attr_native_value: str | None = None
        self._slots: list[dict] = []

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._sn, self._device_name, self._model_name)

    @property
    def extra_state_attributes(self) -> dict:
        if not self._slots:
            return {}
        if len(self._slots) == 1:
            # Flat attributes for the single-slot case (easy to use in automations)
            return {
                "start_time": self._slots[0]["start_time"],
                "end_time": self._slots[0]["end_time"],
                "enabled": self._slots[0]["enabled"],
            }
        # Multiple slots: list with per-slot details
        return {"slots": self._slots}

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

        # Collect ALL slots for this weekday (protocol may carry multiple)
        day_slots = [d for d in days if d.get("week") == self._week]
        if not day_slots:
            return

        self._slots = [
            {
                "start_time": _fmt_min(int(s["startTime"])),
                "end_time": _fmt_min(int(s["endTime"])),
                "enabled": bool(s.get("enable", 1)),
            }
            for s in day_slots
        ]

        # Build human-readable state string
        parts = []
        for slot in self._slots:
            part = f"{slot['start_time']} - {slot['end_time']}"
            if not slot["enabled"]:
                part += " (off)"
            parts.append(part)
        self._attr_native_value = " / ".join(parts)

        _LOGGER.debug(
            "Schedule %s week=%d (%s): %d slot(s): %s",
            self._sn, self._week, _WEEKDAY_NAMES[self._week],
            len(self._slots), self._attr_native_value,
        )
        self.async_write_ha_state()
