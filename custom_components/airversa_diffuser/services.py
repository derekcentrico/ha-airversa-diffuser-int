"""Integration services.

``set_schedule`` writes one native schedule slot. On the ANP this is the only
place the vendor app exposes custom mist and pause times, so it doubles as the
mist/pause control. The device runs these schedules itself, which keeps
working when Home Assistant is down; plain automations are simpler otherwise.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time

import voluptuous as vol

from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
)

from .const import DOMAIN
from .diffuser import AirversaDiffuser, DiffuserError
from .protocol import Schedule, pause_range

SERVICE_SET_SCHEDULE = "set_schedule"

ATTR_SLOT = "slot"
ATTR_ENABLED = "enabled"
ATTR_START = "start"
ATTR_END = "end"
ATTR_DAYS = "days"
ATTR_MIST = "mist"
ATTR_PAUSE = "pause"
ATTR_INTENSITY = "intensity"

# Bit 0 is Sunday in the device's repeat mask.
_DAY_BITS = {
    "sun": 0x01,
    "mon": 0x02,
    "tue": 0x04,
    "wed": 0x08,
    "thu": 0x10,
    "fri": 0x20,
    "sat": 0x40,
}
MAX_SLOTS = 5

SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SLOT): vol.All(
            vol.Coerce(int), vol.Range(1, MAX_SLOTS)
        ),
        vol.Optional(ATTR_ENABLED): cv.boolean,
        vol.Optional(ATTR_START): cv.time,
        vol.Optional(ATTR_END): cv.time,
        vol.Optional(ATTR_DAYS): vol.All(
            cv.ensure_list, [vol.In(list(_DAY_BITS))]
        ),
        vol.Exclusive(ATTR_MIST, "timing"): vol.All(
            vol.Coerce(int), vol.Range(1, 3600)
        ),
        vol.Optional(ATTR_PAUSE): vol.All(vol.Coerce(int), vol.Range(1, 3600)),
        vol.Exclusive(ATTR_INTENSITY, "timing"): vol.All(
            vol.Coerce(int), vol.Range(1, 9)
        ),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services."""

    async def _set_schedule(call: ServiceCall) -> None:
        diffuser = _diffuser_for_device(hass, call.data[ATTR_DEVICE_ID])
        schedule = build_schedule_from_call(diffuser, call.data)
        try:
            await diffuser.async_set_schedule(schedule)
        except DiffuserError as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _set_schedule, schema=SET_SCHEDULE_SCHEMA
    )


def _diffuser_for_device(
    hass: HomeAssistant, device_id: str
) -> AirversaDiffuser:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"unknown device {device_id}")
    for entry_id in device.config_entries:
        diffuser = hass.data.get(DOMAIN, {}).get(entry_id)
        if diffuser is not None:
            return diffuser
    raise ServiceValidationError("device is not an Airversa diffuser")


def _hm(value: time) -> tuple[int, int]:
    return value.hour, value.minute


def build_schedule_from_call(
    diffuser: AirversaDiffuser, data: dict
) -> Schedule:
    """Merge service data onto the slot the device last reported."""
    slot = data[ATTR_SLOT]
    current = diffuser.snapshot.schedules.get(slot)
    if current is None:
        # Unknown slot: refuse to guess times that could clobber a schedule
        # the app created but the device has not reported yet.
        if ATTR_START not in data or ATTR_END not in data:
            raise ServiceValidationError(
                f"slot {slot} has not been read from the device; "
                "give both start and end"
            )
        current = Schedule(
            index=slot,
            enabled=True,
            shown=True,
            start=(0, 0),
            end=(23, 0),
            days=0x7F,
            work_mode=1,
            grade=1,
            mist=10,
            pause=100,
        )

    schedule = replace(current, shown=True)
    if ATTR_ENABLED in data:
        schedule.enabled = data[ATTR_ENABLED]
    if ATTR_START in data:
        schedule.start = _hm(data[ATTR_START])
    if ATTR_END in data:
        schedule.end = _hm(data[ATTR_END])
    if ATTR_DAYS in data:
        mask = 0
        for day in data[ATTR_DAYS]:
            mask |= _DAY_BITS[day]
        if not mask:
            raise ServiceValidationError("pick at least one day")
        schedule.days = mask
    if ATTR_INTENSITY in data:
        if data[ATTR_INTENSITY] > diffuser.max_grade:
            raise ServiceValidationError(
                f"intensity must be 1-{diffuser.max_grade}"
            )
        schedule.work_mode = 0
        schedule.grade = data[ATTR_INTENSITY]
    if ATTR_MIST in data or ATTR_PAUSE in data:
        schedule.work_mode = 1
        if ATTR_MIST in data:
            schedule.mist = data[ATTR_MIST]
        if ATTR_PAUSE in data:
            schedule.pause = data[ATTR_PAUSE]
    _validate_timing(diffuser, schedule)
    return schedule


def _validate_timing(diffuser: AirversaDiffuser, schedule: Schedule) -> None:
    if schedule.work_mode != 1:
        return
    if schedule.mist is None or schedule.pause is None:
        raise ServiceValidationError("custom timing needs both mist and pause")
    limits = diffuser.snapshot.limits
    if (
        limits is not None
        and limits.on_max > 0
        and not limits.on_min <= schedule.mist <= limits.on_max
    ):
        raise ServiceValidationError(
            f"mist must be {limits.on_min}-{limits.on_max} seconds"
        )
    bounds = pause_range(diffuser.model, schedule.mist)
    if bounds is not None:
        low, high = bounds
        if not low <= schedule.pause <= high:
            raise ServiceValidationError(
                f"pause must be {low}-{high} seconds for a "
                f"{schedule.mist} second mist"
            )
