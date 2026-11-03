// ESP32-S3-CAM + OV2640 腳位（依 W8 補充 B 的「本次板型」對照表）。
// 若 AI 或範例給了別款 ESP32-CAM 的腳位，先更正這裡。
#pragma once

#define CAM_PIN_PWDN   -1
#define CAM_PIN_RESET  -1
#define CAM_PIN_XCLK   15
#define CAM_PIN_SIOD    4   // SCCB 資料
#define CAM_PIN_SIOC    5   // SCCB 時鐘
// D0..D7 依序 11, 9, 8, 10, 12, 18, 17, 16
#define CAM_PIN_D0     11
#define CAM_PIN_D1      9
#define CAM_PIN_D2      8
#define CAM_PIN_D3     10
#define CAM_PIN_D4     12
#define CAM_PIN_D5     18
#define CAM_PIN_D6     17
#define CAM_PIN_D7     16
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK   13
