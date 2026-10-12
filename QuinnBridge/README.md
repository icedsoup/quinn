# Quinn Bridge — local prototype

This prototype intentionally contains **no Discord client, OAuth flow, bot token handling, or Discord automation**.
It provides a local queue/API and a SillyTavern UI extension so the generation path can be tested first.

## What it does

- Local-only HTTP server bound to `127.0.0.1:8765`.
- Token-protected local API.
- SQLite queue and per-user conversation histories.
- One generation at a time in the SillyTavern tab.
- Uses SillyTavern's active model connection through `generateRaw()`.
- Does not add these generated messages to the normal SillyTavern chat.
- No browser history or cross-contact history is deliberately included in a prompt.

## Important limitations

- This is an early prototype, not production-ready. The updated extension uses `window.SillyTavern.getContext()` and dynamically imports `/script.js` for `generateRaw()` to avoid fragile relative imports from a user-scoped extension folder.
- The character card's description/personality/scenario/examples are included. Lorebook entries, author's note, regex extensions, and other chat-context features are not automatically reconstructed.
- Long-term semantic memory retrieval is not implemented yet; recent turns are retained per user in SQLite.
- The current message is stored as a user turn when queued. If generation fails, that turn remains in the conversation history.
- Keep the bridge local. Do not port-forward or expose it to your LAN or the internet.
- SillyTavern's extension API can change; test after SillyTavern updates.

## 1. Install the SillyTavern extension

1. Stop SillyTavern before changing files.
2. Find your SillyTavern repository folder (the folder containing `start.bat`).
3. Find the active user folder under `data/` (often `data/default-user/`).
4. Create `data/default-user/extensions/quinn-bridge/`.
5. Copy `manifest.json`, `index.js`, and `style.css` from this package's `sillytavern-extension/` folder into that new folder.
6. Start SillyTavern and reload its browser tab.
7. Select Quinn's character card, open Extensions, and look for **Quinn Bridge (local testing)**.

If your active user is not `default-user`, use that user folder instead. Do not put the extension inside the `public/scripts/extensions/third-party/` folder for this user-scoped install.

## 2. Start the local bridge

Requires Python 3.10+; no third-party Python packages are needed.

1. Open a terminal in the `bridge/` folder.
2. Run `python server.py`.
3. On first run, the bridge creates `bridge_token.txt` and `quinn_bridge.sqlite3`.
4. Paste the contents of `bridge_token.txt` into the SillyTavern extension's token field and click **Save token**.
5. Click **Test connection**, then **Start processing**.

Keep the SillyTavern tab open while processing. If SillyTavern runs on a port other than 8000 or 8001, update the allowed local origins in `server.py` to match the exact origin shown in your browser address bar.

## 3. Submit a test message (no Discord required)

With the bridge running, open a second terminal in the `bridge/` folder:

```powershell
python test_message.py "Hi Quinn, this is a local test."
```

The extension should pick up the queued job and generate a reply. Try another message from the same test contact to verify conversational continuity:

```powershell
python test_message.py "What did I just tell you?" test-contact-1 "Test Contact"
```

Try a different user ID to verify isolation:

```powershell
python test_message.py "Hello, new conversation." test-contact-2 "Another Contact"
```

## API for your later Discord integration

Base URL: `http://127.0.0.1:8765`

Every endpoint except `/health` requires:
`Authorization: Bearer <contents of bridge_token.txt>`

- `GET /health` — health check.
- `POST /v1/messages` — JSON: `{"user_id":"stable-id","display_name":"Name","message":"Text"}`. Returns a job ID.
- `GET /v1/jobs/<job_id>` — poll job status and read `reply` when complete.
- The SillyTavern extension consumes queued jobs using `GET /v1/next` and reports output using `POST /v1/jobs/<job_id>/complete`.

Your later Discord-side code should use a stable user ID as `user_id`, not the display name, so users keep separate histories when they change nicknames.

## Stop / reset

- Stop the Python server with Ctrl+C.
- Pause the extension with its Pause button.
- To reset all stored conversations and queued jobs, stop the server and delete `quinn_bridge.sqlite3`. This permanently removes that local history.
