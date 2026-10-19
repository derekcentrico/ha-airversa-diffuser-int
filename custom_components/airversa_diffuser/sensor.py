"""Sensor platform: current diffusion phase from the status frame."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STATUS_MODELS
from .diffuser import AirversaDiffuser
from .entity import AirversaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for models known to send status frames."""
    diffuser: AirversaDiffuser = hass.data[DOMAIN][entry.entry_id]
    if diffuser.model not in STATUS_MODELS:
        return
    async_add_entities([AirversaPhaseSensor(diffuser, entry)])


class AirversaPhaseSensor(AirversaEntity, SensorEntity):
    """Idle, misting or paused, as reported by the device."""

    _attr_translation_key = "phase"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "misting", "paused"]

    def __init__(self, diffuser: AirversaDiffuser, entry: ConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(diffuser, entry)
        self._attr_unique_id = f"{diffuser.address}_phase"

    @property
    def native_value(self) -> str | None:
        """Return the current phase."""
        status = self._diffuser.snapshot.status
        return None if status is None else status.phase

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the active slot (schedule number or preset)."""
        status = self._diffuser.snapshot.status
        if status is None:
            return None
        return {"slot": status.slot}
