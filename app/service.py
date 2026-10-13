#!/usr/bin/env python3
"""Inspection service with event persistence, authentication, idempotency and filtering."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import urllib.parse

try:
    import psycopg2
    HAVE_PSYCOPG2 = True
except ImportError:
    HAVE_PSYCOPG2 = False

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>巡檢服務儀表板</title>
<style>
body { font-family: sans-serif; margin: 2rem; background: #f9f9f9; color: #333; }
.card { background: white; padding: 1.5rem; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 900px; margin: 0 auto; }
input { padding: 0.5rem; margin: 0.5rem 0; width: calc(100% - 1.2rem); border: 1px solid #ccc; border-radius: 4px; }
button { padding: 0.6rem 1.2rem; background: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer; }
button:hover { background: #0052a3; }
table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
th { background-color: #f2f2f2; }
.meta { font-size: 0.9em; color: #666; margin-bottom: 1rem; }
</style>
</head>
<body>
<div class="card">
  <h2>巡檢事件查詢</h2>
  <div class="meta">請輸入 Operator 權杖以讀取最新巡檢事件</div>
  <div>
    <label for="token">Operator 權杖：</label>
    <input type="password" id="token" placeholder="貼上 operator 權杖">
  </div>
  <div>
    <label for="device_id">設備編號（選填，例如 g06-1-d01）：</label>
    <input type="text" id="device_id" placeholder="留空顯示全部，或輸入設備編號篩選">
  </div>
  <button id="load_btn">讀取事件</button>
  <div id="status" style="margin-top: 10px; color: #666;"></div>
  <table>
    <thead>
      <tr>
        <th>Event ID</th>
        <th>Device ID</th>
        <th>Observed At</th>
        <th>Type</th>
        <th>Note</th>
        <th>Received At</th>
      </tr>
    </thead>
    <tbody id="events_tbody"></tbody>
  </table>
</div>
<script>
document.getElementById('load_btn').addEventListener('click', function() {
  const token = document.getElementById('token').value.trim();
  const deviceId = document.getElementById('device_id').value.trim();
  const statusEl = document.getElementById('status');
  const tbody = document.getElementById('events_tbody');
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  if (!token) {
    statusEl.textContent = '請先輸入 operator 權杖';
    return;
  }
  statusEl.textContent = '載入中...';
  let url = '/events';
  if (deviceId) {
    url += '?device_id=' + encodeURIComponent(deviceId);
  }
  fetch(url, {
    headers: { 'Authorization': 'Bearer ' + token }
  })
  .then(function(res) {
    if (!res.ok) {
      throw new Error('HTTP ' + res.status);
    }
    return res.json();
  })
  .then(function(items) {
    statusEl.textContent = '讀取成功，共 ' + items.length + ' 筆事件';
    items.forEach(function(ev) {
      const tr = document.createElement('tr');
      ['event_id', 'device_id', 'observed_at', 'type', 'note', 'received_at'].forEach(function(key) {
        const td = document.createElement('td');
        td.textContent = ev[key] != null ? String(ev[key]) : '';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  })
  .catch(function(err) {
    statusEl.textContent = '讀取失敗: ' + err.message;
  });
});
</script>
</body>
</html>
"""


class DatabaseManager:
    def __init__(self):
        self.conn = None
        self.configured = False
        self._init_db()

    def _init_db(self):
        host = os.environ.get("DB_HOST")
        password = os.environ.get("DB_PASSWORD")
        if not (host and password and HAVE_PSYCOPG2):
            return
        user = os.environ.get("DB_USER", "inspection")
        dbname = os.environ.get("DB_NAME", "inspection")
        port = int(os.environ.get("DB_PORT", "5432"))
        ca_path = Path("/etc/inspection/rds-ca.pem")

        conn_params = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "dbname": dbname,
            "connect_timeout": 3,
        }
        if ca_path.exists():
            conn_params["sslmode"] = "verify-full"
            conn_params["sslrootcert"] = str(ca_path)

        try:
            self.conn = psycopg2.connect(**conn_params)
            self.conn.autocommit = False
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        event_id VARCHAR(64) PRIMARY KEY,
                        device_id VARCHAR(32) NOT NULL,
                        observed_at TIMESTAMPTZ NOT NULL,
                        type VARCHAR(16) NOT NULL,
                        note VARCHAR(200),
                        received_at TIMESTAMPTZ NOT NULL
                    );
                    """
                )
            self.conn.commit()
            self.configured = True
        except Exception:
            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None
            self.configured = False

    def get_connection(self):
        if not self.configured or not self.conn:
            return None
        try:
            if self.conn.closed:
                self._init_db()
            return self.conn
        except Exception:
            return None

    def insert_event(self, event, received_at):
        conn = self.get_connection()
        if not conn:
            return None
        event_id = event["event_id"]
        dev_id = event["device_id"]
        obs_at = event["observed_at"]
        ev_type = event["type"]
        note = event.get("note")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO events (event_id, device_id, observed_at, type, note, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (event_id, dev_id, obs_at, ev_type, note, received_at)
                )
            conn.commit()
            return 201, {"status": "created", "event_id": event_id}
        except Exception:
            conn.rollback()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT device_id, observed_at, type, note
                        FROM events WHERE event_id = %s;
                        """,
                        (event_id,)
                    )
                    row = cur.fetchone()
                if row:
                    ex_dev, ex_obs, ex_type, ex_note = row
                    req_dt = datetime.fromisoformat(obs_at.replace("Z", "+00:00"))
                    same_obs = (ex_obs == req_dt)
                    same_content = (
                        ex_dev == dev_id and
                        ex_type == ev_type and
                        ex_note == note and
                        same_obs
                    )
                    if same_content:
                        return 200, {"status": "duplicate", "event_id": event_id}
                    return 409, {"error": "event_conflict", "field": "event_id"}
            except Exception:
                pass
            return 500, {"error": "database_error"}

    def query_events(self, device_id=None):
        conn = self.get_connection()
        if not conn:
            return None
        try:
            with conn.cursor() as cur:
                if device_id:
                    cur.execute(
                        """
                        SELECT event_id, device_id, observed_at, type, note, received_at
                        FROM events
                        WHERE device_id = %s
                        ORDER BY received_at DESC
                        LIMIT 50;
                        """,
                        (device_id,)
                    )
                else:
                    cur.execute(
                        """
                        SELECT event_id, device_id, observed_at, type, note, received_at
                        FROM events
                        ORDER BY received_at DESC
                        LIMIT 50;
                        """
                    )
                rows = cur.fetchall()
            result = []
            for r in rows:
                result.append({
                    "event_id": r[0],
                    "device_id": r[1],
                    "observed_at": r[2].isoformat() if hasattr(r[2], "isoformat") else str(r[2]),
                    "type": r[3],
                    "note": r[4],
                    "received_at": r[5].isoformat() if hasattr(r[5], "isoformat") else str(r[5])
                })
            return result
        except Exception:
            conn.rollback()
            return None

    def get_event(self, event_id):
        conn = self.get_connection()
        if not conn:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_id, device_id, observed_at, type, note, received_at
                    FROM events WHERE event_id = %s;
                    """,
                    (event_id,)
                )
                r = cur.fetchone()
            if not r:
                return False
            return {
                "event_id": r[0],
                "device_id": r[1],
                "observed_at": r[2].isoformat() if hasattr(r[2], "isoformat") else str(r[2]),
                "type": r[3],
                "note": r[4],
                "received_at": r[5].isoformat() if hasattr(r[5], "isoformat") else str(r[5])
            }
        except Exception:
            conn.rollback()
            return None


def validate_event_payload(body_bytes, content_type):
    if not (content_type and content_type.startswith("application/json")):
        return 400, {"error": "invalid_content_type", "field": "Content-Type"}
    if len(body_bytes) > 4096:
        return 400, {"error": "payload_too_large"}
    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return 400, {"error": "invalid_json"}

    if not isinstance(data, dict):
        return 400, {"error": "invalid_body"}

    allowed_fields = {"event_id", "device_id", "observed_at", "type", "note"}
    for k in data.keys():
        if k not in allowed_fields:
            return 400, {"error": "unexpected_field", "field": k}

    required_fields = ["event_id", "device_id", "observed_at", "type"]
    for k in required_fields:
        if k not in data or data[k] is None:
            return 400, {"error": "missing_field", "field": k}

    event_id = data["event_id"]
    if not (isinstance(event_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event_id)):
        return 400, {"error": "invalid_format", "field": "event_id"}

    device_id = data["device_id"]
    if not (isinstance(device_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", device_id)):
        return 400, {"error": "invalid_format", "field": "device_id"}

    ev_type = data["type"]
    if ev_type not in ("status", "anomaly", "test"):
        return 400, {"error": "invalid_type", "field": "type"}

    observed_at = data["observed_at"]
    if not isinstance(observed_at, str):
        return 400, {"error": "invalid_format", "field": "observed_at"}
    try:
        dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return 400, {"error": "missing_timezone", "field": "observed_at"}
    except Exception:
        return 400, {"error": "invalid_iso8601", "field": "observed_at"}

    if "note" in data:
        note = data["note"]
        if note is None or not isinstance(note, str) or len(note) > 200:
            return 400, {"error": "invalid_note", "field": "note"}

    return 200, data


def make_server(version_file, port=8080):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    db = DatabaseManager()
    memory_events = {}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, fmt, *args):
            pass  # Never log request paths, bodies, headers, query strings or secrets.

        def send_json(self, status_code, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def get_auth_role(self):
            reporter_token = os.environ.get("REPORTER_TOKEN")
            operator_token = os.environ.get("OPERATOR_TOKEN")
            auth_header = self.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return None
            token = auth_header[7:].strip()
            if reporter_token and token == reporter_token:
                return "reporter"
            if operator_token and token == operator_token:
                return "operator"
            return "invalid"

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == "/health":
                reporter_token = os.environ.get("REPORTER_TOKEN")
                operator_token = os.environ.get("OPERATOR_TOKEN")
                body = {
                    "status": "ok",
                    "service": "inspection",
                    "version": version,
                    "started_at": started,
                    "auth_configured": bool(reporter_token and operator_token),
                    "db_configured": db.configured
                }
                return self.send_json(200, body)

            if path == "/":
                data = HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/events" or path.startswith("/events/"):
                role = self.get_auth_role()
                if role is None or role == "invalid":
                    return self.send_json(401, {"error": "unauthorized"})
                if role != "operator":
                    return self.send_json(403, {"error": "forbidden"})

                if path == "/events":
                    qs = urllib.parse.parse_qs(parsed.query)
                    device_id = qs.get("device_id", [None])[0]
                    if db.configured:
                        events = db.query_events(device_id)
                        if events is not None:
                            return self.send_json(200, events)

                    matched = list(memory_events.values())
                    if device_id:
                        matched = [e for e in matched if e.get("device_id") == device_id]
                    matched.sort(key=lambda e: e.get("received_at", ""), reverse=True)
                    return self.send_json(200, matched[:50])

                event_id = path[len("/events/"):]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event_id):
                    return self.send_json(400, {"error": "invalid_format", "field": "event_id"})

                if db.configured:
                    ev = db.get_event(event_id)
                    if ev is not None:
                        if ev is False:
                            return self.send_json(404, {"error": "not_found"})
                        return self.send_json(200, ev)

                if event_id in memory_events:
                    return self.send_json(200, memory_events[event_id])
                return self.send_json(404, {"error": "not_found"})

            return self.send_json(404, {"error": "not_found"})

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/events":
                return self.send_json(404, {"error": "not_found"})

            role = self.get_auth_role()
            if role is None or role == "invalid":
                return self.send_json(401, {"error": "unauthorized"})
            if role != "reporter":
                return self.send_json(403, {"error": "forbidden"})

            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            content_type = self.headers.get("Content-Type", "")

            code, val_res = validate_event_payload(body_bytes, content_type)
            if code != 200:
                return self.send_json(code, val_res)

            event = val_res
            received_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

            if db.configured:
                res_code, res_body = db.insert_event(event, received_at)
                return self.send_json(res_code, res_body)

            event_id = event["event_id"]
            if event_id in memory_events:
                existing = memory_events[event_id]
                same_content = (
                    existing["device_id"] == event["device_id"] and
                    existing["type"] == event["type"] and
                    existing.get("note") == event.get("note") and
                    existing["observed_at"] == event["observed_at"]
                )
                if same_content:
                    return self.send_json(200, {"status": "duplicate", "event_id": event_id})
                return self.send_json(409, {"error": "event_conflict", "field": "event_id"})

            stored = dict(event)
            stored["received_at"] = received_at
            memory_events[event_id] = stored
            return self.send_json(201, {"status": "created", "event_id": event_id})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
