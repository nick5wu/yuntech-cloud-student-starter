# W09_CloudCamera

W7 感測／警報 + W8 相機 + W9 事件與照片上雲。板型 ESP32-S3（16MB／OPI PSRAM／native USB），Core 3.3.7。

## 編譯（本機 Windows）

```powershell
. .\.local\arduino-cli\env.ps1
arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=app3M_fat9M_16MB" --build-path D:\w09cli\build-w09 arduino/W09_CloudCamera
```

- 工具鏈不能處理含中文的路徑：Core／函式庫在 `D:\w09cli`，build 目錄也要用純英文路徑。
- `tools/w09_build.ps1` 預設 build 在 `.local/w09-build`（專案路徑有中文會連結失敗），且該檔是無 BOM 的 UTF-8，Windows PowerShell 5.1 會語法錯誤；本機改用上面的指令。

## 行為

- NORMAL→ALARM 才固定一次事件；開機即 ALARM、SENSOR_ERROR→ALARM 不觸發。
- 事件 ID：`<device_id>-<開機識別4碼>-<序號>`；重送沿用原 ID／原時間／原圖。
- 流程：MQTT QoS1 等 PUBACK → `GET /w09/device/events/{id}` 收據 → `POST …/photo` → 收據 `photo_state=ready` 且 SHA-256 相符。
- 事件與照片進度分開；單筆工作，滿載時拒收新觸發並保留舊工作。

## Serial 指令

`STATUS`｜`CLOUD RETRY`｜`CLOUD DROP`｜`CLOUD REBOOT`｜`TEST H=<溼度>`（事件 type=test）｜`TEST OFF`｜`SELFTEST`

本地網頁：`/status`（JSON）、`/photo`（最近一張）。

## 尚未完成（需教師規格或本組實測值）

- `w09_config.h`：`GROUP_ID`、`DEVICE_ID`、`RAW_MIN`／`RAW_MAX`（目前是範例值）。
- NVS 配置（Wi-Fi、入口、topic、client ID、token、CA、裝置憑證／私鑰）：鍵名是暫定的，需對齊教師配置工具；寫入步驟尚未做。
- 收據 `event_fingerprint` 演算法（`w09_logic.h` 的 `fingerprintInput`）：欄位順序與格式待教師確認。
- 尚未在實體板子上測試（相機、DHT、TLS、MQTT、HTTPS 均只通過編譯）。
