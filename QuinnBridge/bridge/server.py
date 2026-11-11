from __future__ import annotations

import json
import os
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from native_store import connection, dispatch, initialize

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("QUINN_BRIDGE_DB", str(ROOT / "quinn_bridge.sqlite3")))
HOST = "127.0.0.1"
PORT = int(os.environ.get("QUINN_BRIDGE_PORT", "8765"))
TOKEN_PATH = Path(os.environ.get("QUINN_BRIDGE_TOKEN_PATH", str(ROOT / "bridge_token.txt")))

def load_token():
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return token


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def log(message):
    try:
        print(message)
    except (OSError, ValueError):
        pass


class BridgeServer(ThreadingHTTPServer):
    def server_bind(self):
        if os.name == "nt":
            # SO_REUSEADDR on Windows lets two listeners bind the same port.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = "QuinnLocalBridge/4"

    def log_message(self, fmt, *args):
        log("[bridge] " + fmt % args)

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

        log(
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
        expected = f"Bearer {self.server.token}"
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
        self.native_request('GET')

    def do_POST(self):
        self.native_request('POST')

    def native_request(self, method):
        if self.path == '/health':
            return self.send_json(200, {'ok': True, 'service': 'Quinn Native DM Bridge', 'version': '4'})
        if not self._require_auth():
            return
        try:
            code, result = dispatch(DB_PATH, method, self.path, self._read_json() if method == 'POST' else {})
            self.send_json(code, result)
        except ValueError as error:
            self.send_json(400, {'error': str(error)})
        except Exception as error:
            log(str(error))
            self.send_json(500, {'error': 'Bridge storage operation failed'})


def main():
    with BridgeServer((HOST, PORT), Handler) as server:
        server.token = load_token()
        initialize(DB_PATH)
        with connection(DB_PATH) as db:
            db.execute("DELETE FROM native_meta WHERE key='launcher'")
        log(f"Quinn bridge listening at http://{HOST}:{PORT}")
        log(f"Bridge token file: {TOKEN_PATH}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log("Stopping bridge.")


if __name__ == "__main__":
    main()
