"""Frame encoding and decoding for the Airversa "AK" BLE protocol.

Pure Python with no Home Assistant imports, so it can be unit tested on its
own. See ``docs/PROTOCOL.md`` for the frame reference and the evidence level
behind each entry.

Framing rules on characteristic ``fff6``:

* Host writes use a head byte below ``0x80``. The device acknowledges with
  ``head | 0x80``: one byte on newer firmware (``ad``), sometimes followed by
  an echo of the payload on older firmware (``ad13``).
* Device pushes use a head byte in ``0x40..0x7f``. When the login reply sets
  the "reply" flag the host answers each push with ``head | 0x80``.
* ``0x59`` is a periodic status frame. It is never acknowledged in either
  direction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000fff6-0000-1000-8000-00805f9b34fb"

# Bluetooth SIG company id used in the advertisement ("CY" little endian).
MANUFACTURER_ID = 0x5943

DEFAULT_PIN = "8888"
PAIR_CODE = "OK01"

KEEPALIVE = bytes.fromhex("e0aa55")

# Host -> device command heads.
CMD_LOGIN = 0x8F
CMD_TIME = 0x21
CMD_POWER = 0x2D
CMD_SCHEDULE = 0x2A
CMD_LIGHT_MODE = 0x3A
CMD_MODE = 0x3B

# Device -> host push heads.
PUSH_NAME = 0x42
PUSH_LABEL = 0x43
PUSH_PCB_VERSION = 0x44
PUSH_MODEL = 0x45
PUSH_LIMITS = 0x46
PUSH_GRADES = 0x47
PUSH_SCHEDULE = 0x4A
PUSH_CONTROL = 0x4D
PUSH_PRODUCT = 0x52
PUSH_STATUS = 0x59
PUSH_LIGHT_MODE = 0x5A
PUSH_MODE = 0x5B

# Power byte bits for CMD_POWER. 0x12 is the base value every app build sends.
_POWER_BASE = 0x12
_POWER_ON = 0x01
_POWER_MUTE = 0x40

# PUSH_CONTROL flag bits.
_CTRL_ENABLE = 0x01
_CTRL_DEMO = 0x04
_CTRL_LAMP = 0x10
_CTRL_MUTE = 0x40

# Pushes the host must not answer: status is unacknowledged, and the product
# frame is only acked by cloud-connected models.
_NO_ACK_PUSHES = frozenset({PUSH_STATUS, PUSH_PRODUCT})

# Advertised name prefix -> model code, in the order the vendor app checks.
_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("SP_", "ANW"),
    ("A32", "ANW"),
    ("SP2", "ANA"),
    ("SP1", "ANP"),
    ("SP3", "RDX"),
    ("SP4", "U1"),
)

MODEL_NAMES = {
    "ANP": "Scenta Plug (ANP)",
    "ANA": "Scenta VAST (ANA)",
    "ANW": "Scenta XL (ANW)",
    "RDX": "Scenta (RDX)",
    "U1": "Scenta XL (U1)",
}

# Models known to speak the "gw" protocol on service ee01 instead of fff0.
UNSUPPORTED_MODELS = frozenset({"U1"})

# App defaults, sent when a custom-timing device gets a preset-level schedule.
DEFAULT_SCHEDULE_MIST = 10
DEFAULT_SCHEDULE_PAUSE = 100

_VERSION_RE = re.compile(r"V(\d+\.\d+)")

ANP_PAUSE_MIN = 60
ANP_PAUSE_MIN_LONG_MIST = 75
ANP_LONG_MIST = 15
PAUSE_MAX = 3600


class Mode(IntEnum):
    """Operating modes reported in PUSH_MODE (ANP only)."""

    PRESET = 0
    CUSTOM = 1
    TIMER = 2
    LIGHT = 3


class ProtocolError(ValueError):
    """Raised when a frame is malformed or a value is out of range."""


def model_from_name(name: str | None) -> str | None:
    """Map an advertised local name to a model code."""
    if not name:
        return None
    for prefix, model in _MODEL_PREFIXES:
        if name.startswith(prefix):
            return model
    return None


def firmware_from_manufacturer_data(data: dict[int, bytes]) -> str | None:
    """Extract the firmware version from advertisement manufacturer data."""
    payload = data.get(MANUFACTURER_ID)
    if payload is None or len(payload) < 14:
        return None
    return f"{payload[11]}.{payload[12]}.{payload[13]}"


def ack_head(command: int) -> int:
    """Return the head byte the device uses to acknowledge ``command``."""
    return command | 0x80


# --------------------------------------------------------------- encoders
def build_login(pin: str = DEFAULT_PIN) -> bytes:
    """Build the login frame (PIN followed by the fixed pairing code)."""
    if not pin.isascii() or not pin:
        raise ProtocolError("PIN must be non-empty ASCII")
    return bytes([CMD_LOGIN]) + (pin + PAIR_CODE).encode("ascii")


def build_time_sync(now: datetime) -> bytes:
    """Build the clock sync frame. Fields are binary, weekday 0 is Sunday."""
    weekday = (now.isoweekday()) % 7
    return bytes(
        [
            CMD_TIME,
            weekday,
            now.year % 100,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
        ]
    )


def build_power(on: bool, muted: bool) -> bytes:
    """Build the power frame. Mute rides on the same byte."""
    value = _POWER_BASE | (_POWER_ON if on else 0)
    if muted:
        value |= _POWER_MUTE
    return bytes([CMD_POWER, value])


def build_mode(
    mode: int, work_mode: int, grade: int, on_time: int, off_time: int
) -> bytes:
    """Build the mode/intensity frame (ANP)."""
    for value, upper, label in (
        (mode, 0xFF, "mode"),
        (work_mode, 0xFF, "work_mode"),
        (grade, 0xFF, "grade"),
        (on_time, 0xFFFF, "on_time"),
        (off_time, 0xFFFF, "off_time"),
    ):
        if not 0 <= value <= upper:
            raise ProtocolError(f"{label} out of range: {value}")
    return (
        bytes([CMD_MODE, mode, work_mode, grade, 0, 0, 0, 0, 0, 0])
        + on_time.to_bytes(2, "big")
        + off_time.to_bytes(2, "big")
    )


def build_push_ack(frame: bytes) -> bytes | None:
    """Return the host reply for a device push, or None if none is due."""
    if not frame:
        return None
    head = frame[0]
    if not 0x40 <= head <= 0x7F or head in _NO_ACK_PUSHES:
        return None
    if head == PUSH_SCHEDULE and len(frame) > 5:
        # Schedules are acked with aroma and slot index so the device can
        # tell which one landed.
        return bytes([ack_head(head), frame[1], frame[5]])
    return bytes([ack_head(head)])


# --------------------------------------------------------------- decoders
@dataclass(slots=True)
class LoginReply:
    """Decoded login reply."""

    ok: bool
    firmware: str | None = None
    protocol_version: float | None = None
    reply_required: bool = False
    custom_timing: bool = False
    oil_tracking: bool = False
    multi_aroma: bool = False


def parse_login_reply(frame: bytes) -> LoginReply:
    """Decode the reply to CMD_LOGIN."""
    if not frame or frame[0] != CMD_LOGIN:
        raise ProtocolError("not a login reply")
    text = frame[1:12].decode("ascii", errors="replace")
    if text.startswith("ERROR"):
        return LoginReply(ok=False)
    if len(text) <= 2:
        # Older firmware replies with a short token and no capability bytes.
        return LoginReply(ok=True, protocol_version=2.0)

    # Anything other than ERROR is an accepted login; the vendor app treats
    # it the same way.
    firmware = text[:7] if len(text) >= 7 else text
    match = _VERSION_RE.search(text)
    version = float(match.group(1)) if match else None
    reply = LoginReply(ok=True, firmware=firmware, protocol_version=version)
    if len(frame) > 13:
        caps = frame[13]
        reply.oil_tracking = bool(caps & 0x01)
        reply.custom_timing = bool(caps & 0x04)
        reply.multi_aroma = bool(caps & 0x08)
    if len(frame) > 14:
        reply.reply_required = bool(frame[14] & 0x80)
    return reply


@dataclass(slots=True)
class ControlState:
    """PUSH_CONTROL: master power and feedback-sound flags."""

    power: bool
    muted: bool
    demo: bool
    lamp: bool


def parse_control(frame: bytes) -> ControlState:
    """Decode PUSH_CONTROL."""
    if len(frame) < 2 or frame[0] != PUSH_CONTROL:
        raise ProtocolError("not a control frame")
    flags = frame[1]
    return ControlState(
        power=bool(flags & _CTRL_ENABLE),
        muted=bool(flags & _CTRL_MUTE),
        demo=bool(flags & _CTRL_DEMO),
        lamp=bool(flags & _CTRL_LAMP),
    )


@dataclass(slots=True)
class WorkStatus:
    """PUSH_STATUS: what the diffuser is doing right now."""

    slot: int
    misting: bool
    mist_seconds: int
    pause_seconds: int
    countdown: int | None = None
    raw_tail: bytes = b""

    @property
    def active(self) -> bool:
        """Return True while a schedule slot or mode is driving the pump."""
        return self.slot != 0

    @property
    def phase(self) -> str:
        """Return idle, misting or paused."""
        if not self.active:
            return "idle"
        return "misting" if self.misting else "paused"


def parse_status(frame: bytes) -> WorkStatus | None:
    """Decode PUSH_STATUS. Returns None for status subtypes not understood."""
    if len(frame) < 7 or frame[0] != PUSH_STATUS:
        raise ProtocolError("not a status frame")
    if frame[1] != 0x01:
        return None
    countdown = None
    if len(frame) >= 10:
        countdown = int.from_bytes(frame[7:10], "big")
    return WorkStatus(
        slot=frame[2] & 0x07,
        misting=bool(frame[2] & 0x08),
        mist_seconds=int.from_bytes(frame[3:5], "big"),
        pause_seconds=int.from_bytes(frame[5:7], "big"),
        countdown=countdown,
        raw_tail=bytes(frame[7:]),
    )


@dataclass(slots=True)
class ModeState:
    """PUSH_MODE: active mode and the preset parameters (ANP)."""

    mode: int
    work_mode: int
    grade: int
    countdown: int
    on_time: int | None = None
    off_time: int | None = None


def parse_mode(frame: bytes) -> ModeState:
    """Decode PUSH_MODE."""
    if len(frame) < 10 or frame[0] != PUSH_MODE:
        raise ProtocolError("not a mode frame")
    state = ModeState(
        mode=frame[1],
        work_mode=frame[2],
        grade=frame[3],
        countdown=int.from_bytes(frame[7:10], "big"),
    )
    if len(frame) >= 14:
        state.on_time = int.from_bytes(frame[10:12], "big")
        state.off_time = int.from_bytes(frame[12:14], "big")
    return state


@dataclass(slots=True)
class Limits:
    """PUSH_LIMITS: intensity levels and custom timing bounds in seconds."""

    max_grade: int
    on_min: int
    on_max: int
    off_min: int


def parse_limits(frame: bytes) -> Limits:
    """Decode PUSH_LIMITS."""
    if len(frame) < 8 or frame[0] != PUSH_LIMITS:
        raise ProtocolError("not a limits frame")
    return Limits(
        max_grade=frame[1],
        on_min=int.from_bytes(frame[2:4], "big"),
        on_max=int.from_bytes(frame[4:6], "big"),
        off_min=int.from_bytes(frame[6:8], "big"),
    )


@dataclass(slots=True)
class Grade:
    """One preset intensity level as mist/pause seconds."""

    mist: int
    pause: int


def parse_grades(frame: bytes) -> list[Grade]:
    """Decode PUSH_GRADES into a list ordered from level 1."""
    if not frame or frame[0] != PUSH_GRADES:
        raise ProtocolError("not a grades frame")
    body = frame[1:]
    return [
        Grade(
            mist=int.from_bytes(body[i : i + 2], "big"),
            pause=int.from_bytes(body[i + 2 : i + 4], "big"),
        )
        for i in range(0, len(body) - 3, 4)
    ]


def parse_text(frame: bytes) -> str:
    """Decode a text push (model, PCB version) with NUL padding removed."""
    return (
        frame[1:]
        .split(b"\x00", 1)[0]
        .decode("utf-8", errors="replace")
        .strip()
    )


@dataclass(slots=True)
class Schedule:
    """A native schedule slot."""

    index: int
    enabled: bool
    shown: bool
    start: tuple[int, int]
    end: tuple[int, int]
    days: int
    work_mode: int
    grade: int
    mist: int | None = None
    pause: int | None = None
    aroma: int = 1
    fan: bool = False

    @property
    def all_day(self) -> bool:
        """Return True when start equals end, which the device treats as 24h."""
        return self.start == self.end


def parse_schedule(frame: bytes) -> Schedule:
    """Decode PUSH_SCHEDULE."""
    if len(frame) < 14 or frame[0] != PUSH_SCHEDULE:
        raise ProtocolError("not a schedule frame")
    flags = frame[6]
    schedule = Schedule(
        index=frame[5],
        aroma=frame[1],
        shown=bool(flags & 0x01),
        enabled=bool(flags & 0x02),
        fan=bool(flags & 0x04),
        start=(frame[7], frame[8]),
        end=(frame[9], frame[10]),
        days=frame[11] & 0x7F,
        work_mode=frame[12] & 0x01,
        grade=max(frame[13], 1),
    )
    if len(frame) >= 18:
        schedule.mist = int.from_bytes(frame[14:16], "big")
        schedule.pause = int.from_bytes(frame[16:18], "big")
    return schedule


def build_schedule(schedule: Schedule, custom_timing: bool) -> bytes:
    """Build a schedule write. Custom-timing devices take mist/pause too."""
    for hour, minute in (schedule.start, schedule.end):
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ProtocolError("schedule time out of range")
    if not 1 <= schedule.index <= 7:
        raise ProtocolError("schedule index out of range")
    if not 0 <= schedule.grade <= 0xFF:
        raise ProtocolError("schedule grade out of range")
    shown = schedule.shown or schedule.enabled
    flags = (
        (0x01 if shown else 0)
        | (0x02 if schedule.enabled and shown else 0)
        | (0x04 if schedule.fan else 0)
    )
    frame = bytearray(
        [
            CMD_SCHEDULE,
            schedule.aroma,
            0x02,
            0x01,
            0x00,
            schedule.index,
            flags,
            schedule.start[0],
            schedule.start[1],
            schedule.end[0],
            schedule.end[1],
            schedule.days & 0x7F,
            schedule.work_mode & 0x01,
            schedule.grade,
        ]
    )
    if custom_timing:
        # Custom-timing devices always take the 18-byte form; with a preset
        # level the trailing seconds are ignored, so app defaults fill in.
        mist, pause = schedule.mist, schedule.pause
        if schedule.work_mode == 1 and (mist is None or pause is None):
            raise ProtocolError("custom timing needs mist and pause")
        if mist is None:
            mist = DEFAULT_SCHEDULE_MIST
        if pause is None:
            pause = DEFAULT_SCHEDULE_PAUSE
        if not (0 <= mist <= 0xFFFF and 0 <= pause <= 0xFFFF):
            raise ProtocolError("mist/pause out of range")
        frame += mist.to_bytes(2, "big")
        frame += pause.to_bytes(2, "big")
    return bytes(frame)


def pause_range(model: str | None, mist: int) -> tuple[int, int] | None:
    """Return the allowed pause range for a mist time, if the model has one."""
    if model == "ANP":
        low = (
            ANP_PAUSE_MIN_LONG_MIST if mist >= ANP_LONG_MIST else ANP_PAUSE_MIN
        )
        return low, PAUSE_MAX
    if model in ("RDX", "U1"):
        if mist <= 15:
            return 30, 600
        if mist <= 30:
            return 45, 1200
        if mist <= 45:
            return 60, 1800
        return 75, 1800
    return None


@dataclass(slots=True)
class DeviceSnapshot:
    """Everything learned from a device, updated as frames arrive."""

    login: LoginReply | None = None
    control: ControlState | None = None
    status: WorkStatus | None = None
    mode: ModeState | None = None
    limits: Limits | None = None
    grades: list[Grade] = field(default_factory=list)
    schedules: dict[int, Schedule] = field(default_factory=dict)
    model: str | None = None
    pcb_version: str | None = None


def apply_push(snapshot: DeviceSnapshot, frame: bytes) -> int | None:
    """Fold a device push into ``snapshot``. Returns the head it handled."""
    if not frame:
        return None
    head = frame[0]
    try:
        if head == PUSH_CONTROL:
            snapshot.control = parse_control(frame)
        elif head == PUSH_STATUS:
            status = parse_status(frame)
            if status is None:
                return None
            snapshot.status = status
        elif head == PUSH_MODE:
            snapshot.mode = parse_mode(frame)
        elif head == PUSH_LIMITS:
            snapshot.limits = parse_limits(frame)
        elif head == PUSH_GRADES:
            snapshot.grades = parse_grades(frame)
        elif head == PUSH_SCHEDULE:
            schedule = parse_schedule(frame)
            snapshot.schedules[schedule.index] = schedule
        elif head == PUSH_MODEL:
            snapshot.model = parse_text(frame) or snapshot.model
        elif head == PUSH_PCB_VERSION:
            snapshot.pcb_version = parse_text(frame[:17])
        else:
            return None
    except ProtocolError:
        return None
    return head
