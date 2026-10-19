# Airversa Scenta Diffuser — Home Assistant Integration

A custom Home Assistant **integration** for the **Airversa Scenta** BLE diffuser
(model `SP3M`). It keeps a persistent BLE connection open to each diffuser (with
keepalive and auto-reconnect) and exposes each one as a `switch` entity.

> **Fork note.** This fork adds full local control for the Scenta Plug (ANP):
> confirmed power state, feedback sound, intensity, mode, a phase sensor and
> native schedules. It stays compatible with existing installs (same domain,
> same entity ids). Protocol notes are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

This is the successor to the
[`ha-airversa-diffuser`](https://github.com/ajachierno/ha-airversa-diffuser)
**add-on**. The big difference:

> **It works through Bluetooth proxies.**
> The old add-on ran in its own container and could only talk to the Home
> Assistant host's *internal* Bluetooth adapter. This integration uses Home
> Assistant's native Bluetooth stack, so it can reach diffusers through any
> **ESPHome / Shelly Bluetooth proxy** on your network — no longer limited to
> the range of the host antenna.

## Why convert the add-on to an integration?

| | Add-on (old) | Integration (this repo) |
| --- | --- | --- |
| BLE access | Direct BlueZ in a container → **host adapter only** | Home Assistant `bluetooth` component → **host adapters _and_ proxies** |
| Bluetooth proxies (ESPHome/Shelly) | ❌ not possible | ✅ supported |
| Control surface | A file in `/share` + `shell_command` glue | A real `switch` entity per diffuser |
| Setup | Add-on store + edit `configuration.yaml` | UI config flow (auto-discovery or MAC entry) |
| State | `cat` a file via `command_line` sensor | Native entity state from BLE ACK frames |
| Multiple devices | `devices:` list in add-on options | One config entry per diffuser |

The BLE protocol itself is unchanged — same characteristic, handshake (PIN
`8888`), keepalive, and on/off frames — so the same hardware works exactly as
before, just with a better transport and a cleaner Home Assistant surface.

## Requirements

- Home Assistant 2024.11 or newer.
- The Home Assistant **Bluetooth** integration set up with at least one adapter
  **or** one [Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html)
  (an ESPHome device with `bluetooth_proxy:` + `active: true`, or a compatible
  Shelly).
- The Airversa phone app **closed** — a diffuser only allows one BLE central at
  a time.

## Installation

### HACS

1. HACS, three-dot menu, **Custom repositories**.
2. Add `https://github.com/derekcentrico/ha-airversa-diffuser-int` as an
   **Integration**.
3. Install **Airversa Scenta Diffuser**, then restart Home Assistant.

If the upstream repository is already installed through HACS, remove it from
HACS first (Remove, not Delete from the integration page). Both share the
`airversa_diffuser` domain and the same folder, so only one can be installed.
Your config entries, entity ids and automations stay because they live in
Home Assistant, not in the integration folder.

#### Testing a pre-release

Development builds are published as GitHub pre-releases (tags like
`v2.1.0b1`). In HACS, open the repository, turn on the **Pre-release**
switch entity for it (Settings, Devices & services, HACS, the repository's
device), then use **Redownload** and pick the pre-release from the version
list. Restart afterwards. Pick a normal release from the same list to go
back.

### Manual

Copy `custom_components/airversa_diffuser/` into your Home Assistant
`config/custom_components/` folder and restart.

### Rolling back

* HACS: **Redownload**, pick the previous release (or switch the custom
  repository back to `ajachierno/ha-airversa-diffuser-int`), restart.
* Manual: keep a copy of the old `custom_components/airversa_diffuser/`
  folder and copy it back.

Entries created by this version carry an extra `model` key and new options.
Older versions ignore both, so rolling back needs no cleanup. Entities this
version added (feedback sound, intensity, mode, phase) become orphaned on
rollback; remove them from the entity list if they bother you.

## Setup

Most likely Home Assistant will **auto-discover** the diffuser via Bluetooth and
offer it under **Settings → Devices & services**. Just confirm it.

If it isn't discovered automatically (the advertised name varies between
units/firmware):

1. **Settings → Devices & services → Add integration → Airversa Scenta
   Diffuser**.
2. Enter the diffuser's Bluetooth **MAC address** (e.g. `24:42:E3:37:86:E7`).
   Recently-seen Bluetooth devices are listed on the form to help you identify
   it.
3. Optionally give it a name.

Repeat once per diffuser — each becomes its own device with one switch.

## Entities

Every model gets the power switch. The others are created only for models
where the protocol supports them; the model is read from the advertised name
(`SP1*` is the ANP) or reported by the device after login.

| Entity | Models | Notes |
| --- | --- | --- |
| Power switch | all | Unique id unchanged from 2.0.0, so existing automations keep working |
| Feedback sound switch | ANP, RDX (ANA/ANW on firmware 2.x+) | Mute flag shares the power frame; unavailable until power is known |
| Intensity select | ANP | Preset levels 1-4. Choosing one switches the device to Preset mode |
| Mode select | ANP | Preset, Custom schedules, Light sensor |
| Phase sensor | ANP, ANA | Idle, misting or paused; `slot` attribute shows the active schedule or preset |

### Where the power state comes from

After login the integration sends the clock, which makes the diffuser report
its full state. Once the device reports power the switch shows it as
confirmed (`state_source: device`). If only a command acknowledgement is
available the state is marked as assumed (`assumed_state: true`,
`state_source: command`) so the UI shows both on and off buttons. A command
that is never acknowledged leaves the state unchanged and logs a warning.

The switch is `unavailable` while the BLE link is down and recovers on
reconnect. On reconnect, state the device just reported is never overwritten.
A command issued while disconnected is sent once the link is back.

## Services

### `airversa_diffuser.set_schedule`

Writes one native schedule slot (1-5). On the ANP, custom mist and pause
times only exist inside schedules, so this is also the mist/pause control.
Fields left out keep what the device last reported; a slot the device has not
reported needs `start` and `end`.

```yaml
action: airversa_diffuser.set_schedule
data:
  device_id: <diffuser device id>
  slot: 1
  enabled: true
  start: "07:00:00"
  end: "22:00:00"
  days: [mon, tue, wed, thu, fri]
  mist: 10
  pause: 120
```

ANP pause limits are 60-3600 s (75 s minimum when mist is 15 s or more); mist
limits come from the device. Schedules run on the diffuser, so they keep
working when Home Assistant is down. For anything else, ordinary
automations on the power switch are simpler. Put the device in Custom mode
for schedules to apply.

## Options

Per diffuser (**Configure** on the device):

- **Keepalive interval (seconds)**: default `2.0`.
- **Device PIN**: default `8888`. Change it only if you set a different PIN in
  the Sleekpoint app. A rejected PIN is logged and retried every minute.
- **Read state from the device**: default on. Turn off to get the 2.0.0
  behaviour (no clock sync, command-only state).

## Migrating from the add-on

1. Install and set up this integration for each diffuser.
2. Remove the old `shell_command:` and `command_line:` entries you added for the
   add-on, and any `input_boolean` + automation glue used to bridge them.
3. Point your automations/dashboards at the new `switch.<name>` entities
   directly.
4. **Uninstall the "Airversa Diffuser BLE Bridge" add-on** so it stops holding
   the BLE adapter (only one central can hold a diffuser at a time).

## Protocol reference

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for every known frame and how each
one was established, and [docs/CAPTURE.md](docs/CAPTURE.md) for collecting
BLE captures.

## Troubleshooting

- **Never connects** — confirm the MAC, close the Airversa phone app, and make
  sure an adapter or proxy is actually in range. Enable debug logging (below).
- **Drops after N seconds** — lower the keepalive interval in the device's
  options.
- **Not auto-discovered** — add it manually by MAC; discovery depends on the
  advertised name, which differs across units.

Enable debug logs by adding to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.airversa_diffuser: debug
```

## License

MIT — see [LICENSE](https://github.com/ajachierno/ha-airversa-diffuser-int/blob/main/LICENSE)

## Buy me a coffee

Did you find this helpful? Consider supporting additional development:

<a href="https://www.buymeacoffee.com/ajachiernoo" target="_blank">
  <img src="https://raw.githubusercontent.com/ajachierno/ha-airversa-diffuser-int/main/brand/bmc_button.png" alt="Buy me a coffee" height="50">
</a>

<br><br>

<a href="https://www.buymeacoffee.com/ajachiernoo" target="_blank">
  <img src="https://raw.githubusercontent.com/ajachierno/ha-airversa-diffuser-int/main/brand/buymeacoffee_qr.png" alt="Buy me a coffee QR code" width="200">
</a>
