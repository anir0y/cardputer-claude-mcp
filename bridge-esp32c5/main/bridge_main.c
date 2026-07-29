// ESP-NOW bridge: USB serial <-> ESP-NOW broadcast.
//
// Runs on the Waveshare ESP32-C5. The Mac cannot speak ESP-NOW, so this board is
// its radio: lines arriving on USB serial are broadcast, and anything heard on
// the air is printed back.
//
// Wire format is dictated by M5Cardputer-UserDemo's app_chat, which does:
//     espnow_send(ESPNOW_DATA_TYPE_DATA, ESPNOW_ADDR_BROADCAST,
//                 data.c_str(), data.size(), &frame_head, portMAX_DELAY);
// i.e. broadcast, default frame config, payload is the raw UTF-8 string with no
// header. Matching that exactly is what makes interop work — same esp-now
// component version, same data type, same broadcast address.
//
// The C5 is dual-band; it MUST be pinned to 2.4 GHz or it will never reach the
// Cardputer's ESP32-S3, which is 2.4 GHz only.

#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "driver/usb_serial_jtag.h"

#include "espnow.h"
#include "espnow_storage.h"
#include "espnow_utils.h"

static const char* TAG = "bridge";

// Must match the Cardputer patch. Control traffic, not chat text.
#define ACK_PREFIX "\x01" "ACK:"

// ESP-NOW peers must share a channel. The Cardputer follows its AP's channel
// while associated, so this board joins the same AP and inherits that channel
// instead of pinning one — which also survives the AP moving channels.
//
// Credentials live in NVS, not here: a hardcoded PSK would travel with every
// copy of this source and end up in the built binary. Provision once with
// ./provision_wifi.py — the values survive app reflashes.
#define WIFI_NVS_NAMESPACE "wifi"
#define WIFI_NVS_KEY_SSID  "ssid"
#define WIFI_NVS_KEY_PASS  "pass"
#define WIFI_SSID_MAX 33   // 32 per 802.11 + NUL
#define WIFI_PASS_MAX 65   // 64 per WPA2 + NUL
#define WIFI_CONNECT_TIMEOUT_MS 20000

// Used only if the AP is unreachable, so the bridge still talks to a Cardputer
// that booted with ESP-NOW owning the radio (that path sits on channel 1).
#define ESPNOW_FALLBACK_CHANNEL 1

// Not called LINE_MAX: sys/syslimits.h already defines that.
#define BRIDGE_LINE_MAX 240

static uint32_t crc32_of(const char* data, size_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint8_t)data[i];
        for (int b = 0; b < 8; b++) crc = (crc >> 1) ^ (0xEDB88320u & (-(int32_t)(crc & 1)));
    }
    return ~crc;
}

// Protocol lines to the host are prefixed so the daemon can ignore ESP-IDF log
// output on the same serial link.
static void emit(const char* fmt, ...)
{
    char buf[BRIDGE_LINE_MAX + 64];
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    if (n > 0) {
        usb_serial_jtag_write_bytes(buf, n, pdMS_TO_TICKS(100));
        usb_serial_jtag_write_bytes("\n", 1, pdMS_TO_TICKS(100));
    }
}

static esp_err_t on_espnow_recv(uint8_t* src_addr, void* data, size_t size, wifi_pkt_rx_ctrl_t* rx_ctrl)
{
    if (!src_addr || !data || !size || !rx_ctrl) return ESP_FAIL;

    char text[BRIDGE_LINE_MAX + 1];
    size_t n = size > BRIDGE_LINE_MAX ? BRIDGE_LINE_MAX : size;
    memcpy(text, data, n);
    text[n] = '\0';

    if (strncmp(text, ACK_PREFIX, strlen(ACK_PREFIX)) == 0) {
        // Delivery receipt from the Cardputer, keyed by payload CRC.
        emit("ACK %s " MACSTR " %d", text + strlen(ACK_PREFIX), MAC2STR(src_addr), rx_ctrl->rssi);
    } else {
        emit("RX " MACSTR " %d %s", MAC2STR(src_addr), rx_ctrl->rssi, text);
    }
    return ESP_OK;
}

// Reads the AP credentials provisioned into NVS. Returns false when the board
// has never been provisioned, which is a normal first-boot state rather than an
// error — the caller falls back to a fixed channel and says so on the wire.
static bool wifi_creds_load(char* ssid, size_t ssid_size, char* pass, size_t pass_size)
{
    nvs_handle_t h;
    if (nvs_open(WIFI_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return false;

    size_t n = ssid_size;
    esp_err_t ret = nvs_get_str(h, WIFI_NVS_KEY_SSID, ssid, &n);
    if (ret == ESP_OK) {
        n = pass_size;
        ret = nvs_get_str(h, WIFI_NVS_KEY_PASS, pass, &n);
        // An open network is legitimate: treat a missing key as an empty PSK.
        if (ret == ESP_ERR_NVS_NOT_FOUND) {
            pass[0] = '\0';
            ret = ESP_OK;
        }
    }
    nvs_close(h);
    return ret == ESP_OK && ssid[0] != '\0';
}

static EventGroupHandle_t s_wifi_events;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAILED_BIT    BIT1

// Set only when NVS held credentials; without it the handlers must not call
// esp_wifi_connect(), which would retry forever against an empty SSID.
static bool s_wifi_provisioned;

// Reconnect forever once associated: a bridge sitting on a stale channel after a
// brief AP blip looks exactly like a dead radio from the host's side.
static void on_wifi_event(void* arg, esp_event_base_t base, int32_t id, void* data)
{
    if (!s_wifi_provisioned) return;

    // Deliberately no STA_START -> connect(): that fires inside esp_wifi_start(),
    // before the 2.4 GHz band pin is applied, so the first association attempt
    // would scan with auto band selection. The caller connects explicitly once
    // the radio is fully configured.
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

// Returns the channel the radio ended up on, and whether we are associated.
// A NULL ssid means "unprovisioned": bring the radio up but do not associate.
static uint8_t wifi_start_2g4(bool* associated, const char* ssid, const char* pass)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    s_wifi_provisioned = (ssid != NULL);
    s_wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &on_wifi_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &on_wifi_event, NULL, NULL));

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    if (ssid) {
        wifi_config_t sta = { 0 };
        strlcpy((char*)sta.sta.ssid, ssid, sizeof(sta.sta.ssid));
        strlcpy((char*)sta.sta.password, pass ? pass : "", sizeof(sta.sta.password));
        // All-channel scan so a hidden or distant AP is still found; the default
        // fast scan stops at the first match and can miss it entirely.
        sta.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
        sta.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    }

    ESP_ERROR_CHECK(esp_wifi_start());

    // Pin to 2.4 GHz — the C5 defaults to auto band selection, and the
    // Cardputer's S3 has no 5 GHz radio at all. Also stops the C5 joining a
    // 5 GHz AP that shares this SSID, which would strand it off-channel.
    esp_err_t ret = esp_wifi_set_band(WIFI_BAND_2G);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "esp_wifi_set_band failed: %s", esp_err_to_name(ret));
    }

    // Power save must be off or ESP-NOW receives are missed while idle.
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    EventBits_t bits = 0;
    if (s_wifi_provisioned) {
        // Radio is fully configured now, so the first attempt is 2.4 GHz only.
        esp_wifi_connect();
        bits = xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT | WIFI_FAILED_BIT,
                                   pdFALSE, pdFALSE,
                                   pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));
    }

    uint8_t primary = ESPNOW_FALLBACK_CHANNEL;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;

    if (bits & WIFI_CONNECTED_BIT) {
        *associated = true;
        ESP_ERROR_CHECK(esp_wifi_get_channel(&primary, &second));
    } else {
        // No AP: hold the channel the Cardputer uses when ESP-NOW owns its radio.
        *associated = false;
        esp_wifi_disconnect();
        esp_wifi_set_channel(ESPNOW_FALLBACK_CHANNEL, WIFI_SECOND_CHAN_NONE);
        primary = ESPNOW_FALLBACK_CHANNEL;
    }
    return primary;
}

static void broadcast(const char* text, size_t len)
{
    espnow_frame_head_t frame_head = ESPNOW_FRAME_CONFIG_DEFAULT();
    esp_err_t ret = espnow_send(ESPNOW_DATA_TYPE_DATA, ESPNOW_ADDR_BROADCAST, text, len, &frame_head,
                                pdMS_TO_TICKS(1000));
    if (ret == ESP_OK) {
        emit("SENT %08" PRIx32 " %u", crc32_of(text, len), (unsigned)len);
    } else {
        emit("ERR send %s", esp_err_to_name(ret));
    }
}

void app_main(void)
{
    // Keep the serial link mostly clean for protocol lines; the host tolerates
    // stray log lines but there is no reason to generate them.
    esp_log_level_set("*", ESP_LOG_WARN);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    usb_serial_jtag_driver_config_t usb_cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb_cfg));

    char ssid[WIFI_SSID_MAX] = {0};
    char pass[WIFI_PASS_MAX] = {0};
    bool provisioned = wifi_creds_load(ssid, sizeof(ssid), pass, sizeof(pass));

    // Announce the port before joining WiFi. Association takes several seconds
    // and the host's port probe gives up long before READY would arrive; this
    // line is what tells it "this is the bridge". Host treats INFO as a latch
    // and fills in mac/channel from the READY line that follows.
    vTaskDelay(pdMS_TO_TICKS(300));  // let USB-JTAG re-attach after reset
    if (provisioned) {
        emit("INFO bridge booting, joining WiFi \"%s\"; READY follows", ssid);
    } else {
        emit("INFO no WiFi credentials in NVS; run provision_wifi.py. "
             "Falling back to channel %d", ESPNOW_FALLBACK_CHANNEL);
    }

    espnow_storage_init();
    bool associated = false;
    uint8_t channel = wifi_start_2g4(&associated, provisioned ? ssid : NULL, pass);

    espnow_config_t espnow_config = ESPNOW_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(espnow_init(&espnow_config));
    ESP_ERROR_CHECK(espnow_set_config_for_data_type(ESPNOW_DATA_TYPE_DATA, true, on_espnow_recv));

    uint8_t mac[6] = {0};
    esp_wifi_get_mac(WIFI_IF_STA, mac);
    wifi_band_t band = 0;
    esp_wifi_get_band(&band);

    // Host parses mac=/channel=/band= from this line; extra fields are ignored.
    emit("READY mac=" MACSTR " channel=%u band=%s wifi=%s", MAC2STR(mac), (unsigned)channel,
         band == WIFI_BAND_2G ? "2.4GHz" : "other",
         associated ? ssid : (provisioned ? "join-failed(fallback-ch1)"
                                          : "unprovisioned(fallback-ch1)"));
    emit("INFO send a line to broadcast it; RX/ACK lines are inbound");

    // Line assembly from USB serial. Anything up to \n is one message.
    char line[BRIDGE_LINE_MAX + 1];
    size_t len = 0;
    uint8_t chunk[64];

    while (1) {
        int n = usb_serial_jtag_read_bytes(chunk, sizeof(chunk), pdMS_TO_TICKS(100));
        for (int i = 0; i < n; i++) {
            char c = (char)chunk[i];
            if (c == '\r') continue;
            if (c == '\n') {
                if (len > 0) {
                    line[len] = '\0';
                    broadcast(line, len);
                    len = 0;
                }
            } else if (len < BRIDGE_LINE_MAX) {
                line[len++] = c;
            } else {
                // Over-long line: send what we have rather than silently truncate
                // into the next message.
                line[len] = '\0';
                broadcast(line, len);
                len = 0;
            }
        }
    }
}
