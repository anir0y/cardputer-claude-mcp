# cardputer + Claude MCP

Let Claude (or any MCP client) notify you on an **M5Cardputer ADV** over ESP-NOW,
and let you **type replies back** on the handheld while you're away from the
computer. The Cardputer beeps three times, queues the message, and acknowledges
it so the sender knows it landed.

```
Claude ──stdio MCP──> mcp_server.py ──HTTP──> webapp.py ──USB serial──> ESP32-C5
                                                  │                        │
                                          browser UI + macOS         ESP-NOW 2.4GHz
                                             notification                  ↓
                                                                   Cardputer ADV
        <──────────────────────── replies ─────────────────────────────────┘
```

The Mac has no ESP-NOW radio, so a USB-attached **ESP32-C5 acts as its
transmitter**. The Cardputer runs a patched build of M5's own
`M5Cardputer-UserDemo`.

## Pages

| Page | Read it when |
|---|---|
| **[Getting Started](Getting-Started.md)** | Setting this up from scratch — parts list, toolchain, flashing both boards, provisioning, registering the MCP server. |
| **[Using It](Using-It.md)** | It's built and you want to use it — the five MCP tools, the web UI, the CLI, and how to tell your LLM when to interrupt you. |
| **[Protocol Reference](Protocol-Reference.md)** | You're extending it or writing your own client — serial wire format, HTTP API, the CRC32 ack scheme, hard limits. |
| **[Troubleshooting](Troubleshooting.md)** | Nothing arrives, or a build fails. **Start with the channel check.** |

Deeper background lives outside the wiki: `../cardputer/README.md` (why the
firmware needed patching), `../server/README.md` (why the process split exists),
`../HARDWARE_NOTES.md` (the full build log, including every gotcha hit).

## What you need

- **M5Cardputer ADV** (ESP32-S3)
- **ESP32-C5 dev board** (must be C5 — see below)
- A **2.4 GHz** WiFi AP both boards can see
- A Mac (the notification path uses `osascript`; everything else is portable)
- ESP-IDF **v5.5**, Python 3.9+

## Three things that will save you an afternoon

1. **Both radios must be on the same channel.** ESP-NOW peers can't hear each
   other otherwise, and a station-mode ESP32 is locked to its AP's channel. The
   C5 joins the *same AP as the Cardputer* to inherit its channel rather than
   pinning one. A mismatch looks exactly like a dead device.
2. **`webapp.py` must be running.** It is the sole owner of the serial port; the
   MCP server is a thin HTTP client to it. Never run `webapp.py`,
   `notify_bridge.py`, or `idf.py monitor` at the same time.
3. **Delivery is best-effort.** ESP-NOW has no store-and-forward. A Cardputer
   that is powered off misses messages *permanently* — the ack scheme lets the
   Mac retry through brief absence or interference, nothing more. Always check
   the returned `delivered` flag instead of assuming arrival.

## Why an ESP32-C5 specifically

It's dual-band, and the firmware pins it to 2.4 GHz with
`esp_wifi_set_band(WIFI_BAND_2G)` because the Cardputer's S3 is 2.4-only. It is
also a **preview target in IDF v5.5**, which drives two build requirements
covered in [Getting Started](Getting-Started.md): `--preview` on `set-target`,
and `apply_c5_patches.py` to make esp-now 2.5.3 compile at all.

Any ESP32 with a working ESP-NOW stack and USB serial could stand in, but you'd
be re-deriving the band pinning and the patches yourself.
