# Troubleshooting

## Start here: the channel check

**This is the failure mode to check first when nothing is delivered**, and it
looks exactly like a dead device — `TX-SENT` with no `TX-ACK`.

ESP-NOW peers only hear each other on the same channel, and a station-mode ESP32
is locked to its AP's channel. Read the bridge's `READY` line (or
`get_bridge_status`, or the web UI's status row):

```
READY mac=aa:bb:cc:dd:ee:ff channel=8 band=2.4GHz wifi=YourSSID
```

- `channel=` must match the Cardputer's. Joining both to the same AP is what makes
  them agree.
- `band=` must be `2.4GHz`. The C5 is dual-band; the Cardputer's S3 is not.
- `wifi=unprovisioned(fallback-ch1)` → run `provision_wifi.py`.
- `wifi=join-failed(fallback-ch1)` → wrong PSK, AP out of range, or a 5 GHz-only
  SSID.

If the Cardputer is off WiFi entirely, ESP-NOW owns its radio and it sits on
**channel 1** — which is exactly where the C5's fallback puts it, so that
combination works too.

---

## Symptom → cause

| Symptom | Likely cause |
|---|---|
| Every MCP tool errors with a `fix:` hint | `webapp.py` isn't running. It's the serial owner; start it. |
| `delivered: false`, no chirp | Channel mismatch (above), Cardputer powered off / out of range, or it isn't running the patched firmware. |
| Chirp fires but the message never displays | Expected with Chat closed — the queue holds it. Open Chat to drain, one message per iteration. |
| Messages arrive, **no chirp at all** | Unpatched firmware, or `espNowDeinit()` was called. Stock firmware alerts on nothing. |
| Nothing arrives after a **fresh boot**, but works once Chat has been opened | Unpatched firmware — upstream only inits ESP-NOW in `AppChat::onOpen()`. |
| Chirp is inaudible | Unpatched: upstream never sets a volume. The patch sets 220/255 and uses three 130–180 ms tones. |
| Web app starts but "cannot send" | C5 unplugged, or another process holds the port (`notify_bridge.py`, `idf.py monitor`). |
| `curl` right after login fails | Normal. Port probing takes **up to ~15 s**. |
| Restored history shows everything unacked | Old-format log lines, or a build predating the `TX-SENT`/`TX-ACK` crc8 logging. |
| SetWiFi / NTP broken on the Cardputer | Expected. `espNowInit()` disconnects the station; that's the accepted trade-off. `espNowDeinit()` returns the radio. |
| Reply arrives split across messages | Check `fragments` in the result — it exposes the pieces so you can tell a wire split from real newlines. |
| LaunchAgent logs are empty | `PYTHONUNBUFFERED=1` missing from the plist. Python block-buffers stdout when it isn't a tty. |
| Serial port dead after flashing | `idf.py monitor` still holds it. One owner only. |

---

## Only one serial owner

`webapp.py`, `notify_bridge.py`, and `idf.py monitor` all want the same port. Run
**one**. Keep `webapp.py` as the default, since both the browser and the MCP
server work through it.

Before provisioning or flashing, stop the LaunchAgent:

```bash
launchctl bootout gui/$(id -u)/local.espnow.webapp
# ... do the work ...
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.espnow.webapp.plist
```

---

## Isolating a fault

Work down the chain — each step rules out everything above it.

1. **Radio path.** `notify_bridge.py send "test"` (with `webapp.py` stopped). Acked?
   → the radio, both firmwares, and the channel are all fine.
2. **HTTP layer.** Restart `webapp.py`, send from the web UI. Works here but not
   in step 1's absence → serial ownership conflict.
3. **MCP layer.** `claude mcp list` → `✔ Connected`? Then call
   `get_bridge_status`. Failing only here → registration paths or
   `ESPNOW_WEBAPP_URL`, not the hardware.
4. **Device.** `idf.py monitor` on the Cardputer (stop the serial owner first).

---

## Build failures

| Error | Fix |
|---|---|
| Builds for xtensa `esp32`, dies on missing `usb_serial_jtag_*` | You omitted `--preview`. Plain `idf.py set-target esp32c5` **prints a notice and silently keeps the old target** in IDF v5.5. |
| esp-now fails on `rom/rtc.h`, `g_rom_flashchip`, or `rx_ctrl->secondary_channel` | Run `apply_c5_patches.py`. Three separate C5 breakages in esp-now 2.5.3; idempotent, and it touches nothing on the wire. |
| `esp_now_register_send_cb` signature error | esp-now 2.5.2 doesn't compile against IDF v5.5 at all. Pin `==2.5.3`. M5's `dependencies.lock` pins 2.5.2 even though its manifest says `^2.5.2`. |
| `fatal error: mooncake.h: No such file or directory` | `python3 fetch_repos.py`. Mandatory and easy to miss — the M5 repo has no `.gitmodules` and its CMake never mentions mooncake. |
| Newly fetched deps ignored | `idf.py fullclean`. CMake caches the component list, so `components/` appearing mid-build needs a reconfigure. |
| Acks stop working after upgrading one board | **Both ends must run the same esp-now version.** 2.5.2 vs 2.5.3 is a silent wire-compat risk. |
| `git apply` fails on the Cardputer patch | You're not on base commit `b549eac`. Check out that commit, or copy `cardputer/patched-sources/main/` over `main/` instead. |

---

## What is not a bug

- **A powered-off Cardputer misses messages permanently.** ESP-NOW has no
  store-and-forward, retry, or delivery guarantee in the protocol. The ack scheme
  lets the host retry through brief absence or interference — nothing more. Treat
  delivery as best-effort and check the `delivered` flag.
- **`answered: false` from `ask_user`.** The timeout expired with no reply. It is
  not consent.
- **Messages truncated near 230 characters.** `BRIDGE_LINE_MAX` is 240 bytes; the
  MCP layer truncates to 227 + `...`. Keep messages under ~200.
- **Only the newest 32 messages survive on the device.** Oldest are dropped by
  design.
- **`wifi=unprovisioned(fallback-ch1)` on first boot.** Normal, and reported on the
  wire rather than failing silently.

For the full build log with every gotcha in context, see `../HARDWARE_NOTES.md`.
