# Capturing BLE traffic

Two ways to collect evidence. The first needs only Home Assistant; the second
records what the Sleekpoint app sends and needs an Android phone. Neither
needs root.

## 1. Home Assistant debug log

Add to `configuration.yaml` and restart, or enable debug logging from the
integration page:

```yaml
logger:
  logs:
    custom_components.airversa_diffuser: debug
```

Every frame except the 1 Hz status frame and the login frame (it carries the
PIN) is logged as `rx <hex>` / `tx <hex>`. Status changes show up as state
changes on the Phase sensor. Download diagnostics from the device page for a
decoded snapshot.

## 2. Android HCI snoop log of the Sleekpoint app

Only one central can hold the connection, so Home Assistant has to let go
first.

### Release the device from Home Assistant

1. Settings, Devices & services, Airversa Scenta Diffuser.
2. Three-dot menu on the entry, Disable. The integration disconnects and
   stops reconnecting. Disabling keeps entity ids and history; deleting does
   not.
3. Wait about 10 s, then confirm the indicator shows no connection.

### Enable the snoop log

1. Developer options on: Settings, About phone, tap Build number seven times.
2. Developer options, "Enable Bluetooth HCI snoop log", set to Enabled (on
   some phones "Enabled" rather than "Filtered").
3. Toggle Bluetooth off and on so the log starts fresh.

### Record one action per capture

For each row below: open Sleekpoint, wait for it to connect, wait 10 s
(baseline), do the single action, wait 10 s, then note the clock time and
what the diffuser did (beep, light, mist).

| # | Action | Looking for |
|---|--------|-------------|
| 1 | App connects | Login, clock sync, state dump |
| 2 | Power on | `2d..` and reply |
| 3 | Power off | `2d..` and reply |
| 4 | Sound off | Mute bit (`2d52`/`2d53`) |
| 5 | Sound on | Unmute |
| 6 | Intensity 1, 2, 3, 4 (one capture each) | `3b` grade byte |
| 7 | Edit a schedule's mist time | `2a` mist field |
| 8 | Edit a schedule's pause time | `2a` pause field |
| 9 | Preset to Custom, then Light, then back | `3b` mode byte |
| 10 | Create a schedule | New `2a` slot |
| 11 | Enable, then disable a schedule | `2a` flag byte |
| 12 | Physical button on, then off (app open) | Unsolicited `4d` |
| 13 | Close and reopen the app | Re-sync |
| 14 | Unplug the diffuser 10 s, plug back in, reopen app | Persistence |

### Pull the log

With USB debugging on:

```bash
adb bugreport bugreport.zip
unzip -o bugreport.zip 'FS/data/misc/bluetooth/logs/*' -d snoop
```

The file is `btsnoop_hci.log` (path varies by vendor; search the zip for
`btsnoop`). Open it in Wireshark and filter:

```
btatt && bluetooth.addr == 24:42:e3:60:f8:7e
```

Write requests and notifications on handle for `fff6` carry the frames.
Export just those rows (File, Export Packet Dissections, As CSV) rather than
sharing the whole log: a bug report also holds unrelated personal data.

### Hand the device back

Disable the snoop log, close Sleekpoint (force stop so it releases the
link), then re-enable the integration entry.
