#!/usr/bin/env python3
"""W6 Reconcile verification script (Spec G)."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def run_ssh(host_ip, cmd, key_path=None, user="ec2-user"):
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
    if key_path:
        args.extend(["-i", str(key_path)])
    args.extend([f"{user}@{host_ip}", cmd])
    res = subprocess.run(args, capture_output=True, text=True, check=False)
    return res.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description="W6 Reconciliation check")
    parser.add_argument("--code", required=True, help="Student code, e.g. g06-1")
    parser.add_argument("--run", required=True, help="Run identifier, e.g. r1")
    parser.add_argument("--host", help="Host IP override (default from .local/resources.json)")
    parser.add_argument("--key", help="SSH private key path override")
    args = parser.parse_args()

    code = args.code
    run_id = args.run
    prefix = f"{code}-{run_id}"

    if not re.fullmatch(r"^[A-Za-z0-9-]{1,40}$", prefix):
        print(f"Invalid prefix: {prefix}", file=sys.stderr)
        sys.exit(1)

    resources_file = ROOT / ".local/resources.json"
    host_ip = args.host
    if not host_ip and resources_file.exists():
        res_json = json.loads(resources_file.read_text(encoding="utf-8"))
        host_ip = res_json.get("public_ip")
    if not host_ip:
        print("Error: Missing host IP. Provide via --host or check .local/resources.json", file=sys.stderr)
        sys.exit(1)

    key_path = args.key
    if not key_path:
        # Check ~/.ssh/w03-<code/owner>
        ssh_dir = Path.home() / ".ssh"
        candidates = list(ssh_dir.glob("w03*")) + list(ssh_dir.glob("id_*"))
        private_keys = [c for c in candidates if not str(c).endswith(".pub")]
        if private_keys:
            key_path = str(private_keys[0])

    # 1. Host /health
    try:
        with urllib.request.urlopen(f"http://{host_ip}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Failed to query http://{host_ip}/health: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"version={health.get('version')}")
    print(f"db_configured={health.get('db_configured')}")
    print(f"prefix={prefix}")

    # 2. Read PUBACK from .local/w06-last-run.json
    last_run_file = ROOT / ".local/w06-last-run.json"
    puback_count = "unknown"
    if last_run_file.exists():
        run_data = json.loads(last_run_file.read_text(encoding="utf-8"))
        if run_data.get("prefix") == prefix:
            puback_count = run_data.get("puback_ok")
    print(f"puback_count={puback_count}")

    # 3. Consumer counts and database row count via SSH
    print("Waiting for consumer processing...")
    counts = {"created": 0, "duplicate": 0, "rejected": 0, "error": 0}
    journal_cmd = f"sudo journalctl -u inspection-consumer -o cat --no-pager | grep -F 'event_id={prefix}-' | grep -o 'result=[a-z]*' | sort | uniq -c"

    start_wait = time.time()
    while time.time() - start_wait < 60:
        out = run_ssh(host_ip, journal_cmd, key_path)
        counts = {"created": 0, "duplicate": 0, "rejected": 0, "error": 0}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) == 2:
                n, res = int(parts[0]), parts[1].replace("result=", "")
                if res in counts:
                    counts[res] = n
        if sum(counts.values()) >= 32:
            break
        time.sleep(5)

    print(f"consumer_counts: created={counts['created']} duplicate={counts['duplicate']} rejected={counts['rejected']} error={counts['error']}")

    # Database count via PostgreSQL
    db_query_cmd = f"sudo python3 -c \"import psycopg2; env = dict(l.strip().split('=', 1) for l in open('/etc/inspection/app.env') if '=' in l); conn = psycopg2.connect(host=env['DB_HOST'], user=env['DB_USER'], password=env['DB_PASSWORD'], dbname=env['DB_NAME'], sslmode='verify-full', sslrootcert='/etc/inspection/rds-ca.pem'); cur = conn.cursor(); cur.execute('SELECT count(*) FROM events WHERE event_id LIKE %s', ('{prefix}-%',)); print(cur.fetchone()[0])\""
    db_count = run_ssh(host_ip, db_query_cmd, key_path)
    print(f"db_count={db_count}")

    # 4. Filter query: GET /events?device_id=<代號>-d01
    operator_token = None
    app_env_file = ROOT / ".local/app.env"
    if app_env_file.exists():
        for line in app_env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPERATOR_TOKEN="):
                operator_token = line.split("=", 1)[1].strip()

    filtered_events = []
    if operator_token:
        filter_dev = f"{code}-d01"
        try:
            req = urllib.request.Request(
                f"http://{host_ip}/events?device_id={filter_dev}",
                headers={"Authorization": f"Bearer {operator_token}"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                filtered_all = json.loads(resp.read().decode("utf-8"))
                filtered_events = [e for e in filtered_all if e.get("event_id", "").startswith(f"{prefix}-")]
        except Exception as exc:
            print(f"Failed to query filtered events: {exc}", file=sys.stderr)

    print(f"filtered_count={len(filtered_events)}")
    for ev in filtered_events:
        print(f"  event_id={ev.get('event_id')} received_at={ev.get('received_at')}")


if __name__ == "__main__":
    main()
