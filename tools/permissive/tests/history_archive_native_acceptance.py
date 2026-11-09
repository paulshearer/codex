"""Verify that an edited chat can retrieve exact older context from its archive."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time

from permissive.runtime import Runtime
from permissive.store import Store
from permissive.privacy import INFERENCE_ENDPOINT


async def main(native, endpoint):
    with tempfile.TemporaryDirectory(prefix='permissive-archive-native-') as folder:
        home = Path(folder)
        runtime = Runtime(home, native, endpoint, ['node', str(Path(__file__).parents[1]/'browser/worker.mjs')])
        store = Store(home)
        try:
            old_reply = 'Earlier historical record. ' * 5000 + '\nVerification: ARCHIVE-CODE-47\n' + 'Earlier historical record. ' * 5000
            seed = [{'role':'user','text':'Keep this retained historical record.','attachments':[]},
                    {'role':'assistant','text':old_reply,'attachments':[]},
                    {'role':'user','text':'The next task will check an exact value from that old record.','attachments':[]},
                    {'role':'assistant','text':'Ready for the next task.','attachments':[]}]
            task = store.submit({'prompt':'Find the verification marker matching ARCHIVE-CODE-[0-9]+ in message 2 of the retained history archive. Read only the relevant record, without printing the whole record or changing files. Reply only with the marker.',
                                 'workingDirectory':str(home),'browserMode':'dedicated'})[0]
            store.set_history_seed(task['id'],seed)
            store.update(task['id'],status='running')
            started = time.monotonic()
            await asyncio.wait_for(runtime.run(store.get(task['id']),store),180)
            result = store.get(task['id'])
            assert result['status']=='completed' and 'ARCHIVE-CODE-47' in (result['result'] or ''), {key:result[key] for key in ('status','result','error')}
            assert (home/'history-contexts'/task['id']/'retained-messages.jsonl').is_file()
            tools = [json.loads(row[0]) for row in store.db.execute("SELECT data FROM events WHERE task_id=? AND kind='agent' AND data LIKE '%item/completed%'",(task['id'],))]
            assert any(value.get('params',{}).get('item',{}).get('type') in {'commandExecution','dynamicToolCall'} for value in tools)
            follow = store.submit({'prompt':'Repeat the verification marker from your last answer. Do not use tools.','workingDirectory':str(home),'browserMode':'dedicated','resumeTaskId':task['id']})[0]
            store.update(follow['id'],status='running',thread_id=result['threadId'])
            await asyncio.wait_for(runtime.run(store.get(follow['id']),store),120)
            follow = store.get(follow['id'])
            assert follow['status']=='completed' and 'ARCHIVE-CODE-47' in (follow['result'] or ''), {key:follow[key] for key in ('status','result','error')}
            print(json.dumps({'passed':True,'exactArchivedFactRetrieved':True,'successiveContinuation':True,'seconds':round(time.monotonic()-started,2)}),flush=True)
        finally:
            await runtime.close()
            store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--native',required=True)
    parser.add_argument('--endpoint',default=INFERENCE_ENDPOINT)
    args=parser.parse_args()
    asyncio.run(main(args.native,args.endpoint))
