# TERRAINA Community Integration for Home Assistant

Version: 1.3.12

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
- **Smart Weather Protection** — `binary_sensor.<name>_safe_to_mow` computed from rain hold, weather forecast (hourly or daily with automatic fallback), and temperature threshold; configurable via Options dialog; auto-docks the mower when conditions become unsafe (optional)

## Supported devices

TERRAINA / DCK KDRM210, KDRM220 (and compatible models using the DCK IoT platform).

## Installation

### HACS (recommended)

1. In HACS, go to **⋮ → Custom Repositories**.
2. Add `https://github.com/MatthiasSeu/home-assistant-terraina` as an **Integration**.
3. Search for **TERRAINA Community** and install it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration** and search for **TERRAINA Community**.

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
| `sensor.<name>_cutting_height` | Sensor | Current AI cutting height (read from device) |
| `number.<name>_cutting_height` | Number | Set cutting height via slider |
| `select.<name>_working_mode` | Select | Working mode: `auto` / `manual` |
| `switch.<name>_rain_sensor` | Switch | Enable / disable the device's built-in rain sensor |
| `number.<name>_rain_delay` | Number | Device rain delay in hours (1–3 h) |
| `sensor.<name>_schedule_monday` … `_schedule_sunday` | Sensor (×7) | Mowing time window(s) per weekday, e.g. `10:00 - 20:30`; supports multiple slots |
| `switch.<name>_1_mon_enabled` … `7_sun_enabled` | Switch (×7) | Enable / disable mowing for each weekday |
| `time.<name>_1_mon_start` / `1_mon_stop` … | Time (×14) | Start and stop time per weekday |
| `sensor.<name>_error` | Sensor | Current error code, e.g. `E05 — Cutting motor error`; `None` when no error |
| `sensor.<name>_rain_status` | Sensor | Rain detection state: `Dry`, `Raining`, or `Rain delay active` |
| `binary_sensor.<name>_safe_to_mow` | Binary Sensor | `On` = conditions are safe to mow; `Off` = blocked by rain hold, forecast rain, or high temperature. Attributes expose all configured Smart Protection thresholds. |

### Lawn Mower states

| HA state | Device condition |
|----------|-----------------|
| Mowing | Actively cutting, leaving base station, building map, or locating |
| Returning | Returning to dock / low-power return / completing manual task |
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
| `slot` | integer | No | Time slot index for this day (0-based, default `0`). Use `slot: 1` to add/update a second mowing window on the same day. |
| `enabled` | boolean | Yes | `true` to enable this slot, `false` to disable |
| `start_time` | string | Yes | Mowing start time in `HH:MM` 24h format |
| `end_time` | string | Yes | Mowing end time in `HH:MM` 24h format |

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

### Example — Three mowing windows on Monday (ideal setup)

```yaml
# Slot 0 — morning
service: terraina_community.set_schedule_day
data:
  week: 1
  slot: 0
  enabled: true
  start_time: "07:00"
  end_time: "10:00"

# Slot 1 — midday
service: terraina_community.set_schedule_day
data:
  week: 1
  slot: 1
  enabled: true
  start_time: "12:00"
  end_time: "14:00"

# Slot 2 — evening
service: terraina_community.set_schedule_day
data:
  week: 1
  slot: 2
  enabled: true
  start_time: "17:00"
  end_time: "20:30"
```

The corresponding schedule sensor will then show:
```
sensor.dck_kdrm210_schedule_monday: "07:00 - 10:00 / 12:00 - 14:00 / 17:00 - 20:30"
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
| Schedule sensors show "Unknown" | Mower in Manual mode | In Manual mode the server does not respond to `getSchedule`. Run the mower once in **Auto** mode — schedule sensors populate on the first Auto run |
| Schedule not updating | Mower idle → server responds slowly | Wait 30 s after mower becomes active in Auto mode, or check debug logs |
| `set_schedule_day` has no effect | Mower is in Manual mode | Switch Working Mode to `auto` first — schedule is ignored in manual mode |
| `set_schedule_day` raises error "Cannot change the schedule while active" | Device rejects schedule changes mid-task | Dock the mower first, then update the schedule |

---

## Smart Weather Protection

Configure via **Settings → Devices & Services → TERRAINA Community → Configure**:

| Setting | Description | Default |
|---------|-------------|---------|
| Precipitation sensor | Entity ID of a daily precipitation sensor (mm) | — |
| Base hold time (h) | Fixed hold added regardless of precipitation amount | 0 |
| Factor (mm/step) | Precipitation per additional hour of hold | 5 mm |
| Step size (h) | Hours added per factor block | 1 h |
| Max HA hold (h) | Cap for the HA-managed hold (device hardware adds its own 1–3 h) | 24 h |
| Forecast entity | Weather entity for rain-in-advance detection | — |
| Forecast window (h) | Hours ahead to check for rain (1–12) | 2 h |
| Temperature sensor | Outdoor temperature entity | — |
| Max temperature (°C) | Dock the mower above this temperature | 32 °C |
| Auto-dock when unsafe | Return to dock immediately when protection triggers | Off |

### Rain hold formula

```
HA extra hold = base_hours + floor(precipitation_mm ÷ rain_factor_mm) × rain_hold_step_hours
Total hold    = HA hold (capped at max) + device hardware delay (1–3 h, configured separately)
```

### `safe_to_mow` attributes

All configured thresholds are visible as attributes on the `binary_sensor.<name>_safe_to_mow` entity — no need to open the Options dialog to check what values are active:

```yaml
blocked_reason: "Rain hold: 2h 14m remaining"
rain_hold_until: "2026-06-11T16:45:00+00:00"
precipitation_sensor: sensor.weatherstation_daily_rain
rain_hold_base_hours: 0
rain_factor_mm: 5
rain_hold_step_hours: 1
rain_hold_max_hours: 24
forecast_entity: weather.forecast_weatherstation
forecast_hours_ahead: 2
temperature_sensor: sensor.outdoor_temperature
max_temperature: 32
auto_dock_unsafe: false
```

### Automation example — notify on protection change

```yaml
automation:
  - alias: "Terraina — notify when mowing blocked"
    trigger:
      - platform: state
        entity_id: binary_sensor.dck_kdrm210_safe_to_mow
        to: "off"
    action:
      - service: notify.mobile_app
        data:
          title: "Mowing blocked"
          message: "{{ state_attr('binary_sensor.dck_kdrm210_safe_to_mow', 'blocked_reason') }}"
```

---

## Architecture

```
HA config entry
  ├── TerrainaCoordinator      (REST polling, device list)
  ├── TerrainaHttpClient       (REST commands: start/dock/pause/set_mode/set_schedule)
  ├── TerrainaGrpcStream       (per device)
  │     ├── heartbeat + getDeviceDetail every 30 s
  │     ├── getSchedule every 5 min
  │     └── _state_cb → entity.update_from_grpc()
  │           ├── TerrainaLawnMower        (state, schedule attributes)
  │           ├── TerrainaBatterySensor
  │           ├── TerrainaWorkingModeSelect
  │           ├── TerrainaRainSensorSwitch
  │           ├── TerrainaRainDelayNumber
  │           └── TerrainaCuttingHeightNumber
  └── SmartProtectionManager   (per device)
        ├── async_track_state_change_event (precipitation + temperature sensor)
        ├── async_track_time_interval (every 10 min)
        ├── weather.get_forecasts service call (hourly → daily fallback)
        └── TerrainaSafeToMowBinarySensor.set_safe_state()
```

- OAuth2 tokens (`ory_at_`) — managed by HA's built-in OAuth2 flow
- Platform tokens (`kk5fd5Ce`) — obtained via password grant, refreshed automatically every 30 min
- Device status — decoded from a 15-bit integer field (`split_bits()` in `grpc_util.py`)
- Schedule proto — uses repeated `Params` with the same key `schedule`, built manually via raw protobuf API

---

## Maintainer

**Matthias Seuchter** — [matthias.seuchter@gmail.com](mailto:matthias.seuchter@gmail.com)

Issues and pull requests welcome via GitHub.

## Contributors

| | Role |
|---|---|
| **Matthias Seuchter** | Product vision, feature ideas, real-world testing on a DCK KDRM210, feedback and direction |
| **Claude (Anthropic)** | Technical implementation — gRPC reverse engineering, all Python code, sensors, services, deploy tooling |

---

## Credits

Based on [DCK-China/home-assistant-terraina](https://github.com/DCK-China/home-assistant-terraina) · Apache-2.0 licence
