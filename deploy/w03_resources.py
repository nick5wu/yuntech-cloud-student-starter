#!/usr/bin/env python3
import argparse
import ipaddress
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import lab


def aws_json(arguments, region):
    return cast(dict[str, Any], lab.run_aws(arguments, region))


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise lab.LabError(f"Cannot read {path}: {type(exc).__name__}") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def require_config(config):
    required = ("region", "vpc_id", "subnet_id", "ami_id", "source_cidr", "group", "owner", "key_name", "public_key_path")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise lab.LabError("Missing config fields: " + ", ".join(missing))
    try:
        network = ipaddress.ip_network(config["source_cidr"], strict=True)
    except ValueError as exc:
        raise lab.LabError("source_cidr must be one IPv4 /32") from exc
    if network.version != 4 or network.prefixlen != 32:
        raise lab.LabError("source_cidr must be one IPv4 /32")
    for key in ("group", "owner"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", config[key]):
            raise lab.LabError(f"Invalid {key}; use 1-32 ASCII letters, digits, - or _")


def tags(config):
    return [
        {"Key": "course", "Value": lab.COURSE},
        {"Key": "week", "Value": "w03"},
        {"Key": "group", "Value": config["group"]},
        {"Key": "owner", "Value": config["owner"]},
    ]


def tag_spec(resource_type, config):
    return json.dumps({"ResourceType": resource_type, "Tags": tags(config)}, separators=(",", ":"))


def describe_network(config, region):
    vpc = aws_json(["ec2", "describe-vpcs", "--vpc-ids", config["vpc_id"], "--filters", "Name=isDefault,Values=true"], region)
    if not vpc.get("Vpcs"):
        raise lab.LabError("Configured VPC is not the default VPC")
    subnet = aws_json(["ec2", "describe-subnets", "--subnet-ids", config["subnet_id"]], region)
    subnets = subnet.get("Subnets", [])
    if not subnets or subnets[0].get("VpcId") != config["vpc_id"] or not subnets[0].get("DefaultForAz"):
        raise lab.LabError("Configured subnet is not a default subnet in the configured default VPC")
    tables = aws_json(["ec2", "describe-route-tables", "--filters", "Name=vpc-id,Values=" + config["vpc_id"]], region).get("RouteTables", [])
    eligible = []
    for table in tables:
        associations = table.get("Associations", [])
        associated = any(item.get("SubnetId") == config["subnet_id"] for item in associations)
        main = any(item.get("Main") for item in associations)
        if associated or main:
            eligible.append(table)
    if not any(route.get("GatewayId", "").startswith("igw-") and route.get("DestinationCidrBlock") == "0.0.0.0/0"
               for table in eligible for route in table.get("Routes", [])):
        raise lab.LabError("Configured subnet has no verified default route to an internet gateway")
    image = aws_json(["ec2", "describe-images", "--image-ids", config["ami_id"]], region).get("Images", [])
    if not image or image[0].get("Architecture") != "x86_64" or "al2023" not in image[0].get("Name", "").lower():
        raise lab.LabError("Configured AMI is not a verified AL2023 x86_64 image")


def get_instance(region, instance_id):
    reservations = aws_json(["ec2", "describe-instances", "--instance-ids", instance_id], region).get("Reservations", [])
    instances = [item for reservation in reservations for item in reservation.get("Instances", [])]
    return instances[0] if instances else None


def update_resources(path, resources, **updates):
    resources.update(updates)
    write_json(path, resources)


def wait_for_state(region, instance_id, wanted, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        instance = get_instance(region, instance_id)
        if instance and instance.get("State", {}).get("Name") == wanted:
            return instance
        time.sleep(5)
    raise lab.LabError(f"Instance did not reach {wanted} before timeout")


def health(public_ip, commit):
    request = urllib.request.Request(f"http://{public_ip}/health", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or body.get("version") != commit:
                return False, response.status, body
            return True, response.status, body
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False, 0, None


def up(args):
    config = load_json(args.config)
    require_config(config)
    ctx = lab.verify()
    if ctx["region"] != config["region"]:
        raise lab.LabError("Config region does not match verified Learner Lab region")
    describe_network(config, ctx["region"])
    public_key = Path(config["public_key_path"]).expanduser()
    if not public_key.is_file():
        raise lab.LabError("Public key file does not exist; generate it in ~/.ssh and do not upload the private key")
    resources_path = Path(args.resources)
    if resources_path.exists():
        raise lab.LabError(f"Refusing to overwrite existing resource record: {resources_path}")
    group_name = f"w03-{config['group']}-{config['owner']}-sg"
    print("Will create: 1 key pair, 1 security group, 1 encrypted gp3 t3.micro EC2 instance")
    print(f"Region: {ctx['region']}; VPC: {config['vpc_id']}; subnet: {config['subnet_id']}; source: {config['source_cidr']}")
    print(f"Commit: {args.commit}; group: {config['group']}; owner: {config['owner']}")
    print(f"Network exposure: TCP 22 and 80 from {config['source_cidr']}; service port 8080 remains on loopback.")
    print("Cost footprint: t3.micro runtime, 8 GiB encrypted gp3, and public IPv4 while running; verify current regional pricing and Learner Lab budget.")
    print("Cleanup: deploy/down.sh delete (terminate this instance and remove its SG/key pair; volume and ENI are instance-owned).")
    lab.approve("No AWS resource has been created yet.", "CREATE-W03")

    key_material = public_key.read_text(encoding="utf-8").strip()
    key = aws_json(["ec2", "import-key-pair", "--key-name", config["key_name"], "--public-key-material", key_material,
                    "--tag-specifications", tag_spec("key-pair", config)], ctx["region"])
    resources = {"region": ctx["region"], "commit": args.commit, "key_name": config["key_name"],
                 "key_pair_id": key.get("KeyPairId"), "tags": tags(config)}
    write_json(resources_path, resources)

    sg = aws_json(["ec2", "create-security-group", "--group-name", group_name, "--description", "W3 inspection service",
                   "--vpc-id", config["vpc_id"], "--tag-specifications", tag_spec("security-group", config)], ctx["region"])
    sg_id = sg["GroupId"]
    update_resources(resources_path, resources, security_group_id=sg_id)
    lab.run_aws(["ec2", "authorize-security-group-ingress", "--group-id", sg_id, "--ip-permissions", json.dumps([
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": config["source_cidr"]}]},
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": config["source_cidr"]}]},
    ], separators=(",", ":"))], ctx["region"])

    instance = aws_json(["ec2", "run-instances", "--image-id", config["ami_id"], "--instance-type", "t3.micro",
                         "--count", "1", "--subnet-id", config["subnet_id"], "--security-group-ids", sg_id,
                         "--key-name", config["key_name"], "--user-data", "fileb://" + str(Path(args.user_data).resolve()),
                         "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled",
                         "--block-device-mappings", json.dumps([{"DeviceName": "/dev/xvda", "Ebs": {
                             "VolumeType": "gp3", "VolumeSize": 8, "Encrypted": True, "DeleteOnTermination": True}}], separators=(",", ":")),
                         "--tag-specifications", tag_spec("instance", config), tag_spec("volume", config),
                         tag_spec("network-interface", config)], ctx["region"])
    instance_id = instance["Instances"][0]["InstanceId"]
    update_resources(resources_path, resources, instance_id=instance_id)
    current = get_instance(ctx["region"], instance_id)
    if not current:
        raise lab.LabError("Created instance could not be read back")
    block = current.get("BlockDeviceMappings", [{}])[0]
    update_resources(resources_path, resources, root_volume_id=block.get("Ebs", {}).get("VolumeId"),
                     eni_id=current.get("NetworkInterfaces", [{}])[0].get("NetworkInterfaceId"))
    wait_for_state(ctx["region"], instance_id, "running")
    current = get_instance(ctx["region"], instance_id)
    if not current:
        raise lab.LabError("Running instance could not be read back")
    public_ip = current.get("PublicIpAddress")
    if not public_ip:
        raise lab.LabError("Running instance has no public IPv4 address")
    update_resources(resources_path, resources, public_ip=public_ip, state="running")
    deadline = time.time() + 300
    while time.time() < deadline:
        okay, status, body = health(public_ip, resources["commit"])
        if okay:
            update_resources(resources_path, resources, health_status=status, health_body=body)
            print(f"Health OK: http://{public_ip}/health; version matches {resources['commit']}")
            return
        time.sleep(5)
    raise lab.LabError("/health did not return HTTP 200 with the deployed commit before timeout")


def check_tag(resource, config):
    actual = {tag["Key"]: tag["Value"] for tag in resource.get("Tags", [])}
    expected = {tag["Key"]: tag["Value"] for tag in tags(config)}
    if any(actual.get(key) != value for key, value in expected.items()):
        raise lab.LabError("Resource tags do not prove ownership; refusing operation")


def get_delete_target(region, identifier, command, collection, config, label):
    if not identifier:
        raise lab.LabError(f"Resource record has no {label} ID; refusing destructive operation")
    result = aws_json(command + [identifier], region)
    matches = result.get(collection, [])
    if not matches:
        return None
    if len(matches) != 1:
        raise lab.LabError(f"Could not uniquely verify {label}; refusing destructive operation")
    resource = matches[0]
    check_tag(resource, config)
    return resource


def verify_delete_targets(region, instance_id, resources, config):
    security_group = get_delete_target(region, resources.get("security_group_id"),
                                       ["ec2", "describe-security-groups", "--group-ids"],
                                       "SecurityGroups", config, "security group")
    if security_group and security_group.get("VpcId") != config["vpc_id"]:
        raise lab.LabError("Security group VPC does not match config; refusing destructive operation")

    key_pair = get_delete_target(region, resources.get("key_pair_id"),
                                 ["ec2", "describe-key-pairs", "--key-pair-ids"],
                                 "KeyPairs", config, "key pair")
    volume = get_delete_target(region, resources.get("root_volume_id"),
                               ["ec2", "describe-volumes", "--volume-ids"],
                               "Volumes", config, "root volume")
    if volume and not any(item.get("InstanceId") == instance_id for item in volume.get("Attachments", [])):
        raise lab.LabError("Root volume is not attached to the recorded instance; refusing destructive operation")

    eni = get_delete_target(region, resources.get("eni_id"),
                            ["ec2", "describe-network-interfaces", "--network-interface-ids"],
                            "NetworkInterfaces", config, "network interface")
    if eni and eni.get("Attachment", {}).get("InstanceId") != instance_id:
        raise lab.LabError("Network interface is not attached to the recorded instance; refusing destructive operation")
    return {"security_group": security_group, "key_pair": key_pair}


def down(args):
    config = load_json(args.config)
    require_config(config)
    resources_path = Path(args.resources)
    resources = load_json(resources_path)
    instance_id = resources.get("instance_id")
    if not instance_id:
        raise lab.LabError("Resource record has no instance_id")
    ctx = lab.verify()
    if ctx["region"] != resources.get("region"):
        raise lab.LabError("Resource record region does not match verified Learner Lab region")
    instance = get_instance(ctx["region"], instance_id)
    if not instance:
        raise lab.LabError("Instance is not readable; refusing destructive operation")
    check_tag(instance, config)
    if args.stop:
        print(f"Will stop instance {instance_id}; SG, key pair and volumes remain.")
        lab.approve("No change has been made yet.", "STOP-W03")
        lab.run_aws(["ec2", "stop-instances", "--instance-ids", instance_id], ctx["region"])
        wait_for_state(ctx["region"], instance_id, "stopped")
        update_resources(resources_path, resources, state="stopped")
        print(f"Stopped: {instance_id}")
        return

    targets = verify_delete_targets(ctx["region"], instance_id, resources, config)
    print(f"Will terminate instance {instance_id} and remove only recorded resources:")
    print(f"  SG {resources['security_group_id']}; key pair {resources['key_pair_id']}; root volume {resources['root_volume_id']}; ENI {resources['eni_id']}")
    print("Cost after cleanup: these recorded instance resources are removed; unrelated resources are untouched.")
    lab.approve("Only IDs from the resource record will be processed.", "DELETE-W03")
    lab.run_aws(["ec2", "terminate-instances", "--instance-ids", instance_id], ctx["region"])
    wait_for_state(ctx["region"], instance_id, "terminated")
    sg_id = resources.get("security_group_id")
    if sg_id and targets["security_group"]:
        lab.run_aws(["ec2", "delete-security-group", "--group-id", sg_id], ctx["region"])
    key_id = resources.get("key_pair_id")
    if key_id and targets["key_pair"]:
        lab.run_aws(["ec2", "delete-key-pair", "--key-pair-id", key_id], ctx["region"])
    missing = []
    for kind, identifier, command, collection in (("security_group", sg_id, ["ec2", "describe-security-groups", "--group-ids"], "SecurityGroups"),
                                                   ("key_pair", key_id, ["ec2", "describe-key-pairs", "--key-pair-ids"], "KeyPairs"),
                                                   ("volume", resources.get("root_volume_id"), ["ec2", "describe-volumes", "--volume-ids"], "Volumes"),
                                                   ("eni", resources.get("eni_id"), ["ec2", "describe-network-interfaces", "--network-interface-ids"], "NetworkInterfaces")):
        if not identifier:
            continue
        try:
            result = aws_json(command + [identifier], ctx["region"])
            if not result.get(collection):
                missing.append(kind)
        except lab.LabError as exc:
            if "Invalid" in str(exc) or "NotFound" in str(exc):
                missing.append(kind)
            else:
                raise
    if len(missing) != 4:
        raise lab.LabError("Cleanup read-back is incomplete; inspect only the recorded resource IDs before retrying")
    update_resources(resources_path, resources, state="terminated", deleted=True, missing_after_delete=missing)
    print(f"Terminated: {instance_id}; read-back missing: {', '.join(missing) or 'none'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("up", "down"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--resources", required=True)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--user-data")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.action == "up":
        if not args.user_data:
            parser.error("up requires --user-data")
        up(args)
    else:
        down(args)


if __name__ == "__main__":
    try:
        main()
    except (lab.LabError, OSError, ValueError, KeyError) as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        sys.exit(1)