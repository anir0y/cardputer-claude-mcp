#!/usr/bin/env python3
"""Write the bridge's WiFi credentials into the ESP32-C5's NVS partition.

The credentials are deliberately not in bridge_main.c: a hardcoded PSK would
travel with every copy of that source and end up in the built binary. They live
in NVS instead, which survives `idf.py flash` (that only rewrites the app
partition), so this is normally a one-time step per board.

    ./provision_wifi.py --ssid YourSSID            # prompts for the password
    ./provision_wifi.py --ssid YourSSID --port /dev/cu.usbmodem14201

The password is read with getpass by default so it stays out of your shell
history and the process list. The intermediate NVS image is written to a
0700 temp dir and shredded afterwards.

Two things to know:

- This rewrites the whole `nvs` partition, which also clears esp-now's own
  stored state. That is harmless — espnow_storage_init() recreates it on boot.
- Only one process may hold the serial port. Stop the web app first:
      launchctl bootout gui/$(id -u)/local.espnow.webapp
  and start it again afterwards with `launchctl bootstrap`.
"""

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Offsets come from the built partition table (gen_esp32part.py on
# build/partition_table/partition-table.bin), not guesswork:
#   nvs,data,nvs,0x9000,24K
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000
CHIP = "esp32c5"

IDF_PATH = Path(os.environ.get("IDF_PATH", Path.home() / "esp" / "esp-idf"))
NVS_GEN = IDF_PATH / "components" / "nvs_flash" / "nvs_partition_generator" / "nvs_partition_gen.py"

# Must match bridge_main.c's WIFI_NVS_* defines.
NAMESPACE = "wifi"
KEY_SSID = "ssid"
KEY_PASS = "pass"


def detect_port() -> str:
    """Pick the single Espressif USB-serial port, or make the user choose."""
    candidates = sorted(str(p) for p in Path("/dev").glob("cu.usbmodem*"))
    if not candidates:
        sys.exit("no /dev/cu.usbmodem* found - is the board plugged in?")
    if len(candidates) > 1:
        sys.exit(
            "multiple serial ports found; pass --port explicitly:\n  "
            + "\n  ".join(candidates)
            + "\n(the bridge is the one whose boot banner starts with READY/INFO)"
        )
    return candidates[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssid", required=True, help="2.4 GHz SSID to join")
    ap.add_argument("--password", default=None,
                    help="PSK; omit to be prompted (keeps it out of shell history)")
    ap.add_argument("--port", default=None, help="serial port (default: autodetect)")
    ap.add_argument("--open", action="store_true", help="open network, no password")
    args = ap.parse_args()

    if args.open:
        password = ""
    elif args.password is not None:
        password = args.password
    else:
        password = getpass.getpass(f"PSK for {args.ssid!r}: ")

    if len(args.ssid.encode()) > 32:
        sys.exit("SSID longer than the 32 bytes 802.11 allows")
    if password and not 8 <= len(password.encode()) <= 63:
        sys.exit("WPA2 PSK must be 8-63 bytes (use --open for an open network)")
    if not NVS_GEN.is_file():
        sys.exit(f"nvs_partition_gen.py not found at {NVS_GEN}\nset IDF_PATH and retry")

    port = args.port or detect_port()

    # 0700 dir: the CSV holds the PSK in plaintext for the moment it exists.
    workdir = Path(tempfile.mkdtemp(prefix="nvs-provision-"))
    try:
        csv = workdir / "wifi.csv"
        # nvs_partition_gen's CSV format: a namespace row, then key rows.
        csv.write_text(
            "key,type,encoding,value\n"
            f"{NAMESPACE},namespace,,\n"
            f"{KEY_SSID},data,string,{args.ssid}\n"
            f"{KEY_PASS},data,string,{password}\n"
        )
        csv.chmod(0o600)

        image = workdir / "nvs.bin"
        subprocess.run(
            [sys.executable, str(NVS_GEN), "generate", str(csv), str(image), hex(NVS_SIZE)],
            check=True, capture_output=True, text=True,
        )

        print(f"flashing NVS to {port} at {hex(NVS_OFFSET)} ...")
        subprocess.run(
            [sys.executable, "-m", "esptool", "--chip", CHIP, "-p", port,
             "write_flash", hex(NVS_OFFSET), str(image)],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        # esptool's own stderr is the useful part; don't bury it behind a traceback.
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        sys.exit(f"provisioning failed: {' '.join(str(a) for a in e.cmd)}")
    finally:
        # Overwrite before unlinking so the PSK does not linger in free blocks.
        for f in workdir.glob("*"):
            try:
                size = f.stat().st_size
                with f.open("r+b") as fh:
                    fh.write(b"\0" * size)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\ndone - {args.ssid!r} written to NVS.")
    print("The board reboots and joins on its own; its READY line reports the")
    print("channel it landed on. Restart the web app to pick it up:")
    print("  launchctl bootstrap gui/$(id -u) "
          "~/Library/LaunchAgents/local.espnow.webapp.plist")


if __name__ == "__main__":
    main()
