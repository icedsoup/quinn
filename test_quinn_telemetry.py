import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import quinn_discord

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'QuinnBridge/bridge'))
spec = importlib.util.spec_from_file_location('telemetry_bridge', ROOT / 'QuinnBridge/bridge/server.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / 'bridge.sqlite3'
        bridge.initialize(self.db)
        self.patch = patch.object(bridge, 'DB_PATH', self.db)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.events = []
        self.emitter = patch.object(bridge, 'emit', side_effect=lambda *args, **kwargs: self.events.append((args, kwargs)))
        self.emitter.start()
        self.addCleanup(self.emitter.stop)
        self.server = bridge.BridgeServer(('127.0.0.1', 0), bridge.Handler)
        self.server.token = 'test-only-token'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def request(self, path, data=None, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer test-only-token'
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_browser_metrics_require_authentication_and_validate_measurements(self):
        event = dict(trace='test-job', stage='inference', status='info', detail='Receiving generated text', metrics={'output_chars': 21, 'elapsed_ms': 1250})
        self.assertEqual(self.request('/v1/telemetry', event, False)[0], 401)
        self.assertFalse(self.events)
        self.assertEqual(self.request('/v1/telemetry', event)[0], 200)
        self.assertEqual(self.events[-1][1]['output_chars'], 21)
        self.assertEqual(self.events[-1][1]['trace'], 'test-job')
        for metrics in ({'output_chars': -1}, {'elapsed_ms': float('nan')}, {'prompt': 'hidden'}, {'output_chars': True}):
            self.assertEqual(self.request('/v1/telemetry', dict(event, metrics=metrics))[0], 400)
        self.assertEqual(len(self.events), 1)

    def test_only_accepted_and_claimed_work_advances_pipeline(self):
        payload = dict(user_id='fixture', display_name='Fixture', message='hello', discord_message_id='test-message')
        code, result = self.request('/v1/messages', payload)
        self.assertEqual(code, 202)
        self.assertEqual(self.events[-1][0][1:3], ('intake', 'complete'))
        self.assertEqual(self.events[-1][1]['input_chars'], 5)
        self.request('/v1/messages', payload)
        self.assertEqual(len(self.events), 1)
        _, claim = self.request('/v1/next?worker=4')
        self.assertEqual(claim['job_id'], result['job_id'])
        self.assertEqual(self.events[-1][0][1:3], ('context', 'start'))
        self.request('/v1/next?worker=4')
        self.assertEqual(len(self.events), 2)


class DeliveryTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_transport_never_reports_delivery_complete(self):
        connection = SimpleNamespace(action=AsyncMock(return_value={'reply': 'hello', 'delivery_token': 'test'}))
        sender = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError('transport unavailable')))
        with patch.object(quinn_discord, 'emit') as emit, self.assertLogs('quinn', level='ERROR'):
            await quinn_discord.QuinnClient._send_reply(SimpleNamespace(bridge=connection, cfg={'typing_simulation': False}), 'job', sender, 'hello')
        self.assertEqual([call.args[2] for call in emit.call_args_list], ['start', 'error'])
        self.assertTrue(emit.call_args.kwargs['uncertain'])
        self.assertEqual(sender.send.await_count, 1)

    async def test_delivery_duration_ends_only_after_acknowledgement(self):
        connection = SimpleNamespace(action=AsyncMock(side_effect=[{'reply': 'hello', 'delivery_token': 'test'}, {'ok': True}]))
        sender = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
        with patch.object(quinn_discord, 'emit') as emit:
            await quinn_discord.QuinnClient._send_reply(SimpleNamespace(bridge=connection, cfg={'typing_simulation': False}), 'job', sender, 'hello')
        self.assertEqual(emit.call_args.args[2], 'complete')
        self.assertEqual(emit.call_args.kwargs['output_chars'], 5)
        self.assertGreaterEqual(emit.call_args.kwargs['duration_ms'], emit.call_args.kwargs['send_ms'])
        self.assertEqual(connection.action.await_args.args[1], 'ack')


if __name__ == '__main__':
    unittest.main()
