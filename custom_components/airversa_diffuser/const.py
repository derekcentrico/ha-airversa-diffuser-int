"""Constants for the Airversa Scenta Diffuser integration."""

from __future__ import annotations

from .protocol import (  # noqa: F401 - CHAR_UUID/KEEPALIVE re-exported
    CHAR_UUID,
    DEFAULT_PIN,
    KEEPALIVE,
    build_login,
    build_power,
)

DOMAIN = "airversa_diffuser"

# Config / options keys
CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_KEEPALIVE = "keepalive_seconds"
CONF_MODEL = "model"
CONF_PIN = "pin"
CONF_SYNC_STATE = "sync_state"

DEFAULT_KEEPALIVE = 2.0
DEFAULT_SYNC_STATE = True

# Kept for anything importing the old names. Frames now come from protocol.py.
HANDSHAKE = build_login(DEFAULT_PIN)
CMD_ON = build_power(True, False)
CMD_OFF = build_power(False, False)
ACK_ON = bytes.fromhex("ad13")
ACK_OFF = bytes.fromhex("ad12")

# Connection behaviour.
DEVICE_WAIT_TIMEOUT = (
    60.0  # seconds to wait for an advertisement before retrying
)
RECONNECT_DELAY = 5.0  # seconds between reconnect attempts
PIN_REJECTED_DELAY = 60.0  # back off harder when the device refuses the PIN
HANDSHAKE_SETTLE = 0.3  # pause after start_notify before handshake
KEEPALIVE_SETTLE = 0.5  # pause after handshake before first keepalive
LOGIN_TIMEOUT = 3.0
SYNC_TIMEOUT = 6.0
ACK_TIMEOUT = 2.0
COMMAND_ATTEMPTS = 3
QUEUE_TTL = 300.0  # drop a power command queued while offline after this

# Models with a mode/intensity frame (0x3b/0x5b).
MODE_MODELS = frozenset({"ANP"})
# Models where the status frame (0x59) has been seen on real hardware.
STATUS_MODELS = frozenset({"ANP", "ANA"})

# ANP main firmware that applies 0x3b without any reply.
SILENT_MODE_FIRMWARE = "11.1.2"
