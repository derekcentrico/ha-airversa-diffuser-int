"""Select platform: intensity level and operating mode (ANP)."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MODE_MODELS
from .diffuser import AirversaDiffuser, DiffuserError
from .entity import AirversaEntity
from .protocol import Mode

# Modes the vendor app offers on the ANP. The countdown timer mode exists in
# the frame format but the app never exposes it.
_MODE_OPTIONS = {
    "preset": Mode.PRESET,
    "custom": Mode.CUSTOM,
    "light_sensor": Mode.LIGHT,
}
_MODE_BY_VALUE = {value: key for key, value in _MODE_OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up selects for models with a mode frame."""
    diffuser: AirversaDiffuser = hass.data[DOMAIN][entry.entry_id]
    if diffuser.model not in MODE_MODELS:
        return
    async_add_entities(
        [
            AirversaIntensitySelect(diffuser, entry),
            AirversaModeSelect(diffuser, entry),
        ]
    )


class _ModeBackedSelect(AirversaEntity, SelectEntity):
    """Select that needs the device's mode frame before it can write."""

    @property
    def available(self) -> bool:
        """Return True once the mode frame has been received."""
        return super().available and self._diffuser.snapshot.mode is not None

    async def _async_run(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except DiffuserError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()


class AirversaIntensitySelect(_ModeBackedSelect):
    """Preset intensity level. Choosing one switches the device to preset mode."""

    _attr_translation_key = "intensity"

    def __init__(self, diffuser: AirversaDiffuser, entry: ConfigEntry) -> None:
        """Initialise the select."""
        super().__init__(diffuser, entry)
        self._attr_unique_id = f"{diffuser.address}_intensity"

    @property
    def options(self) -> list[str]:
        """Return one option per level the device reports."""
        return [str(level) for level in range(1, self._diffuser.max_grade + 1)]

    @property
    def current_option(self) -> str | None:
        """Return the active level while in preset mode."""
        mode = self._diffuser.snapshot.mode
        if mode is None or mode.mode != Mode.PRESET:
            return None
        option = str(mode.grade)
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Change the intensity level."""
        await self._async_run(self._diffuser.async_set_grade(int(option)))


class AirversaModeSelect(_ModeBackedSelect):
    """Preset levels, custom schedules or the light-sensor mode."""

    _attr_translation_key = "mode"
    _attr_options = list(_MODE_OPTIONS)

    def __init__(self, diffuser: AirversaDiffuser, entry: ConfigEntry) -> None:
        """Initialise the select."""
        super().__init__(diffuser, entry)
        self._attr_unique_id = f"{diffuser.address}_mode"

    @property
    def current_option(self) -> str | None:
        """Return the active mode."""
        mode = self._diffuser.snapshot.mode
        if mode is None:
            return None
        return _MODE_BY_VALUE.get(mode.mode)

    async def async_select_option(self, option: str) -> None:
        """Switch the operating mode."""
        await self._async_run(
            self._diffuser.async_set_mode(_MODE_OPTIONS[option])
        )
