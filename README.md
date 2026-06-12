# TERRAINA Community Integration for Home Assistant

Version: 1.2.0

A community fork of [DCK-China/home-assistant-terraina](https://github.com/DCK-China/home-assistant-terraina) with extended functionality.

## What's new in this fork

- **`lawn_mower` platform** — native HA lawn mower entity with Start / Dock / Pause controls
- **Real-time gRPC state stream** — device state updates within seconds via the TERRAINA IoT stream endpoint, no polling delay
- **Battery sensor** — live battery level (0 / 25 / 50 / 75 / 100 %)
- **Working Mode select** — switch between Auto and Manual mowing mode directly from HA
- **Mowing schedule display** — current weekly schedule visible as entity attributes
- **Mowing schedule editing** — change any day's schedule via an HA service call or automation
- **State restoration** — entities remember their last known state across HA restarts even when the mower is idle/docked
- **Auto-relogin** — when the phone app invalidates the session, HA automatically re-authenticates using stored credentials
- **DataUpdateCoordinator** polling every 30 s as fallback

## Supported devices

TERRAINA / DCK KDRM210, KDRM220 (and compatible models using the DCK IoT platform).

## Installation

### Manual

1. Copy `custom_components/terraina_community/` into `/config/custom_components/` on your Home Assistant instance.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **TERRAINA Community**.

### Deploy script (NAS / container)

```bash
# Step 1 — local machine
bash tools/deploy_to_nas.sh

# Step 2 — on the NAS (as root)
ssh nasadmin@<your-nas>
sudo -i
bash /tmp/nas_install.sh
```

## Configuration

1. Select your country/region.
2. Complete the OAuth2 login (TERRAINA account).
3. Enter your TERRAINA app email and password — required for the gRPC stream and automatic token renewal.

The integration stores credentials securely in the HA config entry. If re-authentication is needed, a notification will appear in the HA UI.

---

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `lawn_mower.<name>` | Lawn Mower | Mowing state + Start / Dock / Pause controls |
| `sensor.<name>_battery` | Sensor | Battery level in % |
| `select.<name>_working_mode` | Select | Working mode: `auto` / `manual` |
| `sensor.<name>_schedule_sunday` … `_schedule_saturday` | Sensor (×7) | Mowing time window per weekday, e.g. `10:00 - 20:30`; `10:00 - 20:30 (off)` when disabled |

### Lawn Mower states

| HA state | Device condition |
|----------|-----------------|
| Mowing | Actively cutting, leaving base station, building map, or locating |
| Returning | Returning to dock / low-power return |
| Paused | Resting / paused |
| Docked | Hanging / charging / fully charged |
| Error | Device error |

### Lawn Mower attributes

The lawn mower entity exposes additional data as attributes (visible in **Developer Tools → States** or any entity card):

| Attribute | Description |
|-----------|-------------|
| `working_status` | Raw working status string (e.g. `mowing`, `hanging`) |
| `charging` | Charging state: `charging`, `fully charged`, or `not charging` |
| `power` | Raw power level 0–4 |
| `working_mode` | 0 = auto, 1 = manual |
| `ai_height` | Cutting height (AI mode) |
| `error_code` | Error code when in error state |
| `schedule` | Weekly mowing schedule (see below) |

#### Schedule attribute example

```yaml
schedule:
  Sun: "10:00 - 20:00"
  Mon: "10:00 - 20:30"
  Tue: "10:00 - 20:30"
  Wed: "10:00 - 20:30"
  Thu: "10:00 - 20:30"
  Fri: "10:00 - 20:30"
  Sat: "12:00 - 20:30 (disabled)"
```

Days with `(disabled)` are configured but currently not active.

> **Note:** The mowing schedule only takes effect when the mower is in **Auto** mode. In Manual mode, the schedule is ignored by the device.

> **Note:** The schedule can only be **read** while the mower is actively mowing. It cannot be changed while a task is running — the device rejects schedule writes mid-task (same limitation as the official app). Dock the mower first, then update the schedule.

---

## Service: `terraina_community.set_schedule_day`

Modify the mowing schedule for a single weekday and immediately send the updated schedule to the device.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | No | Target lawn mower entity. Can be omitted if only one mower is configured. |
| `week` | integer | Yes | Weekday: `0`=Sunday, `1`=Monday, `2`=Tuesday, `3`=Wednesday, `4`=Thursday, `5`=Friday, `6`=Saturday |
| `enabled` | boolean | Yes | `true` to enable mowing on this day, `false` to disable |
| `start_time` | string | Yes | Mowing start time in `HH:MM` format |
| `end_time` | string | Yes | Mowing end time in `HH:MM` format |

### Example — Developer Tools

```yaml
service: terraina_community.set_schedule_day
data:
  week: 6          # Saturday
  enabled: false   # disable Saturday
  start_time: "12:00"
  end_time: "20:30"
```

### Example — Enable Monday, 09:00–21:00

```yaml
service: terraina_community.set_schedule_day
data:
  entity_id: lawn_mower.kdrm210
  week: 1
  enabled: true
  start_time: "09:00"
  end_time: "21:00"
```

### Example — Automation (disable mowing on public holidays)

```yaml
automation:
  - alias: "Disable mowing on public holiday"
    trigger:
      - platform: calendar
        event: start
        entity_id: calendar.public_holidays
    action:
      - service: terraina_community.set_schedule_day
        data:
          week: "{{ now().weekday() + 1 % 7 }}"  # HA weekday → TERRAINA weekday
          enabled: false
          start_time: "10:00"
          end_time: "20:00"
```

### How it works internally

The service:
1. Reads the current schedule from the entity's `_schedule` attribute (fetched via gRPC at startup and refreshed every 5 minutes).
2. Merges the changed day into the full 7-day schedule.
3. Sends the complete schedule to the device via the REST API (`/smarthome/message/send`) using a hand-crafted protobuf payload (the schedule uses repeated proto params with the same key, which requires manual proto building).
4. Updates the entity attributes immediately (optimistic update) without waiting for the next gRPC push.

---

## gRPC state updates

The integration maintains a persistent bidirectional gRPC stream to `iot-streams-<region>-prod.dongcheng.ink:443`. Every **30 seconds** it sends:
- A heartbeat
- A `getDeviceDetail` query (returns mowing state, battery, working mode)

Every **5 minutes** it additionally sends:
- A `getSchedule` query (returns the weekly mowing schedule)

The server also **pushes** state changes proactively (e.g. when the mower starts or stops mowing). Because the server only pushes when the device is active, the integration uses HA state restoration so entities always show the last known value after a HA restart — even if the mower is docked and silent.

---

## Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.terraina_community: debug
```

Useful log grep commands (adjust container name as needed):

```bash
# Stream status and gRPC messages
docker logs home-assistant 2>&1 | grep -E "gRPC|terraina"

# Schedule updates
docker logs home-assistant 2>&1 | grep -i "schedule"

# Working mode changes
docker logs home-assistant 2>&1 | grep "working_mode\|Working mode"
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Entities show "Unknown" after restart | gRPC server doesn't push state when mower is idle | Normal — entities restore last known state; values update as soon as mower becomes active or within 30 s when gRPC query is answered |
| "Unavailable" for old "Working Mode" sensor | Sensor entity was replaced by the Select entity | Go to **Settings → Entities**, search "Working Mode", delete the old sensor entity |
| Re-authentication dialog | Session invalidated by phone app | Enter email + password again to restore the gRPC stream |
| No state updates | gRPC stream not connected | Check logs: `docker logs home-assistant 2>&1 \| grep grpc_stream` |
| Schedule not updating | Mower idle → server responds slowly | Wait 30 s after mower becomes active, or check debug logs |
| `set_schedule_day` has no effect | Mower is in Manual mode | Switch Working Mode to `auto` first — schedule is ignored in manual mode |
| `set_schedule_day` raises error "Cannot change the schedule while active" | Device rejects schedule changes mid-task | Dock the mower first, then update the schedule |

---

## Architecture

```
HA config entry
  ├── TerrainaCoordinator      (REST polling, device list)
  ├── TerrainaHttpClient       (REST commands: start/dock/pause/set_mode/set_schedule)
  └── TerrainaGrpcStream       (per device)
        ├── heartbeat + getDeviceDetail every 30 s
        ├── getSchedule every 5 min
        └── _state_cb → entity.update_from_grpc()
              ├── TerrainaLawnMower   (state, schedule attributes)
              ├── TerrainaBatterySensor
              └── TerrainarWorkingModeSelect
```

- OAuth2 tokens (`ory_at_`) — managed by HA's built-in OAuth2 flow
- Platform tokens (`kk5fd5Ce`) — obtained via password grant, refreshed automatically every 30 min
- Device status — decoded from a 15-bit integer field (`split_bits()` in `grpc_util.py`)
- Schedule proto — uses repeated `Params` with the same key `schedule`, built manually via raw protobuf API

---

## Credits

Based on [DCK-China/home-assistant-terraina](https://github.com/DCK-China/home-assistant-terraina) · Apache-2.0 licence
