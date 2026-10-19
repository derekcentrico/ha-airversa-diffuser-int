"""Config flow for the Airversa Scenta Diffuser integration."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_KEEPALIVE,
    CONF_MODEL,
    CONF_PIN,
    CONF_SYNC_STATE,
    DEFAULT_KEEPALIVE,
    DEFAULT_SYNC_STATE,
    DOMAIN,
)
from .protocol import DEFAULT_PIN, model_from_name

DEFAULT_NAME = "Airversa Diffuser"

_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def _title(name: str | None, address: str) -> str:
    """Build a friendly entry title."""
    if name and name not in ("", address):
        return name
    return f"{DEFAULT_NAME} ({address})"


class AirversaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Airversa Scenta Diffuser."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._discovery: BluetoothServiceInfoBleak | None = None
        # address -> human readable label, for the manual picker
        self._discovered: dict[str, str] = {}

    # ------------------------------------------------ automatic BT discovery
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a flow started by Bluetooth discovery."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {
            "name": _title(discovery_info.name, discovery_info.address)
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a single discovered device."""
        assert self._discovery is not None
        if user_input is not None:
            return self._async_create(
                self._discovery.address,
                self._discovery.name,
                model_from_name(self._discovery.name),
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": _title(self._discovery.name, self._discovery.address)
            },
        )

    # ----------------------------------------------------- manual / picker
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the manual setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = (
                user_input[CONF_ADDRESS].strip().upper().replace("-", ":")
            )
            if not _MAC_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(
                    format_mac(address), raise_on_progress=False
                )
                self._abort_if_unique_id_configured()
                return self._async_create(address, user_input.get(CONF_NAME))

        # Offer any currently-discovered, not-yet-configured devices as hints.
        current_addrs = {
            entry.unique_id for entry in self._async_current_entries()
        }
        for info in async_discovered_service_info(self.hass, connectable=True):
            if format_mac(info.address) in current_addrs:
                continue
            self._discovered[info.address] = (
                f"{info.name or 'Unknown'} ({info.address})"
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): str,
                vol.Optional(CONF_NAME): str,
            }
        )
        placeholder = (
            "\n".join(sorted(self._discovered.values()))
            if self._discovered
            else "none seen yet"
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"discovered": placeholder},
        )

    # --------------------------------------------------------------- helper
    @callback
    def _async_create(
        self, address: str, name: str | None, model: str | None = None
    ) -> ConfigFlowResult:
        data: dict[str, Any] = {CONF_ADDRESS: address.upper()}
        if model:
            data[CONF_MODEL] = model
        return self.async_create_entry(title=_title(name, address), data=data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> AirversaOptionsFlow:
        """Return the options flow handler."""
        return AirversaOptionsFlow()


class AirversaOptionsFlow(OptionsFlow):
    """Handle Airversa Scenta Diffuser options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage keepalive, PIN and state sync options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_KEEPALIVE,
                    default=options.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=30.0)),
                vol.Optional(
                    CONF_PIN, default=options.get(CONF_PIN, DEFAULT_PIN)
                ): vol.All(str, vol.Match(r"^[0-9]{4,8}$")),
                vol.Optional(
                    CONF_SYNC_STATE,
                    default=options.get(CONF_SYNC_STATE, DEFAULT_SYNC_STATE),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
