import json
import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
import uuid
from unittest.mock import patch

from permissive.portability import export_state, import_state, seal_secret, open_secret
from permissive.vm_runtime import atomic_json
from permissive.store import Store


class PortableStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / 'source'
        self.home.mkdir()
        atomic_json(self.home / 'environment.json', {'environmentId': str(uuid.uuid4()), 'generation': 4, 'phase': 'exported'})

    def tearDown(self):
        self.temporary.cleanup()

    def test_wal_backup_and_destination_rebind_preserve_history(self):
        database = sqlite3.connect(self.home / 'tasks.sqlite')
        try:
            database.execute('PRAGMA journal_mode=WAL')
            database.execute('CREATE TABLE tasks(id TEXT, result TEXT)')
            database.execute('INSERT INTO tasks VALUES (?,?)', ('stable-id', 'Unicode \u2603'))
            database.commit()
            (self.home / 'private-key.dpapi').write_text('must not travel')
            (self.home / 'service.lock').write_text('must not travel')
            artifact = self.home / 'artifacts/task/result.txt'
            artifact.parent.mkdir(parents=True)
            artifact.write_text('exported result')
            exported = self.root / 'bundle'
            export_state(self.home, exported)
            self.assertFalse((exported / 'private-key.dpapi').exists())
            self.assertFalse((exported / 'service.lock').exists())
            destination = self.root / 'different-host/home'
            vm_id = str(uuid.uuid4())
            result = import_state(exported, destination, vm_id)
            self.assertEqual(result['generation'], 5)
            with closing(sqlite3.connect(destination / 'tasks.sqlite')) as imported:
                self.assertEqual(imported.execute('SELECT * FROM tasks').fetchone(), ('stable-id', 'Unicode \u2603'))
            self.assertEqual(json.loads((destination / 'environment.json').read_text())['vmId'], vm_id)
            self.assertEqual((destination / 'artifacts/task/result.txt').read_text(), 'exported result')
        finally:
            database.close()

    def test_unfenced_export_and_nonempty_destination_refused(self):
        atomic_json(self.home / 'environment.json', {'phase': 'ready'})
        with self.assertRaisesRegex(ValueError, 'Fence'):
            export_state(self.home, self.root / 'bundle')

    def test_phone_enrollment_and_local_companion_authority_do_not_travel(self):
        companion = self.home / 'companion'
        companion.mkdir()
        (companion / 'state.sqlite').write_bytes(b'phone keys, invitations and installation identity')
        (companion / 'gateway.lock').write_bytes(b'0')
        auth = self.home / 'auth'
        auth.mkdir()
        atomic_json(auth / 'authorized-keys.json', {'version': 1, 'keys': [
            {'name': 'Local companion API client', 'id': 'source-companion'},
            {'name': 'Remote automation', 'id': 'portable-client'},
        ]})
        exported = self.root / 'bundle'
        export_state(self.home, exported)
        self.assertFalse((exported / 'companion').exists())
        transferred = json.loads((exported / 'auth/authorized-keys.json').read_text())
        self.assertEqual([key['id'] for key in transferred['keys']], ['portable-client'])

    def test_corrupt_transfer_rejected_before_destination_copy(self):
        exported = self.root / 'bundle'
        export_state(self.home, exported)
        (exported / 'environment.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            import_state(exported, self.root / 'destination', str(uuid.uuid4()))
        self.assertFalse((self.root / 'destination').exists())

    def test_parent_escape_rejected(self):
        exported = self.root / 'bundle'
        export_state(self.home, exported)
        manifest_path = exported / 'controller-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['../outside'] = 'a' * 64
        (self.root / 'outside').write_text('outside')
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'path'):
            import_state(exported, self.root / 'destination', str(uuid.uuid4()))

    def test_secret_envelope_is_portable_authenticated_and_password_protected(self):
        envelope = seal_secret('guest-only credential', 'migration-passphrase')
        self.assertNotIn('guest-only', json.dumps(envelope))
        self.assertEqual(open_secret(envelope, 'migration-passphrase'), 'guest-only credential')
        with self.assertRaises(Exception):
            open_secret(envelope, 'wrong passphrase')

    def make_skill(self, directory, text='Read references/example.txt.'):
        skill = directory / 'SKILL.md'
        directory.mkdir(parents=True)
        skill.write_text('---\nname: portable\ndescription: Portable fixture\n---\n' + text, encoding='utf-8')
        support = directory / 'references/example.txt'
        support.parent.mkdir()
        support.write_text('RELATIVE-SUPPORT-CONTENT', encoding='utf-8')
        return skill, support

    def update_manifest_hash(self, exported, name):
        path = exported / 'controller-manifest.json'
        value = json.loads(path.read_text())
        value['files'][name] = hashlib.sha256((exported / name).read_bytes()).hexdigest()
        atomic_json(path, value)

    def test_external_selected_skill_trees_rebind_without_changing_source(self):
        skill, support = self.make_skill(self.root / 'source-user/personal-skill')
        library, _ = self.make_skill(self.root / 'source-user/library-skill', 'Library-only selected package.')
        with closing(Store(self.home, guest_paths=True)) as store:
            workspace = store.save_workspace({'name': 'Guest', 'workingDirectory': r'C:\workspaces\project', 'skills': [str(skill)]})
            task = store.submit({'workspaceId': workspace['id'], 'workingDirectory': workspace['workingDirectory'], 'prompt': 'Keep task history', 'skills': [str(skill)]})[0]
            with store.db:
                store.db.execute('INSERT INTO skill_library VALUES (?,?)', (str(library), 'Personal'))
            source_context = store.task_context(task['id'])
            exported = self.root / 'bundle'
            export_state(self.home, exported)
            self.assertEqual(store.workspace(workspace['id'])['skills'], [str(skill)])
            self.assertEqual(store.task_context(task['id']), source_context)
            self.assertEqual(store.get(task['id'])['request']['skills'], [str(skill)])
            self.assertEqual(store.db.execute('SELECT path FROM skill_library').fetchone()[0], str(library))
            metadata = json.loads((exported / 'portable-skills.json').read_text())
            self.assertEqual(len(metadata['entries']), 2)
            with closing(sqlite3.connect(exported / 'tasks.sqlite')) as snapshot:
                selected = json.loads(snapshot.execute('SELECT skills FROM workspaces WHERE id=?', (workspace['id'],)).fetchone()[0])[0]
                self.assertTrue(selected.startswith('@controller/skills/imported/'))
            destination = self.root / 'destination-user/relocated-controller'
            import_state(exported, destination, str(uuid.uuid4()))
            with closing(Store(destination, guest_paths=True)) as imported:
                paths = imported.workspace(workspace['id'])['skills']
                self.assertEqual(len(paths), 1)
                actual = Path(paths[0])
                self.assertTrue(actual.is_relative_to(destination / 'skills/imported'))
                self.assertEqual(actual.read_text(encoding='utf-8'), skill.read_text(encoding='utf-8'))
                self.assertEqual((actual.parent / 'references/example.txt').read_text(), support.read_text())
                context = imported.task_context(task['id'])['skills'][0]
                self.assertEqual(context['path'], str(actual))
                self.assertEqual(context['sourcePath'], str(skill))
                self.assertEqual(imported.get(task['id'])['request']['skills'], paths)
                imported_library = imported.db.execute('SELECT path,category FROM skill_library').fetchone()
                self.assertTrue(Path(imported_library[0]).is_relative_to(destination / 'skills/imported'))
                self.assertEqual(imported_library[1], 'Personal')

    def test_missing_historical_skill_keeps_text_but_cannot_resolve_destination_file(self):
        missing = str(self.root / 'old-machine-missing/SKILL.md')
        with closing(Store(self.home, guest_paths=True)) as store:
            workspace = store.workspaces()['data'][0]
            task = store.submit({'workspaceId': workspace['id'], 'prompt': 'Historical task'})[0]
            context = {'systemPrompt': '', 'skills': [{'name': 'legacy', 'path': missing, 'content': 'HISTORICAL-SKILL-TEXT'}], 'revision': 1}
            with store.db:
                store.db.execute('UPDATE tasks SET workspace_context=? WHERE id=?', (json.dumps(context), task['id']))
            exported = self.root / 'bundle'
            export_state(self.home, exported)
            self.assertEqual(store.task_context(task['id'])['skills'][0]['path'], missing)
            destination = self.root / 'destination'
            import_state(exported, destination, str(uuid.uuid4()))
            with closing(Store(destination, guest_paths=True)) as imported:
                skill = imported.task_context(task['id'])['skills'][0]
                self.assertEqual(skill['content'], 'HISTORICAL-SKILL-TEXT')
                self.assertEqual(skill['sourcePath'], missing)
                self.assertIsNone(skill['path'])
                self.assertTrue(skill['portableUnavailable'])

    def test_configured_missing_skill_fails_clearly(self):
        skill, _ = self.make_skill(self.root / 'missing-active')
        with closing(Store(self.home, guest_paths=True)) as store:
            workspace = store.save_workspace({'name': 'Configured', 'workingDirectory': r'C:\workspaces', 'skills': [str(skill)]})
            skill.unlink()
            with self.assertRaisesRegex(ValueError, 'Configured skill is unavailable'):
                export_state(self.home, self.root / 'bundle')
            self.assertEqual(store.workspace(workspace['id'])['skills'], [str(skill)])

    def test_import_rejects_active_skill_path_outside_portable_metadata(self):
        with closing(Store(self.home, guest_paths=True)) as store:
            workspace = store.workspaces()['data'][0]
            exported = self.root / 'bundle'
            export_state(self.home, exported)
        unrelated, _ = self.make_skill(self.root / 'destination-user/unrelated-private-skill')
        with closing(sqlite3.connect(exported / 'tasks.sqlite')) as database, database:
            database.execute('UPDATE workspaces SET skills=? WHERE id=?', (json.dumps([str(unrelated)]), workspace['id']))
        self.update_manifest_hash(exported, 'tasks.sqlite')
        destination = self.root / 'destination'
        with self.assertRaisesRegex(ValueError, 'outside portable skill metadata'):
            import_state(exported, destination, str(uuid.uuid4()))
        self.assertFalse(destination.exists())

    def test_supporting_reparse_files_are_rejected_before_copy(self):
        skill, support = self.make_skill(self.root / 'selected')
        with closing(Store(self.home, guest_paths=True)) as store:
            store.save_workspace({'name': 'Configured', 'workingDirectory': r'C:\workspaces', 'skills': [str(skill)]})
            original = Path.lstat
            def lstat(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                if path == support:
                    return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
                return result
            with patch('permissive.portability.Path.lstat', lstat), self.assertRaisesRegex(ValueError, 'reparse'):
                export_state(self.home, self.root / 'bundle')

    def test_import_rejects_traversal_inside_skill_metadata(self):
        skill, _ = self.make_skill(self.root / 'selected')
        with closing(Store(self.home, guest_paths=True)) as store:
            store.save_workspace({'name': 'Configured', 'workingDirectory': r'C:\workspaces', 'skills': [str(skill)]})
            exported = self.root / 'bundle'
            export_state(self.home, exported)
        metadata_path = exported / 'portable-skills.json'
        metadata = json.loads(metadata_path.read_text())
        metadata['entries'][0]['path'] = '@controller/skills/imported/' + 'a' * 64 + '/../SKILL.md'
        atomic_json(metadata_path, metadata)
        self.update_manifest_hash(exported, 'portable-skills.json')
        with self.assertRaisesRegex(ValueError, 'portable skill path'):
            import_state(exported, self.root / 'destination', str(uuid.uuid4()))


if __name__ == '__main__':
    unittest.main()
