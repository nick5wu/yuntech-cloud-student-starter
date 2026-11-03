"""W9 media helpers: JPEG validation, event fingerprint and canonical UTC time.

Pure functions only (no network, no database) so they are easy to test.
"""
from datetime import datetime, timezone
import hashlib
import os
import re

MAX_PHOTO_BYTES = 1024 * 1024        # 與 tools/w09_check.py 的讀回上限一致
MAX_PIXELS = 4_000_000               # 與 tools/w09_check.py 的像素上限一致
EVENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

_NO_LENGTH = set(range(0xD0, 0xD8)) | {0x01, 0xD8, 0x00}
_SOF = {m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC)}


def jpeg_dimensions(data):
    """Return (width, height) of a structurally valid baseline/progressive JPEG, else None."""
    if len(data) < 4 or data[:2] != b"\xff\xd8" or not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        return None
    i, n = 2, len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            return None
        while i < n and data[i] == 0xFF:      # 跳過填充的 0xFF
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker in _NO_LENGTH:
            continue
        if marker == 0xD9 or i + 2 > n:
            return None
        length = int.from_bytes(data[i:i + 2], "big")
        if length < 2 or i + length > n:
            return None
        if marker in _SOF:
            if length < 8:
                return None
            height = int.from_bytes(data[i + 3:i + 5], "big")
            width = int.from_bytes(data[i + 5:i + 7], "big")
            return (width, height) if width and height else None
        if marker == 0xDA:                     # 進入影像資料前還沒看到 SOF
            return None
        i += length
    return None


def parse_time(value):
    """datetime or ISO-8601 string with timezone -> aware datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt


def canonical_time(value):
    """Normalize to UTC, keep a non-zero fraction (never truncate different instants together)."""
    dt = parse_time(value).astimezone(timezone.utc)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        text += "." + f"{dt.microsecond:06d}".rstrip("0")
    return text + "Z"


def event_fingerprint(device_id, observed_at, ev_type, note):
    """SHA-256 over device_id, observed_at(UTC), type, note joined by newlines.

    Must match arduino/W09_CloudCamera/w09_logic.h fingerprintInput().
    """
    text = "\n".join([device_id, canonical_time(observed_at), ev_type, note or ""])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def photo_sha256(data):
    return hashlib.sha256(data).hexdigest()


def photo_key(event_id):
    """S3 物件鍵；event_id 已先通過 EVENT_ID_RE，不含路徑字元。"""
    if not EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("invalid event id")
    return f"w09/events/{event_id}.jpg"


class S3Blobs:
    """私有 bucket 的照片物件存取（憑證來自 EC2 的 instance profile，程式裡沒有金鑰）。"""

    def __init__(self, bucket, client=None):
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                "s3", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                config=Config(retries={"max_attempts": 3}, connect_timeout=5, read_timeout=10))
        self.bucket = bucket
        self.client = client

    def put(self, key, data, sha):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="image/jpeg",
                               Metadata={"sha256": sha}, ServerSideEncryption="AES256")

    def get(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
