#!/usr/bin/env python3
"""Provision AWS IoT Core certificates, policies, and root CA according to W6 Spec A."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import lab


def load_json(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def ensure_iot(code=None, config_file=".local/w03-config.json", resources_file=".local/resources.json"):
    resources = load_json(resources_file)
    config = load_json(config_file)
    ctx = lab.verify()
    account = ctx["account"]
    region = ctx["region"]

    if not code:
        group = config.get("group", "g06")
        owner = config.get("owner", "1")
        code = f"{group}-{owner}"

    iot_dir = Path(".local/iot")
    iot_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(iot_dir, 0o700)
    except Exception:
        pass

    # 1. Endpoint
    endpoint_file = iot_dir / "endpoint"
    if not endpoint_file.exists():
        ep_res = lab.run_aws(["iot", "describe-endpoint", "--endpoint-type", "iot:Data-ATS"], region)
        endpoint = ep_res["endpointAddress"]
        endpoint_file.write_text(endpoint.strip() + "\n", encoding="utf-8")
        print(f"IoT Endpoint saved: {endpoint}")
    else:
        endpoint = endpoint_file.read_text(encoding="utf-8").strip()
        print(f"IoT Endpoint loaded: {endpoint}")

    # 2. Amazon Root CA 1
    root_ca_file = iot_dir / "AmazonRootCA1.pem"
    if not root_ca_file.exists():
        print("Downloading AmazonRootCA1.pem...")
        urllib.request.urlretrieve("https://www.amazontrust.com/repository/AmazonRootCA1.pem", root_ca_file)
        try:
            os.chmod(root_ca_file, 0o600)
        except Exception:
            pass

    # 3. Policies
    gw_policy_name = f"cxe-{code}-gw"
    consumer_policy_name = f"cxe-{code}-consumer"

    gw_policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": f"arn:aws:iot:{region}:{account}:client/gw-{code}"
            },
            {
                "Effect": "Allow",
                "Action": "iot:Publish",
                "Resource": f"arn:aws:iot:{region}:{account}:topic/cxe/{code}/gw/events"
            }
        ]
    }

    consumer_policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": f"arn:aws:iot:{region}:{account}:client/consumer-{code}"
            },
            {
                "Effect": "Allow",
                "Action": "iot:Subscribe",
                "Resource": f"arn:aws:iot:{region}:{account}:topicfilter/cxe/{code}/gw/events"
            },
            {
                "Effect": "Allow",
                "Action": "iot:Receive",
                "Resource": f"arn:aws:iot:{region}:{account}:topic/cxe/{code}/gw/events"
            }
        ]
    }

    def ensure_policy(name, doc):
        try:
            lab.run_aws(["iot", "get-policy", "--policy-name", name], region)
            print(f"Policy {name} already exists.")
        except Exception:
            print(f"Creating policy {name}...")
            lab.run_aws(["iot", "create-policy", "--policy-name", name, "--policy-document", json.dumps(doc)], region)

    ensure_policy(gw_policy_name, gw_policy_doc)
    ensure_policy(consumer_policy_name, consumer_policy_doc)
    resources["iot_gw_policy"] = gw_policy_name
    resources["iot_consumer_policy"] = consumer_policy_name

    # 4. Certificates and Keys
    def ensure_cert(prefix, policy_name, cert_key_in_res):
        cert_file = iot_dir / f"{prefix}.cert.pem"
        key_file = iot_dir / f"{prefix}.private.key"

        existing_cert_id = resources.get(cert_key_in_res)
        if existing_cert_id and cert_file.exists() and key_file.exists():
            print(f"Certificate for {prefix} already exists: {existing_cert_id}")
            return existing_cert_id

        print(f"Creating X.509 keys and certificate for {prefix} (output suppressed from console)...")
        # Direct creation: output contains private key! We process it directly in memory without echoing to terminal.
        res = lab.run_aws(["iot", "create-keys-and-certificate", "--set-as-active"], region)
        cert_id = res["certificateId"]
        cert_arn = res["certificateArn"]
        cert_pem = res["certificatePem"]
        key_pem = res["keyPair"]["PrivateKey"]

        # Write files with 0600 permissions
        cert_file.write_text(cert_pem, encoding="utf-8")
        key_file.write_text(key_pem, encoding="utf-8")
        try:
            os.chmod(cert_file, 0o600)
            os.chmod(key_file, 0o600)
        except Exception:
            pass

        # Attach policy
        print(f"Attaching policy {policy_name} to {prefix} certificate...")
        lab.run_aws(["iot", "attach-policy", "--policy-name", policy_name, "--target", cert_arn], region)

        resources[cert_key_in_res] = cert_id
        return cert_id

    gw_cert_id = ensure_cert("gw", gw_policy_name, "iot_gw_cert_id")
    consumer_cert_id = ensure_cert("consumer", consumer_policy_name, "iot_consumer_cert_id")

    save_json(resources_file, resources)

    # 5. Read back both certificates are ACTIVE
    for label, cid in (("Gateway", gw_cert_id), ("Consumer", consumer_cert_id)):
        desc = lab.run_aws(["iot", "describe-certificate", "--certificate-id", cid], region)
        status = desc.get("certificateDescription", {}).get("status")
        print(f"{label} Certificate ({cid[-4:]}): {status}")
        if status != "ACTIVE":
            raise RuntimeError(f"Certificate {cid} is not ACTIVE")

    print(f"\nIoT Setup Completed successfully for code '{code}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", help="Student code, e.g. g06-1")
    args = parser.parse_args()
    ensure_iot(args.code)
