# Changelog

## 2.1.0

- Fix switch state staying `unknown` on firmware that acknowledges with a
  single byte (`ad`, upstream issue #1). Commands are serialised and matched
  to their ACK; legacy `ad13`/`ad12` still works.
- Send the clock after login so the diffuser reports its state. Power and
  feedback sound now come from the device when it reports them; otherwise
  the switch is marked as assumed state.
- A command that is never acknowledged no longer flips the switch.
- Reconnect no longer re-sends the last command over state the device just
  reported. Commands issued while disconnected are still delivered.
- Power commands keep the device's mute setting instead of always unmuting.
- New for the Scenta Plug (ANP): feedback sound switch, intensity select,
  mode select, phase sensor.
- New `airversa_diffuser.set_schedule` service for native schedules,
  including custom mist and pause times.
- Model detection from the advertised name (`SP1*`, `SP2*`, ...) or the
  device's model frame; Bluetooth discovery for `SP1*` and `SP2*`.
- Firmware version from the advertisement shown on the device page.
- Options for the device PIN and for turning state sync off.
- Diagnostics download with the decoded device state.
- MAC address validation in the manual setup form.
- Protocol layer split into `protocol.py`; reference in `docs/PROTOCOL.md`.
- Tests.

## 2.0.0

- **Rewritten as a Home Assistant integration** (custom component), replacing the
  former add-on.
- **Bluetooth proxy support.** Uses Home Assistant's native Bluetooth stack
  (`bluetooth.async_ble_device_from_address` + `bleak-retry-connector`), so
  diffusers can be reached through ESPHome/Shelly Bluetooth proxies instead of
  being limited to the host's internal adapter.
- **UI config flow** with Bluetooth auto-discovery and manual MAC entry; one
  config entry (device) per diffuser.
- **Native `switch` entity** per diffuser with confirmed state from BLE ACK
  frames, replacing the `/share` file + `shell_command` control channel.
- Per-device **keepalive interval** option.
- Same BLE protocol as the add-on (characteristic `fff6`, handshake/PIN `8888`,
  keepalive `e0aa55`, on `2d13` / off `2d12`), so existing hardware is unchanged.
