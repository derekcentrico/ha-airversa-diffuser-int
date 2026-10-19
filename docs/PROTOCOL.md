# Airversa "AK" BLE protocol

Reference for the Scenta diffusers that use GATT service `fff0`. Written for
interoperability with devices we own. It describes observed and inferred
behaviour; no vendor code is reproduced here.

## Evidence levels

Every entry carries one or more of these tags:

| Tag | Meaning |
|-----|---------|
| U | Present in the original upstream integration / add-on |
| S | Static analysis of the Sleekpoint Android app (`com.sleekp.sleekpoint` 1.0.121) |
| D | Seen in real BLE notification logs (ANP via ESPHome proxy, SP2ANA log in upstream issue #1) |
| H | Behaviour confirmed on hardware by a person watching the device |

Anything without D or H is a hypothesis about the wire format until a capture
or hardware test confirms it.

## Transport

| Item | Value | Evidence |
|------|-------|----------|
| Service | `0000fff0-0000-1000-8000-00805f9b34fb` | U S |
| Characteristic (write + notify) | `0000fff6-0000-1000-8000-00805f9b34fb` | U S D |
| CCCD | `00002902-...` (standard) | S |
| MTU requested by app | 128 | S |
| Frames per notification | one; no reassembly on this family | S D |
| Keepalive | `e0aa55`, app sends every 5 s, never acknowledged | U S |

Models using service `ee01` with characteristics `ee02`/`ee03` (advertised as
`SP4*`, model U1) speak a different packetised protocol and are not covered.

## Model detection

Advertised local-name prefix to model code (S):

| Prefix | Model | Product |
|--------|-------|---------|
| `SP1` | ANP | Scenta Plug |
| `SP2` | ANA | Scenta VAST |
| `SP3` | RDX | Scenta |
| `SP_`, `A32` | ANW | Scenta XL |
| `SP4` | U1 | Scenta XL (ee01 protocol) |

Main firmware version is in manufacturer data, company id `0x5943`, bytes
11-13 as `major.minor.patch` (S). The device also pushes its model string
in frame `0x45` (S).

## Framing and acknowledgements

* Host commands use a head byte below `0x80` (S).
* The device acknowledges a command with `head | 0x80` (S D H). Newer
  firmware sends only that byte, e.g. `ad` for `2d..` (D H, ANP and SP2ANA).
  Older firmware appends an echo of the value, e.g. `ad13` (U).
* The ACK identifies the command type, not the request, so commands of the
  same type must be serialised to correlate replies (S).
* The app treats an unacknowledged command as lost and resends it once a
  second, up to ten times (S).
* Device pushes use heads `0x40`-`0x7f`. If the login reply sets the "reply"
  flag, the host answers each push with `head | 0x80` (S). Schedule pushes
  (`0x4a`) are answered with three bytes: `ca <aroma> <slot>` (S).
* `0x59` (status) is never acknowledged in either direction (S D).
* ANP firmware `11.1.2` does not acknowledge `0x3a` or `0x3b`; the app does
  not wait for an ACK on that version (S). This integration accepts the
  following `0x5b` push as confirmation instead.

## Session start

1. Enable notifications on `fff6`.
2. Login: `8f` + ASCII PIN + ASCII `OK01`. Factory PIN is `8888`, giving
   `8f383838384f4b3031` (U S D H).
3. Login reply (D): `8f` + 11 ASCII bytes + capability bytes. Examples:
   * ANP: `8f 43595f56332e304f4b3031 01 45 c2 10` = `CY_V3.0OK01`, then caps.
   * SP2ANA: `8f 43595f56332e304f4b3031 01 55 c2`.
   * `8f` + `ERROR` means the PIN was rejected (S).

   Decoding (S), byte index from the start of the frame:

   | Byte | Bits | Meaning |
   |------|------|---------|
   | 1-11 | ASCII | BLE module id and version; chars 4-6 are the protocol version (`3.0`) |
   | 13 | `0x01` | oil tracking |
   | 13 | `0x04` | custom mist/pause timing supported |
   | 13 | `0x08` | multiple aroma slots |
   | 13 | `0x80` | round battery UI |
   | 14 | `0x80` | host must acknowledge device pushes ("reply") |
   | 14 | `0x10` | lamp present |
   | 14 | low 3 bits | oil UI variant |

   ANP decodes as: oil tracking, custom timing, single aroma, reply required.
   SP2ANA decodes the same except the unused `0x10`/`0x40` bits (D + S).
4. Clock sync, protocol 3.x only (S): `21 <weekday> <yy> <MM> <dd> <HH> <mm>
   <ss>`, binary values, weekday 0 = Sunday, in the device's local time.
   The device ACKs with `a1` and then pushes its full state (frames
   `0x41`-`0x4d`, plus `0x5b` on the ANP). The app treats `0x5b` (ANP) or
   `0x4d` (others) as "sync complete".
5. Keepalive every few seconds.

Without step 4 the device never sends its state, which is why releases up to
2.0.0 saw only `0x59` frames and the switch stayed `unknown`.

## Commands (host to device)

| Head | Payload | Purpose | Evidence |
|------|---------|---------|----------|
| `2d` | 1 byte: `0x12` base, `+0x01` on, `+0x40` mute | Power and feedback sound | U (13/12) S (mute bit) H (on/off) |
| `21` | clock, see above | Time sync; triggers state dump | S |
| `2a` | schedule, see below | Write one schedule slot | S |
| `3a` | `<light on> <sensitivity> <work mode> <grade> <on:2> <off:2>` | Light-sensor mode parameters | S |
| `3b` | `<mode> <work mode> <grade> 00x6 <on:2> <off:2>` (14 bytes) | ANP mode and preset intensity | S |
| `22` | ASCII name with model prefix | Rename | S |
| `0f` | ASCII new PIN | Change PIN (ACK `8f`, single byte) | S |
| `3c` | child lock / countdown timer (model specific) | Not implemented | S |

Power byte values:

| Byte | Meaning |
|------|---------|
| `13` | on, beep enabled |
| `12` | off, beep enabled |
| `53` | on, muted |
| `52` | off, muted |

Mute shares the power byte, so changing either needs the current value of
the other or it will be reset. Older releases always sent `13`/`12`, which
unmutes the device on every power command.

## Pushes (device to host)

| Head | Layout | Meaning | Evidence |
|------|--------|---------|----------|
| `42` | ASCII from byte 4 | Device name | S |
| `43` | label, NUL, length, fill data | Label and last refill | S |
| `44` | 16 ASCII bytes | PCB version | S |
| `45` | ASCII | Model code (`ANP`, ...) | S |
| `46` | `<max grade> <on min:2> <on max:2> <off min:2>` | Intensity count and mist bounds (seconds). App forces off min = 60 on ANP | S |
| `47` | repeated `<mist:2> <pause:2>` | Seconds per preset level, level 1 first | S |
| `4a` | schedule, see below | One schedule slot | S |
| `4b`, `50` | per-aroma counters | Oil usage | S |
| `4d` | 1 flag byte | `0x01` power, `0x40` muted, `0x04` demo, `0x10` lamp | S |
| `51` | lamp state | Lamp models | S |
| `59` | see below | Live status, about 1 Hz | S D |
| `5a` | `<flags> <sensitivity> <work mode> <grade> <on:2> <off:2>` | Light-sensor parameters | S |
| `5b` | `<mode> <work mode> <grade> <countdown:3> <countdown:3> <on:2> <off:2>` | ANP mode and preset level | S |
| `5c` | 1 byte | Child lock | S |

### `0x59` status

```
59 01 <state> <mist:2> <pause:2> [<countdown:3> <b10> <b11> <b12>]
```

| Field | Meaning | Evidence |
|-------|---------|----------|
| byte 1 | `01` for this layout; others ignored | S D |
| state bits 0-2 | active slot: 0 idle, otherwise schedule number or preset | S D |
| state bit 3 | 1 misting, 0 paused | S D |
| mist | seconds; counts down while misting on the ANP | D |
| pause | seconds | D |
| countdown | timer-mode remaining time | S |
| byte 10 | light mode: light-triggered running flag | S |
| bytes 11-12 | unknown (`03 1f` on the ANP) | D |

Captures:

```
590100000500d200000001031f   ANP idle after HA restart (slot 0)
590109000500d200000001031f   ANP after ON: slot 1, misting, 5 s left
590109000400d200000001031f   4 s
590109000300d200000001031f   3 s
590109000a0000               SP2ANA misting, short layout; counter rises
```

On the SP2ANA the counter rises instead of falling, so this integration does
not publish it as "time remaining". The slot field is not power: the device
can be on with no slot active (custom mode outside every schedule window).
Power comes only from `0x4d` or a command ACK.

### Schedules (`2a` write, `4a` push)

```
2a <aroma> 02 <fan|1> 00 <slot> <flags> <h on> <m on> <h off> <m off> <days> <work mode> <grade> [<mist:2> <pause:2>]
```

* `flags`: `0x01` slot in use, `0x02` enabled, `0x04` fan (S).
* `days`: bit 0 Sunday through bit 6 Saturday; `0x7f` every day (S).
* Same start and end time means all day (S).
* `work mode` 0 uses `grade`; 1 uses the trailing mist/pause seconds, which
  are only sent when the login reply sets the custom-timing bit (S).
* ANP pause limits: 60-3600 s, 75 s minimum when mist is 15 s or more (S).
* The device ACKs with `aa` (S). The push uses the same layout with head
  `4a`.

### ANP modes (`3b` / `5b`)

| Value | App label | Notes | Evidence |
|-------|-----------|-------|----------|
| 0 | Preset | Four intensity buttons; `grade` 1-4 | S |
| 1 | Custom | Runs the native schedules | S |
| 2 | Timer | Countdown; present in frames, hidden in the app | S |
| 3 | Light | Light-sensor driven; toggled from a button on the ANP screen | S |

The app only ever sends `3b` with the work mode and on/off seconds it last
received in `5b`, changing just the mode or grade. This integration does the
same and refuses to send `3b` before a `5b` has arrived.

## Open questions for hardware

* Does the ANP push `0x4d` when the physical button is pressed?
* Do `0x4d`, `0x5b` and `0x4a` arrive after the clock sync on the ANP as the
  app expects?
* Meaning of `0x59` bytes 11-12.
* Exact mist/pause values for each ANP preset level (`0x47`).
