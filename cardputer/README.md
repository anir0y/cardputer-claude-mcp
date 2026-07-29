# Cardputer firmware — ESP-NOW notification receiver

This is the code we wrote for the **M5Cardputer ADV** so it beeps and queues
incoming ESP-NOW notifications from *any* app, not just the Chat app.

It is a patch on top of M5's own firmware, not a standalone project:

- Upstream: <https://github.com/m5stack/M5Cardputer-UserDemo.git>
- Base commit the patch was cut against: **`b549eac`**
  (*"Merge pull request #40 from wdoekes/feat/CardputerADV-improve-boot-ux"*)

## What upstream did wrong

`espNowInit()` was only called from `AppChat::onOpen()`, and incoming messages
were stored in a **single `std::string`** that was only read by
`AppChat::onRunning()`. So on a fresh boot nothing was listening, and once Chat
was closed every message was received and silently overwritten. No alert of any
kind fired. Stock firmware is unusable as a notifier.

`AppChat::onClose()` does *not* deinit ESP-NOW, so the radio does keep running
once Chat has been opened — the messages arrive and are then thrown away.
(Confirmed by reading the source.)

## What the patch changes

| Change | Location |
|---|---|
| ESP-NOW init moved to boot | `Hal::init()` |
| Speaker volume set to 220/255 (default was inaudible) | `Hal::init()` |
| Bounded 32-message queue, **oldest** dropped when full | `hal.cpp` receive handler |
| Three rising chirps from any foreground app | `Hal::update()` → `espNowNotifyUpdate()` |
| ACK reply keyed by payload CRC32 | receive handler + `espNowNotifyUpdate()` |
| ACK traffic filtered out of the chat queue (`\x01ACK:` prefix) | receive handler |
| `esp-now` pinned to `==2.5.3` to match the bridge | `main/idf_component.yml` |

**The key insight is `Hal::update()`.** `main.cpp:48` calls it every loop
iteration regardless of which app is foreground, so a cross-app alert needed no
launcher or UI surgery. The speaker is driven from there and deliberately **not**
from the ESP-NOW receive callback — that callback runs on the espnow task, where
touching M5 objects (speaker/display) is not safe. The callback only sets an
atomic flag; `Hal::update()` does the alerting and flushes the queued ACK.

New public API added to `Hal` (see `patched-sources/main/hal/hal.h`):

```cpp
size_t   espNowPendingCount();
uint32_t espNowTotalReceived();
void     espNowSetAckEnabled(bool enabled);
void     espNowNotifyUpdate();   // called from Hal::update()
```

## Trade-off you must know about

`espNowInit()` disconnects any station connection, so **SetWiFi and NTP time
sync will not work while notifications are active.** Call `espNowDeinit()` to
hand the radio back. When the Cardputer *is* associated with an AP, ESP-NOW
still works — it just follows the AP's channel, which is why the bridge joins
the same AP instead of pinning a channel. See `../HARDWARE_NOTES.md`.

## Files here

- `espnow-notify.patch` — the exact diff, applyable to a clean upstream checkout
- `patched-sources/main/hal/hal.cpp`, `hal.h` — the patched files in full
- `patched-sources/main/main.cpp` — unmodified upstream, included because line 48
  (`hal->update()`) is what makes the cross-app alert work
- `patched-sources/main/idf_component.yml` — the pinned dependency

## Build and flash

```bash
git clone https://github.com/m5stack/M5Cardputer-UserDemo.git
cd M5Cardputer-UserDemo
git checkout b549eac
python3 fetch_repos.py

git apply "path/to/cardputer/espnow-notify.patch"
# or just copy patched-sources/main/ over main/

idf.py set-target esp32s3
idf.py build
idf.py -p /dev/cu.usbmodem<N> flash monitor
```
