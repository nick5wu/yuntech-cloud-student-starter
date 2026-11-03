#!/usr/bin/env python3
"""W9：為 ESP32-S3-CAM 建立專用 IoT 身分（1 個政策 + 1 張憑證）。

- 政策只允許：Connect 到 client <device_id>、Publish 到本組事件 topic；沒有萬用字元。
- 私鑰只在記憶體中處理並寫入 .local/iot/（已 gitignore），不印出。
- 可重複執行：已建立就不重建。回收用 --destroy，只處理 resources.json 內記錄的 ID。
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import lab  # noqa: E402

RES = ROOT / ".local" / "resources.json"
IOT_DIR = ROOT / ".local" / "iot"


def load():
    return json.loads(RES.read_text(encoding="utf-8")) if RES.exists() else {}


def save(data):
    RES.parent.mkdir(parents=True, exist_ok=True)
    RES.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def up(code, device_id):
    ctx = lab.verify()
    account, region = ctx["account"], ctx["region"]
    topic = f"cxe/{code}/gw/events"
    policy_name = f"cxe-{code}-w09-cam"
    doc = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "iot:Connect",
         "Resource": f"arn:aws:iot:{region}:{account}:client/{device_id}"},
        {"Effect": "Allow", "Action": "iot:Publish",
         "Resource": f"arn:aws:iot:{region}:{account}:topic/{topic}"}]}
    res = load()

    try:
        lab.run_aws(["iot", "get-policy", "--policy-name", policy_name], region)
        print(f"政策已存在：{policy_name}")
    except Exception:
        print(f"建立政策：{policy_name}")
        lab.run_aws(["iot", "create-policy", "--policy-name", policy_name,
                     "--policy-document", json.dumps(doc)], region)
    res["iot_cam_policy"] = policy_name
    save(res)

    cert_file, key_file = IOT_DIR / "cam.cert.pem", IOT_DIR / "cam.private.key"
    if res.get("iot_cam_cert_id") and cert_file.exists() and key_file.exists():
        print(f"憑證已存在：…{res['iot_cam_cert_id'][-6:]}")
    else:
        IOT_DIR.mkdir(parents=True, exist_ok=True)
        print("建立憑證與金鑰（輸出不顯示）…")
        out = lab.run_aws(["iot", "create-keys-and-certificate", "--set-as-active"], region)
        cert_file.write_text(out["certificatePem"], encoding="utf-8")
        key_file.write_text(out["keyPair"]["PrivateKey"], encoding="utf-8")
        for f in (cert_file, key_file):
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
        res["iot_cam_cert_id"] = out["certificateId"]
        res["iot_cam_cert_arn"] = out["certificateArn"]
        save(res)  # 先記錄 ID，附加政策失敗時仍可回收
        print("附加政策…")
        lab.run_aws(["iot", "attach-policy", "--policy-name", policy_name,
                     "--target", out["certificateArn"]], region)

    desc = lab.run_aws(["iot", "describe-certificate", "--certificate-id", res["iot_cam_cert_id"]], region)
    status = desc["certificateDescription"]["status"]
    attached = lab.run_aws(["iot", "list-attached-policies", "--target", res["iot_cam_cert_arn"],
                            "--query", "policies[].policyName"], region)
    print(f"憑證狀態：{status}；附加政策：{attached}")
    if status != "ACTIVE" or policy_name not in attached:
        raise SystemExit("驗證失敗：憑證不是 ACTIVE 或政策未附加")
    print(f"完成。device_id/client ID={device_id}，topic={topic}")


def destroy():
    ctx = lab.verify()
    region = ctx["region"]
    res = load()
    cid, arn, pol = res.get("iot_cam_cert_id"), res.get("iot_cam_cert_arn"), res.get("iot_cam_policy")
    if arn and pol:
        try:
            lab.run_aws(["iot", "detach-policy", "--policy-name", pol, "--target", arn], region)
        except Exception as exc:
            print("detach:", type(exc).__name__)
    if cid:
        lab.run_aws(["iot", "update-certificate", "--certificate-id", cid, "--new-status", "INACTIVE"], region)
        lab.run_aws(["iot", "delete-certificate", "--certificate-id", cid], region)
    if pol:
        lab.run_aws(["iot", "delete-policy", "--policy-name", pol], region)
    for k in ("iot_cam_cert_id", "iot_cam_cert_arn", "iot_cam_policy"):
        res.pop(k, None)
    save(res)
    for f in (IOT_DIR / "cam.cert.pem", IOT_DIR / "cam.private.key"):
        f.unlink(missing_ok=True)
    print("已回收板子專用的 IoT 政策與憑證。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", default="g06-1")
    ap.add_argument("--device-id", default="g06-esp32-s3-cam")
    ap.add_argument("--destroy", action="store_true")
    a = ap.parse_args()
    destroy() if a.destroy else up(a.code, a.device_id)
