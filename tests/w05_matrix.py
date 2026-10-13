#!/usr/bin/env python3
"""W5 Idempotency Matrix runner (5 rows)"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def run_matrix(host_ip, reporter_token, operator_token, ssh_key=None, ssh_user="ec2-user"):
    base_url = f"http://{host_ip}"
    event_id = "g06-1-w05-001"
    event_1 = {
        "event_id": event_id,
        "device_id": "g06-d01",
        "observed_at": "2026-10-13T12:00:00+08:00",
        "type": "status",
        "note": "w05 idempotency initial"
    }

    def req(method, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8")

    def run_ssh(cmd):
        ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
        if ssh_key:
            ssh_cmd.extend(["-i", ssh_key])
        ssh_cmd.extend([f"{ssh_user}@{host_ip}", cmd])
        res = subprocess.run(ssh_cmd, capture_output=True, text=True, check=False)
        return res.stdout.strip()

    # Step 0: check health
    health_code, health_body = req("GET", "/health")
    print(f"Health check: HTTP {health_code} -> {health_body}")

    # Row 1: New event -> 201
    c1, b1 = req("POST", "/events", event_1, reporter_token)
    print(f"#1 POST new event -> HTTP {c1} {b1}")

    # Row 2: Replay identical event -> 200
    c2, b2 = req("POST", "/events", event_1, reporter_token)
    print(f"#2 POST identical replay -> HTTP {c2} {b2}")

    # Row 3: Same ID, different note -> 409
    event_diff = dict(event_1)
    event_diff["note"] = "conflicted note"
    c3, b3 = req("POST", "/events", event_diff, reporter_token)
    print(f"#3 POST conflicted content -> HTTP {c3} {b3}")

    # Row 4: Restart inspection service and query #1 -> 200
    print("Restarting inspection service on EC2...")
    run_ssh("sudo systemctl restart inspection")
    c4, b4 = req("GET", f"/events/{event_id}", token=operator_token)
    print(f"#4 GET after service restart -> HTTP {c4} {b4}")

    # Row 5: psql count on EC2 -> 1
    print("Querying PostgreSQL count via psql on EC2...")
    psql_query = f"""sudo bash -c 'set -a; . /etc/inspection/app.env; set +a; PGPASSWORD="$DB_PASSWORD" psql "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" -v event_id="{event_id}" -t -A -c "SELECT count(*) FROM events WHERE event_id = :'"'event_id'"';"'"""
    count = run_ssh(psql_query)
    print(f"#5 psql row count for {event_id}: {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="EC2 Public IP")
    parser.add_argument("--reporter", default=os.environ.get("REPORTER_TOKEN"))
    parser.add_argument("--operator", default=os.environ.get("OPERATOR_TOKEN"))
    parser.add_argument("--key", help="Path to SSH private key")
    args = parser.parse_args()
    run_matrix(args.host, args.reporter, args.operator, args.key)
