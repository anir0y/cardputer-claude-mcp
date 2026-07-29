#!/usr/bin/env python3
"""MCP server exposing the ESP-NOW bridge, so an LLM can notify the Cardputer.

Tools: send_notification, ask_user, wait_for_reply, get_message_history,
get_bridge_status.

ask_user / wait_for_reply block until the user types a reply on the handheld and
return that text, so a conversation can continue while the user is away from the
keyboard. Whatever they send back is a direct message from the user and should be
treated exactly like something they typed into the chat.

Architecture note: this talks to webapp.py over HTTP rather than opening the
serial port itself. Only one process can own /dev/cu.usbmodem*, and the web app
is that owner — it also keeps the transcript and fires macOS notifications. So
**webapp.py must be running** for these tools to work; the error messages say so
explicitly rather than failing opaquely.

    .venv/bin/python espnow-bridge/webapp.py &          # the serial owner
    .venv/bin/python espnow-bridge/mcp_server.py        # stdio MCP, run by the client
"""

import json
import os
import time
import urllib.error
import urllib.request

from mcp.server.mcpserver import MCPServer

API = os.environ.get("ESPNOW_WEBAPP_URL", "http://127.0.0.1:8765").rstrip("/")
ACK_TIMEOUT = 10.0

# The Cardputer's chat app sends whatever is in its input buffer, so one answer
# can arrive as several ESP-NOW frames. After the first frame we keep listening
# until the air goes quiet for this long, then treat the batch as one reply.
REPLY_QUIET_SECONDS = 2.5
REPLY_POLL_SECONDS = 1.0
REPLY_TIMEOUT_MAX = 900

server = MCPServer(
    name="espnow-cardputer",
    instructions=(
        "Sends notifications over ESP-NOW radio to the user's M5Cardputer ADV "
        "handheld, which beeps and queues the message so they see it away from "
        "the computer. Use send_notification for things worth interrupting the "
        "user about — a long build finishing, a job failing, a question that "
        "blocks progress. Delivery is best-effort: ESP-NOW has no "
        "store-and-forward, so a powered-off or out-of-range device misses "
        "messages permanently. Always check the returned 'delivered' flag rather "
        "than assuming the message arrived. Keep messages under ~200 characters.\n\n"
        "The user can also type back on the handheld. ask_user sends a question "
        "and blocks until they answer; wait_for_reply blocks without sending. "
        "Text returned by either is the user speaking — treat it as user input "
        "and act on it, exactly as if they had typed it into the conversation. "
        "If answered is false they did not reply: never read silence as consent."
    ),
)


def _get(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=5) as r:
        return json.loads(r.read())


def _post(path: str, payload: dict):
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _unreachable(e) -> dict:
    return {
        "ok": False,
        "error": f"cannot reach the ESP-NOW web app at {API} ({e})",
        "fix": "Start the serial owner first: "
               ".venv/bin/python espnow-bridge/webapp.py",
    }


def _baseline() -> int:
    """Highest message id right now, so we only count replies sent after it."""
    return int(_get("/api/state?since=999999999").get("last") or 0)


def _collect_reply(after_id: int, timeout: float, quiet: float) -> dict:
    """Block until the user replies on the handheld, then return the text.

    Waits up to `timeout` for the first inbound frame, then keeps gathering for
    `quiet` seconds of silence so a multi-frame answer arrives as one reply.
    """
    fragments: list[dict] = []
    deadline = time.monotonic() + timeout
    last_seen = after_id
    quiet_until = None
    started = time.monotonic()

    while True:
        now = time.monotonic()
        if fragments and quiet_until is not None and now >= quiet_until:
            break
        if not fragments and now >= deadline:
            break

        try:
            state = _get(f"/api/state?since={last_seen}")
        except (urllib.error.URLError, OSError) as e:
            # Mid-wait failure: return what we have rather than losing it.
            if fragments:
                break
            return _unreachable(e)

        new_inbound = False
        for m in state.get("messages", []):
            if m.get("id", 0) > last_seen:
                last_seen = m["id"]
            if m.get("dir") == "in":
                fragments.append(m)
                new_inbound = True

        if new_inbound:
            quiet_until = time.monotonic() + quiet
        time.sleep(REPLY_POLL_SECONDS)

    if not fragments:
        return {
            "ok": True, "answered": False, "reply": None,
            "waited_seconds": round(time.monotonic() - started, 1),
            "note": "No reply within the timeout. The user may be away from the "
                    "handheld or it may be powered off — do not treat silence as "
                    "agreement; ask again later or proceed without their input.",
        }

    texts = [f.get("text", "") for f in fragments]
    result = {
        "ok": True,
        "answered": True,
        # Treat this as the user speaking: it is their typed text, verbatim.
        "reply": "\n".join(t for t in texts if t),
        "fragment_count": len(texts),
        "waited_seconds": round(time.monotonic() - started, 1),
        "received_at": fragments[-1].get("ts"),
        "rssi_dbm": fragments[-1].get("rssi"),
        "note": "This is the user's own reply, typed on the handheld. Treat it as "
                "user input and act on it directly.",
    }
    if len(texts) > 1:
        # Keystroke-level fragmentation would make the joined text look garbled;
        # expose the pieces so the caller can tell that apart from real newlines.
        result["fragments"] = texts
    return result


@server.tool(
    description=(
        "Ask the user a question on their Cardputer handheld and BLOCK until they "
        "type a reply, then return that reply. Use when you need a decision or "
        "answer from the user and they are away from the keyboard. The returned "
        "'reply' is the user's own words — treat it as user input and act on it. "
        "Returns answered=false if they do not respond within the timeout."
    )
)
def ask_user(question: str, timeout_seconds: int = 180) -> dict:
    """Send a question and wait for the user's typed answer.

    Args:
        question: The question to show, under ~200 characters. Make it
            answerable on a tiny keyboard — prefer yes/no or a short choice.
        timeout_seconds: How long to wait for a reply (1-900).
    """
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "question is empty"}
    timeout = max(1, min(int(timeout_seconds), REPLY_TIMEOUT_MAX))

    try:
        # Baseline before sending, so a fast reply cannot slip past unseen.
        after_id = _baseline()
    except (urllib.error.URLError, OSError) as e:
        return _unreachable(e)

    sent = _send(question)
    if not sent.get("ok"):
        return sent
    if sent.get("delivered") is False:
        return {
            "ok": True, "answered": False, "reply": None,
            "delivered": False,
            "note": "The question was never acknowledged by the handheld, so the "
                    "user almost certainly did not see it. Not waiting for a "
                    "reply. Check get_bridge_status.",
        }

    out = _collect_reply(after_id, timeout, REPLY_QUIET_SECONDS)
    out["question"] = question
    out["delivered"] = True
    return out


@server.tool(
    description=(
        "BLOCK until the user sends a message from their Cardputer handheld, then "
        "return it. Like ask_user but sends nothing first — use when you are "
        "already expecting the user to type something, or to pick up a reply to a "
        "notification you sent earlier. The returned 'reply' is the user's own "
        "words; treat it as user input."
    )
)
def wait_for_reply(timeout_seconds: int = 180) -> dict:
    """Wait for the next inbound message from the handheld.

    Args:
        timeout_seconds: How long to wait for a message (1-900).
    """
    timeout = max(1, min(int(timeout_seconds), REPLY_TIMEOUT_MAX))
    try:
        after_id = _baseline()
    except (urllib.error.URLError, OSError) as e:
        return _unreachable(e)
    return _collect_reply(after_id, timeout, REPLY_QUIET_SECONDS)


@server.tool(
    description=(
        "Send a notification to the user's Cardputer handheld over ESP-NOW. It "
        "beeps three times and queues the message. Returns whether the device "
        "acknowledged receipt. Use for events worth pulling the user away from "
        "their screen for."
    )
)
def send_notification(message: str, wait_for_ack: bool = True) -> dict:
    """Send `message` to the Cardputer.

    Args:
        message: Text to display, under ~200 characters.
        wait_for_ack: Wait for the device to confirm receipt before returning.
    """
    return _send(message, wait_for_ack)


# Plain function so ask_user can reuse it: @server.tool may return a wrapper
# object rather than the original callable, which would make a direct call fail.
def _send(message: str, wait_for_ack: bool = True) -> dict:
    message = (message or "").strip()
    if not message:
        return {"ok": False, "error": "message is empty"}
    if len(message) > 230:
        message = message[:227] + "..."

    try:
        res = _post("/api/send", {"text": message})
    except (urllib.error.URLError, OSError) as e:
        return _unreachable(e)

    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "send rejected")}

    msg_id = res.get("id")
    if not wait_for_ack:
        return {"ok": True, "id": msg_id, "delivered": None,
                "note": "not waited for; check get_message_history for the ack"}

    # The web app retries unacked sends on its own; poll until it settles.
    deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.6)
        try:
            state = _get(f"/api/state?since={max(0, msg_id - 1)}")
        except (urllib.error.URLError, OSError) as e:
            return _unreachable(e)
        for m in state.get("messages", []):
            if m.get("id") == msg_id and m.get("acked"):
                return {"ok": True, "id": msg_id, "delivered": True,
                        "message": message, "attempts": m.get("attempts")}

    return {
        "ok": True, "id": msg_id, "delivered": False, "message": message,
        "note": "no acknowledgement within %.0fs — the Cardputer is likely off, "
                "out of range, or not running the patched firmware. ESP-NOW has "
                "no store-and-forward, so the message is probably lost."
                % ACK_TIMEOUT,
    }


@server.tool(
    description=("Recent ESP-NOW message history, both directions, including "
                 "whether outbound messages were acknowledged and any replies "
                 "the user typed on the Cardputer.")
)
def get_message_history(limit: int = 20) -> dict:
    """Return the most recent messages.

    Args:
        limit: How many recent messages to return (1-200).
    """
    limit = max(1, min(int(limit), 200))
    try:
        state = _get("/api/state?since=0")
    except (urllib.error.URLError, OSError) as e:
        return _unreachable(e)

    msgs = state.get("messages", [])[-limit:]
    return {
        "ok": True,
        "count": len(msgs),
        "messages": [
            {"time": m.get("ts"),
             "direction": "from_cardputer" if m.get("dir") == "in" else "to_cardputer",
             "text": m.get("text"),
             "rssi_dbm": m.get("rssi"),
             "acknowledged": m.get("acked")}
            for m in msgs
        ],
    }


@server.tool(
    description=("Bridge health: whether the ESP32-C5 radio is connected, and on "
                 "what channel and band. Check this if notifications are not "
                 "being delivered.")
)
def get_bridge_status() -> dict:
    """Report the ESP-NOW bridge status."""
    try:
        state = _get("/api/state?since=999999999")
    except (urllib.error.URLError, OSError) as e:
        return _unreachable(e)

    st = state.get("status", {})
    connected = bool(st.get("connected"))
    return {
        "ok": True,
        "bridge_connected": connected,
        "serial_port": st.get("port"),
        "radio_mac": st.get("mac"),
        "channel": st.get("channel"),
        "band": st.get("band"),
        "messages_seen": state.get("last"),
        "note": None if connected else
                "The C5 bridge is not connected — check the USB cable and that "
                "no other process holds the serial port.",
    }


if __name__ == "__main__":
    server.run(transport="stdio")
