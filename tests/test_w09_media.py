"""W9 media API: JPEG validation, fingerprint, receipt, upload, authorized read-back."""
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("service_mod_w09", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
media = service.w09_media


def fake_jpeg(width=640, height=480, filler=b"\x11\x22\x33"):
    """Structurally valid JPEG header chain (SOI, APP0, SOF0, SOS, data, EOI)."""
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + (11).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00"
    sos = b"\xff\xda" + (8).to_bytes(2, "big") + b"\x01\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + app0 + sof + sos + filler * 20 + b"\xff\xd9"


class MediaHelperTests(unittest.TestCase):
    def test_dimensions_of_valid_jpeg(self):
        self.assertEqual(media.jpeg_dimensions(fake_jpeg(640, 480)), (640, 480))

    def test_rejects_truncated_garbage_and_missing_sof(self):
        good = fake_jpeg()
        self.assertIsNone(media.jpeg_dimensions(good[:-2]))                 # 缺 EOI
        self.assertIsNone(media.jpeg_dimensions(b"not a jpeg at all"))
        self.assertIsNone(media.jpeg_dimensions(b"\xff\xd8\xff\xda\x00\x02\xff\xd9"))   # SOS 前沒有 SOF
        self.assertIsNone(media.jpeg_dimensions(b""))

    def test_time_is_normalized_to_utc_without_truncating_fractions(self):
        self.assertEqual(media.canonical_time("2026-11-03T09:10:00+08:00"), "2026-11-03T01:10:00Z")
        self.assertEqual(media.canonical_time("2026-11-03T01:10:00Z"), "2026-11-03T01:10:00Z")
        self.assertEqual(media.canonical_time("2026-11-03T01:10:00.500Z"), "2026-11-03T01:10:00.5Z")
        self.assertNotEqual(media.canonical_time("2026-11-03T01:10:00.500Z"), media.canonical_time("2026-11-03T01:10:00Z"))
        with self.assertRaises(ValueError):
            media.canonical_time("2026-11-03T01:10:00")      # 沒有時區

    def test_fingerprint_matches_documented_layout(self):
        # 韌體 w09_logic.h fingerprintInput：device_id\nobserved_at\ntype\nnote
        text = "g06-esp32-s3-cam\n2026-11-03T01:10:00Z\nanomaly\nhum=61.0 th=60 temp=26.0 src=dht11"
        expected = hashlib.sha256(text.encode()).hexdigest()
        got = media.event_fingerprint("g06-esp32-s3-cam", "2026-11-03T09:10:00+08:00", "anomaly",
                                      "hum=61.0 th=60 temp=26.0 src=dht11")
        self.assertEqual(got, expected)
        self.assertEqual(media.event_fingerprint("d", "2026-11-03T01:10:00Z", "test", None),
                         hashlib.sha256(b"d\n2026-11-03T01:10:00Z\ntest\n").hexdigest())


class MediaApiTests(unittest.TestCase):
    DEVICE = "g06-esp32-s3-cam"

    @classmethod
    def setUpClass(cls):
        cls.env_backup = {k: os.environ.get(k) for k in ("REPORTER_TOKEN", "OPERATOR_TOKEN", "W09_DEVICE_TOKEN", "W09_DEVICE_ID", "DB_HOST")}
        os.environ["REPORTER_TOKEN"] = "reporter-token-for-tests-0123456789"
        os.environ["OPERATOR_TOKEN"] = "operator-token-for-tests-0123456789"
        os.environ["W09_DEVICE_TOKEN"] = "device-token-for-tests-0123456789ab"
        os.environ["W09_DEVICE_ID"] = cls.DEVICE
        os.environ.pop("DB_HOST", None)       # 記憶體模式；資料庫路徑要在雲端驗證

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        version = Path(self.td.name) / "version"
        version.write_text("a" * 40)
        self.blobs = self.make_blobs()
        self.server = service.make_server(version, port=0, blob_store=self.blobs)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.port = self.server.server_port
        self.rep, self.op, self.dev = (os.environ["REPORTER_TOKEN"], os.environ["OPERATOR_TOKEN"], os.environ["W09_DEVICE_TOKEN"])

    def make_blobs(self):
        return None            # 預設：照片留在資料庫／記憶體；子類別改用假的 S3

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.td.cleanup()

    def call(self, path, method="GET", body=None, token=None, ctype=None):
        headers = {}
        if ctype:
            headers["Content-Type"] = ctype
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def post_event(self, event_id, device=None, note="hum=61.0 th=60 temp=26.0 src=test"):
        ev = {"event_id": event_id, "device_id": device or self.DEVICE, "observed_at": "2026-11-03T01:10:00Z",
              "type": "test", "note": note}
        status, _, _ = self.call("/events", "POST", json.dumps(ev).encode(), self.rep, "application/json")
        self.assertEqual(status, 201)
        return ev

    def receipt(self, event_id, token=None):
        status, _, raw = self.call("/w09/device/events/" + event_id, token=token or self.dev)
        return status, (json.loads(raw) if raw else {})

    def upload(self, event_id, data, token=None, ctype="image/jpeg"):
        status, _, raw = self.call(f"/w09/device/events/{event_id}/photo", "POST", data, token or self.dev, ctype)
        return status, (json.loads(raw) if raw else {})

    # ---- 授權 ----
    def test_roles_are_separated(self):
        self.post_event("ev-auth-1")
        self.assertEqual(self.call("/w09/device/events/ev-auth-1")[0], 401)
        self.assertEqual(self.receipt("ev-auth-1", token="wrong-token-value-0123456789abcdef")[0], 401)
        self.assertEqual(self.receipt("ev-auth-1", token=self.op)[0], 403)          # operator 不能用裝置收據
        self.assertEqual(self.receipt("ev-auth-1", token=self.rep)[0], 403)
        self.assertEqual(self.call("/events", token=self.dev)[0], 403)              # device 不能列事件
        self.assertEqual(self.call("/w09/events/ev-auth-1/photo", token=self.dev)[0], 403)   # device 不能讀回
        self.assertEqual(self.call("/w09/events/ev-auth-1/photo")[0], 401)

    def test_non_ascii_token_does_not_crash(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/w09/device/events/x1", method="GET")
        req.add_header("Authorization", "Bearer " + "é" * 40)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

    # ---- 收據 ----
    def test_receipt_before_and_after_event(self):
        self.assertEqual(self.receipt("ev-r-1")[0], 404)       # 事件尚在處理：呼叫端保留 ID 有限等待
        ev = self.post_event("ev-r-1")
        status, body = self.receipt("ev-r-1")
        self.assertEqual(status, 200)
        self.assertEqual((body["event_id"], body["device_id"], body["event_stored"]), ("ev-r-1", self.DEVICE, True))
        self.assertEqual(body["photo_state"], "none")
        self.assertEqual(body["event_fingerprint"], media.event_fingerprint(ev["device_id"], ev["observed_at"], ev["type"], ev["note"]))

    def test_receipt_rejects_event_of_another_device(self):
        self.post_event("ev-other-1", device="g06-other")
        self.assertEqual(self.receipt("ev-other-1")[0], 403)
        self.assertEqual(self.upload("ev-other-1", fake_jpeg())[0], 403)

    # ---- 上傳、重送、衝突、讀回 ----
    def test_upload_resend_conflict_and_authorized_readback(self):
        self.post_event("ev-p-1")
        jpeg = fake_jpeg(filler=b"\x01\x02\x03")
        sha = hashlib.sha256(jpeg).hexdigest()
        status, body = self.upload("ev-p-1", jpeg)
        self.assertEqual((status, body["status"], body["photo_sha256"]), (201, "created", sha))
        status, body = self.upload("ev-p-1", jpeg)                                  # 成功回覆遺失 → 原內容重送
        self.assertEqual((status, body["status"]), (200, "duplicate"))
        status, body = self.upload("ev-p-1", fake_jpeg(filler=b"\x09\x08\x07"))     # 同事件換圖
        self.assertEqual((status, body["error"]), (409, "photo_conflict"))
        status, body = self.receipt("ev-p-1")
        self.assertEqual((body["photo_state"], body["photo_sha256"]), ("ready", sha))   # 仍是原圖
        status, headers, raw = self.call("/w09/events/ev-p-1/photo", token=self.op)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(headers["X-Content-SHA256"], sha)
        self.assertEqual(raw, jpeg)

    def test_photo_for_event_without_photo_is_404(self):
        self.post_event("ev-p-2")
        self.assertEqual(self.call("/w09/events/ev-p-2/photo", token=self.op)[0], 404)

    def test_upload_to_missing_event_is_404_and_creates_nothing(self):
        self.assertEqual(self.upload("ev-nope", fake_jpeg())[0], 404)       # 不建立孤兒關聯
        self.assertEqual(self.call("/w09/events/ev-nope/photo", token=self.op)[0], 404)

    # ---- 媒體拒絕 ----
    def test_media_rejections(self):
        self.post_event("ev-m-1")
        self.assertEqual(self.upload("ev-m-1", fake_jpeg(), ctype="image/png")[0], 415)
        self.assertEqual(self.upload("ev-m-1", b"definitely not a jpeg")[0], 400)
        self.assertEqual(self.upload("ev-m-1", fake_jpeg()[:-2])[0], 400)               # 截斷
        self.assertEqual(self.upload("ev-m-1", fake_jpeg(3000, 2000))[0], 413)          # 像素過多
        self.assertEqual(self.receipt("ev-m-1")[1]["photo_state"], "none")               # 拒絕後什麼都沒存

    def test_oversize_declared_length_is_413_without_reading_body(self):
        self.post_event("ev-m-2")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/w09/device/events/ev-m-2/photo")
        conn.putheader("Authorization", "Bearer " + self.dev)
        conn.putheader("Content-Type", "image/jpeg")
        conn.putheader("Content-Length", str(media.MAX_PHOTO_BYTES + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_bad_event_id_format(self):
        self.assertEqual(self.receipt("bad.id")[0], 400)

    def test_health_reports_w09_flag(self):
        status, _, raw = self.call("/health")
        self.assertTrue(json.loads(raw)["w09_configured"])


class FakeBlobs:
    """測試用的 S3 物件存放區：只記錄 put/get，可模擬故障與內容被竄改。"""

    def __init__(self):
        self.objects, self.fail, self.puts = {}, False, 0

    def put(self, key, data, sha):
        if self.fail:
            raise RuntimeError("s3 down")
        self.puts += 1
        self.objects[key] = bytes(data)

    def get(self, key):
        if self.fail:
            raise RuntimeError("s3 down")
        return self.objects[key]


class MediaS3Tests(MediaApiTests):
    """同一組 API 行為，但照片存在（假的）私有 S3。"""

    def make_blobs(self):
        return FakeBlobs()

    def test_photo_bytes_live_in_blob_store_under_event_key(self):
        self.post_event("ev-s3-1")
        jpeg = fake_jpeg(filler=b"")
        self.assertEqual(self.upload("ev-s3-1", jpeg)[0], 201)
        self.assertEqual(self.blobs.objects, {"w09/events/ev-s3-1.jpg": jpeg})
        self.upload("ev-s3-1", jpeg)                                   # 原內容重送：不重複寫 S3
        self.assertEqual(self.blobs.puts, 1)
        self.assertEqual(self.upload("ev-s3-1", fake_jpeg(filler=b"	"))[0], 409)
        self.assertEqual(self.blobs.objects["w09/events/ev-s3-1.jpg"], jpeg)   # 衝突不覆寫原圖

    def test_storage_failure_is_503_and_leaves_no_photo(self):
        self.post_event("ev-s3-2")
        self.blobs.fail = True
        self.assertEqual(self.upload("ev-s3-2", fake_jpeg())[0], 503)
        self.blobs.fail = False
        self.assertEqual(self.receipt("ev-s3-2")[1]["photo_state"], "none")
        self.assertEqual(self.upload("ev-s3-2", fake_jpeg())[0], 201)  # 故障排除後以原內容重試成功

    def test_readback_failure_is_503(self):
        self.post_event("ev-s3-3")
        self.upload("ev-s3-3", fake_jpeg())
        self.blobs.fail = True
        self.assertEqual(self.call("/w09/events/ev-s3-3/photo", token=self.op)[0], 503)

    def test_health_reports_s3_store(self):
        _, _, raw = self.call("/health")
        self.assertEqual(json.loads(raw)["w09_photo_store"], "s3")


if __name__ == "__main__":
    unittest.main()
