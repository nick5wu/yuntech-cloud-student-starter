"""Offline ownership checks for W3 resource cleanup."""
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


if __name__ == "__main__":
    unittest.main()