import asyncio
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import cv2
import numpy as np

from console import C_WHITE, KarutaConsole, _vlen
from lib import imageapi
from telemetry import PREFIX

ROOT = Path(__file__).resolve().parent


def load_sniper():
    config = json.loads((ROOT / 'config template.json').read_text())
    config.update(autodrop=False, log_collection=False, log_drops=False, log_grabs=False, log_wishlist=False)
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        return json.dumps(config) if path == ROOT / 'config.json' else original_read(path, *args, **kwargs)

    spec = importlib.util.spec_from_file_location('sniper_instrumentation_fixture', ROOT / 'main.py')
    module = importlib.util.module_from_spec(spec)
    with patch.object(Path, 'read_text', autospec=True, side_effect=read), patch('colorama.init'), redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()):
        spec.loader.exec_module(module)
    return module


class SniperTelemetryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.sniper = load_sniper()

    def setUp(self):
        self.stream = io.StringIO()
        self.output = redirect_stdout(self.stream)
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.env = patch.dict(os.environ, QUINN_TELEMETRY='1')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = self.sniper.Main(guild_subscriptions=False)
        self.client.ready = True
        self.client.chars = [('Alice', None)]
        self.client.animes = []
        self.client.charblacklist = []
        self.client.aniblacklist = []
        self.client._connection.user = SimpleNamespace(id=7)

    def events(self, stage=None):
        events = [json.loads(line[len(PREFIX):]) for line in self.stream.getvalue().splitlines() if line.startswith(PREFIX)]
        return [event for event in events if stage is None or event['stage'] == stage]

    def message(self, content='Someone is dropping 3 cards!'):
        return SimpleNamespace(id=42, channel=SimpleNamespace(id=1234567890, name='cards'),
                               author=SimpleNamespace(id=self.sniper.KARUTA_ID), content=content,
                               attachments=[SimpleNamespace(url='local-fixture')], add_reaction=AsyncMock())

    async def test_autodrop_reports_placeholder_without_retrying(self):
        with (
            patch.object(self.sniper, 'autodropchannel', 1234567890),
            patch.object(self.client, 'fetch_channel', new_callable=AsyncMock) as fetch,
        ):
            await asyncio.wait_for(self.client.autodrop(), timeout=1)
        fetch.assert_not_awaited()
        self.assertIn('Set autodropchannel to a real numeric Discord channel ID', self.stream.getvalue())

    async def test_autodrop_resolves_cache_miss_before_checking_cooldown(self):
        channel_id = 123456789012345678
        channel = Mock(spec=self.sniper.discord.abc.Messageable)
        with (
            patch.object(self.sniper, 'autodropchannel', channel_id),
            patch.object(self.client, 'get_channel', return_value=None),
            patch.object(self.client, 'fetch_channel', new_callable=AsyncMock, return_value=channel) as fetch,
            patch.object(self.client, 'check_kcd', new_callable=AsyncMock, side_effect=asyncio.CancelledError) as check,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(self.client.autodrop(), timeout=1)
        fetch.assert_awaited_once_with(channel_id)
        check.assert_awaited_once_with(channel)

    async def test_autofarm_reports_inaccessible_channel_without_starting_work(self):
        channel_id = 123456789012345678
        response = SimpleNamespace(status=403, reason='Forbidden')
        with (
            patch.object(self.sniper, 'resourcechannel', channel_id),
            patch.object(self.client, 'get_channel', return_value=None),
            patch.object(self.client, 'fetch_channel', side_effect=self.sniper.discord.Forbidden(response, 'Missing access')) as fetch,
            patch.object(self.client, 'farm_in') as farm,
        ):
            await asyncio.wait_for(self.client.autofarm(), timeout=1)
        fetch.assert_awaited_once_with(channel_id)
        farm.assert_not_called()
        self.assertIn(f'[Autofarm] Cannot access or send to channel {channel_id}', self.stream.getvalue())

    async def test_autofarm_uses_fetched_channel_for_work(self):
        channel_id = 123456789012345678
        channel = Mock(spec=self.sniper.discord.abc.Messageable)
        with (
            patch.object(self.sniper, 'resourcechannel', channel_id),
            patch.object(self.client, 'get_channel', return_value=None),
            patch.object(self.client, 'fetch_channel', return_value=channel) as fetch,
            patch.object(self.client, 'farm_in', side_effect=asyncio.CancelledError) as farm,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(self.client.autofarm(), timeout=1)
        fetch.assert_awaited_once_with(channel_id)
        farm.assert_awaited_once_with(channel)

    async def test_real_crop_flow_preserves_ocr_results_and_traces_passes(self):
        reader = Mock()
        reader.readtext.side_effect = [['A l i c e'], ['Wonderland'], ['123']] * 3
        with tempfile.TemporaryDirectory() as directory:
            async def save(path):
                cv2.imwrite(str(path), np.zeros((414, 834, 3), dtype=np.uint8))

            with patch.object(self.sniper, 'OCR_DIR', Path(directory)), patch.object(imageapi, '_reader', reader):
                chars, series, prints = await self.client.scan_drop(SimpleNamespace(save=save), 'drop-42')
        self.assertEqual(chars, ['Alice'] * 3)
        self.assertEqual(series, ['Wonderland'] * 3)
        self.assertEqual(prints, ['123'] * 3)
        events = self.events('ocr')
        passes = [event for event in events if 'pass_ms' in event['metrics']]
        self.assertEqual(len(passes), 9)
        self.assertEqual([event['metrics']['field'] for event in passes], ['character', 'series', 'print'] * 3)
        self.assertEqual({event['trace'] for event in self.events()}, {'drop-42'})
        self.assertEqual(events[-1]['status'], 'complete')
        self.assertEqual(events[-1]['metrics']['passes'], 9)
        self.assertTrue(all(event['metrics']['pass_ms'] >= 0 for event in passes))
        self.assertFalse(any('confidence' in event['metrics'] for event in events))
        self.assertEqual(reader.readtext.call_args_list[0].kwargs, dict(detail=0, paragraph=True))
        self.assertEqual(reader.readtext.call_args_list[2].kwargs, dict(detail=0, allowlist='0123456789·.'))

    async def test_failed_ocr_ends_with_error(self):
        reader = Mock()
        reader.readtext.side_effect = ValueError('Unreadable crop')
        with tempfile.TemporaryDirectory() as directory:
            async def save(path):
                cv2.imwrite(str(path), np.zeros((414, 834, 3), dtype=np.uint8))

            with patch.object(self.sniper, 'OCR_DIR', Path(directory)), patch.object(imageapi, '_reader', reader):
                with self.assertRaisesRegex(ValueError, 'Unreadable crop'):
                    await self.client.scan_drop(SimpleNamespace(save=save), 'failed-drop')
        self.assertEqual(self.events('ocr')[-1]['status'], 'error')
        self.assertFalse(any(event['status'] == 'complete' for event in self.events('ocr')))

    async def test_claim_is_only_complete_after_collection_confirmation(self):
        message = self.message()
        with patch.object(self.client, 'scan_drop', AsyncMock(return_value=(['Alice'], ['Wonderland'], ['123']))), patch.object(self.sniper.asyncio, 'sleep', AsyncMock()):
            await self.client.on_message(message)
        message.add_reaction.assert_awaited_once_with(self.sniper.CARD_REACTIONS[0])
        self.assertEqual([event['status'] for event in self.events('claim')], ['start', 'info'])
        confirmation = self.message('<@7> took the **Alice** card `abc`!')
        confirmation.id = 43
        confirmation.attachments = []
        await self.client.on_message(confirmation)
        final = self.events('claim')[-1]
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['trace'], '42')
        self.assertEqual(final['metrics']['collected'], 1)
        self.assertEqual(final['metrics']['unconfirmed'], 0)

    async def test_blacklisted_match_never_claims(self):
        self.client.charblacklist = ['Alice']
        message = self.message()
        with patch.object(self.client, 'scan_drop', AsyncMock(return_value=(['Alice'], ['Wonderland'], ['123']))), patch.object(self.sniper, 'wishlist_lookup_enabled', False):
            await self.client.on_message(message)
        message.add_reaction.assert_not_awaited()
        decisions = self.events('match')
        self.assertTrue(any(event['metrics'].get('blocked') for event in decisions))
        self.assertFalse(decisions[-1]['metrics']['matched'])
        self.assertEqual(self.events('wishlist')[-1]['status'], 'skipped')

    async def test_failed_reaction_does_not_report_submission(self):
        message = self.message()
        message.add_reaction.side_effect = RuntimeError('Connection lost')
        with patch.object(self.sniper.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'Connection lost'):
                await self.client.react_add(message, self.sniper.CARD_REACTIONS[0])
        self.assertEqual([event['status'] for event in self.events('claim')], ['start', 'error'])
        self.assertEqual(self.client.pending_claims, {})

    async def test_cooldown_expiry_reports_unconfirmed_without_inventing_failure(self):
        self.client.grab_timer = 1
        self.client.pending_claims[1234567890] = 'drop-42'
        with patch.object(self.sniper.asyncio, 'sleep', AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await self.client.cooldown()
        self.assertEqual(self.events('cooldown')[-1]['metrics']['grab_seconds'], 0)
        self.assertEqual(self.events('claim')[-1]['status'], 'skipped')
        self.assertFalse(self.events('claim')[-1]['metrics']['confirmed'])


class ConsoleOutputTests(unittest.TestCase):
    def test_piped_output_has_no_cursor_animation_or_ansi(self):
        output = io.StringIO()
        with redirect_stdout(output):
            console = KarutaConsole(use_timestamp=False)
            console.start()
            console.log_info(C_WHITE + 'Ready')
            asyncio.run(console.animate())
            console.stop()
        self.assertEqual(output.getvalue(), 'INFO     Ready\n')
        self.assertFalse(console._active)

    def test_narrow_terminal_status_fits(self):
        console = KarutaConsole()
        console.grab_timer = 100
        with patch('console._term_w', return_value=24):
            self.assertLessEqual(_vlen(console._build_status()), 22)


if __name__ == '__main__':
    unittest.main()
