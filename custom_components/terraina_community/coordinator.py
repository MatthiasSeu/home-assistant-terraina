"""DataUpdateCoordinator for TERRAINA Community integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .httpClient import RefreshFailedException, TerrainaHttpClient

_LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = timedelta(seconds=30)


class TerrainaCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls getUserBindDevices every 30 s to keep device availability fresh."""

    def __init__(
        self,
        hass: HomeAssistant,
        http_client: TerrainaHttpClient,
        config_entry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="terraina_community",
            update_interval=UPDATE_INTERVAL,
        )
        self._http_client = http_client
        self._config_entry = config_entry

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self._http_client.get_serial_numbers(self._config_entry)
        except RefreshFailedException as err:
            raise UpdateFailed(f"Token refresh failed: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Failed to update device list: {err}") from err
