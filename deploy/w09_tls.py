#!/usr/bin/env python3
"""W9：在本機產生自建 CA 與 EC2 的 HTTPS 伺服器憑證（不碰 AWS）。

- CA 只建一次並重複使用：板子裝一次 ca.pem，之後換伺服器憑證不用重寫板子。
- 伺服器憑證的 SAN 是連線時使用的主機名（EC2 公開 DNS）；EC2 停止再啟動後 DNS 會變，需重新簽發。
- 私鑰（ca.key、server.key）留在 .local/w09-tls/（已 gitignore），不印出。
- 板子與檢查工具信任的是 ca.pem（公開憑證）。
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".local" / "w09-tls"
HOST_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


def openssl(*args, cwd):
    # 只用相對檔名並把 cwd 設在輸出目錄，避免專案路徑含中文時 openssl 讀不到檔案
    result = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("openssl 失敗：" + " ".join(args[:2]) + "\n" + result.stderr.strip()[:400])
    return result.stdout


def ensure_ca(days):
    ca_key, ca_pem = OUT / "ca.key", OUT / "ca.pem"
    if ca_key.exists() and ca_pem.exists():
        print("CA 已存在，沿用（板子不用重新寫入 CA）。")
        return
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
            "-days", str(days), "-sha256", "-subj", "/CN=yuntech-g06-w09-ca",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=OUT)
    print("已建立 CA：ca.pem（公開）、ca.key（私鑰，留在本機）。")


def issue_server(host, days):
    if not HOST_RE.fullmatch(host):
        raise SystemExit("主機名格式不符。")
    ext = f"subjectAltName=DNS:{host}\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
    ext_path = OUT / "server.ext"
    ext_path.write_text(ext, encoding="ascii")
    try:
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr",
                "-subj", f"/CN={host[:60]}", cwd=OUT)
        openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                "-out", "server.crt", "-days", str(days), "-sha256", "-extfile", "server.ext", cwd=OUT)
    finally:
        ext_path.unlink(missing_ok=True)
        (OUT / "server.csr").unlink(missing_ok=True)
    for f in ("ca.key", "server.key"):
        try:
            os.chmod(OUT / f, 0o600)
        except OSError:
            pass


def verify(host):
    out = openssl("verify", "-CAfile", "ca.pem", "-verify_hostname", host, "server.crt", cwd=OUT).strip()
    san = openssl("x509", "-in", "server.crt", "-noout", "-ext", "subjectAltName", cwd=OUT)
    dates = openssl("x509", "-in", "server.crt", "-noout", "-dates", cwd=OUT).strip().replace("\n", "；")
    print(out)
    print(san.strip().replace("\n", " "))
    print(dates)
    if "OK" not in out:
        raise SystemExit("憑證驗證失敗")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True, help="連線用主機名，例如 ec2-1-2-3-4.compute-1.amazonaws.com")
    ap.add_argument("--days", type=int, default=365)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    ensure_ca(a.days)
    issue_server(a.host, a.days)
    verify(a.host)
    print("完成。給板子與檢查工具的 CA：" + str(OUT / "ca.pem"))


if __name__ == "__main__":
    main()
