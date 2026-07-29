# Getting Started

End-to-end setup. Budget an hour the first time, most of it waiting on two
ESP-IDF builds.

Order matters: flash the **Cardputer first**, because the C5 needs to join the
same AP the Cardputer is on, and you want to be able to check the Cardputer's
channel while provisioning.

---

## 0. Prerequisites

```bash
# ESP-IDF v5.5 — earlier versions will not build the C5 target
source ~/esp/esp-idf/export.sh
idf.py --version        # expect v5.5.x

# Python side
python3 -m venv .venv
.venv/bin/pip install pyserial mcp
```

You also need a **2.4 GHz** SSID + PSK that both boards can reach. A 5 GHz-only
network cannot work — the Cardputer's S3 has no 5 GHz radio.

---

## 1. Flash the Cardputer

The Cardputer code is a **patch on M5's firmware**, not a standalone project.

```bash
git clone https://github.com/m5stack/M5Cardputer-UserDemo.git
cd M5Cardputer-UserDemo
git checkout b549eac                  # the base the patch was cut against

python3 fetch_repos.py                # MANDATORY — pulls M5GFX/M5Unified/mooncake/…

git apply "path/to/cardputer/espnow-notify.patch"
# (or copy cardputer/patched-sources/main/ over main/)

idf.py set-target esp32s3
idf.py build
idf.py -p /dev/cu.usbmodem<N> flash monitor
```

**`fetch_repos.py` is not optional and is easy to miss** — the M5 repo has no
`.gitmodules` and its CMake never mentions mooncake, so skipping it surfaces only
as `fatal error: mooncake.h: No such file or directory`.

If `components/` appears *during* a build, run `idf.py fullclean` — CMake caches
the component list, so freshly fetched deps don't take effect until reconfigure.

### What the patch does to the device

ESP-NOW now comes up **at boot** rather than when you open the Chat app, incoming
messages land in a bounded 32-message queue, and `Hal::update()` chirps three
rising tones (2200 / 2900 / 3600 Hz at volume 220) from whatever app is
foreground. Full rationale in `../cardputer/README.md`.

> **Trade-off you're accepting:** `espNowInit()` disconnects any station
> connection, so **SetWiFi and NTP time sync stop working** while notifications
> are active. Call `espNowDeinit()` to hand the radio back.

### Note the Cardputer's channel

If you join the Cardputer to your AP, it follows that AP's channel. If you leave
it off WiFi, ESP-NOW owns the radio outright and it sits on **channel 1**. Either
is fine — you just need the C5 to end up on the same one, which step 3 handles.

---

## 2. Flash the ESP32-C5 bridge

```bash
cd bridge-esp32c5

idf.py --preview set-target esp32c5   # --preview is REQUIRED
python3 apply_c5_patches.py           # REQUIRED, idempotent
idf.py build
idf.py -p /dev/cu.usbmodem<N> flash monitor
```

Two things that are load-bearing:

- **`--preview`.** `esp32c5` is a preview target in IDF v5.5. Plain
  `idf.py set-target esp32c5` *prints a notice and silently keeps the old
  target* — it builds for xtensa `esp32` and dies on missing
  `usb_serial_jtag_*`.
- **`apply_c5_patches.py`.** esp-now 2.5.3 doesn't compile for the C5: `rom/rtc.h`
  is gone, a `g_rom_flashchip` include is missing, and
  `rx_ctrl->secondary_channel` no longer exists. The script fixes all three,
  idempotently, and **touches nothing on the wire**.

Both ends are pinned to `esp-now ==2.5.3` — matching versions is a wire-compat
requirement, and M5 ships a `dependencies.lock` pinning 2.5.2 despite a `^2.5.2`
manifest, so it must be pinned forward.

You should see:

```
READY mac=aa:bb:cc:dd:ee:ff channel=1 band=2.4GHz wifi=unprovisioned(fallback-ch1)
INFO send a line to broadcast it; RX/ACK lines are inbound
```

`unprovisioned(fallback-ch1)` is the **normal first-boot state**, not an error.
Fix it next.

---

## 3. Provision WiFi on the C5

Credentials live in **NVS, not in the source** — a hardcoded PSK would travel
with every copy of the repo and end up in the built binary. NVS survives
`idf.py flash` (that only rewrites the app partition), so this is a one-time step
per board.

Stop any serial owner first, then:

```bash
.venv/bin/python server/provision_wifi.py --ssid "YourSSID"   # prompts for the PSK
# or: --password "..."   /   --open   for an open network
# --port to override autodetection
```

The board reboots, joins, and re-reports:

```
READY mac=aa:bb:cc:dd:ee:ff channel=8 band=2.4GHz wifi=YourSSID
```

**That `channel=` must match the Cardputer's.** Joining the same AP is what makes
them agree. If the join fails you'll get `join-failed(fallback-ch1)` — explicit,
rather than a silent failure.

---

## 4. Run the server

```bash
.venv/bin/python server/webapp.py &     # owns the serial port
# open http://127.0.0.1:8765
```

Optional flags: `--port` (serial override), `--http-port` (default `8765`). It's
**bound to localhost only**.

The port is auto-detected by probing each `/dev/cu.usbmodem*` for the `READY`
banner — port numbers shift between plug-ins, so nothing hardcodes them. Probing
takes **up to ~15 s**, so an immediate `curl` can look like a failure when it
isn't.

Send yourself a test message from the web UI. The Cardputer should chirp, and the
message should show as acked with an attempt count.

Click **Enable browser alerts** once. Notifications then fire twice on purpose —
a native macOS banner via `osascript` (reaches you with the browser closed) and a
Web Notification (reaches you with the browser open on another tab). Either alone
leaves a gap.

---

## 5. Register the MCP server

`mcp_server.py` is launched by your MCP client over stdio — never by hand.

**Claude Code:**

```bash
claude mcp add espnow-cardputer --scope user -- \
  /absolute/path/to/.venv/bin/python \
  /absolute/path/to/server/mcp_server.py
```

`--scope user` registers it in `~/.claude.json` so it works in every project.
Confirm `✔ Connected` with `claude mcp list`.

**Any other client** (`claude_desktop_config.json`, `.mcp.json`, …):

```json
{
  "mcpServers": {
    "espnow-cardputer": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server/mcp_server.py"],
      "env": { "ESPNOW_WEBAPP_URL": "http://127.0.0.1:8765" }
    }
  }
}
```

`ESPNOW_WEBAPP_URL` is optional; that's the default. Use absolute paths — the
client's working directory is not yours.

Then ask your assistant to send you a test notification and confirm it reports
`delivered: true`. See [Using It](Using-It.md).

---

## 6. Auto-start at login (macOS, optional)

Install `webapp.py` as a user LaunchAgent so the radio is always up. Use the
template in `server/local.espnow.webapp.plist.template` — run this **from the
project root**:

```bash
mkdir -p results        # launchd will NOT create the log directory

sed -e "s|{{PYTHON}}|$PWD/.venv/bin/python|g" \
    -e "s|{{PROJECT_DIR}}|$PWD|g" \
    server/local.espnow.webapp.plist.template \
    > ~/Library/LaunchAgents/local.espnow.webapp.plist

plutil -lint ~/Library/LaunchAgents/local.espnow.webapp.plist    # expect: OK
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.espnow.webapp.plist
```

Managing it afterwards:

```bash
launchctl print gui/$(id -u)/local.espnow.webapp | grep -E 'state|pid|last exit'
launchctl kickstart -k gui/$(id -u)/local.espnow.webapp     # restart
launchctl bootout gui/$(id -u)/local.espnow.webapp          # stop & disable
```

Four things in the template are load-bearing, and each one cost real time:

- **`PYTHONUNBUFFERED=1`** — without it Python block-buffers stdout when it isn't
  a tty, and your logs stay empty exactly when you need them.
- **`ThrottleInterval` 10** — without a throttle, a crash-looping webapp (C5
  unplugged, say) respawns as fast as launchd can fork.
- **`results/` must already exist.** launchd doesn't create the log directory and
  **fails silently** when it's missing.
- **Absolute paths only.** launchd expands neither `~`, `$HOME`, nor shell
  variables — which is why the template is substituted rather than sourced.

If you change `--http-port` in the plist, update `ESPNOW_WEBAPP_URL` for the MCP
server to match.

Remember to `bootout` before provisioning or flashing; the agent holds the port.

---

## Setup checklist

- [ ] Cardputer flashed with the patch, chirps on a test send
- [ ] C5 flashed with `--preview` + `apply_c5_patches.py`
- [ ] `READY` line shows a real SSID and the **same channel** as the Cardputer
- [ ] `webapp.py` running, `http://127.0.0.1:8765` loads, browser alerts enabled
- [ ] Test message from the UI shows as acked
- [ ] MCP server reports `✔ Connected`
- [ ] An assistant-sent notification returns `delivered: true`
