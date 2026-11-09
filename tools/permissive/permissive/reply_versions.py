"""Durable response branches. Dispatch needs context, not a cloned transcript."""
import copy
import json
import os
import shutil
import time
import uuid

from .store import TERMINAL


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS chat_families(branch_id TEXT PRIMARY KEY,family_id TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS chat_family ON chat_families(family_id);
        CREATE TABLE IF NOT EXISTS reply_versions(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,branch_id TEXT NOT NULL,ordinal INTEGER NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS reply_group_ordinal ON reply_versions(group_id,ordinal);
        CREATE TABLE IF NOT EXISTS reply_selections(branch_id TEXT NOT NULL,group_id TEXT NOT NULL,version_id TEXT NOT NULL,
            task_id TEXT NOT NULL,prompt_index INTEGER NOT NULL,segment_number INTEGER NOT NULL,
            PRIMARY KEY(branch_id,group_id));
        CREATE TABLE IF NOT EXISTS regeneration_jobs(branch_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,task_id TEXT NOT NULL,
            body TEXT NOT NULL,mapping TEXT NOT NULL,phase TEXT NOT NULL DEFAULT 'pending',error TEXT);
        CREATE TABLE IF NOT EXISTS regeneration_events(branch_id TEXT NOT NULL,source_event INTEGER NOT NULL,event_id INTEGER NOT NULL,
            PRIMARY KEY(branch_id,source_event));
        CREATE UNIQUE INDEX IF NOT EXISTS regeneration_submission ON regeneration_jobs(source_id,json_extract(body,'$.submissionId'));
    ''')


def family(store, ident):
    row = store.db.execute('SELECT family_id FROM chat_families WHERE branch_id=?', (ident,)).fetchone()
    return row[0] if row else ident


def members(store, ident):
    return [row[0] for row in store.db.execute('SELECT branch_id FROM chat_families WHERE family_id=?', (family(store, ident),))] or [ident]


def pending(store, ident):
    return store.db.execute("""SELECT 1 FROM regeneration_jobs WHERE phase!='ready' AND
        (source_id=? OR branch_id=? OR branch_id IN (SELECT branch_id FROM chat_families WHERE family_id=?))""",
        (ident, ident, family(store, ident))).fetchone() is not None


def plan(store, ident, body):
    from .history import all_events, selected_messages, selection, retained_tool_evidence, execution_evidence
    info = store.segments.info(ident, body.get('segmentId'))
    store.segments.scope = (ident, info['id']) if info else None
    tasks, events = store.conversation_tasks(ident), all_events(store, ident)
    if any(task['status'] not in TERMINAL for task in tasks):
        raise ValueError('Finish or cancel this chat before regenerating')
    messages, selected = selected_messages(tasks, events, body)
    target = selected[-1]
    before = messages[:messages.index(target) + int(target['role'] == 'user')]
    task = next(task for task in tasks if task['id'] == body['taskId'])
    prompt = next((point for point in reversed(before) if point['taskId'] == task['id'] and point['role'] == 'user'), None)
    request = copy.deepcopy(task['request'])
    checkpoint = store.segments.checkpoint(info) if info else {'seed': [], 'status': 'ready'}
    context = copy.deepcopy(store.task_context(task['id']))
    if prompt:
        request.update(prompt=prompt['text'], attachments=prompt['attachments'])
        _, retained, kept_events, kept = selection(store, ident, {**body, 'messageId': prompt['messageId']}, False, (tasks, events))
        client = prompt.get('clientId')
        kept_events = [event for event in kept_events if event['id'] not in prompt['eventIds'] and not
                       (client and event['taskId'] == task['id'] and event['kind'] == 'steering' and event['data'].get('submissionId') == client)]
        seed = checkpoint.get('seed', []) + [{key: point[key] for key in ('role', 'text', 'attachments')} for point in kept]
        users = [point for point in messages if point['taskId'] == task['id'] and point['role'] == 'user']
        prompt_index = users.index(prompt)
    else:
        # A task spanning compaction may have its original prompt hidden here.
        index = tasks.index(task)
        retained = tasks[:index]
        kept_events = [event for event in events if event['taskId'] in {part['id'] for part in retained}]
        kept = [point for point in messages if point['taskId'] in {part['id'] for part in retained}]
        seed = context.get('_regenerationSeed')
        if seed is None:
            raise ValueError('This turn needs its saved context before regeneration')
        checkpoint = {'seed': seed, 'status': 'ready', 'provenance': 'regenerated-turn'}
        prompt_index = -1
    if not request.get('prompt') and not request.get('attachments'):
        raise ValueError('This turn has no retained prompt to regenerate')
    # At a task's original prompt use its saved pre-turn context. This also
    # handles a task whose original prompt belongs to an earlier segment.
    if prompt and prompt['messageId'] == 'prompt' and context.get('_regenerationSeed') is not None:
        seed = context['_regenerationSeed']
    evidence = retained_tool_evidence(kept_events)
    if evidence:
        seed = seed + [dict(role='assistant', text=execution_evidence(evidence), attachments=[])]
    request.pop('submissionId', None)
    request.pop('resumeTaskId', None)
    return dict(info=info, tasks=retained, events=kept_events, messages=kept, request=request,
                context=context, seed=seed, checkpoint=checkpoint, promptIndex=prompt_index, task=task, completeMessages=messages)


def prepare(store, ident, body, key):
    accepted = store.db.execute("SELECT branch_id,body FROM regeneration_jobs WHERE source_id=? AND json_extract(body,'$.submissionId')=?", (ident, body['submissionId'])).fetchone()
    if accepted:
        if json.loads(accepted['body']) != body:
            raise ValueError('This submission ID was already used for a different regeneration')
        return accepted['branch_id'], False
    existing = store.db.execute('SELECT conversation_id FROM tasks WHERE idempotency_key=?', (key,)).fetchone()
    if existing:
        return existing[0], False
    if store.db.execute('SELECT 1 FROM history_deletions WHERE source_id=?', (ident,)).fetchone():
        raise ValueError('This chat has been replaced. Open the updated chat.')
    if pending(store, ident):
        raise ValueError('Response branch preparation is still in progress')
    if store.db.execute("SELECT 1 FROM tasks WHERE conversation_id IN (SELECT branch_id FROM chat_families WHERE family_id=?) AND status NOT IN ('completed','cancelled','failed','interrupted')", (family(store, ident),)).fetchone():
        raise ValueError('Finish or cancel regeneration before choosing another version')
    value = plan(store, ident, body)
    branch, now = str(uuid.uuid4()), int(time.time())
    prefix = value['tasks']
    ids = [branch] + [str(uuid.uuid4()) for _ in prefix]
    mapping = {task['id']: ids[index] for index, task in enumerate(prefix)}
    execution = ids[-1]
    number = value['info']['number'] if value['info'] else 1
    root = family(store, ident)
    anchor = store.db.execute('SELECT * FROM reply_selections WHERE branch_id=? AND task_id=? AND prompt_index=? AND segment_number=?',
        (ident, body['taskId'], value['promptIndex'], number)).fetchone()
    if anchor is None and value['promptIndex'] == -1:
        anchor = store.db.execute('''SELECT * FROM reply_selections WHERE branch_id=? AND task_id=? AND
            prompt_index IN (0,-1) AND segment_number<? ORDER BY segment_number LIMIT 1''', (ident, body['taskId'], number)).fetchone()
    group = anchor['group_id'] if anchor else str(uuid.uuid4())
    version = str(uuid.uuid4())
    with store.db:
        for chat in (ident, branch):
            store.db.execute('INSERT OR IGNORE INTO chat_families VALUES (?,?)', (chat, root))
        # Reserve task identities in transcript order. The finalizer only fills
        # their history; it never overwrites the running execution task.
        for index in range(len(ids)):
            original = prefix[index] if index < len(prefix) else value['task']
            request = copy.deepcopy(original['request']) if index < len(prefix) else value['request']
            request.pop('submissionId', None)
            request.pop('resumeTaskId', None)
            if index:
                request['resumeTaskId'] = ids[index - 1]
            context = copy.deepcopy(store.task_context(original['id']))
            context['_segmentPromptVisible'] = original.get('historyPromptVisible', True) if index < len(prefix) else True
            if index == len(prefix):
                context['_regenerationSeed'] = value['seed']
            answers = [point['text'] for point in value['messages'] if point['taskId'] == original['id'] and point['role'] == 'assistant']
            store.db.execute('''INSERT INTO tasks(id,request,status,result,created_at,updated_at,completed_at,idempotency_key,
                workspace_id,workspace_context,conversation_id,diagnostic_log_level,history_seed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ids[index], json.dumps(request), 'completed' if index < len(prefix) else 'queued',
                 ('\n\n'.join(answers) or None) if index < len(prefix) else None, now, now, now if index < len(prefix) else None,
                 key if index == 0 else None, original['workspaceId'], json.dumps(context), branch, original['diagnosticLogLevel'],
                 json.dumps(value['seed']) if index == len(prefix) else None))
        store.db.execute('INSERT INTO conversation_metadata VALUES (?,?)', (branch, store.conversation_title(ident, value['tasks'] or [value['task']])))
        if not anchor:
            original_version = str(uuid.uuid4())
            store.db.execute('INSERT INTO reply_versions VALUES (?,?,?,1)', (original_version, group, ident))
            store.db.execute('INSERT INTO reply_selections VALUES (?,?,?,?,?,?)', (ident, group, original_version, body['taskId'], value['promptIndex'], number))
        ordinal = store.db.execute('SELECT max(ordinal)+1 FROM reply_versions WHERE group_id=?', (group,)).fetchone()[0]
        store.db.execute('INSERT INTO reply_versions VALUES (?,?,?,?)', (version, group, branch, ordinal))
        # Carry only version choices in the retained prefix. Earlier numbered
        # segments keep their existing immutable task identities.
        for row in store.db.execute('SELECT * FROM reply_selections WHERE branch_id=? AND group_id!=?', (ident, group)).fetchall():
            retained_users = [point for point in value['messages'] if point['taskId'] == row['task_id'] and point['role'] == 'user']
            if row['segment_number'] < number or row['task_id'] in mapping and row['prompt_index'] < len(retained_users):
                store.db.execute('INSERT INTO reply_selections VALUES (?,?,?,?,?,?)',
                    (branch, row['group_id'], row['version_id'], mapping.get(row['task_id'], row['task_id']), row['prompt_index'], row['segment_number']))
                store.db.execute('UPDATE reply_versions SET branch_id=? WHERE id=?', (branch, row['version_id']))
        store.db.execute('INSERT INTO reply_selections VALUES (?,?,?,?,?,?)', (branch, group, version, execution, 0, number))
        store.db.execute('INSERT INTO regeneration_jobs(branch_id,source_id,task_id,body,mapping) VALUES (?,?,?,?,?)',
            (branch, ident, execution, json.dumps(body), json.dumps(mapping)))
    return branch, True


def copy_history(store, job):
    """Idempotent bulk work, on a worker connection after task dispatch."""
    from .secure_delete import checked
    from .segments import compaction
    value = plan(store, job['source_id'], json.loads(job['body']))
    mapping = json.loads(job['mapping'])
    branch = job['branch_id']
    for original, destination_id in mapping.items():
        root = checked(store.home / 'artifacts' / original, store.home)
        destination = checked(store.home / 'artifacts' / destination_id, store.home)
        # Partial tasks must not inherit outputs from the discarded response.
        full = [point for point in value['completeMessages'] if point['taskId'] == original]
        retained = [point for point in value['messages'] if point['taskId'] == original]
        if root.exists() and full == retained:
            for path in root.rglob('*'):
                checked(path, store.home)
            shutil.copytree(root, destination, dirs_exist_ok=True)
            for path in destination.rglob('*'):
                if path.is_file():
                    with path.open('r+b') as output:
                        os.fsync(output.fileno())
    events = [event for event in value['events'] if event['taskId'] in mapping and not compaction(event['data'])]
    for offset in range(0, len(events), 128):
        # Serialization and filesystem work never hold the shared WAL writer.
        # Small commits let the live response continue recording its own events.
        batch = [(event['id'], mapping[event['taskId']], event['kind'], json.dumps(event['data']), event['createdAt'])
                 for event in events[offset:offset + 128]]
        with store.db:
            for event_id, task_id, kind, data, created in batch:
                if store.db.execute('SELECT 1 FROM regeneration_events WHERE branch_id=? AND source_event=?', (branch, event_id)).fetchone():
                    continue
                cursor = store.db.execute('INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)', (task_id, kind, data, created))
                store.db.execute('INSERT INTO regeneration_events VALUES (?,?,?)', (branch, event_id, cursor.lastrowid))
        time.sleep(0)
    with store.db:
        store.db.execute("UPDATE regeneration_jobs SET phase='copied',error=NULL WHERE branch_id=?", (branch,))


def publish(store, job):
    # The caller waits for execution to stop before moving live transcript data
    # into segments, avoiding a second writer racing native event persistence.
    branch = job['branch_id']
    marker = store.home / 'chat-segments' / ('branch-' + branch + '.json')
    if marker.exists():
        saved = json.loads(marker.read_text(encoding='utf-8'))
        store.segments.references(branch, saved['source'], recovering=saved)
    value = plan(store, job['source_id'], json.loads(job['body']))
    if not store.segments.ready(branch):
        store.segments.migrate(branch, initial_checkpoint=value['checkpoint'])
    info = value['info']
    first = store.db.execute('SELECT file_id FROM chat_segments WHERE chat_id=? ORDER BY number LIMIT 1', (branch,)).fetchone()
    if info and info['number'] > 1 and first and first[0] == branch + '-1':
        store.segments.references(branch, info)
    with store.db:
        store.db.execute("UPDATE regeneration_jobs SET phase='ready',error=NULL WHERE branch_id=?", (branch,))
        store.db.execute('DELETE FROM regeneration_events WHERE branch_id=?', (branch,))


def metadata(store, ident, tasks, segment=None):
    from .history import points
    number = segment['number'] if segment else 1
    choices = store.db.execute('SELECT * FROM reply_selections WHERE branch_id=? AND segment_number<=?', (ident, number)).fetchall()
    choices = [row for row in choices if row['task_id'] in {task['id'] for task in tasks}]
    messages = []
    # Controls are disabled for live responses, so locating their first token
    # must not rescan a long transcript on every status/usage refresh.
    for task in tasks:
        if task['id'] not in {row['task_id'] for row in choices} or task['status'] not in TERMINAL:
            continue
        if segment:
            messages.extend(store.segments.reply_points(segment, task))
        else:
            rows = store.db.execute('SELECT * FROM events WHERE task_id=? ORDER BY id', (task['id'],)).fetchall()
            events = [dict(id=row['id'], taskId=row['task_id'], kind=row['kind'], data=json.loads(row['data']), createdAt=row['created_at']) for row in rows]
            messages.extend(points([task], events))
    result = []
    for choice in choices:
        task_messages = [point for point in messages if point['taskId'] == choice['task_id']]
        users = [point for point in task_messages if point['role'] == 'user']
        index = choice['prompt_index']
        prompt = users[index] if choice['segment_number'] == number and 0 <= index < len(users) else None
        after = task_messages[task_messages.index(prompt) + 1:] if prompt else task_messages
        answers = []
        for point in after:
            if point['role'] == 'user':
                break
            answers.append(point)
        variants = [dict(id=row['id'], conversationId=row['branch_id'], ordinal=row['ordinal'])
                    for row in store.db.execute('SELECT * FROM reply_versions WHERE group_id=? ORDER BY ordinal', (choice['group_id'],))]
        result.append(dict(groupId=choice['group_id'], selectedVersionId=choice['version_id'], taskId=choice['task_id'],
                           messageId=answers[-1]['messageId'] if answers else None, versions=variants))
    jobs = store.db.execute("""SELECT phase,error FROM regeneration_jobs WHERE phase!='ready' AND
        (branch_id=? OR source_id=? OR branch_id IN (SELECT branch_id FROM chat_families WHERE family_id=?))""",
        (ident, ident, family(store, ident))).fetchall()
    running = store.db.execute("SELECT 1 FROM tasks WHERE conversation_id IN (SELECT branch_id FROM chat_families WHERE family_id=?) AND status NOT IN ('completed','cancelled','failed','interrupted') LIMIT 1", (family(store, ident),)).fetchone()
    return dict(chatGroupId=family(store, ident), replyVersions=result, branchPreparing=bool(jobs),
                versionsBusy=bool(jobs or running), branchPreparationError=next((row['error'] for row in jobs if row['error']), None))


def replace_branch(store, source, replacement, mapping, original_points, retained_points):
    """Destructive edits replace one branch, never its sibling replies."""
    root = family(store, source)
    if not store.db.execute('SELECT 1 FROM chat_families WHERE branch_id=?', (source,)).fetchone():
        return
    store.db.execute('INSERT OR REPLACE INTO chat_families VALUES (?,?)', (replacement, root))
    selected_number = store.segments.info(replacement)['number']
    for row in store.db.execute('SELECT * FROM reply_selections WHERE branch_id=?', (source,)).fetchall():
        if row['segment_number'] < selected_number:
            new_task, index = row['task_id'], row['prompt_index']
        elif row['task_id'] in mapping:
            before = [point for point in original_points if point['taskId'] == row['task_id'] and point['role'] == 'user']
            after = [point for point in retained_points if point['taskId'] == row['task_id'] and point['role'] == 'user']
            index = row['prompt_index']
            if index >= 0:
                if index >= len(before) or before[index] not in after:
                    continue
                index = after.index(before[index])
            new_task = mapping[row['task_id']]
        else:
            continue
        if new_task:
            store.db.execute('INSERT OR REPLACE INTO reply_selections VALUES (?,?,?,?,?,?)',
                (replacement, row['group_id'], row['version_id'], new_task, index, row['segment_number']))
    store.db.execute('UPDATE reply_versions SET branch_id=? WHERE branch_id=?', (replacement, source))
    job = store.db.execute('SELECT task_id FROM regeneration_jobs WHERE branch_id=?', (source,)).fetchone()
    if job:
        store.db.execute('UPDATE regeneration_jobs SET branch_id=?,task_id=? WHERE branch_id=?', (replacement, mapping.get(job[0], replacement), source))
    store.db.execute('DELETE FROM reply_selections WHERE branch_id=?', (source,))
    store.db.execute('DELETE FROM chat_families WHERE branch_id=?', (source,))
