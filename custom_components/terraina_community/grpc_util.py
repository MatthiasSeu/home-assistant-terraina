"""gRPC message builders for the TERRAINA Community integration."""

import base64
import logging

from . import platform_iot_streams_pb2
from .const import (
    BINDING,
    CHARGING,
    DOMAIN,
    EMERGENCY_STOP,
    LOCKED,
    LOG,
    OTA,
    RAINED,
    TASK,
    VERSION,
    WORKING_MODE,
    WORKING_STATUS,
)
from .payload import DeviceMessageWrapper
from .util import random_code, utc_now_str

CLOCK_OUT_OF_SYNC_MAX_SEC = 20
HEARTBEAT_TIME_INTERVAL = 30
_LOGGER = logging.getLogger(__name__)


class MessageBuilder:
    """Build gRPC / REST message payloads."""

    _HA_PREFIX = "ha-"
    _VERSION = VERSION

    def build_heartbeat(self, serial_number: str) -> platform_iot_streams_pb2.In:
        """Produce heartbeat message."""
        return platform_iot_streams_pb2.In(
            type="heartbeat",
            sn=serial_number,
            payload="",
        )

    def build_query_state_message(
        self, serial_number: str
    ) -> tuple[platform_iot_streams_pb2.In, str]:
        """Produce getDeviceDetail message (for future gRPC state polling)."""
        msg_id = self._HA_PREFIX + random_code()
        service_id = self._HA_PREFIX + random_code()
        msg = DeviceMessageWrapper()
        msg.set_info(self._VERSION, msg_id, serial_number, utc_now_str()).set_service(
            service_id, "get", {"getDeviceDetail": None}
        )
        return platform_iot_streams_pb2.In(
            type="device",
            sn=serial_number,
            payload=msg.to_base64(),
        ), msg_id

    def build_change_status_message_payload(
        self, serial_number: str, status: int
    ) -> str:
        """Produce base64 payload for setWorkStatus command."""
        msg_id = self._HA_PREFIX + random_code()
        service_id = self._HA_PREFIX + random_code()
        msg = DeviceMessageWrapper()
        msg.set_info(self._VERSION, msg_id, serial_number, utc_now_str()).set_service(
            service_id,
            "set",
            {"setWorkStatus": {"status": status}},
        )
        return msg.to_base64()

    def build_change_working_mode_payload(
        self, serial_number: str, manual_mode_type: int
    ) -> str:
        """Produce base64 payload for setWorkMode command.

        0 = auto (schedule), 1 = manual.
        The device's status bit 13 (working_mode) is controlled by 'workMode'.
        'manualModeType' is a sub-type that only applies within manual mode.
        """
        msg_id = self._HA_PREFIX + random_code()
        service_id = self._HA_PREFIX + random_code()
        msg = DeviceMessageWrapper()
        msg.set_info(self._VERSION, msg_id, serial_number, utc_now_str()).set_service(
            service_id,
            "set",
            {"setWorkMode": {"workMode": manual_mode_type}},
        )
        return msg.to_base64()

    def build_set_rain_enable_payload(self, serial_number: str, enabled: int) -> str:
        """Produce base64 payload for setRainEnable command (enabled: 0 or 1)."""
        msg_id = self._HA_PREFIX + random_code()
        service_id = self._HA_PREFIX + random_code()
        msg = DeviceMessageWrapper()
        msg.set_info(self._VERSION, msg_id, serial_number, utc_now_str()).set_service(
            service_id,
            "set",
            {"setRainEnable": {"rainEnable": enabled}},
        )
        return msg.to_base64()

    def build_set_rain_delay_payload(self, serial_number: str, minutes: int) -> str:
        """Produce base64 payload for setRainDelay command (minutes: 60, 120, or 180)."""
        msg_id = self._HA_PREFIX + random_code()
        service_id = self._HA_PREFIX + random_code()
        msg = DeviceMessageWrapper()
        msg.set_info(self._VERSION, msg_id, serial_number, utc_now_str()).set_service(
            service_id,
            "set",
            {"setRainDelay": {"rainDelay": minutes}},
        )
        return msg.to_base64()

    def build_set_ai_height_payload(self, serial_number: str, height: int) -> str:
        """Produce base64 payload for setAiHeight command (height in mm, EU: 30-80)."""
        msg_id = self._HA_PREFIX + random_code()
        service_id = self._HA_PREFIX + random_code()
        msg = DeviceMessageWrapper()
        msg.set_info(self._VERSION, msg_id, serial_number, utc_now_str()).set_service(
            service_id,
            "set",
            {"setAiHeight": {"aiHeight": height}},
        )
        return msg.to_base64()

    def build_set_schedule_payload(
        self, serial_number: str, schedule_days: list[dict]
    ) -> str:
        """Produce base64 payload for setSchedule command.

        schedule_days: list of dicts with keys week(0-6), enable(0/1),
        startTime(minutes), endTime(minutes), mapId, boundaryId, regionId, needEdge.
        ParamsWrapper.set_value cannot serialize Python lists, so we build the
        repeated 'schedule' params manually using the raw proto API.
        """
        from .terraina_pb2 import DeviceMessage

        msg_id = self._HA_PREFIX + random_code()
        service_id = self._HA_PREFIX + random_code()

        proto = DeviceMessage()
        proto.version = self._VERSION
        proto.id = msg_id
        proto.sn = serial_number
        proto.time = utc_now_str()

        svc = proto.services
        svc.id = service_id
        svc.name = "set"

        # Top-level param: setSchedule
        set_sche = svc.params.add()
        set_sche.key = "setSchedule"

        type_p = set_sche.params.add()
        type_p.key = "type"
        type_p.intValue = 0

        global_sche = set_sche.params.add()
        global_sche.key = "globalSche"

        def _add_int(parent, k: str, v: int) -> None:
            p = parent.params.add()
            p.key = k
            p.intValue = int(v)

        for day in schedule_days:
            day_p = global_sche.params.add()
            day_p.key = "schedule"
            _add_int(day_p, "enable", day.get("enable", 1))
            _add_int(day_p, "week", day["week"])
            _add_int(day_p, "startTime", day["startTime"])
            _add_int(day_p, "endTime", day["endTime"])
            _add_int(day_p, "mapId", day.get("mapId", 1))
            _add_int(day_p, "boundaryId", day.get("boundaryId", -1))
            _add_int(day_p, "regionId", day.get("regionId", -1))
            _add_int(day_p, "needEdge", day.get("needEdge", 1))

        return base64.b64encode(proto.SerializeToString()).decode()


def split_bits(value: int) -> dict:
    """Decode a TERRAINA integer status field into named sub-fields.

    Bit layout:
      0     locked          (LOCKED)
      1     emergency_stop  (EMERGENCY_STOP)
      2-3   charging        (CHARGING)
      4-5   rained          (RAINED)
      6-9   working_status  (WORKING_STATUS)
      10    ota             (OTA)
      11    log             (LOG)
      12    task            (TASK)
      13    working_mode    (WORKING_MODE)
      14    binding         (BINDING)
    """

    def _safe(lst: list, idx: int) -> str:
        return lst[idx] if 0 <= idx < len(lst) else f"unknown({idx})"

    return {
        "locked":          _safe(LOCKED,         (value >> 0)  & 0b1),
        "emergency_stop":  _safe(EMERGENCY_STOP, (value >> 1)  & 0b1),
        "charging":        _safe(CHARGING,       (value >> 2)  & 0b11),
        "rained":          _safe(RAINED,         (value >> 4)  & 0b11),
        "working_status":  _safe(WORKING_STATUS, (value >> 6)  & 0b1111),
        "ota":             _safe(OTA,            (value >> 10) & 0b1),
        "log":             _safe(LOG,            (value >> 11) & 0b1),
        "task":            _safe(TASK,           (value >> 12) & 0b1),
        "working_mode":    _safe(WORKING_MODE,   (value >> 13) & 0b1),
        "binding":         _safe(BINDING,        (value >> 14) & 0b1),
        "raw_value":       value,
    }
