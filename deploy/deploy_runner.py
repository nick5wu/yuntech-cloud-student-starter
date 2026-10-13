#!/usr/bin/env python3
"""Deploy committed application, secrets and IoT credentials to EC2 host (Spec E)."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def run_ssh(host_ip, remote_cmd, key_path=None, stdin_data=None, user="ec2-user"):
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
    if key_path:
        args.extend(["-i", str(key_path)])
    args.extend([f"{user}@{host_ip}", remote_cmd])
    res = subprocess.run(args, input=stdin_data, text=True, capture_output=True, check=False)
    if res.returncode != 0:
        print(f"SSH command failed (code {res.returncode}): {res.stderr.strip()}", file=sys.stderr)
    return res.returncode, res.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description="Deploy to EC2 host")
    parser.add_argument("commit", nargs="?", default="HEAD", help="Commit to deploy")
    parser.add_argument("--host", help="Host IP override (default read from .local/resources.json)")
    parser.add_argument("--key", help="SSH private key override")
    parser.add_argument("--code", help="Student code override (e.g. g06-1)")
    args = parser.parse_args()

    # 1. Resolve commit
    try:
        commit_sha = subprocess.check_output(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{args.commit}^{{commit}}"],
            cwd=ROOT, text=True
        ).strip()
    except subprocess.CalledProcessError as exc:
        print(f"Invalid commit: {args.commit}", file=sys.stderr)
        sys.exit(1)

    # 2. Resolve host
    resources_file = ROOT / ".local/resources.json"
    resources = {}
    if resources_file.exists():
        resources = json.loads(resources_file.read_text(encoding="utf-8"))
    host_ip = args.host or resources.get("public_ip")
    if not host_ip:
        print("Missing host IP in .local/resources.json. Specify via --host.", file=sys.stderr)
        sys.exit(1)

    # 3. Resolve key
    key_path = args.key
    if not key_path:
        ssh_dir = Path.home() / ".ssh"
        candidates = list(ssh_dir.glob("w03*")) + list(ssh_dir.glob("id_*"))
        priv = [c for c in candidates if not str(c).endswith(".pub")]
        if priv:
            key_path = str(priv[0])

    # 4. Resolve code
    config_file = ROOT / ".local/w03-config.json"
    code = args.code
    if not code and config_file.exists():
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
        code = f"{cfg.get('group', 'g06')}-{cfg.get('owner', '1')}"
    if not code:
        code = "g06-1"

    print(f"Target host: {host_ip}")
    print(f"Deploying commit: {commit_sha}")
    print(f"Student code: {code}")

    # 5. Prepare local app.env
    app_env_file = ROOT / ".local/app.env"
    if not app_env_file.exists():
        print("Generating .local/app.env...")
        rep_token = secrets.token_urlsafe(32)
        op_token = secrets.token_urlsafe(32)
        app_env_file.write_text(f"REPORTER_TOKEN={rep_token}\nOPERATOR_TOKEN={op_token}\n", encoding="utf-8")
        try:
            app_env_file.chmod(0o600)
        except Exception:
            pass

    env_lines = app_env_file.read_text(encoding="utf-8").splitlines()

    # Merge db.env if present
    db_env_file = ROOT / ".local/db.env"
    if db_env_file.exists():
        for line in db_env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not any(l.startswith(line.split("=")[0] + "=") for l in env_lines):
                env_lines.append(line)
        db_host = resources.get("db_host")
        if db_host and not any(l.startswith("DB_HOST=") for l in env_lines):
            env_lines.append(f"DB_HOST={db_host}")
            env_lines.append("DB_PORT=5432")

    # Merge IoT settings if present
    iot_dir = ROOT / ".local/iot"
    endpoint_file = iot_dir / "endpoint"
    if endpoint_file.exists():
        ep = endpoint_file.read_text(encoding="utf-8").strip()
        iot_vars = {
            "IOT_ENDPOINT": ep,
            "IOT_TOPIC": f"cxe/{code}/gw/events",
            "IOT_CLIENT_ID": f"consumer-{code}",
            "IOT_DEVICE_IDS": ",".join([f"{code}-d{i:02d}" for i in range(1, 11)]),
            "IOT_CA": "/etc/inspection/iot/AmazonRootCA1.pem",
            "IOT_CERT": "/etc/inspection/iot/consumer.cert.pem",
            "IOT_KEY": "/etc/inspection/iot/consumer.private.key",
        }
        for k, v in iot_vars.items():
            if not any(l.startswith(f"{k}=") for l in env_lines):
                env_lines.append(f"{k}={v}")

    full_app_env = "\n".join(env_lines) + "\n"

    # 6. Build user-data package
    tmp_ud_path = Path(tempfile.gettempdir()) / f"user-data-{os.getpid()}-{int(time.time())}.sh"
    if tmp_ud_path.exists():
        tmp_ud_path.unlink()
    try:
        subprocess.check_call([sys.executable, str(ROOT / "deploy/make_user_data.py"), commit_sha, str(tmp_ud_path)], cwd=ROOT)
        ud_content = tmp_ud_path.read_text(encoding="utf-8")
    finally:
        if tmp_ud_path.exists():
            tmp_ud_path.unlink()

    # 7. Execute on remote host via SSH
    print("Executing package installation on EC2...")
    code_rc, out = run_ssh(host_ip, "sudo bash -s", key_path, stdin_data=ud_content)
    if code_rc != 0:
        print(f"Package execution error: {out}", file=sys.stderr)
        sys.exit(1)

    # 8. Setup /etc/inspection/iot and upload consumer certs
    if (iot_dir / "consumer.cert.pem").exists() and (iot_dir / "consumer.private.key").exists():
        print("Uploading IoT credentials to /etc/inspection/iot...")
        setup_iot_cmd = "sudo install -d -o inspection -g inspection -m 700 /etc/inspection/iot"
        run_ssh(host_ip, setup_iot_cmd, key_path)

        for fname in ("AmazonRootCA1.pem", "consumer.cert.pem", "consumer.private.key"):
            local_f = iot_dir / fname
            if local_f.exists():
                f_data = local_f.read_text(encoding="utf-8")
                upload_cmd = f"sudo bash -c 'umask 077; cat > /etc/inspection/iot/{fname} && chown inspection:inspection /etc/inspection/iot/{fname} && chmod 600 /etc/inspection/iot/{fname}'"
                run_ssh(host_ip, upload_cmd, key_path, stdin_data=f_data)

    # 9. Upload /etc/inspection/app.env and set permissions
    print("Uploading /etc/inspection/app.env...")
    upload_env_cmd = "sudo bash -c 'cat > /etc/inspection/app.env && chown root:root /etc/inspection/app.env && chmod 600 /etc/inspection/app.env && chmod 755 /etc/inspection'"
    run_ssh(host_ip, upload_env_cmd, key_path, stdin_data=full_app_env)

    # 10. Restart services
    print("Restarting services on EC2...")
    restart_cmd = "sudo systemctl restart inspection && (systemctl is-enabled inspection-consumer >/dev/null 2>&1 && sudo systemctl restart inspection-consumer || true); systemctl is-active inspection; systemctl is-active inspection-consumer || true"
    _, restart_out = run_ssh(host_ip, restart_cmd, key_path)
    print(restart_out)

    # 11. Verify health
    print("Verifying /health...")
    time.sleep(2)
    try:
        with urllib.request.urlopen(f"http://{host_ip}/health", timeout=5) as resp:
            health_json = json.loads(resp.read().decode("utf-8"))
            print(f"/health response: {json.dumps(health_json, indent=2)}")
            if health_json.get("version") == commit_sha:
                print(f"Deployment SUCCESSFUL! Version matches commit {commit_sha[:8]}.")
            else:
                print(f"Warning: Deployed version {health_json.get('version')} differs from {commit_sha}")
    except Exception as exc:
        print(f"Health check query failed: {exc}")


if __name__ == "__main__":
    main()
