"""Offline ownership checks for W3 resource cleanup."""
from argparse import Namespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import w03_resources


CONFIG = {
    "vpc_id": "vpc-test",
    "group": "g03",
    "owner": "m2",
}
INSTANCE_ID = "i-test"
RESOURCES = {
    "security_group_id": "sg-test",
    "key_pair_id": "key-test",
    "root_volume_id": "vol-test",
    "eni_id": "eni-test",
}
TAGS = w03_resources.tags(CONFIG)


def resource_responses(volume_tags=None):
    return [
        {"SecurityGroups": [{"VpcId": CONFIG["vpc_id"], "Tags": TAGS}]},
        {"KeyPairs": [{"Tags": TAGS}]},
        {"Volumes": [{"Tags": volume_tags or TAGS,
                       "Attachments": [{"InstanceId": INSTANCE_ID}]}]},
        {"NetworkInterfaces": [{"Tags": TAGS,
                                "Attachment": {"InstanceId": INSTANCE_ID}}]},
    ]


class W03ResourceOwnership(unittest.TestCase):
    def test_delete_targets_require_owned_tags_and_instance_links(self):
        with patch.object(w03_resources, "aws_json", side_effect=resource_responses()):
            targets = w03_resources.verify_delete_targets(
                "us-east-1", INSTANCE_ID, RESOURCES, CONFIG
            )

        self.assertIsNotNone(targets["security_group"])
        self.assertIsNotNone(targets["key_pair"])

    def test_delete_targets_reject_a_volume_with_foreign_tags(self):
        foreign_tags = [*TAGS[:-1], {"Key": "owner", "Value": "someone-else"}]
        with patch.object(
            w03_resources, "aws_json", side_effect=resource_responses(foreign_tags)
        ):
            with self.assertRaises(w03_resources.lab.LabError):
                w03_resources.verify_delete_targets(
                    "us-east-1", INSTANCE_ID, RESOURCES, CONFIG
                )

    def test_delete_targets_reject_missing_recorded_ids(self):
        incomplete = {**RESOURCES, "eni_id": ""}
        with patch.object(w03_resources, "aws_json", side_effect=resource_responses()) as aws:
            with self.assertRaises(w03_resources.lab.LabError):
                w03_resources.verify_delete_targets(
                    "us-east-1", INSTANCE_ID, incomplete, CONFIG
                )
        self.assertEqual(aws.call_count, 3)

    def test_import_key_pair_uses_fileb_public_key_path(self):
        class StopAfterKeyImport(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_key = root / "test.pub"
            public_key.write_text("ssh-ed25519 AAAATEST fixture\n", encoding="utf-8")
            config = {
                "region": "us-east-1", "vpc_id": "vpc-test", "subnet_id": "subnet-test",
                "ami_id": "ami-test", "source_cidr": "192.0.2.1/32", "group": "g06",
                "owner": "6", "key_name": "w03-g06-6", "public_key_path": str(public_key),
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            args = Namespace(
                config=str(config_path), resources=str(root / "resources.json"),
                commit="a" * 40, user_data=str(root / "user-data.sh"),
            )
            calls = []

            def fake_aws(arguments, region):
                calls.append(arguments)
                if arguments[1] == "import-key-pair":
                    return {"KeyPairId": "key-test"}
                raise StopAfterKeyImport

            with patch.object(w03_resources.lab, "verify", return_value={"region": "us-east-1"}), \
                 patch.object(w03_resources, "describe_network"), \
                 patch.object(w03_resources.lab, "approve"), \
                 patch.object(w03_resources, "aws_json", side_effect=fake_aws):
                with self.assertRaises(StopAfterKeyImport):
                    w03_resources.up(args)

            argument_index = calls[0].index("--public-key-material") + 1
            self.assertEqual(calls[0][argument_index], "fileb://" + str(public_key.resolve()))


if __name__ == "__main__":
    unittest.main()