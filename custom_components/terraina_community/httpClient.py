"""HTTP client for TERRAINA Community integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientTimeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CLIENT_ID, CLIENT_SECRET, PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET, SERVER_DOMAIN_NAME
from .grpc_util import MessageBuilder
from .util import get_standard_headers

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=10)


class RefreshFailedException(HomeAssistantError):
    """Raised when OAuth2 token refresh fails."""


def _is_valid_token(token: dict[str, Any]) -> bool:
    return "access_token" in token and "refresh_token" in token


def _bearer_headers(token: dict[str, Any]) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token['access_token']}",
    }


class TerrainaHttpClient:
    """HTTP client for all Dongcheng IoT REST API calls."""

    def __init__(
        self,
        hass: HomeAssistant,
        base_url: str,
        session: aiohttp.ClientSession,
        region: str = "eu",
    ) -> None:
        self._base_url = base_url
        self._region = region
        self._token_url = (
            f"{SERVER_DOMAIN_NAME[region]}/user-center/oauth2/token"
            if region in SERVER_DOMAIN_NAME
            else ""
        )
        self._session = session
        self._hass = hass
        self._message_builder = MessageBuilder()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_countries(self) -> dict[str, str]:
        """Return country → serRegion mapping (no auth required)."""
        url = f"{self._base_url}/iot-provision/app/getRegionInfo"
        try:
            async with self._session.get(
                url, headers={"Accept-Language": "en"}, timeout=_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()
                return {item["name"]: item["serRegion"] for item in result["data"]}
        except ClientError as err:
            raise HomeAssistantError(f"Failed to get countries: {err}") from err
        except TimeoutError as err:
            raise HomeAssistantError("Timeout connecting to TERRAINA server") from err

    async def get_serial_numbers(self, config_entry: ConfigEntry) -> list[dict]:
        """Return list of bound device dicts (sn, deviceName, modelName, …)."""
        return await self._api_post(
            config_entry,
            "/smarthome/device/getUserBindDevices",
            {},
            data_key="deviceList",
        )

    async def set_work_status(
        self, config_entry: ConfigEntry, sn: str, status: int
    ) -> None:
        """Send a setWorkStatus command to the device."""
        payload_b64 = self._message_builder.build_change_status_message_payload(sn, status)
        await self._send_message(config_entry, sn, payload_b64)

    async def set_work_mode(
        self, config_entry: ConfigEntry, sn: str, manual_mode_type: int
    ) -> None:
        """Send a setWorkMode command (0=auto, 1=manual)."""
        payload_b64 = self._message_builder.build_change_working_mode_payload(sn, manual_mode_type)
        await self._send_message(config_entry, sn, payload_b64)

    async def set_schedule(
        self, config_entry: ConfigEntry, sn: str, schedule_days: list[dict]
    ) -> None:
        """Send the full mowing schedule to the device."""
        payload_b64 = self._message_builder.build_set_schedule_payload(sn, schedule_days)
        await self._send_message(config_entry, sn, payload_b64)

    async def set_rain_enable(
        self, config_entry: ConfigEntry, sn: str, enabled: int
    ) -> None:
        """Send setRainEnable command (enabled: 0 or 1)."""
        payload_b64 = self._message_builder.build_set_rain_enable_payload(sn, enabled)
        await self._send_message(config_entry, sn, payload_b64)

    async def set_rain_delay(
        self, config_entry: ConfigEntry, sn: str, minutes: int
    ) -> None:
        """Send setRainDelay command (minutes: 60, 120, or 180)."""
        payload_b64 = self._message_builder.build_set_rain_delay_payload(sn, minutes)
        await self._send_message(config_entry, sn, payload_b64)

    async def set_ai_height(
        self, config_entry: ConfigEntry, sn: str, height: int
    ) -> None:
        """Send setAiHeight command (height in mm, EU: 30-80)."""
        payload_b64 = self._message_builder.build_set_ai_height_payload(sn, height)
        await self._send_message(config_entry, sn, payload_b64)

    async def go_home(self, config_entry: ConfigEntry, serial_number: str) -> None:
        """Send the mower back to its charging station (backing = index 4)."""
        from .const import WORKING_STATUS
        await self.set_work_status(
            config_entry, serial_number, WORKING_STATUS.index("backing")
        )

    async def probe_map_rest(
        self,
        config_entry: ConfigEntry,
        sn: str,
        map_version: int,
        boundary_version: int = 0,
    ) -> None:
        """Probe REST endpoints to discover cloud map/boundary data.

        v1.3.18 confirmed: every /iot-map/ endpoint name returns 403 with just
        the ory_at_ Bearer — auth is the blocker, not the endpoint name.

        v1.3.19: try 4 authentication header strategies:
          A) ory_at_ Bearer only              (confirmed 403)
          B) app_token (kk5fd5Ce) as Bearer
          C) ory_at_ Bearer + x-api-key: app_token
          D) full gRPC-style: ory_at_ + x-api-key + HMAC platform headers
        """
        ory_at = (config_entry.data.get("token") or {}).get("access_token", "")
        app_token = (config_entry.data.get("app_token") or {}).get("access_token", "")

        base_ct = {"Content-Type": "application/json"}
        platform_sig = get_standard_headers(PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET) or {}

        auth_variants: list[tuple[str, dict]] = [
            ("ory_only",   {**base_ct, "Authorization": f"Bearer {ory_at}"}),
            ("app_bearer", {**base_ct, "Authorization": f"Bearer {app_token}"}),
            ("ory+apikey", {**base_ct, "Authorization": f"Bearer {ory_at}", "x-api-key": app_token}),
            ("full_grpc",  {**base_ct, "Authorization": f"Bearer {ory_at}", "x-api-key": app_token, **platform_sig}),
        ]

        probe_paths = [
            "/iot-map/device/getMulBoundary",
            "/iot-map/device/getBoundary",
            "/iot-map/device/getMapData",
            "/iot-map/device/getMulMapVersion",
            "/api/smarthome/device/getMulBoundary",
            "/api/smarthome/device/getBoundary",
        ]
        body_base: dict = {"sn": sn, "mapId": 1}

        for path in probe_paths:
            url = f"{self._base_url}{path}"
            for auth_name, hdrs in auth_variants:
                try:
                    async with self._session.post(
                        url, json=body_base, headers=hdrs, timeout=_TIMEOUT
                    ) as resp:
                        http_status = resp.status
                        try:
                            payload = await resp.json()
                        except Exception:
                            payload = (await resp.text())[:300]
                        _LOGGER.debug(
                            "REST probe [%s] POST %s → HTTP%d %s",
                            auth_name, path, http_status, payload,
                        )
                except Exception as exc:
                    _LOGGER.debug("REST probe [%s] POST %s → error: %s", auth_name, path, exc)

        # GET variants for the most likely boundary endpoints
        get_paths = [
            "/iot-map/device/getMulBoundary",
            "/iot-map/device/getBoundary",
            "/smarthome/device/getMulBoundary",
        ]
        params: dict[str, str] = {"sn": sn, "mapId": "1"}
        for path in get_paths:
            url = f"{self._base_url}{path}"
            for auth_name, hdrs in auth_variants:
                try:
                    async with self._session.get(
                        url, params=params, headers=hdrs, timeout=_TIMEOUT
                    ) as resp:
                        http_status = resp.status
                        try:
                            payload = await resp.json()
                        except Exception:
                            payload = (await resp.text())[:300]
                        _LOGGER.debug(
                            "REST probe [%s] GET %s → HTTP%d %s",
                            auth_name, path, http_status, payload,
                        )
                except Exception as exc:
                    _LOGGER.debug("REST probe [%s] GET %s → error: %s", auth_name, path, exc)


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_message(
        self, config_entry: ConfigEntry, sn: str, payload_b64: str
    ) -> None:
        """POST a base64-encoded DeviceMessage to /smarthome/message/send."""
        url = f"{self._base_url}/smarthome/message/send"
        data = {"sn": sn, "payload": payload_b64}
        await self._api_post(config_entry, "/smarthome/message/send", data)

    async def _api_post(
        self,
        config_entry: ConfigEntry,
        path: str,
        body: dict,
        data_key: str | None = None,
    ) -> Any:
        """Generic authenticated POST with automatic token refresh on 401."""
        url = f"{self._base_url}{path}"
        headers = _bearer_headers(config_entry.data["token"])
        async with self._session.post(
            url, json=body if body else None, headers=headers, timeout=_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            resp_json = await resp.json()

        if resp_json.get("code") == 401:
            await self._refresh_token(config_entry)
            return await self._api_post(config_entry, path, body, data_key)

        if resp_json.get("code") not in (200, None):
            raise HomeAssistantError(
                f"API error on {path}: code={resp_json.get('code')} "
                f"message={resp_json.get('message', resp_json.get('msg', ''))}"
            )

        if data_key is not None:
            return resp_json.get("data", {}).get(data_key, [])
        return resp_json

    async def _refresh_token(self, config_entry: ConfigEntry) -> None:
        """Refresh the OAuth2 access token using the stored refresh token."""
        url = self._token_url
        auth = aiohttp.BasicAuth(CLIENT_ID, CLIENT_SECRET)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": config_entry.data["token"]["refresh_token"],
        }
        try:
            async with self._session.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                auth=auth,
                timeout=_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                new_token = await resp.json()
        except (ClientError, TimeoutError) as err:
            self._schedule_reauth(config_entry)
            raise RefreshFailedException(f"Token refresh failed: {err}") from err

        if resp.status != 200:
            self._schedule_reauth(config_entry)
            raise RefreshFailedException("Refresh token is invalid or expired.")

        self._hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, "token": {**config_entry.data["token"], **new_token}},
        )

    def _schedule_reauth(self, config_entry: ConfigEntry) -> None:
        """Schedule a re-authentication notification in HA."""
        self._hass.async_create_task(
            self._hass.config_entries.flow.async_init(
                config_entry.domain,
                context={"source": "reauth", "entry_id": config_entry.entry_id},
                data=config_entry.data,
            )
        )
