"""Setup, entity and service tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.airversa_diffuser import protocol as p
from custom_components.airversa_diffuser.const import CONF_MODEL, DOMAIN
from custom_components.airversa_diffuser.diffuser import AirversaDiffuser

ADDRESS = "24:42:E3:60:F8:7E"
MODE_PRESET_2 = bytes.fromhex("5b000002000000000000000500d2")


class _Info:
    def __init__(self, name: str) -> None:
        self.name = name
        self.manufacturer_data = {0x5943: bytes(11) + bytes([11, 1, 2])}


@pytest.fixture
def no_connect():
    with (
        patch.object(AirversaDiffuser, "async_start", AsyncMock()),
        patch.object(AirversaDiffuser, "async_stop", AsyncMock()),
        patch(
            "homeassistant.components.bluetooth_adapters.async_setup",
            return_value=True,
        ),
    ):
        yield


async def _setup(
    hass: HomeAssistant, adv_name: str | None, data: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Scenta Plug",
        unique_id=ADDRESS.lower(),
        data={"address": ADDRESS, **(data or {})},
    )
    entry.add_to_hass(hass)
    info = _Info(adv_name) if adv_name else None
    with patch(
        "custom_components.airversa_diffuser.bluetooth.async_last_service_info",
        return_value=info,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _diffuser(hass: HomeAssistant, entry: MockConfigEntry) -> AirversaDiffuser:
    return hass.data[DOMAIN][entry.entry_id]


async def test_anp_entities(hass: HomeAssistant, no_connect) -> None:
    entry = await _setup(hass, "SP1ANP_E360F87E")
    assert entry.data[CONF_MODEL] == "ANP"
    registry = er.async_get(hass)
    ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert ids == {
        ADDRESS,
        f"{ADDRESS}_feedback_sound",
        f"{ADDRESS}_intensity",
        f"{ADDRESS}_mode",
        f"{ADDRESS}_phase",
    }
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), entry.entry_id
    )
    assert device.model == "Scenta Plug (ANP)"
    assert device.sw_version == "11.1.2"


async def test_unknown_model_keeps_legacy_layout(
    hass: HomeAssistant, no_connect
) -> None:
    """Existing installs without a detectable model see only the power switch."""
    entry = await _setup(hass, None)
    registry = er.async_get(hass)
    ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert ids == {ADDRESS}
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), entry.entry_id
    )
    assert device.model == "Scenta SP3M"


async def test_power_switch_states(hass: HomeAssistant, no_connect) -> None:
    entry = await _setup(hass, "SP1ANP_E360F87E")
    diffuser = _diffuser(hass, entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, ADDRESS
    )

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    diffuser._connected = True
    diffuser._notify_listeners()
    assert hass.states.get(entity_id).state == STATE_UNKNOWN

    diffuser._on_notify(None, bytearray.fromhex("4d01"))
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["state_source"] == "device"
    assert not state.attributes.get("assumed_state", False)


async def test_switch_reports_assumed_state_after_command(
    hass: HomeAssistant, no_connect
) -> None:
    entry = await _setup(hass, "SP1ANP_E360F87E")
    diffuser = _diffuser(hass, entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, ADDRESS
    )

    diffuser._connected = True
    diffuser._power = True
    diffuser._power_source = "command"
    diffuser._notify_listeners()
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["assumed_state"] is True


async def test_sound_unavailable_until_power_known(
    hass: HomeAssistant, no_connect
) -> None:
    entry = await _setup(hass, "SP1ANP_E360F87E")
    diffuser = _diffuser(hass, entry)
    diffuser._connected = True
    diffuser._notify_listeners()
    sound_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{ADDRESS}_feedback_sound"
    )
    # Unknown power -> the sound switch stays unavailable.
    assert hass.states.get(sound_id).state == STATE_UNAVAILABLE


async def test_selects_and_phase(hass: HomeAssistant, no_connect) -> None:
    entry = await _setup(hass, "SP1ANP_E360F87E")
    diffuser = _diffuser(hass, entry)
    registry = er.async_get(hass)
    intensity = registry.async_get_entity_id(
        "select", DOMAIN, f"{ADDRESS}_intensity"
    )
    mode = registry.async_get_entity_id("select", DOMAIN, f"{ADDRESS}_mode")
    phase = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_phase")

    diffuser._connected = True
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    diffuser._on_notify(None, bytearray.fromhex("590109000500d200000001031f"))
    assert hass.states.get(intensity).state == "2"
    assert hass.states.get(mode).state == "preset"
    assert hass.states.get(phase).state == "misting"
    assert hass.states.get(phase).attributes["slot"] == 1

    with patch.object(diffuser, "async_set_grade", AsyncMock()) as set_grade:
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": intensity, "option": "3"},
            blocking=True,
        )
    set_grade.assert_awaited_once_with(3)


async def test_select_error_surfaces(hass: HomeAssistant, no_connect) -> None:
    entry = await _setup(hass, "SP1ANP_E360F87E")
    diffuser = _diffuser(hass, entry)
    diffuser._connected = True
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    mode = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{ADDRESS}_mode"
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": mode, "option": "custom"},
            blocking=True,
        )


async def test_model_reported_later_is_stored(
    hass: HomeAssistant, no_connect
) -> None:
    entry = await _setup(hass, None)
    diffuser = _diffuser(hass, entry)
    with patch(
        "custom_components.airversa_diffuser.bluetooth.async_last_service_info",
        return_value=None,
    ):
        diffuser._on_notify(None, bytearray.fromhex("45414e50"))
        await hass.async_block_till_done()
    assert entry.data[CONF_MODEL] == "ANP"


async def test_set_schedule_service(hass: HomeAssistant, no_connect) -> None:
    entry = await _setup(hass, "SP1ANP_E360F87E")
    diffuser = _diffuser(hass, entry)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), entry.entry_id
    )

    # No login reply yet -> custom timing support unknown.
    with pytest.raises(ServiceValidationError, match="custom"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {
                "device_id": device.id,
                "slot": 1,
                "start": "07:00",
                "end": "22:30",
                "mist": 10,
                "pause": 100,
            },
            blocking=True,
        )
    diffuser.snapshot.login = p.parse_login_reply(
        bytes.fromhex("8f43595f56332e304f4b30310145c210")
    )

    with pytest.raises(ServiceValidationError, match="not both"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {
                "device_id": device.id,
                "slot": 1,
                "start": "07:00",
                "end": "22:30",
                "intensity": 2,
                "pause": 100,
            },
            blocking=True,
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {"device_id": device.id, "slot": 1, "mist": 10, "pause": 100},
            blocking=True,
        )

    with patch.object(diffuser, "async_set_schedule", AsyncMock()) as write:
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {
                "device_id": device.id,
                "slot": 1,
                "start": "07:00",
                "end": "22:30",
                "days": ["mon", "fri"],
                "mist": 10,
                "pause": 120,
            },
            blocking=True,
        )
    schedule = write.await_args.args[0]
    assert schedule.start == (7, 0) and schedule.end == (22, 30)
    assert schedule.days == 0x22
    assert (schedule.work_mode, schedule.mist, schedule.pause) == (1, 10, 120)

    with pytest.raises(ServiceValidationError, match="pause"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {
                "device_id": device.id,
                "slot": 1,
                "start": "07:00",
                "end": "22:30",
                "mist": 20,
                "pause": 60,
            },
            blocking=True,
        )


async def test_diagnostics_redacts(hass: HomeAssistant, no_connect) -> None:
    from custom_components.airversa_diffuser.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(hass, "SP1ANP_E360F87E")
    _diffuser(hass, entry)._on_notify(
        None, bytearray.fromhex("590109000500d200000001031f")
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"]["address"] == "**REDACTED**"
    assert diag["snapshot"]["status"]["raw_tail"] == "00000001031f"


async def test_unload(hass: HomeAssistant, no_connect) -> None:
    entry = await _setup(hass, "SP1ANP_E360F87E")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.entry_id not in hass.data[DOMAIN]
