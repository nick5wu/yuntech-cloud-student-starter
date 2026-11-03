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

## 配置與雲端（本組自建，不是教師提供的服務）

- 組別：`GROUP_ID=g06`、`DEVICE_ID=g06-esp32-s3-cam`。
- 設定寫入板子：`python tools/w09_provision.py --port <COM> --all --api-base https://<EC2公開DNS>`（Wi-Fi 密碼隱藏輸入；其餘從 `.local/` 讀，不顯示）。
- 雲端：`app/service.py` 的 `/w09` 端點；照片存在私有 S3（`deploy/w09_s3.py`，EC2 用 LabInstanceProfile），RDS 只留關聯與摘要，先前存在 RDS 的照片仍可讀；HTTPS 用自建 CA（`.local/w09-tls/ca.pem`），EC2 重啟後 DNS 會變，需重跑 `deploy/w09_tls.py` 與 `deploy/w09_up.py`。
- 收據指紋：`SHA-256(device_id \n observed_at(UTC) \n type \n note)`，與 `app/w09_media.py` 一致；若教師公布不同規格，兩邊一起改。

## 尚未完成

- `w09_config.h` 的 `RAW_MIN`／`RAW_MAX` 仍是範例值（W7 實測值不在 repo）。
- 尚未在實體板子上測試（相機、DHT、TLS、MQTT、HTTPS 均只通過編譯；雲端端點已用真實權杖驗證權限分離）。
- 板子走手機熱點時，需把熱點的公網 IP `/32` 加進安全群組 443（`deploy/w09_up.py --allow <IP>/32 --apply`）。
