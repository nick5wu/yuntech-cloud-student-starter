"""Test event API endpoints, auth, validation, idempotency and filtering."""
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
spec = importlib.util.spec_from_file_location("service_mod", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class EventApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["REPORTER_TOKEN"] = "test-reporter-token-secret"
        os.environ["OPERATOR_TOKEN"] = "test-operator-token-secret"
        cls.valid_event = json.loads((ROOT / "tests/fixtures/valid_event.json").read_text(encoding="utf-8"))
        cls.invalid_no_tz = json.loads((ROOT / "tests/fixtures/invalid_no_tz.json").read_text(encoding="utf-8"))
        cls.invalid_extra = json.loads((ROOT / "tests/fixtures/invalid_extra_field.json").read_text(encoding="utf-8"))

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.version = Path(self.td.name) / "version"
        self.version.write_text("a" * 40)
        self.server = service.make_server(self.version, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.td.cleanup()

    def make_request(self, path, method="GET", body=None, token=None, content_type="application/json"):
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if resp.headers.get("Content-Type", "").startswith("application/json") else raw.decode("utf-8")
        except urllib.error.HTTPError as err:
            raw = err.read()
            return err.code, json.loads(raw) if err.headers.get("Content-Type", "").startswith("application/json") else raw.decode("utf-8")

    def test_health_includes_auth_and_db_flags(self):
        status, body = self.make_request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["auth_configured"])
        self.assertIn("db_configured", body)

    def test_post_without_token_returns_401(self):
        status, body = self.make_request("/events", method="POST", body=self.valid_event)
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error"), "unauthorized")

    def test_post_with_operator_token_returns_403(self):
        status, body = self.make_request("/events", method="POST", body=self.valid_event, token="test-operator-token-secret")
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "forbidden")

    def test_post_without_tz_returns_400(self):
        status, body = self.make_request("/events", method="POST", body=self.invalid_no_tz, token="test-reporter-token-secret")
        self.assertEqual(status, 400)
        self.assertEqual(body.get("field"), "observed_at")

    def test_post_with_extra_field_returns_400(self):
        status, body = self.make_request("/events", method="POST", body=self.invalid_extra, token="test-reporter-token-secret")
        self.assertEqual(status, 400)
        self.assertEqual(body.get("field"), "extra_hack")

    def test_post_valid_event_created_and_idempotent(self):
        # 1. Post new event -> 201 Created
        status, body = self.make_request("/events", method="POST", body=self.valid_event, token="test-reporter-token-secret")
        self.assertEqual(status, 201)
        self.assertEqual(body.get("status"), "created")

        # 2. Resend identical event -> 200 Duplicate
        status, body = self.make_request("/events", method="POST", body=self.valid_event, token="test-reporter-token-secret")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "duplicate")

        # 3. Resend with different note -> 409 Conflict
        conflicted = dict(self.valid_event)
        conflicted["note"] = "different note content"
        status, body = self.make_request("/events", method="POST", body=conflicted, token="test-reporter-token-secret")
        self.assertEqual(status, 409)
        self.assertEqual(body.get("error"), "event_conflict")

    def test_get_events_operator_only_and_filtering(self):
        # Insert event
        self.make_request("/events", method="POST", body=self.valid_event, token="test-reporter-token-secret")

        # Reporter cannot GET /events -> 403
        status, _ = self.make_request("/events", token="test-reporter-token-secret")
        self.assertEqual(status, 403)

        # Operator can GET /events -> 200
        status, events = self.make_request("/events", token="test-operator-token-secret")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, list)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], self.valid_event["event_id"])

        # Filter by matched device_id -> 1
        status, events = self.make_request(f"/events?device_id={self.valid_event['device_id']}", token="test-operator-token-secret")
        self.assertEqual(status, 200)
        self.assertEqual(len(events), 1)

        # Filter by unmatched device_id -> 0
        status, events = self.make_request("/events?device_id=other-dev", token="test-operator-token-secret")
        self.assertEqual(status, 200)
        self.assertEqual(len(events), 0)

    def test_root_page_serves_html(self):
        status, body = self.make_request("/")
        self.assertEqual(status, 200)
        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn("textContent", body)
        self.assertNotIn("innerHTML", body)
        self.assertNotIn("localStorage", body)


if __name__ == "__main__":
    unittest.main()
