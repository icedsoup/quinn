# Bridge maintenance

SillyTavern owns chat history. The bridge stores routing, jobs, and delivery state in SQLite. It listens on loopback and requires the token in `bridge_token.txt` for its API. The launcher manages the current extension and session. See the [main README](../README.md) for setup.

## Recovery

Pause the worker before editing chats. A processing claim expires after 90 seconds without a heartbeat. Saved job markers prevent a retry from appending the same message or generating a second completed reply. If a partial reply blocks recovery, pause the worker, remove the partial reply, and restart after the claim expires. If delivery is marked uncertain, check Discord before intervening. The bridge never automatically resends an uncertain delivery.

## Legacy migration

For an installation from before Native DMs, stop the bridge, SillyTavern, and Quinn Discord before running `python QuinnBridge/bridge/migrate_native.py`. The migration backs up the SQLite database, settings, and installed extension to `backups/native-migration-*`. It archives legacy `discord_*.jsonl` mirrors and leaves other chats intact. The local `native_migration.json` file records the backup and prevents a repeat migration. To roll back, stop all components and restore the recorded backup with the matching older source.

## Isolated browser tests

`test_browser.cjs` exercises real generation through SillyTavern and the local model. Point it at a disposable SillyTavern data directory on port 8001 and an isolated bridge on port 8766. The test creates, edits, and deletes chats. It does not contact Discord.

Set `QUINN_BRIDGE_DB`, `QUINN_BRIDGE_TOKEN_PATH`, and `QUINN_BRIDGE_PORT` when starting the isolated bridge. The browser script needs Playwright with Edge, `PLAYWRIGHT_MODULE`, `TEST_TOKEN`, and `TEST_CASES` as a JSON object of message arrays. `TEST_OUTPUT` optionally saves synthetic prompts and replies. Keep those outputs in `backups/`.
