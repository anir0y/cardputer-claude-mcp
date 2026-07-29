# Server side — MCP server + web app

Two processes on the Mac. **`webapp.py` owns the serial port; `mcp_server.py`
talks to it over HTTP.** Only one process can open `/dev/cu.usbmodem*`, so this
split is deliberate — the web app is the single owner, keeps the transcript, and
fires the macOS notifications.

```
Claude ──stdio MCP──> mcp_server.py ──HTTP──> webapp.py ──USB serial──> ESP32-C5
                                                  │
                                            browser UI + macOS banner
```

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install pyserial mcp

.venv/bin/python webapp.py &        # the serial owner — must be running
# open http://127.0.0.1:8765
```

`mcp_server.py` is launched by the MCP client (Claude Code / Claude Desktop),
not by hand. Register it as a stdio server:

```json
{
  "mcpServers": {
    "espnow-cardputer": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server/mcp_server.py"]
    }
  }
}
```

Override the web app location with `ESPNOW_WEBAPP_URL` (default
`http://127.0.0.1:8765`).

**If `webapp.py` is not running, every tool fails** — the errors say so
explicitly rather than failing opaquely.

## Files

| File | What it is |
|---|---|
| `mcp_server.py` | The MCP server. Tools: `send_notification`, `ask_user`, `wait_for_reply`, `get_message_history`, `get_bridge_status`. Stdio transport, no serial access of its own. |
| `webapp.py` | Serial owner + local web UI on `:8765`. Routes: `/`, `/api/state`, `/api/send`. Auto-detects the `usbmodem` port. Stdlib only apart from pyserial. |
| `notify_bridge.py` | Standalone CLI sender. **Do not run at the same time as `webapp.py`** — the second process to start cannot open the port. Useful for debugging without the UI. |
| `provision_wifi.py` | Writes WiFi credentials into the C5's NVS. The bridge joins the Cardputer's AP to inherit its radio channel, so this is required setup, not optional. |
| `LLM_INSTRUCTIONS.md` | Paste-ready block for a system prompt / `CLAUDE.md` describing when the model should notify the user. |
| `local.espnow.webapp.plist.template` | macOS LaunchAgent to keep `webapp.py` running at login. Substitute `{{PYTHON}}` / `{{PROJECT_DIR}}` — launchd expands neither `~` nor shell variables. Install steps in [`wiki/Getting-Started.md`](../wiki/Getting-Started.md#6-auto-start-at-login-macos-optional). |

## Behaviour worth knowing

- **`ask_user` / `wait_for_reply` block** until the user types a reply on the
  handheld and return that text. Whatever comes back is a direct message from
  the user and should be treated exactly like something they typed into the chat.
  If `answered` is false they did not reply — silence is not consent.
- **Delivery is best-effort.** ESP-NOW has no store-and-forward. A powered-off or
  out-of-range Cardputer misses messages permanently. Check the returned
  `delivered` flag; don't assume arrival. Acks are keyed by payload CRC32.
- **Notifications fire twice on purpose** — a native macOS banner via `osascript`
  (reaches you with the browser closed) and a Web Notification (reaches you with
  the browser open on another tab). Either alone leaves a gap.
- Keep messages under ~200 characters.
