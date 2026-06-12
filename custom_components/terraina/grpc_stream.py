"""Async gRPC streaming client for TERRAINA device state.

Connects to iot-streams-*-prod.dongcheng.ink:443 via the /DeviceConnect/Downstream
bidirectional streaming RPC.

Auth headers (required for every call):
  authorization:          Bearer {ory_at_}
  x-api-key:              {kk5fd5Ce app_token}
  x-platform-client-id:   200001
  x-platform-nonce:        <random 8-char>
  x-platform-timestamp:    <unix seconds>
  x-platform-signature:    HMAC-SHA256(rqFcjZXa9oT2, "client_id=200001&nonce=X&timestamp=T")
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import grpc
import grpc.aio

from .const import GRPC_HEARTBEAT_INTERVAL, GRPC_RECONNECT_DELAY
from . import platform_iot_streams_pb2
from .payload import DeviceMessageWrapper, PayLoadExtractor
from .util import from_dict_2_metadata, get_standard_headers
from .const import PLATFORM_CLIENT_ID, PLATFORM_CLIENT_SECRET

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
    from .grpc_util import MessageBuilder
    from .util import random_code, utc_now_str
    from .const import VERSION
    msg_id = "ha-" + random_code()
    svc_id = "ha-" + random_code()
    msg = DeviceMessageWrapper()
    msg.set_info(VERSION, msg_id, sn, utc_now_str()).set_service(
        svc_id, "get", {"getDeviceDetail": None}
    )
    return platform_iot_streams_pb2.In(
        type="device", sn=sn, payload=msg.to_base64()
    )


class TerrainaGrpcStream:
    """Manages a single long-lived gRPC stream for one TERRAINA device."""

    def __init__(
        self,
        grpc_host: str,
        sn: str,
        get_ory_token: Callable[[], str],
        get_app_token: Callable[[], str],
        state_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Init.

        Args:
            grpc_host: host:port  e.g. "iot-streams-eu-prod.dongcheng.ink:443"
            sn: device serial number
            get_ory_token: callable returning current ory_at_ access token string
            get_app_token: callable returning current kk5fd5Ce access token string
            state_callback: called with parsed state dict on every device update
        """
        self._host = grpc_host
        self._sn = sn
        self._get_ory = get_ory_token
        self._get_app = get_app_token
        self._callback = state_callback
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        """Start the streaming task (idempotent)."""
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.ensure_future(self._run_loop())

    def stop(self) -> None:
        """Stop the streaming task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run_loop(self) -> None:
        """Outer reconnection loop."""
        while self._running:
            try:
                await self._stream_once()
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.warning(
                    "gRPC stream for %s failed; retrying in %ds",
                    self._sn,
                    GRPC_RECONNECT_DELAY,
                    exc_info=True,
                )
            if self._running:
                await asyncio.sleep(GRPC_RECONNECT_DELAY)

    async def _stream_once(self) -> None:
        """Open one gRPC stream, consume messages until disconnected."""
        ory = self._get_ory()
        app = self._get_app()
        if not ory or not app:
            _LOGGER.debug("Waiting for tokens before connecting gRPC (%s)", self._sn)
            await asyncio.sleep(GRPC_RECONNECT_DELAY)
            return

        metadata = _build_metadata(ory, app)
        creds = grpc.ssl_channel_credentials()

        async with grpc.aio.secure_channel(self._host, creds) as channel:
            stub = channel.stream_stream(
                _GRPC_PATH,
                request_serializer=platform_iot_streams_pb2.In.SerializeToString,
                response_deserializer=platform_iot_streams_pb2.Out.FromString,
            )
            call = stub(metadata=metadata)

            # Send connect + initial state query
            await call.write(_connect_msg(self._sn))
            await call.write(_heartbeat_msg(self._sn))
            await call.write(_query_state_msg(self._sn))

            _LOGGER.debug("gRPC stream connected for %s", self._sn)

            heartbeat_task = asyncio.ensure_future(
                self._heartbeat_writer(call)
            )
            try:
                async for msg in call:
                    if not self._running:
                        break
                    await self._handle_message(msg)
            finally:
                heartbeat_task.cancel()
                try:
                    await call.done_writing()
                except Exception:
                    pass

    async def _heartbeat_writer(self, call: Any) -> None:
        """Send periodic heartbeat messages."""
        while True:
            await asyncio.sleep(GRPC_HEARTBEAT_INTERVAL)
            try:
                await call.write(_heartbeat_msg(self._sn))
            except Exception:
                break

    async def _handle_message(self, msg: platform_iot_streams_pb2.Out) -> None:
        """Parse an incoming gRPC message and invoke the state callback."""
        if msg.type == "heartbeat":
            _LOGGER.debug("gRPC heartbeat from server: %s", msg.payload)
            return

        if msg.type != "device":
            _LOGGER.debug("gRPC msg type=%r (ignored)", msg.type)
            return

        if not msg.payload:
            return

        try:
            wrapper = DeviceMessageWrapper.from_base64(msg.payload)
            extractor = PayLoadExtractor(wrapper)
            state = extractor.extract()
            if state:
                _LOGGER.debug("gRPC device state for %s: %s", self._sn, state)
                self._callback(state)
        except Exception:
            _LOGGER.debug(
                "Failed to parse gRPC payload for %s", self._sn, exc_info=True
            )
