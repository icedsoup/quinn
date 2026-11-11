"""Run with Quinn Discord, bridge and ST stopped. Idempotent native-DM rollout."""
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
from native_store import initialize, connection


def migrate(root):
    root = Path(root).resolve()
    marker = root / 'QuinnBridge/bridge/native_migration.json'
    if marker.exists():
        return json.loads(marker.read_text())
    backup = root / 'backups' / ('native-migration-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    for relative in ['QuinnBridge/bridge/quinn_bridge.sqlite3',
                     'SillyTavern/data/default-user/settings.json',
                     'SillyTavern/data/default-user/extensions/quinn-bridge']:
        source = root / relative
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.exists() and source.suffix == '.sqlite3':
            with connection(source) as db, connection(target) as copy:
                db.backup(copy)
        elif source.exists():
            shutil.copy2(source, target)
    chats = root / 'SillyTavern/data/default-user/chats/Quinn'
    archived = []
    for source in chats.glob('discord_*.jsonl'):
        target = backup / 'archived_chats' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Verify both resolved paths before moving a private transcript.
        if not source.resolve().is_relative_to(chats.resolve()) or not target.resolve().is_relative_to(backup):
            raise ValueError('Unsafe archive path')
        shutil.move(str(source), str(target))
        archived.append(source.name)
    initialize(root / 'QuinnBridge/bridge/quinn_bridge.sqlite3')
    install = root / 'SillyTavern/data/default-user/extensions/quinn-bridge'
    install.mkdir(parents=True, exist_ok=True)
    for name in ('index.js', 'style.css', 'manifest.json'):
        shutil.copy2(root / 'put in tavern extensions/quinn-bridge' / name, install / name)
    result = dict(protocol=4, backup=str(backup), archived=archived)
    marker.write_text(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    print(json.dumps(migrate(Path(__file__).resolve().parents[2]), indent=2))
