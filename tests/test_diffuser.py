"""Connection manager tests with a scripted fake BLE client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.airversa_diffuser import (
    diffuser as diffuser_mod,
    protocol as p,
)
from custom_components.airversa_diffuser.diffuser import (
    SOURCE_COMMAND,
    SOURCE_DEVICE,
    AirversaDiffuser,
    DiffuserError,
)

ANP_LOGIN = bytes.fromhex("8f43595f56332e304f4b30310145c210")
ANP_IDLE = bytes.fromhex("590100000500d200000001031f")
ANP_MIST = bytes.fromhex("590109000500d200000001031f")
ANP_MIST_TICK = bytes.fromhex("590109000400d200000001031f")
MODE_PRESET_2 = bytes.fromhex("5b000002000000000000000500d2")

Responder = Callable[[bytes], list[bytes]]


class FakeClient:
    """Records writes and replays scripted notifications."""

    def __init__(
        self, diffuser: AirversaDiffuser, responder: Responder
    ) -> None:
        self.diffuser = diffuser
        self.responder = responder
        self.writes: list[bytes] = []
        self.is_connected = True

    async def write_gatt_char(
        self, _uuid: str, data: bytes, response: bool
    ) -> None:
        self.writes.append(bytes(data))
        loop = asyncio.get_running_loop()
        for reply in self.responder(bytes(data)):
            loop.call_soon(self.diffuser._on_notify, None, bytearray(reply))

    async def start_notify(self, *_args) -> None:
        return None

    async def stop_notify(self, *_args) -> None:
        return None

    async def disconnect(self) -> None:
        self.is_connected = False


def single_byte_acks(frame: bytes) -> list[bytes]:
    """ANP style: a bare ACK head for every tracked command."""
    if frame[0] < 0x80 and frame != p.KEEPALIVE:
        return [bytes([frame[0] | 0x80])]
    return []


def silent(_frame: bytes) -> list[bytes]:
    return []


@pytest.fixture
def fast_timeouts():
    with (
        patch.object(diffuser_mod, "ACK_TIMEOUT", 0.05),
        patch.object(diffuser_mod, "LOGIN_TIMEOUT", 0.05),
        patch.object(diffuser_mod, "SYNC_TIMEOUT", 0.1),
        patch.object(diffuser_mod, "HANDSHAKE_SETTLE", 0),
        patch.object(diffuser_mod, "KEEPALIVE_SETTLE", 0),
    ):
        yield


def make(
    hass: HomeAssistant, responder: Responder, model: str | None = "ANP"
) -> tuple[AirversaDiffuser, FakeClient]:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model=model)
    client = FakeClient(diffuser, responder)
    diffuser._client = client
    diffuser._connected = True
    return diffuser, client


async def test_single_byte_ack_confirms_power(hass: HomeAssistant) -> None:
    """Regression for upstream issue #1: a bare 'ad' now confirms the command."""
    diffuser, client = make(hass, single_byte_acks)
    await diffuser.async_turn_on()
    assert client.writes == [bytes.fromhex("2d13")]
    assert diffuser.is_on is True
    assert diffuser.power_source == SOURCE_COMMAND
    assert not diffuser.power_reported

    await diffuser.async_turn_off()
    assert diffuser.is_on is False


async def test_legacy_echo_ack(hass: HomeAssistant) -> None:
    def legacy(frame: bytes) -> list[bytes]:
        return (
            [bytes([frame[0] | 0x80]) + frame[1:]] if frame[0] == 0x2D else []
        )

    diffuser, _ = make(hass, legacy, model=None)
    await diffuser.async_turn_on()
    assert diffuser.is_on is True
    await diffuser.async_turn_off()
    assert diffuser.is_on is False


async def test_missing_ack_leaves_state_and_retries(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser, client = make(hass, silent)
    await diffuser.async_turn_on()
    assert diffuser.is_on is None
    assert client.writes == [bytes.fromhex("2d13")] * 3


async def test_late_ack_after_retry_matches(
    hass: HomeAssistant, fast_timeouts
) -> None:
    attempts = 0

    def second_try(frame: bytes) -> list[bytes]:
        nonlocal attempts
        attempts += 1
        return [b"\xad"] if attempts == 2 else []

    diffuser, client = make(hass, second_try)
    await diffuser.async_turn_on()
    assert diffuser.is_on is True
    assert len(client.writes) == 2


async def test_unrelated_ack_does_not_confirm(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser, _ = make(hass, lambda f: [b"\xbb"])
    await diffuser.async_turn_on()
    assert diffuser.is_on is None


async def test_rapid_toggles_are_serialised(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, single_byte_acks)
    await asyncio.gather(
        diffuser.async_turn_on(),
        diffuser.async_turn_off(),
        diffuser.async_turn_on(),
    )
    assert client.writes == [
        bytes.fromhex("2d13"),
        bytes.fromhex("2d12"),
        bytes.fromhex("2d13"),
    ]
    assert diffuser.is_on is True


async def test_control_push_is_authoritative(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, silent)
    diffuser.snapshot.login = p.parse_login_reply(ANP_LOGIN)
    diffuser._on_notify(None, bytearray.fromhex("4d53"))
    await hass.async_block_till_done()
    assert diffuser.is_on is True
    assert diffuser.muted is True
    assert diffuser.power_source == SOURCE_DEVICE
    assert diffuser.power_reported
    # reply flag set in the login reply -> host acks the push
    assert client.writes == [bytes.fromhex("cd")]


async def test_push_not_acked_without_reply_flag(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, silent)
    diffuser._on_notify(None, bytearray.fromhex("4d13"))
    await hass.async_block_till_done()
    assert client.writes == []


async def test_status_frames_never_acked(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, silent)
    diffuser.snapshot.login = p.parse_login_reply(ANP_LOGIN)
    diffuser._on_notify(None, bytearray(ANP_IDLE))
    await hass.async_block_till_done()
    assert client.writes == []
    assert diffuser.snapshot.status.phase == "idle"


async def test_status_countdown_does_not_spam_listeners(
    hass: HomeAssistant,
) -> None:
    diffuser, _ = make(hass, silent)
    calls = 0

    def listener() -> None:
        nonlocal calls
        calls += 1

    diffuser.add_listener(listener)
    for frame in (ANP_IDLE, ANP_MIST, ANP_MIST_TICK, ANP_MIST_TICK, ANP_IDLE):
        diffuser._on_notify(None, bytearray(frame))
    assert calls == 3  # idle, misting, idle


async def test_status_does_not_change_power(hass: HomeAssistant) -> None:
    """The status slot is not proof of power; only 0x4d is."""
    diffuser, _ = make(hass, silent)
    diffuser._on_notify(None, bytearray(ANP_MIST))
    assert diffuser.is_on is None


async def test_sound_needs_known_power(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, single_byte_acks)
    with pytest.raises(DiffuserError):
        await diffuser.async_set_sound(False)
    assert client.writes == []


async def test_sound_keeps_power_bit(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, single_byte_acks)
    diffuser._on_notify(None, bytearray.fromhex("4d01"))
    await diffuser.async_set_sound(False)
    assert client.writes[-1] == bytes.fromhex("2d53")
    assert diffuser.muted is True
    await diffuser.async_turn_off()
    # Turning off must not silently unmute.
    assert client.writes[-1] == bytes.fromhex("2d52")


async def test_power_preserves_reported_mute(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, single_byte_acks)
    diffuser._on_notify(None, bytearray.fromhex("4d40"))
    await diffuser.async_turn_on()
    assert client.writes[-1] == bytes.fromhex("2d53")


async def test_grade_needs_mode_frame(hass: HomeAssistant) -> None:
    diffuser, _ = make(hass, single_byte_acks)
    with pytest.raises(DiffuserError):
        await diffuser.async_set_grade(2)


async def test_grade_write(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, single_byte_acks)
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    await diffuser.async_set_grade(4)
    assert client.writes[-1] == bytes.fromhex("3b000004000000000000000500d2")
    assert diffuser.snapshot.mode.grade == 4
    with pytest.raises(DiffuserError):
        await diffuser.async_set_grade(5)


async def test_mode_confirmed_by_push_when_no_ack(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """Firmware 11.1.2 does not ack 0x3b; the 0x5b push confirms instead."""

    def push_back(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_MODE:
            return [bytes([p.PUSH_MODE]) + frame[1:]]
        return []

    diffuser, _ = make(hass, push_back)
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    await diffuser.async_set_mode(p.Mode.CUSTOM)
    assert diffuser.snapshot.mode.mode == p.Mode.CUSTOM


async def test_mode_unconfirmed_raises(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser, _ = make(hass, silent)
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    with pytest.raises(DiffuserError):
        await diffuser.async_set_mode(p.Mode.CUSTOM)


async def test_disconnect_fails_pending(hass: HomeAssistant) -> None:
    diffuser, client = make(hass, silent)
    task = asyncio.create_task(diffuser.async_turn_on())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    client.is_connected = False
    diffuser._on_disconnect(client)
    assert not diffuser.connected
    with pytest.raises(DiffuserError):
        await task


async def test_queue_power_while_disconnected(hass: HomeAssistant) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0)
    await diffuser.async_turn_on()
    assert diffuser._queued_power is True
    assert diffuser.is_on is None


async def test_connect_flow_login_sync_and_ack(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """Login, clock sync, device dump, acks, then keepalive."""
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model="ANP")

    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_LOGIN:
            return [ANP_LOGIN]
        if frame[0] == p.CMD_TIME:
            return [b"\xa1", bytes.fromhex("4d01"), MODE_PRESET_2]
        return []

    client = FakeClient(diffuser, device)
    diffuser._stop.set()  # exit the keepalive loop right after setup

    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
        await hass.async_block_till_done()

    heads = [w[0] for w in client.writes]
    assert heads[0] == p.CMD_LOGIN
    assert heads[1] == p.CMD_TIME
    assert bytes.fromhex("cd") in client.writes  # acked 0x4d
    assert bytes.fromhex("db") in client.writes  # acked 0x5b
    assert p.KEEPALIVE in client.writes
    assert diffuser.connected
    assert diffuser.firmware == "CY_V3.0"
    assert diffuser.is_on is True and diffuser.power_reported
    assert diffuser.snapshot.mode.grade == 2
    # Device reported state, so nothing is re-applied.
    assert not any(w[0] == p.CMD_POWER for w in client.writes)


async def test_connect_does_not_reapply_over_reported_state(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """A button press while disconnected must survive the reconnect."""
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model="ANP")
    diffuser._desired = True

    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_LOGIN:
            return [ANP_LOGIN]
        if frame[0] == p.CMD_TIME:
            return [bytes.fromhex("4d00"), MODE_PRESET_2]
        return single_byte_acks(frame)

    client = FakeClient(diffuser, device)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert diffuser.is_on is False
    assert not any(w[0] == p.CMD_POWER for w in client.writes)


async def test_connect_reapplies_for_silent_device(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """Devices that never report keep the old re-apply behaviour."""
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model=None)
    diffuser._desired = False
    client = FakeClient(diffuser, single_byte_acks)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert bytes.fromhex("2d12") in client.writes
    assert diffuser.is_on is False


async def test_connect_sends_queued_power(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model="ANP")
    await diffuser.async_turn_off()

    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_LOGIN:
            return [ANP_LOGIN]
        if frame[0] == p.CMD_TIME:
            return [bytes.fromhex("4d01"), MODE_PRESET_2]
        return single_byte_acks(frame)

    client = FakeClient(diffuser, device)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert bytes.fromhex("2d12") in client.writes
    assert diffuser._queued_power is None


async def test_pin_rejected(hass: HomeAssistant, fast_timeouts) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, pin="1234")

    def device(frame: bytes) -> list[bytes]:
        return [b"\x8fERROR"] if frame[0] == p.CMD_LOGIN else []

    client = FakeClient(diffuser, device)
    with (
        patch.object(
            diffuser_mod, "establish_connection", return_value=client
        ),
        pytest.raises(DiffuserError, match="PIN"),
    ):
        await diffuser._async_connect_and_maintain(object())
    assert client.writes[0] == bytes.fromhex("8f313233344f4b3031")
    assert not diffuser.connected


async def test_sync_disabled_skips_clock(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser = AirversaDiffuser(
        hass, "24:42:e3:60:f8:7e", 2.0, model="ANP", sync_state=False
    )

    def device(frame: bytes) -> list[bytes]:
        return [ANP_LOGIN] if frame[0] == p.CMD_LOGIN else []

    client = FakeClient(diffuser, device)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert not any(w[0] == p.CMD_TIME for w in client.writes)


async def test_model_listener(hass: HomeAssistant) -> None:
    diffuser, _ = make(hass, silent, model=None)
    seen: list[str] = []
    diffuser.add_model_listener(seen.append)
    diffuser._on_notify(None, bytearray.fromhex("45414e50"))
    assert seen == ["ANP"]
    assert diffuser.model == "ANP"
