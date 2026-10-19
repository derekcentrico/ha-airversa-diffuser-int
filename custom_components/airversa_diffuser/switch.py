"""Switch platform for the Airversa Scenta Diffuser integration."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .diffuser import AirversaDiffuser, DiffuserError
from .entity import AirversaEntity

# Models with a feedback beep that can be muted. ANA/ANW on 1.x firmware
# cannot, so they are only included once a newer firmware is advertised.
_SOUND_MODELS = frozenset({"ANP", "RDX"})
_SOUND_MODELS_NEW_FW = frozenset({"ANA", "ANW"})


def supports_sound(diffuser: AirversaDiffuser) -> bool:
    """Return True when the model has a mutable feedback beep."""
    if diffuser.model in _SOUND_MODELS:
        return True
    firmware = diffuser.advertised_firmware
    return (
        diffuser.model in _SOUND_MODELS_NEW_FW
        and firmware is not None
        and not firmware.startswith("1.")
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diffuser switches from a config entry."""
    diffuser: AirversaDiffuser = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [AirversaDiffuserSwitch(diffuser, entry)]
    if supports_sound(diffuser):
        entities.append(AirversaSoundSwitch(diffuser, entry))
    async_add_entities(entities)


class AirversaDiffuserSwitch(AirversaEntity, SwitchEntity):
    """Power switch for one Airversa Scenta diffuser."""

    _attr_name = None  # use the device name
    _attr_icon = "mdi:scent"

    def __init__(self, diffuser: AirversaDiffuser, entry: ConfigEntry) -> None:
        """Initialise the switch entity."""
        super().__init__(diffuser, entry)
        # Unique id predates the multi-entity layout; keep it so existing
        # entity ids and automations survive the upgrade.
        self._attr_unique_id = diffuser.address

    @property
    def is_on(self) -> bool | None:
        """Return the best known power state."""
        return self._diffuser.is_on

    @property
    def assumed_state(self) -> bool:
        """Return True unless the device itself reported its power state."""
        return not self._diffuser.power_reported

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose where the state came from."""
        return {"state_source": self._diffuser.power_source}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the diffuser on."""
        await self._async_call(self._diffuser.async_turn_on())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the diffuser off."""
        await self._async_call(self._diffuser.async_turn_off())

    async def _async_call(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except DiffuserError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()


class AirversaSoundSwitch(AirversaEntity, SwitchEntity):
    """Feedback beep on button presses and commands."""

    _attr_translation_key = "feedback_sound"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, diffuser: AirversaDiffuser, entry: ConfigEntry) -> None:
        """Initialise the switch entity."""
        super().__init__(diffuser, entry)
        self._attr_unique_id = f"{diffuser.address}_feedback_sound"

    @property
    def available(self) -> bool:
        """Return True once power is known, since mute rides on that frame."""
        return super().available and self._diffuser.power_fresh

    @property
    def is_on(self) -> bool | None:
        """Return True when the beep is enabled."""
        muted = self._diffuser.muted
        return None if muted is None else not muted

    @property
    def assumed_state(self) -> bool:
        """Return True unless the device itself reported the mute flag."""
        return not self._diffuser.muted_reported

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the beep."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Mute the beep."""
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        try:
            await self._diffuser.async_set_sound(enabled)
        except DiffuserError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()
