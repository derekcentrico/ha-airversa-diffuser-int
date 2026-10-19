"""Config and options flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.airversa_diffuser.const import DOMAIN


@pytest.fixture(autouse=True)
def no_setup():
    with (
        patch(
            "custom_components.airversa_diffuser.async_setup_entry",
            AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.airversa_diffuser.config_flow.async_discovered_service_info",
            return_value=[],
        ),
        patch(
            "homeassistant.components.bluetooth_adapters.async_setup",
            return_value=True,
        ),
    ):
        yield


async def test_manual_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"address": "not-a-mac"}
    )
    assert result["errors"] == {"address": "invalid_address"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"address": "24-42-e3-60-f8-7e", "name": "Hall"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Hall"
    assert result["data"] == {"address": "24:42:E3:60:F8:7E"}


async def test_bluetooth_discovery_stores_model(hass: HomeAssistant) -> None:
    info = AsyncMock()
    info.address = "24:42:E3:60:F8:7E"
    info.name = "SP1ANP_E360F87E"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["model"] == "ANP"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"address": "24:42:E3:60:F8:7E"}, unique_id="x"
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"keepalive_seconds": 3.0, "pin": "1234", "sync_state": False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "keepalive_seconds": 3.0,
        "pin": "1234",
        "sync_state": False,
    }
