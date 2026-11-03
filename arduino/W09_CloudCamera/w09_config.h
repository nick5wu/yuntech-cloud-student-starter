// 本組設定。秘密（Wi-Fi 密碼、權杖、私鑰）不寫在這裡、不進 Git，一律放板上 NVS。
#pragma once

// ---- W7 感測與判斷（沿用 W7 接線）----
#define PIN_DHT   21   // 擴充板 D4  → GPIO21
#define PIN_KNOB   1   // 擴充板 A0  → GPIO1（ADC1，開 Wi-Fi 後仍可讀）
#define PIN_LED    2   // 擴充板 D12 → GPIO2
static const int LED_ON = HIGH;       // 本課擴充板高電位有效；相反就改 LOW

// 旋鈕端點：2026-11-03 在本組板子實測（轉到最左／最右，各連續 5 筆穩定）。每塊板不同。
static const int RAW_MIN = 0;
static const int RAW_MAX = 4095;
static const int TH_MIN = 30, TH_MAX = 90;

// TODO(你們組)：組別與裝置名稱。device_id 限 [A-Za-z0-9_-] 1~32 字。
#define GROUP_ID   "g06"
#define DEVICE_ID  "g06-esp32-s3-cam"

// ---- 取樣節奏 ----
static const uint32_t SENSOR_PERIOD_MS = 2000;   // DHT11 每 2 秒讀一次

// ---- 照片 ----
static const size_t MAX_JPEG_BYTES = 900 * 1024;  // 檢查工具上限 1MB，這裡留餘裕
// W8 T3 以 VGA 為主；IoT 上傳用同一張當次原圖。

// ---- NVS（Preferences）鍵名 ----
// TODO(教師配置工具)：教師的配置工具若使用不同命名空間／鍵名，在這裡對齊。
#define NVS_NS            "w09"
#define NVS_WIFI_SSID     "wifi_ssid"
#define NVS_WIFI_PASS     "wifi_pass"
#define NVS_API_BASE      "api_base"      // 例：https://<本組入口>（不含路徑）
#define NVS_IOT_HOST      "iot_host"      // IoT Core 資料端點主機名
#define NVS_IOT_TOPIC     "iot_topic"     // 本組事件主題
#define NVS_IOT_CLIENT    "iot_client"    // 本裝置 client ID（須在 IoT 政策內）
#define NVS_TOKEN_DEVICE  "tok_device"    // device 權杖（只收據與上傳，不是 operator）
#define NVS_CA_API        "ca_api"        // API 信任 CA（PEM，公開憑證）
#define NVS_CA_IOT        "ca_iot"        // AmazonRootCA1（PEM，公開憑證）
#define NVS_DEV_CERT      "dev_cert"      // 裝置憑證（PEM）
#define NVS_DEV_KEY       "dev_key"       // 裝置私鑰（PEM，只存在板上）
