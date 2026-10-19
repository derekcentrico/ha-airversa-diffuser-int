"""Diagnostics for the Airversa Scenta Diffuser integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS, CONF_PIN, DOMAIN
from .diffuser import AirversaDiffuser

TO_REDACT = {CONF_ADDRESS, CONF_PIN, "unique_id", "title"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return connection and protocol state for a config entry."""
    diffuser: AirversaDiffuser = hass.data[DOMAIN][entry.entry_id]
    snapshot = asdict(diffuser.snapshot)
    status = snapshot.get("status")
    if status is not None:
        status["raw_tail"] = diffuser.snapshot.status.raw_tail.hex()
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "connected": diffuser.connected,
        "last_error": diffuser.last_error,
        "model": diffuser.model,
        "ble_module": diffuser.firmware,
        "advertised_firmware": diffuser.advertised_firmware,
        "power": diffuser.is_on,
        "power_source": diffuser.power_source,
        "muted": diffuser.muted,
        "snapshot": snapshot,
    }
