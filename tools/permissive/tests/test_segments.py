import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from permissive.store import Store
from permissive.segments import CheckpointRequired
from permissive.history import all_events, points, build_replacement, operation_key
from permissive.secure_delete import erase_saved_data


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_deleted_branch_recovery_marker_does_not_block_service_restart(self):
        task = self.task('retained prompt')
        self.answer(task, 'retained answer')
        orphan = str(uuid.uuid4())
        journal = self.home / 'chat-segments' / ('branch-' + orphan + '.json')
        journal.write_text(json.dumps({'chat': orphan, 'source': {'chat_id': task['id'], 'number': 4},
            'active': {'file_id': orphan + '-1', 'start_event': 0}}), encoding='utf-8')
        self.store.close()
        self.store = Store(self.home)
        self.assertFalse(journal.exists())
        self.assertEqual(self.texts(task['id']), ['retained prompt', 'retained answer'])

    def test_secure_delete_erases_interrupted_segment_staging_files(self):
        task = self.task('prompt')
        self.answer(task, 'answer')
        staged = self.home / 'chat-segments' / (task['id'] + '-1.sqlite.branch')
        staged.write_bytes(b'private transcript awaiting branch publication')
        erase_saved_data(self.store, [task['id']])
        self.assertFalse(staged.exists())

    def task(self, prompt, parent=None):
        task, _ = self.store.submit({'prompt': prompt, 'workingDirectory': str(self.home),
            'browserMode': 'dedicated', **({'resumeTaskId': parent['id']} if parent else {})})
        self.store.update(task['id'], thread_id=str(uuid.uuid4()))
        return task

    def answer(self, task, text, item=None):
        self.store.event(task['id'], 'agent', {'method': 'item/completed', 'params': {
            'turnId': 'turn', 'item': {'id': item or str(uuid.uuid4()), 'type': 'agentMessage', 'text': text}}})
        self.store.update(task['id'], status='completed', result=text)

    def compact(self, task, number=1):
        self.store.event(task['id'], 'agent', {'method': 'item/completed', 'params': {
            'turnId': 'turn', 'item': {'id': 'compaction-' + str(number), 'type': 'contextCompaction'}}})
        info = self.store.segments.info(task['conversationId'])
        self.store.segments.set_checkpoint(info, {'seed': [{'role': 'assistant', 'text': 'summary ' + str(number), 'attachments': []}], 'status': 'ready'})
        return info

    def texts(self, chat, segment=None):
        return [point['text'] for point in points(self.store.conversation_tasks(chat, segment),
                    self.store.conversation_events_all(chat, segment))]

    def chain(self):
        first = self.task('first prompt')
        self.answer(first, 'first answer')
        self.compact(first)
        second = self.task('second prompt', first)
        self.answer(second, 'second answer')
        return first, second

    def test_latest_only_and_one_previous_file(self):
        first, second = self.chain()
        self.compact(second, 2)
        third = self.task('third prompt', second)
        self.answer(third, 'third answer')
        self.store.segments.close()
        opened = []
        original = self.store.segments.connection
        def observe(file_id, write=False):
            opened.append(file_id)
            return original(file_id, write)
        with patch.object(self.store.segments, 'connection', observe):
            current = self.store.conversation(first['id'])
            self.assertEqual(current['segment']['number'], 3)
            self.assertEqual(self.texts(first['id']), ['third prompt', 'third answer'])
            self.assertEqual(set(opened), {first['id'] + '-3'})
            opened.clear()
            previous = self.store.conversation(first['id'], current['segment']['previousId'])
            self.assertEqual(self.texts(first['id'], previous['segment']['id']), ['second prompt', 'second answer'])
            self.assertEqual(set(opened), {first['id'] + '-2'})

    def test_compacted_native_continuation_does_not_reimport_history(self):
        first, second = self.chain()
        self.assertGreater(self.store.segments.info(first['id'])['number'], 1)
        self.store.set_history_seed(second['id'], None)
        child = self.store.submit({'prompt': 'Continue normally', 'workingDirectory': str(self.home),
            'browserMode': 'dedicated', 'resumeTaskId': second['id']})[0]
        self.assertIsNone(self.store.history_seed(child['id']))
        self.store.update(child['id'], status='completed')
        # Explicit edits still require their retained context to be imported.
        seed = [{'role': 'assistant', 'text': 'Edited context', 'attachments': []}]
        self.store.set_history_seed(child['id'], seed)
        edited = self.store.submit({'prompt': 'Continue edited history', 'workingDirectory': str(self.home),
            'browserMode': 'dedicated', 'resumeTaskId': child['id']})[0]
        self.assertEqual(self.store.history_seed(edited['id']), seed)

    def test_global_database_has_no_transcript_bodies(self):
        first, second = self.chain()
        dump = '\n'.join(self.store.db.iterdump())
        for text in ('first answer', 'second answer', 'second prompt', 'summary 1'):
            self.assertNotIn(text, dump)
        self.assertTrue(self.store.segments.path(first['id'] + '-1').is_file())
        self.assertTrue(self.store.segments.path(first['id'] + '-2').is_file())

    def test_modern_deprecated_and_repeated_compaction_are_one_boundary(self):
        task = self.task('prompt')
        self.compact(task)
        self.store.event(task['id'], 'agent', {'method': 'thread/compacted', 'params': {'turnId': 'turn'}})
        self.compact(task)
        self.assertEqual(self.store.segments.info(task['id'])['number'], 2)
        self.compact(task, 2)
        self.assertEqual(self.store.segments.info(task['id'])['number'], 3)

    def test_spanning_turn_hides_duplicate_prompt_and_retains_regeneration_base(self):
        task = self.task('original prompt')
        self.store.update(task['id'], status='running')
        self.answer(task, 'before compaction')
        self.compact(task)
        self.answer(task, 'after compaction')
        current = self.store.conversation(task['id'])
        self.assertEqual(current['tasks'][0]['request']['prompt'], '')
        self.assertEqual(self.texts(task['id']), ['after compaction'])
        body = {'taskId': task['id'], 'messageId': 'response', 'segmentId': current['segment']['id'],
                'submissionId': str(uuid.uuid4()), '_operation': 'regenerate'}
        replacement = build_replacement(self.home, False, task['id'], body, 'regenerate', operation_key(task['id'], body))
        latest = self.store.conversation_tasks(replacement)[-1]
        self.assertEqual(latest['request']['prompt'], 'original prompt')
        self.assertEqual(self.store.history_seed(latest['id']), [])
        self.assertEqual(self.store.segments.checkpoint(self.store.segments.info(replacement))['seed'], [])

    def test_regenerating_steering_in_spanning_task_keeps_current_checkpoint_and_prior_answer(self):
        task = self.task('original prompt')
        self.store.update(task['id'], status='running')
        self.answer(task, 'before compaction')
        self.compact(task)
        self.answer(task, 'retained after compaction')
        self.store.event(task['id'], 'steering', {'submissionId': str(uuid.uuid4()), 'prompt': 'selected later input', 'attachments': []})
        self.store.event(task['id'], 'agent', {'method': 'item/completed', 'params': {'item': {'id': 'later', 'type': 'agentMessage', 'text': 'old later answer'}}})
        self.store.update(task['id'], status='completed', result='old later answer')
        current = self.store.conversation(task['id'])
        body = {'taskId': task['id'], 'messageId': 'response', 'segmentId': current['segment']['id'],
                'submissionId': str(uuid.uuid4()), '_operation': 'regenerate'}
        replacement = build_replacement(self.home, False, task['id'], body, 'regenerate', operation_key(task['id'], body))
        latest = self.store.conversation_tasks(replacement)[-1]
        self.assertEqual(latest['request']['prompt'], 'selected later input')
        self.assertEqual([message['text'] for message in self.store.history_seed(latest['id'])], ['summary 1', 'retained after compaction'])
        self.assertEqual(self.texts(replacement), ['retained after compaction', 'selected later input'])
        self.assertEqual([message['text'] for message in self.store.segments.checkpoint(self.store.segments.info(replacement))['seed']], ['summary 1'])

    def test_older_regeneration_reads_only_selected_file_and_excludes_later_segments(self):
        first, second = self.chain()
        self.compact(second, 2)
        third = self.task('later forbidden prompt', second)
        self.answer(third, 'later forbidden answer')
        body = {'taskId': second['id'], 'messageId': 'response', 'segmentId': first['id'] + '-2',
                'submissionId': str(uuid.uuid4()), '_operation': 'regenerate'}
        opened = []
        from permissive.segments import Segments
        original = Segments.connection
        def observe(instance, file_id, write=False):
            opened.append(file_id)
            return original(instance, file_id, write)
        with patch.object(Segments, 'connection', observe):
            replacement = build_replacement(self.home, False, first['id'], body, 'regenerate', operation_key(first['id'], body))
        self.assertNotIn(first['id'] + '-1', opened)
        self.assertNotIn(first['id'] + '-3', opened)
        current = self.store.conversation(replacement)
        self.assertEqual(current['segment']['number'], 2)
        self.assertNotIn('forbidden', json.dumps(current))
        self.assertEqual(self.store.history_seed(current['tasks'][-1]['id'])[0]['text'], 'summary 1')
        self.assertEqual(build_replacement(self.home, False, first['id'], body, 'regenerate', operation_key(first['id'], body)), replacement)

    def test_fast_regeneration_of_older_segment_and_compaction_in_new_response(self):
        from permissive.reply_versions import prepare, copy_history, publish
        first, second = self.chain()
        self.compact(second, 2)
        later = self.task('later forbidden prompt', second)
        self.answer(later, 'later forbidden answer')
        body = dict(taskId=second['id'], messageId='response', segmentId=first['id'] + '-2',
                    submissionId=str(uuid.uuid4()), _operation='regenerate')
        opened = []
        original = self.store.segments.connection
        def observe(file_id, write=False):
            opened.append(file_id)
            return original(file_id, write)
        with patch.object(self.store.segments, 'connection', observe):
            branch, _ = prepare(self.store, first['id'], body, operation_key(first['id'], body, True))
        self.store.segments.scope = None
        self.assertNotIn(first['id'] + '-1', opened)
        self.assertNotIn(first['id'] + '-3', opened)
        job = dict(self.store.db.execute('SELECT * FROM regeneration_jobs WHERE branch_id=?', (branch,)).fetchone())
        task = self.store.get(job['task_id'])
        self.assertEqual(self.store.history_seed(task['id'])[0]['text'], 'summary 1')
        self.store.event(task['id'], 'agent', {'method': 'item/completed', 'params': {'turnId': 'new',
            'item': {'id': 'c', 'type': 'contextCompaction'}}})
        self.store.event(task['id'], 'agent', {'method': 'thread/compacted', 'params': {'turnId': 'new'}})
        self.answer(task, 'new response after compaction')
        # Native recovery normally supplies this checkpoint. Use a deterministic
        # stand-in to exercise numbering and publication without a model process.
        with patch.object(self.store.segments, 'recover_checkpoint', return_value={'seed': [{'role': 'assistant', 'text': 'new compacted context', 'attachments': []}], 'status': 'ready'}):
            copy_history(self.store, job)
            publish(self.store, job)
        self.store.segments.scope = None
        final = self.store.conversation(branch)
        self.assertEqual(final['segment']['number'], 3)
        self.assertEqual(self.store.segments.checkpoint(self.store.segments.info(branch))['seed'][0]['text'], 'new compacted context')
        self.assertIn('new response after compaction', self.texts(branch))
        self.assertNotIn('forbidden', json.dumps(final))
        self.assertEqual(self.texts(branch, branch + '-1'), ['first prompt', 'first answer'])
        self.assertEqual(len(final['replyVersions'][0]['versions']), 2)
        self.assertFalse(self.store.db.execute('SELECT 1 FROM history_deletions').fetchone())

    def test_shared_prefix_survives_source_delete_then_last_reference_is_erased(self):
        first, second = self.chain()
        body = {'taskId': second['id'], 'messageId': 'response', 'segmentId': first['id'] + '-2',
                'submissionId': str(uuid.uuid4())}
        replacement = build_replacement(self.home, False, first['id'], body, 'fork', operation_key(first['id'], body))
        old_file = self.store.segments.path(first['id'] + '-1')
        erase_saved_data(self.store, [first['id']])
        self.assertTrue(old_file.exists())
        self.assertIn('first answer', self.texts(replacement, replacement + '-1'))
        erase_saved_data(self.store, [replacement])
        self.assertFalse(old_file.exists())

    def legacy(self):
        task = self.task('legacy prompt')
        self.answer(task, 'legacy answer')
        raw = self.store.get(task['id'])
        events = all_events(self.store, task['id'])
        self.store.segments.close()
        with self.store.db:
            for table in ('chat_storage', 'chat_segments', 'task_segments', 'segment_task_refs'):
                self.store.db.execute('DELETE FROM ' + table)
            self.store.db.execute('UPDATE tasks SET request=?,result=? WHERE id=?', (json.dumps(raw['request']), raw['result'], raw['id']))
            for event in events:
                self.store.db.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(event['data']), event['id']))
            self.store.db.execute('INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,0)',
                (task['id'], 'agent', json.dumps({'method': 'item/completed', 'params': {'turnId': 't', 'item': {'id': 'c', 'type': 'contextCompaction'}}})))
        self.store.segments.path(task['id'] + '-1').unlink()
        return task

    def test_missing_checkpoint_is_reconstructed_once_and_migration_is_idempotent(self):
        task = self.legacy()
        calls = []
        self.store.segments.migrate(task['id'], lambda messages: calls.append(messages) or 'Recovered summary')
        self.assertEqual(len(calls), 1)
        self.store.segments.migrate(task['id'], lambda _: self.fail('Repeated summary'))
        self.assertEqual(self.store.segments.info(task['id'])['number'], 2)
        self.assertEqual(self.store.segments.checkpoint(self.store.segments.info(task['id']))['provenance'], 'reconstructed')

    def test_failed_checkpoint_reconstruction_keeps_legacy_authoritative(self):
        task = self.legacy()
        with self.assertRaises(CheckpointRequired):
            self.store.segments.migrate(task['id'])
        self.assertFalse(self.store.segments.ready(task['id']))
        self.assertEqual(self.store.get(task['id'])['request']['prompt'], 'legacy prompt')

    def test_legacy_migration_does_not_expand_history_before_checkpoint(self):
        first, second = self.chain()
        tasks = self.store.conversation_tasks(first['id'], all_segments=True)
        events = self.store.conversation_events_all(first['id'], first['id'] + '-1') + self.store.conversation_events_all(first['id'], first['id'] + '-2')
        self.store.segments.close()
        with self.store.db:
            for table in ('chat_storage', 'chat_segments', 'task_segments', 'segment_task_refs'):
                self.store.db.execute('DELETE FROM ' + table)
            for task in tasks:
                self.store.db.execute('UPDATE tasks SET request=?,result=? WHERE id=?', (json.dumps(task['request']), task['result'], task['id']))
            for event in events:
                self.store.db.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(event['data']), event['id']))
        self.store.segments.migrate(first['id'])
        seed = self.store.task_context(second['id'])['_regenerationSeed']
        self.assertEqual([point['text'] for point in seed], ['summary 1'])
        with self.store.db:
            self.store.db.execute('UPDATE chat_storage SET version=1 WHERE chat_id=?', (first['id'],))
        self.store.segments.recover(first['id'])
        self.assertEqual([point['text'] for point in self.store.task_context(second['id'])['_regenerationSeed']], ['summary 1'])

    def test_rollover_checkpoint_recovers_after_restart(self):
        task = self.task('prompt')
        self.answer(task, 'before')
        self.store.event(task['id'], 'agent', {'method': 'thread/compacted', 'params': {'turnId': 'turn'}})
        self.store.close()
        self.store = Store(self.home)
        info = self.store.segments.recover(task['id'], lambda _: 'Recovered live checkpoint')
        self.assertEqual(info['number'], 2)
        self.assertEqual(self.store.segments.checkpoint(info)['status'], 'ready')
        self.assertFalse(self.store.segments.needs_recovery(info))

    def test_branch_pointer_publication_recovers_after_header_write_failure(self):
        first, second = self.chain()
        branch = self.task('branch prompt')
        self.answer(branch, 'branch answer')
        source = self.store.segments.info(first['id'])
        original = self.store.segments.connection
        def interrupted(file_id, write=False):
            if write and file_id == branch['id'] + '-2':
                raise OSError('simulated interruption')
            return original(file_id, write)
        with patch.object(self.store.segments, 'connection', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.segments.references(branch['id'], source)
        self.store.close()
        self.store = Store(self.home)
        self.assertEqual(self.store.segments.info(branch['id'])['number'], 2)
        self.assertIn('first answer', self.texts(branch['id'], branch['id'] + '-1'))
        self.assertEqual(self.texts(branch['id']), ['branch prompt', 'branch answer'])

    def test_shared_steering_attachment_and_artifact_are_erased_after_last_reference(self):
        import base64
        from permissive.attachments import Attachments
        first = self.task('first')
        attachments = Attachments(self.store)
        attachment = attachments.upload({'workspaceId': first['workspaceId'], 'name': 'evidence.txt', 'data': base64.b64encode(b'evidence').decode()})['id']
        attachments.claim([attachment], first['id'])
        self.store.event(first['id'], 'steering', {'submissionId': str(uuid.uuid4()), 'prompt': 'read evidence', 'attachments': [attachment]})
        self.answer(first, 'done')
        artifact = self.home / 'artifacts' / first['id'] / 'result.txt'
        artifact.parent.mkdir(parents=True)
        artifact.write_text('result')
        self.compact(first)
        second = self.task('second', first)
        self.answer(second, 'second answer')
        body = dict(taskId=second['id'], messageId='response', segmentId=first['id'] + '-2', submissionId=str(uuid.uuid4()))
        branch = build_replacement(self.home, False, first['id'], body, 'fork', operation_key(first['id'], body))
        erase_saved_data(self.store, [first['id']])
        self.assertTrue(attachments.path(attachment).exists())
        self.assertTrue(artifact.exists())
        attachments.validate([attachment], first['workspaceId'], branch)
        erase_saved_data(self.store, [branch])
        self.assertFalse(artifact.exists())
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM attachments WHERE id=?', (attachment,)).fetchone())


if __name__ == '__main__':
    unittest.main()
