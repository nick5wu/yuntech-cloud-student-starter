#!/usr/bin/env python3
"""MQTT 5 connectivity checker for gateway and consumer identities (W6 Spec B)."""
import argparse
import json
from pathlib import Path
import ssl
import sys
import time

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    print("paho-mqtt 2.1.0 required. Run: python3 -m pip install --user paho-mqtt==2.1.0", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="MQTT 5 check tool")
    parser.add_argument("--as", dest="role", choices=["gw", "consumer"], required=True)
    parser.add_argument("--pub", help="Topic to publish test message to")
    parser.add_argument("--sub", help="Topic filter to subscribe to")
    parser.add_argument("--wait", type=int, default=10, help="Seconds to wait when subscribing")
    parser.add_argument("--code", help="Student code override (default inferred from config)")
    args = parser.parse_args()

    iot_dir = ROOT / ".local/iot"
    endpoint_file = iot_dir / "endpoint"
    if not endpoint_file.exists():
        print("Missing .local/iot/endpoint. Run deploy/iot-up.sh first.", file=sys.stderr)
        sys.exit(1)
    endpoint = endpoint_file.read_text(encoding="utf-8").strip()

    config_file = ROOT / ".local/w03-config.json"
    code = args.code
    if not code and config_file.exists():
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
        code = f"{cfg.get('group', 'g06')}-{cfg.get('owner', '1')}"
    if not code:
        code = "g06-1"

    client_id = f"{args.role}-{code}"
    cert_file = str(iot_dir / f"{args.role}.cert.pem")
    key_file = str(iot_dir / f"{args.role}.private.key")
    ca_file = str(iot_dir / "AmazonRootCA1.pem")

    state = {
        "connected": False,
        "connack": None,
        "suback": None,
        "puback": None,
        "published_mid": None,
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
        print(f"connack={rc}")

    def on_subscribe(c, userdata, mid, reason_codes, properties):
        rc = reason_codes[0].value if hasattr(reason_codes[0], "value") else int(reason_codes[0])
        state["suback"] = rc
        print(f"suback={rc}")

    def on_publish(c, userdata, mid, reason_code, properties):
        rc = reason_code.value if hasattr(reason_code, "value") else int(reason_code)
        state["puback"] = rc
        print(f"puback={rc}")

    def on_message(c, userdata, msg):
        payload_str = msg.payload.decode("utf-8", errors="replace")
        print(f"recv topic={msg.topic} payload={payload_str}")

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_publish = on_publish
    client.on_message = on_message

    try:
        client.connect(endpoint, 8883, keepalive=60)
    except Exception as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    client.loop_start()

    # Wait for connect
    start_t = time.time()
    while state["connack"] is None and time.time() - start_t < 10:
        time.sleep(0.1)

    if state["connack"] != 0:
        client.loop_stop()
        sys.exit(0)

    if args.sub:
        client.subscribe(args.sub, qos=1)
        # Wait specified seconds
        wait_start = time.time()
        while time.time() - wait_start < args.wait:
            time.sleep(0.2)

    if args.pub:
        payload = json.dumps({"check": "t2", "code": code})
        info = client.publish(args.pub, payload=payload, qos=1)
        state["published_mid"] = info.mid
        pub_start = time.time()
        while state["puback"] is None and time.time() - pub_start < 10:
            time.sleep(0.1)

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
