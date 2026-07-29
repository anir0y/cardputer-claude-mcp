# cardputer + Claude MCP

Claude notifies an **M5Cardputer ADV** over ESP-NOW, and the user can type
replies back on the handheld while away from the computer.

```
Claude ──stdio MCP──> mcp_server.py ──HTTP──> webapp.py ──USB serial──> ESP32-C5
                                                                          │
                                                                   ESP-NOW 2.4GHz
                                                                          ↓
                                                                  Cardputer ADV
        <──────────────────────── replies ───────────────────────────────┘
```

The Mac has no ESP-NOW radio, so the ESP32-C5 acts as its transmitter. The
Cardputer runs a patched build of M5's own `M5Cardputer-UserDemo`.

## Layout

| Directory | What's in it |
|---|---|
| `server/` | **The MCP server and web app.** `mcp_server.py` (stdio MCP, 5 tools) + `webapp.py` (serial owner, UI on `:8765`) + CLI/provisioning helpers. See `server/README.md`. |
| `cardputer/` | **The Cardputer firmware code we wrote** — a patch on M5's `M5Cardputer-UserDemo` that makes it queue and chirp notifications from any app. See `cardputer/README.md`. |
| `bridge-esp32c5/` | The ESP32-C5 bridge firmware (`main/bridge_main.c`). The Mac's radio. Nothing is delivered without it. |
| `wiki/` | **User-facing docs — start here.** Setup walkthrough, tool reference, protocol spec, troubleshooting. |
| `HARDWARE_NOTES.md` | Full build write-up: channels, WiFi/ESP-NOW trade-off, C5 patches, every gotcha hit. |

## Start here

**→ [`wiki/Home.md`](wiki/Home.md)**

| Page | For |
|---|---|
| [Getting Started](wiki/Getting-Started.md) | Building it from scratch: toolchain, flashing both boards, provisioning, MCP registration |
| [Using It](wiki/Using-It.md) | The five MCP tools, web UI, CLI, when the LLM should interrupt you |
| [Protocol Reference](wiki/Protocol-Reference.md) | Serial wire format, ESP-NOW ack scheme, HTTP API |
| [Troubleshooting](wiki/Troubleshooting.md) | Nothing arrives, or a build fails |

The `wiki/` pages use flat `Page-Name.md` naming, so the folder can be pushed
straight to a GitHub wiki (`git clone <repo>.wiki.git`) with no rewriting.

## The one failure mode to check first

**Both ends must be on the same radio channel.** ESP-NOW peers only hear each
other on the same channel, and a station-mode ESP32 is locked to whatever channel
its AP uses. The C5 therefore joins the same AP as the Cardputer and *inherits*
its channel rather than pinning one. Its `READY` line reports what it landed on:

```
READY mac=aa:bb:cc:dd:ee:ff channel=8 band=2.4GHz wifi=YourSSID
```

A channel mismatch looks exactly like a dead device — `TX-SENT` with no `TX-ACK`.

## What's deliberately not in this repo

- **Build output** (`build/`, `sdkconfig`, `managed_components/`) — regenerate
  with the `idf.py` steps in [Getting Started](wiki/Getting-Started.md).
- **Vendored M5 sources.** The Cardputer side ships as a *patch* against upstream
  `b549eac` plus the handful of files it touches, not a fork of the whole demo.
- **WiFi credentials.** They live in the C5's NVS, written by
  `server/provision_wifi.py`. A hardcoded PSK would travel with every clone and
  land in the built binary.
- **Your transcript.** `results/` is gitignored — it holds your message history.

## License

MIT. `cardputer/patched-sources/` contains M5Stack files (also MIT) modified by
this project, with their original SPDX headers intact — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
