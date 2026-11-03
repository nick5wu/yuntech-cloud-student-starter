"""一鍵查看雲端上的 W9 照片：找最新事件 → 讀收據 → 授權讀回照片 → 開啟。

用法（專案根目錄）：
  python tools/w09_show.py                 # 最新一筆事件
  python tools/w09_show.py --event-id <ID> # 指定事件
  python tools/w09_show.py --no-open       # 只下載不開啟

權杖從 .local/app.env 讀，不顯示、不經過命令列。只讀：不發事件、不上傳、不改 AWS。
照片存到 .local/w09-check/<事件ID>.jpg。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".local" / "w09-check.json"


def load():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    env = {}
    for line in (ROOT / ".local" / "app.env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v.strip()
    ca = (CONFIG.parent / cfg["ca_file"]).resolve()
    ctx = ssl.create_default_context(cafile=str(ca))
    return cfg["api_base_url"].rstrip("/"), cfg["expected_device_id"], env, ctx


def get(ctx, url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {}, b""
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"連不上雲端（{type(exc).__name__}）。可能是網路換了、IP 不在安全群組內，或 EC2 的位址變了。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event-id")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    base, device, env, ctx = load()
    operator, device_token = env.get("OPERATOR_TOKEN"), env.get("W09_DEVICE_TOKEN")
    if not operator or not device_token:
        sys.exit(".local/app.env 缺少權杖。")

    event_id = a.event_id
    if not event_id:
        status, _, body = get(ctx, f"{base}/events?device_id={device}", operator)
        if status != 200:
            sys.exit(f"讀事件清單失敗：HTTP {status}")
        events = json.loads(body)
        if not events:
            sys.exit("雲端還沒有這個裝置的事件。")
        event_id = events[0]["event_id"]
        print(f"最新事件：{event_id}（{events[0]['type']}，{events[0]['observed_at']}）")

    status, _, body = get(ctx, f"{base}/w09/device/events/{event_id}", device_token)
    if status != 200:
        sys.exit(f"收據查詢失敗：HTTP {status}（404 表示雲端沒有這個事件）")
    receipt = json.loads(body)
    print(f"收據：事件已存={receipt['event_stored']}，照片狀態={receipt['photo_state']}")
    if receipt["photo_state"] != "ready":
        sys.exit("這個事件雲端上還沒有照片。")

    status, headers, photo = get(ctx, f"{base}/w09/events/{event_id}/photo", operator)
    if status != 200:
        sys.exit(f"讀回照片失敗：HTTP {status}")
    digest = hashlib.sha256(photo).hexdigest()
    if digest != receipt["photo_sha256"] or digest != headers.get("X-Content-SHA256"):
        sys.exit("照片摘要與收據不符！")
    out = ROOT / ".local" / "w09-check" / f"{event_id}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(photo)
    print(f"照片已從雲端讀回並核對摘要相符：{len(photo)} bytes → {out}")
    if not a.no_open and hasattr(os, "startfile"):
        os.startfile(str(out))


if __name__ == "__main__":
    main()
