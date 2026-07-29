#!/usr/bin/env python3
"""Re-apply the ESP32-C5 portability fixes to the esp-now managed component.

esp-now 2.5.3 does not compile for ESP32-C5 (preview silicon in IDF v5.5). Three
files need touching. These live under managed_components/, which `idf.py
fullclean` and any re-resolution will wipe — so run this after either.

    python3 apply_c5_patches.py

Idempotent: safe to run repeatedly. None of these changes affect the ESP-NOW wire
format, so interoperability with the Cardputer is unaffected.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent / "managed_components" / "espressif__esp-now"

PATCHES = [
    # (file, needle-to-detect-already-applied, old, new, why)
    (
        "src/utils/src/espnow_reboot.c",
        "esp_rom_get_reset_reason(cpu)",
        '#elif CONFIG_IDF_TARGET_ESP32C6\n#include "esp32c6/rom/rtc.h"\n#endif',
        '#elif CONFIG_IDF_TARGET_ESP32C6\n#include "esp32c6/rom/rtc.h"\n'
        '#else\n'
        '// C5 and other newer targets no longer ship <target>/rom/rtc.h, leaving\n'
        '// RESET_REASON and rtc_get_reset_reason() undeclared. Map onto the current API.\n'
        '#include "esp_rom_sys.h"\n'
        '#include "soc/reset_reasons.h"\n'
        '#define RESET_REASON              soc_reset_reason_t\n'
        '#define rtc_get_reset_reason(cpu) esp_rom_get_reset_reason(cpu)\n'
        '#define DEEPSLEEP_RESET           RESET_REASON_CORE_DEEP_SLEEP\n'
        '#define RTCWDT_BROWN_OUT_RESET    RESET_REASON_SYS_BROWN_OUT\n'
        '#endif',
        "legacy rom/rtc.h absent on C5",
    ),
    (
        "src/debug/src/commands/cmd_system.c",
        "esp_rom_spiflash.h",
        '#include "esp_log.h"',
        '// g_rom_flashchip is declared here; not pulled in transitively on newer targets\n'
        '#include "esp_rom_spiflash.h"\n'
        '#include "esp_log.h"',
        "g_rom_flashchip undeclared",
    ),
    (
        "src/espnow/src/espnow.c",
        "channel: %d, size",
        'rx_ctrl->secondary_channel, espnow_data->size, espnow_data->payload,',
        'espnow_data->size, espnow_data->payload,',
        "esp_wifi_rxctrl_t has no secondary_channel on C5",
    ),
    (
        "src/espnow/src/espnow.c",
        "channel: %d, size",
        'rssi: %d, channel: %d/%d, size: %d, %s, magic: 0x%x, ack: %d",',
        'rssi: %d, channel: %d, size: %d, %s, magic: 0x%x, ack: %d",',
        "matching format string for the dropped field",
    ),
]


def main():
    if not ROOT.exists():
        sys.exit(f"esp-now component not found at {ROOT}\n"
                 "Run `idf.py reconfigure` first so the component manager fetches it.")

    applied = skipped = 0
    for rel, marker, old, new, why in PATCHES:
        path = ROOT / rel
        if not path.exists():
            print(f"  MISSING {rel} — component layout changed?")
            continue
        text = path.read_text()
        if marker in text:
            print(f"  ok      {rel}: already patched ({why})")
            skipped += 1
            continue
        if old not in text:
            print(f"  WARN    {rel}: anchor not found, patch needs review ({why})")
            continue
        path.write_text(text.replace(old, new, 1))
        print(f"  patched {rel}: {why}")
        applied += 1

    print(f"\n{applied} applied, {skipped} already present")
    if applied:
        print("Now run: idf.py build")


if __name__ == "__main__":
    main()
