import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import socket
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'QuinnBridge/bridge'))
from native_store import connection, dispatch, initialize
from launcher import Component, InstanceLock, LaunchError, Launcher


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / 'state.sqlite3'
        initialize(self.db)
        self.runtime = self.post(operation='create', autostart=True)[1]
        self.session = self.runtime['session']

    def post(self, **body):
        return dispatch(self.db, 'POST', '/v1/launcher', body)

    def status(self):
        return dispatch(self.db, 'GET', '/v1/launcher?session=' + self.session, {})[1]

    def enqueue(self, identifier):
        code, body = dispatch(self.db, 'POST', '/v1/messages', dict(user_id='synthetic', discord_message_id=identifier, display_name='Test', message='hello'))
        self.assertEqual(code, 202)
        return body['job_id']

    def test_stale_sessions_and_duplicate_create_are_rejected(self):
        self.assertEqual(self.post(operation='create')[0], 409)
        self.assertEqual(self.post(operation='heartbeat', session='old')[0], 409)
        self.assertEqual(dispatch(self.db, 'GET', '/v1/launcher?session=old', {})[0], 409)

    def test_drain_fences_new_work_and_preserves_queue(self):
        self.enqueue('one')
        self.enqueue('two')
        job = dispatch(self.db, 'GET', '/v1/next?worker=4', {})[1]
        self.post(operation='drain', session=self.session)
        self.assertIsNone(dispatch(self.db, 'GET', '/v1/next?worker=4', {})[1]['job_id'])
        self.assertEqual(dispatch(self.db, 'POST', '/v1/messages', {})[0], 503)
        self.assertEqual(self.status()['active'], 1)
        with connection(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM native_jobs WHERE state='queued'").fetchone()[0], 1)
            db.execute("UPDATE native_jobs SET state='ready',reply='hello' WHERE id=?", (job['job_id'],))
        delivery = dispatch(self.db, 'POST', '/v1/jobs/' + job['job_id'] + '/delivery', {})[1]
        self.assertEqual(self.status()['active'], 1)
        dispatch(self.db, 'POST', '/v1/jobs/' + job['job_id'] + '/ack', dict(delivery_token=delivery['delivery_token'], discord_message_id='sent'))
        self.assertEqual(self.status()['active'], 0)

    def test_expired_lease_cannot_be_revived(self):
        with connection(self.db) as db:
            state = dict(self.runtime, heartbeat=time.time() - 16)
            db.execute("UPDATE native_meta SET value=? WHERE key='launcher'", (json.dumps(state),))
        self.assertEqual(self.status()['desired'], 'close')
        self.post(operation='heartbeat', session=self.session)
        self.assertEqual(self.status()['desired'], 'close')

    def test_reports_and_new_session_after_close(self):
        self.post(operation='report', session=self.session, role='browser', state='failed', error='Missing character')
        self.assertEqual(self.status()['error'], 'Missing character')
        self.post(operation='close', session=self.session)
        replacement = self.post(operation='create', autostart=False)[1]
        self.assertNotEqual(replacement['session'], self.session)
        self.assertFalse(replacement['autostart'])


class ProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows job lifetime')
    def test_launcher_death_closes_its_job(self):
        import win32api
        import win32event
        with tempfile.TemporaryDirectory(prefix='Quinn crash test ') as folder:
            target = Path(folder) / 'owned.pid'
            script = "from launcher import Component; import sys,time; from pathlib import Path; c=Component('owned',lambda *_:None); c.start([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(str(c.proc.pid)); time.sleep(60)"
            parent = subprocess.Popen([sys.executable, '-c', script, str(target)], cwd=ROOT,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not target.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(target.exists())
                owned = win32api.OpenProcess(0x100000, False, int(target.read_text()))
                self.addCleanup(owned.Close)
                parent.kill()
                parent.wait(timeout=3)
                self.assertEqual(win32event.WaitForSingleObject(owned, 2000), 0)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=3)

    def test_occupied_port_is_reported_without_taking_ownership(self):
        launcher = Launcher()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            launcher.bridge_url = f'http://127.0.0.1:{listener.getsockname()[1]}'
            launcher.tavern_url = 'http://127.0.0.1:8000'
            with self.assertRaisesRegex(LaunchError, 'independently launched'):
                launcher.check_ports(False)
            self.assertGreater(listener.fileno(), 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows batch entry point')
    def test_batch_opens_launcher_and_q_exits(self):
        result = subprocess.run(f'cmd /d /s /c ""{ROOT / "Start Quinn.bat"}""', input='q\n', text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_isolated_bridge_requires_authentication(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            token_file = Path(folder) / 'token.txt'
            component = Component('bridge', lambda *_: None)
            self.addCleanup(component.stop)
            component.start([sys.executable, '-u', str(ROOT / 'QuinnBridge/bridge/server.py')], env=dict(os.environ,
                QUINN_BRIDGE_PORT=str(port), QUINN_BRIDGE_DB=str(Path(folder) / 'isolated.sqlite3'), QUINN_BRIDGE_TOKEN_PATH=str(token_file)))
            base = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(base + '/health', timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + '/v1/launcher', timeout=1)
            self.assertEqual(denied.exception.code, 401)
            request = urllib.request.Request(base + '/v1/launcher', data=b'{"operation":"create","autostart":true}', headers={
                'Authorization': 'Bearer ' + token_file.read_text(), 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=1) as response:
                self.assertTrue(json.load(response)['autostart'])
            component.stop()

    @unittest.skipUnless(os.name == 'nt', 'Windows ownership')
    def test_duplicate_lock_and_release(self):
        with InstanceLock():
            with self.assertRaises(LaunchError):
                with InstanceLock():
                    pass
        with InstanceLock():
            pass

    @unittest.skipUnless(os.name == 'nt', 'Windows ownership')
    def test_hidden_process_and_descendant_are_stopped(self):
        import win32api
        import win32event
        with tempfile.TemporaryDirectory(prefix='Quinn process test ') as folder:
            target = Path(folder) / 'child.pid'
            script = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(str(p.pid)); print('ready',flush=True); time.sleep(60)"
            component = Component('test', lambda *_: None)
            self.addCleanup(component.stop)
            component.start([sys.executable, '-u', '-c', script, str(target)])
            deadline = time.monotonic() + 5
            while not target.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(target.exists())
            child = win32api.OpenProcess(0x100000, False, int(target.read_text()))
            self.addCleanup(child.Close)
            component.stop()
            self.assertEqual(win32event.WaitForSingleObject(child, 2000), 0)

    def test_stop_during_startup_sets_cancellation(self):
        launcher = Launcher()
        launcher.operation = 'starting'
        class Worker:
            def is_alive(self):
                return True
        launcher.worker = Worker()
        launcher.stop_requested()
        self.assertTrue(launcher.cancel.is_set())

    def test_second_stop_forces_shutdown(self):
        launcher = Launcher()
        launcher.operation = 'stopping'
        launcher.stop_requested()
        self.assertTrue(launcher.force.is_set())

    def test_startup_failure_rolls_back_only_new_components(self):
        launcher = Launcher()
        with patch.object(launcher, 'configure', side_effect=LaunchError('missing model')):
            launcher.startup()
        self.assertTrue(all(not component.alive for component in launcher.components.values()))
        self.assertTrue(any('missing model' in line for line in launcher.logs))

    def test_shutdown_drains_before_disconnect_and_cleanup(self):
        launcher = Launcher()
        launcher.session = 'synthetic'
        stopped = []
        launcher.components = {name: Mock(alive=name == 'bridge', stop=lambda name=name: stopped.append(name))
                               for name in ('discord', 'tavern', 'kobold', 'bridge', 'sniper')}
        launcher.control = Mock()
        launcher.status = Mock(side_effect=[
            dict(active=1, browser='busy', discord='ready'),
            dict(active=0, browser='ready', discord='ready'),
        ])
        launcher.close_tab = Mock()
        launcher.shutdown()
        self.assertEqual([call.args[0] for call in launcher.control.call_args_list], ['drain', 'disconnect'])
        self.assertEqual(launcher.status.call_count, 2)
        self.assertLess(stopped.index('discord'), stopped.index('tavern'))
        self.assertLess(stopped.index('kobold'), stopped.index('bridge'))
        self.assertIsNone(launcher.session)


if __name__ == '__main__':
    unittest.main()
