"""TERRAINA Community lawn_mower platform."""

from __future__ import annotations

import logging
import os

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
from .map_renderer import (
    ZONE_COLORS,
    bitmap_diff,
    decode_path_value,
    render_map_png,
)

_LOGGER = logging.getLogger(__name__)

_WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_MAP_SUBDIR = "terraina_community"


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
    """Represents a TERRAINA lawn mower."""

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
        self._extra: dict = {}

        # Manual/zone mode tracking (from getDeviceDetail)
        self._manual_mode_type: int = 0

        # Map / path state
        self._current_bitmap: bytes | None = None   # latest full mowed-area bitmap
        self._map_w: int = 912
        self._map_h: int = 705
        self._current_pos: tuple[int, int, float] | None = None

        # Zone session tracking
        self._zone_sessions: list[tuple[bytes, tuple[int, int, int]]] = []
        self._zone_session_active: bool = False
        self._zone_session_snapshot: bytes | None = None  # bitmap at session start

        # HA www URL for the map image
        self._map_url: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            for activity in LawnMowerActivity:
                if activity.value == last.state:
                    self._attr_activity = activity
                    break
            # Restore map URL if the file still exists
            saved_url = (last.attributes or {}).get("map_url")
            if saved_url and self.hass:
                map_path = self.hass.config.path("www", _MAP_SUBDIR, f"map_{self._sn}.png")
                if os.path.isfile(map_path):
                    self._map_url = saved_url

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
            info = state_dict["postDeviceDetail"].get("info") or {}
        elif "getDeviceDetail" in state_dict:
            data = state_dict["getDeviceDetail"].get("data") or {}
            info = data.get("info") or {}

        # Track manualModeType — only present in getDeviceDetail responses
        if "manualModeType" in info:
            self._manual_mode_type = info["manualModeType"]

        self._update_schedule(state_dict)
        self._update_map_cloud(state_dict)

        raw = info.get("status") or state_dict.get("status") or state_dict.get("workStatus")
        if raw is None:
            self._update_path(state_dict)
            return
        try:
            bits = split_bits(int(raw))
        except (TypeError, ValueError):
            return

        ws = bits.get("working_status", "")
        charging = bits.get("charging", "")
        power = info.get("power", 0)

        prev_activity = self._attr_activity

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

        # Zone session: start when zone mow begins
        if (
            ws == "leaving basestation"
            and self._manual_mode_type == 1
            and not self._zone_session_active
        ):
            self._zone_session_active = True
            self._zone_session_snapshot = bytes(self._current_bitmap) if self._current_bitmap else None
            _LOGGER.debug("Zone session started for %s", self._sn)

        # Zone session: end when mower docks after zone mow
        if self._zone_session_active and self._attr_activity == LawnMowerActivity.DOCKED:
            self._finish_zone_session()

        self._extra = {
            "working_status": ws,
            "charging": charging,
            "power": power,
            "working_mode": self._manual_mode_type,
            "ai_height": info.get("aiHeight"),
            "error_code": info.get("errorCode"),
        }
        self._update_path(state_dict)

        _LOGGER.debug(
            "gRPC state update for %s: ws=%r charging=%r power=%r → %s",
            self._sn, ws, charging, power, self._attr_activity,
        )
        self.async_write_ha_state()

    def _finish_zone_session(self) -> None:
        """Save the completed zone session and assign it a colour."""
        self._zone_session_active = False
        if self._zone_session_snapshot is not None and self._current_bitmap is not None:
            diff = bitmap_diff(self._zone_session_snapshot, self._current_bitmap)
            color = ZONE_COLORS[len(self._zone_sessions) % len(ZONE_COLORS)]
            self._zone_sessions.append((diff, color))
            _LOGGER.debug(
                "Zone session ended for %s — session #%d, color=%r",
                self._sn, len(self._zone_sessions), color,
            )
        self._zone_session_snapshot = None

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

    def _update_map_cloud(self, state_dict: dict) -> None:
        """Handle cloud map responses."""
        if "getMulMapVersion" in state_dict:
            _LOGGER.debug(
                "getMulMapVersion for %s: %s",
                self._sn, state_dict["getMulMapVersion"],
            )
        if "getMulMapData" in state_dict:
            _LOGGER.debug(
                "getMulMapData for %s: %s",
                self._sn, state_dict["getMulMapData"],
            )
        if "getMulBoundary" in state_dict:
            _LOGGER.debug(
                "getMulBoundary for %s: %s",
                self._sn, state_dict["getMulBoundary"],
            )
        if "getMapConfig" in state_dict:
            _LOGGER.debug(
                "getMapConfig for %s: %s",
                self._sn, state_dict["getMapConfig"],
            )

    def _update_path(self, state_dict: dict) -> None:
        """Process getPath data: update bitmap, position, and schedule map write."""
        if "getPath" not in state_dict:
            return
        path = (state_dict["getPath"].get("data") or {})

        # Basic path attributes (always update)
        self._extra.update({
            "progress": path.get("progress"),
            "remain_time_min": path.get("remainTime"),
            "total_area_sqm": path.get("totalArea"),
            "position": path.get("local"),
        })

        # Parse position
        local_str = path.get("local", "")
        if local_str:
            try:
                parts = [float(v) for v in local_str.split(",")]
                if len(parts) >= 2:
                    self._current_pos = (int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else 0.0)
            except (ValueError, IndexError):
                pass

        # Only update bitmap when path type=2 (active mowing with trace)
        value = path.get("value")
        if not isinstance(value, bytes) or len(value) < 20:
            return
        decoded = decode_path_value(value)
        if decoded is None:
            return
        w, h, bitmap = decoded
        self._map_w = w
        self._map_h = h
        self._current_bitmap = bitmap

        # Schedule async map render + file write
        if self.hass:
            self.hass.async_create_task(self._async_write_map())

    async def _async_write_map(self) -> None:
        """Render current path map to a PNG and write it to www/."""
        if not self.hass or self._current_bitmap is None:
            return

        bitmap = self._current_bitmap
        pos = self._current_pos
        past = list(self._zone_sessions)
        w, h = self._map_w, self._map_h

        # Build current-session diff (what's new since session start)
        if self._zone_session_active and self._zone_session_snapshot is not None:
            current_diff = bitmap_diff(self._zone_session_snapshot, bitmap)
            current_color = ZONE_COLORS[len(past) % len(ZONE_COLORS)]
        elif not self._zone_session_active and not past:
            # No session tracking yet — show everything in the first zone colour
            current_diff = bitmap
            current_color = ZONE_COLORS[0]
        else:
            current_diff = None
            current_color = ZONE_COLORS[0]

        try:
            png = await self.hass.async_add_executor_job(
                render_map_png, past, current_diff, current_color, w, h, pos
            )
        except Exception as exc:
            _LOGGER.debug("Map render failed for %s: %s", self._sn, exc)
            return

        www_dir = self.hass.config.path("www", _MAP_SUBDIR)
        map_path = os.path.join(www_dir, f"map_{self._sn}.png")
        try:
            await self.hass.async_add_executor_job(self._write_file, www_dir, map_path, png)
            self._map_url = f"/local/{_MAP_SUBDIR}/map_{self._sn}.png"
        except OSError as exc:
            _LOGGER.debug("Map write failed for %s: %s", self._sn, exc)

    @staticmethod
    def _write_file(directory: str, path: str, data: bytes) -> None:
        os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    @property
    def extra_state_attributes(self) -> dict:
        attrs = dict(self._extra)
        if self._map_url:
            attrs["map_url"] = self._map_url
        if self._current_pos:
            attrs["map_x"] = self._current_pos[0]
            attrs["map_y"] = self._current_pos[1]
            attrs["map_angle_rad"] = round(self._current_pos[2], 4)
        if self._zone_sessions:
            attrs["zone_sessions"] = len(self._zone_sessions)
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
    # Commands
    # ------------------------------------------------------------------

    async def async_start_mowing(self) -> None:
        await self._http_client.set_work_status(
            self._entry, self._sn, WORKING_STATUS.index("mowing")
        )
        self._attr_activity = LawnMowerActivity.MOWING
        self.async_write_ha_state()

    async def async_dock(self) -> None:
        await self._http_client.set_work_status(
            self._entry, self._sn, WORKING_STATUS.index("backing")
        )
        self._attr_activity = LawnMowerActivity.RETURNING
        self.async_write_ha_state()

    async def async_pause(self) -> None:
        await self._http_client.set_work_status(
            self._entry, self._sn, WORKING_STATUS.index("resting")
        )
        self._attr_activity = LawnMowerActivity.PAUSED
        self.async_write_ha_state()
