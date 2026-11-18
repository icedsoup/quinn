"""Protocol 4: operational state only. ST owns conversation text/history."""
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse, parse_qs

BROWSER_LEASE = 15
JOB_DEADLINE = 90


@contextmanager
def connection(path):
    db = sqlite3.connect(path, timeout=15)
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    with connection(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS native_people (
          user_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0,
          route TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS native_jobs (
          id TEXT PRIMARY KEY, discord_id TEXT UNIQUE NOT NULL, user_id TEXT NOT NULL,
          epoch INTEGER NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
          claim TEXT, touched REAL, claimed_at REAL, reply TEXT, delivery_token TEXT,
          sent_id TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS native_meta (key TEXT PRIMARY KEY, value TEXT);
        ''')
        if 'claimed_at' not in {row[1] for row in db.execute('PRAGMA table_info(native_jobs)')}:
            db.execute('ALTER TABLE native_jobs ADD COLUMN claimed_at REAL')
        # Legacy records are never a history source or eligible for native claims.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone():
            db.execute("UPDATE jobs SET status='failed',error='Archived by native DM upgrade' WHERE status IN ('queued','processing')")
        db.execute("INSERT OR IGNORE INTO native_meta VALUES ('worker','Paused')")


def fail_processing(db, reason):
    db.execute("UPDATE native_jobs SET state='failed', claim=NULL, reply=NULL, error=?, touched=? WHERE state='processing'",
               (reason, time.time()))


def save_runtime(db, runtime):
    db.execute("INSERT OR REPLACE INTO native_meta VALUES ('launcher',?)", (json.dumps(runtime),))


def check_browser(db, runtime, now):
    if not runtime.get('autostart') or runtime['desired'] == 'close':
        return
    if runtime['browser'] in ('failed', 'left', 'closing', 'closed', 'blocked'):
        reason = runtime['error'] or 'The managed SillyTavern tab disconnected.'
    elif runtime.get('browser_seen') is not None and now - runtime['browser_seen'] > BROWSER_LEASE:
        reason = 'Firefox stopped responding to the bridge.'
    else:
        return
    runtime.update(desired='close', browser='failed', error=reason, failure=reason)
    fail_processing(db, reason)
    save_runtime(db, runtime)


def dispatch(path, method, url, data):
    parsed = urlparse(url)
    endpoint = parsed.path
    with connection(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        runtime_row = db.execute("SELECT value FROM native_meta WHERE key='launcher'").fetchone()
        runtime = json.loads(runtime_row[0]) if runtime_row else None
        now = time.time()
        if runtime:
            if runtime['desired'] != 'close' and now - runtime['heartbeat'] > BROWSER_LEASE:
                runtime['desired'] = 'close'
                fail_processing(db, 'Quinn launcher stopped responding.')
                save_runtime(db, runtime)
            check_browser(db, runtime, now)
            if runtime['autostart'] and runtime['desired'] != 'close':
                db.execute('''UPDATE native_jobs
                    SET state='failed', claim=NULL, reply=NULL, error=?, touched=?
                    WHERE state='processing' AND coalesce(claimed_at,touched)<?''',
                    (f'The Quinn job exceeded {JOB_DEADLINE} seconds.', now, now - JOB_DEADLINE))
        if endpoint == '/v1/launcher' and method == 'POST':
            operation = data.get('operation')
            if operation == 'create':
                if runtime and runtime['desired'] != 'close':
                    return 409, {'error': 'A launcher session is already active'}
                runtime = dict(session=secrets.token_urlsafe(24), desired='run', heartbeat=time.time(), started=time.time(),
                               autostart=bool(data.get('autostart')), browser='waiting', discord='waiting', error='')
            else:
                if not runtime or data.get('session') != runtime['session']:
                    return 409, {'error': 'Expired launcher session'}
                if operation == 'heartbeat':
                    if runtime['desired'] != 'close':
                        runtime['heartbeat'] = time.time()
                elif operation in ('drain', 'close'):
                    if operation == 'close':
                        fail_processing(db, 'Quinn stopped before the reply completed.')
                    if runtime['desired'] != 'close':
                        runtime['desired'] = operation
                elif operation == 'disconnect':
                    runtime['disconnect'] = True
                elif operation == 'report':
                    role = data.get('role')
                    if role not in ('browser', 'discord'):
                        raise ValueError('Invalid lifecycle role')
                    runtime[role] = str(data.get('state', ''))[:100]
                    if 'error' in data:
                        runtime['error'] = str(data['error'])[:500]
                    if role == 'browser':
                        runtime['browser_seen'] = now
                        check_browser(db, runtime, now)
                else:
                    raise ValueError('Invalid lifecycle operation')
            save_runtime(db, runtime)
            return 200, runtime
        if endpoint == '/v1/launcher' and method == 'GET':
            if not runtime or parse_qs(parsed.query).get('session') != [runtime['session']]:
                return 409, {'error': 'Expired launcher session'}
            active = db.execute("SELECT count(*) FROM native_jobs WHERE state IN ('processing','ready') OR (state='sending' AND touched>=?)", (runtime['started'],)).fetchone()[0]
            return 200, dict(runtime, active=active)
        if runtime and runtime['desired'] != 'run':
            if endpoint == '/v1/messages' and method == 'POST':
                return 503, {'error': 'Quinn is stopping'}
            if endpoint == '/v1/next' and method == 'GET':
                return 200, {'job_id': None}
        if endpoint == '/v1/pending' and method == 'GET':
            rows = db.execute("SELECT * FROM native_jobs WHERE state IN ('queued','processing','ready') ORDER BY rowid").fetchall()
            return 200, {'jobs': [dict(json.loads(r['payload']), job_id=r['id']) for r in rows]}
        if endpoint == '/v1/status' and method == 'GET':
            return 200, dict(pending=db.execute("SELECT count(*) FROM native_jobs WHERE state IN ('queued','processing')").fetchone()[0],
                             uncertain=db.execute("SELECT count(*) FROM native_jobs WHERE state='sending'").fetchone()[0])
        if endpoint == '/v1/messages' and method == 'POST':
            if data.get('channel_id') is not None:
                return 409, {'error': 'Native integration is DM-only'}
            for key in ('user_id', 'discord_message_id', 'display_name', 'message'):
                if not isinstance(data.get(key), str) or not data[key] or len(data[key]) > 20000:
                    raise ValueError('Missing or invalid ' + key)
            old = db.execute('SELECT id FROM native_jobs WHERE discord_id=?', (data['discord_message_id'],)).fetchone()
            if old:
                return 200, {'job_id': old['id']}
            uid = data['user_id']
            db.execute('INSERT OR IGNORE INTO native_people(user_id) VALUES (?)', (uid,))
            epoch = db.execute('SELECT epoch FROM native_people WHERE user_id=?', (uid,)).fetchone()[0]
            job = uuid.uuid4().hex
            db.execute('INSERT INTO native_jobs(id,discord_id,user_id,epoch,payload,state,touched) VALUES (?,?,?,?,?,?,?)',
                       (job, data['discord_message_id'], uid, epoch, json.dumps(data), 'queued', time.time()))
            return 202, {'job_id': job}
        if endpoint == '/v1/next' and method == 'GET':
            if parse_qs(parsed.query).get('worker') != ['4']:
                return 409, {'error': 'Reload the protocol 4 extension'}
            db.execute("UPDATE native_jobs SET state='queued',claim=NULL WHERE state='processing' AND touched<?", (time.time()-90,))
            if db.execute("SELECT 1 FROM native_jobs WHERE state='processing' LIMIT 1").fetchone():
                return 200, {'job_id': None}
            row = db.execute("SELECT * FROM native_jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return 200, {'job_id': None}
            claim = secrets.token_urlsafe(24)
            db.execute("UPDATE native_jobs SET state='processing',claim=?,touched=?,claimed_at=? WHERE id=?", (claim,now,now,row['id']))
            person = db.execute('SELECT * FROM native_people WHERE user_id=?', (row['user_id'],)).fetchone()
            return 200, dict(json.loads(row['payload']), job_id=row['id'], claim_token=claim,
                             epoch=row['epoch'], route=json.loads(person['route']))
        if endpoint.startswith('/v1/people/'):
            uid = endpoint.rsplit('/', 1)[1]
            person = db.execute('SELECT * FROM native_people WHERE user_id=?', (uid,)).fetchone()
            if method == 'GET':
                return (200, dict(person)) if person else (404, {'error':'Unknown person'})
            if not person:
                return 404, {'error':'Unknown person'}
            if data.get('operation') in ('reset','forget'):
                preserve = data.get('preserve_job') if data['operation']=='reset' else None
                if preserve:
                    current=db.execute("SELECT * FROM native_jobs WHERE id=? AND user_id=? AND state='processing'",(preserve,uid)).fetchone()
                    if not current or data.get('claim_token') != current['claim']:
                        return 409, {'error':'Invalid reset claim'}
                db.execute("UPDATE native_jobs SET state='canceled',reply=NULL,claim=NULL WHERE user_id=? AND state NOT IN ('sent','failed','canceled')", (uid,))
                db.execute("UPDATE native_people SET epoch=epoch+1,route='{}' WHERE user_id=?", (uid,))
                if preserve:
                    db.execute("UPDATE native_jobs SET state='processing',epoch=?,claim=? WHERE id=?",(person['epoch']+1,current['claim'],preserve))
                if data['operation']=='forget':
                    db.execute('DELETE FROM native_jobs WHERE user_id=?', (uid,))
                return 200, {'epoch': person['epoch']+1}
            if data.get('epoch') != person['epoch']:
                return 409, {'error':'Conversation was reset'}
            db.execute('UPDATE native_people SET route=? WHERE user_id=?', (json.dumps(data['route']),uid))
            return 200, {'ok':True}
        if endpoint.startswith('/v1/jobs/'):
            parts=endpoint.split('/')
            row=db.execute('SELECT * FROM native_jobs WHERE id=?',(parts[3],)).fetchone()
            if not row:
                return 404, {'error':'Unknown job'}
            if method=='GET':
                return 200, dict(status=row['state'],reply=row['reply'],error=row['error'])
            op=parts[4] if len(parts)>4 else ''
            if op in ('heartbeat','complete','fail'):
                if row['state']=='ready' and op=='complete' and data.get('claim_token')==row['claim']:
                    return 200, {'ok':True}
                if row['state']!='processing' or not secrets.compare_digest(str(data.get('claim_token','')),row['claim'] or ''):
                    return 409, {'error':'Expired claim'}
                if op=='heartbeat':
                    db.execute('UPDATE native_jobs SET touched=? WHERE id=?',(time.time(),row['id']))
                else:
                    reply=data.get('reply','')
                    if op=='complete' and (not isinstance(reply,str) or not reply.strip() or len(reply)>20000):
                        raise ValueError('Invalid reply')
                    db.execute('UPDATE native_jobs SET state=?,reply=?,error=?,touched=? WHERE id=?',
                               ('ready' if op=='complete' else 'failed',reply if op=='complete' else None,data.get('error'),time.time(),row['id']))
                return 200, {'ok':True}
            if op=='delivery':
                if row['state']!='ready':
                    return 409, {'error':'Delivery already started, canceled, or uncertain'}
                token=secrets.token_urlsafe(24)
                db.execute("UPDATE native_jobs SET state='sending',delivery_token=?,touched=? WHERE id=?",(token,time.time(),row['id']))
                return 200, {'reply':row['reply'],'delivery_token':token}
            if op=='ack':
                if row['state'] not in ('sending','sent') or data.get('delivery_token')!=row['delivery_token']:
                    return 409, {'error':'Invalid delivery acknowledgement'}
                db.execute("UPDATE native_jobs SET state='sent',sent_id=? WHERE id=?",(data['discord_message_id'],row['id']))
                return 200, {'ok':True}
        return 404, {'error':'Not found'}
