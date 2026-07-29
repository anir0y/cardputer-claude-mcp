#!/usr/bin/env python3
"""Local web UI for the ESP-NOW bridge: history, sending, and notifications.

    .venv/bin/python espnow-bridge/webapp.py
    # then open http://127.0.0.1:8765

One process must own the serial port, so do NOT run notify_bridge.py at the same
time — the second one to start will fail to open the port.

Notifications fire twice on purpose: a native macOS banner via osascript (reaches
you with the browser closed) and a Web Notification (reaches you with the browser
open on another tab). Either alone leaves a gap.

Stdlib only apart from pyserial, so there is nothing to install.
"""

import argparse
import html
import json
import pathlib
import re
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial missing: .venv/bin/pip install pyserial")

BAUD = 115200
ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "results" / "espnow-chat.log"
RETRY_ATTEMPTS = 3
RETRY_WAIT = 2.5


def crc32_hex(text: str) -> str:
    """Must match crc32_of() in bridge_main.c and the Cardputer patch."""
    return f"{zlib.crc32(text.encode()) & 0xFFFFFFFF:08x}"


def mac_notify(title: str, message: str):
    t = title.replace('"', "'")
    m = message.replace('"', "'")
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{m}" with title "{t}" sound name "Ping"'],
                       check=False, capture_output=True, timeout=5)
    except Exception:
        pass


class Bridge:
    """Owns the serial link and the message history."""

    def __init__(self, port=None):
        self.lock = threading.Lock()
        self.history = []
        self.next_id = 1
        self.pending = {}          # crc -> message id awaiting ACK
        self.status = {"port": None, "mac": None, "channel": None, "band": None,
                       "connected": False}
        self.ser = self._open(port)
        self._load_log()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._retrier, daemon=True).start()

    # ---------------------------------------------------------------- serial

    def _open(self, explicit):
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
            # Probing must not be able to kill the process. Other usbmodem
            # devices (the nRF dongle) and ports still settling at login raise
            # SerialException("[Errno 6] Device not configured") on read — that
            # used to crash startup instead of moving to the next candidate,
            # which made the LaunchAgent fail on every boot.
            try:
                s.write(b"\n")
                # Opening the port resets the bridge, and it now joins WiFi
                # before emitting READY, so first output can be ~10s away. A 5s
                # deadline silently rejected the real bridge as "not the bridge".
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    line = s.readline().decode("utf-8", "replace").strip()
                    if line.startswith(("READY", "INFO", "SENT", "RX", "ACK")):
                        self.status.update(port=dev, connected=True)
                        self._parse_ready(line)
                        print(f"bridge on {dev}: {line}", flush=True)
                        return s
            except Exception as e:
                print(f"  {dev}: not the bridge ({e})", flush=True)
            try:
                s.close()
            except Exception:
                pass
        return None

    def _parse_ready(self, line):
        m = re.search(r"mac=(\S+).*channel=(\d+).*band=(\S+)", line)
        if m:
            self.status.update(mac=m.group(1), channel=m.group(2), band=m.group(3))

    def _load_log(self):
        """Seed history from the transcript so the page isn't empty on restart."""
        if not LOG.exists():
            return
        lines = LOG.read_text(errors="replace").splitlines()[-400:]

        # Acks are logged separately from sends, so collect them first — without
        # this, restored outbound messages always showed as unacked even when
        # they had been delivered.
        acked_crcs = {ln.split(" ")[3].lower()
                      for ln in lines
                      if len(ln.split(" ")) > 3 and ln.split(" ")[2] == "TX-ACK"}

        for raw in lines:
            parts = raw.split(" ", 3)
            if len(parts) < 3:
                continue
            ts, kind = f"{parts[0]} {parts[1]}", parts[2]
            body = parts[3] if len(parts) > 3 else ""
            if kind == "RX":
                bits = body.split(" ", 2)
                self._add("in", bits[2] if len(bits) > 2 else body,
                          mac=bits[0] if bits else None,
                          rssi=bits[1] if len(bits) > 1 else None, ts=ts, persist=False)
            elif kind == "TX-ACK":
                continue  # metadata only, already folded into acked_crcs
            elif kind.startswith("TX"):
                # Current format is "TX-SENT <crc8> <text>"; older lines are
                # "TX-SENT <text>" / "TX-ACKED <text>" and are still tolerated.
                acked = kind == "TX-ACKED"
                text = body
                m = re.match(r"([0-9a-f]{8}) (.*)", body, re.S)
                if m:
                    text = m.group(2)
                    acked = acked or m.group(1).lower() in acked_crcs
                self._add("out", text, acked=acked, ts=ts, persist=False)

    # -------------------------------------------------------------- history

    def _add(self, direction, text, mac=None, rssi=None, acked=False, ts=None,
             persist=True):
        with self.lock:
            msg = {"id": self.next_id, "dir": direction, "text": text, "mac": mac,
                   "rssi": rssi, "acked": acked,
                   "ts": ts or time.strftime("%Y-%m-%d %H:%M:%S"),
                   "attempts": 1 if direction == "out" else 0}
            self.next_id += 1
            self.history.append(msg)
            if len(self.history) > 500:
                del self.history[:-500]
        if persist:
            LOG.parent.mkdir(exist_ok=True)
            with LOG.open("a") as f:
                if direction == "in":
                    f.write(f"{msg['ts']} RX {mac} {rssi} {text}\n")
                else:
                    f.write(f"{msg['ts']} TX-SENT {crc32_hex(text)} {text}\n")
        return msg

    def _reader(self):
        while True:
            if not self.ser:
                time.sleep(1)
                continue
            try:
                line = self.ser.readline().decode("utf-8", "replace").strip()
            except Exception as e:
                print(f"serial read error: {e}")
                self.status["connected"] = False
                time.sleep(1)
                continue
            if not line:
                continue
            if line.startswith("RX "):
                p = line.split(" ", 3)
                if len(p) == 4:
                    msg = self._add("in", p[3], mac=p[1], rssi=p[2])
                    print(f"  < RX [{p[1]} {p[2]}dBm] {p[3]}")
                    # Native banner: this is the one that reaches you when the
                    # browser is closed or in the background.
                    mac_notify(f"Cardputer ({p[2]} dBm)", p[3])
            elif line.startswith("ACK "):
                crc = line.split(" ")[1].lower()
                with self.lock:
                    mid = self.pending.pop(crc, None)
                    if mid:
                        for m in self.history:
                            if m["id"] == mid:
                                m["acked"] = True
                                break
                if mid:
                    with LOG.open("a") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} TX-ACK {crc}\n")
                print(f"  < ACK {crc}", flush=True)
            elif line.startswith(("READY", "INFO")):
                self._parse_ready(line)

    def _retrier(self):
        """ESP-NOW has no retransmission of its own; unacked sends get retried."""
        while True:
            time.sleep(RETRY_WAIT)
            with self.lock:
                stale = [(crc, mid) for crc, mid in self.pending.items()]
            for crc, mid in stale:
                with self.lock:
                    msg = next((m for m in self.history if m["id"] == mid), None)
                if not msg or msg["acked"]:
                    continue
                if msg["attempts"] >= RETRY_ATTEMPTS:
                    with self.lock:
                        self.pending.pop(crc, None)
                    continue
                msg["attempts"] += 1
                self._write(msg["text"])

    def _write(self, text):
        if not self.ser:
            return False
        try:
            self.ser.write((text + "\n").encode())
            self.ser.flush()
            return True
        except Exception as e:
            print(f"serial write error: {e}")
            return False

    def send(self, text):
        text = text.strip()
        if not text:
            return {"ok": False, "error": "empty"}
        if not self.ser:
            return {"ok": False, "error": "bridge not connected"}
        msg = self._add("out", text)
        with self.lock:
            self.pending[crc32_hex(text)] = msg["id"]
        self._write(text)
        print(f"  -> {text!r} (crc {crc32_hex(text)})")
        return {"ok": True, "id": msg["id"]}

    def state(self, since=0):
        with self.lock:
            msgs = [m for m in self.history if m["id"] > since]
            return {"messages": msgs, "last": self.next_id - 1,
                    "status": dict(self.status)}


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESP-NOW · Cardputer</title>
<style>
:root{--bg:#faf9f7;--fg:#1a1a19;--muted:#6b6b68;--line:#e4e2dd;
      --out:#e8f0fe;--in:#eef7ed;--accent:#2f6f4f;--warn:#b3541e}
@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#ececeb;--muted:#9a9a97;
      --line:#2c2c31;--out:#1d2836;--in:#1b2a20;--accent:#7fc79c;--warn:#e0925f}}
:root[data-theme=dark]{--bg:#16161a;--fg:#ececeb;--muted:#9a9a97;--line:#2c2c31;
      --out:#1d2836;--in:#1b2a20;--accent:#7fc79c;--warn:#e0925f}
:root[data-theme=light]{--bg:#faf9f7;--fg:#1a1a19;--muted:#6b6b68;--line:#e4e2dd;
      --out:#e8f0fe;--in:#eef7ed;--accent:#2f6f4f;--warn:#b3541e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
  flex-wrap:wrap;gap:10px 18px;align-items:center;position:sticky;top:0;background:var(--bg)}
h1{font-size:16px;margin:0;font-weight:600}
.pill{font:12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
  border:1px solid var(--line);border-radius:99px;padding:5px 10px;white-space:nowrap}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}
.up{background:var(--accent)}.down{background:var(--warn)}
main{max-width:820px;margin:0 auto;padding:18px}
#list{display:flex;flex-direction:column;gap:8px}
.msg{border:1px solid var(--line);border-radius:10px;padding:9px 12px}
.msg.out{background:var(--out)}.msg.in{background:var(--in)}
.meta{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
  display:flex;gap:10px;flex-wrap:wrap;margin-bottom:3px}
.txt{white-space:pre-wrap;word-break:break-word}
.ok{color:var(--accent)}.bad{color:var(--warn)}
form{display:flex;gap:8px;margin:18px 0 8px;position:sticky;bottom:0;
  background:var(--bg);padding:10px 0;border-top:1px solid var(--line)}
input[type=text]{flex:1;min-width:0;padding:10px 12px;border:1px solid var(--line);
  border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
button{padding:10px 16px;border:1px solid var(--line);border-radius:8px;
  background:var(--fg);color:var(--bg);font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);font-weight:400}
.empty{color:var(--muted);padding:24px 0;text-align:center}
.note{color:var(--muted);font-size:12.5px;margin-top:6px}
</style></head><body>
<header>
  <h1>ESP-NOW · Cardputer</h1>
  <span class="pill" id="conn"><span class="dot down"></span>connecting…</span>
  <span class="pill" id="radio">—</span>
  <button class="ghost" id="notif" type="button">Enable browser alerts</button>
</header>
<main>
  <div id="list"><div class="empty">No messages yet.</div></div>
  <form id="f" autocomplete="off">
    <input type="text" id="t" placeholder="Message to Cardputer…" maxlength="230">
    <button type="submit">Send</button>
  </form>
  <div class="note">Sends retry until the Cardputer ACKs. A powered-off Cardputer
  misses messages permanently — ESP-NOW has no store-and-forward.</div>
</main>
<script>
let since=0, seeded=false;
const list=document.getElementById('list');

document.getElementById('notif').onclick=async()=>{
  const p=await Notification.requestPermission();
  document.getElementById('notif').textContent =
    p==='granted' ? 'Browser alerts on' : 'Alerts blocked';
};

function row(m){
  const d=document.createElement('div');
  d.className='msg '+(m.dir==='in'?'in':'out');
  const bits=[m.ts, m.dir==='in'?'from Cardputer':'to Cardputer'];
  if(m.rssi) bits.push(m.rssi+' dBm');
  if(m.dir==='out') bits.push(m.acked
      ? '<span class="ok">acked</span>'
      : '<span class="bad">unacked'+(m.attempts>1?' ×'+m.attempts:'')+'</span>');
  d.innerHTML='<div class="meta">'+bits.join('<span>·</span>')+'</div>'+
              '<div class="txt"></div>';
  d.querySelector('.txt').textContent=m.text;
  return d;
}

async function poll(){
  try{
    const r=await fetch('/api/state?since='+since);
    const s=await r.json();
    const c=document.getElementById('conn');
    c.innerHTML='<span class="dot '+(s.status.connected?'up':'down')+'"></span>'+
                (s.status.connected? s.status.port.replace('/dev/cu.','') : 'disconnected');
    document.getElementById('radio').textContent =
      s.status.mac ? s.status.mac+' · ch'+s.status.channel+' · '+s.status.band : '—';
    if(s.messages.length){
      if(!seeded){ list.innerHTML=''; seeded=true; }
      for(const m of s.messages){
        list.appendChild(row(m));
        // Only alert for genuinely new inbound traffic, not the seeded history.
        if(m.dir==='in' && since>0 && Notification.permission==='granted'){
          new Notification('Cardputer'+(m.rssi?' ('+m.rssi+' dBm)':''),{body:m.text});
        }
      }
      window.scrollTo(0,document.body.scrollHeight);
    }
    // Re-render acked state for outbound already on screen.
    since=s.last;
  }catch(e){}
  setTimeout(poll,1000);
}

document.getElementById('f').onsubmit=async e=>{
  e.preventDefault();
  const i=document.getElementById('t'); const v=i.value.trim();
  if(!v) return; i.value='';
  await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:v})});
};
poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    bridge = None

    def log_message(self, *a):
        pass  # keep the console for protocol traffic

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            since = 0
            m = re.search(r"since=(\d+)", self.path)
            if m:
                since = int(m.group(1))
            return self._json(self.bridge.state(since))
        if self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.send_error(404)

    def do_POST(self):
        if self.path != "/api/send":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "error": "bad json"}, 400)
        return self._json(self.bridge.send(str(data.get("text", ""))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None, help="serial port override")
    ap.add_argument("--http-port", type=int, default=8765)
    args = ap.parse_args()

    bridge = Bridge(args.port)
    if not bridge.ser:
        print("WARNING: no ESP-NOW bridge found — the UI will load and show "
              "history, but cannot send. Is the C5 plugged in? Is another "
              "process (notify_bridge.py) holding the port?")
    Handler.bridge = bridge

    srv = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    print(f"\n  http://127.0.0.1:{args.http_port}\n")
    print("  Bound to localhost only. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
