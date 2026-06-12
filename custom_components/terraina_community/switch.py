"""TERRAINA Community switch platform — schedule day enable/disable (slot 0)."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TerrainaCoordinator
from .httpClient import TerrainaHttpClient
from .sensor import _WEEK_DISPLAY_ORDER, _device_info, schedule_control_label

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

        for week in _WEEK_DISPLAY_ORDER:
            sw = TerrainaScheduleSwitch(coordinator, entry, sn, name, model, week, http_client)
            entity_map.setdefault(sn, []).append(sw)
            entities.append(sw)

    async_add_entities(entities)


class TerrainaScheduleSwitch(CoordinatorEntity[TerrainaCoordinator], SwitchEntity, RestoreEntity):
    """Toggle switch for enabling/disabling the primary (slot 0) mowing window on a weekday."""

    _attr_icon = "mdi:calendar-check"

    def __init__(
        self,
        coordinator: TerrainaCoordinator,
        entry: ConfigEntry,
        sn: str,
        device_name: str,
        model_name: str,
        week: int,
        http_client: TerrainaHttpClient,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._device_name = device_name
        self._model_name = model_name
        self._week = week
        self._http_client = http_client
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{sn}_schedule_enabled_{week}"
        self._attr_name = f"{device_name} {schedule_control_label(week)} Enabled"
        self._attr_is_on: bool | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._sn, self._device_name, self._model_name)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state == "on":
                self._attr_is_on = True
            elif last.state == "off":
                self._attr_is_on = False

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        if "getSchedule" not in state_dict:
            return
        days = _extract_day_slots(state_dict, self._week)
        if not days:
            return
        self._attr_is_on = bool(days[0].get("enable", 1))
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, enabled: bool) -> None:
        from .lawn_mower import TerrainaLawnMower
        from homeassistant.components.lawn_mower import LawnMowerActivity

        mower = _find_mower(self.hass, self._entry.entry_id, self._sn)
        if mower and mower._attr_activity in (LawnMowerActivity.MOWING, LawnMowerActivity.RETURNING):
            _LOGGER.warning("Cannot change schedule while mower is active")
            return

        current = list(mower._schedule) if mower else []
        day_slots = [d for d in current if d.get("week") == self._week]
        other = [d for d in current if d.get("week") != self._week]

        if day_slots:
            day_slots[0] = {**day_slots[0], "enable": 1 if enabled else 0}
        else:
            day_slots = [{"week": self._week, "enable": 1 if enabled else 0,
                          "startTime": 600, "endTime": 1200,
                          "mapId": 1, "boundaryId": -1, "regionId": -1, "needEdge": 1}]

        updated = sorted(other + day_slots, key=lambda d: d.get("week", 0))
        await self._http_client.set_schedule(self._entry, self._sn, updated)
        if mower:
            mower._schedule = updated
        self._attr_is_on = enabled
        self.async_write_ha_state()


def _extract_day_slots(state_dict: dict, week: int) -> list[dict]:
    data = state_dict.get("getSchedule", {}).get("data") or {}
    days = (data.get("globalSche") or {}).get("schedule") or []
    if isinstance(days, dict):
        days = [days]
    return [d for d in days if d.get("week") == week]


def _find_mower(hass, entry_id: str, sn: str):
    from .lawn_mower import TerrainaLawnMower
    entities = hass.data.get(DOMAIN, {}).get(entry_id, {}).get("entities", {}).get(sn, [])
    return next((e for e in entities if isinstance(e, TerrainaLawnMower)), None)
