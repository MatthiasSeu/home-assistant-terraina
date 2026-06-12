# TERRAINA Community Integration for Home Assistant

Version: 1.1.0

A community fork of [DCK-China/home-assistant-terraina](https://github.com/DCK-China/home-assistant-terraina) with extended functionality.

## What's new in this fork

- **`lawn_mower` platform** — native HA lawn mower entity with Start / Dock / Pause controls
- **Real-time gRPC state stream** — device state updates within seconds via the TERRAINA IoT stream endpoint, no polling delay
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

The integration stores credentials securely in the HA config entry. If a re-authentication is needed, a notification will appear in the HA UI.

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `lawn_mower.<name>` | Lawn Mower | Mowing state, Start / Dock / Pause controls |

**Lawn Mower states:** Mowing · Returning · Paused · Docked · Error

## Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.terraina_community: debug
```

## Troubleshooting

- **Re-authentication dialog** — appears when the session has been invalidated (e.g. by the phone app). Enter email + password again to restore the gRPC stream.
- **No state updates** — check that the gRPC stream is connected: `docker logs home-assistant 2>&1 | grep grpc_stream`
- **My Home Assistant** — the integration uses OAuth2 via My HA. Ensure `https://my.home-assistant.io` is reachable from your instance.

## Architecture

- OAuth2 tokens (`ory_at_`) managed by HA's built-in OAuth2 flow
- Platform tokens (`kk5fd5Ce`) obtained via password grant and refreshed automatically
- gRPC bidirectional stream: `/DeviceConnect/Downstream` at `iot-streams-<region>-prod.dongcheng.ink:443`
- Device status decoded from a 15-bit integer field (`split_bits()`)

## Credits

Based on [DCK-China/home-assistant-terraina](https://github.com/DCK-China/home-assistant-terraina) · Apache-2.0 licence
