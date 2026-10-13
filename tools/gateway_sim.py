#!/usr/bin/env python3
"""Simulate Gateway sending 32 events for 10 devices, 1 duplicate, and 1 unauthorized device (Spec F)."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import ssl
import sys
import time

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    print("paho-mqtt 2.1.0 required", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Gateway simulator (32 events)")
    parser.add_argument("--code", required=True, help="Student code, e.g. g06-1")
    parser.add_argument("--run", required=True, help="Run identifier, e.g. r1")
    args = parser.parse_args()

    code = args.code
    run_id = args.run
    prefix = f"{code}-{run_id}"

    if not re.fullmatch(r"^[A-Za-z0-9-]{1,40}$", prefix):
        print(f"Invalid prefix: {prefix}. Must match ^[A-Za-z0-9-]{{1,40}}$", file=sys.stderr)
        sys.exit(1)

    iot_dir = ROOT / ".local/iot"
    endpoint_file = iot_dir / "endpoint"
    if not endpoint_file.exists():
        print("Missing .local/iot/endpoint. Run deploy/iot-up.sh first.", file=sys.stderr)
        sys.exit(1)
    endpoint = endpoint_file.read_text(encoding="utf-8").strip()

    client_id = f"gw-{code}"
    topic = f"cxe/{code}/gw/events"
    cert_file = str(iot_dir / "gw.cert.pem")
    key_file = str(iot_dir / "gw.private.key")
    ca_file = str(iot_dir / "AmazonRootCA1.pem")

    state = {
        "connected": False,
        "connack": None,
        "pending_mid": None,
        "puback_received": False,
        "puback_count": 0,
    }

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5
    )

    client.tls_set(
        ca_certs=ca_file,
        certfile=cert_file,
        keyfile=key_file,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT
    )

    def on_connect(c, userdata, flags, reason_code, properties):
        rc = reason_code.value if hasattr(reason_code, "value") else int(reason_code)
        state["connack"] = rc
        state["connected"] = (rc == 0)

    def on_publish(c, userdata, mid, reason_code, properties):
        rc = reason_code.value if hasattr(reason_code, "value") else int(reason_code)
        if mid == state["pending_mid"]:
            state["puback_received"] = True
            if rc == 0:
                state["puback_count"] += 1

    client.on_connect = on_connect
    client.on_publish = on_publish

    try:
        client.connect(endpoint, 8883, keepalive=60)
    except Exception as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    client.loop_start()

    conn_start = time.time()
    while not state["connected"] and time.time() - conn_start < 10:
        time.sleep(0.1)

    if not state["connected"]:
        print(f"Failed to connect: connack={state['connack']}", file=sys.stderr)
        client.loop_stop()
        sys.exit(1)

    # Prepare 32 messages
    messages = []
    # 10 devices x 3 messages
    for d_idx in range(1, 11):
        dev_id = f"{code}-d{d_idx:02d}"
        for s_idx in range(1, 4):
            ev_id = f"{prefix}-d{d_idx:02d}-{s_idx:02d}"
            now_str = datetime.now(timezone.utc).astimezone().isoformat()
            messages.append({
                "event_id": ev_id,
                "device_id": dev_id,
                "observed_at": now_str,
                "type": "status"
            })

    # 31st: duplicate of first message (identical event_id, device_id, observed_at, type)
    messages.append(dict(messages[0]))

    # 32nd: unauthorized device d99
    now_str = datetime.now(timezone.utc).astimezone().isoformat()
    messages.append({
        "event_id": f"{prefix}-d99-01",
        "device_id": f"{code}-d99",
        "observed_at": now_str,
        "type": "status"
    })

    print(f"Publishing 32 messages to topic '{topic}' with prefix '{prefix}'...")
    for idx, msg in enumerate(messages, 1):
        state["puback_received"] = False
        payload = json.dumps(msg)
        info = client.publish(topic, payload=payload, qos=1)
        state["pending_mid"] = info.mid

        # Wait for PUBACK
        wait_start = time.time()
        while not state["puback_received"] and time.time() - wait_start < 5:
            time.sleep(0.05)

        if not state["puback_received"]:
            print(f"Warning: Timed out waiting for PUBACK for message {idx} ({msg['event_id']})")

    client.loop_stop()
    client.disconnect()

    puback_ok = state["puback_count"]
    print(f"puback_ok={puback_ok}/32")

    last_run_file = ROOT / ".local/w06-last-run.json"
    last_run_file.parent.mkdir(parents=True, exist_ok=True)
    last_run_file.write_text(json.dumps({
        "prefix": prefix,
        "code": code,
        "run": run_id,
        "puback_ok": puback_ok,
        "total": len(messages),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
