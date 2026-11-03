// W9 小組作品：警報事件與照片上雲
// 板型：ESP32-S3（16MB Flash／OPI PSRAM／native USB）；Arduino-ESP32 Core 3.3.7
//
// 主線：NORMAL→ALARM 固定一次事件(含當次原圖) → MQTT over TLS(QoS1) 等 PUBACK
//       → HTTPS 查本裝置收據確認已入庫 → 同 event_id 上傳原 JPEG → 收據確認照片 ready
// 事件與照片分開顯示進度；重試沿用原 ID／原時間／原圖；單筆工作滿載時拒收新觸發。
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <Preferences.h>
#include <DHT.h>
#include <ArduinoJson.h>
#include "esp_camera.h"
#include "mqtt_client.h"
#include "mbedtls/sha256.h"
#include "mbedtls/base64.h"
#include "esp_random.h"
#include "w09_config.h"
#include "camera_board.h"
#include "w09_selftest.h"

using namespace w09;

// ===================== 設定（來自 NVS，不放程式碼）=====================
struct Cfg {
  String ssid, pass, apiBase, iotHost, topic, clientId, token, caApi, caIot, devCert, devKey;
} cfg;
static String cfgMissing;        // 空字串＝設定齊全
static bool cfgReady = false;

static void loadConfig() {
  Preferences p;
  const bool opened = p.begin(NVS_NS, true);
  auto get = [&](const char* k) { return opened ? p.getString(k, "") : String(""); };
  cfg.ssid = get(NVS_WIFI_SSID);    cfg.pass = get(NVS_WIFI_PASS);
  cfg.apiBase = get(NVS_API_BASE);  cfg.iotHost = get(NVS_IOT_HOST);
  cfg.topic = get(NVS_IOT_TOPIC);   cfg.clientId = get(NVS_IOT_CLIENT);
  cfg.token = get(NVS_TOKEN_DEVICE);
  cfg.caApi = get(NVS_CA_API);      cfg.caIot = get(NVS_CA_IOT);
  cfg.devCert = get(NVS_DEV_CERT);  cfg.devKey = get(NVS_DEV_KEY);
  if (opened) p.end();
  cfgMissing = "";
  auto need = [&](const String& v, const char* name) { if (!v.length()) { cfgMissing += name; cfgMissing += ' '; } };
  need(cfg.ssid, NVS_WIFI_SSID);   need(cfg.apiBase, NVS_API_BASE); need(cfg.iotHost, NVS_IOT_HOST);
  need(cfg.topic, NVS_IOT_TOPIC);  need(cfg.clientId, NVS_IOT_CLIENT); need(cfg.token, NVS_TOKEN_DEVICE);
  need(cfg.caApi, NVS_CA_API);     need(cfg.caIot, NVS_CA_IOT);
  need(cfg.devCert, NVS_DEV_CERT); need(cfg.devKey, NVS_DEV_KEY);
  cfgReady = cfgMissing.length() == 0;
  // API 入口只接受 HTTPS，不接受 HTTP
  if (cfgReady && !cfg.apiBase.startsWith("https://")) { cfgMissing = "api_base(須為https://) "; cfgReady = false; }
  while (cfg.apiBase.endsWith("/")) cfg.apiBase.remove(cfg.apiBase.length() - 1);
}

// ===================== 全域狀態 =====================
static DHT dht(PIN_DHT, DHT11);
static WebServer server(80);
static SemaphoreHandle_t jobMu;

static char bootId[8];
static uint32_t eventSeq = 0;

// 感測
static float lastH = NAN, lastT = NAN;
static bool sensorOk = false;
static State state = State::SENSOR_ERROR;
static TriggerDetector det;
static bool testMode = false;
static float testH = 0;
static int curTh = TH_MIN;

// 計數與最近一次拒收原因
static uint32_t rejBusy = 0, rejTime = 0, dropped = 0;
static char lastReject[40] = "";

// 網路
static bool timeSynced = false;
static bool webUp = false;
static volatile bool mqttUp = false;
static volatile int pubackId = -1;
static esp_mqtt_client_handle_t mqtt = nullptr;
static char mqttErr[40] = "";

// 工作（同時只保留一筆）
struct Job {
  bool active = false;
  char eventId[66] = "";
  char observedAt[24] = "";
  char type[8] = "";
  char note[100] = "";
  char payload[400] = "";
  EventStage ev = EventStage::NONE;
  PhotoStage ph = PhotoStage::NONE;
  char evReason[56] = "";
  char phReason[56] = "";
  uint8_t* jpeg = nullptr;
  size_t jpegLen = 0;
  char jpegSha[65] = "";
  int attempts = 0;      // 當前這一段的失敗次數
  int polls = 0;
  uint32_t nextMs = 0;   // 這個時間以前不動作（退避／輪詢間隔）
  int pubMsgId = -1;
  uint32_t pubMs = 0;
  bool published = false;
  int lastHttp = 0;
} job;

struct Lock {
  Lock() { xSemaphoreTake(jobMu, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(jobMu); }
};

static void logf(const char* fmt, ...) {
  char buf[200];
  va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof buf, fmt, ap); va_end(ap);
  Serial.println(buf);
}
static void logLine(const char* s) { Serial.println(s); }

// ===================== 小工具 =====================
static void sha256Hex(const uint8_t* data, size_t len, char out[65]) {
  uint8_t d[32];
  mbedtls_sha256_context c; mbedtls_sha256_init(&c);
  mbedtls_sha256_starts(&c, 0); mbedtls_sha256_update(&c, data, len); mbedtls_sha256_finish(&c, d);
  mbedtls_sha256_free(&c);
  for (int i = 0; i < 32; i++) sprintf(out + i * 2, "%02x", d[i]);
  out[64] = 0;
}

static void setEv(EventStage s, const char* why = "") {
  Lock l; job.ev = s; strlcpy(job.evReason, why, sizeof job.evReason);
  logf("[cloud] event=%s %s", eventStageName(s), why);
}
static void setPh(PhotoStage s, const char* why = "") {
  Lock l; job.ph = s; strlcpy(job.phReason, why, sizeof job.phReason);
  logf("[cloud] photo=%s %s", photoStageName(s), why);
}

// ===================== W7：輸入 =====================
static int knobRaw = 0;   // 夾限前的原始值，量端點用（狀態 JSON 的 knob_raw）
static int readThreshold() {
  long sum = 0;
  for (int i = 0; i < 8; i++) sum += analogRead(PIN_KNOB);
  knobRaw = (int)(sum / 8);
  int raw = constrain(knobRaw, RAW_MIN, RAW_MAX);
  return map(raw, RAW_MIN, RAW_MAX, TH_MIN, TH_MAX);
}
static void readSensor() {
  float h = dht.readHumidity(), t = dht.readTemperature();
  sensorOk = !isnan(h) && !isnan(t);          // 兩個都有效才算讀到；失敗不沿用舊值
  lastH = sensorOk ? h : NAN;
  lastT = sensorOk ? t : NAN;
}
static void updateLed(uint32_t now) {
  bool on = false;
  if (state == State::ALARM) on = true;
  else if (state == State::SENSOR_ERROR) on = ((now / 500) % 2) == 0;   // 慢閃
  digitalWrite(PIN_LED, on ? LED_ON : !LED_ON);
}

// ===================== W8：相機 =====================
static bool cameraOk = false;
static String cameraErr;

static bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0; c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = CAM_PIN_D0; c.pin_d1 = CAM_PIN_D1; c.pin_d2 = CAM_PIN_D2; c.pin_d3 = CAM_PIN_D3;
  c.pin_d4 = CAM_PIN_D4; c.pin_d5 = CAM_PIN_D5; c.pin_d6 = CAM_PIN_D6; c.pin_d7 = CAM_PIN_D7;
  c.pin_xclk = CAM_PIN_XCLK; c.pin_pclk = CAM_PIN_PCLK; c.pin_vsync = CAM_PIN_VSYNC; c.pin_href = CAM_PIN_HREF;
  c.pin_sccb_sda = CAM_PIN_SIOD; c.pin_sccb_scl = CAM_PIN_SIOC;
  c.pin_pwdn = CAM_PIN_PWDN; c.pin_reset = CAM_PIN_RESET;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAMESIZE_VGA;           // 640x480
  c.jpeg_quality = 12;
  c.fb_count = 2;
  c.fb_location = CAMERA_FB_IN_PSRAM;
  c.grab_mode = CAMERA_GRAB_LATEST;       // 取最新一格，不吃舊畫面
  const esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) { cameraErr = String("esp_camera_init 0x") + String((unsigned)err, HEX); return false; }
  return true;
}

// 取當次原圖並複製到自己的緩衝；驅動緩衝一律歸還。
static bool captureJpeg(uint8_t** out, size_t* len, char* why, size_t whyCap) {
  *out = nullptr; *len = 0;
  if (!cameraOk) { strlcpy(why, "camera_not_ready", whyCap); return false; }
  camera_fb_t* fb = esp_camera_fb_get();        // 丟掉可能殘留的舊格
  if (fb) esp_camera_fb_return(fb);
  fb = esp_camera_fb_get();
  if (!fb || fb->len == 0) { if (fb) esp_camera_fb_return(fb); strlcpy(why, "fb_get_failed", whyCap); return false; }
  if (fb->len > MAX_JPEG_BYTES) { esp_camera_fb_return(fb); strlcpy(why, "jpeg_too_large", whyCap); return false; }
  uint8_t* buf = (uint8_t*)ps_malloc(fb->len);
  if (!buf) buf = (uint8_t*)malloc(fb->len);
  if (!buf) { esp_camera_fb_return(fb); strlcpy(why, "no_memory", whyCap); return false; }
  memcpy(buf, fb->buf, fb->len);
  *len = fb->len; *out = buf;
  esp_camera_fb_return(fb);
  return true;
}

// ===================== 觸發：固定一次事件 =====================
static void setReject(const char* r) { strlcpy(lastReject, r, sizeof lastReject); logf("[trigger] 拒收: %s", r); }

static void onTrigger(float h, float t, int th, bool isTest) {
  time_t now = time(nullptr);
  if (!timeLooksSynced((long long)now)) { rejTime++; setReject("TIME_NOT_READY"); return; }   // 沒校時不亂填日期
  { Lock l; if (job.active && !jobReleasable(job.ev, job.ph)) { rejBusy++; setReject("BUSY_KEEP_OLD_JOB"); return; } }

  uint8_t* buf = nullptr; size_t n = 0; char why[40] = "";
  const bool got = captureJpeg(&buf, &n, why, sizeof why);   // 不在上傳失敗時重拍：這張就是當次原圖

  Lock l;
  if (job.jpeg) { free(job.jpeg); job.jpeg = nullptr; }
  job = Job();
  job.active = true;
  eventSeq++;
  if (!buildEventId(job.eventId, sizeof job.eventId, DEVICE_ID, bootId, eventSeq)) { job.active = false; if (buf) free(buf); setReject("EVENT_ID_INVALID"); return; }
  formatUtc(now, job.observedAt, sizeof job.observedAt);
  strlcpy(job.type, isTest ? "test" : "anomaly", sizeof job.type);
  char ts[16];
  if (isnan(t)) strcpy(ts, "na"); else snprintf(ts, sizeof ts, "%.1f", t);
  snprintf(job.note, sizeof job.note, "hum=%.1f th=%d temp=%s src=%s", h, th, ts, isTest ? "test" : "dht11");
  snprintf(job.payload, sizeof job.payload,
           "{\"event_id\":\"%s\",\"device_id\":\"%s\",\"observed_at\":\"%s\",\"type\":\"%s\",\"note\":\"%s\"}",
           job.eventId, DEVICE_ID, job.observedAt, job.type, job.note);
  job.ev = EventStage::PENDING;
  if (got) {
    job.jpeg = buf; job.jpegLen = n; sha256Hex(buf, n, job.jpegSha);
    job.ph = PhotoStage::WAIT_EVENT;
  } else {
    job.ph = PhotoStage::CAPTURE_FAILED;                     // 當次沒有照片：保留原因，不假填
    strlcpy(job.phReason, why, sizeof job.phReason);
  }
  lastReject[0] = 0;
  logf("[trigger] 新事件 %s at %s photo=%s(%u bytes)", job.eventId, job.observedAt, got ? "captured" : why, (unsigned)n);
}

// ===================== 雲端：MQTT over TLS =====================
static void mqttEvent(void*, esp_event_base_t, int32_t id, void* data) {
  esp_mqtt_event_handle_t e = (esp_mqtt_event_handle_t)data;
  switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED: mqttUp = true; mqttErr[0] = 0; break;
    case MQTT_EVENT_DISCONNECTED: mqttUp = false; break;
    case MQTT_EVENT_PUBLISHED: pubackId = e->msg_id; break;      // 收到 PUBACK
    case MQTT_EVENT_ERROR:
      if (e->error_handle) snprintf(mqttErr, sizeof mqttErr, "type=%d connack=%d tls=0x%x",
                                    (int)e->error_handle->error_type, (int)e->error_handle->connect_return_code,
                                    (unsigned)e->error_handle->esp_tls_last_esp_err);
      break;
    default: break;
  }
}
static void ensureMqtt() {
  if (mqtt) return;
  esp_mqtt_client_config_t mc = {};
  mc.broker.address.hostname = cfg.iotHost.c_str();
  mc.broker.address.port = 8883;
  mc.broker.address.transport = MQTT_TRANSPORT_OVER_SSL;
  mc.broker.verification.certificate = cfg.caIot.c_str();           // 驗證 IoT Core 伺服器
  mc.credentials.client_id = cfg.clientId.c_str();
  mc.credentials.authentication.certificate = cfg.devCert.c_str();  // 雙向 TLS：裝置憑證與私鑰
  mc.credentials.authentication.key = cfg.devKey.c_str();
  mc.session.protocol_ver = MQTT_PROTOCOL_V_3_1_1;
  mc.session.keepalive = 30;
  mc.network.reconnect_timeout_ms = 3000;
  mqtt = esp_mqtt_client_init(&mc);
  if (!mqtt) return;
  esp_mqtt_client_register_event(mqtt, MQTT_EVENT_ANY, mqttEvent, nullptr);
  esp_mqtt_client_start(mqtt);
}

// ===================== 雲端：HTTPS =====================
static int httpsCall(const String& url, const uint8_t* body, size_t len, String* resp) {
  WiFiClientSecure client;
  client.setCACert(cfg.caApi.c_str());      // 只信任課程給的 CA；不關閉驗證
  HTTPClient http;
  http.setConnectTimeout(8000);
  http.setTimeout(12000);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  if (!http.begin(client, url)) return -1;
  http.addHeader("Authorization", String("Bearer ") + cfg.token);
  int code;
  if (body) { http.addHeader("Content-Type", "image/jpeg"); code = http.POST((uint8_t*)body, len); }
  else code = http.GET();
  if (code > 0 && resp) *resp = http.getString();
  http.end();
  return code;
}

// 收據核對。回傳 0=相符 1=事件未存(可等待) 2=內容不符(卡住)
static int checkReceipt(const String& body, bool wantPhoto, bool* photoReady) {
  JsonDocument doc;
  if (deserializeJson(doc, body)) return 2;
  if (strcmp(doc["event_id"] | "", job.eventId) != 0 || strcmp(doc["device_id"] | "", DEVICE_ID) != 0) return 2;
  if (!(doc["event_stored"] | false)) return 1;
  // 內容指紋：與本機固定的事件內容比對
  // TODO(教師規格)：指紋演算法細節見 w09_logic.h fingerprintInput；欄位名稱 event_fingerprint 待確認。
  const char* fp = doc["event_fingerprint"] | (const char*)nullptr;
  if (fp) {
    char in[300], hex[65];
    int n = fingerprintInput(in, sizeof in, DEVICE_ID, job.observedAt, job.type, job.note);
    sha256Hex((const uint8_t*)in, n, hex);
    if (strcasecmp(fp, hex) != 0) return 2;
  }
  if (wantPhoto) {
    *photoReady = strcmp(doc["photo_state"] | "", "ready") == 0;
    const char* ph = doc["photo_sha256"] | (const char*)nullptr;
    if (*photoReady && (!ph || strcasecmp(ph, job.jpegSha) != 0)) return 2;
  }
  return 0;
}

// ===================== 雲端工作迴圈（獨立 task，不卡感測與 LED）=====================
static bool failAttempt(bool isEvent, const char* why) {
  Lock l;
  job.attempts++;
  const bool give = job.attempts >= MAX_ATTEMPTS;
  if (!give) job.nextMs = millis() + backoffMs(job.attempts);
  (void)isEvent; (void)why;
  return give;
}

static void stepEvent(uint32_t now) {
  switch (job.ev) {
    case EventStage::PENDING: {
      if (!mqttUp) return;                       // 等連上，不算失敗
      const int id = esp_mqtt_client_publish(mqtt, cfg.topic.c_str(), job.payload, 0, 1, 0);   // QoS 1
      if (id < 0) { if (failAttempt(true, "publish")) setEv(EventStage::BLOCKED, "publish failed; CLOUD RETRY"); return; }
      { Lock l; job.pubMsgId = id; job.pubMs = now; }
      pubackId = -1;
      setEv(EventStage::PUBACK_WAIT);
      break;
    }
    case EventStage::PUBACK_WAIT:
      if (pubackId == job.pubMsgId && job.pubMsgId >= 0) {
        { Lock l; job.published = true; job.attempts = 0; job.polls = 0; job.nextMs = now; }
        setEv(EventStage::RECEIPT_WAIT, "PUBACK ok");        // PUBACK 只代表發布階段
      } else if (now - job.pubMs > PUBACK_TIMEOUT_MS) {
        if (failAttempt(true, "puback")) setEv(EventStage::BLOCKED, "PUBACK timeout; CLOUD RETRY");
        else setEv(EventStage::PENDING, "PUBACK timeout, retry same id");
      }
      break;
    case EventStage::RECEIPT_WAIT: {
      String body;
      const int code = httpsCall(cfg.apiBase + "/w09/device/events/" + job.eventId, nullptr, 0, &body);
      { Lock l; job.lastHttp = code; }
      const Next a = receiptAction(code);
      if (a == Next::DONE) {
        bool dummy = false;
        const int r = checkReceipt(body, false, &dummy);
        if (r == 0) {
          setEv(EventStage::STORED, "receipt ok");
          Lock l; if (job.ph == PhotoStage::WAIT_EVENT) { job.ph = PhotoStage::UPLOAD; job.attempts = 0; job.nextMs = now; }
          return;
        }
        if (r == 2) { setEv(EventStage::BLOCKED, "receipt mismatch"); return; }
      } else if (a == Next::BLOCK) { char m[40]; snprintf(m, sizeof m, "receipt http %d", code); setEv(EventStage::BLOCKED, m); return; }
      // 事件尚在處理或暫時失敗：保留原 ID 有限等待，不用固定 delay 猜成功
      bool giveUp;
      { Lock l; job.polls++; job.nextMs = now + RECEIPT_POLL_MS; giveUp = job.polls >= MAX_RECEIPT_POLLS; }
      if (giveUp) setEv(EventStage::BLOCKED, "receipt wait timeout; CLOUD RETRY");
      break;
    }
    default: break;
  }
}

static void stepPhoto(uint32_t now) {
  switch (job.ph) {
    case PhotoStage::UPLOAD: {
      if (!job.jpeg) { setPh(PhotoStage::CAPTURE_FAILED, "no_original_image"); return; }
      const int code = httpsCall(cfg.apiBase + "/w09/device/events/" + job.eventId + "/photo", job.jpeg, job.jpegLen, nullptr);
      { Lock l; job.lastHttp = code; }
      const Next a = photoAction(code);
      if (a == Next::DONE) { { Lock l; job.polls = 0; job.attempts = 0; job.nextMs = now; } setPh(PhotoStage::VERIFY, code == 201 ? "uploaded 201" : "resend 200"); }
      else if (a == Next::RECHECK_EVENT) {                 // 附圖 404：回頭核對原事件，不改 ID
        { Lock l; job.polls = 0; job.nextMs = now; job.ph = PhotoStage::WAIT_EVENT; }
        setEv(EventStage::RECEIPT_WAIT, "photo 404, recheck event");
      }
      else if (a == Next::BLOCK) { char m[40]; snprintf(m, sizeof m, "upload http %d", code); setPh(PhotoStage::BLOCKED, m); }
      else if (failAttempt(false, "upload")) { char m[48]; snprintf(m, sizeof m, "upload failed http %d; CLOUD RETRY", code); setPh(PhotoStage::BLOCKED, m); }
      break;
    }
    case PhotoStage::VERIFY: {
      String body;
      const int code = httpsCall(cfg.apiBase + "/w09/device/events/" + job.eventId, nullptr, 0, &body);
      { Lock l; job.lastHttp = code; }
      if (receiptAction(code) == Next::DONE) {
        bool ready = false;
        const int r = checkReceipt(body, true, &ready);
        if (r == 2) { setPh(PhotoStage::BLOCKED, "photo sha/receipt mismatch"); return; }
        if (r == 0 && ready) { setPh(PhotoStage::READY, "receipt photo ready"); return; }
      } else if (receiptAction(code) == Next::BLOCK) { char m[40]; snprintf(m, sizeof m, "verify http %d", code); setPh(PhotoStage::BLOCKED, m); return; }
      bool giveUp;
      { Lock l; job.polls++; job.nextMs = now + RECEIPT_POLL_MS; giveUp = job.polls >= MAX_RECEIPT_POLLS; }
      if (giveUp) setPh(PhotoStage::BLOCKED, "photo verify timeout; CLOUD RETRY");
      break;
    }
    default: break;
  }
}

static void cloudStep() {
  if (!cfgReady || WiFi.status() != WL_CONNECTED) return;
  if (!timeLooksSynced((long long)time(nullptr))) return;
  ensureMqtt();
  if (!job.active || jobFinished(job.ev, job.ph)) return;
  const uint32_t now = millis();
  if ((int32_t)(now - job.nextMs) < 0) return;
  if (job.ev != EventStage::STORED) stepEvent(now);
  else stepPhoto(now);
}

static void cloudTask(void*) {
  for (;;) { cloudStep(); vTaskDelay(pdMS_TO_TICKS(100)); }
}

// ===================== 本地狀態：Serial 與網頁 =====================
static void buildStatus(String& out) {
  JsonDocument d;
  d["group_id"] = GROUP_ID; d["device_id"] = DEVICE_ID; d["boot_id"] = bootId;
  d["uptime_ms"] = millis();
  if (sensorOk) { d["temp_c"] = lastT; d["humidity_pct"] = lastH; } else { d["temp_c"] = nullptr; d["humidity_pct"] = nullptr; }
  d["threshold_pct"] = curTh; d["knob_raw"] = knobRaw; d["sensor_ok"] = sensorOk; d["state"] = stateName(state);
  d["test_mode"] = testMode;
  d["triggers"] = det.fired; d["rejected_busy"] = rejBusy; d["rejected_time"] = rejTime; d["dropped"] = dropped;
  d["last_reject"] = lastReject;
  d["camera_ok"] = cameraOk; if (!cameraOk) d["camera_err"] = cameraErr;
  d["wifi"] = WiFi.status() == WL_CONNECTED; d["time_synced"] = timeLooksSynced((long long)time(nullptr));
  d["mqtt"] = (bool)mqttUp; d["mqtt_err"] = mqttErr;
  d["cfg_ready"] = cfgReady; d["cfg_missing"] = cfgMissing;
  Lock l;
  JsonObject j = d["job"].to<JsonObject>();
  j["active"] = job.active;
  if (job.active) {
    j["event_id"] = job.eventId; j["observed_at"] = job.observedAt; j["type"] = job.type;
    j["event_stage"] = eventStageName(job.ev); j["event_reason"] = job.evReason;
    j["photo_stage"] = photoStageName(job.ph); j["photo_reason"] = job.phReason;
    j["jpeg_bytes"] = (unsigned)job.jpegLen; j["jpeg_sha256"] = job.jpegSha;
    j["attempts"] = job.attempts; j["last_http"] = job.lastHttp; j["finished"] = jobFinished(job.ev, job.ph);
  }
  serializeJson(d, out);
}

static void startWeb() {
  if (webUp) return;
  server.on("/status", []() { String s; buildStatus(s); server.send(200, "application/json", s); });
  server.on("/photo", []() {
    Lock l;
    if (!job.jpeg) { server.send(404, "text/plain", "no photo"); return; }
    server.setContentLength(job.jpegLen);
    server.send(200, "image/jpeg", "");
    server.sendContent((const char*)job.jpeg, job.jpegLen);
  });
  server.begin(); webUp = true;
}

static void cmdRetry() {
  Lock l;
  if (!job.active) { Serial.println("[cmd] 沒有工作"); return; }
  job.attempts = 0; job.polls = 0; job.nextMs = millis();
  if (job.ev == EventStage::BLOCKED) job.ev = job.published ? EventStage::RECEIPT_WAIT : EventStage::PENDING;
  else if (job.ev == EventStage::STORED && job.ph == PhotoStage::BLOCKED && job.jpeg) job.ph = PhotoStage::UPLOAD;
  else if (job.ph == PhotoStage::CAPTURE_FAILED) { logf("[cmd] 當次沒有原圖(%s)，不能冒充；請重新觸發一次新事件", job.phReason); return; }
  Serial.println("[cmd] 沿用原事件/原圖重試");
}
static void cmdDrop() {
  Lock l;
  if (job.jpeg) free(job.jpeg);
  job = Job(); dropped++;
  Serial.println("[cmd] 已放棄 RAM 工作（雲端既有紀錄不變）");
}

// ---- NVS 配置寫入（由 tools/w09_provision.py 經 USB 呼叫）----
// 只寫入、不提供讀回：私鑰與權杖寫進去之後，沒有任何指令能把值印出來。
static const char* const NVS_KEYS[] = {NVS_WIFI_SSID, NVS_WIFI_PASS, NVS_API_BASE, NVS_IOT_HOST, NVS_IOT_TOPIC,
                                       NVS_IOT_CLIENT, NVS_TOKEN_DEVICE, NVS_CA_API, NVS_CA_IOT, NVS_DEV_CERT, NVS_DEV_KEY};
static String nvsKey, nvsB64;
static const size_t NVS_MAX_VALUE = 3900;   // Preferences 字串上限約 4000 bytes

static bool nvsKeyAllowed(const String& k) {
  for (const char* a : NVS_KEYS) if (k == a) return true;
  return false;
}
static void nvsEnd() {
  if (!nvsKey.length()) { Serial.println("[nvs] ERR no_begin"); return; }
  const size_t cap = nvsB64.length() + 4;
  uint8_t* out = (uint8_t*)malloc(cap);
  size_t olen = 0;
  const int rc = out ? mbedtls_base64_decode(out, cap, &olen, (const uint8_t*)nvsB64.c_str(), nvsB64.length()) : -1;
  if (rc != 0 || olen == 0 || olen > NVS_MAX_VALUE) { free(out); Serial.println("[nvs] ERR decode_or_size"); nvsKey = ""; nvsB64 = ""; return; }
  out[olen] = 0;
  Preferences p;
  bool ok = p.begin(NVS_NS, false) && p.putString(nvsKey.c_str(), (const char*)out) == olen;
  p.end();
  free(out);
  if (ok) logf("[nvs] OK key=%s bytes=%u（重新開機後生效）", nvsKey.c_str(), (unsigned)olen);
  else Serial.println("[nvs] ERR write_failed");
  nvsKey = ""; nvsB64 = "";
}
static void nvsList() {
  Preferences p;
  const bool opened = p.begin(NVS_NS, true);
  for (const char* k : NVS_KEYS) {
    const size_t n = opened ? p.getString(k, "").length() : 0;
    logf("[nvs] %-12s %s", k, n ? "set" : "MISSING");     // 只顯示有沒有，不顯示內容
  }
  if (opened) p.end();
}
static void nvsClear(const String& which) {
  Preferences p;
  if (!p.begin(NVS_NS, false)) { Serial.println("[nvs] ERR open"); return; }
  if (which == "ALL") { p.clear(); Serial.println("[nvs] OK cleared ALL"); }
  else if (nvsKeyAllowed(which)) { p.remove(which.c_str()); logf("[nvs] OK cleared %s", which.c_str()); }
  else Serial.println("[nvs] ERR unknown_key");
  p.end();
}
static bool handleNvs(const String& s) {
  if (!s.startsWith("NVS ")) return false;
  if (s.startsWith("NVS BEGIN ")) {
    String k = s.substring(10); k.trim();
    if (!nvsKeyAllowed(k)) { Serial.println("[nvs] ERR unknown_key"); nvsKey = ""; return true; }
    nvsKey = k; nvsB64 = ""; Serial.println("[nvs] READY");
  } else if (s.startsWith("NVS DATA ")) {
    if (!nvsKey.length() || nvsB64.length() + s.length() > NVS_MAX_VALUE * 2) { Serial.println("[nvs] ERR data"); nvsKey = ""; nvsB64 = ""; return true; }
    nvsB64 += s.substring(9);
    Serial.println("[nvs] ACK");
  } else if (s == "NVS END") nvsEnd();
  else if (s == "NVS LIST") nvsList();
  else if (s.startsWith("NVS CLEAR ")) { String w = s.substring(10); w.trim(); nvsClear(w); }
  else Serial.println("[nvs] ERR unknown_cmd");
  return true;
}

static void handleLine(String s) {
  s.trim();
  if (handleNvs(s)) return;
  if (s == "STATUS") { String o; buildStatus(o); Serial.println(o); }
  else if (s == "CLOUD RETRY") cmdRetry();
  else if (s == "CLOUD DROP") cmdDrop();
  else if (s == "CLOUD REBOOT") { Serial.println("[cmd] 重新開機會失去 RAM 工作"); delay(100); ESP.restart(); }
  else if (s == "TEST OFF") { testMode = false; Serial.println("[cmd] 回到真實感測"); }
  else if (s.startsWith("TEST H=")) { testH = s.substring(7).toFloat(); testMode = true; logf("[cmd] 測試輸入 humidity=%.1f（事件 type=test）", testH); }
  else if (s == "SELFTEST") { int f = runSelfTest(logLine); logf("[selftest] 失敗 %d 項", f); }
  else if (s.length()) Serial.println("指令: STATUS | CLOUD RETRY | CLOUD DROP | CLOUD REBOOT | TEST H=<數字> | TEST OFF | SELFTEST");
}
static void pollSerial() {
  static String line;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') { if (line.length()) { handleLine(line); line = ""; } }
    else if (line.length() < 1400) line += c;     // NVS DATA 一行最多約 1.2KB
  }
}

// ===================== setup / loop =====================
static void wifiTick() {
  static bool started = false;
  static uint32_t lastTry = 0;
  if (!cfg.ssid.length()) return;
  if (!started) {
    WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(true);
    WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
    started = true; lastTry = millis();
  }
  if (WiFi.status() == WL_CONNECTED) {
    if (!timeSynced) { configTime(0, 0, "pool.ntp.org", "time.google.com"); timeSynced = true; }   // NTP 校時（UTC）
    startWeb();
  }
}

void setup() {
  Serial.setRxBufferSize(4096);     // 配置寫入時一行較長，須在 begin 前設定
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, !LED_ON);
  analogReadResolution(12);
  dht.begin();
  jobMu = xSemaphoreCreateMutex();
  snprintf(bootId, sizeof bootId, "%04X", (unsigned)(esp_random() & 0xFFFF));   // 區分同裝置不同開機
  loadConfig();
  cameraOk = initCamera();
  logf("W09 boot device=%s boot_id=%s camera=%s cfg=%s", DEVICE_ID, bootId, cameraOk ? "ok" : cameraErr.c_str(),
       cfgReady ? "ready" : cfgMissing.c_str());
  xTaskCreatePinnedToCore(cloudTask, "cloud", 16384, nullptr, 1, nullptr, 0);
}

void loop() {
  static uint32_t lastRead = 0;
  const uint32_t now = millis();
  pollSerial();
  wifiTick();
  if (webUp) server.handleClient();

  curTh = readThreshold();
  if (now - lastRead >= SENSOR_PERIOD_MS) {
    lastRead = now;
    readSensor();
    String s; buildStatus(s); Serial.println(s);
  }
  const float h = testMode ? testH : lastH;
  const bool ok = testMode ? true : sensorOk;
  const State cur = decide(ok, h, curTh);
  if (det.step(cur)) onTrigger(h, lastT, curTh, testMode);   // 只在 NORMAL→ALARM 的那一輪
  state = cur;
  updateLed(now);
}
