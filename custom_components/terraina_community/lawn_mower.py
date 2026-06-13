"""TERRAINA Community lawn_mower platform."""

from __future__ import annotations

import logging

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, WORKING_STATUS
from .coordinator import TerrainaCoordinator
from .grpc_util import split_bits
from .httpClient import TerrainaHttpClient

_LOGGER = logging.getLogger(__name__)

_WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _fmt_min(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TERRAINA Community lawn_mower entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: TerrainaCoordinator = data["coordinator"]
    http_client: TerrainaHttpClient = data["http_client"]

    entity_map = hass.data[DOMAIN][entry.entry_id].setdefault("entities", {})
    entities = []
    for device in (coordinator.data or []):
        sn = str(device["sn"])
        entity = TerrainaLawnMower(
            coordinator=coordinator,
            entry=entry,
            sn=sn,
            device_name=device.get("deviceName", f"TERRAINA {device['sn']}"),
            model_name=device.get("modelName", "KDRM"),
            http_client=http_client,
        )
        entity_map.setdefault(sn, []).append(entity)
        entities.append(entity)
    async_add_entities(entities)


class TerrainaLawnMower(CoordinatorEntity[TerrainaCoordinator], LawnMowerEntity, RestoreEntity):
    """Represents a TERRAINA lawn mower.

    State is tracked optimistically (updated immediately on command) because
    the device-state stream endpoint is not yet known (Phase 1a).
    Phase 1b will add gRPC-based real state polling once the endpoint is found.
    """

    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.DOCK
        | LawnMowerEntityFeature.PAUSE
    )

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
        self._attr_unique_id = f"{DOMAIN}_{sn}"
        self._attr_name = device_name
        self._attr_activity: LawnMowerActivity | None = None
        self._schedule: list[dict] = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            for activity in LawnMowerActivity:
                if activity.value == last.state:
                    self._attr_activity = activity
                    break

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
        """Update availability from coordinator poll; preserve gRPC-sourced activity."""
        devices: list[dict] = self.coordinator.data or []
        self._attr_available = any(str(d["sn"]) == self._sn for d in devices)
        self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        """Called by TerrainaGrpcStream with decoded device state."""
        info: dict = {}
        if "postDeviceDetail" in state_dict:
            # push notification: {'postDeviceDetail': {'info': {...}}}
            info = state_dict["postDeviceDetail"].get("info") or {}
        elif "getDeviceDetail" in state_dict:
            # poll response: {'getDeviceDetail': {'data': {'info': {...}}}}
            data = state_dict["getDeviceDetail"].get("data") or {}
            info = data.get("info") or {}

        self._update_schedule(state_dict)

        raw = info.get("status") or state_dict.get("status") or state_dict.get("workStatus")
        if raw is None:
            # may still have getPath data — update attributes without state change
            self._update_path_attrs(state_dict)
            return
        try:
            bits = split_bits(int(raw))
        except (TypeError, ValueError):
            return

        ws = bits.get("working_status", "")
        charging = bits.get("charging", "")
        power = info.get("power", 0)

        if ws in ("mowing", "leaving basestation", "building graph", "locating"):
            self._attr_activity = LawnMowerActivity.MOWING
        elif ws in ("backing", "backing with low power", "completing"):
            self._attr_activity = LawnMowerActivity.RETURNING
        elif ws == "resting":
            self._attr_activity = LawnMowerActivity.PAUSED
        elif ws == "error":
            self._attr_activity = LawnMowerActivity.ERROR
        elif ws == "offline":
            self._attr_available = False
            self._attr_activity = None
        elif ws in ("hanging", "") and charging in ("charging", "fully charged"):
            self._attr_activity = LawnMowerActivity.DOCKED
        elif ws == "hanging":
            self._attr_activity = LawnMowerActivity.DOCKED
        else:
            self._attr_activity = None

        self._extra: dict = {
            "working_status": ws,
            "charging": charging,
            "power": power,
            "working_mode": info.get("manualModeType", 0),
            "ai_height": info.get("aiHeight"),
            "error_code": info.get("errorCode"),
        }
        self._update_path_attrs(state_dict)

        _LOGGER.debug(
            "gRPC state update for %s: ws=%r charging=%r power=%r → %s",
            self._sn, ws, charging, power, self._attr_activity,
        )
        self.async_write_ha_state()

    def _update_schedule(self, state_dict: dict) -> None:
        if "getSchedule" not in state_dict:
            return
        data = state_dict["getSchedule"].get("data") or {}
        global_sche = data.get("globalSche") or {}
        days = global_sche.get("schedule") or []
        if isinstance(days, dict):
            days = [days]
        if days:
            self._schedule = sorted(days, key=lambda d: d.get("week", 0))

    def _update_path_attrs(self, state_dict: dict) -> None:
        if "getPath" not in state_dict:
            return
        path = (state_dict["getPath"].get("data") or {})
        extra = getattr(self, "_extra", {})
        extra.update({
            "progress": path.get("progress"),
            "remain_time_min": path.get("remainTime"),
            "total_area_sqm": path.get("totalArea"),
            "position": path.get("local"),
        })
        self._extra = extra

    @property
    def extra_state_attributes(self) -> dict:
        attrs = dict(getattr(self, "_extra", {}))
        if self._schedule:
            attrs["schedule"] = {
                _WEEKDAY_NAMES[d["week"]]: (
                    f"{_fmt_min(d['startTime'])} - {_fmt_min(d['endTime'])}"
                    + ("" if d.get("enable") else " (disabled)")
                )
                for d in self._schedule
            }
        return attrs

    # ------------------------------------------------------------------
    # Commands — optimistic state update while gRPC state is unavailable
    # ------------------------------------------------------------------

    async def async_start_mowing(self) -> None:
        """Start mowing (setWorkStatus 3)."""
        await self._http_client.set_work_status(
            self._entry, self._sn, WORKING_STATUS.index("mowing")
        )
        self._attr_activity = LawnMowerActivity.MOWING
        self.async_write_ha_state()

    async def async_dock(self) -> None:
        """Send mower back to dock (setWorkStatus 4 = backing)."""
        await self._http_client.set_work_status(
            self._entry, self._sn, WORKING_STATUS.index("backing")
        )
        self._attr_activity = LawnMowerActivity.RETURNING
        self.async_write_ha_state()

    async def async_pause(self) -> None:
        """Pause mowing (setWorkStatus 7 = resting)."""
        await self._http_client.set_work_status(
            self._entry, self._sn, WORKING_STATUS.index("resting")
        )
        self._attr_activity = LawnMowerActivity.PAUSED
        self.async_write_ha_state()
