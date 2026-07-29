# Mac ↔ Cardputer notifications over ESP-NOW

Send notifications from the Mac to an M5Cardputer ADV, with native macOS
notifications for anything coming back. The user can type replies on the
handheld, and an LLM can block on them via the MCP `ask_user` tool.

```
Mac ──USB serial──> ESP32-C5 (bridge) ──ESP-NOW 2.4GHz──> Cardputer ADV
    <──────────────────── replies ─────────────────────
```

Both ends must share a radio channel; the bridge joins the Cardputer's AP to
inherit it. See "Channels" below — it's the first thing to check when nothing
arrives.

The Mac has no ESP-NOW radio, so the C5 acts as its transmitter. The Cardputer
runs a patched build of M5's own `M5Cardputer-UserDemo`.

## Why the Cardputer firmware needed patching

Stock firmware cannot do background notifications. From `hal.cpp`:

```cpp
_espnow_received_data = std::string((char*)data, size);   // single string!
```

- **One slot, overwritten** by every message — not a queue
- Only ever read from `AppChat::onRunning()`, so nothing happens with Chat closed
- `espNowInit()` is called from `AppChat::onOpen()`, so **nothing listens after a
  fresh boot** until you open Chat once
- No alert of any kind

Interestingly `AppChat::onClose()` does *not* deinit ESP-NOW, so the radio does
keep running once Chat has been opened — the messages arrive and are then thrown
away. Confirmed by reading the source, not guessed.

### The patch (`vendor/m5-userdemo`)

`vendor/` is gitignored, so the patch itself is committed at
`../patches/m5-userdemo-espnow.patch` — see `patches/README.md` for the base
commit and how to re-apply it to a fresh clone.

| Change | Location |
|---|---|
| ESP-NOW init moved to boot | `Hal::init()` |
| Bounded 32-message queue, oldest dropped when full | `hal.cpp` receive handler |
| Two-chirp audible alert from any app | `Hal::update()`, called by `main.cpp`'s loop |
| ACK reply keyed by payload CRC32 | receive handler + `espNowNotifyUpdate()` |
| ACK traffic filtered out of the chat queue | `\x01ACK:` prefix |

`Hal::update()` is the key insight — `main.cpp:48` calls it every loop iteration
regardless of the foreground app, so no launcher/UI surgery was needed for a
cross-app alert. The speaker is deliberately driven from there and **not** from
the ESP-NOW callback, which runs on the espnow task where touching M5 objects
isn't safe.

## Channels: both ends must agree

**This is the failure mode to check first when nothing is delivered.** ESP-NOW
peers only hear each other on the same channel, and a station-mode ESP32 is
locked to whatever channel its AP uses.

The C5 therefore **joins the same AP as the Cardputer and inherits its channel**,
rather than pinning one. Credentials come from NVS — see "WiFi provisioning"
below. Its `READY` line reports what it actually landed on:

```
READY mac=aa:bb:cc:dd:ee:ff channel=8 band=2.4GHz wifi=YourSSID
```

If the AP can't be reached it falls back to **channel 1**, which is where a
Cardputer sits when ESP-NOW owns its radio outright.

This replaced a hardcoded `#define ESPNOW_CHANNEL 1`. That assumption held only
while the Cardputer stayed off WiFi; once it joined an AP on channel 8, every
send was transmitted correctly and heard by nobody — `TX-SENT` with no `TX-ACK`,
which looks exactly like a dead device.

## Trade-off: WiFi vs. ESP-NOW on the Cardputer

`espNowInit()` on the Cardputer disconnects any station connection, so SetWiFi
and NTP time sync won't work while notifications are active that way. Call
`espNowDeinit()` to hand the radio back. When the Cardputer *is* associated with
an AP, ESP-NOW still works — it just follows the AP's channel, which is why the
bridge now does the same.

## WiFi provisioning

Credentials are **not** in `bridge_main.c`; a hardcoded PSK would travel with
every copy of the source and land in the built binary. They live in NVS, which
survives
`idf.py flash` (that only rewrites the app partition), so this is a one-time step
per board. Stop the serial owner first — only one process can hold the port:

```bash
launchctl bootout gui/$(id -u)/local.espnow.webapp
./provision_wifi.py --ssid YourSSID          # prompts for the PSK
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.espnow.webapp.plist
```

The board reboots, joins, and reports its channel. Unprovisioned boards say so on
the wire (`wifi=unprovisioned(fallback-ch1)`) instead of failing silently.

## Hard limit: no store-and-forward

ESP-NOW has no queueing, retry, or delivery guarantee in the protocol. The ACK
scheme lets the Mac retry, which covers brief absence or interference — but a
Cardputer that is **powered off misses messages permanently**. Nothing in this
design changes that. Treat it as best-effort.

## Usage

```bash
# one-shot, retries until ACKed
.venv/bin/python espnow-bridge/notify_bridge.py send "build finished"

# listen, and raise a macOS notification per inbound message
.venv/bin/python espnow-bridge/notify_bridge.py watch

# both directions; type to send
.venv/bin/python espnow-bridge/notify_bridge.py daemon
```

The bridge port is **auto-detected via its `READY` banner** — port numbers shift
between plug-ins (the C5 and Cardputer swapped between `14201`/`14301` during
development), so nothing hardcodes them. Transcript lands in
`results/espnow-chat.log`.

## Build

```bash
source ~/esp/esp-idf/export.sh        # IDF v5.5

# C5 bridge — note --preview, esp32c5 is not a stable target in v5.5
cd espnow-bridge
idf.py --preview set-target esp32c5
python3 apply_c5_patches.py            # required, see below
idf.py build && idf.py -p <c5-port> flash

# Cardputer
cd vendor/m5-userdemo
python3 fetch_repos.py                 # REQUIRED: pulls M5GFX/M5Unified/mooncake/…
idf.py set-target esp32s3
idf.py build && idf.py -p <cardputer-port> flash
```

## Web app

```bash
.venv/bin/python espnow-bridge/webapp.py     # then open http://127.0.0.1:8765
```

Shows full history both directions, send box, live bridge status (port, MAC,
channel, band), and per-message ack state with retry counts. Bound to
**localhost only**.

Notifications fire **twice on purpose**: a native macOS banner via `osascript`
(reaches you with the browser closed) and a Web Notification (reaches you with the
browser open on another tab). Either alone leaves a gap. Click *Enable browser
alerts* once to grant the second.

History is seeded from `results/espnow-chat.log` on startup, so restarts don't
lose the transcript.

## MCP server — let an LLM notify you

```bash
claude mcp add espnow-cardputer --scope user -- \
  /path/to/cardputer-claude-mcp/.venv/bin/python \
  /path/to/cardputer-claude-mcp/espnow-bridge/mcp_server.py
```

Registered in `~/.claude.json` (user scope, so it works in every project).
Verified `✔ Connected`, and a `send_notification` call returned
`delivered: true` on the first attempt.

| Tool | Purpose |
|---|---|
| `send_notification(message, wait_for_ack=True)` | Beeps the Cardputer; returns whether it acked |
| `get_message_history(limit=20)` | Both directions, with ack state and RSSI |
| `get_bridge_status()` | Radio health — check this when delivery fails |

**`webapp.py` must be running.** Only one process can own the serial port, and the
web app is that owner — the MCP server is a thin HTTP client to it. That's
deliberate: it means the web UI, the macOS notifications, and the LLM all share
one transcript and one radio. The tools return an explicit `fix:` hint if the web
app isn't up, rather than failing opaquely.

The server's `instructions` tell the model to use it for things worth interrupting
a human over (build finished, job failed, blocked question) and to **check the
returned `delivered` flag** rather than assume arrival — since ESP-NOW can't
guarantee it.

### Auto-start at login

Installed as a user LaunchAgent: `~/Library/LaunchAgents/local.espnow.webapp.plist`

```bash
launchctl print gui/$(id -u)/local.espnow.webapp | grep -E 'state|pid|last exit'
launchctl kickstart -k gui/$(id -u)/local.espnow.webapp     # restart
launchctl bootout gui/$(id -u)/local.espnow.webapp          # stop & disable
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.espnow.webapp.plist
```

`KeepAlive` restarts it if it dies, with a 10 s throttle. Logs go to
`results/webapp.{out,err}.log`, with `PYTHONUNBUFFERED=1` set — without it Python
block-buffers stdout when it isn't a tty and the logs stay empty exactly when you
need them.

**It takes up to ~15 s after login to serve**, because it probes each
`/dev/cu.usbmodem*` for up to 5 s looking for the bridge's `READY` banner. An
immediate `curl` right after boot will look like a failure when it isn't.

Two bugs the LaunchAgent exposed, both fixed:

- **Serial probing could kill the process.** `_open()` guarded `Serial.open()` but
  not the readline probe, so a candidate port that raises
  `SerialException: [Errno 6] Device not configured` — the nRF dongle, or a port
  still settling at login — crashed startup. It now logs and moves to the next
  candidate. This failed on *every* login before the fix.
- **Ack state wasn't persisted**, so restored history always showed outbound
  messages as unacked. Sends now log `TX-SENT <crc8> <text>` and acks log
  `TX-ACK <crc8>`; the loader folds them together. Old-format lines still parse.

### Only one serial owner

`webapp.py`, `notify_bridge.py`, and `idf.py monitor` all want the same port.
Run **one** at a time. `webapp.py` is the one to keep running, since the MCP server
and the browser both work through it.

## Gotchas hit while building this

Recorded because every one of them cost real time:

1. **`esp32c5` is preview in IDF v5.5.** Plain `idf.py set-target esp32c5`
   *prints a notice and silently keeps the old target* — it built for xtensa
   `esp32` and failed on missing `usb_serial_jtag_*`. Must use `--preview`.
2. **esp-now 2.5.3 doesn't compile for C5** — three separate breakages
   (`rom/rtc.h` gone, `g_rom_flashchip` include missing, `rx_ctrl->secondary_channel`
   absent). `apply_c5_patches.py` fixes all three, idempotently. None touch the
   wire format.
3. **esp-now 2.5.2 doesn't compile against IDF v5.5** at all — `esp_now_register_send_cb`
   changed signature. M5 ships a `dependencies.lock` pinning 2.5.2 even though its
   manifest says `^2.5.2`, so it must be pinned forward to `==2.5.3`.
4. **Both ends must run the same esp-now version.** They initially resolved to
   2.5.2 (Cardputer) vs 2.5.3 (bridge) — a silent wire-compat risk. Both are now
   pinned to `==2.5.3`.
5. **`fetch_repos.py` is mandatory** and easy to miss — the M5 repo has no
   `.gitmodules` and its CMake never mentions mooncake, so a missing run shows up
   only as `fatal error: mooncake.h: No such file or directory`.
6. **`components/` appearing needs a `fullclean`** — CMake caches the component
   list, so fetching deps mid-build doesn't take effect until reconfigure.
7. **The C5 must be pinned to 2.4 GHz** with `esp_wifi_set_band(WIFI_BAND_2G)`.
   It's dual-band; the Cardputer's S3 is not. The firmware prints the band in its
   `READY` line so you can confirm.
