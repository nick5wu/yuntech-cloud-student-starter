#!/usr/bin/env python3
"""Provision private RDS PostgreSQL instance, private subnets, route table and security group."""
import argparse
import json
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import lab


def load_json(path):
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def ensure_db(config_file=".local/w03-config.json", resources_file=".local/resources.json"):
    resources = load_json(resources_file)
    config = load_json(config_file)
    region = config.get("region", "us-east-1")
    vpc_id = resources.get("vpc_id") or config.get("vpc_id")
    host_sg_id = resources.get("security_group_id")
    group = config.get("group", "g06")
    owner = config.get("owner", "1")

    if not vpc_id or not host_sg_id:
        print("Error: vpc_id and host security_group_id must be in resources.json", file=sys.stderr)
        sys.exit(1)

    tag_course = lab.COURSE
    tags_param = f"Key=course,Value={tag_course} Key=week,Value=w05 Key=group,Value={group} Key=owner,Value={owner}"

    # 1. Check existing RDS
    db_id = resources.get("db_identifier", "inspection-db")
    try:
        rds_info = lab.run_aws(["rds", "describe-db-instances", "--db-instance-identifier", db_id], region)
        inst = rds_info.get("DBInstances", [{}])[0]
        status = inst.get("DBInstanceStatus")
        endpoint = inst.get("Endpoint", {}).get("Address")
        publicly_acc = inst.get("PubliclyAccessible", False)
        print(f"RDS {db_id} already exists. Status: {status}, Endpoint: {endpoint}, PubliclyAccessible: {publicly_acc}")
        resources["db_identifier"] = db_id
        resources["db_status"] = status
        if endpoint:
            resources["db_host"] = endpoint
        resources["publicly_accessible"] = publicly_acc
        save_json(resources_file, resources)
        return
    except Exception:
        print(f"RDS {db_id} not found in AWS, proceeding to verify/create subnets and DB...")

    # 2. Create private subnets if not already in resources
    subnet_a = resources.get("db_subnet_a_id")
    subnet_b = resources.get("db_subnet_b_id")
    rtb_id = resources.get("db_route_table_id")

    if not rtb_id:
        print("Creating private route table (local only)...")
        res = lab.run_aws(["ec2", "create-route-table", "--vpc-id", vpc_id, "--tag-specifications", f"ResourceType=route-table,Tags=[{tags_param.replace(' ', ',')}]"], region)
        rtb_id = res["RouteTable"]["RouteTableId"]
        resources["db_route_table_id"] = rtb_id
        save_json(resources_file, resources)

    if not (subnet_a and subnet_b):
        print("Creating 2 private subnets across 2 AZs...")
        res_a = lab.run_aws(["ec2", "create-subnet", "--vpc-id", vpc_id, "--cidr-block", "172.31.100.0/24", "--availability-zone", f"{region}a", "--tag-specifications", f"ResourceType=subnet,Tags=[{tags_param.replace(' ', ',')}]"], region)
        subnet_a = res_a["Subnet"]["SubnetId"]
        res_b = lab.run_aws(["ec2", "create-subnet", "--vpc-id", vpc_id, "--cidr-block", "172.31.101.0/24", "--availability-zone", f"{region}b", "--tag-specifications", f"ResourceType=subnet,Tags=[{tags_param.replace(' ', ',')}]"], region)
        subnet_b = res_b["Subnet"]["SubnetId"]
        resources["db_subnet_a_id"] = subnet_a
        resources["db_subnet_b_id"] = subnet_b
        resources["db_subnet_ids"] = [subnet_a, subnet_b]

        # Associate with private route table
        lab.run_aws(["ec2", "associate-route-table", "--route-table-id", rtb_id, "--subnet-id", subnet_a], region)
        lab.run_aws(["ec2", "associate-route-table", "--route-table-id", rtb_id, "--subnet-id", subnet_b], region)
        save_json(resources_file, resources)

    # 3. DB Subnet group
    sng_name = resources.get("db_subnet_group", "inspection-private-subnets")
    try:
        lab.run_aws(["rds", "describe-db-subnet-groups", "--db-subnet-group-name", sng_name], region)
    except Exception:
        print(f"Creating DB subnet group {sng_name}...")
        lab.run_aws(["rds", "create-db-subnet-group", "--db-subnet-group-name", sng_name, "--db-subnet-group-description", "Private subnets for RDS", "--subnet-ids", subnet_a, subnet_b], region)
    resources["db_subnet_group"] = sng_name
    save_json(resources_file, resources)

    # 4. SG-db
    sg_db = resources.get("sg_db_id")
    if not sg_db:
        print("Creating SG-db...")
        res_sg = lab.run_aws(["ec2", "create-security-group", "--group-name", f"sg-db-{group}-{owner}", "--description", "Private RDS SG", "--vpc-id", vpc_id], region)
        sg_db = res_sg["GroupId"]
        resources["sg_db_id"] = sg_db
        # Ingress: only TCP 5432 from host SG
        lab.run_aws(["ec2", "authorize-security-group-ingress", "--group-id", sg_db, "--protocol", "tcp", "--port", "5432", "--source-group", host_sg_id], region)
        save_json(resources_file, resources)

    # 5. Generate DB password and create RDS
    db_env_file = Path(".local/db.env")
    if not db_env_file.exists():
        db_pass = secrets.token_urlsafe(16)
        db_env_file.write_text(f"DB_USER=inspection\nDB_PASSWORD={db_pass}\nDB_NAME=inspection\n", encoding="utf-8")
        try:
            db_env_file.chmod(0o600)
        except Exception:
            pass
    else:
        # read existing password
        content = db_env_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("DB_PASSWORD="):
                db_pass = line.split("=", 1)[1].strip()

    print(f"Creating RDS instance {db_id} (this takes several minutes)...")
    lab.run_aws([
        "rds", "create-db-instance",
        "--db-instance-identifier", db_id,
        "--db-instance-class", "db.t3.micro",
        "--engine", "postgres",
        "--allocated-storage", "20",
        "--storage-type", "gp3",
        "--master-username", "inspection",
        "--master-user-password", db_pass,
        "--db-name", "inspection",
        "--db-subnet-group-name", sng_name,
        "--vpc-security-group-ids", sg_db,
        "--no-publicly-accessible",
        "--storage-encrypted",
        "--backup-retention-period", "0"
    ], region)

    resources["db_identifier"] = db_id
    resources["db_status"] = "creating"
    resources["publicly_accessible"] = False
    save_json(resources_file, resources)
    print("RDS creation initiated. Status recorded.")


if __name__ == "__main__":
    ensure_db()
