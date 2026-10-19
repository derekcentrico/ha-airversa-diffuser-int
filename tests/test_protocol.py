"""Frame encode/decode tests.

Hex fixtures marked "capture" are notifications recorded from real hardware:
an ANP (SP1ANP_*) behind an ESPHome proxy, and the SP2ANA log posted in
upstream issue #1. Frames marked "synthetic" follow the layout in
docs/PROTOCOL.md but have not been seen on hardware yet.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.airversa_diffuser import protocol as p

# capture: ANP login reply after HA restart
ANP_LOGIN = bytes.fromhex("8f43595f56332e304f4b30310145c210")
# capture: SP2ANA login reply (issue #1)
ANA_LOGIN = bytes.fromhex("8f43595f56332e304f4b30310155c2")
# capture: ANP status, idle then slot 1 misting with a countdown
ANP_IDLE = bytes.fromhex("590100000500d200000001031f")
ANP_MIST = [
    bytes.fromhex("590109000500d200000001031f"),
    bytes.fromhex("590109000400d200000001031f"),
    bytes.fromhex("590109000300d200000001031f"),
]
# capture: SP2ANA short status frame
ANA_STATUS = bytes.fromhex("590109000a0000")


def test_login_frame_matches_legacy_handshake() -> None:
    """The default PIN reproduces the handshake older releases hardcoded."""
    assert p.build_login() == bytes.fromhex("8f383838384f4b3031")


def test_login_rejects_non_ascii_pin() -> None:
    with pytest.raises(p.ProtocolError):
        p.build_login("８８８８")


def test_parse_anp_login() -> None:
    login = p.parse_login_reply(ANP_LOGIN)
    assert login.ok
    assert login.firmware == "CY_V3.0"
    assert login.protocol_version == 3.0
    assert login.reply_required is True
    assert login.custom_timing is True
    assert login.oil_tracking is True
    assert login.multi_aroma is False


def test_parse_ana_login() -> None:
    login = p.parse_login_reply(ANA_LOGIN)
    assert login.ok
    assert login.custom_timing is True
    assert login.reply_required is True


def test_parse_login_error() -> None:
    assert not p.parse_login_reply(b"\x8fERROR").ok


def test_parse_login_short_token() -> None:
    login = p.parse_login_reply(b"\x8fOK")
    assert login.ok
    assert login.protocol_version == 2.0
    assert login.reply_required is False


def test_power_frames() -> None:
    assert p.build_power(True, False) == bytes.fromhex("2d13")
    assert p.build_power(False, False) == bytes.fromhex("2d12")
    assert p.build_power(True, True) == bytes.fromhex("2d53")
    assert p.build_power(False, True) == bytes.fromhex("2d52")


def test_ack_head() -> None:
    assert p.ack_head(p.CMD_POWER) == 0xAD
    assert p.ack_head(p.CMD_MODE) == 0xBB


def test_status_idle_capture() -> None:
    status = p.parse_status(ANP_IDLE)
    assert status is not None
    assert status.phase == "idle"
    assert not status.active
    assert status.mist_seconds == 5
    assert status.pause_seconds == 210
    assert status.raw_tail == bytes.fromhex("00000001031f")


def test_status_misting_capture_counts_down() -> None:
    parsed = [p.parse_status(frame) for frame in ANP_MIST]
    assert all(s is not None and s.phase == "misting" for s in parsed)
    assert [s.slot for s in parsed] == [1, 1, 1]
    assert [s.mist_seconds for s in parsed] == [5, 4, 3]


def test_status_short_frame() -> None:
    status = p.parse_status(ANA_STATUS)
    assert status is not None
    assert status.phase == "misting"
    assert status.countdown is None


def test_status_paused_synthetic() -> None:
    status = p.parse_status(bytes.fromhex("590101000500d0"))
    assert status is not None
    assert status.phase == "paused"


def test_status_unknown_subtype() -> None:
    assert p.parse_status(bytes.fromhex("590209000500d2")) is None


def test_status_too_short() -> None:
    with pytest.raises(p.ProtocolError):
        p.parse_status(bytes.fromhex("5901"))


def test_control_synthetic() -> None:
    state = p.parse_control(bytes.fromhex("4d41"))
    assert state.power and state.muted
    state = p.parse_control(bytes.fromhex("4d00"))
    assert not state.power and not state.muted


def test_mode_synthetic() -> None:
    frame = bytes.fromhex("5b000002000000000000000500d2")
    state = p.parse_mode(frame)
    assert state.mode == p.Mode.PRESET
    assert state.grade == 2
    assert state.on_time == 5
    assert state.off_time == 210


def test_build_mode_round_trip() -> None:
    frame = p.build_mode(p.Mode.PRESET, 0, 3, 5, 210)
    assert frame == bytes.fromhex("3b000003000000000000000500d2")
    assert len(frame) == 14


def test_build_mode_range() -> None:
    with pytest.raises(p.ProtocolError):
        p.build_mode(0, 0, 3, 70000, 0)


def test_limits_and_grades_synthetic() -> None:
    limits = p.parse_limits(bytes.fromhex("460400050014003c"))
    assert limits.max_grade == 4
    assert (limits.on_min, limits.on_max, limits.off_min) == (5, 20, 60)
    grades = p.parse_grades(bytes.fromhex("47000500d20005012c"))
    assert [(g.mist, g.pause) for g in grades] == [(5, 210), (5, 300)]


def test_schedule_round_trip() -> None:
    schedule = p.Schedule(
        index=2,
        enabled=True,
        shown=True,
        start=(7, 30),
        end=(22, 0),
        days=0x3E,
        work_mode=1,
        grade=1,
        mist=10,
        pause=120,
    )
    frame = p.build_schedule(schedule, custom_timing=True)
    assert frame.hex() == "2a010201000203071e16003e0101000a0078"
    # Device echoes the same layout with head 0x4a.
    parsed = p.parse_schedule(bytes([p.PUSH_SCHEDULE]) + frame[1:])
    assert parsed.index == 2
    assert parsed.enabled and parsed.shown
    assert parsed.start == (7, 30) and parsed.end == (22, 0)
    assert parsed.days == 0x3E
    assert (parsed.mist, parsed.pause) == (10, 120)


def test_schedule_without_custom_timing_is_short() -> None:
    schedule = p.Schedule(1, True, True, (0, 0), (0, 0), 0x7F, 0, 2)
    assert len(p.build_schedule(schedule, custom_timing=False)) == 14


def test_schedule_validation() -> None:
    bad = p.Schedule(1, True, True, (24, 0), (0, 0), 0x7F, 0, 2)
    with pytest.raises(p.ProtocolError):
        p.build_schedule(bad, custom_timing=False)
    missing = p.Schedule(1, True, True, (1, 0), (2, 0), 0x7F, 1, 1)
    with pytest.raises(p.ProtocolError):
        p.build_schedule(missing, custom_timing=True)


def test_push_ack() -> None:
    assert p.build_push_ack(bytes.fromhex("4d13")) == bytes.fromhex("cd")
    assert p.build_push_ack(ANP_IDLE) is None
    assert p.build_push_ack(bytes.fromhex("ad")) is None
    sched = bytes.fromhex("4a0102010002030700160000000101")
    assert p.build_push_ack(sched) == bytes.fromhex("ca0102")


def test_time_sync() -> None:
    frame = p.build_time_sync(datetime(2026, 10, 18, 21, 5, 9))
    # 2026-10-18 is a Sunday -> weekday 0.
    assert frame == bytes([0x21, 0, 26, 10, 18, 21, 5, 9])


def test_model_from_name() -> None:
    assert p.model_from_name("SP1ANP_E360F87E") == "ANP"
    assert p.model_from_name("SP2ANA_00000779") == "ANA"
    assert p.model_from_name("SP3M1234") == "RDX"
    assert p.model_from_name("Mr. No Stink") is None
    assert p.model_from_name(None) is None


def test_firmware_from_manufacturer_data() -> None:
    payload = bytes(11) + bytes([11, 1, 2])
    assert (
        p.firmware_from_manufacturer_data({p.MANUFACTURER_ID: payload})
        == "11.1.2"
    )
    assert p.firmware_from_manufacturer_data({}) is None


def test_pause_range() -> None:
    assert p.pause_range("ANP", 10) == (60, 3600)
    assert p.pause_range("ANP", 15) == (75, 3600)
    assert p.pause_range("ANA", 10) is None


def test_apply_push_ignores_garbage() -> None:
    snap = p.DeviceSnapshot()
    assert p.apply_push(snap, bytes.fromhex("4d")) is None
    assert p.apply_push(snap, b"") is None
    assert p.apply_push(snap, bytes.fromhex("45414e5000")) == p.PUSH_MODEL
    assert snap.model == "ANP"
