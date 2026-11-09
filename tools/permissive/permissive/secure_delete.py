"""Seven flushed overwrite passes for application-owned data only.

This is logical file erasure, not a guarantee for SSDs, snapshots or backups.
Shared SQLite files are rebuilt with surviving records before the old files
are overwritten, so unrelated conversations remain intact.
"""

import json
import hashlib
import os
import re
from pathlib import Path
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar


_deferred = ContextVar('deferred_secure_overwrites', default=False)


@contextmanager
def defer_overwrites():
    """Retire files now; a worker overwrites only these detached files later."""
    token = _deferred.set(True)
    try:
        yield
    finally:
        _deferred.reset(token)


def retire_for_overwrite(path, root):
    folder = checked(root / 'secure-erase-files', root)
    folder.mkdir(exist_ok=True)
    destination = checked(folder / (str(uuid.uuid4()) + '.erase'), root)
    retire_database(path, destination)
    return destination


def drain_overwrites(root):
    # Each rename is itself the durable queue entry. No database connection is
    # accessed by this worker, and interrupted files receive all seven passes.
    folder = checked(root / 'secure-erase-files', root)
    for path in folder.glob('*.erase'):
        uuid.UUID(path.stem)
        overwrite_file(path, root)


def save_rotation(path, temporary, old, root):
    folder = checked(root / 'secure-db-rotations', root)
    folder.mkdir(exist_ok=True)
    queue = checked(root / 'secure-erase-files', root)
    queue.mkdir(exist_ok=True)
    manifest = folder / (str(uuid.uuid4()) + '.json')
    value = {'path': str(path.relative_to(root)), 'clean': str(temporary.relative_to(root)),
             'old': str(old.relative_to(root)),
             'sidecars': {suffix: str((queue / (str(uuid.uuid4()) + '.erase')).relative_to(root))
                          for suffix in ('-wal', '-shm', '-journal')}}
    with manifest.open('x', encoding='utf-8') as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    return manifest, value


def recover_rotations(root):
    """Finish detaching old WAL files before opening a replacement database."""
    folder = checked(root / 'secure-db-rotations', root)
    for manifest in folder.glob('*.json'):
        uuid.UUID(manifest.stem)
        value = json.loads(checked(manifest, root).read_text(encoding='utf-8'))
        path, clean, old = (checked(root / value[key], root) for key in ('path', 'clean', 'old'))
        if path.suffix not in {'.sqlite', '.sqlite3'} or clean.parent != path.parent or old.parent != path.parent:
            raise ValueError('Invalid database rotation')
        if clean.exists():
            if not old.exists():
                retire_database(path, old)
            elif path.exists():
                raise ValueError('Database rotation has conflicting originals')
            for suffix, destination in value['sidecars'].items():
                if suffix not in {'-wal', '-shm', '-journal'}:
                    raise ValueError('Invalid database sidecar')
                sidecar, destination = checked(Path(str(path) + suffix), root), checked(root / destination, root)
                if sidecar.exists():
                    retire_database(sidecar, destination)
            clean.replace(path)
        if not path.exists():
            raise ValueError('Database rotation has no replacement')
        if old.exists():
            retire_for_overwrite(old, root)
        retire_for_overwrite(manifest, root)


def checked(path, root):
    path, root = Path(path), Path(root).absolute()
    # Reject reparse points anywhere along the path (including Windows junctions).
    current = path
    while current != root:
        if current == current.parent:
            raise ValueError("Deletion target is outside application storage")
        if current.exists() and (current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError("Deletion target contains a link or junction")
        current = current.parent
    allowed_roots = [root.resolve()]
    # Windows packaged-app storage can merge the nominal LocalAppData directory
    # with a virtualized directory: the root resolves nominally, but its files
    # resolve into LocalCache. Use the existing, non-linked task DB as the anchor
    # for that additional app-owned storage root. Child links remain forbidden.
    database = root / 'tasks.sqlite'
    if database.exists():
        if database.is_symlink() or getattr(database.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Application database cannot be a link or junction')
        allowed_roots.append(database.resolve().parent)
    else:
        # During rotation the existing app-owned database is at this retired
        # name. Keep the packaged-storage anchor until its clean successor is
        # installed, including recovery after a process stops between renames.
        for retired in root.glob('tasks.sqlite.*.erase'):
            try:
                uuid.UUID(retired.name.rsplit('.', 2)[1])
            except ValueError:
                continue
            if not retired.is_symlink() and not getattr(retired.lstat(), 'st_file_attributes', 0) & 0x400:
                allowed_roots.append(retired.resolve().parent)
    if not any(path.resolve().is_relative_to(allowed) for allowed in allowed_roots):
        raise ValueError("Deletion target is outside application storage")
    return path


def overwrite_file(path, root, remove=True):
    path = checked(path, root)
    if not path.exists():
        return
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Expected an application file")
    if path.stat().st_nlink > 1:
        raise ValueError('Cannot overwrite a file with other hard links')
    if _deferred.get():
        if not remove:
            raise ValueError('Live files cannot be deferred for overwrite')
        retire_for_overwrite(path, root)
        return
    size = path.stat().st_size
    with path.open('r+b', buffering=0) as handle:
        for _ in range(7):
            handle.seek(0)
            remaining = size
            while remaining:
                chunk = os.urandom(min(1024 * 1024, remaining))
                view = memoryview(chunk)
                while view:
                    written = handle.write(view)
                    if not written:
                        raise OSError("Overwrite did not make progress")
                    view = view[written:]
                remaining -= len(chunk)
            os.fsync(handle.fileno())
    if remove:
        path.unlink()


def erase_tree(path, root):
    path = checked(path, root)
    if not path.exists():
        return
    if path.is_dir():
        for child in path.iterdir():
            erase_tree(child, root)
        path.rmdir()
    else:
        overwrite_file(path, root)


def rebuild_database(connection, path, root):
    path = checked(path, root)
    if connection.execute('PRAGMA wal_checkpoint(FULL)').fetchone()[0]:
        raise ValueError("Database is busy; retry after other tasks finish")
    temporary = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.clean')
    old = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.erase')
    connection.execute('VACUUM INTO ?', (str(temporary),))
    with temporary.open('r+b') as handle:
        os.fsync(handle.fileno())
    if _deferred.get():
        # A final read-only connection leaves checkpointed WAL files on disk
        # when it closes. Preserve the actual files for seven-pass erasure,
        # rather than letting SQLite unlink them on the writer's last close.
        manifest, rotation = save_rotation(path, temporary, old, root)
        try:
            reader = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                reader.execute('SELECT name FROM sqlite_master LIMIT 1').fetchall()
                connection.close()
            finally:
                reader.close()
        except BaseException:
            retire_for_overwrite(manifest, root)
            raise
        retired = []
        try:
            retire_database(path, old)
            # Install no new connection until every old sidecar is detached.
            for suffix in ('-wal', '-shm', '-journal'):
                sidecar = checked(Path(str(path) + suffix), root)
                if sidecar.exists():
                    destination = checked(root / rotation['sidecars'][suffix], root)
                    retire_database(sidecar, destination)
                    retired.append((sidecar, destination))
            temporary.replace(path)
        except BaseException:
            # Nothing has been overwritten yet, so failed Windows renames can
            # restore a coherent database before the service reopens it.
            for sidecar, detached in reversed(retired):
                detached.replace(sidecar)
            if old.exists():
                old.replace(path)
            retire_for_overwrite(manifest, root)
            raise
        overwrite_file(old, root)
        overwrite_file(manifest, root)
        return
    # Closing SQLite normally unlinks its WAL. Overwrite its existing frames
    # only after checkpointing and saving the clean replacement, before close.
    # No queries run against this connection after this point.
    overwrite_file(Path(str(path) + '-wal'), root, remove=False)
    connection.close()
    # Windows can retain the just-closed SQLite handle briefly. A sharing
    # violation here must not expose a half-finished deletion to the client.
    retire_database(path, old)
    try:
        temporary.replace(path)
    except BaseException:
        old.replace(path)
        raise
    # Current database is usable even if erasure fails; retain .erase for retry.
    overwrite_file(old, root)
    for suffix in ('-wal', '-shm', '-journal'):
        overwrite_file(Path(str(path) + suffix), root)


def retire_database(path, old):
    for attempt in range(21):
        try:
            path.replace(old)
            return
        except OSError as error:
            if getattr(error, 'winerror', None) not in {32, 33} or attempt == 20:
                raise
            # SQLite cursors can survive in collected request/task cycles.
            if attempt == 0:
                import gc
                gc.collect()
            time.sleep(.1)


def replace_text(path, text, root):
    path = checked(path, root)
    clean = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.clean')
    retired = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.erase')
    with clean.open('w', encoding='utf-8', newline='') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    path.replace(retired)
    try:
        clean.replace(path)
    except BaseException:
        retired.replace(path)
        raise
    overwrite_file(retired, root)


def erase_native(home, threads):
    root = home / 'native-home'
    if not root.exists() or not threads:
        return
    checked(root, home)
    databases = list(root.glob('*.sqlite')) + list(root.glob('*.sqlite3'))
    marks = ','.join('?' for _ in threads)
    for path in databases:
        checked(path, home)
        connection = sqlite3.connect(path)
        try:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='thread_spawn_edges'").fetchone() and connection.execute(f'SELECT 1 FROM thread_spawn_edges WHERE parent_thread_id IN ({marks}) AND child_thread_id NOT IN ({marks}) LIMIT 1', tuple(threads) * 2).fetchone():
                raise ValueError('Another native chat depends on this history; delete that chat first')
        finally:
            connection.close()
    # Protect persisted forks that still read a selected rollout as their base.
    for directory in ('sessions', 'archived_sessions'):
        folder = checked(root / directory, home)
        if folder.exists():
            for path in folder.rglob('*.jsonl'):
                checked(path, home)
                with path.open('r', encoding='utf-8') as handle:
                    line = handle.readline(1024 * 1024)
                try:
                    meta = json.loads(line).get('payload', {})
                except (ValueError, TypeError):
                    continue
                if meta.get('id') not in threads and (meta.get('history_base') or {}).get('thread_id') in threads:
                    raise ValueError('Another native chat references this history; delete that chat first')
    # Rollouts and shell snapshots have the exact native UUID in their filename.
    for directory in ('sessions', 'archived_sessions', 'shell_snapshots'):
        folder = checked(root / directory, home)
        if folder.exists():
            for path in folder.rglob('*'):
                if any(path.name == thread or re.search(r'-' + re.escape(thread) + r'(?:_[0-9a-f-]{36})?\.jsonl(?:\.zst)?$', path.name) or path.name in (thread + '.sh', thread + '.ps1') for thread in threads):
                    erase_tree(path, home)
    for path in databases:
        checked(path, home)
        connection = sqlite3.connect(path)
        try:
            connection.execute('PRAGMA secure_delete=ON')
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            marks = ','.join('?' for _ in threads)
            with connection:
                for table in tables:
                    quote = '"' + table.replace('"', '""') + '"'
                    columns = [row[1] for row in connection.execute(f'PRAGMA table_info({quote})')]
                    keys = [column for column in columns if column in ('thread_id', 'parent_thread_id', 'child_thread_id', 'source_thread_id', 'assigned_thread_id') or table == 'threads' and column == 'id']
                    if keys:
                        where = ' OR '.join(f'"{key}" IN ({marks})' for key in keys)
                        connection.execute(f'DELETE FROM {quote} WHERE {where}', tuple(threads) * len(keys))
            rebuild_database(connection, path, home)
        finally:
            connection.close()
    # Native index is shared: preserve entries for other threads.
    index = checked(root / 'session_index.jsonl', home)
    if index.exists():
        lines = index.read_text(encoding='utf-8').splitlines(keepends=True)
        kept = [line for line in lines if json.loads(line).get('id') not in threads]
        replace_text(index, ''.join(kept), home)


def pending_path(home, ident, workspace=False):
    key = hashlib.sha256(('workspace:' if workspace else 'chat:').encode() + ident.encode()).hexdigest()
    return checked(home / 'secure-deletions' / (key + '.json'), home)


def recover_databases(home):
    recover_rotations(home)
    # A stopped process may have retired a DB before installing its clean replacement.
    for folder in (home, home / 'native-home', *list((home / 'logs' / 'workspaces').glob('*'))):
        checked(folder, home)
        if not folder.exists():
            continue
        for clean in folder.glob('*.clean'):
            parts = clean.name.rsplit('.', 2)
            if len(parts) != 3 or not parts[0].endswith(('.sqlite', '.sqlite3', '.jsonl')):
                continue
            try:
                uuid.UUID(parts[1])
            except ValueError:
                continue
            original = checked(folder / parts[0], home)
            if not original.exists():
                checked(clean, home).replace(original)


def erase_saved_data(store, conversations, workspace_id=None):
    manifest = pending_path(store.home, workspace_id or conversations[0], bool(workspace_id))
    if manifest.exists():
        pending = json.loads(manifest.read_text(encoding='utf-8'))
        tasks, conversations = pending['tasks'], pending['conversations']
        attachment_ids = pending.get('attachments', [])
    else:
        tasks = [task for ident in conversations for task in store.conversation_tasks(ident, all_segments=True)]
        attachment_ids = []
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='attachments'").fetchone():
            selected = {task['id'] for task in tasks}
            attachment_ids = [row['id'] for row in store.db.execute('SELECT * FROM attachments') if (row['owner_task_id'] in selected or workspace_id and row['workspace_id'] == workspace_id and row['owner_task_id'] is None) and not store.segments.shared_attachment(row['id'], conversations)]
    if any(task['status'] not in {'completed', 'cancelled', 'failed', 'interrupted'} for task in tasks):
        raise ValueError('Finish or cancel chats before deleting them')
    threads = {task['threadId'] for task in tasks if task['threadId']}
    ids = {task['id'] for task in tasks}
    # Never erase a native conversation still referenced by another saved chat.
    if any(row['id'] not in ids for thread in threads for row in store.db.execute('SELECT id FROM tasks WHERE thread_id=?', (thread,))):
        raise ValueError('Native history is shared with another chat')
    # Preflight all selected file trees before mutating records.
    paths = [store.home / folder / task['id'] for task in tasks if not store.segments.shared_task(task['id'], conversations) for folder in ('artifacts', 'controls', 'history-contexts')]
    paths += [store.home / 'attachments' / ident for ident in attachment_ids]
    paths += [store.home / 'logs' / 'workspaces' / ident for ident in {task['workspaceId'] for task in tasks} | ({workspace_id} if workspace_id else set())]
    for path in paths:
        checked(path, store.home)
        if path.is_dir():
            for child in path.rglob('*'):
                checked(child, store.home)
    manifest.parent.mkdir(exist_ok=True)
    if not manifest.exists():
        manifest.write_text(json.dumps({'tasks': [{key: task[key] for key in ('id', 'threadId', 'workspaceId', 'status')} for task in tasks], 'conversations': conversations, 'attachments': attachment_ids}), encoding='utf-8')
    # Retry any retired files from an interrupted erase before reporting success.
    for folder in (store.home, store.home / 'native-home', *[path for path in paths if path.parent.name == 'workspaces']):
        if folder.exists():
            for retired in folder.glob('*.erase'):
                parts = retired.name.rsplit('.', 2)
                if len(parts) == 3 and parts[0].endswith(('.sqlite', '.sqlite3', '.jsonl')):
                    try:
                        uuid.UUID(parts[1])
                    except ValueError:
                        continue
                    overwrite_file(retired, store.home)
    erase_native(store.home, threads)
    for path in paths:
        # Workspace diagnostic logs mix chats; sanitize selected records first.
        if path.parent.name == 'workspaces' and workspace_id is None and path.exists():
            for log in path.glob('*.jsonl'):
                lines = log.read_text(encoding='utf-8').splitlines(keepends=True)
                kept = [line for line in lines if json.loads(line).get('taskId') not in ids]
                replace_text(log, ''.join(kept), store.home)
        else:
            erase_tree(path, store.home)
    store.db.execute('PRAGMA secure_delete=ON')
    store.segments.remove_chats(conversations)
    with store.db:
        for ident in attachment_ids:
            store.db.execute('DELETE FROM attachments WHERE id=?', (ident,))
        for ident in conversations:
            store.db.execute('DELETE FROM reply_versions WHERE branch_id=?', (ident,))
            store.db.execute('DELETE FROM reply_selections WHERE branch_id=?', (ident,))
            store.db.execute('DELETE FROM chat_families WHERE branch_id=?', (ident,))
            store.db.execute('DELETE FROM regeneration_events WHERE branch_id=?', (ident,))
            store.db.execute('DELETE FROM regeneration_jobs WHERE branch_id=?', (ident,))
            store.db.execute('DELETE FROM conversation_metadata WHERE conversation_id=?', (ident,))
            store.db.execute('DELETE FROM followups WHERE conversation_id=?', (ident,))
            store.db.execute('DELETE FROM conversation_queues WHERE conversation_id=?', (ident,))
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='message_edits'").fetchone():
                store.db.execute('DELETE FROM message_edits WHERE conversation_id=?', (ident,))
        for ident in ids:
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='vm_attempts'").fetchone():
                store.db.execute('DELETE FROM vm_event_receipts WHERE attempt_id IN (SELECT attempt_id FROM vm_attempts WHERE task_id=?)', (ident,))
                store.db.execute('DELETE FROM vm_attempts WHERE task_id=?', (ident,))
                store.db.execute('DELETE FROM vm_journal_usage WHERE task_id=?', (ident,))
            store.db.execute('DELETE FROM steering WHERE task_id=?', (ident,))
            store.db.execute('DELETE FROM event_payload_sizes WHERE event_id IN (SELECT id FROM events WHERE task_id=?)', (ident,))
            store.db.execute('DELETE FROM events WHERE task_id=?', (ident,))
            store.db.execute('DELETE FROM tasks WHERE id=?', (ident,))
        if workspace_id:
            store.db.execute('DELETE FROM workspaces WHERE id=?', (workspace_id,))
    try:
        rebuild_database(store.db, store.home / 'tasks.sqlite', store.home)
    finally:
        # Reopen even if overwriting the retired file failed; never report success then.
        store.db.close()
        store.db = sqlite3.connect(store.home / 'tasks.sqlite')
        store.db.row_factory = sqlite3.Row
        store.db.execute('PRAGMA journal_mode=WAL')
        store.db.execute('PRAGMA foreign_keys=ON')
    overwrite_file(manifest, store.home)
    return {'deleted': True, 'passes': 7, 'conversationIds': list(conversations), 'taskIds': list(ids), 'threadIds': list(threads)}
