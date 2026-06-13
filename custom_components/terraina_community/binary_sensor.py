"""TERRAINA Community binary sensor — safe to mow."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TerrainaCoordinator
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)


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

        sensor = TerrainaSafeToMowBinarySensor(coordinator, entry, sn, name, model)
        entity_map.setdefault(sn, []).append(sensor)
        entities.append(sensor)
        # SmartProtectionManager reads this reference after platform setup
        data.setdefault("safe_to_mow_sensors", {})[sn] = sensor

    async_add_entities(entities)


class TerrainaSafeToMowBinarySensor(
    CoordinatorEntity[TerrainaCoordinator], BinarySensorEntity, RestoreEntity
):
    """True = safe to mow; False = blocked by rain hold, forecast, or high temperature.

    State is Unknown when no external sensors are configured — in that case the
    mower's built-in rain sensor is the only active protection.
    rain_hold_until is persisted in extra_state_attributes so it survives HA restarts.
    """

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
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{sn}_safe_to_mow"
        self._attr_name = f"{device_name} Safe to Mow"
        self._attr_is_on: bool | None = None
        self._blocked_reason: str = ""
        self._rain_hold_until: datetime | None = None

    @property
    def icon(self) -> str:
        if self._attr_is_on is None:
            return "mdi:shield-question"
        return "mdi:shield-check" if self._attr_is_on else "mdi:shield-off"

    @property
    def device_info(self):
        return _device_info(self._sn, self._device_name, self._model_name)

    @property
    def extra_state_attributes(self) -> dict:
        opts = self._entry.options
        return {
            "blocked_reason": self._blocked_reason,
            "rain_hold_until": self._rain_hold_until.isoformat() if self._rain_hold_until else None,
            "precipitation_sensor": opts.get("precipitation_sensor") or None,
            "rain_hold_base_hours": opts.get("rain_hold_base_hours", 0),
            "rain_factor_mm": opts.get("rain_factor_mm", 5),
            "rain_hold_step_hours": opts.get("rain_hold_step_hours", 1),
            "rain_hold_max_hours": opts.get("rain_hold_max_hours", 24),
            "forecast_entity": opts.get("forecast_entity") or None,
            "forecast_hours_ahead": opts.get("forecast_hours_ahead", 2),
            "temperature_sensor": opts.get("temperature_sensor") or None,
            "max_temperature": opts.get("max_temperature", 32),
            "auto_dock_unsafe": opts.get("auto_dock_unsafe", False),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            hold_str = (last.attributes or {}).get("rain_hold_until")
            if hold_str:
                try:
                    self._rain_hold_until = datetime.fromisoformat(hold_str)
                    _LOGGER.debug(
                        "Restored rain_hold_until=%s for %s", hold_str, self._sn
                    )
                except (TypeError, ValueError):
                    pass

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def set_safe_state(
        self, is_safe: bool, reason: str, rain_hold_until: datetime | None
    ) -> None:
        """Called by SmartProtectionManager when protection state changes."""
        self._attr_is_on = is_safe
        self._blocked_reason = reason
        self._rain_hold_until = rain_hold_until
        if self.hass:
            self.async_write_ha_state()

    def update_from_grpc(self, state_dict: dict) -> None:
        """No-op — state is managed by SmartProtectionManager, not gRPC updates."""
