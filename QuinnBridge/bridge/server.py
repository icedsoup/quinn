from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "quinn_bridge.sqlite3"
HOST = "127.0.0.1"
PORT = int(os.environ.get("QUINN_BRIDGE_PORT", "8765"))
TOKEN_PATH = ROOT / "bridge_token.txt"

# A job that remains processing longer than this is considered abandoned.
# Keep this conservative to avoid reclaiming a genuinely slow generation.
PROCESSING_TIMEOUT_SECONDS = 60 * 60

if TOKEN_PATH.exists():
    TOKEN = TOKEN_PATH.read_text(encoding="utf-8").strip()
else:
    TOKEN = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(TOKEN, encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                avatar_url TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                reply TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                completed_at REAL,
                processing_at REAL
            )
        """)

        # Upgrade existing databases without deleting any existing data.
        columns = {
            row["name"]
            for row in con.execute("PRAGMA table_info(jobs)").fetchall()
        }

        if "processing_at" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN processing_at REAL")

        if "avatar_url" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''")

        con.execute("""
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created "
            "ON jobs(status, created_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_user_id "
            "ON turns(user_id, id)"
        )


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "QuinnLocalBridge/0.2"

    def log_message(self, fmt, *args):
        print("[bridge]", fmt % args)

    def _allowed_origin(self):
        origin = self.headers.get("Origin", "")

        try:
            parsed = urlparse(origin)
            return (
                parsed.scheme == "http"
                and parsed.hostname in ("localhost", "127.0.0.1")
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            return False

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if self._allowed_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        requested_headers = self.headers.get(
            "Access-Control-Request-Headers", ""
        )

        print(
            f"[bridge] CORS preflight: origin={origin!r}, "
            f"requested_headers={requested_headers!r}, "
            f"allowed={self._allowed_origin()}"
        )

        self.send_response(204)

        if self._allowed_origin():
            self.send_header("Vary", "Origin")
            self.send_header(
                "Access-Control-Allow-Methods",
                "GET, POST, OPTIONS",
            )

            if requested_headers:
                self.send_header(
                    "Access-Control-Allow-Headers",
                    requested_headers,
                )

            self.send_header("Access-Control-Max-Age", "600")

        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self):
        self._cors()
        super().end_headers()

    def send_json(self, code, payload):
        body = json_bytes(payload)
        self.send_response(code)
        self.send_header(
            "Content-Type", "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_empty(self, code=204):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self):
        expected = f"Bearer {TOKEN}"
        actual = self.headers.get("Authorization", "")
        return secrets.compare_digest(actual, expected)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc

        if length <= 0 or length > 1_000_000:
            raise ValueError("Expected a JSON body smaller than 1 MB.")

        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))

        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object.")

        return data

    def _require_auth(self):
        if not self._authorized():
            self.send_json(401, {
                "error": (
                    "Unauthorized. Set the local bridge token "
                    "in the SillyTavern extension."
                )
            })
            return False

        return True

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self.send_json(200, {
                "ok": True,
                "service": "Quinn Local Bridge",
                "version": "0.2.0",
            })
            return

        if not self._require_auth():
            return

        if path == "/v1/next":
            now = time.time()
            stale_before = now - PROCESSING_TIMEOUT_SECONDS

            with connect_db() as con:
                con.execute("BEGIN IMMEDIATE")

                # Recover abandoned claims after a long timeout.
                recovered = con.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued',
                        processing_at = NULL,
                        error = 'Recovered after processing timeout.'
                    WHERE status = 'processing'
                      AND processing_at IS NOT NULL
                      AND processing_at < ?
                    """,
                    (stale_before,),
                ).rowcount

                if recovered:
                    print(
                        f"[bridge] Requeued {recovered} stale job(s)."
                    )

                row = con.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'queued'
                    ORDER BY created_at, rowid
                    LIMIT 1
                    """
                ).fetchone()

                if row is None:
                    con.commit()
                    self.send_json(200, {"job_id": None})
                    return

                con.execute(
                    """
                    UPDATE jobs
                    SET status = 'processing',
                        processing_at = ?,
                        error = NULL
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (now, row["job_id"]),
                )

                history_rows = con.execute(
                    """
                    SELECT role, content FROM turns
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT 21
                    """,
                    (row["user_id"],),
                ).fetchall()

                con.commit()

            history = [
                {"role": item["role"], "content": item["content"]}
                for item in reversed(history_rows)
            ]

            # The current message is sent separately when it is the
            # latest user turn, to avoid including it twice.
            if (
                history
                and history[-1]["role"] == "user"
                and history[-1]["content"] == row["message"]
            ):
                history.pop()

            history = history[-20:]

            self.send_json(200, {
                "job_id"      : row["job_id"],
                "user_id"     : row["user_id"],
                "display_name": row["display_name"],
                "avatar_url"  : row["avatar_url"] or "",
                "message"     : row["message"],
                "history"     : history,
            })
            return

        if path.startswith("/v1/jobs/"):
            job_id = unquote(path[len("/v1/jobs/"):])

            with connect_db() as con:
                row = con.execute(
                    "SELECT * FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()

            if not row:
                self.send_json(404, {"error": "Unknown job id"})
                return

            self.send_json(200, {
                "job_id": row["job_id"],
                "status": row["status"],
                "reply": row["reply"],
                "error": row["error"],
            })
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if not self._require_auth():
            return

        try:
            data = self._read_json()
        except (
            ValueError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            self.send_json(400, {"error": str(exc)})
            return

        if path == "/v1/messages":
            user_id = str(data.get("user_id", "")).strip()
            display_name = (
                str(data.get("display_name", "Contact")).strip()[:100]
                or "Contact"
            )
            avatar_url = str(data.get("avatar_url", "")).strip()[:512]
            message = str(data.get("message", "")).strip()

            if not user_id or len(user_id) > 128:
                self.send_json(400, {
                    "error": (
                        "user_id is required and must be "
                        "at most 128 characters"
                    )
                })
                return

            if not message:
                self.send_json(400, {
                    "error": "message must not be empty"
                })
                return

            if len(message) > 12000:
                self.send_json(413, {
                    "error": "message exceeds 12,000 characters"
                })
                return

            job_id = uuid.uuid4().hex
            now = time.time()

            with connect_db() as con:
                con.execute(
                    """
                    INSERT INTO jobs(
                        job_id, user_id, display_name, avatar_url, message,
                        status, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'queued', ?)
                    """,
                    (job_id, user_id, display_name, avatar_url, message, now),
                )

                con.execute(
                    """
                    INSERT INTO turns(
                        user_id, role, content, created_at
                    )
                    VALUES (?, 'user', ?, ?)
                    """,
                    (user_id, message, now),
                )

            self.send_json(202, {
                "job_id": job_id,
                "status": "queued",
            })
            return

        if path.startswith("/v1/jobs/") and path.endswith("/complete"):
            job_id = unquote(
                path[len("/v1/jobs/"):-len("/complete")].strip("/")
            )
            reply = str(data.get("reply", "")).strip()

            if not reply:
                self.send_json(400, {
                    "error": "reply must not be empty"
                })
                return

            if len(reply) > 20000:
                self.send_json(413, {
                    "error": "reply exceeds 20,000 characters"
                })
                return

            now = time.time()

            with connect_db() as con:
                con.execute("BEGIN IMMEDIATE")

                row = con.execute(
                    "SELECT user_id, status FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()

                if not row:
                    con.rollback()
                    self.send_json(404, {"error": "Unknown job id"})
                    return

                if row["status"] != "processing":
                    con.rollback()
                    self.send_json(409, {
                        "error": (
                            f"Job is {row['status']}, not processing"
                        )
                    })
                    return

                con.execute(
                    """
                    UPDATE jobs
                    SET status = 'complete',
                        reply = ?,
                        error = NULL,
                        completed_at = ?,
                        processing_at = NULL
                    WHERE job_id = ? AND status = 'processing'
                    """,
                    (reply, now, job_id),
                )

                con.execute(
                    """
                    INSERT INTO turns(
                        user_id, role, content, created_at
                    )
                    VALUES (?, 'assistant', ?, ?)
                    """,
                    (row["user_id"], reply, now),
                )

                con.commit()

            self.send_json(200, {
                "ok": True,
                "job_id": job_id,
                "status": "complete",
            })
            return

        if path.startswith("/v1/jobs/") and path.endswith("/fail"):
            job_id = unquote(
                path[len("/v1/jobs/"):-len("/fail")].strip("/")
            )
            error = str(data.get("error", "Generation failed")).strip()
            error = error[:2000] or "Generation failed"

            with connect_db() as con:
                con.execute("BEGIN IMMEDIATE")

                row = con.execute(
                    "SELECT status FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()

                if not row:
                    con.rollback()
                    self.send_json(404, {"error": "Unknown job id"})
                    return

                if row["status"] != "processing":
                    con.rollback()
                    self.send_json(409, {
                        "error": (
                            f"Job is {row['status']}, not processing"
                        )
                    })
                    return

                con.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed',
                        error = ?,
                        processing_at = NULL
                    WHERE job_id = ? AND status = 'processing'
                    """,
                    (error, job_id),
                )

                con.commit()

            print(f"[bridge] Job {job_id} failed: {error}")
            self.send_json(200, {
                "ok": True,
                "job_id": job_id,
                "status": "failed",
            })
            return

        if path.startswith("/v1/jobs/") and path.endswith("/retry"):
            job_id = unquote(
                path[len("/v1/jobs/"):-len("/retry")].strip("/")
            )

            with connect_db() as con:
                con.execute("BEGIN IMMEDIATE")

                row = con.execute(
                    "SELECT status FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()

                if not row:
                    con.rollback()
                    self.send_json(404, {"error": "Unknown job id"})
                    return

                if row["status"] != "failed":
                    con.rollback()
                    self.send_json(409, {
                        "error": "Only failed jobs can be retried"
                    })
                    return

                con.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued',
                        error = NULL,
                        reply = NULL,
                        completed_at = NULL,
                        processing_at = NULL
                    WHERE job_id = ? AND status = 'failed'
                    """,
                    (job_id,),
                )

                con.commit()

            self.send_json(200, {
                "ok": True,
                "job_id": job_id,
                "status": "queued",
            })
            return

        self.send_json(404, {"error": "Not found"})


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    print(f"Quinn Local Bridge listening at http://{HOST}:{PORT}")
    print(f"Token saved in: {TOKEN_PATH}")
    print(
        "Local-only server. Do not expose this port "
        "to your network or the internet."
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping bridge.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()