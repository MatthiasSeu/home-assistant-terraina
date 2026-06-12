"""TERRAINA Community time platform — schedule start/end time per day (slot 0)."""

from __future__ import annotations

import logging
from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
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
from .switch import _extract_day_slots, _find_mower

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
            for is_end in (False, True):
                te = TerrainaScheduleTimeEntity(
                    coordinator, entry, sn, name, model, week, is_end, http_client
                )
                entity_map.setdefault(sn, []).append(te)
                entities.append(te)

    async_add_entities(entities)


class TerrainaScheduleTimeEntity(CoordinatorEntity[TerrainaCoordinator], TimeEntity, RestoreEntity):
    """Time picker for the primary (slot 0) mowing start or end time on a weekday."""

    def __init__(
        self,
        coordinator: TerrainaCoordinator,
        entry: ConfigEntry,
        sn: str,
        device_name: str,
        model_name: str,
        week: int,
        is_end: bool,
        http_client: TerrainaHttpClient,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._device_name = device_name
        self._model_name = model_name
        self._week = week
        self._is_end = is_end
        self._http_client = http_client
        self._entry = entry
        kind = "End" if is_end else "Start"
        uid_kind = "end" if is_end else "start"
        self._attr_unique_id = f"{DOMAIN}_{sn}_schedule_{uid_kind}_{week}"
        self._attr_name = f"{device_name} {schedule_control_label(week)} {kind}"
        self._attr_icon = "mdi:clock-end" if is_end else "mdi:clock-start"
        self._attr_native_value: dt_time | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._sn, self._device_name, self._model_name)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            try:
                h, m, *_ = last.state.split(":")
                self._attr_native_value = dt_time(int(h), int(m))
            except (ValueError, AttributeError):
                pass

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        if "getSchedule" not in state_dict:
            return
        day_slots = _extract_day_slots(state_dict, self._week)
        if not day_slots:
            return
        slot = day_slots[0]
        minutes = int(slot["endTime"] if self._is_end else slot["startTime"])
        self._attr_native_value = dt_time(minutes // 60, minutes % 60)
        self.async_write_ha_state()

    async def async_set_value(self, value: dt_time) -> None:
        from homeassistant.components.lawn_mower import LawnMowerActivity

        mower = _find_mower(self.hass, self._entry.entry_id, self._sn)
        if mower and mower._attr_activity in (LawnMowerActivity.MOWING, LawnMowerActivity.RETURNING):
            _LOGGER.warning("Cannot change schedule while mower is active")
            return

        minutes = value.hour * 60 + value.minute
        current = list(mower._schedule) if mower else []
        day_slots = [d for d in current if d.get("week") == self._week]
        other = [d for d in current if d.get("week") != self._week]

        if day_slots:
            field = "endTime" if self._is_end else "startTime"
            day_slots[0] = {**day_slots[0], field: minutes}
        else:
            day_slots = [{"week": self._week, "enable": 1,
                          "startTime": minutes if not self._is_end else 600,
                          "endTime": minutes if self._is_end else 1200,
                          "mapId": 1, "boundaryId": -1, "regionId": -1, "needEdge": 1}]

        updated = sorted(other + day_slots, key=lambda d: d.get("week", 0))
        await self._http_client.set_schedule(self._entry, self._sn, updated)
        if mower:
            mower._schedule = updated
        self._attr_native_value = value
        self.async_write_ha_state()
