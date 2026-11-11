"""Exercise the real client's delivery function with a simulated Discord channel."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

spec = importlib.util.spec_from_file_location('quinn_client', Path(__file__).resolve().parents[2] / 'quinn_discord.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class DeliveryTests(IsolatedAsyncioTestCase):
    async def test_success_acknowledges_actual_discord_message(self):
        bridge = SimpleNamespace(poll=AsyncMock(return_value='hi'), action=AsyncMock(side_effect=[
            {'reply':'hi','delivery_token':'token'}, {'ok':True}]))
        sender = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
        await client.QuinnClient._deliver_locked(SimpleNamespace(bridge=bridge,cfg={'typing_simulation':False}), 'job', sender)
        sender.send.assert_awaited_once_with('hi')
        self.assertEqual(bridge.action.await_args.args, ('job','ack',{'delivery_token':'token','discord_message_id':'123'}))

    async def test_canceled_claim_suppresses_send(self):
        bridge=SimpleNamespace(poll=AsyncMock(return_value='hi'),action=AsyncMock(return_value=None))
        sender=SimpleNamespace(send=AsyncMock())
        await client.QuinnClient._deliver_locked(SimpleNamespace(bridge=bridge,cfg={'typing_simulation':False}), 'job', sender)
        sender.send.assert_not_awaited()

    async def test_failed_send_is_not_retried(self):
        bridge=SimpleNamespace(poll=AsyncMock(return_value='hi'),action=AsyncMock(return_value={'reply':'hi','delivery_token':'token'}))
        sender=SimpleNamespace(send=AsyncMock(side_effect=RuntimeError('Simulated uncertain transport')))
        with self.assertLogs('quinn',level='ERROR'):
            await client.QuinnClient._deliver_locked(SimpleNamespace(bridge=bridge,cfg={'typing_simulation':False}), 'job', sender)
        self.assertEqual(sender.send.await_count,1)
        self.assertEqual(bridge.action.await_count,1)
