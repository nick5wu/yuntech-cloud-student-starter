// W9 純邏輯：不依賴 Arduino／網路，狀態判斷、觸發偵測、事件 ID、工作階段與錯誤分流。
// 狀態機規則放這裡，編譯期用 static_assert 驗證（見 w09_selftest.h）。
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

namespace w09 {

// ---------- W7：感測狀態 ----------
enum class State : uint8_t { UNKNOWN, NORMAL, ALARM, SENSOR_ERROR };

constexpr const char* stateName(State s) {
  return s == State::NORMAL ? "NORMAL"
       : s == State::ALARM ? "ALARM"
       : s == State::SENSOR_ERROR ? "SENSOR_ERROR" : "UNKNOWN";
}

// 先看有沒有讀到，再比門檻（NaN 比大小一律 false，所以讀值有效性要排第一）。
constexpr State decide(bool sensorOk, float humidity, int threshold) {
  return !sensorOk ? State::SENSOR_ERROR
       : (humidity >= (float)threshold ? State::ALARM : State::NORMAL);
}

// ---------- W8/W9：只在 NORMAL → ALARM 觸發一次 ----------
// 開機即 ALARM、SENSOR_ERROR → ALARM 都不是指定的轉換。
struct TriggerDetector {
  State prev = State::UNKNOWN;
  uint32_t fired = 0;
  constexpr bool step(State cur) {
    const bool fire = (prev == State::NORMAL && cur == State::ALARM);
    prev = cur;
    if (fire) fired++;
    return fire;
  }
};

// ---------- 事件 ID：裝置-開機識別-序號 ----------
inline bool validIdChar(char c) {
  return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_' || c == '-';
}
inline bool buildEventId(char* out, size_t cap, const char* device, const char* boot, uint32_t seq) {
  if (!out || cap == 0) return false;
  const int n = snprintf(out, cap, "%s-%s-%lu", device, boot, (unsigned long)seq);
  if (n <= 0 || (size_t)n >= cap || n > 64) { out[0] = 0; return false; }
  for (int i = 0; i < n; i++) if (!validIdChar(out[i])) { out[0] = 0; return false; }
  return true;
}

// observed_at：以 UTC 固定，格式 2026-11-03T09:10:00Z（含時區）。
inline bool formatUtc(time_t t, char* out, size_t cap) {
  struct tm tmv;
  if (cap < 21 || !gmtime_r(&t, &tmv)) return false;
  return strftime(out, cap, "%Y-%m-%dT%H:%M:%SZ", &tmv) == 20;
}
constexpr bool timeLooksSynced(long long epoch) { return epoch >= 1700000000LL; }  // 2023-11 之後

// ---------- 工作階段：事件與照片各有自己的進度 ----------
enum class EventStage : uint8_t { NONE, PENDING, PUBACK_WAIT, RECEIPT_WAIT, STORED, BLOCKED };
enum class PhotoStage : uint8_t { NONE, WAIT_EVENT, UPLOAD, VERIFY, READY, CAPTURE_FAILED, BLOCKED };

constexpr const char* eventStageName(EventStage s) {
  return s == EventStage::PENDING ? "EVENT_PENDING"
       : s == EventStage::PUBACK_WAIT ? "PUBACK_WAIT"
       : s == EventStage::RECEIPT_WAIT ? "RECEIPT_WAIT"
       : s == EventStage::STORED ? "EVENT_STORED"
       : s == EventStage::BLOCKED ? "BLOCKED" : "NONE";
}
constexpr const char* photoStageName(PhotoStage s) {
  return s == PhotoStage::WAIT_EVENT ? "WAIT_EVENT"
       : s == PhotoStage::UPLOAD ? "PHOTO_PENDING"
       : s == PhotoStage::VERIFY ? "PHOTO_VERIFY"
       : s == PhotoStage::READY ? "COMPLETE"
       : s == PhotoStage::CAPTURE_FAILED ? "CAPTURE_FAILED"
       : s == PhotoStage::BLOCKED ? "BLOCKED" : "NONE";
}

// 工作結束＝事件失敗卡住，或（事件已存且照片已有定論）。
constexpr bool jobFinished(EventStage e, PhotoStage p) {
  return e == EventStage::BLOCKED ||
         (e == EventStage::STORED &&
          (p == PhotoStage::READY || p == PhotoStage::CAPTURE_FAILED || p == PhotoStage::BLOCKED));
}

// 新觸發可以接手的條件：事件已存，且照片已成功或確定沒有原圖。
// BLOCKED（含照片 BLOCKED）仍保留原事件與原圖，須 CLOUD RETRY 或明確 CLOUD DROP，不被新觸發覆寫。
constexpr bool jobReleasable(EventStage e, PhotoStage p) {
  return e == EventStage::STORED && (p == PhotoStage::READY || p == PhotoStage::CAPTURE_FAILED);
}

// ---------- HTTP 結果分流：用階段與可否重試判讀，不只背狀態碼 ----------
enum class Next : uint8_t { DONE, RETRY, BLOCK, RECHECK_EVENT };

// 查事件收據：404 = 事件可能仍在處理，保留原 ID 有限等待，不換 ID。
constexpr Next receiptAction(int http) {
  return http == 200 ? Next::DONE
       : (http == 404 || http == 408 || http == 429 || http <= 0 || http >= 500) ? Next::RETRY
       : Next::BLOCK;
}
// 上傳照片：404 = 事件不存在，先回頭核對事件；400/413/415 修輸入；409 內容衝突不重試。
constexpr Next photoAction(int http) {
  return (http == 200 || http == 201) ? Next::DONE
       : http == 404 ? Next::RECHECK_EVENT
       : (http == 408 || http == 429 || http <= 0 || http >= 500) ? Next::RETRY
       : Next::BLOCK;
}

constexpr int MAX_ATTEMPTS = 6;        // 每一段最多自動重試次數（有限重試）
constexpr int MAX_RECEIPT_POLLS = 20;  // 收據有限等待次數
constexpr uint32_t RECEIPT_POLL_MS = 1500;
constexpr uint32_t PUBACK_TIMEOUT_MS = 10000;

constexpr uint32_t backoffMs(int attempt) {
  return attempt <= 0 ? 1000u : (attempt >= 5 ? 30000u : (1000u << attempt));
}

// 事件指紋輸入：device_id、observed_at(UTC)、type、note，固定順序、換行連接。
// TODO(教師規格)：確認欄位順序、是否含 event_id，以及 note 的正規化方式。
inline int fingerprintInput(char* out, size_t cap, const char* device, const char* observedAt,
                            const char* type, const char* note) {
  return snprintf(out, cap, "%s\n%s\n%s\n%s", device, observedAt, type, note);
}

}  // namespace w09
