idf_component_register(SRCS "bridge_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_driver_usb_serial_jtag
                                esp_wifi
                                esp_netif
                                esp_event
                                nvs_flash)
