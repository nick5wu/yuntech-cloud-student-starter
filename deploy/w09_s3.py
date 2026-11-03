#!/usr/bin/env python3
"""W9：建立私有 S3 bucket，讓既有 EC2 透過 LabInstanceProfile 存放照片。預設只列計畫，加 --apply 才執行。

做的事：
  1. 建立 bucket（四項 public access block 全開、預設加密 SSE-S3、課程標籤）
  2. 把既有的 LabInstanceProfile 關聯到既有 EC2（不建立或修改 IAM；EC2 本身不重建）
  3. 在 .local/app.env 設定 W09_S3_BUCKET（之後用 deploy/w09_up.py --apply 部署程式）
驗證：public access block 為 true、匿名讀取被拒絕。
回收：--teardown 只刪本 bucket 內 w09/ 前綴的物件與 bucket、解除 instance profile 關聯、移除 env 設定。
"""
import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import lab  # noqa: E402

APP_ENV = ROOT / ".local" / "app.env"
PROFILE = "LabInstanceProfile"
TAGS = [{"Key": "course", "Value": "yuntech-115-1"}, {"Key": "week", "Value": "w09"},
        {"Key": "group", "Value": "g06"}, {"Key": "owner", "Value": "1"}]


def find_instance(region, key_name):
    out = lab.run_aws(["ec2", "describe-instances", "--filters", f"Name=key-name,Values={key_name}",
                       "Name=instance-state-name,Values=running", "--query",
                       "Reservations[].Instances[].InstanceId"], region)
    if len(out) != 1:
        raise SystemExit(f"預期剛好 1 台執行中的主機（key={key_name}），實際 {len(out)} 台。")
    return out[0]


def association(region, instance):
    out = lab.run_aws(["ec2", "describe-iam-instance-profile-associations", "--filters",
                       f"Name=instance-id,Values={instance}", "--query",
                       "IamInstanceProfileAssociations[?State=='associated'].{Id:AssociationId,Arn:IamInstanceProfile.Arn}"],
                      region)
    return out[0] if out else None


def env_lines():
    return APP_ENV.read_text(encoding="utf-8").splitlines() if APP_ENV.exists() else []


def set_env(name, value):
    lines = [l for l in env_lines() if not l.startswith(name + "=")]
    if value is not None:
        lines.append(f"{name}={value}")
    APP_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--teardown", action="store_true")
    ap.add_argument("--key-name", default="w03-g06-1")
    a = ap.parse_args()

    ctx = lab.verify()
    region, account = ctx["region"], ctx["account"]
    bucket = f"yuntech-g06-1-w09-{account}"
    instance = find_instance(region, a.key_name)
    assoc = association(region, instance)

    if a.teardown:
        print(f"[teardown] 刪除 s3://{bucket}/w09/ 內的物件與 bucket；解除 {instance} 的 instance profile；移除 W09_S3_BUCKET")
        if not a.apply:
            print("（dry-run；加 --apply 執行）")
            return
        try:
            keys = lab.run_aws(["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", "w09/",
                                "--query", "Contents[].Key"], region) or []
        except Exception:
            keys = []
        for k in keys:
            lab.run_aws(["s3api", "delete-object", "--bucket", bucket, "--key", k], region)
        try:
            lab.run_aws(["s3api", "delete-bucket", "--bucket", bucket], region)
        except Exception as exc:
            print("delete-bucket:", type(exc).__name__)
        if assoc:
            lab.run_aws(["ec2", "disassociate-iam-instance-profile", "--association-id", assoc["Id"]], region)
        set_env("W09_S3_BUCKET", None)
        print("已回收。需重新部署程式（或移除 W09_S3_BUCKET 後重啟服務）才會回到資料庫存放。")
        return

    plan = [
        f"建立 bucket {bucket}（{region}）：四項 public access block=true、預設加密 AES256、標籤 course/week/group/owner",
        f"把既有的 {PROFILE} 關聯到 EC2 {instance}" + ("（已關聯：" + assoc["Arn"].split("/")[-1] + "，略過）" if assoc else ""),
        f".local/app.env 設定 W09_S3_BUCKET={bucket}",
        "不建立或修改 IAM；不開放任何公開讀取；照片物件鍵 w09/events/<event_id>.jpg",
    ]
    print("計畫：")
    for n, line in enumerate(plan, 1):
        print(f" {n}. {line}")
    if not a.apply:
        print("\n（dry-run；確認後加 --apply 執行）")
        return

    try:
        lab.run_aws(["s3api", "head-bucket", "--bucket", bucket], region)
        print("bucket 已存在，沿用。")
    except Exception:
        lab.run_aws(["s3api", "create-bucket", "--bucket", bucket], region)
        print("已建立 bucket。")
    lab.run_aws(["s3api", "put-public-access-block", "--bucket", bucket, "--public-access-block-configuration",
                 "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"], region)
    lab.run_aws(["s3api", "put-bucket-encryption", "--bucket", bucket, "--server-side-encryption-configuration",
                 json.dumps({"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})], region)
    lab.run_aws(["s3api", "put-bucket-tagging", "--bucket", bucket, "--tagging", json.dumps({"TagSet": TAGS})], region)

    if not assoc:
        lab.run_aws(["ec2", "associate-iam-instance-profile", "--instance-id", instance,
                     "--iam-instance-profile", f"Name={PROFILE}"], region)
        print(f"已把 {PROFILE} 關聯到 {instance}。")
    set_env("W09_S3_BUCKET", bucket)

    block = lab.run_aws(["s3api", "get-public-access-block", "--bucket", bucket, "--query",
                         "PublicAccessBlockConfiguration"], region)
    print("public access block:", block)
    if not all(block.values()):
        raise SystemExit("public access block 未全部開啟！")
    try:
        urllib.request.urlopen(f"https://{bucket}.s3.amazonaws.com/w09/", timeout=10)
        raise SystemExit("匿名讀取竟然成功！請立即檢查 bucket。")
    except urllib.error.HTTPError as exc:
        print(f"匿名讀取被拒絕：HTTP {exc.code}")
    print("完成。接著執行 deploy/w09_up.py --apply 部署使用 S3 的新版程式。")


if __name__ == "__main__":
    main()
