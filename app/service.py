#!/usr/bin/env python3
"""Inspection service with event persistence, authentication, idempotency and filtering."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import sys
import threading
import urllib.parse

try:
    import psycopg2
    HAVE_PSYCOPG2 = True
except ImportError:
    HAVE_PSYCOPG2 = False

try:
    import w09_media
except ImportError:  # 以檔案路徑載入時（測試）app 目錄不在 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import w09_media

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


def locked(method):
    """ThreadingHTTPServer 會並行呼叫；單一連線的資料庫操作一次只讓一個執行緒進行。"""
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper


class DatabaseManager:
    def __init__(self):
        self.conn = None
        self.configured = False
        self.lock = threading.RLock()
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
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_photos (
                        event_id VARCHAR(64) PRIMARY KEY REFERENCES events(event_id),
                        content_type VARCHAR(32) NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        sha256 CHAR(64) NOT NULL,
                        width INTEGER NOT NULL,
                        height INTEGER NOT NULL,
                        data BYTEA NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
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

    @locked
    def put_photo(self, event_id, jpeg, sha, width, height, created_at):
        """Return 'created' | 'duplicate' | 'conflict' | None(database error)."""
        conn = self.get_connection()
        if not conn:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO event_photos (event_id, content_type, size_bytes, sha256, width, height, data, created_at)
                    VALUES (%s, 'image/jpeg', %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING RETURNING event_id;
                    """,
                    (event_id, len(jpeg), sha, width, height, psycopg2.Binary(jpeg), created_at),
                )
                inserted = cur.fetchone() is not None
                existing = None
                if not inserted:
                    cur.execute("SELECT sha256 FROM event_photos WHERE event_id = %s;", (event_id,))
                    row = cur.fetchone()
                    existing = row[0].strip() if row else None
            conn.commit()
            if inserted:
                return "created"
            return "duplicate" if existing == sha else "conflict"
        except Exception:
            conn.rollback()
            return None

    @locked
    def photo_meta(self, event_id):
        """dict | False(no photo) | None(database error)."""
        conn = self.get_connection()
        if not conn:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT sha256, size_bytes FROM event_photos WHERE event_id = %s;", (event_id,))
                row = cur.fetchone()
            conn.commit()
            return {"sha256": row[0].strip(), "size_bytes": row[1]} if row else False
        except Exception:
            conn.rollback()
            return None

    @locked
    def photo_data(self, event_id):
        """(bytes, sha256) | False | None(database error)."""
        conn = self.get_connection()
        if not conn:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data, sha256 FROM event_photos WHERE event_id = %s;", (event_id,))
                row = cur.fetchone()
            conn.commit()
            return (bytes(row[0]), row[1].strip()) if row else False
        except Exception:
            conn.rollback()
            return None

    @locked
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

    @locked
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

    @locked
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
    memory_photos = {}

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
            device_token = os.environ.get("W09_DEVICE_TOKEN")
            auth_header = self.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return None
            token = auth_header[7:].strip()
            def same(expected):
                return bool(expected) and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
            if same(reporter_token):
                return "reporter"
            if same(operator_token):
                return "operator"
            if same(device_token):
                return "device"
            return "invalid"

        # ---- W9：裝置收據、原圖上傳、授權讀回 ----
        def w09_authorize(self, want):
            role = self.get_auth_role()
            if role is None or role == "invalid":
                self.send_json(401, {"error": "unauthorized"})
                return False
            if role != want:
                self.send_json(403, {"error": "forbidden"})
                return False
            return True

        def w09_event_for(self, event_id, device_scoped):
            """Return the event dict, or None after an error response was sent."""
            if not w09_media.EVENT_ID_RE.fullmatch(event_id):
                self.send_json(400, {"error": "invalid_format", "field": "event_id"})
                return None
            if db.configured:
                ev = db.get_event(event_id)
                if ev is None:
                    self.send_json(503, {"error": "database_unavailable"})
                    return None
            else:
                ev = memory_events.get(event_id, False)
            if ev is False:
                self.send_json(404, {"error": "not_found"})
                return None
            expected = os.environ.get("W09_DEVICE_ID")
            if device_scoped and expected and ev["device_id"] != expected:
                self.send_json(403, {"error": "forbidden"})   # 事件不屬於這個裝置身分
                return None
            return ev

        def w09_receipt(self, event_id):
            if not self.w09_authorize("device"):
                return
            ev = self.w09_event_for(event_id, True)
            if ev is None:
                return
            if db.configured:
                meta = db.photo_meta(event_id)
                if meta is None:
                    return self.send_json(503, {"error": "database_unavailable"})
            else:
                meta = memory_photos.get(event_id)
                meta = {"sha256": meta["sha256"], "size_bytes": len(meta["data"])} if meta else False
            self.send_json(200, {
                "event_id": ev["event_id"],
                "device_id": ev["device_id"],
                "event_stored": True,
                "event_fingerprint": w09_media.event_fingerprint(
                    ev["device_id"], ev["observed_at"], ev["type"], ev.get("note")),
                "photo_state": "ready" if meta else "none",
                "photo_sha256": meta["sha256"] if meta else None,
                "photo_bytes": meta["size_bytes"] if meta else None,
            })

        def w09_upload(self, event_id):
            if not self.w09_authorize("device"):
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "image/jpeg":
                return self.send_json(415, {"error": "unsupported_media_type"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.send_json(400, {"error": "length_required"})
            if length <= 0:
                return self.send_json(400, {"error": "empty_body"})
            if length > w09_media.MAX_PHOTO_BYTES:
                self.close_connection = True      # 不讀超大內容
                return self.send_json(413, {"error": "payload_too_large"})
            data = self.rfile.read(length)
            if len(data) != length:
                return self.send_json(400, {"error": "incomplete_body"})
            dims = w09_media.jpeg_dimensions(data)
            if dims is None:
                return self.send_json(400, {"error": "invalid_jpeg"})
            if dims[0] * dims[1] > w09_media.MAX_PIXELS:
                return self.send_json(413, {"error": "too_many_pixels"})
            ev = self.w09_event_for(event_id, True)    # 事件不存在 → 404，不建立孤兒關聯
            if ev is None:
                return
            sha = w09_media.photo_sha256(data)
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            if db.configured:
                outcome = db.put_photo(event_id, data, sha, dims[0], dims[1], created_at)
                if outcome is None:
                    return self.send_json(503, {"error": "database_unavailable"})
            elif event_id not in memory_photos:
                memory_photos[event_id] = {"data": data, "sha256": sha}
                outcome = "created"
            else:
                outcome = "duplicate" if memory_photos[event_id]["sha256"] == sha else "conflict"
            if outcome == "conflict":
                return self.send_json(409, {"error": "photo_conflict", "field": "event_id"})
            body = {"status": outcome, "event_id": event_id, "photo_sha256": sha}
            return self.send_json(201 if outcome == "created" else 200, body)

        def w09_read_photo(self, event_id):
            if not self.w09_authorize("operator"):
                return
            if not w09_media.EVENT_ID_RE.fullmatch(event_id):
                return self.send_json(400, {"error": "invalid_format", "field": "event_id"})
            if db.configured:
                found = db.photo_data(event_id)
                if found is None:
                    return self.send_json(503, {"error": "database_unavailable"})
            else:
                mem = memory_photos.get(event_id)
                found = (mem["data"], mem["sha256"]) if mem else False
            if found is False:
                return self.send_json(404, {"error": "not_found"})
            data, sha = found
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-SHA256", sha)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

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
                    "db_configured": db.configured,
                    "w09_configured": bool(os.environ.get("W09_DEVICE_TOKEN"))
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

            m = re.fullmatch(r"/w09/device/events/([^/]+)", path)
            if m:
                return self.w09_receipt(m.group(1))
            m = re.fullmatch(r"/w09/events/([^/]+)/photo", path)
            if m:
                return self.w09_read_photo(m.group(1))

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
            m = re.fullmatch(r"/w09/device/events/([^/]+)/photo", parsed.path)
            if m:
                return self.w09_upload(m.group(1))
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
