"""把 W9 的設定寫進 ESP32 的 NVS（經 USB Serial）。

- 密碼與權杖用隱藏輸入；不放命令列、不寫檔、不印出。
- 憑證／私鑰只讀你指定的檔案，直接送進板子，不複製、不顯示。
- 板子端只寫入、不提供讀回；`--list` 只會顯示每個鍵「有設定」或「MISSING」。

用法（本機筆電，板子已接 USB）：
  python tools/w09_provision.py --port COM5 --list
  python tools/w09_provision.py --port COM5 --wifi --api-base https://<入口> \
      --iot --token --ca-api <API的CA.pem> --dev-cert <裝置.cert.pem> --dev-key <裝置.private.key>
"""
import argparse
import base64
import getpass
import re
import sys
import time
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("請先安裝 pyserial：python -m pip install pyserial")

ROOT = Path(__file__).resolve().parents[1]
CHUNK = 900   # base64 字元數；板子單行上限約 1400


class ProvisionError(Exception):
    pass


def open_port(name):
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = name, 115200, 0.3
    ser.dtr = False          # 避免控制線讓板子重置
    ser.rts = False
    ser.open()
    return ser


def read_tagged(ser, wait=4.0):
    """讀到第一行 [nvs] 開頭的回應；狀態 JSON 等其他輸出略過。"""
    end = time.time() + wait
    while time.time() < end:
        line = ser.readline().decode("utf-8", "replace").strip()
        if line.startswith("[nvs]"):
            return line
    raise ProvisionError("板子沒有回應；確認韌體是 W09_CloudCamera、埠號正確，且序列監控沒有佔用埠。")


def send(ser, text):
    ser.write((text + "\n").encode("utf-8"))
    ser.flush()


def put(ser, key, value: bytes):
    if not value:
        raise ProvisionError(f"{key}：內容是空的。")
    ser.reset_input_buffer()
    send(ser, "NVS BEGIN " + key)
    if "READY" not in read_tagged(ser):
        raise ProvisionError(f"{key}：板子拒絕（鍵名不認得？）")
    b64 = base64.b64encode(value).decode("ascii")
    for i in range(0, len(b64), CHUNK):
        send(ser, "NVS DATA " + b64[i:i + CHUNK])
        if "ACK" not in read_tagged(ser):
            raise ProvisionError(f"{key}：傳輸中斷。")
    send(ser, "NVS END")
    reply = read_tagged(ser)
    if "OK" not in reply:
        raise ProvisionError(f"{key}：寫入失敗（{reply}）")
    print(f"  ✔ {key}（{len(value)} bytes）")


def read_pem(path, what, expect):
    p = Path(path)
    if not p.is_file():
        raise ProvisionError(f"{what}：找不到檔案 {p}")
    data = p.read_bytes()
    if expect not in data:
        raise ProvisionError(f"{what}：內容不像 {expect.decode()}，請確認選對檔案。")
    return data.replace(b"\r\n", b"\n")


def hidden(prompt):
    v = getpass.getpass(prompt).strip()
    if not v or any(c.isspace() for c in v):
        raise ProvisionError("輸入是空的或含空白；只貼值本身。")
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="板子的 COM 埠；不給則列出可用埠")
    ap.add_argument("--list", action="store_true", help="只列出各鍵有沒有設定")
    ap.add_argument("--wifi", action="store_true", help="寫入 Wi-Fi 名稱與密碼（密碼隱藏輸入）")
    ap.add_argument("--api-base", help="本組 HTTPS 入口，例如 https://xxx（不含路徑）")
    ap.add_argument("--iot", action="store_true", help="寫入 IoT 端點、topic、client ID")
    ap.add_argument("--iot-host", help="IoT 端點主機名；省略時讀 .local/iot/endpoint")
    ap.add_argument("--topic", default="cxe/g06-1/gw/events")
    ap.add_argument("--client-id", default="g06-esp32-s3-cam")
    ap.add_argument("--token", action="store_true", help="寫入 device 權杖（隱藏輸入）")
    ap.add_argument("--ca-api", help="API 信任 CA 的 PEM 檔")
    ap.add_argument("--ca-iot", help="AmazonRootCA1 PEM；省略時用 .local/iot/AmazonRootCA1.pem")
    ap.add_argument("--dev-cert", help="裝置憑證 PEM 檔")
    ap.add_argument("--dev-key", help="裝置私鑰 PEM 檔（只送進板子）")
    ap.add_argument("--clear", metavar="KEY|ALL", help="清除指定鍵或全部")
    ap.add_argument("--token-from-env-file", metavar="FILE",
                    help="從 env 檔讀 W09_DEVICE_TOKEN 直接寫進板子（不顯示、不經過聊天），例如 .local/app.env")
    ap.add_argument("--all", action="store_true",
                    help="一次寫入全部：Wi-Fi(隱藏輸入)、IoT、權杖(.local/app.env)、API CA(.local/w09-tls/ca.pem)、"
                         "裝置憑證與私鑰(.local/iot/cam.*)；需搭配 --api-base")
    a = ap.parse_args()
    if a.all:
        a.wifi = a.iot = True
        a.token_from_env_file = a.token_from_env_file or str(ROOT / ".local/app.env")
        a.ca_api = a.ca_api or str(ROOT / ".local/w09-tls/ca.pem")
        a.dev_cert = a.dev_cert or str(ROOT / ".local/iot/cam.cert.pem")
        a.dev_key = a.dev_key or str(ROOT / ".local/iot/cam.private.key")
        if not a.api_base:
            ap.error("--all 需要 --api-base https://<EC2 公開 DNS>")

    if not a.port:
        for p in list_ports.comports():
            print(f"{p.device}\t{p.description}")
        return 0
    try:
        ser = open_port(a.port)
    except serial.SerialException as exc:
        sys.exit(f"無法開啟 {a.port}：{exc}")
    try:
        if a.clear:
            send(ser, "NVS CLEAR " + a.clear)
            print(read_tagged(ser))
        if a.wifi:
            ssid = input("Wi-Fi 名稱（2.4 GHz）：").strip()
            put(ser, "wifi_ssid", ssid.encode())
            put(ser, "wifi_pass", hidden("Wi-Fi 密碼（隱藏）：").encode())
        if a.api_base:
            if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:443)?", a.api_base.rstrip("/")):
                raise ProvisionError("api-base 必須是 https://主機名，不含路徑。")
            put(ser, "api_base", a.api_base.rstrip("/").encode())
        if a.iot:
            host = a.iot_host or (ROOT / ".local/iot/endpoint").read_text(encoding="utf-8").strip()
            if not re.fullmatch(r"[a-z0-9-]+-ats\.iot\.[a-z0-9-]+\.amazonaws\.com", host):
                raise ProvisionError("IoT 端點格式不符。")
            put(ser, "iot_host", host.encode())
            put(ser, "iot_topic", a.topic.encode())
            put(ser, "iot_client", a.client_id.encode())
        if a.token:
            put(ser, "tok_device", hidden("device 權杖（隱藏）：").encode())
        if a.token_from_env_file:
            line = next((l for l in Path(a.token_from_env_file).read_text(encoding="utf-8").splitlines()
                         if l.startswith("W09_DEVICE_TOKEN=")), None)
            if not line or len(line) < 40:
                raise ProvisionError("env 檔裡沒有 W09_DEVICE_TOKEN；先完成雲端部署。")
            put(ser, "tok_device", line.split("=", 1)[1].strip().encode())
        if a.ca_api:
            put(ser, "ca_api", read_pem(a.ca_api, "API CA", b"BEGIN CERTIFICATE"))
        if a.iot:
            ca_iot = a.ca_iot or str(ROOT / ".local/iot/AmazonRootCA1.pem")
            put(ser, "ca_iot", read_pem(ca_iot, "IoT 根 CA", b"BEGIN CERTIFICATE"))
        if a.dev_cert:
            put(ser, "dev_cert", read_pem(a.dev_cert, "裝置憑證", b"BEGIN CERTIFICATE"))
        if a.dev_key:
            put(ser, "dev_key", read_pem(a.dev_key, "裝置私鑰", b"PRIVATE KEY"))
        send(ser, "NVS LIST")
        print("目前板上設定：")
        for _ in range(11):
            print("  " + read_tagged(ser, 3.0)[6:])
        print("寫入的值要重新開機後才生效。")
    except ProvisionError as exc:
        print("[待處理] " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[取消]", file=sys.stderr)
        return 2
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
