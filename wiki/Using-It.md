# Using It

Day-to-day use once [Getting Started](Getting-Started.md) is done.

**One rule underpins everything here: `webapp.py` must be running.** It owns the
serial port; the MCP tools are HTTP clients to it. If it's down, every tool
returns an explicit `fix:` hint rather than failing opaquely.

---

## The five MCP tools

| Tool | Signature | What it does |
|---|---|---|
| `send_notification` | `(message: str, wait_for_ack: bool = True)` | Beeps the Cardputer and queues the message. Returns whether the device acked. |
| `ask_user` | `(question: str, timeout_seconds: int = 180)` | Sends a question and **blocks** until the user types a reply, then returns it. |
| `wait_for_reply` | `(timeout_seconds: int = 180)` | Blocks for the next inbound message. Sends nothing first. |
| `get_message_history` | `(limit: int = 20)` | Recent messages both directions, with ack state and RSSI. |
| `get_bridge_status` | `()` | Radio health: connected, channel, band. Check this when delivery fails. |

`timeout_seconds` is clamped to **1–900**. Messages over 230 characters are
truncated to 227 + `...`, so **keep it under ~200**.

### Reading the results

**Never assume arrival — read the `delivered` flag.**

```jsonc
// good
{"ok": true, "id": 42, "delivered": true, "message": "build finished", "attempts": 1}

// the message is probably lost
{"ok": true, "id": 43, "delivered": false, "message": "...",
 "note": "no acknowledgement within 10s — the Cardputer is likely off, out of
          range, or not running the patched firmware. ESP-NOW has no
          store-and-forward, so the message is probably lost."}
```

`send_notification` polls for up to **10 s** (`ACK_TIMEOUT`) while the web app
retries on its own — **3 attempts, 2.5 s apart**. `delivered: null` means you
passed `wait_for_ack=False` and nobody waited; check
`get_message_history` for the ack later.

### `ask_user` and `wait_for_reply`

These **block**, which is the point: the conversation can continue while you're
away from the keyboard.

```jsonc
{"ok": true, "answered": true, "reply": "yes, ship it", "question": "Deploy to prod?",
 "delivered": true}
```

Three things to know:

- **The `reply` is the user speaking.** Treat it exactly like text typed into the
  chat and act on it.
- **`answered: false` is not consent.** It means the timeout expired with no
  reply. Never read silence as approval.
- **`ask_user` refuses to wait on an undelivered question.** If the send isn't
  acked it returns `answered: false, delivered: false` immediately — waiting 180 s
  for a reply to a question nobody saw is wasted time.

A reply is collected after **2.5 s of quiet** (`REPLY_QUIET_SECONDS`), so
multi-part typing arrives as one message. When the wire splits a reply,
`fragments` exposes the pieces so you can tell that apart from real newlines.

Because the handheld keyboard is tiny, **phrase questions as yes/no or a short
choice.**

### When should the model interrupt you?

Things worth pulling a human away from their screen for: a long build finishing,
a job failing, a question that blocks progress. Not routine progress chatter.

`server/LLM_INSTRUCTIONS.md` is a paste-ready block for a system prompt or
`CLAUDE.md`. The MCP server also ships `instructions`, so a well-behaved client
picks up the same guidance automatically.

---

## The web UI — `http://127.0.0.1:8765`

Full history in both directions, a send box, live bridge status (port, MAC,
channel, band), and per-message ack state with retry counts. **Localhost only.**

History is seeded from `results/espnow-chat.log` on startup, so restarts don't
lose the transcript. Sends log `TX-SENT <crc8> <text>` and acks log
`TX-ACK <crc8>`; the loader folds them together so restored history shows correct
ack state.

This is the fastest way to answer "did that actually go out?" — and the place to
look first when the MCP tools report `delivered: false`.

---

## The CLI — `notify_bridge.py`

For debugging without the UI. **Stop `webapp.py` first** — one serial owner only.

```bash
.venv/bin/python server/notify_bridge.py send "build finished"   # retries until acked
.venv/bin/python server/notify_bridge.py watch                   # inbound → macOS notification
.venv/bin/python server/notify_bridge.py daemon                  # both ways; type to send
```

Use it to prove the radio path works independently of the HTTP layer. If `send`
gets an ack but the MCP tools don't, the problem is the web app or the MCP
registration, not the radio.

---

## Typical loops

**Notify on a long build:**

```
"Run the full test suite, then notify me on the Cardputer with the result."
```

**Block on a decision while away:**

```
"Deploy when CI is green, but ask me on the Cardputer first."
→ ask_user("CI green. Deploy to prod?", 600) → blocks → "yes" → proceeds
```

**Pick up a reply to something sent earlier:**

```
send_notification("Migration ready. Reply go/wait.")   → delivered: true
wait_for_reply(900)                                    → "go"
```

## Limits worth remembering

| Limit | Value | Why |
|---|---|---|
| Message length | ~200 chars (hard truncate at 230) | `BRIDGE_LINE_MAX` is 240 bytes |
| Device queue | 32 messages, **oldest dropped** | For a notifier the newest matters most |
| Reply timeout | 1–900 s, default 180 | |
| Ack wait | 10 s, over 3 retries 2.5 s apart | |
| Store-and-forward | **none** | A powered-off Cardputer misses messages permanently |

Something not arriving? → [Troubleshooting](Troubleshooting.md).
Writing your own client? → [Protocol Reference](Protocol-Reference.md).
