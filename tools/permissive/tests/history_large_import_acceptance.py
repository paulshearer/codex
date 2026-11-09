"""Exercise a new edited chat against the installed native agent and live provider.

All chat state and files are isolated in a temporary application home. No tools
are requested and no existing user conversation is modified.
"""
import argparse
import asyncio
import json
from pathlib import Path
import random
import string
import tempfile
import uuid

from permissive.history import EVIDENCE_PREFIX, all_events, copy_prefix, points
from permissive.privacy import INFERENCE_ENDPOINT
from permissive.runtime import Runtime
from permissive.store import Store


async def main(native, endpoint):
    with tempfile.TemporaryDirectory(prefix='permissive-large-history-') as folder:
        home = Path(folder)
        store = Store(home)
        runtime = Runtime(home, native, endpoint, ['node', str(Path(__file__).parents[1] / 'browser/worker.mjs')])
        try:
            source = store.submit({'prompt': 'Remember the codeword CEDAR-47. This is a regression test; do not use tools.', 'workingDirectory': str(home), 'browserMode': 'dedicated'})[0]
            store.event(source['id'], 'agent', {'method': 'item/completed', 'params': {'item': {'id': 'answer', 'type': 'agentMessage', 'text': 'CEDAR-47'}}})
            rng = random.Random(47)
            output = '\n'.join('record ' + str(i) + ' ' + ''.join(rng.choices(string.ascii_letters + string.digits, k=95)) for i in range(14500))
            item = {'id': 'historical-fixture', 'type': 'commandExecution', 'command': 'synthetic test fixture; already finished', 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': output}
            store.event(source['id'], 'agent', {'method': 'item/completed', 'params': {'item': item}})
            store.event(source['id'], 'agent', {'method': 'item/completed', 'params': {'item': {'id': 'after-tool', 'type': 'agentMessage', 'text': 'The synthetic fixture is complete.'}}})
            store.update(source['id'], status='completed', result='The synthetic fixture is complete.')
            messages = points(store.conversation_tasks(source['conversationId']), all_events(store, source['conversationId']))
            fork_id = copy_prefix(store, source['conversationId'], messages[-1], True)
            parent = store.conversation_tasks(fork_id)[-1]
            bounded = store.history_seed(parent['id'])
            assert len(json.dumps(bounded)) < 70000
            # Also exercise automatic recovery of an import created by v0.1.2,
            # with failed attempts but no executed commands or assistant output.
            legacy = bounded[:-1] + [dict(role='assistant', text=EVIDENCE_PREFIX + json.dumps([item]), attachments=[])]
            store.set_history_seed(parent['id'], legacy)
            failed = store.submit({'prompt': 'Reply only with the remembered codeword. Do not use tools.', 'resumeTaskId': parent['id'], 'workingDirectory': str(home), 'browserMode': 'dedicated'})[0]
            store.set_history_seed(failed['id'], None)
            native_id = str(uuid.uuid4())
            store.update(failed['id'], status='failed', thread_id=native_id, result='', error='stream disconnected before completion: response.failed event received')
            task = store.submit({'prompt': 'What codeword was remembered? Reply only with that codeword. Do not use tools.', 'resumeTaskId': failed['id'], 'workingDirectory': str(home), 'browserMode': 'dedicated'})[0]
            store.update(task['id'], thread_id=native_id, status='running')
            await asyncio.wait_for(runtime.run(store.get(task['id']), store), 180)
            result = store.get(task['id'])
            assert result['status'] == 'completed' and 'CEDAR-47' in result['result'], {key: result[key] for key in ('status', 'result', 'error')}
            assert result['threadId'] != native_id
            continuation = store.submit({'prompt': 'Reply only READY. Do not use tools.', 'resumeTaskId': task['id'], 'workingDirectory': str(home), 'browserMode': 'dedicated'})[0]
            store.update(continuation['id'], thread_id=result['threadId'], status='running')
            await asyncio.wait_for(runtime.run(store.get(continuation['id']), store), 180)
            result = store.get(continuation['id'])
            assert result['status'] == 'completed' and 'READY' in result['result'], {key: result[key] for key in ('status', 'result', 'error')}
            print(json.dumps({'passed': True, 'syntheticToolOutputBytes': len(output), 'newChatImportBytes': len(json.dumps(bounded)), 'legacyFailureRecovery': True, 'successiveContinuation': True}), flush=True)
        finally:
            await runtime.close()
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', required=True)
    parser.add_argument('--endpoint', default=INFERENCE_ENDPOINT)
    args = parser.parse_args()
    asyncio.run(main(args.native, args.endpoint))
