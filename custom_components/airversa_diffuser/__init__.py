"""The Airversa Scenta Diffuser integration."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_KEEPALIVE,
    CONF_MODEL,
    CONF_PIN,
    CONF_SYNC_STATE,
    DEFAULT_KEEPALIVE,
    DEFAULT_SYNC_STATE,
    DOMAIN,
)
from .diffuser import AirversaDiffuser
from .protocol import (
    DEFAULT_PIN,
    firmware_from_manufacturer_data,
    model_from_name,
)
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-wide services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Airversa Scenta Diffuser from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    keepalive: float = entry.options.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE)

    model: str | None = entry.data.get(CONF_MODEL)
    firmware: str | None = None
    service_info = bluetooth.async_last_service_info(
        hass, address.upper(), connectable=True
    )
    if service_info is not None:
        model = model or model_from_name(service_info.name)
        firmware = firmware_from_manufacturer_data(
            service_info.manufacturer_data
        )
    if model and entry.data.get(CONF_MODEL) != model:
        # Done before the update listener exists, so this does not reload.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MODEL: model}
        )

    diffuser = AirversaDiffuser(
        hass,
        address,
        keepalive,
        pin=entry.options.get(CONF_PIN, DEFAULT_PIN),
        model=model,
        sync_state=entry.options.get(CONF_SYNC_STATE, DEFAULT_SYNC_STATE),
        advertised_firmware=firmware,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = diffuser

    @callback
    def _on_model(reported: str) -> None:
        # Name-based detection failed but the device told us; store it and
        # reload so the model-specific entities appear.
        if entry.data.get(CONF_MODEL):
            return
        _LOGGER.debug("%s: device reported model %s", address, reported)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MODEL: reported}
        )

    # Listeners go in before the link starts so an early model frame is
    # not missed.
    entry.async_on_unload(diffuser.add_model_listener(_on_model))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await diffuser.async_start()
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await diffuser.async_stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        diffuser: AirversaDiffuser = hass.data[DOMAIN].pop(entry.entry_id)
        await diffuser.async_stop()
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload the entry when its options or stored model change."""
    await hass.config_entries.async_reload(entry.entry_id)
