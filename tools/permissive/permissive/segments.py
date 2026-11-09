"""Numbered, independently readable SQLite transcript segments.

The task database indexes ownership and event positions. Transcript bodies and
compaction checkpoints live here; sealed files may be referenced by branches.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .secure_delete import checked


class CheckpointRequired(ValueError):
    pass


def compaction(data):
    if not isinstance(data, dict):
        return False
    return (data.get('method') == 'thread/compacted' or
            data.get('method') == 'item/completed' and
            (data.get('params', {}).get('item') or {}).get('type') == 'contextCompaction')


def summary_seed(text):
    return [{'role': 'assistant', 'text': 'Compacted earlier conversation:\n' + text, 'attachments': []}]


class Segments:
    def __init__(self, store):
        self.store = store
        self.home = store.home
        self.connections = {}
        self.reply_points_cache = {}
        self.scope = None
        self.busy = False
        self.compactions = {}
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS chat_storage (
                chat_id TEXT PRIMARY KEY, latest INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS chat_segments (
                chat_id TEXT NOT NULL, number INTEGER NOT NULL, file_id TEXT NOT NULL,
                start_event INTEGER NOT NULL, end_event INTEGER, sealed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id,number)
            );
            CREATE INDEX IF NOT EXISTS segment_file_refs ON chat_segments(file_id);
            CREATE TABLE IF NOT EXISTS task_segments (task_id TEXT PRIMARY KEY,file_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS segment_task_refs (file_id TEXT NOT NULL,task_id TEXT NOT NULL,PRIMARY KEY(file_id,task_id));
            CREATE TABLE IF NOT EXISTS segment_attachments (file_id TEXT NOT NULL,attachment_id TEXT NOT NULL,PRIMARY KEY(file_id,attachment_id));
            CREATE TABLE IF NOT EXISTS segment_pending (chat_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS event_payload_sizes(event_id INTEGER PRIMARY KEY,bytes INTEGER NOT NULL);
            CREATE TRIGGER IF NOT EXISTS segment_event_insert AFTER INSERT ON events WHEN NEW.data!=''
            BEGIN INSERT OR IGNORE INTO segment_pending SELECT conversation_id FROM tasks WHERE id=NEW.task_id; END;
            CREATE TRIGGER IF NOT EXISTS segment_event_update AFTER UPDATE OF data ON events WHEN NEW.data!=''
            BEGIN INSERT OR IGNORE INTO segment_pending SELECT conversation_id FROM tasks WHERE id=NEW.task_id; END;
            CREATE TRIGGER IF NOT EXISTS segment_task_insert AFTER INSERT ON tasks
            BEGIN INSERT OR IGNORE INTO segment_pending VALUES(NEW.conversation_id); END;
            CREATE TRIGGER IF NOT EXISTS segment_task_update AFTER UPDATE OF request,result,error,history_seed ON tasks
            WHEN json_type(NEW.request,'$.prompt') IS NOT NULL OR NEW.result IS NOT NULL OR NEW.error IS NOT NULL OR NEW.history_seed IS NOT NULL
            BEGIN INSERT OR IGNORE INTO segment_pending VALUES(NEW.conversation_id); END;
        ''')
        journals = (self.home / 'chat-segments').glob('branch-*.json') if getattr(store, 'recover_segment_branches', True) else []
        for journal in journals:
            pending = json.loads(checked(journal, self.home).read_text(encoding='utf-8'))
            chat = pending['chat']
            if not any(store.db.execute(sql, (chat,)).fetchone() for sql in (
                    'SELECT 1 FROM tasks WHERE conversation_id=?',
                    'SELECT 1 FROM chat_storage WHERE chat_id=?',
                    'SELECT 1 FROM chat_segments WHERE chat_id=?')):
                # A retired staging branch may already have been erased. Its
                # recovery marker must not revive it or prevent every chat loading.
                from .secure_delete import overwrite_file
                overwrite_file(journal, self.home)
                continue
            self.references(pending['chat'], pending['source'], recovering=pending)

    def path(self, file_id):
        root, number = file_id.rsplit('-', 1)
        uuid.UUID(root)
        if not number.isdigit() or int(number) < 1:
            raise ValueError('Invalid chat segment')
        return checked(self.home / 'chat-segments' / (file_id + '.sqlite'), self.home)

    def connection(self, file_id, write=False):
        key = (file_id, write)
        if key not in self.connections:
            path = self.path(file_id)
            if not path.is_file():
                raise KeyError(file_id)
            db = sqlite3.connect(path if write else path.resolve().as_uri() + '?mode=ro', uri=not write, timeout=30)
            db.row_factory = sqlite3.Row
            if write:
                db.execute('PRAGMA journal_mode=WAL')
            self.connections[key] = db
        return self.connections[key]

    def close_file(self, file_id):
        self.reply_points_cache = {key: value for key, value in self.reply_points_cache.items() if key[0] != file_id}
        for key in sorted(list(self.connections), key=lambda value: value[1]):
            if key[0] == file_id:
                db = self.connections.pop(key)
                if key[1]:
                    try:
                        db.execute('PRAGMA wal_checkpoint(PASSIVE)')
                    except sqlite3.Error:
                        pass  # Another service connection may still own this file.
                db.close()

    def close(self):
        for file_id in {key[0] for key in self.connections}:
            self.close_file(file_id)

    def ready(self, chat_id):
        return bool(self.store.db.execute('SELECT 1 FROM chat_storage WHERE chat_id=?', (chat_id,)).fetchone())

    def info(self, chat_id, segment_id=None):
        latest = self.store.db.execute('SELECT latest FROM chat_storage WHERE chat_id=?', (chat_id,)).fetchone()
        if latest is None:
            return None
        number = latest[0]
        if segment_id is not None:
            prefix, suffix = str(segment_id).rsplit('-', 1)
            if prefix != chat_id or not suffix.isdigit() or int(suffix) < 1:
                raise ValueError('Select a segment in this chat')
            number = int(suffix)
        row = self.store.db.execute('SELECT * FROM chat_segments WHERE chat_id=? AND number=?', (chat_id, number)).fetchone()
        if row is None:
            raise KeyError(segment_id)
        return {**dict(row), 'id': chat_id + '-' + str(number), 'number': number,
                'latestId': chat_id + '-' + str(latest[0]),
                'previousId': chat_id + '-' + str(number - 1) if number > 1 else None}

    def checkpoint(self, info):
        row = self.connection(info['file_id']).execute("SELECT value FROM header WHERE key='checkpoint'").fetchone()
        return json.loads(row[0]) if row else {'seed': [], 'status': 'ready', 'provenance': 'initial'}

    def create_file(self, file_id, checkpoint, previous=None):
        path = self.path(file_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        temporary = checked(path.with_name(path.name + '.' + str(uuid.uuid4()) + '.new'), self.home)
        db = sqlite3.connect(temporary)
        try:
            db.executescript('''
                PRAGMA journal_mode=DELETE;
                CREATE TABLE header(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE turns(task_id TEXT PRIMARY KEY,value TEXT NOT NULL,context TEXT NOT NULL,seed TEXT,prompt_visible INTEGER NOT NULL);
                CREATE TABLE events(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE INDEX events_task ON events(task_id,id);
                CREATE TABLE messages(task_id TEXT NOT NULL,message_id TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(task_id,message_id));
            ''')
            with db:
                db.executemany('INSERT INTO header VALUES (?,?)', [('format', json.dumps(1)), ('checkpoint', json.dumps(checkpoint)),
                    ('fileId', json.dumps(file_id)), ('previous', json.dumps(previous))])
        finally:
            db.close()
        with temporary.open('r+b') as handle:
            os.fsync(handle.fileno())
        temporary.replace(path)

    def task_payload(self, ident, info=None, task_only=False):
        if info is None:
            row = self.store.db.execute('SELECT file_id FROM task_segments WHERE task_id=?', (ident,)).fetchone()
            if row is None:
                row = self.store.db.execute('SELECT r.file_id FROM segment_task_refs r JOIN chat_segments s ON s.file_id=r.file_id WHERE r.task_id=? ORDER BY s.number DESC LIMIT 1', (ident,)).fetchone()
            if row is None:
                return None
            file_id = row[0]
        else:
            file_id = info['file_id']
        row = self.connection(file_id).execute('SELECT * FROM turns WHERE task_id=?', (ident,)).fetchone()
        if row is None:
            return None
        return {'task': json.loads(row['value']), 'context': json.loads(row['context']) if not task_only else None,
                'seed': json.loads(row['seed']) if row['seed'] is not None and not task_only else None, 'promptVisible': bool(row['prompt_visible'])}

    def tasks(self, info):
        result = []
        for row in self.connection(info['file_id']).execute('SELECT * FROM turns ORDER BY rowid'):
            task = json.loads(row['value'])
            task['conversationId'] = info['chat_id']
            task['segmentId'] = info['id']
            task['historyPromptVisible'] = bool(row['prompt_visible'])
            if info['sealed'] and task['status'] not in {'completed', 'cancelled', 'failed', 'interrupted'}:
                task.update(status='completed', activeTurnId=None)
            if not info['sealed']:
                metadata = self.store.db.execute('SELECT status,thread_id,active_turn_id,updated_at,completed_at FROM tasks WHERE id=?', (task['id'],)).fetchone()
                if metadata:
                    task.update(status=metadata['status'], threadId=metadata['thread_id'], activeTurnId=metadata['active_turn_id'],
                                updatedAt=metadata['updated_at'], completedAt=metadata['completed_at'])
            result.append(task)
        return result

    def rows(self, info, after=0, through=None, limit=None, task_id=None):
        sql = 'SELECT * FROM events WHERE id>? AND (? IS NULL OR id<=?) AND (? IS NULL OR task_id=?) ORDER BY id'
        params = [max(after, info['start_event']), through, through, task_id, task_id]
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        return self.connection(info['file_id']).execute(sql, params).fetchall()

    def cursor(self, info):
        return self.connection(info['file_id']).execute('SELECT coalesce(max(id),?) FROM events', (info['start_event'],)).fetchone()[0]

    def reply_points(self, info, task):
        """Read only the selected turn, reusing its stable message identities."""
        from .history import points
        db = self.connection(info['file_id'])
        latest = db.execute('SELECT coalesce(max(id),0) FROM events WHERE task_id=?', (task['id'],)).fetchone()[0]
        key = (info['file_id'], task['id'])
        signature = (latest, task['request'].get('prompt'), task.get('result'), task.get('historyPromptVisible', True))
        cached = self.reply_points_cache.get(key)
        if cached is None or cached[0] != signature:
            events = [dict(id=row['id'], taskId=row['task_id'], kind=row['kind'], data=json.loads(row['data']), createdAt=row['created_at'])
                      for row in self.rows(info, task_id=task['id'])]
            cached = (signature, points([task], events))
            self.reply_points_cache[key] = cached
        return cached[1]

    def put_task(self, task, context, seed=None, prompt_visible=True, info=None):
        info = info or self.info(task['conversationId'])
        db = self.connection(info['file_id'], True)
        with db:
            db.execute('INSERT INTO turns VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET value=excluded.value,context=excluded.context,seed=excluded.seed,prompt_visible=excluded.prompt_visible',
                (task['id'], json.dumps(task, ensure_ascii=False), json.dumps(context), json.dumps(seed) if seed is not None else None, int(prompt_visible)))
        self.store.db.execute('INSERT OR REPLACE INTO task_segments VALUES (?,?)', (task['id'], info['file_id']))
        self.store.db.execute('INSERT OR IGNORE INTO segment_task_refs VALUES (?,?)', (info['file_id'], task['id']))
        for attachment in task['request'].get('attachments', []):
            self.store.db.execute('INSERT OR IGNORE INTO segment_attachments VALUES (?,?)', (info['file_id'], attachment))
        for message in (seed or []) + context.get('_regenerationSeed', []):
            for attachment in message.get('attachments', []):
                self.store.db.execute('INSERT OR IGNORE INTO segment_attachments VALUES (?,?)', (info['file_id'], attachment))

    def patch_task(self, ident, **changes):
        location = self.store.db.execute('SELECT file_id FROM task_segments WHERE task_id=?', (ident,)).fetchone()
        if not location:
            return
        db = self.connection(location[0], True)
        row = db.execute('SELECT value FROM turns WHERE task_id=?', (ident,)).fetchone()
        task = json.loads(row[0])
        task.update(changes)
        with db:
            db.execute('UPDATE turns SET value=? WHERE task_id=?', (json.dumps(task, ensure_ascii=False), ident))

    def put_event(self, info, event_id, task_id, kind, data, created_at):
        self.reply_points_cache.pop((info['file_id'], task_id), None)
        db = self.connection(info['file_id'], True)
        with db:
            db.execute('INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)', (event_id, task_id, kind, json.dumps(data, ensure_ascii=False), created_at))
            if kind == 'agent' and compaction(data):
                db.execute("INSERT OR REPLACE INTO header VALUES ('pendingCompaction',?)", (json.dumps({'task': task_id, 'boundary': event_id, 'notification': data}),))
        for attachment in data.get('attachments', []) if isinstance(data, dict) else []:
            if isinstance(attachment, str):
                self.store.db.execute('INSERT OR IGNORE INTO segment_attachments VALUES (?,?)', (info['file_id'], attachment))

    def recover_checkpoint(self, tasks, ordinal):
        threads = list(dict.fromkeys(task['threadId'] for task in tasks if task.get('threadId')))
        checkpoints = []
        for thread in threads:
            for folder in ('sessions', 'archived_sessions'):
                for path in (self.home / 'native-home' / folder).glob('**/*' + thread + '*.jsonl'):
                    checked(path, self.home)
                    with path.open(encoding='utf-8') as stream:
                        for line in stream:
                            value = json.loads(line)
                            if value.get('type') == 'compacted':
                                payload = value.get('payload', {})
                                text = payload.get('message', '')
                                native = payload.get('replacement_history')
                                if not text and native:
                                    text = '\n\n'.join(part.get('text', '') for item in native if item.get('type') == 'message' for part in item.get('content', []))
                                if text:
                                    checkpoints.append({'seed': summary_seed(text), 'nativeHistory': native, 'nativeOrdinal': len(checkpoints),
                                                        'status': 'ready', 'provenance': 'native'})
        if ordinal is None:
            return checkpoints[-1] if checkpoints else None
        return checkpoints[ordinal] if ordinal < len(checkpoints) else None

    def migrate(self, chat_id, summarize=None, initial_checkpoint=None):
        """Validate files before publishing pointers and retiring legacy bodies."""
        if self.ready(chat_id):
            self.sync_pending(chat_id)
            return self.info(chat_id)
        tasks = self.store.conversation_tasks(chat_id, all_segments=True)
        rows = self.store.db.execute('SELECT e.* FROM events e JOIN tasks t ON t.id=e.task_id WHERE t.conversation_id=? ORDER BY e.id', (chat_id,)).fetchall()
        events = [dict(row) for row in rows]
        markers, modern_turns = [], set()
        for row in events:
            data = json.loads(row['data'])
            params = data.get('params', {}) if isinstance(data, dict) else {}
            if compaction(data) and data.get('method') == 'item/completed':
                modern_turns.add((row['task_id'], params.get('turnId')))
        for row in events:
            data = json.loads(row['data'])
            if not compaction(data):
                continue
            if data.get('method') == 'thread/compacted' and (row['task_id'], data.get('params', {}).get('turnId')) in modern_turns:
                continue
            markers.append(row['id'])
        from .history import points
        bounds = [0, *markers]
        files, first_ids, last_ids = [], {}, {}
        for row in events:
            first_ids.setdefault(row['task_id'], row['id'])
            last_ids[row['task_id']] = row['id']
        checkpoints = [initial_checkpoint or {'seed': [], 'status': 'ready', 'provenance': 'initial'}]
        for ordinal, boundary in enumerate(markers):
            staged_id = chat_id + '-' + str(ordinal + 2)
            staged_path = self.path(staged_id)
            staged = self.connection(staged_id).execute("SELECT value FROM header WHERE key='checkpoint'").fetchone() if staged_path.exists() else None
            checkpoint = json.loads(staged[0]) if staged else self.recover_checkpoint(tasks, ordinal)
            if checkpoint is None:
                if summarize is None:
                    raise CheckpointRequired('A compaction checkpoint needs reconstruction')
                prior = [{'id': row['id'], 'taskId': row['task_id'], 'kind': row['kind'], 'data': json.loads(row['data']), 'createdAt': row['created_at']}
                         for row in events if row['id'] <= boundary]
                text = summarize(points(tasks, prior))
                checkpoint = {'seed': summary_seed(text), 'status': 'ready', 'provenance': 'reconstructed'}
            self.create_file(staged_id, checkpoint, chat_id + '-' + str(ordinal + 1))
            checkpoints.append(checkpoint)
        regeneration = {}
        for index, start in enumerate(bounds):
            number = index + 1
            end = bounds[index + 1] if index + 1 < len(bounds) else None
            file_id = chat_id + '-' + str(number)
            self.create_file(file_id, checkpoints[index], chat_id + '-' + str(number - 1) if number > 1 else None)
            db = self.connection(file_id, True)
            selected = [row for row in events if row['id'] > start and (end is None or row['id'] <= end)]
            ids = {row['task_id'] for row in selected}
            if start == 0:
                ids.update(task['id'] for task in tasks if task['id'] not in first_ids)
            if end is None:
                ids.add(tasks[-1]['id'])
            projected = [{'id': row['id'], 'taskId': row['task_id'], 'kind': row['kind'], 'data': json.loads(row['data']), 'createdAt': row['created_at']} for row in selected]
            with db:
                db.execute('DELETE FROM turns')
                db.execute('DELETE FROM events')
                db.execute('DELETE FROM messages')
                db.executemany('INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)',
                    ((row['id'], row['task_id'], row['kind'], row['data'], row['created_at']) for row in selected))
                segment_tasks = []
                for original in tasks:
                    if original['id'] not in ids:
                        continue
                    task = copy.deepcopy(original)
                    context = self.store.task_context(task['id'])
                    provided_base = context.get('_regenerationSeed') if context.get('_segmentPromptVisible') is False else None
                    visible = context.pop('_segmentPromptVisible', first_ids.get(task['id'], start + 1) > start)
                    task['historyPromptVisible'] = visible
                    if end is not None and last_ids.get(task['id'], 0) > end:
                        task['result'] = None
                    if start and last_ids.get(task['id'], 0) <= start:
                        task['result'] = None
                    task_points = points([task], [event for event in projected if event['taskId'] == task['id']])
                    answers = [point['text'] for point in task_points if point['role'] == 'assistant']
                    task['result'] = original.get('result') if start == 0 and end is None else '\n\n'.join(answers) or None
                    if task['id'] not in regeneration:
                        prior_events = [event for event in projected if event['taskId'] != task['id']]
                        regeneration[task['id']] = provided_base if provided_base is not None else checkpoints[index].get('seed', []) + [
                            {key: point[key] for key in ('role', 'text', 'attachments')}
                            for point in points(segment_tasks, prior_events)]
                    context['_regenerationSeed'] = regeneration[task['id']]
                    segment_tasks.append(task)
                    seed = self.store.history_seed(task['id']) if end is None else None
                    db.execute('INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?)', (task['id'], json.dumps(task, ensure_ascii=False), json.dumps(context), json.dumps(seed) if seed is not None else None, int(visible)))
                    for point in task_points:
                        db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?)', (task['id'], point['messageId'], json.dumps(point, ensure_ascii=False)))
            if db.execute('SELECT count(*) FROM events').fetchone()[0] != len(selected):
                raise ValueError('Segment event validation failed')
            expected = hashlib.sha256()
            actual = hashlib.sha256()
            for row in selected:
                expected.update(json.dumps([row[key] for key in ('id', 'task_id', 'kind', 'data')] + [float(row['created_at'])], ensure_ascii=False).encode('utf-8'))
            for row in db.execute('SELECT * FROM events ORDER BY id'):
                actual.update(json.dumps([row[key] for key in ('id', 'task_id', 'kind', 'data')] + [float(row['created_at'])], ensure_ascii=False).encode('utf-8'))
            if expected.digest() != actual.digest():
                raise ValueError('Segment transcript validation failed')
            files.append((number, file_id, start, end, int(end is not None)))
        # Segment files are committed first. A stopped migration is safe to rerun;
        # no old transcript body is removed until every file validates.
        with self.store.db:
            for number, file_id, start, end, sealed in files:
                self.store.db.execute('INSERT OR REPLACE INTO chat_segments VALUES (?,?,?,?,?,?)', (chat_id, number, file_id, start, end, sealed))
                for row in self.connection(file_id).execute('SELECT task_id,value FROM turns'):
                    self.store.db.execute('INSERT OR REPLACE INTO task_segments VALUES (?,?)', (row['task_id'], file_id))
                    self.store.db.execute('INSERT OR IGNORE INTO segment_task_refs VALUES (?,?)', (file_id, row['task_id']))
                    task = json.loads(row['value'])
                    for attachment in task['request'].get('attachments', []):
                        self.store.db.execute('INSERT OR IGNORE INTO segment_attachments VALUES (?,?)', (file_id, attachment))
                for row in self.connection(file_id).execute('SELECT context,seed FROM turns'):
                    for message in (json.loads(row['seed']) if row['seed'] else []) + json.loads(row['context']).get('_regenerationSeed', []):
                        for attachment in message.get('attachments', []):
                            self.store.db.execute('INSERT OR IGNORE INTO segment_attachments VALUES (?,?)', (file_id, attachment))
                for row in self.connection(file_id).execute("SELECT data FROM events WHERE kind='steering'"):
                    for attachment in json.loads(row[0]).get('attachments', []):
                        self.store.db.execute('INSERT OR IGNORE INTO segment_attachments VALUES (?,?)', (file_id, attachment))
            self.store.db.execute('INSERT INTO chat_storage VALUES (?,?,2)', (chat_id, len(files)))
            self.store.db.execute('INSERT OR IGNORE INTO conversation_metadata VALUES (?,?)', (chat_id, tasks[0]['request'].get('prompt', '')[:110] or 'Chat'))
            self.store.db.execute("UPDATE events SET data='' WHERE task_id IN (SELECT id FROM tasks WHERE conversation_id=?)", (chat_id,))
            self.store.db.executemany('INSERT OR IGNORE INTO event_payload_sizes VALUES (?,?)', [(row['id'], len(row['data'].encode('utf-8'))) for row in events])
            for task in tasks:
                config = {key: value for key, value in task['request'].items() if key not in {'prompt', 'attachments'}}
                context = self.store.task_context(task['id'])
                context.pop('_regenerationSeed', None)
                context.pop('_segmentPromptVisible', None)
                self.store.db.execute('UPDATE tasks SET request=?,workspace_context=?,result=NULL,error=NULL,history_seed=NULL WHERE id=?', (json.dumps(config), json.dumps(context), task['id']))
            self.store.db.execute('DELETE FROM segment_pending WHERE chat_id=?', (chat_id,))
        return self.info(chat_id)

    def sync_pending(self, chat_id):
        if self.busy or not self.ready(chat_id) or not self.store.db.execute('SELECT 1 FROM segment_pending WHERE chat_id=?', (chat_id,)).fetchone():
            return
        self.busy = True
        try:
            info = self.info(chat_id)
            for row in self.store.db.execute("SELECT * FROM tasks WHERE conversation_id=? AND (json_type(request,'$.prompt') IS NOT NULL OR result IS NOT NULL OR error IS NOT NULL OR history_seed IS NOT NULL OR id NOT IN (SELECT task_id FROM task_segments)) ORDER BY rowid", (chat_id,)).fetchall():
                pending = json.loads(row['request'])
                old = self.task_payload(row['id'])
                if 'prompt' not in pending and old is not None:
                    changes = {key: row[key] for key in ('result', 'error') if row[key] is not None}
                    if changes:
                        self.patch_task(row['id'], **changes)
                    if row['history_seed'] is not None:
                        self.store.set_history_seed(row['id'], json.loads(row['history_seed']))
                else:
                    task = self.store._get_legacy(row['id'])
                    context = old['context'] if old else json.loads(row['workspace_context'])
                    from .history import points
                    if old is None:
                        earlier = self.tasks(info)
                        context['_regenerationSeed'] = self.checkpoint(info).get('seed', []) + [
                            {key: point[key] for key in ('role', 'text', 'attachments')}
                            for point in points(earlier, self.store.conversation_events_all(chat_id))]
                    self.put_task(task, context, json.loads(row['history_seed']) if row['history_seed'] is not None else None, info=info)
                config = {key: value for key, value in pending.items() if key not in {'prompt', 'attachments'}}
                context = json.loads(row['workspace_context'])
                context.pop('_regenerationSeed', None)
                self.store.db.execute('UPDATE tasks SET request=?,workspace_context=?,result=NULL,error=NULL,history_seed=NULL WHERE id=?', (json.dumps(config), json.dumps(context), row['id']))
            for row in self.store.db.execute("SELECT e.* FROM events e JOIN tasks t ON t.id=e.task_id WHERE t.conversation_id=? AND e.data!='' ORDER BY e.id", (chat_id,)).fetchall():
                data = json.loads(row['data'])
                self.put_event(info, row['id'], row['task_id'], row['kind'], data, row['created_at'])
                self.store.db.execute('INSERT OR REPLACE INTO event_payload_sizes VALUES (?,?)', (row['id'], len(row['data'].encode('utf-8'))))
                self.store.db.execute("UPDATE events SET data='' WHERE id=?", (row['id'],))
                if row['kind'] == 'agent' and compaction(data):
                    info = self.rotate(chat_id, row['task_id'], row['id'], data)
            self.store.db.execute('DELETE FROM segment_pending WHERE chat_id=?', (chat_id,))
        finally:
            self.busy = False

    def rotate(self, chat_id, task_id, boundary, notification=None):
        old = self.info(chat_id)
        if old['start_event'] >= boundary:
            return old
        notification = notification or {}
        params = notification.get('params', {})
        turn = params.get('turnId')
        item_id = (params.get('item') or {}).get('id')
        identity = (task_id, turn, item_id)
        saved = self.connection(old['file_id']).execute("SELECT value FROM header WHERE key='lastCompaction'").fetchone()
        previous = self.compactions.get(chat_id) or (tuple(json.loads(saved[0])) if saved else None)
        if previous and (previous == identity or notification.get('method') == 'thread/compacted' and previous[:2] == identity[:2]):
            db = self.connection(old['file_id'], True)
            with db:
                db.execute("DELETE FROM header WHERE key='pendingCompaction'")
            return old
        self.compactions[chat_id] = identity
        number = old['number'] + 1
        file_id = chat_id + '-' + str(number)
        checkpoint = {'seed': [], 'status': 'pending', 'provenance': 'native'}
        self.create_file(file_id, checkpoint, old['id'])
        db = self.connection(file_id, True)
        with db:
            db.execute("INSERT OR REPLACE INTO header VALUES ('lastCompaction',?)", (json.dumps(identity),))
        task = self.store.get(task_id)
        task.update(result=None, error=None)
        payload = self.task_payload(task_id)
        from .history import points
        source = self.tasks(old)
        visible = self.store.conversation_events_all(chat_id, old['id'])
        checkpoint['recoveryMessages'] = self.checkpoint(old).get('seed', []) + [
            {key: point[key] for key in ('role', 'text', 'attachments')} for point in points(source, visible)]
        self.set_checkpoint({'file_id': file_id}, checkpoint)
        with self.store.db:
            self.store.db.execute('UPDATE chat_segments SET sealed=1,end_event=? WHERE chat_id=? AND number=?', (boundary, chat_id, old['number']))
            self.store.db.execute('INSERT INTO chat_segments VALUES (?,?,?,?,NULL,0)', (chat_id, number, file_id, boundary))
            self.store.db.execute('UPDATE chat_storage SET latest=? WHERE chat_id=?', (number, chat_id))
            self.put_task(task, payload['context'], None, False, self.info(chat_id))
        self.close_file(old['file_id'])
        return self.info(chat_id)

    def recover(self, chat_id, summarize=None):
        """Finish a rollover/checkpoint interrupted between durable steps."""
        version = self.store.db.execute('SELECT version FROM chat_storage WHERE chat_id=?', (chat_id,)).fetchone()[0]
        if version < 2:
            self.repair_contexts(chat_id)
        info = self.info(chat_id)
        marker = self.connection(info['file_id']).execute("SELECT value FROM header WHERE key='pendingCompaction'").fetchone()
        if marker:
            marker = json.loads(marker[0])
            info = self.rotate(chat_id, marker['task'], marker['boundary'], marker['notification'])
        checkpoint = self.checkpoint(info)
        if checkpoint.get('status') == 'pending':
            tasks = self.tasks(info)
            recovered = self.recover_checkpoint(tasks, None)
            if recovered is None:
                if summarize is None:
                    raise CheckpointRequired('Compaction checkpoint recovery is pending')
                recovered = {'seed': summary_seed(summarize(checkpoint.get('recoveryMessages', []))), 'status': 'ready', 'provenance': 'reconstructed'}
            self.set_checkpoint(info, recovered)
        return info

    def repair_contexts(self, chat_id):
        """Upgrade saved turn bases using each segment and its checkpoint only."""
        from .history import points
        bases = {}
        rows = self.store.db.execute('SELECT number FROM chat_segments WHERE chat_id=? ORDER BY number', (chat_id,)).fetchall()
        for row in rows:
            info = self.info(chat_id, chat_id + '-' + str(row[0]))
            tasks = self.tasks(info)
            events = self.store.conversation_events_all(chat_id, info['id'])
            prefix = self.checkpoint(info).get('seed', [])
            db = self.connection(info['file_id'], True)
            for index, task in enumerate(tasks):
                if task['id'] not in bases:
                    bases[task['id']] = prefix + [{key: point[key] for key in ('role', 'text', 'attachments')}
                        for point in points(tasks[:index], events)]
                context = self.task_payload(task['id'], info)['context']
                context['_regenerationSeed'] = bases[task['id']]
                with db:
                    db.execute('UPDATE turns SET context=? WHERE task_id=?', (json.dumps(context, ensure_ascii=False), task['id']))
        with self.store.db:
            self.store.db.execute('UPDATE chat_storage SET version=2 WHERE chat_id=?', (chat_id,))

    def needs_recovery(self, info):
        if self.store.db.execute('SELECT version FROM chat_storage WHERE chat_id=?', (info['chat_id'],)).fetchone()[0] < 2:
            return True
        if self.checkpoint(info).get('status') == 'pending':
            return True
        return bool(self.connection(info['file_id']).execute("SELECT 1 FROM header WHERE key='pendingCompaction'").fetchone())

    def set_checkpoint(self, info, checkpoint):
        db = self.connection(info['file_id'], True)
        with db:
            db.execute("INSERT OR REPLACE INTO header VALUES ('checkpoint',?)", (json.dumps(checkpoint, ensure_ascii=False),))

    def event_data(self, event_id):
        if self.scope:
            info = self.info(*self.scope)
            row = self.connection(info['file_id']).execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if row:
                return row
        row = self.store.db.execute('SELECT task_id FROM events WHERE id=?', (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        for location in self.store.db.execute('SELECT file_id FROM segment_task_refs WHERE task_id=?', (row[0],)):
            event = self.connection(location[0]).execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if event:
                return event
        raise KeyError(event_id)

    def replace_event(self, event_id, data, kind=None):
        location = self.event_data(event_id)
        self.reply_points_cache.clear()
        locations = [(self.info(*self.scope)['file_id'],)] if self.scope else self.store.db.execute('SELECT file_id FROM segment_task_refs WHERE task_id=?', (location['task_id'],))
        for row in locations:
            db = self.connection(row[0])
            if db.execute('SELECT 1 FROM events WHERE id=?', (event_id,)).fetchone():
                writable = self.connection(row[0], True)
                with writable:
                    if data is None:
                        writable.execute('DELETE FROM events WHERE id=?', (event_id,))
                    else:
                        writable.execute('UPDATE events SET data=?,kind=coalesce(?,kind) WHERE id=?', (json.dumps(data, ensure_ascii=False), kind, event_id))
                return
        raise KeyError(event_id)

    def references(self, chat_id, source_info, recovering=None):
        """Share immutable prefix files when publishing a branch."""
        if source_info['number'] > 1 and (recovering is None or 'files' in recovering):
            return self.branch_references(chat_id, source_info, recovering)
        active = recovering['active'] if recovering else self.info(chat_id)
        if source_info['number'] == 1:
            return active
        destination = chat_id + '-' + str(source_info['number'])
        journal = checked(self.home / 'chat-segments' / ('branch-' + chat_id + '.json'), self.home)
        if not recovering:
            temporary = journal.with_suffix('.new')
            with temporary.open('w', encoding='utf-8') as output:
                json.dump({'chat': chat_id, 'source': source_info, 'active': active}, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(journal)
        self.close_file(active['file_id'])
        if self.path(active['file_id']).exists():
            self.path(active['file_id']).replace(self.path(destination))
        if not self.path(destination).is_file():
            raise ValueError('Branch segment publication is incomplete')
        with self.store.db:
            self.store.db.execute('DELETE FROM chat_segments WHERE chat_id=?', (chat_id,))
            for row in self.store.db.execute('SELECT * FROM chat_segments WHERE chat_id=? AND number<? ORDER BY number', (source_info['chat_id'], source_info['number'])).fetchall():
                self.store.db.execute('INSERT INTO chat_segments VALUES (?,?,?,?,?,?)', (chat_id, row['number'], row['file_id'], row['start_event'], row['end_event'], 1))
            self.store.db.execute('INSERT INTO chat_segments VALUES (?,?,?,?,NULL,0)', (chat_id, source_info['number'], destination, active['start_event']))
            self.store.db.execute('UPDATE chat_storage SET latest=? WHERE chat_id=?', (source_info['number'], chat_id))
            self.store.db.execute('UPDATE task_segments SET file_id=? WHERE file_id=?', (destination, active['file_id']))
            self.store.db.execute('UPDATE segment_task_refs SET file_id=? WHERE file_id=?', (destination, active['file_id']))
            self.store.db.execute('UPDATE segment_attachments SET file_id=? WHERE file_id=?', (destination, active['file_id']))
        self.set_checkpoint(self.info(chat_id), self.checkpoint(source_info))
        db = self.connection(destination, True)
        with db:
            db.execute("UPDATE header SET value=? WHERE key='fileId'", (json.dumps(destination),))
            db.execute("UPDATE header SET value=? WHERE key='previous'", (json.dumps(chat_id + '-' + str(source_info['number'] - 1)),))
        journal.unlink()
        return self.info(chat_id)

    def branch_references(self, chat_id, source, recovering=None):
        """Offset every new segment, including compactions in a regenerated turn."""
        offset = source['number'] - 1
        journal = checked(self.home / 'chat-segments' / ('branch-' + chat_id + '.json'), self.home)
        pending = recovering or {'chat': chat_id, 'source': source, 'active': self.info(chat_id),
            'files': [dict(row) for row in self.store.db.execute('SELECT * FROM chat_segments WHERE chat_id=? ORDER BY number', (chat_id,))]}
        def save():
            temporary = journal.with_suffix('.new')
            with temporary.open('w', encoding='utf-8') as output:
                json.dump(pending, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(journal)
        if not recovering:
            save()
        self.close()
        if pending.get('phase') != 'staged':
            for row in pending['files']:
                path = self.path(row['file_id'])
                staged = path.with_suffix('.sqlite.branch')
                if path.exists():
                    path.replace(staged)
                if not staged.exists():
                    raise ValueError('Branch segment staging is incomplete')
            pending['phase'] = 'staged'
            save()
        for row in pending['files']:
            staged = self.path(row['file_id']).with_suffix('.sqlite.branch')
            destination = self.path(chat_id + '-' + str(row['number'] + offset))
            if staged.exists():
                staged.replace(destination)
            if not destination.exists():
                raise ValueError('Branch segment publication is incomplete')
        files = []
        for row in pending['files']:
            number = row['number'] + offset
            file_id = chat_id + '-' + str(number)
            files.append({**row, 'number': number, 'file_id': file_id})
            db = self.connection(file_id, True)
            with db:
                db.execute("UPDATE header SET value=? WHERE key='fileId'", (json.dumps(file_id),))
                db.execute("UPDATE header SET value=? WHERE key='previous'", (json.dumps(chat_id + '-' + str(number - 1)),))
        with self.store.db:
            self.store.db.execute('DELETE FROM chat_segments WHERE chat_id=?', (chat_id,))
            for row in self.store.db.execute('SELECT * FROM chat_segments WHERE chat_id=? AND number<?', (source['chat_id'], source['number'])).fetchall():
                self.store.db.execute('INSERT INTO chat_segments VALUES (?,?,?,?,?,1)', (chat_id, row['number'], row['file_id'], row['start_event'], row['end_event']))
            for row in pending['files']:
                for table in ('task_segments', 'segment_task_refs', 'segment_attachments'):
                    self.store.db.execute('DELETE FROM ' + table + ' WHERE file_id=?', (row['file_id'],))
            for row in files:
                self.store.db.execute('INSERT INTO chat_segments VALUES (?,?,?,?,?,?)',
                    (chat_id, row['number'], row['file_id'], row['start_event'], row['end_event'], row['sealed']))
                db = self.connection(row['file_id'])
                attachments = set()
                for turn in db.execute('SELECT * FROM turns'):
                    self.store.db.execute('INSERT OR REPLACE INTO task_segments VALUES (?,?)', (turn['task_id'], row['file_id']))
                    self.store.db.execute('INSERT OR IGNORE INTO segment_task_refs VALUES (?,?)', (row['file_id'], turn['task_id']))
                    attachments.update(json.loads(turn['value'])['request'].get('attachments', []))
                    for message in (json.loads(turn['seed']) if turn['seed'] else []) + json.loads(turn['context']).get('_regenerationSeed', []):
                        attachments.update(message.get('attachments', []))
                for event in db.execute("SELECT data FROM events WHERE kind='steering'"):
                    attachments.update(json.loads(event[0]).get('attachments', []))
                self.store.db.executemany('INSERT OR IGNORE INTO segment_attachments VALUES (?,?)', [(row['file_id'], ident) for ident in attachments])
            self.store.db.execute('UPDATE chat_storage SET latest=? WHERE chat_id=?', (files[-1]['number'], chat_id))
        journal.unlink()
        return self.info(chat_id)

    def shared_task(self, task_id, excluding):
        return bool(self.store.db.execute('SELECT 1 FROM segment_task_refs r JOIN chat_segments s ON s.file_id=r.file_id WHERE r.task_id=? AND s.chat_id NOT IN (' + ','.join('?' for _ in excluding) + ') LIMIT 1', (task_id, *excluding)).fetchone())

    def shared_attachment(self, attachment_id, excluding):
        return bool(self.store.db.execute('SELECT 1 FROM segment_attachments r JOIN chat_segments s ON s.file_id=r.file_id WHERE r.attachment_id=? AND s.chat_id NOT IN (' + ','.join('?' for _ in excluding) + ') LIMIT 1', (attachment_id, *excluding)).fetchone())

    def remove_chats(self, chats):
        from .secure_delete import erase_tree
        files = {row[0] for chat in chats for row in self.store.db.execute('SELECT file_id FROM chat_segments WHERE chat_id=?', (chat,))}
        for chat in chats:
            uuid.UUID(chat)
            erase_tree(self.home / 'chat-segments' / ('branch-' + chat + '.json'), self.home)
            files.update(path.stem for path in (self.home / 'chat-segments').glob(chat + '-*.sqlite'))
            for path in (self.home / 'chat-segments').glob(chat + '-*.sqlite.*.new'):
                erase_tree(path, self.home)
            for path in (self.home / 'chat-segments').glob(chat + '-*.sqlite.branch'):
                erase_tree(path, self.home)
        with self.store.db:
            for chat in chats:
                self.store.db.execute('DELETE FROM chat_segments WHERE chat_id=?', (chat,))
                self.store.db.execute('DELETE FROM chat_storage WHERE chat_id=?', (chat,))
                self.store.db.execute('DELETE FROM segment_pending WHERE chat_id=?', (chat,))
            for file_id in files:
                if self.store.db.execute('SELECT 1 FROM chat_segments WHERE file_id=?', (file_id,)).fetchone():
                    continue
                # A branch can be the last owner of a prefix whose original
                # task metadata was removed when the source chat was retired.
                for row in self.store.db.execute('SELECT task_id FROM segment_task_refs WHERE file_id=?', (file_id,)).fetchall():
                    if not self.store.db.execute('SELECT 1 FROM tasks WHERE id=?', (row[0],)).fetchone() and not self.shared_task(row[0], chats):
                        for folder in ('artifacts', 'controls', 'history-contexts'):
                            erase_tree(self.home / folder / row[0], self.home)
                if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='attachments'").fetchone():
                    for row in self.store.db.execute('SELECT attachment_id FROM segment_attachments WHERE file_id=?', (file_id,)).fetchall():
                        owner = self.store.db.execute('SELECT owner_task_id FROM attachments WHERE id=?', (row[0],)).fetchone()
                        if owner and owner[0] and not self.store.db.execute('SELECT 1 FROM tasks WHERE id=?', (owner[0],)).fetchone() and not self.shared_attachment(row[0], chats):
                            erase_tree(self.home / 'attachments' / row[0], self.home)
                            self.store.db.execute('DELETE FROM attachments WHERE id=?', (row[0],))
                self.close_file(file_id)
                erase_tree(self.path(file_id), self.home)
                for suffix in ('-wal', '-shm', '-journal'):
                    erase_tree(Path(str(self.path(file_id)) + suffix), self.home)
                self.store.db.execute('DELETE FROM task_segments WHERE file_id=?', (file_id,))
                self.store.db.execute('DELETE FROM segment_task_refs WHERE file_id=?', (file_id,))
                self.store.db.execute('DELETE FROM segment_attachments WHERE file_id=?', (file_id,))
