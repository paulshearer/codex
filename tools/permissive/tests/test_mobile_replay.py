import json
from pathlib import Path
import tempfile
import unittest
from permissive.mobile_replay import MobileReplay
from permissive.store import Store


class MobileReplayTests(unittest.TestCase):
    def test_large_replay_keeps_text_ids_and_snapshot_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder))
            try:
                task=store.submit({'prompt':'Retained prompt','workingDirectory':folder})[0]
                rows=[]
                def agent(method,params):
                    rows.append((task['id'],'agent',json.dumps({'method':method,'params':params}),1))
                agent('turn/started',{'turn':{'id':'turn-1'}})
                for index in range(63500):
                    agent('item/agentMessage/delta',{'itemId':'a','delta':'x'})
                    rows.append((task['id'],'inference',json.dumps({'phase':'output','requestId':'r','timestamp':2,'estimatedTokens':.25}),2))
                agent('item/completed',{'item':{'id':'a','type':'agentMessage','text':'x'*63500}})
                agent('turn/started',{'turn':{'id':'turn-2'}})
                agent('item/agentMessage/delta',{'itemId':'a','delta':'Second '})
                agent('item/agentMessage/delta',{'itemId':'a','delta':'answer'})
                agent('item/completed',{'item':{'id':'a','type':'agentMessage','text':'Second answer'}})
                with store.db:
                    store.db.executemany('INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)',rows)
                store.update(task['id'],status='completed',result='Second answer')
                original=store.conversation_events(task['conversationId'])
                first=next(value['id'] for value in original if value['data'].get('method')=='item/agentMessage/delta')
                upper=store.conversation(task['conversationId'])['eventCursor']
                replay=MobileReplay();after=0;events=[]
                while after<upper:
                    page=replay.page(store,task['conversationId'],after,upper,limit=2)
                    self.assertGreater(page['nextCursor'],after)
                    events.extend(page['data']);after=page['nextCursor']
                self.assertEqual(after,upper)
                self.assertLess(len(events),20)
                self.assertEqual(next(value for value in events if value['id']==first)['data']['params']['delta'],'x'*63500)
                self.assertEqual([value['data']['params']['delta'] for value in events if value['kind']=='agent' and value['data']['method']=='item/agentMessage/delta'],['x'*63500,'Second answer'])
                self.assertEqual(next(value for value in events if value['kind']=='inference')['data']['estimatedTokens'],63500*.25)
                self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0],len(rows)+2)
                page=replay.page(store,task['conversationId'],0,first)
                self.assertEqual(next(value for value in page['data'] if value['id']==first)['data']['params']['delta'],'x')
            finally:
                store.close()
