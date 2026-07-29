# Protocol Reference

For extending the system or writing your own client. Three layers: the USB serial
line protocol, the ESP-NOW payload conventions, and the web app's HTTP API.

---

## Layer 1 — USB serial (Mac ↔ ESP32-C5)

Line-oriented ASCII over `usb_serial_jtag`. `BRIDGE_LINE_MAX` is **240 bytes**.

### Host → bridge

Anything up to `\n` is one message, broadcast over ESP-NOW as-is. `\r` is
skipped. An over-long line is **sent as-is rather than silently truncated into
the next message** — a deliberate choice so you lose the tail instead of
corrupting the following message.

### Bridge → host

| Line | Meaning |
|---|---|
| `READY mac=<MAC> channel=<N> band=<2.4GHz\|other> wifi=<state>` | Boot banner. Parse `mac=`/`channel=`/`band=`; extra fields are ignored by design, so you can add more without breaking clients. |
| `INFO send a line to broadcast it; RX/ACK lines are inbound` | Static hint after `READY`. |
| `RX <MAC> <rssi> <text>` | Inbound message from a peer. |
| `ACK <crc32-hex> <MAC> <rssi>` | Delivery receipt, keyed by payload CRC32. |
| `ERR send <err>` | A send failed. |

`wifi=` is one of the joined SSID, `join-failed(fallback-ch1)`, or
`unprovisioned(fallback-ch1)`. The last is a **normal first-boot state**.

**Port discovery: parse the `READY` banner, don't hardcode a device path.** USB
port numbers shift between plug-ins (the C5 and Cardputer swapped between
`14201`/`14301` during development). `webapp.py` probes each
`/dev/cu.usbmodem*` for the banner, tolerating up to ~5 s per candidate.

A probe **must not be able to kill your process**. Guarding `Serial.open()` isn't
enough — the readline probe itself can raise `SerialException: [Errno 6] Device
not configured` on an unrelated port (an nRF dongle, or a port still settling at
login). Log it and move to the next candidate.

---

## Layer 2 — ESP-NOW payloads

Broadcast (`ESPNOW_ADDR_BROADCAST`), `ESPNOW_DATA_TYPE_DATA`, no encryption. The
payload is raw text with one reserved convention:

```
"\x01" "ACK:" <crc32 of the original payload, %08x lowercase hex>
```

Anything starting with `\x01ACK:` is **control traffic, never a displayed
message**. Both ends filter it out of their message queues.

### The ack scheme

1. Cardputer receives a payload, computes CRC32 (standard reflected poly
   `0xEDB88320`, init `0xFFFFFFFF`, final xor) and queues an ack string.
2. The ack is broadcast from `Hal::update()`, **not** from the receive callback —
   sending from the callback risks blocking the espnow task.
3. The C5 sees the `\x01ACK:` prefix and emits `ACK <crc32> <mac> <rssi>`.
4. The host matches the CRC to its outbound message and marks it delivered.

Keying on payload CRC rather than a sequence number means **no framing change and
no visible message text change** — an unpatched receiver still shows the message
correctly, it just never acks.

The host log uses a shorter `crc8` for `TX-SENT`/`TX-ACK` correlation; the wire
uses the full CRC32.

### Channel constraint

ESP-NOW peers only hear each other **on the same channel**, and a station-mode
ESP32 is locked to whatever channel its AP uses. So the bridge **joins the same
AP as the Cardputer and inherits its channel** rather than pinning one, falling
back to **channel 1** (where a Cardputer sits when ESP-NOW owns the radio
outright) if the AP is unreachable.

This replaced a hardcoded `#define ESPNOW_CHANNEL 1`, an assumption that held only
while the Cardputer stayed off WiFi. Once it joined an AP on channel 8, every send
was transmitted correctly and heard by nobody.

The C5 is dual-band and the Cardputer's S3 is not, so the C5 pins itself with
`esp_wifi_set_band(WIFI_BAND_2G)` and reports the band in `READY`.

### Hard limits

- **No store-and-forward, retry, or delivery guarantee in the protocol.** The ack
  scheme lets the *host* retry, which covers brief absence or interference. A
  powered-off device misses messages permanently.
- **Both ends must run the same esp-now version** — 2.5.2 vs 2.5.3 is a silent
  wire-compat risk. Both are pinned to `==2.5.3`.

### Device-side queue

32 messages (`ESPNOW_QUEUE_MAX`), **oldest dropped when full** — for a notifier
the newest matters most. Guarded by a mutex; the receive callback only sets an
atomic flag, because it runs on the espnow task where touching M5 objects
(speaker/display) isn't safe.

---

## Layer 3 — HTTP API (`webapp.py`)

Bound to **`127.0.0.1` only**, default port `8765`. Override the base URL for
clients with `ESPNOW_WEBAPP_URL`.

### `GET /api/state?since=<id>`

Returns messages with `id > since`, plus bridge status. `since=0` for the whole
transcript; a very large `since` for status only.

```jsonc
{
  "messages": [
    {"id": 42, "ts": "...", "dir": "out", "text": "build finished",
     "acked": true, "attempts": 1, "rssi": null},
    {"id": 43, "ts": "...", "dir": "in",  "text": "on my way",
     "acked": null, "attempts": null, "rssi": -61}
  ],
  "status": { "port": "...", "mac": "...", "channel": 8, "band": "2.4GHz" }
}
```

`dir` is `"out"` (to the Cardputer) or `"in"` (from it). `rssi` is populated
inbound only; `acked`/`attempts` outbound only.

### `POST /api/send`

```jsonc
// request
{"text": "build finished"}
// response
{"ok": true, "id": 42}
```

Returns as soon as the send is queued. **Ack state arrives later** — poll
`/api/state?since=<id-1>` and watch for `acked` on your `id`. The web app retries
unacked sends itself: **3 attempts, 2.5 s apart**.

### `GET /`

The UI. Nothing depends on it programmatically.

### Client pattern

To send-and-confirm, mirror `mcp_server.py`: baseline your position with a
`/api/state` call *before* sending so a fast reply can't slip past unseen, POST
the send, then poll until the ack lands or a deadline passes.

---

## Why the process split

Only one process can own `/dev/cu.usbmodem*`. `webapp.py` is that owner, so the
web UI, macOS notifications, MCP tools, and the transcript all share **one radio
and one history**. The MCP server holds no serial handle at all.

Consequence: `webapp.py`, `notify_bridge.py`, and `idf.py monitor` are mutually
exclusive. Run one.

---

## Persistence

Transcript: `results/espnow-chat.log`, replayed into history on startup.
LaunchAgent logs: `results/webapp.{out,err}.log`.
WiFi credentials: **C5 NVS**, namespace `wifi`, keys `ssid` / `pass` — never in
source, and they survive `idf.py flash` (which only rewrites the app partition).
A missing `pass` key is treated as an empty PSK, since an open network is
legitimate.
