import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from permissive.store import Store
from permissive.secure_delete import erase_saved_data, overwrite_file, checked, pending_path, retire_database, defer_overwrites, drain_overwrites, save_rotation, recover_databases


class SecureDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def task(self, secret, workspace=None):
        body = {'prompt': secret, 'workingDirectory': str(self.home)}
        if workspace:
            body['workspaceId'] = workspace
        task = self.store.submit(body)[0]
        self.store.event(task['id'], 'agent', {'private': secret})
        self.store.update(task['id'], status='completed', result=secret, thread_id=task['id'])
        return self.store.get(task['id'])

    def test_seven_flushed_passes_before_unlink(self):
        path = self.home / 'fixture'
        path.write_bytes(b'secret' * 100)
        with patch('permissive.secure_delete.os.fsync') as sync, patch('permissive.secure_delete.os.urandom', return_value=b'x' * 600) as random:
            overwrite_file(path, self.home)
            self.assertEqual(sync.call_count, 7)
            self.assertEqual(random.call_count, 7)
        self.assertFalse(path.exists())

    def test_deferred_cleanup_detaches_actual_wal_and_keeps_live_database_writable(self):
        secret = 'DEFERRED_PRIVATE_WAL_RECORD_723864'
        deleted, kept = self.task(secret), self.task('keep')
        with defer_overwrites():
            erase_saved_data(self.store, [deleted['conversationId']])
        queue = list((self.home / 'secure-erase-files').glob('*.erase'))
        self.assertTrue(any(secret.encode() in path.read_bytes() for path in queue))
        self.assertEqual(self.store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.store.rename_conversation(kept['conversationId'], 'Written during cleanup')
        with patch('permissive.secure_delete.os.fsync') as sync:
            drain_overwrites(self.home)
        self.assertEqual(sync.call_count, 7 * len(queue))
        self.assertFalse(list((self.home / 'secure-erase-files').glob('*.erase')))
        self.assertEqual(self.store.conversation(kept['conversationId'])['title'], 'Written during cleanup')

    def test_interrupted_rotation_retires_old_wal_before_installing_clean_database(self):
        task = self.task('old private record')
        path = self.home / 'tasks.sqlite'
        clean = path.with_name('tasks.sqlite.' + str(uuid.uuid4()) + '.clean')
        old = path.with_name('tasks.sqlite.' + str(uuid.uuid4()) + '.erase')
        self.store.db.execute('PRAGMA wal_checkpoint(FULL)')
        self.store.db.execute('VACUUM INTO ?', (str(clean),))
        manifest, rotation = save_rotation(path, clean, old, self.home)
        self.store.close()
        path.replace(old)
        # A stale sidecar must never be attached to the replacement on restart.
        Path(str(path) + '-wal').write_bytes(b'private old WAL')
        recover_databases(self.home)
        self.assertFalse(Path(str(path) + '-wal').exists())
        self.assertFalse(manifest.exists())
        self.store = Store(self.home)
        self.assertEqual(self.store.get(task['id'])['result'], 'old private record')
        drain_overwrites(self.home)

    def test_failed_deferred_sidecar_rename_restores_usable_database_for_retry(self):
        task = self.task('retry background deletion')
        original = retire_database
        def fail_sidecar(path, target):
            if path.name.endswith('-wal'):
                raise OSError('fixture locked WAL')
            return original(path, target)
        with patch('permissive.secure_delete.retire_database', side_effect=fail_sidecar), defer_overwrites():
            with self.assertRaisesRegex(OSError, 'locked WAL'):
                erase_saved_data(self.store, [task['conversationId']])
        self.assertEqual(self.store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertFalse(list((self.home / 'secure-db-rotations').glob('*.json')))
        with defer_overwrites():
            self.assertTrue(erase_saved_data(self.store, [task['conversationId']])['deleted'])
        drain_overwrites(self.home)

    def test_windows_sharing_violation_is_retried_without_repeating_erasure(self):
        path, retired = self.home / 'fixture.sqlite', self.home / 'fixture.erase'
        path.write_text('surviving database')
        original = Path.replace
        attempts = []
        def transient_lock(source, target):
            attempts.append(source)
            if len(attempts) < 3:
                error = PermissionError('Windows SQLite handle is closing')
                error.winerror = 32
                raise error
            return original(source, target)
        with patch.object(Path, 'replace', transient_lock), patch('permissive.secure_delete.time.sleep') as sleep:
            retire_database(path, retired)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(retired.read_text(), 'surviving database')
        with patch.object(Path, 'replace', side_effect=PermissionError('real permission failure')), patch('permissive.secure_delete.time.sleep') as sleep:
            with self.assertRaises(PermissionError):
                retire_database(retired, path)
            sleep.assert_not_called()

    def test_chat_erasure_preserves_other_data_and_reopens(self):
        secret = 'UNIQUE_PRIVATE_CHAT_CONTENT_912874'
        deleted, kept = self.task(secret), self.task('keep me')
        self.store.rename_conversation(deleted['conversationId'], secret)
        artifacts = self.home / 'artifacts' / deleted['id']
        artifacts.mkdir(parents=True)
        (artifacts / 'screenshot.png').write_bytes(secret.encode())
        project = self.home / 'project-file.txt'
        project.write_text('project stays')
        native = self.home / 'native-home'
        native.mkdir()
        sessions = native / 'sessions' / '2026'
        sessions.mkdir(parents=True)
        rollout = sessions / ('rollout-date-' + deleted['threadId'] + '.jsonl')
        rollout.write_text(secret)
        db = sqlite3.connect(native / 'state_5.sqlite')
        db.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT); CREATE TABLE thread_items(thread_id TEXT, item_json TEXT);')
        db.execute('INSERT INTO threads VALUES (?,?)', (deleted['threadId'], secret))
        db.execute('INSERT INTO threads VALUES (?,?)', (kept['threadId'], 'retained'))
        db.execute('INSERT INTO thread_items VALUES (?,?)', (deleted['threadId'], secret))
        db.commit(); db.close()
        logs = self.home / 'logs' / 'workspaces' / deleted['workspaceId']
        logs.mkdir(parents=True)
        (logs / 'diagnostics.jsonl').write_text(json.dumps({'taskId': deleted['id'], 'data': secret}) + '\n' + json.dumps({'taskId': kept['id'], 'data': 'retain log'}) + '\n')
        result = erase_saved_data(self.store, [deleted['conversationId']])
        self.assertEqual(result['passes'], 7)
        with self.assertRaises(KeyError):
            self.store.get(deleted['id'])
        self.assertEqual(self.store.get(kept['id'])['result'], 'keep me')
        self.assertFalse(rollout.exists())
        self.assertFalse(artifacts.exists())
        self.assertEqual(project.read_text(), 'project stays')
        self.store.close()
        self.store = Store(self.home)
        self.assertEqual(self.store.get(kept['id'])['result'], 'keep me')
        for path in self.home.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_workspace_erasure_removes_all_turns_but_preserves_other_workspace(self):
        workspace = self.store.save_workspace({'name': 'Delete', 'workingDirectory': str(self.home)})
        first = self.task('first', workspace['id'])
        follow = self.store.submit({'prompt': 'second', 'workspaceId': workspace['id'], 'resumeTaskId': first['id']})[0]
        self.store.update(follow['id'], status='completed')
        other = self.store.save_workspace({'name': 'Keep', 'workingDirectory': str(self.home)})
        keep = self.task('keep', other['id'])
        self.store.rename_conversation(first['conversationId'], 'Deleted workspace title')
        self.store.rename_conversation(keep['conversationId'], 'Retained title')
        erase_saved_data(self.store, [first['conversationId']], workspace['id'])
        with self.assertRaises(KeyError): self.store.workspace(workspace['id'])
        with self.assertRaises(KeyError): self.store.get(follow['id'])
        self.assertEqual(self.store.get(keep['id'])['result'], 'keep')
        self.assertEqual(self.store.conversation(keep['conversationId'])['title'], 'Retained title')
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM conversation_metadata WHERE conversation_id=?', (first['conversationId'],)).fetchone())

    def test_active_chat_and_external_paths_are_rejected(self):
        task = self.store.submit({'prompt': 'active', 'workingDirectory': str(self.home)})[0]
        with self.assertRaises(ValueError): erase_saved_data(self.store, [task['conversationId']])
        with self.assertRaises(ValueError): checked(self.home.parent / 'outside', self.home)
        self.assertEqual(self.store.get(task['id'])['status'], 'queued')

    def test_windows_merged_app_storage_uses_existing_database_as_anchor(self):
        physical = self.home.parent / ('virtualized-' + self.home.name)
        native = self.home / 'native-home'
        resolve = Path.resolve
        def virtualized(path, *args, **kwargs):
            if path == self.home / 'tasks.sqlite':
                return physical / 'tasks.sqlite'
            if path == native:
                return physical / 'native-home'
            return resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', virtualized):
            self.assertEqual(checked(native, self.home), native)
            with self.assertRaises(ValueError):
                checked(physical / 'unselected', self.home)

    def test_existing_wal_is_overwritten_before_sqlite_closes_it(self):
        task = self.task('private WAL fixture')
        observed = []
        original = overwrite_file
        def observe(path, root, **kwargs):
            if path.name == 'tasks.sqlite-wal' and kwargs.get('remove') is False:
                observed.append(path.stat().st_size)
            return original(path, root, **kwargs)
        with patch('permissive.secure_delete.overwrite_file', side_effect=observe):
            erase_saved_data(self.store, [task['conversationId']])
        self.assertTrue(observed and observed[0] > 0)
        self.assertEqual(self.store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_native_fork_reference_prevents_removing_rollouts(self):
        task = self.task('shared history')
        folder = self.home / 'native-home' / 'sessions'
        folder.mkdir(parents=True)
        original = folder / ('rollout-date-' + task['threadId'] + '.jsonl')
        original.write_text(json.dumps({'payload': {'id': task['threadId']}}))
        (folder / 'fork.jsonl').write_text(json.dumps({'payload': {'id': 'external', 'history_base': {'thread_id': task['threadId']}}}))
        with self.assertRaisesRegex(ValueError, 'references'):
            erase_saved_data(self.store, [task['conversationId']])
        self.assertTrue(original.exists())
        self.assertEqual(self.store.get(task['id'])['result'], 'shared history')

    def test_failed_retired_file_erase_can_retry_after_rows_are_removed(self):
        task = self.task('erase me')
        original = overwrite_file
        def fail(path, root, **kwargs):
            if path.name.endswith('.erase'):
                raise OSError('fixture disk error')
            original(path, root, **kwargs)
        with patch('permissive.secure_delete.overwrite_file', side_effect=fail):
            with self.assertRaises(OSError): erase_saved_data(self.store, [task['conversationId']])
        self.assertTrue(pending_path(self.home, task['conversationId']).exists())
        self.assertEqual(erase_saved_data(self.store, [task['conversationId']])['deleted'], True)
        self.assertFalse(list(self.home.glob('*.erase')))
