import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN = (ROOT / "bridge_token.txt").read_text(encoding="utf-8").strip()
BASE = "http://127.0.0.1:8765"

if len(sys.argv) < 2:
    print('Usage: python test_message.py "Hello Quinn" [user-id] [display name]')
    raise SystemExit(2)

message = sys.argv[1]
user_id = sys.argv[2] if len(sys.argv) > 2 else "test-contact-1"
display_name = sys.argv[3] if len(sys.argv) > 3 else "Test Contact"
body = json.dumps({
    "user_id": user_id,
    "display_name": display_name,
    "message": message
}).encode("utf-8")
req = urllib.request.Request(
    BASE + "/v1/messages",
    data=body,
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    method="POST"
)
with urllib.request.urlopen(req, timeout=5) as response:
    print(response.read().decode("utf-8"))
