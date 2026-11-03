#!/usr/bin/env python3
"""W9：把照片 API 與 HTTPS 入口部署到既有的 W3 EC2。預設只列出計畫，加 --apply 才會執行。

會做的事（逐項列在 plan 輸出）：
  1. 在 .local/app.env 補 W09_DEVICE_TOKEN／W09_DEVICE_ID，並把裝置 ID 加進 IOT_DEVICE_IDS（consumer 白名單）
  2. 以既有 deploy_runner 部署已 commit 的程式並重啟服務（含 consumer）
  3. 上傳 HTTPS 憑證到 /etc/inspection/tls，放入 nginx 的 443 設定並 reload
  4. 安全群組新增 TCP 443，來源只允許你指定的 /32（不開 0.0.0.0/0）
  5. 用 CA 驗證 HTTPS 並檢查 /health

不會做：不建立或修改 IAM、不建 S3／NAT／公開 DB、不新增 EC2。
回收：--teardown 移除 443 規則、conf.d 設定與 tls 目錄（程式與資料保留）。
"""
import argparse
import ipaddress
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy"))
from scripts import lab  # noqa: E402
from deploy_runner import run_ssh  # noqa: E402

APP_ENV = ROOT / ".local" / "app.env"
TLS_DIR = ROOT / ".local" / "w09-tls"
DEFAULT_IDS = [f"g06-1-d{i:02d}" for i in range(1, 11)]


def read_env():
    lines = APP_ENV.read_text(encoding="utf-8").splitlines() if APP_ENV.exists() else []
    return {l.split("=", 1)[0]: l.split("=", 1)[1] for l in lines if "=" in l}


def write_env(values):
    APP_ENV.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    try:
        APP_ENV.chmod(0o600)
    except OSError:
        pass


def find_instance(region, key_name):
    out = lab.run_aws(["ec2", "describe-instances", "--filters", f"Name=key-name,Values={key_name}",
                       "Name=instance-state-name,Values=running", "--query",
                       "Reservations[].Instances[].{Id:InstanceId,Ip:PublicIpAddress,Dns:PublicDnsName,SG:SecurityGroups[0].GroupId}"],
                      region)
    if len(out) != 1:
        raise SystemExit(f"預期剛好 1 台執行中的主機（key={key_name}），實際 {len(out)} 台；請確認後再執行。")
    return out[0]


def check_cidrs(cidrs):
    for c in cidrs:
        net = ipaddress.ip_network(c, strict=True)
        if net.version != 4 or net.prefixlen != 32:
            raise SystemExit(f"{c} 不是單一 IPv4 /32；本課不開放更大範圍。")


def sg_has_443(region, sg, cidr):
    perms = lab.run_aws(["ec2", "describe-security-groups", "--group-ids", sg, "--query",
                         "SecurityGroups[0].IpPermissions[?FromPort==`443`].IpRanges[].CidrIp"], region)
    return cidr in perms


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--teardown", action="store_true")
    ap.add_argument("--allow", action="append", default=[], metavar="IP/32", help="允許連 443 的來源，可重複")
    ap.add_argument("--allow-admin", action="append", default=[], metavar="IP/32",
                    help="為目前這台電腦新增 TCP 22 與 80 的來源（IP 變動時 SSH 才連得上），可重複")
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--device-id", default="g06-esp32-s3-cam")
    ap.add_argument("--key-name", default="w03-g06-1")
    a = ap.parse_args()
    check_cidrs(a.allow + a.allow_admin)

    ctx = lab.verify()
    region = ctx["region"]
    inst = find_instance(region, a.key_name)
    host_ip, dns, sg = inst["Ip"], inst["Dns"], inst["SG"]
    key_pem = str(Path.home() / ".ssh" / a.key_name)

    if a.teardown:
        print(f"[teardown] 主機 {inst['Id']}：移除 SG {sg} 的 TCP 443 規則、nginx w09 設定、/etc/inspection/tls")
        if not a.apply:
            print("（dry-run；加 --apply 執行）")
            return
        existing = lab.run_aws(["ec2", "describe-security-groups", "--group-ids", sg, "--query",
                                "SecurityGroups[0].IpPermissions[?FromPort==`443`].IpRanges[].CidrIp"], region)
        for cidr in existing:
            lab.run_aws(["ec2", "revoke-security-group-ingress", "--group-id", sg, "--protocol", "tcp",
                         "--port", "443", "--cidr", cidr], region)
        run_ssh(host_ip, "sudo rm -f /etc/nginx/conf.d/w09-tls.conf && sudo rm -rf /etc/inspection/tls && sudo nginx -t && sudo systemctl reload nginx",
                key_pem)
        print("已回收 443 入口。")
        return

    if not a.allow:
        raise SystemExit("請至少用 --allow <你的公網IP>/32 指定一個來源。")
    for f in ("ca.pem", "server.crt", "server.key"):
        if not (TLS_DIR / f).exists():
            raise SystemExit(f"缺少 {f}；先執行 python deploy/w09_tls.py --host {dns}")
    san = subprocess.run(["openssl", "x509", "-in", "server.crt", "-noout", "-ext", "subjectAltName"],
                         cwd=TLS_DIR, capture_output=True, text=True).stdout
    if dns not in san:
        raise SystemExit(f"伺服器憑證的 SAN 不含目前主機名 {dns}（EC2 重啟後 DNS 會變）；請重新執行 w09_tls.py。")

    env = read_env()
    ids = [i for i in env.get("IOT_DEVICE_IDS", ",".join(DEFAULT_IDS)).split(",") if i]
    plan = [
        f"主機 {inst['Id']}（{host_ip}，{dns}），安全群組 {sg}",
        *([f"安全群組新增 TCP 22 與 80，來源：{', '.join(a.allow_admin)}（讓這台電腦能 SSH／連 HTTP）"] if a.allow_admin else []),
        ".local/app.env：新增 W09_DEVICE_TOKEN（隨機產生，不顯示）、W09_DEVICE_ID=" + a.device_id,
        f"IOT_DEVICE_IDS 白名單{'已含' if a.device_id in ids else '加入'} {a.device_id}",
        f"以 deploy_runner 部署 commit {a.commit}（重啟 inspection 與 inspection-consumer）",
        "上傳 /etc/inspection/tls/{server.crt,server.key}（root 擁有，0600/0644）與 /etc/nginx/conf.d/w09-tls.conf，nginx -t 後 reload",
        "安全群組新增 TCP 443，來源：" + ", ".join(a.allow) + "（22／80 規則不變）",
        "驗證：用自建 CA 連 https://" + dns + "/health，檢查 w09_configured=true",
    ]
    print("計畫：")
    for n, line in enumerate(plan, 1):
        print(f" {n}. {line}")
    if not a.apply:
        print("\n（dry-run；確認後加 --apply 執行）")
        return

    # 0. 先讓這台電腦連得上（SSH 22／HTTP 80），僅限指定的 /32
    for cidr in a.allow_admin:
        for port in (22, 80):
            have = lab.run_aws(["ec2", "describe-security-groups", "--group-ids", sg, "--query",
                                f"SecurityGroups[0].IpPermissions[?FromPort==`{port}`].IpRanges[].CidrIp"], region)
            if cidr in have:
                continue
            lab.run_aws(["ec2", "authorize-security-group-ingress", "--group-id", sg, "--protocol", "tcp",
                         "--port", str(port), "--cidr", cidr], region)
            print(f"已開放 TCP {port}：{cidr}")

    # 1. 權杖與白名單
    env.setdefault("REPORTER_TOKEN", secrets.token_urlsafe(32))
    env.setdefault("OPERATOR_TOKEN", secrets.token_urlsafe(32))
    env.setdefault("W09_DEVICE_TOKEN", secrets.token_urlsafe(32))
    env["W09_DEVICE_ID"] = a.device_id
    if a.device_id not in ids:
        ids.append(a.device_id)
    env["IOT_DEVICE_IDS"] = ",".join(ids)
    write_env(env)

    # 2. 程式（要先 commit）
    print("部署程式…")
    subprocess.check_call([sys.executable, str(ROOT / "deploy" / "deploy_runner.py"), a.commit, "--host", host_ip, "--key", key_pem],
                          cwd=ROOT)

    # 3. TLS + nginx
    print("設定 HTTPS…")
    run_ssh(host_ip, "sudo install -d -o root -g root -m 755 /etc/inspection/tls", key_pem)
    for name, mode in (("server.key", "600"), ("server.crt", "644")):
        run_ssh(host_ip, f"sudo bash -c 'umask 077; cat > /etc/inspection/tls/{name} && chown root:root /etc/inspection/tls/{name} && chmod {mode} /etc/inspection/tls/{name}'",
                key_pem, stdin_data=(TLS_DIR / name).read_bytes())
    run_ssh(host_ip, "sudo bash -c 'cat > /etc/nginx/conf.d/w09-tls.conf && chmod 644 /etc/nginx/conf.d/w09-tls.conf'",
            key_pem, stdin_data=(ROOT / "deploy" / "nginx-w09-tls.conf").read_bytes())
    rc, out = run_ssh(host_ip, "sudo nginx -t 2>&1 && sudo systemctl reload nginx && echo reloaded", key_pem)
    print(out.splitlines()[-1] if out else "")
    if rc != 0:
        raise SystemExit("nginx 設定檢查失敗；已停止，443 尚未對外開放。")

    # 4. 安全群組
    for cidr in a.allow:
        if sg_has_443(region, sg, cidr):
            print(f"443 規則已存在：{cidr}")
            continue
        lab.run_aws(["ec2", "authorize-security-group-ingress", "--group-id", sg, "--protocol", "tcp",
                     "--port", "443", "--cidr", cidr], region)
        print(f"已開放 TCP 443：{cidr}")

    # 5. 驗證
    ctx_tls = ssl.create_default_context(cafile=str(TLS_DIR / "ca.pem"))
    try:
        with urllib.request.urlopen(f"https://{dns}/health", context=ctx_tls, timeout=10) as resp:
            body = json.loads(resp.read())
        print(f"HTTPS 驗證成功：status={body.get('status')} w09_configured={body.get('w09_configured')} db_configured={body.get('db_configured')}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"HTTPS 驗證失敗：{type(exc).__name__}（若你的 IP 不在 --allow 內會逾時）")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
