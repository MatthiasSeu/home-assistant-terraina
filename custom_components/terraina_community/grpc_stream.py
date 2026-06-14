"""Async gRPC streaming client for TERRAINA Community device state.

Connects to iot-streams-*-prod.dongcheng.ink:443 via /DeviceConnect/Downstream.

Auth headers required for every call:
  authorization:         Bearer {ory_at_}
  x-api-key:             {kk5fd5Ce app_token}
  x-platform-client-id:  200001
  x-platform-nonce:       <random>
  x-platform-timestamp:   <unix seconds>
  x-platform-signature:   HMAC-SHA256(rqFcjZXa9oT2, "client_id=…&nonce=…&timestamp=…")
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import grpc
import grpc.aio

from .const import (
    GRPC_HEARTBEAT_INTERVAL,
    GRPC_RECONNECT_DELAY,
    PLATFORM_CLIENT_ID,
    PLATFORM_CLIENT_SECRET,
    VERSION,
)
from . import platform_iot_streams_pb2
from .payload import DeviceMessageWrapper, PayLoadExtractor
from .util import from_dict_2_metadata, get_standard_headers, random_code, utc_now_str

_LOGGER = logging.getLogger(__name__)
_GRPC_PATH = "/DeviceConnect/Downstream"


def _build_metadata(ory_at: str, app_token: str) -> list[tuple[str, str]]:
    headers = get_standard_headers(PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET)
    headers["authorization"] = f"Bearer {ory_at}"
    headers["x-api-key"] = app_token
    return from_dict_2_metadata(headers)


def _connect_msg(sn: str) -> platform_iot_streams_pb2.In:
    return platform_iot_streams_pb2.In(type="connect", sn=sn, payload="")


def _heartbeat_msg(sn: str) -> platform_iot_streams_pb2.In:
    return platform_iot_streams_pb2.In(type="heartbeat", sn=sn, payload="")


def _query_state_msg(sn: str) -> platform_iot_streams_pb2.In:
    msg_id = "ha-" + random_code()
    svc_id = "ha-" + random_code()
    msg = DeviceMessageWrapper()
    msg.set_info(VERSION, msg_id, sn, utc_now_str()).set_service(
        svc_id, "get", {"getDeviceDetail": None}
    )
    return platform_iot_streams_pb2.In(type="device", sn=sn, payload=msg.to_base64())


def _query_map_msgs(sn: str) -> list[platform_iot_streams_pb2.In]:
    """Return probe messages for map-related gRPC commands (sent once at startup, no params)."""
    candidates = ["getMulMapData", "getMapConfig", "getMulMapVersion", "getMulBoundary"]
    msgs = []
    for cmd in candidates:
        msg_id = "ha-" + random_code()
        svc_id = "ha-" + random_code()
        msg = DeviceMessageWrapper()
        msg.set_info(VERSION, msg_id, sn, utc_now_str()).set_service(
            svc_id, "get", {cmd: None}
        )
        msgs.append(platform_iot_streams_pb2.In(type="device", sn=sn, payload=msg.to_base64()))
    return msgs


def _query_mul_map_data_msgs(sn: str, map_ver: int) -> list[platform_iot_streams_pb2.In]:
    """Follow-up queries once we know the mapVersion from getMulMapVersion.

    We probe multiple parameter key names because the protocol is undocumented.
    """
    variants = [
        {"getMulMapData": {"mapVer": map_ver}},
        {"getMulMapData": {"mapVersion": map_ver}},
        {"getMulMapData": {"mapId": 1, "mapVer": map_ver}},
        {"getMulBoundary": {"mapId": 1}},
    ]
    msgs = []
    for params in variants:
        msg_id = "ha-" + random_code()
        svc_id = "ha-" + random_code()
        msg = DeviceMessageWrapper()
        msg.set_info(VERSION, msg_id, sn, utc_now_str()).set_service(svc_id, "get", params)
        msgs.append(platform_iot_streams_pb2.In(type="device", sn=sn, payload=msg.to_base64()))
    return msgs


def _query_regions_msg(sn: str) -> platform_iot_streams_pb2.In:
    msg_id = "ha-" + random_code()
    svc_id = "ha-" + random_code()
    msg = DeviceMessageWrapper()
    msg.set_info(VERSION, msg_id, sn, utc_now_str()).set_service(
        svc_id, "get", {"getRegions": None}
    )
    return platform_iot_streams_pb2.In(type="device", sn=sn, payload=msg.to_base64())


def _query_schedule_msg(sn: str) -> platform_iot_streams_pb2.In:
    msg_id = "ha-" + random_code()
    svc_id = "ha-" + random_code()
    msg = DeviceMessageWrapper()
    msg.set_info(VERSION, msg_id, sn, utc_now_str()).set_service(
        svc_id, "get", {"getSchedule": None}
    )
    return platform_iot_streams_pb2.In(type="device", sn=sn, payload=msg.to_base64())


class TerrainaGrpcStream:
    """Manages a single long-lived gRPC stream for one TERRAINA device."""

    def __init__(
        self,
        grpc_host: str,
        sn: str,
        get_ory_token: Callable[[], str],
        get_app_token: Callable[[], str],
        state_callback: Callable[[dict[str, Any]], None],
        on_unauthenticated: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._host = grpc_host
        self._sn = sn
        self._get_ory = get_ory_token
        self._get_app = get_app_token
        self._callback = state_callback
        self._on_unauthenticated = on_unauthenticated
        self._task: asyncio.Task | None = None
        self._running = False
        self._map_data_queried: bool = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.ensure_future(self._run_loop())

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run_loop(self) -> None:
        import grpc
        while self._running:
            try:
                await self._stream_once()
            except asyncio.CancelledError:
                break
            except grpc.aio.AioRpcError as exc:
                code = exc.code()
                details = (exc.details() or "")[:200]
                _LOGGER.warning(
                    "gRPC %s for %s: %r", code.name, self._sn, details
                )
                needs_relogin = code in (
                    grpc.StatusCode.UNAUTHENTICATED,
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                if needs_relogin and self._on_unauthenticated:
                    refreshed = await self._on_unauthenticated()
                    if not refreshed:
                        _LOGGER.warning(
                            "Token refresh failed for %s; retrying in %ds",
                            self._sn, GRPC_RECONNECT_DELAY,
                        )
            except Exception as exc:
                _LOGGER.warning(
                    "gRPC stream for %s failed (%s); retrying in %ds",
                    self._sn, exc, GRPC_RECONNECT_DELAY, exc_info=True,
                )
            if self._running:
                await asyncio.sleep(GRPC_RECONNECT_DELAY)

    async def _stream_once(self) -> None:
        ory = self._get_ory()
        app = self._get_app()
        if not ory or not app:
            _LOGGER.debug("Waiting for tokens before connecting gRPC (%s)", self._sn)
            await asyncio.sleep(GRPC_RECONNECT_DELAY)
            return

        _LOGGER.debug("Connecting gRPC for %s (ory=...%s kk5=...%s)",
                      self._sn, ory[-8:], app[-8:])

        metadata = _build_metadata(ory, app)
        creds = grpc.ssl_channel_credentials()

        self._map_data_queried = False  # reset per connection

        async with grpc.aio.secure_channel(self._host, creds) as channel:
            stub = channel.stream_stream(
                _GRPC_PATH,
                request_serializer=platform_iot_streams_pb2.In.SerializeToString,
                response_deserializer=platform_iot_streams_pb2.Out.FromString,
            )
            call = stub(metadata=metadata)

            # Only send connect here; heartbeat_writer sends heartbeat immediately
            # and then every GRPC_HEARTBEAT_INTERVAL seconds.
            # Sending multiple messages before the channel is ready causes
            # ExecuteBatchError on some grpcio versions.
            await call.write(_connect_msg(self._sn))

            _LOGGER.debug("gRPC stream connected for %s", self._sn)

            schedule_fetched = False   # reset per connection
            heartbeat_task = asyncio.ensure_future(self._heartbeat_writer(call))
            try:
                async for msg in call:
                    if not self._running:
                        break
                    newly_active, followup_msgs = await self._handle_message(msg)
                    for fmsg in followup_msgs:
                        try:
                            await call.write(fmsg)
                        except Exception:
                            pass
                    if newly_active and not schedule_fetched:
                        # Mower just became active — grab schedule immediately
                        try:
                            await call.write(_query_schedule_msg(self._sn))
                            schedule_fetched = True
                            _LOGGER.debug("Triggered on-demand getSchedule for %s", self._sn)
                        except Exception:
                            pass
            finally:
                heartbeat_task.cancel()
                try:
                    await call.done_writing()
                except Exception:
                    pass

    async def _heartbeat_writer(self, call: Any) -> None:
        # Short delay so the connect message is fully sent before we write more.
        await asyncio.sleep(0.5)
        try:
            await call.write(_heartbeat_msg(self._sn))
            await call.write(_query_state_msg(self._sn))
            await call.write(_query_schedule_msg(self._sn))
            await call.write(_query_regions_msg(self._sn))
            for map_msg in _query_map_msgs(self._sn):
                await call.write(map_msg)
        except Exception:
            return
        tick = 0
        while True:
            await asyncio.sleep(GRPC_HEARTBEAT_INTERVAL)
            tick += 1
            try:
                await call.write(_heartbeat_msg(self._sn))
                await call.write(_query_state_msg(self._sn))
                # Refresh schedule every ~5 minutes (10 heartbeat intervals of 30s)
                if tick % 10 == 0:
                    await call.write(_query_schedule_msg(self._sn))
            except Exception:
                break

    async def _handle_message(
        self, msg: platform_iot_streams_pb2.Out
    ) -> tuple[bool, list[platform_iot_streams_pb2.In]]:
        """Handle one incoming gRPC message.

        Returns (device_active, followup_msgs):
          device_active   — True when postDeviceDetail shows the device is mowing
          followup_msgs   — additional messages to write to the stream immediately
        """
        if msg.type == "heartbeat":
            _LOGGER.debug("gRPC heartbeat from server: %s", msg.payload)
            return False, []

        # Server sends device-state responses with type="" (not "device")
        if msg.type not in ("device", ""):
            _LOGGER.debug("gRPC msg type=%r sm=%r (ignored)", msg.type, msg.sm)
            return False, []

        if not msg.payload:
            _LOGGER.debug("gRPC msg type=%r sm=%r — empty payload (skipped)", msg.type, msg.sm)
            return False, []

        device_active = False
        followup_msgs: list[platform_iot_streams_pb2.In] = []
        try:
            wrapper = DeviceMessageWrapper.from_base64(msg.payload)
            extractor = PayLoadExtractor(wrapper)
            state = extractor.extract()
            if state:
                _LOGGER.debug("gRPC device state sm=%r: %s", msg.sm, state)
                self._callback(state)

                # As soon as we know the mapVersion, probe getMulMapData with params
                if "getMulMapVersion" in state and not self._map_data_queried:
                    map_ver = (state["getMulMapVersion"].get("data") or {}).get("mapVersion")
                    if map_ver:
                        self._map_data_queried = True
                        followup_msgs = _query_mul_map_data_msgs(self._sn, map_ver)
                        _LOGGER.debug(
                            "gRPC: queuing %d getMulMapData variants with mapVer=%d for %s",
                            len(followup_msgs), map_ver, self._sn,
                        )

                if "postDeviceDetail" in state:
                    info = state["postDeviceDetail"].get("info") or {}
                    try:
                        is_manual = bool((int(info.get("status", 0)) >> 13) & 1)
                    except (TypeError, ValueError):
                        is_manual = False
                    if is_manual:
                        _LOGGER.debug(
                            "gRPC: mower %s is in manual mode — getSchedule skipped", self._sn
                        )
                    device_active = not is_manual
            else:
                inner = wrapper.get_proto()
                prop_parts = []
                for p in inner.properties:
                    if p.stringValue:
                        v = repr(p.stringValue[:80])
                    elif p.intValue:
                        v = str(p.intValue)
                    elif p.doubleValue:
                        v = str(p.doubleValue)
                    elif p.bytesValue:
                        v = f"<bytes:{len(p.bytesValue)}>"
                    else:
                        v = "(empty)"
                    prop_parts.append(f"{p.name}={v}")
                _LOGGER.debug(
                    "gRPC unknown msg type=%r sm=%r svc_name=%r events=%d props=[%s] | %s",
                    msg.type, msg.sm,
                    inner.services.name,
                    len(inner.events),
                    ", ".join(prop_parts),
                    wrapper,
                )
        except Exception:
            _LOGGER.debug("Failed to parse gRPC payload (type=%r sm=%r)", msg.type, msg.sm, exc_info=True)
        return device_active, followup_msgs
