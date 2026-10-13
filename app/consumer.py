#!/usr/bin/env python3
"""W6 Inspection MQTT Consumer service (Spec C)."""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
    HAVE_PAHO = True
except ImportError:
    HAVE_PAHO = False


def main():
    if not HAVE_PAHO:
        print("paho-mqtt 2.1.0 required. Run: python3 -m pip install --user paho-mqtt==2.1.0", file=sys.stderr)
        sys.exit(1)
    endpoint = os.environ.get("IOT_ENDPOINT")
    topic = os.environ.get("IOT_TOPIC")
    client_id = os.environ.get("IOT_CLIENT_ID")
    device_ids_raw = os.environ.get("IOT_DEVICE_IDS", "")
    ca_file = os.environ.get("IOT_CA")
    cert_file = os.environ.get("IOT_CERT")
    key_file = os.environ.get("IOT_KEY")
    reporter_token = os.environ.get("REPORTER_TOKEN")

    if not (endpoint and topic and client_id and ca_file and cert_file and key_file and reporter_token):
        print("Missing required IOT_ or REPORTER_TOKEN environment variables", file=sys.stderr)
        sys.exit(1)

    allowed_device_ids = {d.strip() for d in device_ids_raw.split(",") if d.strip()}

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
        if rc == 0:
            c.subscribe(topic, qos=1)
        else:
            print(f"connect_failed rc={rc}", file=sys.stderr)

    def on_subscribe(c, userdata, mid, reason_codes, properties):
        rc = reason_codes[0].value if hasattr(reason_codes[0], "value") else int(reason_codes[0])
        print(f"subscribed rc={rc}", flush=True)

    def on_message(c, userdata, msg):
        event_id = "unknown"
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            event_id = payload.get("event_id", "unknown")
            dev_id = payload.get("device_id")

            # CRITICAL REVIEW RULE 1:
            # Check device_id against whitelist FIRST before calling API.
            # If not in whitelist, count as rejected and do NOT call API!
            if dev_id not in allowed_device_ids:
                print(f"event_id={event_id} result=rejected", flush=True)
                return

            # Forward to API
            req = urllib.request.Request(
                "http://127.0.0.1:8080/events",
                data=msg.payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {reporter_token}"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 201:
                    result = "created"
                elif resp.status == 200:
                    result = "duplicate"
                else:
                    result = "rejected"
        except urllib.error.HTTPError as err:
            if err.code == 200:
                result = "duplicate"
            elif 400 <= err.code < 500:
                result = "rejected"
            else:
                result = "error"
        except Exception:
            result = "error"

        print(f"event_id={event_id} result={result}", flush=True)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        client.connect(endpoint, 8883, keepalive=60)
    except Exception as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    client.loop_forever()


if __name__ == "__main__":
    main()
