"""Constants for TERRAINA Community integration."""

DOMAIN = "terraina_community"
NAME = "TERRAINA Community"
VERSION = "1.0.0"

GLOBAL_DOMAIN = "https://iot-platform-global-prod.dongcheng.ink"

SERVER_DOMAIN_NAME = {
    "cn": "https://iot-platform-cn-prod.dongcheng.ink",
    "eu": "https://iot-platform-eu-prod.dongcheng.ink",
    "us": "https://iot-platform-us-prod.dongcheng.ink",
}
CLIENT_ID = "AfAsyMwEMPDf1I5CTfIc9G2a7LT5YnQCAxLm5rVlx992mNh7B9spEVpK"
CLIENT_SECRET = "rqFcjZXa9oT2"

PLATFORM_CLIENT_ID = "200001"
PLATFORM_CLIENT_SECRET = "rqFcjZXa9oT2"

GRPC_HOST = {
    "cn": "iot-streams-cn-prod.dongcheng.ink:443",
    "eu": "iot-streams-eu-prod.dongcheng.ink:443",
    "us": "iot-streams-us-prod.dongcheng.ink:443",
}
APP_TOKEN_URL = {
    "cn": "https://iot-platform-cn-prod.dongcheng.ink/user-center/token",
    "eu": "https://iot-platform-eu-prod.dongcheng.ink/user-center/token",
    "us": "https://iot-platform-us-prod.dongcheng.ink/user-center/token",
}
GRPC_HEARTBEAT_INTERVAL = 30
GRPC_RECONNECT_DELAY = 10

# Bit-field decode tables for split_bits()
LOCKED = ["unlocked", "locked"]
EMERGENCY_STOP = ["no emergency stop", "emergency stop"]
CHARGING = ["not in basestation", "charging", "fully charged"]
RAINED = ["not rained", "being rained", "being rained and delayed"]
WORKING_STATUS = [
    "",                     # 0x00
    "hanging",              # 0x01  待机
    "building graph",       # 0x02
    "mowing",               # 0x03
    "backing",              # 0x04
    "backing with low power",  # 0x05
    "locating",             # 0x06
    "resting",              # 0x07
    "error",                # 0x08
    "offline",              # 0x09
    "",                     # 0x0A
    "leaving basestation",  # 0x0B
    "",                     # 0x0C
]
OTA = ["available", "upgrading"]
LOG = ["available", "uploading"]
TASK = ["available", "assigned"]
WORKING_MODE = ["auto", "manual"]
BINDING = ["not binding", "binding"]
