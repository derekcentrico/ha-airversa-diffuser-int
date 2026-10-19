"""Shared entity base for the Airversa Scenta Diffuser integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceInfo,
)
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .diffuser import AirversaDiffuser
from .protocol import MODEL_NAMES

# Model string used before model detection existed; kept for unknown devices
# so existing device registry entries do not change.
LEGACY_MODEL_NAME = "Scenta SP3M"


def device_info(diffuser: AirversaDiffuser, entry: ConfigEntry) -> DeviceInfo:
    """Build the device registry entry shared by every entity."""
    info = DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, diffuser.address)},
        identifiers={(DOMAIN, diffuser.address)},
        name=entry.title,
        manufacturer="Airversa",
        model=MODEL_NAMES.get(diffuser.model or "", LEGACY_MODEL_NAME),
    )
    if diffuser.model:
        info["model_id"] = diffuser.model
    if diffuser.advertised_firmware:
        info["sw_version"] = diffuser.advertised_firmware
    return info


class AirversaEntity(Entity):
    """Base class: availability and updates come from the connection manager."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, diffuser: AirversaDiffuser, entry: ConfigEntry) -> None:
        """Initialise the entity."""
        self._diffuser = diffuser
        self._attr_device_info = device_info(diffuser, entry)

    @property
    def available(self) -> bool:
        """Return True while a live BLE link is held."""
        return self._diffuser.connected

    async def async_added_to_hass(self) -> None:
        """Subscribe to connection-state updates."""
        self.async_on_remove(self._diffuser.add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
