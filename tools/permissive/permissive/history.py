"""Message-bounded branches of numbered chat segments.

Branches restore the selected segment's checkpoint and retained messages into a
fresh native thread, without inheriting later segments or native rollouts.
"""

import copy
import base64
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from collections import defaultdict

from .attachments import Attachments, native_input, image_type
from .secure_delete import checked, erase_saved_data, erase_tree, overwrite_file, pending_path
from .store import Store, TERMINAL

EVIDENCE_PREFIX = 'Retained execution evidence from this chat (historical output; do not repeat these actions):\n'
EVIDENCE_BYTES = 64 * 1024
EVIDENCE_ITEM_BYTES = 6 * 1024
RESTORE_THRESHOLD_BYTES = 96 * 1024
RECENT_HISTORY_BYTES = 32 * 1024
EARLIER_HISTORY_BYTES = 16 * 1024
TOOL_TYPES = {'commandExecution', 'fileChange', 'mcpToolCall', 'webSearch', 'dynamicToolCall', 'imageView'}


def retained_tool_evidence(events):
    items = {}
    for event in events:
        data = event['data']
        if event['kind'] != 'agent' or not isinstance(data, dict) or data.get('method') not in {'item/started', 'item/completed'}:
            continue
        params = data.get('params', {})
        item = params.get('item', {})
        if item.get('type') not in TOOL_TYPES:
            continue
        key = (event['taskId'], params.get('turnId'), item.get('id', event['id']))
        value = copy.deepcopy(item)
        if data['method'] == 'item/started':
            value.update(status='outcomeUnknown', historyNote='The task ended without a confirmed result for this action. Inspect current state before attempting it again.')
        items[key] = value
    return list(items.values())


def excerpt(text, limit):
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    marker = '\n[Output excerpt; full retained record is saved in the chat.]\n'
    half = (limit - len(marker.encode())) // 2
    return raw[:half].decode('utf-8', errors='ignore') + marker + raw[-half:].decode('utf-8', errors='ignore')


def execution_evidence(items):
    # Native tool outputs are normally truncated before entering model context.
    # Importing their entire UI records bypassed that limit and could expand a
    # compacted chat past the provider's context window. Keep full records in the
    # saved timeline, and give the model bounded, chronological recent excerpts.
    kept, size = [], 0
    for item in reversed(items):
        text = excerpt(json.dumps(item, ensure_ascii=False), EVIDENCE_ITEM_BYTES)
        if size + len(text.encode('utf-8')) > EVIDENCE_BYTES - 1024:
            break
        kept.append(text)
        size += len(text.encode('utf-8')) + 2
    note = f'[{len(items) - len(kept)} earlier tool records omitted from model context; full records remain in the saved chat.]\n' if len(kept) < len(items) else ''
    return EVIDENCE_PREFIX + note + '\n\n'.join(reversed(kept))


def bounded_seed(seed):
    result = []
    for message in seed:
        text = message['text']
        # Also normalize snapshots made by the first release of chat editing.
        if message['role'] == 'assistant' and text.startswith(EVIDENCE_PREFIX):
            try:
                items = json.loads(text[len(EVIDENCE_PREFIX):])
            except ValueError:
                items = None
            text = execution_evidence(items) if isinstance(items, list) else excerpt(text, EVIDENCE_BYTES)
        result.append({**message, 'text': text})
    return result


def recover_failed_import(store, task):
    """Rebuild a legacy oversized import only when no follow-up did any work."""
    tasks = store.conversation_tasks(task['conversationId'])
    current = next(index for index, value in enumerate(tasks) if value['id'] == task['id'])
    for index in range(current - 1, -1, -1):
        seed = store.history_seed(tasks[index]['id'])
        if seed is None:
            continue
        if not any(message['role'] == 'assistant' and message['text'].startswith(EVIDENCE_PREFIX)
                   and len(message['text'].encode('utf-8')) > EVIDENCE_BYTES for message in seed):
            return None
        bounded = bounded_seed(seed)
        if bounded == seed:
            return None
        failed = tasks[index + 1:current]
        if not failed or any(value['status'] != 'failed' or value['result'] or
                             'response.failed event received' not in (value['error'] or '') for value in failed):
            return None
        ids = {value['id'] for value in failed}
        # Never reconstruct a thread after a command or other tool may have run.
        for event in all_events(store, task['conversationId']):
            if event['taskId'] in ids and event['kind'] == 'agent' and event['data'].get('method') == 'item/started':
                if event['data'].get('params', {}).get('item', {}).get('type') not in {'reasoning', 'userMessage'}:
                    return None
            if event['taskId'] in ids and event['kind'] == 'agent' and event['data'].get('method') == 'item/completed':
                if event['data'].get('params', {}).get('item', {}).get('type') not in {'reasoning', 'userMessage'}:
                    return None
        return bounded + [dict(role='user', text=value['request']['prompt'], attachments=value['request'].get('attachments', [])) for value in failed]
    return None


def upgrade_unprojected_history(store, task):
    """Upgrade an already-imported large edited chat without replaying its work."""
    if not task.get('threadId'):
        return None
    tasks = store.conversation_tasks(task['conversationId'])
    prior = tasks[:next(index for index, value in enumerate(tasks) if value['id'] == task['id'])]
    owner = next((value for value in prior if value['threadId'] == task['threadId']), None)
    if owner is None or (store.home / 'history-contexts' / owner['id'] / 'retained-messages.jsonl').is_file():
        return None
    imported = next((store.history_seed(value['id']) for value in reversed(prior) if store.history_seed(value['id']) is not None), None)
    if imported is None or sum(len(message['text'].encode('utf-8')) for message in bounded_seed(imported)) <= RESTORE_THRESHOLD_BYTES:
        return None
    if any(value['status'] not in TERMINAL for value in prior):
        return None
    ids = {value['id'] for value in prior}
    events = [event for event in all_events(store, task['conversationId']) if event['taskId'] in ids]
    messages = points(prior, events)
    seed = [dict(role=value['role'], text=value['text'], attachments=value['attachments']) for value in messages]
    evidence = retained_tool_evidence(events)
    if evidence:
        seed.append(dict(role='assistant', text=execution_evidence(evidence), attachments=[]))
    return seed


def all_events(store, ident):
    # Internal history operations must not repeatedly sort the same conversation
    # once per 200-row network page. Long chats contain hundreds of thousands of
    # token events; scan their ordered rows only once.
    return [{'id': row['id'], 'taskId': row['task_id'], 'kind': row['kind'],
             'data': json.loads(row['data']), 'createdAt': row['created_at']}
            for row in store.event_rows(ident)]


def points(tasks, events):
    """Use the same first-seen message order as both clients."""
    result = []
    grouped = defaultdict(list)
    for event in events:
        grouped[event['taskId']].append(event)
    for task in tasks:
        task_events = grouped[task['id']]
        prompt_point = None
        if task.get('historyPromptVisible', True) and (task['request'].get('prompt') or task['request'].get('attachments')):
            prompt_point = dict(taskId=task['id'], messageId='prompt', role='user',
                               text=task['request']['prompt'], attachments=task['request'].get('attachments', []),
                               start=0, end=0, complete=True, eventIds=[])
            result.append(prompt_point)
        steering = {event['data']['submissionId']: event['data'] for event in task_events if event['kind'] == 'steering'}
        native_users = {event['data'].get('params', {}).get('item', {}).get('clientId') or event['data'].get('params', {}).get('item', {}).get('id')
                        for event in task_events if event['kind'] == 'agent' and event['data'].get('params', {}).get('item', {}).get('type') == 'userMessage'}
        messages, users, turn = {}, set(), ''
        for event in task_events:
            data = event['data']
            if event['kind'] == 'steering':
                client = data['submissionId']
                if client in native_users or client in users:
                    continue
                users.add(client)
                result.append(dict(taskId=task['id'], messageId=str(event['id']), role='user', text=data['prompt'],
                                   attachments=data.get('attachments', []), start=event['id'], end=event['id'], complete=True, eventIds=[event['id']], clientId=client))
            if event['kind'] != 'agent':
                continue
            method, params = data.get('method'), data.get('params', {})
            item = params.get('item') or {}
            if method == 'turn/started':
                turn = params.get('turn', {}).get('id', '')
            if item.get('type') == 'userMessage':
                client = item.get('clientId') or item.get('id')
                if not (client in steering or item.get('clientId')):
                    if prompt_point is not None:
                        prompt_point['eventIds'].append(event['id'])
                    continue
                if client in users or not (client in steering or item.get('clientId')):
                    continue
                accepted = steering.get(client, {})
                text = accepted.get('prompt') or '\n'.join(part.get('text', '') for part in item.get('content', []) if part.get('type') == 'text')
                users.add(client)
                result.append(dict(taskId=task['id'], messageId=str(event['id']), role='user', text=text,
                                   attachments=accepted.get('attachments', []), start=event['id'], end=event['id'], complete=True, eventIds=[event['id']], clientId=client))
            if method == 'item/agentMessage/delta' or item.get('type') == 'agentMessage':
                key = (params.get('turnId') or turn, item.get('id') or params.get('itemId'))
                if key not in messages:
                    messages[key] = dict(taskId=task['id'], messageId=str(event['id']), role='assistant', text='',
                                         attachments=[], start=event['id'], end=event['id'], complete=False, eventIds=[])
                    result.append(messages[key])
                message = messages[key]
                message['eventIds'].append(event['id'])
                if method == 'item/agentMessage/delta' and not message['complete']:
                    message['text'] += params.get('delta', '')
                if method == 'item/completed':
                    message.update(text=item.get('text', ''), end=event['id'], complete=True)
        if not any(point['taskId'] == task['id'] and point['role'] == 'assistant' for point in result) and task.get('result'):
            result.append(dict(taskId=task['id'], messageId='result', role='assistant', text=task['result'], attachments=[],
                               start=task_events[-1]['id'] + 1 if task_events else 1, end=task_events[-1]['id'] if task_events else 0, complete=True, eventIds=[]))
    return result


def selection(store, ident, body, inclusive, source=None):
    tasks, events = source if source is not None else (store.conversation_tasks(ident), all_events(store, ident))
    if any(task['status'] not in TERMINAL for task in tasks):
        raise ValueError('Finish or cancel this chat before changing its history')
    messages, selected = selected_messages(tasks, events, body)
    target = selected[-1] if inclusive else selected[0]
    if inclusive and not target['complete']:
        raise ValueError('Only completed messages can be forked')
    index = messages.index(target)
    retained = messages[:index + int(inclusive)]
    task_index = next(index for index, task in enumerate(tasks) if task['id'] == target['taskId'])
    cutoff = target['end'] if inclusive else target['start'] - 1
    earlier = {task['id'] for task in tasks[:task_index]}
    kept_events = [event for event in events if event['taskId'] in earlier
                   or event['taskId'] == target['taskId'] and event['id'] <= cutoff]
    kept_tasks = tasks[:task_index] + ([tasks[task_index]] if any(point['taskId'] == target['taskId'] for point in retained) else [])
    allowed = {event_id for point in retained for event_id in point['eventIds']}
    kept_events = [event for event in kept_events if event['kind'] != 'agent'
                   or not (event['data'].get('method') == 'item/agentMessage/delta' or event['data'].get('params', {}).get('item', {}).get('type') in {'agentMessage', 'userMessage'})
                   or event['id'] in allowed]
    kept_events = [event for event in kept_events if event['kind'] != 'history']
    if not inclusive:
        # Thinking is displayed below a task's messages. A partial deletion must
        # not leave the removed response's private thinking beneath its prompt.
        kept_events = [event for event in kept_events if not (event['taskId'] == target['taskId'] and event['kind'] == 'agent'
                       and ('reasoning' in event['data'].get('method', '') or event['data'].get('params', {}).get('item', {}).get('type') == 'reasoning'))]
    # Native steering can arrive before its acceptance journal. Carry its original
    # text and attachments even when that journal is beyond the selected cutoff.
    for event in kept_events:
        item = event['data'].get('params', {}).get('item', {}) if event['kind'] == 'agent' else {}
        if item.get('type') == 'userMessage':
            point = next((point for point in retained if point['messageId'] == str(event['id'])), None)
            if point:
                event['data']['params']['item']['content'] = [{'type': 'text', 'text': point['text']}]
    kept_ids = {event['id'] for event in kept_events}
    for event in events:
        if event['kind'] == 'steering' and event['id'] not in kept_ids:
            if any(point.get('clientId') == event['data']['submissionId'] and point['taskId'] == event['taskId'] for point in retained):
                kept_events.append(event)
    kept_events.sort(key=lambda event: event['id'])
    return tasks, kept_tasks, kept_events, retained


def selected_messages(tasks, events, body):
    messages = points(tasks, events)
    selected = [point for point in messages if point['taskId'] == body['taskId'] and
                (point['messageId'] == body['messageId'] or body['messageId'] == 'response' and point['role'] == 'assistant')]
    if not selected:
        raise ValueError('The selected message is no longer in this chat. Refresh and try again.')
    return messages, selected


def message_deletion(store, ident, body, source=None):
    source, events = source if source is not None else (store.conversation_tasks(ident), all_events(store, ident))
    if any(task['status'] not in TERMINAL for task in source):
        raise ValueError('Finish or cancel this chat before deleting a message')
    messages, removed = selected_messages(source, events, body)
    removed_ids = {event_id for point in removed for event_id in point['eventIds']}
    clients = {point['clientId'] for point in removed if point.get('clientId')}
    def keep(event):
        if event['id'] in removed_ids or event['kind'] == 'history':
            return False
        if event['taskId'] != body['taskId']:
            return True
        data = event['data']
        item = data.get('params', {}).get('item', {}) if event['kind'] == 'agent' else {}
        if event['kind'] == 'steering' and data.get('submissionId') in clients:
            return False
        if item.get('type') == 'userMessage' and (item.get('clientId') or item.get('id')) in clients:
            return False
        # Do not retain private thinking from the removed response.
        return not (event['kind'] == 'agent' and ('reasoning' in data.get('method', '') or item.get('type') == 'reasoning'))
    tasks = copy.deepcopy(source)
    if any(point['messageId'] == 'prompt' for point in removed):
        task = next(task for task in tasks if task['id'] == body['taskId'])
        task['request'].update(prompt='', attachments=[])
    return source, tasks, [event for event in events if keep(event)], [point for point in messages if point not in removed]


def copy_prefix(store, ident, body, inclusive, key=None, snapshot=None, publish=None, source=None, prepare=None):
    source = source if source is not None else (store.conversation_tasks(ident), all_events(store, ident))
    original_points = points(*source)
    source, tasks, events, messages = snapshot if snapshot is not None else selection(store, ident, body, inclusive, source)
    new_id, now = str(uuid.uuid4()), int(time.time())
    if not tasks:
        tasks = [dict(source[0], request={**source[0]['request'], 'prompt': '', 'attachments': []})]
    mapping = {task['id']: new_id if index == 0 else str(uuid.uuid4()) for index, task in enumerate(tasks)}
    attachment_map, files = {}, []
    attachments = Attachments(store)
    try:
        for point in messages:
            for old_id in point['attachments']:
                if old_id in attachment_map:
                    continue
                row = attachments.get(old_id)
                new_attachment = str(uuid.uuid4())
                destination = attachments.path(new_attachment, row['name'])
                destination.parent.mkdir(parents=True)
                files.append(destination.parent)
                with destination.open('wb') as output:
                    output.write(attachments.path(old_id).read_bytes())
                    output.flush()
                    os.fsync(output.fileno())
                attachment_map[old_id] = (new_attachment, row, mapping[point['taskId']])
        source_points = original_points
        for task in tasks:
            task_messages = [point for point in messages if point['taskId'] == task['id']]
            source_messages = [point for point in source_points if point['taskId'] == task['id']]
            if (snapshot is None or body.get('_operation') == 'regenerate') and task_messages != source_messages:
                continue
            root = checked(store.home / 'artifacts' / task['id'], store.home)
            destination = checked(store.home / 'artifacts' / mapping[task['id']], store.home)
            if root.exists():
                for path in root.rglob('*'):
                    checked(path, store.home)
                files.append(destination)
                shutil.copytree(root, destination)
                for path in destination.rglob('*'):
                    if path.is_file():
                        with path.open('r+b') as copied:
                            os.fsync(copied.fileno())
        def remap(value):
            if isinstance(value, list):
                return [remap(part) for part in value]
            if isinstance(value, dict):
                return {key: remap(part) for key, part in value.items()}
            return attachment_map[value][0] if isinstance(value, str) and value in attachment_map else value
        info = store.segments.info(ident, body.get('segmentId'))
        seed = (store.segments.checkpoint(info).get('seed', []) if info else []) + [dict(role=point['role'], text=point['text'], attachments=[attachment_map[value][0] for value in point['attachments']]) for point in messages]
        if body.get('_operation') == 'regenerate':
            base = None if body.get('_regenerationUserMessage') else store.task_context(body['taskId']).get('_regenerationSeed')
            if base is not None:
                seed = remap(copy.deepcopy(base))
            else:
                seed.pop(next(index for index in range(len(seed) - 1, -1, -1) if seed[index]['role'] == 'user'))
        # Preserve retained tool evidence, without replaying any tools or importing
        # reasoning/compaction that may contain text after the cutoff.
        evidence = retained_tool_evidence(events)
        if evidence:
            seed.append(dict(role='assistant', text=execution_evidence(evidence), attachments=[]))
        # Serialize before acquiring the database write lock. Otherwise other
        # running turns can block the API loop waiting for a long WAL writer.
        from .segments import compaction
        copied_events = [(mapping[event['taskId']], event['kind'], json.dumps(remap(event['data']), ensure_ascii=False), event['createdAt'])
                         for event in events if event['taskId'] in mapping and not compaction(event['data'])]
        with store.db:
            for index, task in enumerate(tasks):
                request = remap(copy.deepcopy(task['request']))
                request.pop('submissionId', None)
                request.pop('resumeTaskId', None)
                if index:
                    request['resumeTaskId'] = mapping[tasks[index - 1]['id']]
                context = remap(copy.deepcopy(task['_historyContext'] if '_historyContext' in task else store.task_context(task['id'])))
                context['_segmentPromptVisible'] = task.get('historyPromptVisible', True)
                # Materialize an independent native thread on first continuation.
                store.db.execute('INSERT INTO tasks (id,request,status,thread_id,result,created_at,updated_at,completed_at,idempotency_key,workspace_id,workspace_context,conversation_id,diagnostic_log_level,history_seed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (mapping[task['id']], json.dumps(request), 'completed', None,
                     next((point['text'] for point in reversed(messages) if point['taskId'] == task['id'] and point['role'] == 'assistant'), None),
                     task['createdAt'], now, now, 'history-replacement:staging:' + new_id if index == 0 else None, task['workspaceId'], json.dumps(context), new_id,
                     task['diagnosticLogLevel'], json.dumps(seed, ensure_ascii=False) if index == len(tasks) - 1 else None))
                if snapshot is not None:
                    store.db.execute('UPDATE tasks SET status=?,error=?,token_usage=?,completed_at=? WHERE id=?',
                                     (task['status'], task.get('error'), json.dumps(task['tokenUsage']) if task.get('tokenUsage') else None, task.get('completedAt'), mapping[task['id']]))
            for new_attachment, row, owner in attachment_map.values():
                store.db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)', (new_attachment, row['name'], row['mime'], row['size'], row['workspace_id'], owner))
            store.db.executemany('INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)',
                                copied_events)
            title = store.conversation_title(ident, source)
            store.db.execute('INSERT INTO conversation_metadata VALUES (?,?)', (new_id, (title.replace('\n', ' ').strip()[:110] + ' — Fork') if inclusive and not body.get('_operation') else title))
            store.db.execute('INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)',
                             (mapping[tasks[-1]['id']], 'history', json.dumps({'action': 'fork' if inclusive else 'delete'}), now))
        checkpoint = body.get('_checkpointOverride') or (store.segments.checkpoint(info) if info else None)
        store.segments.migrate(new_id, initial_checkpoint=checkpoint)
        if info:
            store.segments.references(new_id, info)
        if body.get('_checkpointOverride'):
            store.segments.set_checkpoint(store.segments.info(new_id), checkpoint)
        if prepare:
            prepare(new_id)
        with store.db:
            if publish:
                publish(new_id, mapping[tasks[-1]['id']], seed)
            if body.get('_operation') != 'regenerate' and not (inclusive and not body.get('_operation')):
                from .reply_versions import replace_branch
                replace_branch(store, ident, new_id, mapping, original_points, messages)
            store.db.execute('UPDATE tasks SET idempotency_key=? WHERE id=?', (key, new_id))
            store.segments.sync_pending(new_id)
        return new_id
    except BaseException:
        for path in files:
            erase_tree(path, store.home)
        raise


def build_replacement(home, guest_paths, ident, body, operation, key):
    """Perform expensive history/filesystem work on a dedicated connection."""
    store = Store.worker_connection(home, guest_paths)
    try:
        existing = store.db.execute('SELECT conversation_id FROM tasks WHERE idempotency_key=?', (key,)).fetchone()
        if existing:
            return existing[0]
        if store.db.execute('SELECT 1 FROM history_deletions WHERE source_id=?', (ident,)).fetchone():
            raise ValueError('This chat has already been replaced. Open the updated chat.')
        if store.db.execute("SELECT 1 FROM tasks WHERE conversation_id=? AND status NOT IN ('completed','failed','cancelled','interrupted')", (ident,)).fetchone():
            raise ValueError('Finish or cancel this chat before changing its history')
        info = store.segments.info(ident, body.get('segmentId'))
        store.segments.scope = (ident, info['id']) if info else None
        source = (store.conversation_tasks(ident), all_events(store, ident))
        if operation == 'edit-message':
            messages, selected = selected_messages(*source, body)
            task_index = next(index for index, task in enumerate(source[0]) if task['id'] == body['taskId'])
            point_index = [point for point in messages if point['taskId'] == body['taskId']].index(selected[0])
            def prepare(replacement):
                store.segments.scope = None
                copied = store.conversation_tasks(replacement)
                copied_points = [point for point in points(copied, all_events(store, replacement)) if point['taskId'] == copied[task_index]['id']]
                edited = {**body, 'taskId': copied[task_index]['id'], 'segmentId': store.segments.info(replacement)['id'],
                          'messageId': body['messageId'] if body['messageId'] in {'prompt', 'response'} else copied_points[point_index]['messageId']}
                edit_message(store, replacement, edited)
            def publish_edit(replacement, latest, seed):
                store.db.execute('INSERT INTO history_deletions VALUES (?,?,0)', (ident, replacement))
                store.db.execute('INSERT OR REPLACE INTO conversation_queues VALUES (?,1)', (ident,))
            return copy_prefix(store, ident, body, True, key, snapshot=(source[0], source[0], source[1], messages),
                source=source, prepare=prepare, publish=publish_edit)
        snapshot = message_deletion(store, ident, body, source) if operation == 'delete-message' else None
        boundary, inclusive = body, operation == 'fork'
        if operation == 'regenerate':
            task = next((task for task in source[0] if task['id'] == body['taskId']), None)
            if task is None or any(task['status'] not in TERMINAL for task in source[0]):
                raise ValueError('Select a finished turn in this chat')
            messages, selected = selected_messages(*source, body)
            target = selected[-1]
            before = messages[:messages.index(target) + int(target['role'] == 'user')]
            prompt = next((point for point in reversed(before) if point['taskId'] == task['id'] and point['role'] == 'user'), None)
            if prompt is not None and prompt['messageId'] != 'prompt':
                # A long task can contain several steering/native user inputs.
                # Preserve its earlier exchanges as a completed task and give
                # the selected input its own task for the new model execution.
                boundary = {**body, 'messageId': prompt['messageId'], '_regenerationUserMessage': True}
                original, retained, events, kept = selection(store, ident, boundary, True, source)
                ident_new = str(uuid.uuid4())
                regenerated = {**copy.deepcopy(task), 'id': ident_new,
                    'request': {**copy.deepcopy(task['request']), 'prompt': prompt['text'], 'attachments': prompt['attachments']},
                    'historyPromptVisible': True, 'result': None, 'error': None, 'tokenUsage': None,
                    'createdAt': int(time.time()), 'completedAt': None,
                    '_historyContext': store.task_context(task['id'])}
                user_events = set(prompt['eventIds'])
                client = prompt.get('clientId')
                events = [event for event in events if event['id'] not in user_events and not
                    (client and event['taskId'] == task['id'] and event['kind'] == 'steering' and event['data'].get('submissionId') == client)]
                # selection builds fresh point objects; match the selected input
                # by message identity rather than by object identity.
                kept = [{**point, 'taskId': ident_new, 'messageId': 'prompt', 'start': 0, 'end': 0, 'eventIds': []}
                    if point['taskId'] == task['id'] and point['messageId'] == prompt['messageId'] else point for point in kept]
                snapshot = (original, retained + [regenerated], events, kept)
                inclusive = True
            else:
                boundary, inclusive = {**body, 'messageId': 'prompt'}, True
            if not (prompt['text'] if prompt else task['request']['prompt']) and not (prompt['attachments'] if prompt else task['request'].get('attachments')):
                raise ValueError('This turn has no retained prompt to regenerate')
            # A task can straddle compaction. Its original prompt remains in
            # this file for regeneration, even when hidden in the timeline.
            if prompt is None and not task.get('historyPromptVisible', True):
                base = store.task_context(task['id']).get('_regenerationSeed')
                if base is None:
                    raise ValueError('This turn needs its saved context before regeneration')
                body['_checkpointOverride'] = {'seed': base, 'status': 'ready', 'provenance': 'regenerated-turn'}
                boundary['_checkpointOverride'] = body['_checkpointOverride']
            if prompt is None or prompt['messageId'] == 'prompt':
                task['historyPromptVisible'] = True

        def publish(replacement, latest, seed):
            if operation == 'regenerate':
                store.db.execute('UPDATE tasks SET history_seed=?,status=?,error=?,completed_at=NULL WHERE id=?',
                    (json.dumps(seed), 'interrupted', 'Regeneration ready. Retry Regenerate to start.', latest))
            else:
                store.db.execute('INSERT INTO history_deletions(source_id,replacement_id) VALUES (?,?)', (ident, replacement))
                store.db.execute('INSERT OR REPLACE INTO conversation_queues VALUES (?,1)', (ident,))

        return copy_prefix(store, ident, boundary, inclusive, key, snapshot=snapshot,
                           publish=publish if operation != 'fork' else None, source=source)
    finally:
        store.close()


def edit_path(store, ident):
    # IDs are resolved by the database before this path is constructed.
    uuid.UUID(ident)
    return checked(store.home / 'history-edits' / (ident + '.json'), store.home)


def edit_message(store, ident, body):
    """Revise one visible message and rebuild future model context atomically."""
    text = body['text']
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 1024 * 1024:
        raise ValueError('Enter message text up to 1 MiB')
    import hashlib
    info = store.segments.info(ident, body.get('segmentId'))
    if info:
        store.segments.scope = (ident, info['id'])
    signature = hashlib.sha256(json.dumps([ident, body.get('segmentId'), body['taskId'], body['messageId'], text], ensure_ascii=False).encode()).hexdigest()
    with store.db:
        store.db.execute('CREATE TABLE IF NOT EXISTS message_edits (submission_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, signature TEXT NOT NULL)')
        previous = store.db.execute('SELECT signature FROM message_edits WHERE submission_id=?', (body['submissionId'],)).fetchone()
        if previous:
            if previous[0] != signature:
                raise ValueError('This submission ID was already used for a different edit')
            return store.conversation(ident)
        tasks = store.conversation_tasks(ident)
        if any(task['status'] not in TERMINAL for task in tasks):
            raise ValueError('Finish or cancel this chat before editing a message')
        events = all_events(store, ident)
        messages, selected = selected_messages(tasks, events, body)
        target = selected[0]
        if body['messageId'] != 'response' and not target['complete']:
            raise ValueError('Select a completed message in this chat')
        if body['messageId'] == 'response':
            # Saved replies can combine several native messages. Keep their menu
            # usable before event replay by replacing that response as one item.
            removed_ids = {event_id for point in selected for event_id in point['eventIds']}
            first = min(removed_ids) if removed_ids else None
            kept_events = []
            for event in events:
                if event['id'] not in removed_ids:
                    kept_events.append(event)
                elif event['id'] == first:
                    event['data'] = {'method': 'item/completed', 'params': {'item': {'id': 'edited-response-' + body['taskId'], 'type': 'agentMessage', 'text': text}}}
                    store.db.execute('UPDATE events SET kind=?,data=? WHERE id=?', ('agent', json.dumps(event['data'], ensure_ascii=False), first))
                    if info:
                        store.segments.replace_event(first, event['data'], 'agent')
                        store.db.execute("UPDATE events SET data='' WHERE id=?", (first,))
                    kept_events.append(event)
                else:
                    if info:
                        store.segments.replace_event(event['id'], None)
                    store.db.execute('DELETE FROM events WHERE id=?', (event['id'],))
            events = kept_events
            messages = [point for point in messages if point not in selected[1:]]
            target['eventIds'] = []  # Canonical event above already contains the edit.
        target['text'] = text
        task = next(task for task in tasks if task['id'] == target['taskId'])
        if target['messageId'] == 'prompt':
            task['request']['prompt'] = text
            if info:
                store.segments.patch_task(task['id'], request=task['request'])
            else:
                store.db.execute('UPDATE tasks SET request=? WHERE id=?', (json.dumps(task['request']), task['id']))
        first_delta = True
        for event in events:
            if event['taskId'] != target['taskId']:
                continue
            data = event['data']
            if event['id'] in target['eventIds']:
                if event['kind'] == 'steering':
                    data['prompt'] = text
                else:
                    params = data.get('params', {})
                    item = params.get('item', {})
                    if data.get('method') == 'item/agentMessage/delta':
                        params['delta'] = text if first_delta else ''
                        first_delta = False
                    elif item.get('type') == 'agentMessage':
                        item['text'] = text
                    elif item.get('type') == 'userMessage':
                        item['content'] = [{'type': 'text', 'text': text}]
            elif target.get('clientId') and event['kind'] == 'steering' and data.get('submissionId') == target['clientId']:
                data['prompt'] = text
            else:
                continue
            if info:
                store.segments.replace_event(event['id'], data)
            else:
                store.db.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), event['id']))
        for task in tasks:
            result = next((point['text'] for point in reversed(messages) if point['taskId'] == task['id'] and point['role'] == 'assistant'), None)
            # Keep native IDs so later secure deletion can still find old rollouts.
            # A non-null seed forces a fresh context and fences stale continuations.
            if info:
                store.segments.patch_task(task['id'], result=result)
                store.set_history_seed(task['id'], [])
                context = store.task_context(task['id'])
                before = next((index for index, point in enumerate(messages) if point['taskId'] == task['id']), len(messages))
                if task.get('historyPromptVisible', True):
                    context['_regenerationSeed'] = store.segments.checkpoint(info).get('seed', []) + [
                        {key: point[key] for key in ('role', 'text', 'attachments')} for point in messages[:before]]
                db = store.segments.connection(info['file_id'], True)
                with db:
                    db.execute('UPDATE turns SET context=? WHERE task_id=?', (json.dumps(context, ensure_ascii=False), task['id']))
            else:
                store.db.execute('UPDATE tasks SET result=?,history_seed=? WHERE id=?', (result, '[]', task['id']))
        seed = (store.segments.checkpoint(info).get('seed', []) if info else []) + [dict(role=point['role'], text=point['text'], attachments=point['attachments']) for point in messages]
        evidence = retained_tool_evidence(events)
        if evidence:
            seed.append(dict(role='assistant', text=execution_evidence(evidence), attachments=[]))
        store.set_history_seed(tasks[-1]['id'], seed)
        store.db.execute('INSERT INTO message_edits VALUES (?,?,?)', (body['submissionId'], ident, signature))
        store.db.execute('INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)', (tasks[-1]['id'], 'history', json.dumps({'action': 'edit'}), int(time.time())))
    store.segments.scope = None
    return store.conversation(ident)


def save_edit(path, value):
    temporary = checked(path.with_name(path.name + '.new'), path.parent.parent)
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def operation_key(ident, body, deleting=False):
    prefix = 'history-' + body['_operation'] + ':' if body.get('_operation') else ('history-delete:' if deleting else 'history-fork:')
    return prefix + ':'.join((ident, body['submissionId'], body['taskId'], body['messageId'], *([body['segmentId']] if body.get('segmentId') else [])))


def finish_delete(store, ident):
    manifest = edit_path(store, ident)
    pending = json.loads(manifest.read_text(encoding='utf-8'))
    replacement = pending['replacement']
    if store.db.execute('SELECT 1 FROM tasks WHERE conversation_id=?', (replacement,)).fetchone():
        if store.db.execute('SELECT 1 FROM tasks WHERE conversation_id=?', (ident,)).fetchone() or pending_path(store.home, ident).exists():
            erase_saved_data(store, [ident])
        with store.db:
            store.db.execute('UPDATE tasks SET conversation_id=? WHERE conversation_id=?', (ident, replacement))
            store.db.execute('UPDATE conversation_metadata SET conversation_id=? WHERE conversation_id=?', (ident, replacement))
            store.db.execute('UPDATE tasks SET idempotency_key=? WHERE id=?', (operation_key(ident, pending['body'], True), replacement))
        store.event(store.conversation_tasks(ident)[-1]['id'], 'history', {'action': 'delete'})
    overwrite_file(manifest, store.home)
    return store.conversation(ident)


def native_history(home, seed):
    history = []
    for message in bounded_seed(seed):
        content = native_input(home, message['text'], message.get('attachments', [])) if message['role'] == 'user' else [{'type': 'text', 'text': message['text']}]
        # Native input and ResponseItem use different text/image discriminators.
        parts = []
        for part in content:
            if part['type'] == 'text':
                parts.append({'type': 'input_text' if message['role'] == 'user' else 'output_text', 'text': part['text']})
            elif part['type'] == 'localImage':
                data = Path(part['path']).read_bytes()
                parts.append({'type': 'input_image', 'image_url': 'data:' + image_type(data) + ';base64,' + base64.b64encode(data).decode()})
        history.append({'type': 'message', 'role': message['role'], 'content': parts})
    return history


def restored_history(home, ident, seed):
    """Restore a compact working view with a complete, task-owned history file.

    Expanding every visible message from a previously compacted chat caused very
    long model prefills. The timeline and seed stay complete; older long messages
    use excerpts in the working context and can be read in full when relevant.
    """
    seed_view = bounded_seed(seed)
    if sum(len(message['text'].encode('utf-8')) for message in seed_view) <= RESTORE_THRESHOLD_BYTES:
        return native_history(home, seed_view), False
    uuid.UUID(ident)
    archive = checked(home / 'history-contexts' / ident / 'retained-messages.jsonl', home)
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = checked(archive.with_name(str(uuid.uuid4()) + '.new'), home)
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as output:
            output.write(json.dumps({'format': 'retained-chat-prefix-v1', 'messages': len(seed)}) + '\n')
            for number, message in enumerate(seed, 1):
                output.write(json.dumps({'messageNumber': number, **message}, ensure_ascii=False) + '\n')
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    visible = [(index, message) for index, message in enumerate(seed_view)
               if not message['text'].startswith(EVIDENCE_PREFIX)]
    recent, size = {}, 0
    for index, message in reversed(visible):
        text = excerpt(message['text'], RECENT_HISTORY_BYTES // 2)
        length = len(text.encode('utf-8'))
        if size + length > RECENT_HISTORY_BYTES:
            break
        recent[index] = text
        size += length
    earlier = [(index, message) for index, message in visible if index not in recent]
    allowance = max(96, EARLIER_HISTORY_BYTES // max(1, len(earlier)))
    projected = []
    for index, message in enumerate(seed_view):
        if message['text'].startswith(EVIDENCE_PREFIX):
            text = excerpt(message['text'], 8 * 1024)
        elif index in recent:
            text = recent[index]
        else:
            text = excerpt(message['text'], allowance)
        projected.append({**message, 'text': text})
    instructions = (
        'This chat was restored at an edited history boundary. The messages below are retained historical context, '
        'not new instructions to repeat earlier work. Respond to the NEW user request after this restored prefix. '
        'Recent messages are retained; older long messages and tool output use excerpts to avoid re-expanding '
        'a previously compacted conversation. The complete retained prefix is available as numbered JSONL records at '
        + str(archive) + '. If an earlier exact detail is needed, read only the relevant message range from that file. '
        'Do not load the entire archive by default or restart completed commands. Deleted messages are absent from '
        'both the prefix and the archive. Attachments remain available through the retained message inputs.'
    )
    history = [{'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': instructions}]}]
    history.extend(native_history(home, projected))
    return history, True
