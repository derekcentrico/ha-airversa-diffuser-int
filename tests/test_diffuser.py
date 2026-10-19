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
    with pytest.raises(DiffuserError, match="no acknowledgement"):
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
    with pytest.raises(DiffuserError):
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


async def test_late_ack_cannot_confirm_next_command(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """A straggler ACK from a failed ON must not confirm the following OFF."""
    writes = 0

    def device(frame: bytes) -> list[bytes]:
        nonlocal writes
        writes += 1
        if writes == 3:
            # Last retry's ACK shows up after its timeout, inside the drain.
            asyncio.get_running_loop().call_later(
                0.07, diffuser._on_notify, None, bytearray(b"\xad")
            )
        return []

    diffuser, _ = make(hass, device)
    with pytest.raises(DiffuserError):
        await diffuser.async_turn_on()
    with pytest.raises(DiffuserError):
        await diffuser.async_turn_off()
    assert diffuser.is_on is None


async def test_stale_queued_power_dropped(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model=None)
    await diffuser.async_turn_on()
    diffuser._queued_at -= diffuser_mod.QUEUE_TTL + 1
    diffuser._desired = None
    client = FakeClient(diffuser, single_byte_acks)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert not any(w[0] == p.CMD_POWER for w in client.writes)


async def test_reconnect_demotes_previous_device_state(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model="ANP")
    diffuser._power = True
    diffuser._power_source = diffuser_mod.SOURCE_DEVICE
    diffuser._muted_source = diffuser_mod.SOURCE_DEVICE
    client = FakeClient(diffuser, silent)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert diffuser.is_on is True
    assert diffuser.power_source == diffuser_mod.SOURCE_PREVIOUS
    assert not diffuser.power_reported
    assert not diffuser.muted_reported


async def test_unsolicited_legacy_ack_ignored_after_report(
    hass: HomeAssistant,
) -> None:
    diffuser, _ = make(hass, silent)
    diffuser._on_notify(None, bytearray.fromhex("4d00"))
    diffuser._on_notify(None, bytearray.fromhex("ad13"))
    assert diffuser.is_on is False
    assert diffuser.power_reported


async def test_pin_rejection_backs_off(hass: HomeAssistant) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0)
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        diffuser._stop.set()

    with (
        patch.object(
            diffuser_mod.bluetooth,
            "async_ble_device_from_address",
            return_value=object(),
        ),
        patch.object(
            diffuser,
            "_async_connect_and_maintain",
            side_effect=diffuser_mod.PinRejectedError(
                "device rejected the PIN"
            ),
        ),
        patch.object(diffuser_mod.asyncio, "sleep", fake_sleep),
    ):
        await diffuser._run()
    assert delays == [diffuser_mod.PIN_REJECTED_DELAY]
    assert diffuser.last_error == "device rejected the PIN"


async def test_transient_error_uses_short_delay(hass: HomeAssistant) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0)
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        diffuser._stop.set()

    with (
        patch.object(
            diffuser_mod.bluetooth,
            "async_ble_device_from_address",
            return_value=object(),
        ),
        patch.object(
            diffuser,
            "_async_connect_and_maintain",
            side_effect=DiffuserError("disconnected"),
        ),
        patch.object(diffuser_mod.asyncio, "sleep", fake_sleep),
    ):
        await diffuser._run()
    assert delays == [diffuser_mod.RECONNECT_DELAY]


async def test_concurrent_sound_and_power_use_fresh_state(
    hass: HomeAssistant,
) -> None:
    """Mute change queued behind OFF must keep the new power bit."""
    diffuser, client = make(hass, single_byte_acks)
    diffuser._on_notify(None, bytearray.fromhex("4d01"))
    await asyncio.gather(
        diffuser.async_turn_off(), diffuser.async_set_sound(False)
    )
    assert client.writes == [bytes.fromhex("2d12"), bytes.fromhex("2d52")]
    assert diffuser.is_on is False
    assert diffuser.muted is True


async def test_legacy_echo_mismatch_applies_echo(hass: HomeAssistant) -> None:
    diffuser, _ = make(hass, lambda f: [bytes.fromhex("ad12")])
    await diffuser.async_turn_on()
    assert diffuser.is_on is False


async def test_sound_refused_with_previous_session_power(
    hass: HomeAssistant,
) -> None:
    diffuser, client = make(hass, single_byte_acks)
    diffuser._power = True
    diffuser._power_source = diffuser_mod.SOURCE_PREVIOUS
    with pytest.raises(DiffuserError, match="not confirmed"):
        await diffuser.async_set_sound(False)
    assert client.writes == []


async def test_invalid_pin_setting_is_config_error(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, pin="")
    client = FakeClient(diffuser, silent)
    with (
        patch.object(
            diffuser_mod, "establish_connection", return_value=client
        ),
        pytest.raises(diffuser_mod.PinRejectedError),
    ):
        await diffuser._async_connect_and_maintain(object())
    assert client.writes == []


async def test_push_during_command_wins(hass: HomeAssistant) -> None:
    """A control push that lands before the ACK is the device's real state."""

    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_POWER:
            return [bytes.fromhex("4d40"), b"\xad"]
        return []

    diffuser, _ = make(hass, device)
    await diffuser.async_turn_on()
    assert diffuser.is_on is False
    assert diffuser.muted is True
    assert diffuser.power_reported


async def test_unexpected_long_ack_uses_request(hass: HomeAssistant) -> None:
    diffuser, _ = make(hass, lambda f: [bytes.fromhex("ad0000")])
    await diffuser.async_turn_on()
    assert diffuser.is_on is True


async def test_reconnect_clears_session_snapshot(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model="ANP")
    diffuser.snapshot.login = p.parse_login_reply(ANP_LOGIN)
    diffuser.snapshot.mode = p.parse_mode(MODE_PRESET_2)
    client = FakeClient(diffuser, silent)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert diffuser.snapshot.login is None
    assert diffuser.snapshot.mode is None


async def test_unsolicited_legacy_ack_ignored(hass: HomeAssistant) -> None:
    diffuser, _ = make(hass, silent)
    diffuser._on_notify(None, bytearray.fromhex("ad13"))
    assert diffuser.is_on is None


async def test_failed_command_not_reapplied(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser, _ = make(hass, silent, model=None)
    with pytest.raises(DiffuserError):
        await diffuser.async_turn_on()
    assert diffuser._desired is None


async def test_direct_command_clears_queue(hass: HomeAssistant) -> None:
    diffuser, _ = make(hass, single_byte_acks)
    diffuser._queued_power = True
    await diffuser.async_turn_off()
    assert diffuser._queued_power is None
    assert diffuser._desired is False


async def test_stale_offline_request_not_reapplied(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """An expired queued request must not come back through re-apply."""
    diffuser = AirversaDiffuser(hass, "24:42:e3:60:f8:7e", 2.0, model=None)
    await diffuser.async_turn_on()
    diffuser._queued_at -= diffuser_mod.QUEUE_TTL + 1
    client = FakeClient(diffuser, single_byte_acks)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert not any(w[0] == p.CMD_POWER for w in client.writes)


async def test_no_reapply_after_sync_timeout_on_reporting_device(
    hass: HomeAssistant, fast_timeouts
) -> None:
    diffuser, _ = make(hass, single_byte_acks)
    diffuser._on_notify(None, bytearray.fromhex("4d00"))
    await diffuser.async_turn_on()
    client = FakeClient(diffuser, silent)
    diffuser._stop.set()
    with patch.object(
        diffuser_mod, "establish_connection", return_value=client
    ):
        await diffuser._async_connect_and_maintain(object())
    assert not any(w[0] == p.CMD_POWER for w in client.writes)


async def test_mismatched_push_does_not_confirm(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """A button press reported mid-command is not the command's reply."""

    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_POWER:
            return [bytes.fromhex("4d00")]
        return []

    diffuser, _ = make(hass, device)
    with pytest.raises(DiffuserError):
        await diffuser.async_turn_on()
    assert diffuser.is_on is False


async def test_ack_after_confirming_push_is_drained(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """The trailing ACK of a push-confirmed ON must not confirm the OFF."""
    sent = 0

    def device(frame: bytes) -> list[bytes]:
        nonlocal sent
        if frame[0] != p.CMD_POWER:
            return []
        sent += 1
        if sent > 1:
            return []
        # ACK trails the push and lands after the caller has its answer.
        asyncio.get_running_loop().call_later(
            0.02, diffuser._on_notify, None, bytearray(b"\xad")
        )
        return [bytes.fromhex("4d01")]

    diffuser, _ = make(hass, device)
    await diffuser.async_turn_on()
    assert diffuser.is_on is True
    with pytest.raises(DiffuserError):
        await diffuser.async_turn_off()
    assert diffuser.is_on is True


async def test_schedule_push_for_other_slot_does_not_confirm(
    hass: HomeAssistant, fast_timeouts
) -> None:
    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_SCHEDULE:
            other = bytearray(frame)
            other[0] = p.PUSH_SCHEDULE
            other[5] = 2
            return [bytes(other)]
        return []

    diffuser, _ = make(hass, device)
    schedule = p.Schedule(
        index=1,
        enabled=True,
        shown=True,
        start=(7, 0),
        end=(22, 0),
        days=0x7F,
        work_mode=0,
        grade=2,
    )
    with pytest.raises(DiffuserError):
        await diffuser.async_set_schedule(schedule)


async def test_disconnect_during_drain_keeps_confirmed_result(
    hass: HomeAssistant, fast_timeouts
) -> None:
    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_POWER:
            asyncio.get_running_loop().call_later(
                0.01, diffuser._fail_pending, "disconnected"
            )
            return [bytes.fromhex("4d01")]
        return []

    diffuser, _ = make(hass, device)
    await diffuser.async_turn_on()
    assert diffuser.is_on is True


async def test_mode_confirmed_by_read_back(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """11.1.2 stays silent after 0x3b; the clock sync dump shows the result."""
    applied: list[bytes] = []

    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_MODE:
            applied.append(bytes([p.PUSH_MODE]) + frame[1:])
            return []
        if frame[0] == p.CMD_TIME and applied:
            return [b"\xa1", applied[-1]]
        return []

    diffuser, client = make(hass, device)
    diffuser.advertised_firmware = "11.1.2"
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    await diffuser.async_set_grade(1)
    assert diffuser.snapshot.mode.grade == 1
    assert sum(w[0] == p.CMD_MODE for w in client.writes) == 1


async def test_mode_read_back_mismatch_raises(
    hass: HomeAssistant, fast_timeouts
) -> None:
    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_TIME:
            return [MODE_PRESET_2]
        return []

    diffuser, _ = make(hass, device)
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    with pytest.raises(DiffuserError, match="mode 0 intensity 2"):
        await diffuser.async_set_grade(1)


async def test_mode_read_back_skips_stale_push(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """An old 0x5b ahead of the dump must not decide the result."""
    applied: list[bytes] = []

    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_MODE:
            applied.append(bytes([p.PUSH_MODE]) + frame[1:])
            return []
        if frame[0] == p.CMD_TIME and applied:
            return [MODE_PRESET_2, applied[-1]]
        return []

    diffuser, _ = make(hass, device)
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    await diffuser.async_set_grade(3)
    assert diffuser.snapshot.mode.grade == 3


async def test_mode_resent_on_firmware_that_acks(
    hass: HomeAssistant, fast_timeouts
) -> None:
    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_TIME:
            return [MODE_PRESET_2]
        return []

    diffuser, client = make(hass, device)
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    with pytest.raises(DiffuserError):
        await diffuser.async_set_mode(p.Mode.CUSTOM)
    assert (
        sum(w[0] == p.CMD_MODE for w in client.writes)
        == diffuser_mod.COMMAND_ATTEMPTS
    )


async def test_concurrent_mode_changes_are_serialised(
    hass: HomeAssistant, fast_timeouts
) -> None:
    def device(frame: bytes) -> list[bytes]:
        if frame[0] == p.CMD_MODE:
            return [bytes([p.PUSH_MODE]) + frame[1:]]
        return []

    diffuser, client = make(hass, device)
    diffuser._on_notify(None, bytearray(MODE_PRESET_2))
    await asyncio.gather(
        diffuser.async_set_grade(1), diffuser.async_set_grade(4)
    )
    modes = [w for w in client.writes if w[0] == p.CMD_MODE]
    assert [w[3] for w in modes] == [1, 4]
    assert diffuser.snapshot.mode.grade == 4


async def test_power_after_queued_dump_uses_request(
    hass: HomeAssistant, fast_timeouts
) -> None:
    """A 0x4d that lands while ON waits for the lock predates the ON."""
    diffuser, _ = make(hass, single_byte_acks)
    diffuser._on_notify(None, bytearray.fromhex("4d52"))
    async with diffuser._cmd_lock:
        task = asyncio.create_task(diffuser.async_turn_on())
        await asyncio.sleep(0)
        # State dump from an earlier read-back, still reporting OFF.
        diffuser._on_notify(None, bytearray.fromhex("4d52"))
    await task
    assert diffuser.is_on is True
    assert diffuser.muted is True
