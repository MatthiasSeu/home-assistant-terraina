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

from .const import DOMAIN, ERROR_CODES
from .coordinator import TerrainaCoordinator
from .grpc_util import split_bits

_LOGGER = logging.getLogger(__name__)

_POWER_TO_PCT = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}

# Maps the rained bit-field string to a user-friendly HA state
_RAIN_STATE = {
    "not rained":              "Dry",
    "being rained":            "Raining",
    "being rained and delayed": "Rain delay active",
}

# Protocol: 0=Sun, 1=Mon, …, 6=Sat.  Display order: Mon first.
_WEEKDAY_NAMES = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday"}
_DAY_ABBR      = {0: "Sun",    1: "Mon",    2: "Tue",     3: "Wed",       4: "Thu",      5: "Fri",    6: "Sat"}
# Mon=1 … Sat=6, Sun=7 → alphabetical sort of "N-Dayname" matches week order
_WEEK_DISPLAY_NUM = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 0: 7}
_WEEK_DISPLAY_ORDER = [1, 2, 3, 4, 5, 6, 0]   # Mon → … → Sat → Sun


def _fmt_min(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def schedule_day_label(week: int) -> str:
    """Full label for Sensors section: '1-Monday'."""
    return f"{_WEEK_DISPLAY_NUM[week]}-{_WEEKDAY_NAMES[week]}"


def schedule_control_label(week: int) -> str:
    """Short label for Controls section: '1-Mon' (fits in HA device card width)."""
    return f"{_WEEK_DISPLAY_NUM[week]}-{_DAY_ABBR[week]}"


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

        error = TerrainaErrorSensor(coordinator, entry, sn, name, model)
        entity_map.setdefault(sn, []).append(error)
        entities.append(error)

        rain = TerrainaRainSensor(coordinator, entry, sn, name, model)
        entity_map.setdefault(sn, []).append(rain)
        entities.append(rain)

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
    """Sensor showing the current cutting height in mm.

    aiHeight lives in getDeviceDetail.data.settings (not in .info).
    postDeviceDetail only carries rainEnable/rainDelay in its settings block.
    """

    _attr_icon = "mdi:ruler"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "mm"

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
        settings: dict = {}
        if "getDeviceDetail" in state_dict:
            data = state_dict["getDeviceDetail"].get("data") or {}
            settings = data.get("settings") or {}
        elif "postDeviceDetail" in state_dict:
            settings = state_dict["postDeviceDetail"].get("settings") or {}
        else:
            return

        height = settings.get("aiHeight")
        if height is None:
            return
        self._attr_native_value = int(height)
        _LOGGER.debug("Cutting height for %s: %s mm", self._sn, height)
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
        self._attr_name = f"{device_name} Schedule {schedule_day_label(week)}"
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
            self._sn, self._week, _WEEKDAY_NAMES[self._week],  # type: ignore[index]
            len(self._slots), self._attr_native_value,
        )
        self.async_write_ha_state()


class TerrainaErrorSensor(CoordinatorEntity[TerrainaCoordinator], RestoreSensor):
    """Sensor showing the current error code with human-readable description.

    State is None when no error is active (errorCode 0 or absent).
    State is 'E05 — Cutting motor error' when an error is present.
    """

    _attr_icon = "mdi:check-circle-outline"

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
        self._attr_unique_id = f"{DOMAIN}_{sn}_error_code"
        self._attr_name = f"{device_name} Error"
        self._attr_native_value: str | None = None
        self._has_error = False

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._sn, self._device_name, self._model_name)

    @property
    def icon(self) -> str:
        return "mdi:alert-circle" if self._has_error else "mdi:check-circle-outline"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
            self._has_error = (
                self._attr_native_value is not None
                and self._attr_native_value != "OK"
            )

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

        raw = info.get("errorCode")
        if raw is None:
            return

        try:
            code = int(raw)
        except (TypeError, ValueError):
            return

        if code == 0:
            self._has_error = False
            self._attr_native_value = "OK"
        else:
            self._has_error = True
            desc = ERROR_CODES.get(code, "Unknown error")
            self._attr_native_value = f"E{code:02d} — {desc}"

        _LOGGER.debug("Error code for %s: %r → %s", self._sn, code, self._attr_native_value)
        self.async_write_ha_state()


class TerrainaRainSensor(CoordinatorEntity[TerrainaCoordinator], RestoreSensor):
    """Sensor showing the current rain detection state.

    Decoded from bits 4-5 of the device status integer (same field as working_status).
    States: 'Dry', 'Raining', 'Rain delay active'.
    """

    _attr_icon = "mdi:weather-rainy"

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
        self._attr_unique_id = f"{DOMAIN}_{sn}_rain_status"
        self._attr_name = f"{device_name} Rain Status"
        self._attr_native_value: str | None = None

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

        raw = info.get("status")
        if raw is None:
            return

        try:
            bits = split_bits(int(raw))
        except (TypeError, ValueError):
            return

        rained = bits.get("rained", "")
        self._attr_native_value = _RAIN_STATE.get(rained, rained) or None
        _LOGGER.debug("Rain status for %s: %r → %s", self._sn, rained, self._attr_native_value)
        self.async_write_ha_state()
