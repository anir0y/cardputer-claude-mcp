#!/usr/bin/env python3
"""Mac-side driver for the ESP32-C5 ESP-NOW bridge.

Sends notifications to the Cardputer and surfaces anything it sends back as a
native macOS notification, so you don't have to be watching a terminal.

    .venv/bin/python espnow-bridge/notify_bridge.py send "build finished"
    .venv/bin/python espnow-bridge/notify_bridge.py watch
    .venv/bin/python espnow-bridge/notify_bridge.py daemon      # both directions

ESP-NOW is fire-and-forget with no store-and-forward, so delivery is confirmed by
an ACK from the patched Cardputer firmware, keyed by payload CRC32. Unacked
messages are retried. A Cardputer that is powered off will still miss messages —
no amount of retrying fixes that.

The bridge port is auto-detected: port numbering shifts between plug-ins, so
hardcoding /dev/cu.usbmodemXXXX is unreliable.
"""

import argparse
import pathlib
import subprocess
import sys
import time
import zlib

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial missing: .venv/bin/pip install pyserial")

BAUD = 115200
LOG = pathlib.Path(__file__).resolve().parent.parent / "results" / "espnow-chat.log"


def crc32_hex(text: str) -> str:
    """Must match crc32_of() in bridge_main.c — standard CRC-32, same as zlib."""
    return f"{zlib.crc32(text.encode()) & 0xFFFFFFFF:08x}"


def notify(title: str, message: str):
    """Native macOS notification — reaches you regardless of focused window."""
    safe_t = title.replace('"', "'")
    safe_m = message.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_m}" with title "{safe_t}" sound name "Ping"'],
            check=False, capture_output=True, timeout=5)
    except Exception as e:
        print(f"  (notification failed: {e})")


def log_line(text: str):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def find_bridge(explicit=None, timeout=6.0):
    """Locate the C5 by its READY banner rather than trusting a port number."""
    candidates = [explicit] if explicit else [
        p.device for p in list_ports.comports() if "usbmodem" in p.device]
    for dev in candidates:
        try:
            s = serial.Serial()
            s.port = dev
            s.baudrate = BAUD
            s.timeout = 0.3
            s.dtr = False
            s.rts = False
            s.open()
        except Exception:
            continue
        # Nudge it: if the banner already scrolled past, a bare newline is
        # harmless (empty lines are ignored by the firmware).
        s.write(b"\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = s.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith(("READY", "INFO", "SENT", "RX", "ACK")):
                print(f"bridge on {dev}: {line}")
                return s
        s.close()
    return None


def pump(s, on_rx=None, on_ack=None, seconds=None):
    """Read protocol lines, ignoring ESP-IDF log noise on the same link."""
    deadline = None if seconds is None else time.monotonic() + seconds
    while deadline is None or time.monotonic() < deadline:
        raw = s.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("RX "):
            parts = line.split(" ", 3)
            if len(parts) == 4 and on_rx:
                on_rx(parts[1], parts[2], parts[3])
        elif line.startswith("ACK ") and on_ack:
            on_ack(line.split(" ")[1])
        elif line.startswith(("SENT", "ERR", "READY", "INFO")):
            print(f"  < {line}")


def send_with_retry(s, text, attempts=4, wait=1.5):
    want = crc32_hex(text)
    acked = {"ok": False}

    def on_ack(crc):
        if crc.lower() == want:
            acked["ok"] = True

    for i in range(1, attempts + 1):
        s.write((text + "\n").encode())
        s.flush()
        print(f"  -> attempt {i}/{attempts}: {text!r} (crc {want})")
        pump(s, on_ack=on_ack, seconds=wait)
        if acked["ok"]:
            print(f"  ACKed by Cardputer")
            log_line(f"TX-ACKED {text}")
            return True
    print("  no ACK — Cardputer may be off, out of range, or running "
          "unpatched firmware (stock firmware never ACKs)")
    log_line(f"TX-UNACKED {text}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("send", "watch", "daemon"))
    ap.add_argument("text", nargs="*", help="message, for send mode")
    ap.add_argument("--port", default=None, help="override auto-detection")
    ap.add_argument("--attempts", type=int, default=4)
    args = ap.parse_args()

    s = find_bridge(args.port)
    if not s:
        sys.exit("No ESP-NOW bridge found. Is the C5 plugged in and flashed?")

    def on_rx(mac, rssi, text):
        print(f"  < RX [{mac} {rssi}dBm] {text}")
        log_line(f"RX {mac} {rssi} {text}")
        notify(f"Cardputer ({rssi} dBm)", text)

    try:
        if args.mode == "send":
            if not args.text:
                sys.exit("nothing to send")
            send_with_retry(s, " ".join(args.text), attempts=args.attempts)
        elif args.mode == "watch":
            print("Watching for messages (Ctrl-C to stop). Notifications enabled.")
            pump(s, on_rx=on_rx)
        else:
            print("Daemon: type a line to send, inbound messages notify. Ctrl-C to stop.")
            import threading
            threading.Thread(target=pump, args=(s,), kwargs={"on_rx": on_rx},
                             daemon=True).start()
            for line in sys.stdin:
                line = line.strip()
                if line:
                    s.write((line + "\n").encode())
                    s.flush()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        s.close()
        print(f"transcript: {LOG}")


if __name__ == "__main__":
    main()
