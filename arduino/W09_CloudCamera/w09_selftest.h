// 編譯期自測：編譯通過就代表這些規則成立。執行期自測見 runSelfTest()。
#pragma once
#include "w09_logic.h"

namespace w09 {

constexpr bool simulate(const State* seq, int n) {
  TriggerDetector d;
  for (int i = 0; i < n; i++) d.step(seq[i]);
  return d.fired;
}
constexpr uint32_t firedCount(State a, State b, State c, State d4, State e, State f) {
  TriggerDetector d;
  d.step(a); d.step(b); d.step(c); d.step(d4); d.step(e); d.step(f);
  return d.fired;
}

// W9 簡報 M01：正常→警報→維持→維持→正常→警報，只觸發 2 次（觀察 2 與 6）
static_assert(firedCount(State::NORMAL, State::ALARM, State::ALARM, State::ALARM, State::NORMAL, State::ALARM) == 2,
              "持續警報不可重複觸發，回到正常後再警報才觸發");
// 開機即 ALARM 不觸發
static_assert(firedCount(State::ALARM, State::ALARM, State::ALARM, State::ALARM, State::ALARM, State::ALARM) == 0,
              "開機即 ALARM 不符合 NORMAL→ALARM");
// SENSOR_ERROR → ALARM 不觸發
static_assert(firedCount(State::SENSOR_ERROR, State::ALARM, State::ALARM, State::NORMAL, State::SENSOR_ERROR, State::ALARM) == 0,
              "ERROR→ALARM 不符合 NORMAL→ALARM");

// 沒讀到不代表正常：讀值無效一律 SENSOR_ERROR，即使數值剛好超過門檻
static_assert(decide(false, 99.0f, 70) == State::SENSOR_ERROR, "無效讀值要 SENSOR_ERROR");
static_assert(decide(true, 70.0f, 70) == State::ALARM, "溼度 >= 門檻為 ALARM");
static_assert(decide(true, 69.9f, 70) == State::NORMAL, "溼度 < 門檻為 NORMAL");

// HTTP 分流
static_assert(receiptAction(404) == Next::RETRY, "收據 404 = 有限等待，不換 ID");
static_assert(photoAction(404) == Next::RECHECK_EVENT, "附圖 404 = 先核對事件");
static_assert(photoAction(409) == Next::BLOCK, "409 內容衝突不自動重試");
static_assert(photoAction(413) == Next::BLOCK && photoAction(415) == Next::BLOCK && photoAction(400) == Next::BLOCK, "輸入錯誤要修正");
static_assert(photoAction(503) == Next::RETRY && photoAction(-1) == Next::RETRY, "暫時失敗有限重試");
static_assert(photoAction(201) == Next::DONE && photoAction(200) == Next::DONE, "201 首次、200 原內容重送");
static_assert(photoAction(401) == Next::BLOCK && photoAction(403) == Next::BLOCK, "權限問題不重試");

// 工作結束條件：事件已存但照片失敗，仍算結束（保留原因），事件未存則不結束
static_assert(!jobFinished(EventStage::STORED, PhotoStage::UPLOAD), "照片未傳完不算結束");
static_assert(jobFinished(EventStage::STORED, PhotoStage::READY), "事件+照片成功才完成");
static_assert(jobFinished(EventStage::STORED, PhotoStage::CAPTURE_FAILED), "沒有原圖也要結束並保留原因");
static_assert(!jobFinished(EventStage::RECEIPT_WAIT, PhotoStage::CAPTURE_FAILED), "事件未確認不可結束");

// 滿載保護：卡住的工作不可被新觸發覆寫；成功或確定沒有原圖才可釋放
static_assert(!jobReleasable(EventStage::BLOCKED, PhotoStage::WAIT_EVENT), "卡住的事件不覆寫");
static_assert(!jobReleasable(EventStage::STORED, PhotoStage::BLOCKED), "待處理的原圖不覆寫");
static_assert(!jobReleasable(EventStage::STORED, PhotoStage::UPLOAD), "上傳中不覆寫");
static_assert(jobReleasable(EventStage::STORED, PhotoStage::READY) && jobReleasable(EventStage::STORED, PhotoStage::CAPTURE_FAILED), "完成可釋放");

static_assert(timeLooksSynced(1793000000LL) && !timeLooksSynced(30LL), "校時判斷");
static_assert(backoffMs(1) == 2000u && backoffMs(9) == 30000u, "退避有上限");

// 執行期自測（板上用 SELFTEST 指令）：event ID 與 UTC 格式。回傳失敗項數。
inline int runSelfTest(void (*log)(const char*)) {
  int fail = 0;
  char id[80], ts[32];
  if (!buildEventId(id, sizeof id, "g03-esp1", "A7F2", 1) || strcmp(id, "g03-esp1-A7F2-1") != 0) { log("FAIL event_id 格式"); fail++; }
  if (buildEventId(id, sizeof id, "bad id", "A7F2", 1)) { log("FAIL 非法字元應被拒絕"); fail++; }
  char longDev[70]; memset(longDev, 'a', 69); longDev[69] = 0;
  if (buildEventId(id, sizeof id, longDev, "A7F2", 1)) { log("FAIL 超長 ID 應被拒絕"); fail++; }
  if (!formatUtc((time_t)0, ts, sizeof ts) || strcmp(ts, "1970-01-01T00:00:00Z") != 0) { log("FAIL UTC 格式"); fail++; }
  if (!formatUtc((time_t)1793000000, ts, sizeof ts) || strcmp(ts, "2026-10-26T07:33:20Z") != 0) { log("FAIL UTC 換算"); fail++; }
  return fail;
}

}  // namespace w09
