#!/usr/bin/env python3
"""W4 Rejection Matrix runner (7 rows)"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def run_matrix(base_url, reporter_token, operator_token):
    headers_base = {"Content-Type": "application/json"}
    valid_event = {
        "event_id": "g06-1-matrix-01",
        "device_id": "g06-d01",
        "observed_at": "2026-10-13T12:00:00+08:00",
        "type": "status",
        "note": "matrix test"
    }

    def req(method, path, body=None, token=None):
        headers = dict(headers_base)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, raw
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            return err.code, raw

    tests = [
        ("1", "POST", "/events", valid_event, reporter_token, 201),
        ("2", "POST", "/events", valid_event, None, 401),
        ("3", "POST", "/events", valid_event, operator_token, 403),
        ("4", "POST", "/events", {**valid_event, "event_id": "g06-1-matrix-04", "observed_at": "2026-10-13T12:00:00"}, reporter_token, 400),
        ("5", "POST", "/events", valid_event, reporter_token, 200),  # In W5+ idempotency identical replay is 200
        ("6", "GET", "/events", None, reporter_token, 403),
        ("7", "GET", "/events", None, operator_token, 200),
    ]

    print("=== W4 Rejection Matrix ===")
    for num, method, path, body, token, expected in tests:
        code, resp = req(method, path, body, token)
        ok = "PASS" if code == expected else f"FAIL (expected {expected})"
        print(f"#{num} {method} {path} -> HTTP {code} [{ok}]")
        print(f"   Response: {resp}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--reporter", default=os.environ.get("REPORTER_TOKEN"))
    parser.add_argument("--operator", default=os.environ.get("OPERATOR_TOKEN"))
    args = parser.parse_args()
    if not (args.reporter and args.operator):
        print("Set REPORTER_TOKEN and OPERATOR_TOKEN env or use --reporter / --operator", file=sys.stderr)
        sys.exit(1)
    run_matrix(args.url.rstrip("/"), args.reporter, args.operator)
