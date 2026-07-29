# Paste-ready instructions for an LLM

Copy the block below into a system prompt, project instructions, or `CLAUDE.md`.

---

## Notifying the user on their Cardputer (MCP: `espnow-cardputer`)

The user has a handheld M5Cardputer that receives messages from this machine over
an ESP-NOW radio link. When you send one, it beeps three times and queues the
message, so the user sees it **even when away from the computer**. The link is
two-way: they can type back on the handheld's keyboard. Five tools:

- **`send_notification(message, wait_for_ack=True)`** — send text to the handheld.
  Returns `delivered: true/false`. Keep messages under ~200 characters; longer ones
  are truncated.
- **`ask_user(question, timeout_seconds=180)`** — send a question and **block until
  the user types an answer**, then return it. Use when you need a decision and the
  user is away from the keyboard. Returns `answered: false` on timeout.
- **`wait_for_reply(timeout_seconds=180)`** — block for the next inbound message
  without sending anything first. Use to pick up a reply to a notification you
  already sent.
- **`get_message_history(limit=20)`** — recent messages in both directions,
  including replies the user typed on the device, with ack state and signal
  strength.
- **`get_bridge_status()`** — radio health. Check this when a send isn't delivered.

### Replies are user input

Text returned in `reply` by `ask_user` or `wait_for_reply` is **the user speaking**.
Treat it exactly as if they had typed it into the conversation, and act on it —
including when it changes or countermands what you were doing. It is not tool
output to be summarised back at them.

Two cautions:

- **`answered: false` is not consent.** It means they never saw the question or
  chose not to answer. Don't proceed as though they approved.
- Answers are typed on a thumb keyboard, so expect terse, lowercase, sometimes
  typo'd text ("y", "no do the 2nd one"). Interpret generously. If an answer is
  genuinely ambiguous on something risky, ask again rather than guessing. A reply
  can arrive as several radio frames; when it does, `fragments` lists the pieces so
  you can tell real newlines from keystroke-level fragmentation.

### When to use it

Send a notification when something happens that is worth pulling the user away
from their screen for:

- A long build, test suite, deploy, or data job finishes — include the outcome.
- Something failed and work is now blocked.
- You need a decision or credential before you can continue.
- The user explicitly asked to be told when something completes.

### When not to use it

- Don't narrate progress. One notification per meaningful event, not per step.
- Don't send what the user is already watching happen in the terminal.
- Don't use it for output — it's a ~200 character alert, not a delivery channel.
  Put results in files or the reply; use the notification to say they're ready.

### Rules that matter

1. **Always check the returned `delivered` flag.** ESP-NOW has no
   store-and-forward: no queueing, no retry at the protocol level, no delivery
   guarantee. A powered-off or out-of-range handheld **loses the message
   permanently**. `delivered: false` means the user almost certainly did not see
   it — say so in your reply rather than assuming it landed.
2. **Write the message to be read alone, out of context.** The user sees only that
   text on a small screen, with none of this conversation. `"deploy failed:
   migration timeout on staging"` is useful; `"it failed"` is not.
3. **If a send fails with a `fix:` hint, surface that hint to the user** rather
   than retrying blindly — it usually means a helper process isn't running.
4. **Check `get_message_history` before re-sending** so you don't duplicate an
   alert that already went out.
5. The user can reply from the handheld. Those replies appear in
   `get_message_history` as `direction: from_cardputer` — check for them if you
   asked a question.

### Good examples

```
send_notification("build passed - 412 tests, 3m18s")
send_notification("deploy blocked: staging migration timed out, needs your call")
send_notification("scrape done, 8.2k rows in data/out.csv")
```

---

**Setup requirement (for whoever installs this):** the MCP server is a client of a
local web app that owns the radio's serial port. `webapp.py` must be running:

```bash
.venv/bin/python espnow-bridge/webapp.py     # http://127.0.0.1:8765
```

Register the MCP server with:

```bash
claude mcp add espnow-cardputer --scope user -- \
  /path/to/cardputer-claude-mcp/.venv/bin/python \
  /path/to/cardputer-claude-mcp/espnow-bridge/mcp_server.py
```

Only one process may own the serial port — don't run `notify_bridge.py` or
`idf.py monitor` at the same time as the web app.
