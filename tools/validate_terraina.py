#!/usr/bin/env python3
"""
Phase 0 Validation Script — TERRAINA KDRM210/220 Community Integration
=======================================================================

Tests four scenarios against a real device:
  Step 1 — OAuth2 token validity  (GET /smarthome/device/getUserBindDevices)
  Step 2 — getDeviceDetail query  (sent via REST message/send + optional gRPC)
  Step 3 — setWorkStatus(3=mowing)  *** STARTS THE MOWER — use --confirm-mow ***
  Step 4 — setWorkStatus(7=resting) *** STOPS  THE MOWER — use --confirm-rest ***

Requires:
  pip install aiohttp "protobuf>=6.30" "grpcio>=1.72"  # grpcio only for --test-grpc

Token sources (pick one):
  --token ACCESS_TOKEN_STRING
  --ha-storage /path/to/core.config_entries      (HA .storage file)
  --token-file /path/to/token.json               (JSON with access_token + region)

Examples:
  python validate_terraina.py --ha-storage /mnt/ha-config/.storage/core.config_entries
  python validate_terraina.py --token eyJh... --region eu --dry-run
  python validate_terraina.py --token eyJh... --confirm-rest
  python validate_terraina.py --token eyJh... --confirm-mow  # ACTUALLY MOWS
  python validate_terraina.py --decode-bits 1792              # offline bit decode
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac as _hmac_module
import importlib.util as _ilu
import json
import logging
import random
import string
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
try:
    import aiohttp
    from aiohttp import ClientTimeout
except ImportError:
    sys.exit("ERROR: aiohttp not installed.  Run:  pip install aiohttp")

try:
    from google.protobuf import descriptor as _pb_descriptor  # noqa: F401
except ImportError:
    sys.exit("ERROR: protobuf not installed.  Run:  pip install 'protobuf>=6.30'")

# ---------------------------------------------------------------------------
# Load protobuf definitions from the component directory
# (avoids importing HA-dependent modules)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent
_COMPONENT_DIR = _REPO_ROOT / "custom_components" / "terraina"

if not _COMPONENT_DIR.exists():
    sys.exit(f"ERROR: Component directory not found: {_COMPONENT_DIR}")


def _load_pb2(name: str):
    spec = _ilu.spec_from_file_location(name, _COMPONENT_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        sys.exit(f"ERROR: Cannot locate {name}.py in {_COMPONENT_DIR}")
    mod = _ilu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        sys.exit(f"ERROR loading {name}.py: {exc}\n"
                 "Make sure protobuf>=6.30 is installed.")
    return mod


_terraina_pb2 = _load_pb2("terraina_pb2")
_streams_pb2 = _load_pb2("platform_iot_streams_pb2")

DeviceMessage = _terraina_pb2.DeviceMessage
StreamIn = _streams_pb2.In
StreamOut = _streams_pb2.Out

# ---------------------------------------------------------------------------
# Constants (mirrors const.py — kept standalone)
# ---------------------------------------------------------------------------
REGION_BASE_URL: dict[str, str] = {
    "cn": "https://iot-platform-cn-prod.dongcheng.ink",
    "eu": "https://iot-platform-eu-prod.dongcheng.ink",
    "us": "https://iot-platform-us-prod.dongcheng.ink",
}

# TERRAINA-specific REST host — discovered via mitmproxy traffic capture (Phase 1b)
REGION_TERRAINA_URL: dict[str, str] = {
    "cn": "https://iot-terraina-cn-prod.dongcheng.ink",
    "eu": "https://iot-terraina-eu-prod.dongcheng.ink",
    "us": "https://iot-terraina-us-prod.dongcheng.ink",
}

# Dedicated gRPC stream host — discovered via APK static analysis (libapp.so strings)
REGION_GRPC_HOST: dict[str, str] = {
    "cn": "iot-streams-cn-prod.dongcheng.ink",
    "eu": "iot-streams-eu-prod.dongcheng.ink",
    "us": "iot-streams-us-prod.dongcheng.ink",
}

# gRPC service paths — discovered via APK static analysis
GRPC_PATH_DOWNSTREAM = "/DeviceConnect/Downstream"   # server → client state stream
GRPC_PATH_UPMESSAGE  = "/DeviceConnect/UpMessage"    # client → server commands

CLIENT_ID = "AfAsyMwEMPDf1I5CTfIc9G2a7LT5YnQCAxLm5rVlx992mNh7B9spEVpK"
CLIENT_SECRET = ""

# Bit-field decode tables (from const.py)
_LOCKED         = ["unlocked", "locked"]
_EMERGENCY_STOP = ["no emergency stop", "emergency stop"]
_CHARGING       = ["not in basestation", "charging", "fully charged"]
_RAINED         = ["not rained", "being rained", "being rained and delayed"]
_WORKING_STATUS = [
    "",                    # 0x00
    "hanging",             # 0x01  待机
    "building graph",      # 0x02
    "mowing",              # 0x03
    "backing",             # 0x04
    "backing with low power",  # 0x05
    "locating",            # 0x06
    "resting",             # 0x07
    "error",               # 0x08
    "offline",             # 0x09
    "",                    # 0x0A
    "leaving basestation", # 0x0B
    "",                    # 0x0C
]
_OTA          = ["available", "upgrading"]
_LOG          = ["available", "uploading"]
_TASK         = ["available", "assigned"]
_WORKING_MODE = ["auto", "manual"]
_BINDING      = ["not binding", "binding"]

VERSION = "1.0.0"
HA_PREFIX = "ha-"


# ---------------------------------------------------------------------------
# Utility helpers (inlined from util.py — no HA dependencies)
# ---------------------------------------------------------------------------

def _random_code(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def _utc_now_str() -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(now.microsecond / 1000):03d}"


def _hmac_sha256_hex(message: str, key: str) -> str:
    return _hmac_module.new(
        key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _platform_headers(access_token: str) -> dict[str, str]:
    """Build standard REST + HMAC headers required by the Dongcheng platform."""
    ts = str(int(time.time()))
    nonce = str(uuid.uuid4())
    params_for_sig = {"client_id": CLIENT_ID, "timestamp": ts, "nonce": nonce}
    sorted_msg = "&".join(f"{k}={v}" for k, v in sorted(params_for_sig.items()))
    sig = _hmac_sha256_hex(sorted_msg, CLIENT_SECRET)
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "x-platform-client-id": CLIENT_ID,
        "x-platform-timestamp": ts,
        "x-platform-nonce": nonce,
        "x-platform-signature": sig,
    }


def _grpc_metadata_hmac(
    access_token: str | None = None,
    iso_country: str = "AT",
    phone_os: str = "android",
) -> list[tuple[str, str]]:
    """Build gRPC metadata with full 5-parameter HMAC signature.

    APK analysis revealed the gRPC server requires ALL x-platform headers to be
    included in the HMAC-SHA256 signature (not just client_id+nonce+timestamp):
      client_id, iso_country, nonce, phone_os, timestamp  (sorted alphabetically)
    """
    ts = str(int(time.time()))
    nonce = str(uuid.uuid4())
    params_for_sig: dict[str, str] = {
        "client_id":   CLIENT_ID,
        "iso_country": iso_country,
        "nonce":       nonce,
        "phone_os":    phone_os,
        "timestamp":   ts,
    }
    sorted_msg = "&".join(f"{k}={v}" for k, v in sorted(params_for_sig.items()))
    sig = _hmac_sha256_hex(sorted_msg, CLIENT_SECRET)
    meta = [
        ("x-platform-client-id",   CLIENT_ID),
        ("x-platform-iso-country", iso_country),
        ("x-platform-phone-os",    phone_os),
        ("x-platform-nonce",       nonce),
        ("x-platform-timestamp",   ts),
        ("x-platform-signature",   sig),
    ]
    if access_token:
        meta = [("authorization", f"Bearer {access_token}")] + meta
    return meta


def _grpc_metadata_hmac_only() -> list[tuple[str, str]]:
    """Legacy: HMAC-only with old 3-param signature (kept for REST compat)."""
    ts = str(int(time.time()))
    nonce = str(uuid.uuid4())
    params_for_sig = {"client_id": CLIENT_ID, "timestamp": ts, "nonce": nonce}
    sorted_msg = "&".join(f"{k}={v}" for k, v in sorted(params_for_sig.items()))
    sig = _hmac_sha256_hex(sorted_msg, CLIENT_SECRET)
    return [
        ("x-platform-client-id", CLIENT_ID),
        ("x-platform-timestamp", ts),
        ("x-platform-nonce", nonce),
        ("x-platform-signature", sig),
    ]


def _grpc_metadata_bearer_only(access_token: str) -> list[tuple[str, str]]:
    """Bearer token only."""
    return [("authorization", f"Bearer {access_token}")]


def _grpc_metadata_both(access_token: str) -> list[tuple[str, str]]:
    """Bearer token + HMAC headers combined."""
    return _grpc_metadata_bearer_only(access_token) + _grpc_metadata_hmac_only()


def _platform_grpc_metadata(access_token: str) -> list[tuple[str, str]]:
    """Build gRPC call metadata from the same headers (legacy — kept for compatibility)."""
    return _grpc_metadata_both(access_token)


# ---------------------------------------------------------------------------
# Protobuf message builders (inlined from payload.py / grpc_util.py)
# ---------------------------------------------------------------------------

def _set_params_proto(proto, key: str, value: Any) -> None:
    """Recursively populate a Params proto from a Python value."""
    proto.key = key
    if value is None:
        pass  # leave oneof unset — server interprets as "no value"
    elif isinstance(value, bool):
        proto.boolValue = value
    elif isinstance(value, int):
        proto.intValue = value
    elif isinstance(value, float):
        proto.doubleValue = value
    elif isinstance(value, str):
        proto.stringValue = value
    elif isinstance(value, dict):
        for k, v in value.items():
            _set_params_proto(proto.params.add(), k, v)


def _build_device_message(sn: str, service_name: str, params: dict[str, Any]) -> str:
    """Serialize a DeviceMessage service call to a base64 string."""
    msg = DeviceMessage()
    msg.version = VERSION
    msg.id = HA_PREFIX + _random_code()
    msg.sn = sn
    msg.time = _utc_now_str()
    msg.services.id = HA_PREFIX + _random_code()
    msg.services.name = service_name
    for k, v in params.items():
        _set_params_proto(msg.services.params.add(), k, v)
    return base64.b64encode(msg.SerializeToString()).decode()


def build_get_device_detail(sn: str) -> str:
    return _build_device_message(sn, "get", {"getDeviceDetail": None})


def build_set_work_status(sn: str, status: int) -> str:
    return _build_device_message(sn, "set", {"setWorkStatus": {"status": status}})


# ---------------------------------------------------------------------------
# Bit-field decoder  (fixes the spurious `self` parameter in original split_bits)
# ---------------------------------------------------------------------------

def split_bits(value: int) -> dict[str, Any]:
    """Decode the TERRAINA integer status field into named sub-fields."""

    def _safe(lst: list, idx: int) -> str:
        return lst[idx] if 0 <= idx < len(lst) else f"unknown({idx})"

    return {
        "locked":          _safe(_LOCKED,         (value >> 0)  & 0b1),
        "emergency_stop":  _safe(_EMERGENCY_STOP, (value >> 1)  & 0b1),
        "charging":        _safe(_CHARGING,       (value >> 2)  & 0b11),
        "rained":          _safe(_RAINED,         (value >> 4)  & 0b11),
        "working_status":  _safe(_WORKING_STATUS, (value >> 6)  & 0b1111),
        "ota":             _safe(_OTA,            (value >> 10) & 0b1),
        "log":             _safe(_LOG,            (value >> 11) & 0b1),
        "task":            _safe(_TASK,           (value >> 12) & 0b1),
        "working_mode":    _safe(_WORKING_MODE,   (value >> 13) & 0b1),
        "binding":         _safe(_BINDING,        (value >> 14) & 0b1),
        "raw_decimal":     value,
        "raw_hex":         hex(value),
        "raw_binary":      bin(value),
    }


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _params_proto_to_dict(params) -> dict[str, Any]:
    """Recursively convert a repeated Params proto field to a Python dict."""
    result: dict[str, Any] = {}
    for p in params:
        key = p.key
        if len(p.params) > 0:
            value: Any = _params_proto_to_dict(p.params)
        else:
            oneof = p.WhichOneof("value")
            value = getattr(p, oneof) if oneof else None
        if key in result:
            existing = result[key]
            result[key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            result[key] = value
    return result


def parse_device_message_b64(b64: str) -> dict[str, Any]:
    """Decode a base64 DeviceMessage and return a structured dict."""
    try:
        raw = base64.b64decode(b64)
        msg = DeviceMessage()
        msg.ParseFromString(raw)
        return {
            "version": msg.version,
            "id": msg.id,
            "sn": msg.sn,
            "time": msg.time,
            "service_id": msg.services.id,
            "service_name": msg.services.name,
            "params": _params_proto_to_dict(msg.services.params),
            "properties": [
                {
                    "name": p.name,
                    "value_oneof": p.WhichOneof("value"),
                    "value": getattr(p, p.WhichOneof("value")) if p.WhichOneof("value") else None,
                }
                for p in msg.properties
            ],
        }
    except Exception as exc:
        return {"parse_error": str(exc), "raw_b64": b64[:120] + "…"}


def _find_status_int(d: dict) -> int | None:
    """Recursively search a parsed params dict for any integer that looks like a status."""
    for k, v in d.items():
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, dict):
            result = _find_status_int(v)
            if result is not None:
                return result
    return None


# ---------------------------------------------------------------------------
# Token resolution helpers
# ---------------------------------------------------------------------------

def load_token_from_ha_storage(storage_path: str) -> dict[str, str]:
    """Extract the first TERRAINA token from HA core.config_entries."""
    path = Path(storage_path).expanduser()
    if not path.exists():
        sys.exit(f"ERROR: HA storage file not found: {path}")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("data", {}).get("entries", [])
    terraina_entries = [
        e for e in entries
        if e.get("domain", "").startswith("terraina")
    ]

    if not terraina_entries:
        domains = sorted({e.get("domain") for e in entries})
        sys.exit(
            f"ERROR: No terraina/terraina_community entry in {path}.\n"
            f"Available domains: {domains}"
        )

    entry = terraina_entries[0]
    token_data = entry.get("data", {}).get("token", {})
    region = entry.get("data", {}).get("region", "eu")
    print(f"[token]  Source : HA storage  ({path.name})")
    print(f"[token]  Domain : {entry['domain']}")
    print(f"[token]  Region : {region}")
    return {
        "access_token":  token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "region": region,
    }


def load_token_from_file(path_str: str) -> dict[str, str]:
    path = Path(path_str).expanduser()
    if not path.exists():
        sys.exit(f"ERROR: Token file not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if "access_token" not in data:
        sys.exit(f"ERROR: Token file must contain 'access_token' key.")
    return data


# ---------------------------------------------------------------------------
# REST API calls
# ---------------------------------------------------------------------------

TIMEOUT = ClientTimeout(total=20)

_LOG = logging.getLogger("terraina_validate")


async def probe_terraina_host(
    session: aiohttp.ClientSession, access_token: str, region: str, sn: str | None = None
) -> None:
    """Probe iot-terraina-eu-prod.dongcheng.ink for device status endpoints (Phase 1b discovery)."""
    terraina_url = REGION_TERRAINA_URL.get(region, REGION_TERRAINA_URL["eu"])
    headers = _platform_headers(access_token)
    print(f"  Probing TERRAINA host: {terraina_url}")

    endpoints_get = [
        "/smarthome/device/getUserBindDevices",
        "/smarthome/device/getDeviceList",
        "/smarthome/device/getDeviceStatus",
        "/smarthome/device/getDeviceInfo",
        "/smarthome/device/deviceList",
        "/device/list",
        "/device/status",
    ]
    endpoints_post_with_sn = [
        "/smarthome/device/getDeviceDetail",
        "/smarthome/device/getWorkStatus",
        "/smarthome/device/getDeviceStatus",
        "/smarthome/message/send",
    ]

    for path in endpoints_get:
        url = f"{terraina_url}{path}"
        try:
            async with session.get(url, headers=headers, timeout=ClientTimeout(total=8)) as resp:
                body = await resp.text()
                print(f"  GET  {path:50s}  HTTP {resp.status}  {body[:120]}")
        except Exception as exc:
            print(f"  GET  {path:50s}  ERROR: {exc}")

    # Also try POST with getUserBindDevices body (same as platform host)
    try:
        url = f"{terraina_url}/smarthome/device/getUserBindDevices"
        async with session.post(url, headers=headers, timeout=ClientTimeout(total=8)) as resp:
            body = await resp.text()
            print(f"  POST /smarthome/device/getUserBindDevices  HTTP {resp.status}  {body[:200]}")
    except Exception as exc:
        print(f"  POST getUserBindDevices  ERROR: {exc}")

    if sn:
        for path in endpoints_post_with_sn:
            url = f"{terraina_url}{path}"
            try:
                async with session.post(
                    url,
                    headers=headers,
                    json={"sn": sn, "payload": build_get_device_detail(sn)},
                    timeout=ClientTimeout(total=8),
                ) as resp:
                    body = await resp.text()
                    print(f"  POST {path:50s}  HTTP {resp.status}  {body[:200]}")
            except Exception as exc:
                print(f"  POST {path:50s}  ERROR: {exc}")


async def step1_get_devices(
    session: aiohttp.ClientSession, base_url: str, access_token: str
) -> list[dict]:
    """Validate token and retrieve device list."""
    url = f"{base_url}/smarthome/device/getUserBindDevices"
    headers = _platform_headers(access_token)
    _LOG.debug("POST %s", url)

    async with session.post(url, headers=headers, timeout=TIMEOUT) as resp:
        body = await resp.json(content_type=None)

    code = body.get("code")
    msg  = body.get("message", "")
    print(f"  HTTP {resp.status}  api_code={code}  message={msg!r}")

    if resp.status == 401 or code == 401:
        sys.exit(
            "FAIL: Token rejected (401).  "
            "The access_token is expired — refresh it via HA or re-run OAuth."
        )
    if code != 200:
        sys.exit(f"FAIL: getUserBindDevices returned unexpected code={code}: {body}")

    devices: list[dict] = body.get("data", {}).get("deviceList", [])
    print(f"  Found {len(devices)} device(s):")
    for d in devices:
        print(f"    sn={d.get('sn')}  name={d.get('deviceName')!r}  model={d.get('modelName')!r}")

    # Dump full device payload — may contain status fields the original code ignores
    print("\n  Full device payload (all fields):")
    print("  " + json.dumps(devices, indent=2, ensure_ascii=False).replace("\n", "\n  "))
    return devices


async def _send_message(
    session: aiohttp.ClientSession,
    base_url: str,
    access_token: str,
    sn: str,
    payload_b64: str,
) -> dict:
    url = f"{base_url}/smarthome/message/send"
    headers = _platform_headers(access_token)
    body = {"sn": sn, "payload": payload_b64}
    _LOG.debug("POST %s  body=%s", url, json.dumps(body)[:120])
    async with session.post(url, json=body, headers=headers, timeout=TIMEOUT) as resp:
        resp_body = await resp.json(content_type=None)
    return {"http_status": resp.status, "body": resp_body}


async def step2_get_device_detail(
    session: aiohttp.ClientSession, base_url: str, access_token: str, sn: str
) -> None:
    """Send getDeviceDetail via REST and inspect the response."""
    payload_b64 = build_get_device_detail(sn)
    print(f"  Payload (b64, first 60): {payload_b64[:60]}…")

    result = await _send_message(session, base_url, access_token, sn, payload_b64)
    http_st = result["http_status"]
    body    = result["body"]
    code    = body.get("code")
    print(f"  HTTP {http_st}  api_code={code}  message={body.get('message', '')!r}")
    print(f"  Full response: {json.dumps(body, ensure_ascii=False)}")

    # Try to parse an embedded payload in the response
    resp_payload = (body.get("data") or {}).get("payload")
    if resp_payload:
        parsed = parse_device_message_b64(resp_payload)
        print("\n  Parsed response DeviceMessage:")
        print("  " + json.dumps(parsed, indent=2, ensure_ascii=False).replace("\n", "\n  "))
        status_int = _find_status_int(parsed.get("params", {}))
        if status_int is not None:
            print(f"\n  *** Status integer found: {status_int} (0x{status_int:04X}) ***")
            _print_decoded_bits(status_int)
        else:
            print("\n  NOTE: No integer status value found in response params.")
    else:
        print(
            "\n  NOTE: Response contains no embedded payload.\n"
            "        getDeviceDetail uses the gRPC bidirectional stream (DeviceConnect/Message)\n"
            "        to push device state — REST message/send is fire-and-forget.\n"
            "        Use --test-grpc to attempt gRPC state query (requires grpcio>=1.72)."
        )


async def step3_set_work_status(
    session: aiohttp.ClientSession,
    base_url: str,
    access_token: str,
    sn: str,
    status: int,
    label: str,
) -> None:
    payload_b64 = build_set_work_status(sn, status)
    print(f"  Sending setWorkStatus(status={status}={label!r})")
    print(f"  Payload (b64, first 60): {payload_b64[:60]}…")

    result = await _send_message(session, base_url, access_token, sn, payload_b64)
    http_st = result["http_status"]
    body    = result["body"]
    code    = body.get("code")
    print(f"  HTTP {http_st}  api_code={code}  message={body.get('message', '')!r}")
    print(f"  Full response: {json.dumps(body, ensure_ascii=False)}")

    if code == 200:
        print(f"  PASS: Command accepted by API.")
    elif code == 401:
        print("  FAIL: Token expired (401).")
    else:
        print(f"  WARN: Unexpected api_code={code} — may still have been delivered.")


# ---------------------------------------------------------------------------
# Optional gRPC state query
# ---------------------------------------------------------------------------

def _try_grpc_one(
    channel,
    path: str,
    rpc_type: str,
    messages: list,
    metadata: list,
    timeout_sec: int,
    label: str,
) -> bool:
    """Try one gRPC call variant. Returns True if it succeeded (no RpcError)."""
    import grpc
    try:
        if rpc_type == "stream_stream":
            callable_ = channel.stream_stream(
                path,
                request_serializer=StreamIn.SerializeToString,
                response_deserializer=StreamOut.FromString,
            )
            it = callable_(iter(messages), metadata=metadata, timeout=timeout_sec, wait_for_ready=False)
        elif rpc_type == "unary_stream":
            callable_ = channel.unary_stream(
                path,
                request_serializer=StreamIn.SerializeToString,
                response_deserializer=StreamOut.FromString,
            )
            it = callable_(messages[0], metadata=metadata, timeout=timeout_sec, wait_for_ready=False)
        elif rpc_type == "stream_unary":
            callable_ = channel.stream_unary(
                path,
                request_serializer=StreamIn.SerializeToString,
                response_deserializer=StreamOut.FromString,
            )
            resp = callable_(iter(messages), metadata=metadata, timeout=timeout_sec, wait_for_ready=False)
            print(f"      stream_unary response: type={getattr(resp,'type','?')!r}  sm={getattr(resp,'sm','?')!r}")
            return True
        else:
            return False

        count = 0
        for out in it:
            count += 1
            print(f"      ← Out[{count}]: type={out.type!r}  sm={out.sm!r}")
            if out.payload:
                parsed = parse_device_message_b64(out.payload)
                print("        Payload: " + json.dumps(parsed, indent=2).replace("\n", "\n        "))
                status_int = _find_status_int(parsed.get("params", {}))
                if status_int is not None:
                    print(f"\n      *** Status integer: {status_int} (0x{status_int:04X}) ***")
                    _print_decoded_bits(status_int)
            if count >= 5:
                print("      (stopping after 5 messages)")
                break
        print(f"      PASS ({rpc_type}): {count} message(s) received in {timeout_sec}s")
        return True

    except grpc.RpcError as exc:
        print(f"      RpcError [{rpc_type}]: {exc.code()} — {exc.details()}")
        return False
    except Exception as exc:
        print(f"      Error [{rpc_type}]: {type(exc).__name__}: {exc}")
        return False


def _try_grpc_query(region: str, access_token: str, sn: str, timeout_sec: int = 15) -> None:
    """
    Phase 1b gRPC probe — uses the dedicated stream host discovered via APK analysis.

    Hosts (from libapp.so strings):
      EU : iot-streams-eu-prod.dongcheng.ink:443
    Paths:
      /DeviceConnect/Downstream  — server → client state push
      /DeviceConnect/UpMessage   — client → server commands
    """
    try:
        import grpc
    except ImportError:
        print("  SKIP: grpcio not installed.  Run:  pip install 'grpcio>=1.72'")
        return

    grpc_host = REGION_GRPC_HOST.get(region, REGION_GRPC_HOST["eu"])
    target = f"{grpc_host}:443"
    print(f"  gRPC target : {target}")
    print(f"  Paths       : {GRPC_PATH_DOWNSTREAM}  |  {GRPC_PATH_UPMESSAGE}")

    query_payload = build_get_device_detail(sn)
    heartbeat_msg = StreamIn(type="heartbeat", sn=sn, payload="")
    query_msg     = StreamIn(type="device",    sn=sn, payload=query_payload)
    connect_msg   = StreamIn(type="connect",   sn=sn, payload="")

    def _hmac_token_as_key(iso: str = "AT") -> list[tuple[str, str]]:
        """Variant: access_token used as HMAC signing key instead of CLIENT_SECRET."""
        ts = str(int(time.time()))
        nonce = str(uuid.uuid4())
        params = {"client_id": CLIENT_ID, "iso_country": iso, "nonce": nonce,
                  "phone_os": "android", "timestamp": ts}
        msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = _hmac_sha256_hex(msg, access_token)   # token as key!
        return [
            ("authorization",            f"Bearer {access_token}"),
            ("x-platform-client-id",     CLIENT_ID),
            ("x-platform-iso-country",   iso),
            ("x-platform-phone-os",      "android"),
            ("x-platform-nonce",         nonce),
            ("x-platform-timestamp",     ts),
            ("x-platform-signature",     sig),
        ]

    auth_variants = [
        ("token-as-key iso=AT",        _hmac_token_as_key("AT")),
        ("token-as-key iso=DE",        _hmac_token_as_key("DE")),
        ("token-as-key iso=''",        _hmac_token_as_key("")),
        ("5-param HMAC+Bearer iso=AT", _grpc_metadata_hmac(access_token, iso_country="AT")),
        ("5-param HMAC+Bearer iso=''", _grpc_metadata_hmac(access_token, iso_country="")),
        ("Bearer-only",                _grpc_metadata_bearer_only(access_token)),
    ]

    # Combinations to try: (path, rpc_type, messages_list)
    call_variants = [
        (GRPC_PATH_DOWNSTREAM, "stream_stream", [heartbeat_msg, query_msg]),
        (GRPC_PATH_DOWNSTREAM, "unary_stream",  [connect_msg]),
        (GRPC_PATH_DOWNSTREAM, "stream_stream", [connect_msg]),
        (GRPC_PATH_UPMESSAGE,  "stream_unary",  [heartbeat_msg, query_msg]),
        (GRPC_PATH_UPMESSAGE,  "stream_stream", [heartbeat_msg, query_msg]),
    ]

    creds = grpc.ssl_channel_credentials()

    for auth_label, metadata in auth_variants:
        print(f"\n  ── Auth: {auth_label} ──")
        channel = grpc.secure_channel(target, creds)
        try:
            for path, rpc_type, msgs in call_variants:
                print(f"    {path}  [{rpc_type}]")
                success = _try_grpc_one(channel, path, rpc_type, msgs, metadata, timeout_sec, auth_label)
                if success:
                    print(f"\n  *** SUCCESS with auth={auth_label!r}  path={path!r}  type={rpc_type!r} ***")
                    return
        finally:
            channel.close()

    print(
        "\n  NOTE: All gRPC variants returned errors.\n"
        "        Check the RpcError codes above — UNAUTHENTICATED means auth is close,\n"
        "        PERMISSION_DENIED / NOT_FOUND means path or host may differ.\n"
        "        Next step: mitmproxy on the patched APK to capture live traffic."
    )


# ---------------------------------------------------------------------------
# Offline bit-decode utility
# ---------------------------------------------------------------------------

def _print_decoded_bits(value: int) -> None:
    decoded = split_bits(value)
    width = max(len(k) for k in decoded)
    print(f"  {'Field':<{width}}   Value")
    print(f"  {'-'*width}   -----")
    for k, v in decoded.items():
        if k.startswith("raw_"):
            continue
        marker = " ◄" if k == "working_status" else ""
        print(f"  {k:<{width}}   {v}{marker}")
    print(f"  {'raw_decimal':<{width}}   {decoded['raw_decimal']}")
    print(f"  {'raw_hex':<{width}}   {decoded['raw_hex']}")
    print(f"  {'raw_binary':<{width}}   {decoded['raw_binary']}")


# ---------------------------------------------------------------------------
# Argument parsing & main
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Phase 0 Validation — TERRAINA KDRM210/220",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Token source (mutually exclusive)
    tg = p.add_mutually_exclusive_group()
    tg.add_argument("--token",      metavar="TOKEN",
                    help="OAuth2 access_token string")
    tg.add_argument("--ha-storage", metavar="PATH",
                    help="Path to HA .storage/core.config_entries")
    tg.add_argument("--token-file", metavar="PATH",
                    help="JSON file with access_token (and optionally region)")

    p.add_argument("--region",  default="eu", choices=list(REGION_BASE_URL),
                   help="API region (default: eu)")
    p.add_argument("--sn",      metavar="SERIAL",
                   help="Device serial number (auto-selects first device if omitted)")

    # Actions
    p.add_argument("--dry-run",     action="store_true",
                   help="Validate auth/status only; skip all write commands")
    p.add_argument("--confirm-mow", action="store_true",
                   help="Execute setWorkStatus(3=mowing)  *** STARTS THE MOWER ***")
    p.add_argument("--confirm-rest", action="store_true",
                   help="Execute setWorkStatus(7=resting) *** STOPS THE MOWER ***")
    p.add_argument("--skip-detail", action="store_true",
                   help="Skip Step 2 (getDeviceDetail REST probe)")
    p.add_argument("--test-grpc",   action="store_true",
                   help="Also attempt gRPC DeviceConnect/Message state query (needs grpcio)")

    # Utility
    p.add_argument("--decode-bits", metavar="INT", type=lambda s: int(s, 0),
                   help="Offline: decode a status integer and exit  (e.g. --decode-bits 0x1C0)")
    p.add_argument("--verbose",     action="store_true",
                   help="Enable DEBUG logging")
    return p


async def _run(args: argparse.Namespace) -> None:
    # --- Token resolution ---
    region = args.region
    access_token = ""

    if args.token:
        access_token = args.token
        print(f"[token]  Source : --token argument")
    elif args.ha_storage:
        tok_data = load_token_from_ha_storage(args.ha_storage)
        access_token = tok_data["access_token"]
        region = tok_data.get("region", region)
    elif args.token_file:
        tok_data = load_token_from_file(args.token_file)
        access_token = tok_data["access_token"]
        region = tok_data.get("region", region)
    else:
        sys.exit(
            "ERROR: Provide one of --token, --ha-storage, or --token-file.\n"
            "Run with --help for usage."
        )

    if not access_token:
        sys.exit("ERROR: access_token is empty.  Token may need refreshing.")

    base_url = REGION_BASE_URL[region]
    tok_preview = f"{access_token[:20]}…{access_token[-8:]}" if len(access_token) > 28 else access_token

    print()
    print("=" * 60)
    print("  TERRAINA Phase 0 Validation")
    print("=" * 60)
    print(f"  Region    : {region}")
    print(f"  Base URL  : {base_url}")
    print(f"  Token     : {tok_preview}")
    print(f"  Dry-run   : {args.dry_run}")
    print(f"  Mow       : {args.confirm_mow}")
    print(f"  Rest      : {args.confirm_rest}")
    print(f"  gRPC test : {args.test_grpc}")
    print("=" * 60)

    async with aiohttp.ClientSession() as session:
        # ------------------------------------------------------------------
        print("\n[ Step 1 ] Validate token — getUserBindDevices")
        print("-" * 60)
        devices = await step1_get_devices(session, base_url, access_token)
        if not devices:
            sys.exit("FAIL: Device list is empty — check account binding.")
        print("  PASS: Token is valid, devices returned.")

        # Resolve serial number
        sn = args.sn or str(devices[0]["sn"])
        print(f"\n  Using SN: {sn}")

        # ------------------------------------------------------------------
        print("\n[ Step 1b ] Probe TERRAINA-specific REST host (Phase 1b discovery)")
        print("-" * 60)
        await probe_terraina_host(session, access_token, region, sn)

        # ------------------------------------------------------------------
        if not args.skip_detail:
            print("\n[ Step 2 ] getDeviceDetail — REST probe")
            print("-" * 60)
            await step2_get_device_detail(session, base_url, access_token, sn)

        # ------------------------------------------------------------------
        if args.test_grpc:
            print("\n[ Step 2b ] getDeviceDetail — gRPC probe (Phase 1b)")
            print("-" * 60)
            _try_grpc_query(region, access_token, sn)

        # ------------------------------------------------------------------
        print("\n[ Step 3 ] setWorkStatus(3 = mowing)")
        print("-" * 60)
        if args.dry_run:
            print("  SKIP (--dry-run)")
        elif args.confirm_mow:
            print("  *** WARNING: Sending START MOWING command NOW ***")
            await step3_set_work_status(session, base_url, access_token, sn, 3, "mowing")
        else:
            print("  SKIP — add --confirm-mow to actually execute this command.")

        # ------------------------------------------------------------------
        print("\n[ Step 4 ] setWorkStatus(7 = resting)")
        print("-" * 60)
        if args.dry_run:
            print("  SKIP (--dry-run)")
        elif args.confirm_rest:
            await step3_set_work_status(session, base_url, access_token, sn, 7, "resting")
        else:
            print("  SKIP — add --confirm-rest to actually execute this command.")

    print("\n" + "=" * 60)
    print("  Validation complete.")
    print("=" * 60)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Offline-only mode
    if args.decode_bits is not None:
        print(f"\nDecoding status integer: {args.decode_bits} (0x{args.decode_bits:04X})")
        print("-" * 50)
        _print_decoded_bits(args.decode_bits)
        return

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
