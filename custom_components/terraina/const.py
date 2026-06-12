"""Constants for Terraina integration."""

DOMAIN = "terraina"
NAME = "TERRAINA"
VERSION = "1.0.0"

GLOBAL_DOMAIN = "https://iot-platform-global-prod.dongcheng.ink"

SERVER_DOMAIN_NAME = {
    "cn": "https://iot-platform-cn-prod.dongcheng.ink",
    "eu": "https://iot-platform-eu-prod.dongcheng.ink",
    "us": "https://iot-platform-us-prod.dongcheng.ink",
}
CLIENT_ID = "AfAsyMwEMPDf1I5CTfIc9G2a7LT5YnQCAxLm5rVlx992mNh7B9spEVpK"

CLIENT_SECRET = ""

# Platform API credentials (used for HMAC signing and app-token exchange)
PLATFORM_CLIENT_ID = "200001"
PLATFORM_CLIENT_SECRET = "rqFcjZXa9oT2"

# gRPC streaming hosts per region
GRPC_HOST = {
    "cn": "iot-streams-cn-prod.dongcheng.ink:443",
    "eu": "iot-streams-eu-prod.dongcheng.ink:443",
    "us": "iot-streams-us-prod.dongcheng.ink:443",
}

# App-token endpoint (kk5fd5Ce format, separate from Ory OAuth2 tokens)
APP_TOKEN_URL = {
    "cn": "https://iot-platform-cn-prod.dongcheng.ink/user-center/token",
    "eu": "https://iot-platform-eu-prod.dongcheng.ink/user-center/token",
    "us": "https://iot-platform-us-prod.dongcheng.ink/user-center/token",
}

# gRPC heartbeat interval in seconds
GRPC_HEARTBEAT_INTERVAL = 30
# Reconnect delay on gRPC error
GRPC_RECONNECT_DELAY = 10

LOCKED = ["locked", "unlocked"]
EMERGENCY_STOP = ["no emergency stop", "emergency stop"]
CHARGING = ["not in basestation", "charging", "fully charged"]
RAINED = ["not rained", "being rained", "being rained and delayed"]
WORKING_STATUS = [
    "",  # 00
    "hanging",  # 01 待机
    "buidling graph",
    "mowing",
    "backing",
    "backing with low power",
    "locating",
    "resting",
    "error",
    "offline",
    "",
    "leaving basestation",
    "",
]
OTA = ["available", "upgrading"]
LOG = ["available", "uploading"]
TASK = ["available", "assigned"]
WORKING_MODE = ["auto", "manual"]
BINDING = ["not binding", "binding"]
