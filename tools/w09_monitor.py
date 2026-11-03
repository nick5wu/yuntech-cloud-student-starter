"""W9 序列監控：把板子每 2 秒的狀態 JSON 整理成一行，並標出事件 ID 與雲端進度。

用法：python tools/w09_monitor.py --port COM8
- 開埠時不動 DTR/RTS，不會讓板子重開機（重開機會失去進行中的工作）。
- 同一時間只能有一個程式開著這個 COM 埠；燒錄前請先按 Ctrl+C 關掉。
- 只讀，不送任何指令、不顯示任何設定值。
"""
import argparse
import json
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("請先安裝 pyserial：python -m pip install pyserial")


def short(s, n):
    return (s or "")[:n]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True)
    a = ap.parse_args()
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = a.port, 115200, 0.5
    ser.dtr = False
    ser.rts = False
    ser.open()
    print(f"監看 {a.port}（Ctrl+C 結束）。欄位：時間 狀態 溼度/門檻 觸發數 | 事件ID  事件進度 / 照片進度")
    last = None
    try:
        while True:
            raw = ser.readline().decode("utf-8", "replace").strip()
            if not raw:
                continue
            stamp = time.strftime("%H:%M:%S")
            if raw.startswith("{"):
                try:
                    s = json.loads(raw)
                except ValueError:
                    continue
                job = s.get("job", {})
                net = ("Wi-Fi" if s.get("wifi") else "無Wi-Fi") + "/" + ("MQTT" if s.get("mqtt") else "無MQTT")
                cam = "" if s.get("camera_ok") else " 相機失敗"
                line = (f"{stamp} {s.get('state'):<12} 溼度{s.get('humidity_pct')}% 門檻{s.get('threshold_pct')}% "
                        f"觸發{s.get('triggers')} {net}{cam}")
                if job.get("active"):
                    line += (f" | {job.get('event_id')}  {job.get('event_stage')} / {job.get('photo_stage')}"
                             + (f" ({job.get('photo_reason')})" if job.get("photo_reason") and job.get("photo_stage") != "COMPLETE" else ""))
                if line[9:] != last:           # 沒變化就不重複洗版
                    print(line)
                    last = line[9:]
            elif raw.startswith("[trigger]") or raw.startswith("[cloud]"):
                print(f"{stamp} {raw}")
    except KeyboardInterrupt:
        print("\n已結束。")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
