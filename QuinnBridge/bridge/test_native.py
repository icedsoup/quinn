import tempfile
import unittest
import sqlite3
from pathlib import Path
from native_store import initialize, dispatch, connection


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'bridge.db'
        initialize(self.path)
        self.addCleanup(self.tmp.cleanup)

    def call(self, method, path, data=None):
        return dispatch(self.path, method, path, data or {})

    def enqueue(self, uid='1', mid='10'):
        return self.call('POST','/v1/messages', dict(user_id=uid,discord_message_id=mid,display_name='same name',message='hey'))[1]['job_id']

    def claim(self):
        return self.call('GET','/v1/next?worker=4')[1]

    def test_isolation_and_dedup(self):
        one=self.enqueue()
        self.assertEqual(one,self.enqueue())
        two=self.enqueue('2','11')
        a=self.claim()
        self.assertIsNone(self.claim()['job_id'])
        self.call('POST',f'/v1/jobs/{one}/complete',dict(claim_token=a['claim_token'],reply='hi'))
        b=self.claim()
        self.assertEqual([a['user_id'],b['user_id']],['1','2'])
        self.assertNotEqual(one,two)
        self.assertNotIn('history',a)

    def test_old_worker_and_servers_rejected(self):
        self.assertEqual(self.call('GET','/v1/next?worker=2')[0],409)
        self.assertEqual(self.call('POST','/v1/messages',{'channel_id':'123'})[0],409)

    def test_reset_fences_completion_and_delivery(self):
        job=self.enqueue(); a=self.claim()
        self.call('POST','/v1/people/1',{'operation':'reset'})
        self.assertEqual(self.call('POST',f'/v1/jobs/{job}/complete',{'claim_token':a['claim_token'],'reply':'stale'})[0],409)
        self.assertEqual(self.call('POST',f'/v1/jobs/{job}/delivery')[0],409)
        self.enqueue(mid='12')
        self.assertEqual(self.claim()['epoch'],1)

    def test_delivery_once_with_simulated_sender(self):
        job=self.enqueue(); a=self.claim()
        complete={'claim_token':a['claim_token'],'reply':'hi'}
        for _ in range(2):
            self.assertEqual(self.call('POST',f'/v1/jobs/{job}/complete',complete)[0],200)
        code,delivery=self.call('POST',f'/v1/jobs/{job}/delivery')
        self.assertEqual(code,200)
        sent=[delivery['reply']]  # simulated Discord sender
        self.assertEqual(self.call('POST',f'/v1/jobs/{job}/delivery')[0],409)
        self.call('POST',f'/v1/jobs/{job}/ack',dict(delivery_token=delivery['delivery_token'],discord_message_id='99'))
        self.assertEqual(sent,['hi'])
        self.assertEqual(self.call('GET',f'/v1/jobs/{job}')[1]['status'],'sent')

    def test_uncertain_not_retried_and_pending_persists(self):
        job=self.enqueue(); a=self.claim()
        self.call('POST',f'/v1/jobs/{job}/complete',{'claim_token':a['claim_token'],'reply':'hi'})
        self.call('POST',f'/v1/jobs/{job}/delivery')
        initialize(self.path)
        self.assertEqual(self.call('GET','/v1/status')[1]['uncertain'],1)
        self.assertEqual(self.call('GET','/v1/pending')[1]['jobs'],[])

    def test_route_epoch_and_forget(self):
        self.enqueue()
        self.assertEqual(self.call('POST','/v1/people/1',dict(epoch=0,route={'file':'renamed'}))[0],200)
        self.call('POST','/v1/people/1',{'operation':'forget'})
        self.assertEqual(self.call('POST','/v1/people/1',dict(epoch=0,route={}))[0],409)
        self.assertEqual(self.call('GET','/v1/pending')[1]['jobs'],[])

    def test_expired_claim_recovery(self):
        job=self.enqueue(); old=self.claim()
        with connection(self.path) as db:
            db.execute('UPDATE native_jobs SET touched=0 WHERE id=?',(job,))
        new=self.claim()
        self.assertEqual(new['job_id'],job)
        self.assertNotEqual(new['claim_token'],old['claim_token'])
        self.assertEqual(self.call('POST',f'/v1/jobs/{job}/complete',dict(claim_token=old['claim_token'],reply='old'))[0],409)

    def test_missing_chat_reset_preserves_triggering_dm(self):
        job=self.enqueue(); claimed=self.claim()
        code,result=self.call('POST','/v1/people/1',dict(operation='reset',preserve_job=job,claim_token=claimed['claim_token']))
        self.assertEqual(code,200)
        self.assertEqual(result['epoch'],1)
        self.assertEqual(self.call('POST',f'/v1/jobs/{job}/complete',dict(claim_token=claimed['claim_token'],reply='fresh'))[0],200)

    def test_migration_is_repeatable_and_preserves_unrelated_data(self):
        from migrate_native import migrate
        root=Path(self.tmp.name)
        (root/'QuinnBridge/bridge').mkdir(parents=True)
        chats=root/'SillyTavern/data/default-user/chats/Quinn'; chats.mkdir(parents=True)
        (chats/'discord_dm_test.jsonl').write_text('old transcript')
        (chats/'unrelated.jsonl').write_text('keep')
        source=root/'put in tavern extensions/quinn-bridge'; source.mkdir(parents=True)
        for name in ('index.js','style.css','manifest.json'):
            (source/name).write_text('test')
        first=migrate(root); second=migrate(root)
        self.assertEqual(first,second)
        self.assertEqual((chats/'unrelated.jsonl').read_text(),'keep')
        self.assertFalse((chats/'discord_dm_test.jsonl').exists())
        self.assertEqual((Path(first['backup'])/'archived_chats/discord_dm_test.jsonl').read_text(),'old transcript')


if __name__ == '__main__':
    unittest.main()
